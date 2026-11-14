"""The one place detector work runs: a dedicated single-worker executor (B118).

PII detection (Presidio) is CPU-bound; on a 56k-token prompt one pass takes
~16 s. Run on the asyncio event loop it froze `/health`, so the liveness probe
killed the pod. Every detector-bearing call reached from a request -- the gate,
the per-message redacted view, step-9 `verify`, egress tool-call redaction, a
cache-hit re-verify, the incremental streaming verifier, telemetry/console
`redacted_copy` -- is awaited through `run_detector` instead, so **no detector
call runs on the event-loop thread**.

Why a dedicated executor and not `asyncio.to_thread`: the default executor is
also where the loop resolves DNS (`getaddrinfo`) for httpx/LiteLLM. Detector
work queued there -- or parked on the Presidio engine lock -- would starve
outbound connects. One worker, so work runs in FIFO submission order and one
engine is only ever used by one thread; `PresidioDetector`'s own lock stays as
defence in depth.

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
