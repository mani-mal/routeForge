"""CCR (Conditional Context Retrieval) — a LOCAL, TTL-bounded cache of
compression originals. Hard rules (CLAUDE.md rule 6 / headroom skill):
local-only, TTL-enforced, contents NEVER exported to telemetry, and for
`onprem_only` requests `retrievals_to_cloud` must always stay 0 (enforced by
the caller, which never retrieves to_cloud for onprem_only)."""
from __future__ import annotations

import time
import uuid
from typing import Callable


class CCRStore:
    def __init__(self, ttl_seconds: int,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self._ttl = ttl_seconds
        self._clock = clock
        self._data: dict[str, tuple[str, float]] = {}
        self._retrievals = 0
        self._retrievals_to_cloud = 0

    def store(self, original: str) -> str:
        # Opportunistic TTL sweep on every write so the cache is bounded even
        # when nothing ever retrieves (the demo's steady state — retrieval is a
        # later flow). Without this, "enforced TTL" (CLAUDE.md rule 6) would be
        # a claim the code doesn't back: expired originals would linger for the
        # whole process lifetime.
        self._sweep_expired()
        cid = uuid.uuid4().hex
        self._data[cid] = (original, self._clock())
        return cid

    def _sweep_expired(self) -> None:
        now = self._clock()
        expired = [cid for cid, (_, stamped) in self._data.items()
                   if now - stamped > self._ttl]
        for cid in expired:
            del self._data[cid]

    def retrieve(self, ccr_id: str, *, to_cloud: bool) -> str | None:
        entry = self._data.get(ccr_id)
        if entry is None:
            return None
        original, stamped = entry
        if self._clock() - stamped > self._ttl:
            del self._data[ccr_id]  # enforce TTL: evict on access
            return None
        self._retrievals += 1
        if to_cloud:
            self._retrievals_to_cloud += 1
        return original

    @property
    def retrievals(self) -> int:
        return self._retrievals

    @property
    def retrievals_to_cloud(self) -> int:
        return self._retrievals_to_cloud

    @property
    def size(self) -> int:
        return len(self._data)
