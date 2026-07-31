"""Backend factory + startup health validation (Phase 9 Task 4; cut over to
LiteLLM in the litellm-gateway-swap plan's Task 5).

`make_backend` builds the single `LiteLLMBackend` for the configured
`serving.inference_backend`. `resolve_backend` then health-checks it and
applies the fail-closed startup rule: unreachable onprem (`vllm`/`dynamo`) is
a boot error (`sys.exit(1)`) -- there is no mock fallback any more. Never
silently degrade in a customer demo.

`none` semantics: `inference_backend: none` still returns a working
`LiteLLMBackend` -- `health()` reports `ok=True` without a network probe
(there is no onprem server to check). The `none` REJECTION behaviour
(onprem_only requests must be rejected, never silently routed to cloud) is
enforced structurally in `app/policy/context.py` (Task 1): when
`inference_backend == "none"`, "onprem" is dropped from the enabled provider
set, so an onprem_only request's `allowed_providers` is empty and the router
rejects it downstream. The backend object itself just needs to exist so
public/cloud-eligible requests still get served; it is never actually asked
to serve an onprem_only request.

`SP_SKIP_BACKEND_HEALTHCHECK`: test-only escape hatch, see `resolve_backend`.
"""
from __future__ import annotations

import logging
import os
import sys

from app.config.schema import Settings
from app.gateway.backends.litellm_backend import LiteLLMBackend

log = logging.getLogger("serving.backend")


def make_backend(settings: Settings) -> LiteLLMBackend:
    temp = settings.routing.confidence_logprob_temperature
    return LiteLLMBackend(settings, logprob_temperature=temp)


async def resolve_backend(settings: Settings) -> LiteLLMBackend:
    # Probe with a throwaway instance (loop-safety, see README), serve with a
    # fresh one. No mock fallback exists any more: an unreachable onprem
    # backend is a fail-closed boot error (never silently degrade in a
    # customer deploy).
    #
    # SP_SKIP_BACKEND_HEALTHCHECK: test escape hatch. app/main.py builds
    # `app = create_app()` at IMPORT time (before any test fixture/monkeypatch),
    # so a real vllm/dynamo probe would hang or sys.exit(1) at test collection.
    # Tests set this to build the backend without a network probe; generation
    # is driven by a monkeypatched litellm.acompletion. Never set in
    # production.
    if os.environ.get("SP_SKIP_BACKEND_HEALTHCHECK"):
        return make_backend(settings)
    probe = make_backend(settings)
    health = await probe.health()
    if not health.ok:
        log.error("backend_unavailable_no_fallback backend=%s detail=%s",
                  settings.serving.inference_backend, health.detail)
        sys.exit(1)
    log.info("serving.backend active backend=litellm inference=%s models=%s",
              settings.serving.inference_backend, health.models)
    return make_backend(settings)
