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
    if kind == "vllm":
        return VllmBackend(settings.serving.vllm)
    if kind == "dynamo":
        return DynamoBackend(settings.serving.dynamo)
    if kind == "none":
        # See module docstring: 'none' rejection is enforced in context.py,
        # not here. This is a working object, never actually asked to serve
        # an onprem_only request.
        return MockBackend(settings)
    raise ValueError(f"unknown inference_backend: {kind!r}")


async def resolve_backend(settings: Settings) -> InferenceBackend:
    backend = make_backend(settings)
    health = await backend.health()  # called once; reused for the banner below
    if not health.ok:
        if settings.serving.allow_backend_fallback:
            log.warning(
                "backend_unavailable_falling_back backend=%s detail=%s",
                settings.serving.inference_backend, health.detail)
            backend = MockBackend(settings)
            health = await backend.health()  # trivial: mock never makes a network call
        else:
            log.error(
                "backend_unavailable_no_fallback backend=%s detail=%s",
                settings.serving.inference_backend, health.detail)
            sys.exit(1)
    log.info(
        "serving.backend active backend=%s models=%s",
        backend.name, health.models)
    return backend
