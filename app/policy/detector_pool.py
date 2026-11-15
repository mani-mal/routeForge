"""Where detector work runs: inline, or on a dedicated single-worker executor
(B118).

PII detection with Presidio is CPU-bound; on a 56k-token prompt one pass takes
~16 s. Run on the asyncio event loop it froze `/health`, so the liveness probe
killed the pod. So every request-path detector call -- the gate, the
per-message redacted view, step-9 `verify`, egress tool-call redaction, a
cache-hit re-verify, the incremental streaming verifier, telemetry/console
`redacted_copy` -- goes through `run_detector_for`, which decides:

* **Work that can reach a Presidio engine** (or any detector this module does
  not know to be cheap -- fail-safe) runs on `_EXECUTOR`, so no such call runs
  on the event-loop thread.
* **NullDetector / RegexDetector work** (optionally wrapped in the per-app
  Filtered/Effective layers) runs inline, exactly as cheap as it always was.
  Queueing it behind a 16 s Presidio pass would only add latency.

Why a dedicated executor and not `asyncio.to_thread`: the default executor is
also where the loop resolves DNS (`getaddrinfo`) for httpx/LiteLLM. Presidio
work queued there -- or parked on the engine lock -- would starve outbound
connects. One worker, so work runs in FIFO submission order and one engine is
only ever used by one thread; `PresidioDetector`'s own lock stays as defence
in depth.

Deliberately NOT here yet (backlog B119): a bound on the queue (fail-closed 503
when full) and a per-engine executor. A caller cancelled while its work is
still QUEUED does get that work dropped: asyncio cancels the pending
concurrent future, so it never starts.

**Timing (B119 step 1, measurement only).** Every call is timed against a
per-request `DetectorTiming` accumulator held in a `ContextVar`
(`start_request_timing` installs one at the top of the chat route). For an
executor job: *queue* = submission (loop) to the worker starting it, *run* =
the worker's start to finish, and the number of jobs already in flight at
submission (`queue_depth_max`). Inline (regex/off) work counts as *inline*
with no queue. The worker only writes two floats on a per-job `_JobClock`;
the accumulator itself is only ever mutated on the loop thread, after the
job's future has resolved. With no accumulator installed (boot warm-up,
offline scripts) nothing is recorded. Nothing here changes what runs where,
or in what order.
"""
from __future__ import annotations

import asyncio
import contextvars
import functools
import json
import threading
import time
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any

from app.obs import metrics

from app.policy.detectors import NullDetector, RegexDetector
from app.policy.effective import EffectiveDetector, FilteredDetector

#: Thread-name prefix of the detector worker; tests use it to prove that
#: detector work ran here and not on the loop or in the default executor.
THREAD_NAME_PREFIX = "rf-detector"

_EXECUTOR = ThreadPoolExecutor(max_workers=1, thread_name_prefix=THREAD_NAME_PREFIX)

#: Hop label used when a caller names none.
UNLABELLED = "other"


# --- per-request timing (B119 step 1) ----------------------------------------


@dataclass
class DetectorTiming:
    """One request's detector timings, summed. Milliseconds, floats until
    `attributes()` rounds them. `by_hop[label]` is
    `[executor_jobs, queue_ms, run_ms, inline_ms]`. Content-free: counts,
    durations and fixed hop labels only."""
    jobs: int = 0
    queue_ms: float = 0.0
    run_ms: float = 0.0
    inline_calls: int = 0
    inline_ms: float = 0.0
    queue_depth_max: int = 0
    by_hop: dict[str, list[float]] = field(default_factory=dict)

    def _hop(self, label: str) -> list[float]:
        return self.by_hop.setdefault(label, [0, 0.0, 0.0, 0.0])

    def add_job(self, label: str, queue_ms: float, run_ms: float, depth: int) -> None:
        self.jobs += 1
        self.queue_ms += queue_ms
        self.run_ms += run_ms
        self.queue_depth_max = max(self.queue_depth_max, depth)
        hop = self._hop(label)
        hop[0] += 1
        hop[1] += queue_ms
        hop[2] += run_ms

    def add_inline(self, label: str, run_ms: float) -> None:
        self.inline_calls += 1
        self.inline_ms += run_ms
        self._hop(label)[3] += run_ms

    @property
    def empty(self) -> bool:
        return self.jobs == 0 and self.inline_calls == 0

    def by_hop_json(self) -> str:
        """`{label: [jobs, queue_ms, run_ms, inline_ms]}`, rounded ms ints."""
        return json.dumps({label: [int(v[0]), round(v[1]), round(v[2]), round(v[3])]
                           for label, v in sorted(self.by_hop.items())},
                          separators=(",", ":"))

    def attributes(self) -> dict[str, int | str]:
        """The `sp.detector_*` trace attributes (rounded ms ints)."""
        return {
            "sp.detector_queue_ms": round(self.queue_ms),
            "sp.detector_run_ms": round(self.run_ms),
            "sp.detector_inline_ms": round(self.inline_ms),
            "sp.detector_jobs": self.jobs,
            "sp.detector_queue_depth_max": self.queue_depth_max,
            "sp.detector_by_hop": self.by_hop_json(),
        }


