"""Semantic cache substrate (feature #6, Task 1) -- a LOCAL, TTL-bounded,
tenant+residency-partitioned cache of already-verified responses. CCR-class
(CLAUDE.md rule 6): local only, TTL-enforced, contents never exported to
telemetry (only a counts-only `app.models.CacheRecord` reaches a trace).

Mirrors `app/compression/ccr.py::CCRStore`'s TTL sweep-on-write / evict-on-
read / honesty-counter machinery, but keys on a CONTENT-DERIVED normalized
query instead of an opaque uuid, and additionally partitions by residency: a
cache hit skips the model entirely, so serving a cloud-produced answer to an
onprem_only request would be as real a Rule-3 breach as routing the live call
to the cloud. See docs/architecture/semantic-cache-residency-spec.md
(guarantees C1-C8; this module builds C2, C3, C5, C6, C8's typed seam).

NOT wired into the pipeline -- Task 2 does the check/store calls. This module
exists so config/lint/tests can land ahead of the pipeline change, default
OFF (config/serving.yaml semantic_cache.enabled: false). No embedding library
is imported here; SemanticCacheBackend is a typed seam for a future backend
only (C8, deferred).
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass
from typing import Callable, Protocol

from app.api.history import _LOCAL_PROVIDERS

# partition = (tenant_id, onprem_only, allowed_providers). A tuple of
# immutable members is itself a frozen/hashable key -- no separate class
# needed to make the partition "frozen".
CachePartition = tuple[str, bool, frozenset[str]]

_WHITESPACE_RUN = re.compile(r"\s+")


def _normalize(query: str) -> str:
    """Deterministic exact-ish match key: lowercase, strip, collapse internal
    whitespace runs to a single space. This is the ONLY matching strategy
    built now (C4/C8) -- no stemming, no embedding similarity. "Hello  World"
    and "hello world" match; anything else is a genuinely different query."""
    return _WHITESPACE_RUN.sub(" ", query.strip().lower())


def _partition(tenant_id: str, onprem_only: bool,
               allowed_providers: frozenset[str]) -> CachePartition:
    if not tenant_id:
        raise ValueError("semantic cache requires a non-empty tenant_id (C2: "
                          "tenant isolation is not optional)")
    return (tenant_id, onprem_only, frozenset(allowed_providers))


def _is_residency_consistent(onprem_only: bool, provider: str) -> bool:
    """C3, load-bearing: an onprem_only partition may only ever hold (or
    serve) entries produced by a local provider. Shared by both store()
    (write-side refusal) and lookup() (read-side re-check) so the rule holds
    even if an entry somehow ended up in an onprem_only partition some other
    way -- lookup does not merely trust that store behaved."""
    return not onprem_only or provider in _LOCAL_PROVIDERS


@dataclass(frozen=True)
class CacheEntry:
    response_text: str  # verified, redacted text -- caller's contract (C1/C4)
    model: str
    provider: str        # the producing provider; entries SELF-DESCRIBE residency
    timestamp: float


class SemanticCacheBackend(Protocol):
    """The seam a future fuzzy/embedding backend implements (C8, deferred).
    DeterministicSemanticCache (exact/normalized-query match) is the only
    backend built now."""

    def lookup(self, *, tenant_id: str, onprem_only: bool,
               allowed_providers: frozenset[str],
               query: str) -> CacheEntry | None: ...

    def store(self, *, tenant_id: str, onprem_only: bool,
              allowed_providers: frozenset[str], query: str,
              response_text: str, model: str, provider: str) -> None: ...

    @property
    def size(self) -> int: ...

    @property
    def hits(self) -> int: ...

    @property
    def served_from_cloud_entry(self) -> int: ...


class DeterministicSemanticCache:
    """Deterministic exact/normalized-query match cache. See module docstring
    and docs/architecture/semantic-cache-residency-spec.md."""

    def __init__(self, ttl_seconds: int,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self._ttl = ttl_seconds
        self._clock = clock
        self._data: dict[CachePartition, dict[str, CacheEntry]] = {}
        self._hits = 0
        self._served_from_cloud_entry = 0

    def store(self, *, tenant_id: str, onprem_only: bool,
              allowed_providers: frozenset[str], query: str,
              response_text: str, model: str, provider: str) -> None:
        # Opportunistic TTL sweep on every write, mirroring CCRStore, so the
        # cache stays bounded even in steady state where nothing ever looks
        # up (CLAUDE.md rule 6: enforced TTL, not an unbounded liability).
        self._sweep_expired()
        if not _is_residency_consistent(onprem_only, provider):
            # C3 write-side refusal: never let a cloud-produced entry enter
            # an onprem_only partition in the first place.
            return
        part = _partition(tenant_id, onprem_only, allowed_providers)
        bucket = self._data.setdefault(part, {})
        bucket[_normalize(query)] = CacheEntry(
            response_text=response_text, model=model, provider=provider,
            timestamp=self._clock())

    def _sweep_expired(self) -> None:
        now = self._clock()
        empty_partitions = []
        for part, bucket in self._data.items():
            expired = [k for k, e in bucket.items()
                       if now - e.timestamp > self._ttl]
            for k in expired:
                del bucket[k]
            if not bucket:
                empty_partitions.append(part)
        for part in empty_partitions:
            del self._data[part]

    def lookup(self, *, tenant_id: str, onprem_only: bool,
               allowed_providers: frozenset[str],
               query: str) -> CacheEntry | None:
        part = _partition(tenant_id, onprem_only, allowed_providers)
        bucket = self._data.get(part)
        if bucket is None:
            return None
        key = _normalize(query)
        entry = bucket.get(key)
        if entry is None:
            return None
        if self._clock() - entry.timestamp > self._ttl:
            del bucket[key]  # enforce TTL: evict on access
            if not bucket:
                del self._data[part]
            return None
        # C3, LOAD-BEARING: an onprem_only lookup can NEVER return a
        # cloud-produced entry, no matter how it got into this partition.
        # store()'s own refusal makes this branch unreachable via the public
        # API -- it is kept as the structural check inside lookup itself
        # (defense in depth), and the honesty counter records it if it ever
        # fires, exactly like CCR's retrievals_to_cloud.
        if not _is_residency_consistent(onprem_only, entry.provider):
            self._served_from_cloud_entry += 1
            return None
        self._hits += 1
        return entry

    @property
    def size(self) -> int:
        return sum(len(bucket) for bucket in self._data.values())

    @property
    def hits(self) -> int:
        return self._hits

    @property
    def served_from_cloud_entry(self) -> int:
        return self._served_from_cloud_entry
