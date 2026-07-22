"""In-process asyncio pub/sub for the Phase-6 UI's live decision stream
(Phase 6a Task 2).

`DecisionBus` fans each published event out to every currently-subscribed
`asyncio.Queue`. Queues are bounded: a slow/stalled browser tab must never
be able to block request serving by filling an unbounded queue, so a full
queue drops its OLDEST entry (not the new one) before enqueueing — the
demo prioritizes showing recent activity over completeness for a laggy
viewer.

Events published here carry the same redaction-safe `RequestSummary` /
types-only trace records already produced by `HistoryStore`
(app/api/history.py) — this module never sees or stores raw PII or CCR
contents; it's a plain fan-out mailbox.
"""

from __future__ import annotations

import asyncio

_DEFAULT_MAXSIZE = 1000


class DecisionBus:
    def __init__(self, maxsize: int = _DEFAULT_MAXSIZE) -> None:
        self._maxsize = maxsize
        self._subscribers: set[asyncio.Queue] = set()

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=self._maxsize)
        self._subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._subscribers.discard(q)

    async def publish(self, event: dict) -> None:
        for q in list(self._subscribers):
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                # Drop the oldest queued event to make room, then enqueue
                # the new one — a slow subscriber falls behind rather than
                # ever blocking the publisher (which runs on the request
                # serving path).
                try:
                    q.get_nowait()
                except asyncio.QueueEmpty:
                    pass
                try:
                    q.put_nowait(event)
                except asyncio.QueueFull:
                    # Another producer raced us and refilled the queue
                    # between our get_nowait() and put_nowait(); skip this
                    # subscriber for this event rather than loop/block.
                    pass
