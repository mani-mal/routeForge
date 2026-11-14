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
"""
from __future__ import annotations

import asyncio
import contextvars
import functools
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from app.policy.detectors import NullDetector, RegexDetector
from app.policy.effective import EffectiveDetector, FilteredDetector

#: Thread-name prefix of the detector worker; tests use it to prove that
#: detector work ran here and not on the loop or in the default executor.
THREAD_NAME_PREFIX = "rf-detector"

_EXECUTOR = ThreadPoolExecutor(max_workers=1, thread_name_prefix=THREAD_NAME_PREFIX)


async def run_detector[T](fn: Callable[..., T], /, *args: Any, **kwargs: Any) -> T:
    """Run `fn(*args, **kwargs)` on the detector executor and await its result.

    Same semantics as `asyncio.to_thread`: the caller's contextvars travel with
    the call, and an exception raised by `fn` is re-raised here unchanged, so
    a caller's fail-closed handler sees exactly what it saw before."""
    loop = asyncio.get_running_loop()
    call = functools.partial(contextvars.copy_context().run, fn, *args, **kwargs)
    return await loop.run_in_executor(_EXECUTOR, call)


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
                              *args: Any, **kwargs: Any) -> T:
    """`fn(*args, **kwargs)` on the detector executor when `detector` needs it
    (`needs_detector_executor`), inline otherwise. Exceptions propagate
    unchanged either way."""
    if needs_detector_executor(detector):
        return await run_detector(fn, *args, **kwargs)
    return fn(*args, **kwargs)
