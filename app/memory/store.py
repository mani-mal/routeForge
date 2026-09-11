"""Persistent memory substrate (mem0-style, step-5 extension) -- CCR-class
residency posture (CLAUDE.md rule 6): local, TTL-bounded, tenant-partitioned,
sanitized-only, NEVER exported. See
docs/architecture/specs/persistent-memory-residency-spec.md for the binding
contract (R1-R7).

`DeterministicMemoryStore` mirrors `app.compression.ccr.CCRStore` exactly:
in-process dict, TTL sweep-on-write + evict-on-read,
`retrievals`/`retrievals_to_cloud` honesty counters. It is the only backend
built now. A future mem0-backed store would implement the same `MemoryStore`
protocol behind a lazy import and a `[memory]` extra (mirrors
DeterministicCompressor <-> HeadroomCompressor) -- NOT built here. It would
additionally need a LOCAL embedder + LOCAL vector store to satisfy residency,
an air-gap deployment concern out of scope for this task (R6).

This module is substrate only: nothing here is wired into the serving
pipeline (Task 2 wires read/write in), and no caller constructs a store yet.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable, Protocol


@dataclass(frozen=True)
class MemoryEntry:
    # `text` is a sanitized summary only -- storing raw input/output here
    # would violate R1. That is the CALLER's contract (Task 2 guarantees it,
    # mirroring how CCRStore trusts the compressor to pass sanitized text);
    # this module cannot itself verify sanitization.
    text: str
    timestamp: float


class MemoryStore(Protocol):
    """The seam a future backend (e.g. mem0) implements. `write` persists a
    sanitized summary into a tenant-partitioned store; `recall` returns up to
    `max_items` most-recent sanitized summaries for the SAME partition only
    -- tenant isolation (R2) is structural, never a filter applied after a
    broader scan."""

    def write(self, text: str, *, tenant_id: str, application_id: str = "",
              session_id: str = "") -> None: ...

    def recall(self, *, tenant_id: str, application_id: str = "",
               session_id: str = "", max_items: int,
               to_cloud: bool = False) -> list[str]: ...

    @property
    def size(self) -> int: ...

    @property
    def retrievals(self) -> int: ...

    @property
    def retrievals_to_cloud(self) -> int: ...


PartitionKey = tuple[str, str, str]


def _partition_key(tenant_id: str, application_id: str, session_id: str) -> PartitionKey:
    # tenant_id is MANDATORY and always the leading component of the key --
    # a recall for tenant B can structurally never observe tenant A's
    # entries (R2), because they never share a dict key, not because of a
    # post-hoc filter. The key is the native (tenant_id, application_id,
    # session_id) tuple -- NOT a joined/delimited string. Python tuple
    # equality compares component-by-component with no separator character,
    # so distinct tuples can never collide onto the same key regardless of
    # what characters an id contains: unlike a joined string, this is
    # collision-free (injective) by construction.
    if not tenant_id:
        raise ValueError("tenant_id is required for a memory partition key")
    return (tenant_id, application_id, session_id)


class DeterministicMemoryStore:
    """The default (and only, for now) `MemoryStore`. Mirrors `CCRStore`'s
    residency posture exactly: in-process dict, TTL sweep-on-write +
    evict-on-read, honesty counters. "Relevant" here means recency within the
    tenant partition -- most-recent-first, capped at `max_items`. No
    embeddings, no cross-partition scan."""

    def __init__(self, ttl_seconds: int,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self._ttl = ttl_seconds
        self._clock = clock
        self._data: dict[PartitionKey, list[MemoryEntry]] = {}
        self._retrievals = 0
        self._retrievals_to_cloud = 0

    def write(self, text: str, *, tenant_id: str, application_id: str = "",
              session_id: str = "") -> None:
        # Opportunistic TTL sweep on every write, exactly like CCRStore.store
        # -- keeps the store bounded even when nothing ever recalls.
        self._sweep_expired()
        key = _partition_key(tenant_id, application_id, session_id)
        self._data.setdefault(key, []).append(
            MemoryEntry(text=text, timestamp=self._clock()))

    def _sweep_expired(self) -> None:
        now = self._clock()
        for key in list(self._data):
            kept = [e for e in self._data[key] if now - e.timestamp <= self._ttl]
            if kept:
                self._data[key] = kept
            else:
                del self._data[key]

    def recall(self, *, tenant_id: str, application_id: str = "",
               session_id: str = "", max_items: int,
               to_cloud: bool = False) -> list[str]:
        if not tenant_id:
            raise ValueError("tenant_id is required for a memory partition key")
        key = _partition_key(tenant_id, application_id, session_id)
        now = self._clock()
        entries = self._data.get(key, [])
        live = [e for e in entries if now - e.timestamp <= self._ttl]
        if live:
            self._data[key] = live
        elif key in self._data:
            del self._data[key]  # enforce TTL: evict fully-expired partition on read

        self._retrievals += 1
        if to_cloud:
            self._retrievals_to_cloud += 1

        if max_items <= 0:
            return []
        return [e.text for e in reversed(live[-max_items:])]  # most-recent-first, capped

    @property
    def retrievals(self) -> int:
        return self._retrievals

    @property
    def retrievals_to_cloud(self) -> int:
        return self._retrievals_to_cloud

    @property
    def size(self) -> int:
        return sum(len(v) for v in self._data.values())
