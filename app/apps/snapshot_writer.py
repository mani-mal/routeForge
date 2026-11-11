"""Buffered writer for the console's `request_snapshots` cache (B86).

The request path used to write each request's snapshot (summary + full
response) and then prune the table to its newest `SNAPSHOT_CAPACITY` rows,
synchronously, in its own session and commit. Now the request only hands the
snapshot to this writer (no I/O); a background loop writes the buffer in one
transaction every `flush_interval_s` and prunes every `prune_every` written
snapshots, not every request.

Durability and visibility (B3):

- A snapshot reaches the shared table at most one flush interval after its
  request (default 1 s, `SP_SNAPSHOT_FLUSH_S`), so another replica's console
  can lag by that much.
- This process's own console reads flush first (`app/api/ui_routes.py`), so
  the replica that served a request always shows it.
- Graceful shutdown flushes whatever is buffered.
- The cross-replica live-stream NOTIFY for each request is sent inside the
  flush transaction, so another replica never looks a snapshot up before it
  exists (`app/api/pg_bridge.py`).
- Like the old inline write, a failed flush is logged and swallowed (the
  snapshot cache must never break serving); the batch is dropped. The buffer is
  bounded at the table's own capacity -- older entries would be pruned anyway.
"""
from __future__ import annotations

import asyncio
import copy
import datetime as dt
import logging
import os

from app.apps import store
from app.apps.models import utcnow

_log = logging.getLogger("serving.obs")

FLUSH_INTERVAL_S = float(os.environ.get("SP_SNAPSHOT_FLUSH_S", "1.0"))
PRUNE_EVERY = 100


class SnapshotWriter:
    def __init__(self, db, *, capacity: int, bridge=None,
                 flush_interval_s: float = FLUSH_INTERVAL_S,
                 prune_every: int = PRUNE_EVERY) -> None:
        self._db = db
        self._bridge = bridge
        self.capacity = capacity
        self.flush_interval_s = flush_interval_s
        self.prune_every = prune_every
        self._buf: list[dict] = []
        self._since_prune = 0
        self._lock: asyncio.Lock | None = None
        self._stop: asyncio.Event | None = None
        self._task: asyncio.Task | None = None
        self.dropped = 0
        self.prunes = 0

    def enqueue(self, *, request_id: str, application_id: str,
                summary_json: dict, full_json: dict,
                ts: dt.datetime | None = None) -> None:
        """Buffer one snapshot. No I/O, never raises on the serving path's
        behalf beyond a programming error. `full_json` is copied now, so a
        later change to the response dict cannot alter what is stored."""
        self._buf.append({
            "request_id": request_id, "application_id": application_id,
            "summary_json": summary_json, "full_json": copy.deepcopy(full_json),
            "ts": ts or utcnow()})
        overflow = len(self._buf) - max(self.capacity, 1)
        if overflow > 0:
            del self._buf[:overflow]
            self.dropped += overflow
            _log.warning("snapshot buffer full; dropped %d oldest snapshot(s)", overflow)

    def pending(self) -> int:
        return len(self._buf)

    async def flush(self, *, prune: bool = False) -> int:
        """Write everything buffered in one transaction (plus the prune when
        due, or when `prune` and anything was written since the last one).
        Waits for a flush already in flight, so a caller that flushes before
        reading sees every snapshot this process had buffered. Never raises;
        returns the number of snapshots written."""
        if self._lock is None:
            self._lock = asyncio.Lock()
        async with self._lock:
            batch, self._buf = self._buf, []
            do_prune = (self._since_prune + len(batch) >= self.prune_every
                        or (prune and self._since_prune + len(batch) > 0))
            if not batch and not do_prune:
                return 0
            try:
                await self._write(batch, prune=do_prune)
            except Exception:
                if len(batch) <= 1:
                    _log.exception("snapshot flush failed; %d snapshot(s) dropped "
                                   "(swallowed)", len(batch))
                    return 0
                # One bad row (e.g. its application was deleted meanwhile) must
                # not cost the whole batch: retry each row on its own, as the
                # per-request write used to be.
                _log.warning("snapshot batch flush failed; retrying %d row(s) singly",
                             len(batch), exc_info=True)
                written = 0
                for row in batch:
                    try:
                        await self._write([row], prune=False)
                        written += 1
                    except Exception:
                        _log.exception("snapshot write failed request=%s (swallowed)",
                                       row["request_id"])
                self._since_prune += written
                return written
            if do_prune:
                self._since_prune = 0
                self.prunes += 1
            else:
                self._since_prune += len(batch)
            return len(batch)

    async def _write(self, batch: list[dict], *, prune: bool) -> None:
        async with self._db.session() as s:
            await store.record_snapshots(s, batch)
            if prune:
                await store.prune_snapshots(s, capacity=self.capacity)
            if batch and self._bridge is not None:
                await self._bridge.notify_in(s, [r["request_id"] for r in batch])
            await s.commit()

    async def _loop(self) -> None:
        stop = self._stop
        while not stop.is_set():
            try:
                await asyncio.wait_for(stop.wait(), self.flush_interval_s)
            except asyncio.TimeoutError:
                pass
            if stop.is_set():
                break   # stop() does the final flush
            await self.flush()

    def start(self) -> None:
        """Start the background flush loop (call on the serving loop)."""
        self._stop = asyncio.Event()
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        """Stop the loop (never mid-flush) and flush what is buffered. Never
        raises."""
        if self._stop is not None and self._task is not None:
            self._stop.set()
            try:
                await asyncio.wait_for(self._task, timeout=self.flush_interval_s + 5)
            except (Exception, asyncio.CancelledError):
                _log.warning("snapshot flusher did not stop cleanly", exc_info=True)
        await self.flush(prune=True)
