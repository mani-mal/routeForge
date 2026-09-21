"""The observation log's in-process half (signal hardening, spec §3.2).

`SignalStore` hands every observation it APPLIES to an `ObservationSink`, so
the persisted log can never disagree with the live signal. The sink is on the
serving path: `put` does no I/O, never blocks and never raises. A lifespan
task (`app/main.py`) drains it into `endpoint_observations`.

An observation carries no tenant, request id or content -- endpoint health
only, the same bounded-label stance as GET /metrics.
"""
from __future__ import annotations

import threading
from collections import deque
from dataclasses import dataclass


@dataclass(frozen=True)
class Observation:
    ts: float                  # wall-clock epoch seconds
    model: str
    outcome: str               # "ok" | "error"
    latency_ms: float | None   # non-streamed ok only
    streamed: bool
    ttft_ms: float | None      # streamed ok only; logged, never scored


class ObservationSink:
    def __init__(self, max_buffer: int) -> None:
        self._buf: deque[Observation] = deque(maxlen=max_buffer)
        self._lock = threading.Lock()
        self._dropped = 0

    def put(self, obs: Observation) -> None:
        with self._lock:
            if len(self._buf) == self._buf.maxlen:
                self._dropped += 1          # deque(maxlen) evicts the oldest
            self._buf.append(obs)

    def drain(self) -> list[Observation]:
        with self._lock:
            out = list(self._buf)
            self._buf.clear()
            return out

    def take_dropped(self) -> int:
        with self._lock:
            n, self._dropped = self._dropped, 0
            return n
