"""Backend factory + startup health-validation/fallback (Phase 9 Task 4).

`make_backend` maps `serving.inference_backend` to a concrete
`InferenceBackend` instance. `resolve_backend` then health-checks it and
applies the CLAUDE.md / inference-backends-skill startup-validation rule:
unreachable + `allow_backend_fallback: true` -> warn + fall back to
`MockBackend`; unreachable + fallback disabled -> exit non-zero. Never
silently degrade in a customer demo.

`none` semantics: `make_backend` returns a *working* `MockBackend` for
`inference_backend: none` -- this is intentional, not a bug. The `none`
REJECTION behaviour (onprem_only requests must be rejected, never silently
routed to cloud) is enforced structurally in `app/policy/context.py` (Task 1):
when `inference_backend == "none"`, "onprem" is dropped from the enabled
provider set, so an onprem_only request's `allowed_providers` is empty and
the router rejects it downstream. The backend object itself just needs to
exist so public/cloud-eligible requests still get served; it is never
actually asked to serve an onprem_only request. `mock`/`none` health() is
always `ok=True` (no external server involved), so they never trigger the
fallback/exit path below -- only `vllm`/`dynamo` can fail health.
"""
from __future__ import annotations

import logging
import sys

from app.config.schema import Settings
from app.gateway.backends.base import InferenceBackend
from app.gateway.backends.dynamo import DynamoBackend
from app.gateway.backends.mock import MockBackend
from app.gateway.backends.vllm import VllmBackend

log = logging.getLogger("serving.backend")


def make_backend(settings: Settings) -> InferenceBackend:
    kind = settings.serving.inference_backend
    if kind == "mock":
        return MockBackend(settings)
    temp = settings.routing.confidence_logprob_temperature
    if kind == "vllm":
        return VllmBackend(settings.serving.vllm, logprob_temperature=temp)
    if kind == "dynamo":
        return DynamoBackend(settings.serving.dynamo, logprob_temperature=temp)
    if kind == "none":
        # See module docstring: 'none' rejection is enforced in context.py,
        # not here. This is a working object, never actually asked to serve
        # an onprem_only request.
        return MockBackend(settings)
    raise ValueError(f"unknown inference_backend: {kind!r}")


async def _aclose(backend: InferenceBackend) -> None:
    """Close a backend's httpx client if it has one (mock/none don't)."""
    client = getattr(backend, "_client", None)
    if client is not None:
        await client.aclose()


async def resolve_backend(settings: Settings) -> InferenceBackend:
    # IMPORTANT: probe with one instance, SERVE with a fresh one.
    #
    # app/main.py runs resolve_backend under asyncio.run(...), whose event loop
    # is closed the moment it returns — but requests are later served on
    # uvicorn's own loop. An httpx.AsyncClient binds its connection pool to the
    # loop of its FIRST request, so if the health probe below and request
    # serving shared one backend instance, the first real /v1/chat/completions
    # would raise "RuntimeError: Event loop is closed" (intermittently — httpx
    # sometimes reconnects, sometimes not, which is exactly how this presented).
    # So we health-check a throwaway `probe`, discard its now-loop-bound client,
    # and return a fresh instance whose client binds lazily to the serving loop.
    probe = make_backend(settings)
    health = await probe.health()  # called once; reused for the banner below
    await _aclose(probe)
    if not health.ok:
        if settings.serving.allow_backend_fallback:
            log.warning(
                "backend_unavailable_falling_back backend=%s detail=%s",
                settings.serving.inference_backend, health.detail)
            log.info("serving.backend active backend=mock models=[]")
            return MockBackend(settings)  # mock has no client; loop-safe as-is
        log.error(
            "backend_unavailable_no_fallback backend=%s detail=%s",
            settings.serving.inference_backend, health.detail)
        sys.exit(1)
    log.info(
        "serving.backend active backend=%s models=%s",
        probe.name, health.models)
    return make_backend(settings)