_TIMING: contextvars.ContextVar[DetectorTiming | None] = contextvars.ContextVar(
    "routeforge_detector_timing", default=None)


def start_request_timing() -> DetectorTiming:
    """Install a fresh accumulator for the current request and return it.
    Call it in the request's own task before any task or stream generator is
    spawned for it: those copy the context, so they all see this object."""
    timing = DetectorTiming()
    _TIMING.set(timing)
    return timing


def current_timing() -> DetectorTiming | None:
    """The current request's accumulator, or None outside a request."""
    return _TIMING.get()


class _JobClock:
    """Written once by the worker (start, end); read by the loop only after
    the job's future has resolved."""
    __slots__ = ("started", "ended")

    def __init__(self) -> None:
        self.started: float | None = None
        self.ended: float | None = None


def _timed_call[T](clock: _JobClock, call: Callable[[], T]) -> T:
    clock.started = time.perf_counter()
    try:
        return call()
    finally:
        clock.ended = time.perf_counter()


#: Jobs submitted through `run_detector` and not yet finished (queued or
#: running). Incremented on submission, decremented by the future's done
#: callback -- which also fires for a job cancelled while still queued.
_in_flight = 0
_in_flight_lock = threading.Lock()


def _admit() -> int:
    """Count a new job in; return how many were in flight ahead of it."""
    global _in_flight
    with _in_flight_lock:
        ahead = _in_flight
        _in_flight += 1
    return ahead


def _release(_future: Future | None = None) -> None:
    global _in_flight
    with _in_flight_lock:
        _in_flight -= 1


def _record_job(label: str, submitted: float, clock: _JobClock, depth: int) -> None:
    """On the loop, after the job's future resolved. A job that never started
    (cancelled while queued) or is still running (its awaiter was cancelled)
    has no complete clock and is not recorded."""
    if clock.started is None or clock.ended is None:
        return
    queue_s = max(0.0, clock.started - submitted)
    run_s = max(0.0, clock.ended - clock.started)
    metrics.observe_detector_job(label, queue_s, run_s)
    timing = _TIMING.get()
    if timing is not None:
        timing.add_job(label, queue_s * 1000.0, run_s * 1000.0, depth)


async def run_detector[T](fn: Callable[..., T], /, *args: Any,
                          hop_label: str = UNLABELLED, **kwargs: Any) -> T:
    """Run `fn(*args, **kwargs)` on the detector executor and await its result.

    Same semantics as `asyncio.to_thread`: the caller's contextvars travel with
    the call, and an exception raised by `fn` is re-raised here unchanged, so
    a caller's fail-closed handler sees exactly what it saw before.

    `hop_label` names the call site for the timing breakdown (B119); it is
    never passed to `fn`. `executor.submit` + `wrap_future` is exactly what
    `loop.run_in_executor` does, spelled out so the job's concurrent future
    can carry the in-flight counter's done callback."""
    loop = asyncio.get_running_loop()
    call = functools.partial(contextvars.copy_context().run, fn, *args, **kwargs)
    clock = _JobClock()
    depth = _admit()
    submitted = time.perf_counter()
    try:
        future = _EXECUTOR.submit(_timed_call, clock, call)
    except BaseException:
        _release()
        raise
    future.add_done_callback(_release)
    try:
        return await asyncio.wrap_future(future, loop=loop)
    finally:
        _record_job(hop_label, submitted, clock, depth)


#: Detector types known to be cheap enough to run inline on the loop. Matched
#: by EXACT type: a subclass, Presidio, or anything unrecognised is treated as
#: expensive and goes to the executor (the fail-safe direction).
_INLINE_TYPES = (NullDetector, RegexDetector)


def needs_detector_executor(detector: object) -> bool:
    """True when work with `detector` can reach an expensive (Presidio or
    unknown) engine. Unwraps the per-app Filtered/Effective layers -- their own
    work is regex-cheap; what matters is the base underneath. `None` (no
    detector ran) is cheap."""
    while isinstance(detector, (FilteredDetector, EffectiveDetector)):
        detector = detector._base
    return detector is not None and type(detector) not in _INLINE_TYPES


async def run_detector_for[T](detector: object, fn: Callable[..., T], /,
                              *args: Any, hop_label: str = UNLABELLED,
                              **kwargs: Any) -> T:
    """`fn(*args, **kwargs)` on the detector executor when `detector` needs it
    (`needs_detector_executor`), inline otherwise. Exceptions propagate
    unchanged either way.

    `hop_label` (never passed to `fn`) names the call site for the B119
    timing breakdown. Inline work is timed as the whole inline call; with
    `detector is None` no detector runs at all and nothing is recorded."""
    if needs_detector_executor(detector):
        return await run_detector(fn, *args, hop_label=hop_label, **kwargs)
    timing = _TIMING.get() if detector is not None else None
    if timing is None:
        return fn(*args, **kwargs)
    started = time.perf_counter()
    try:
        return fn(*args, **kwargs)
    finally:
        timing.add_inline(hop_label, (time.perf_counter() - started) * 1000.0)
