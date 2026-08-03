"""Backend factory (cut over to the ModelRegistry in the model-onboarding plan).

`make_backend`/`resolve_backend` build the single `LiteLLMBackend`, injecting
the `ModelRegistry` so onboarded on-prem models (endpoint + auth env) are
resolvable at egress. There is no boot-time health probe any more: per-model
reachability is probed at onboard time (registry), and on-prem availability is
derived from `registry.has_healthy_onprem()` in `app/policy/context.py` (a
request that is onprem_only with no healthy on-prem model resolves to an empty
provider set and is rejected downstream — never silently routed to cloud).
"""
from __future__ import annotations

import logging

from app.config.schema import Settings
from app.gateway.backends.litellm_backend import LiteLLMBackend

log = logging.getLogger("serving.backend")


def make_backend(settings: Settings, registry=None) -> LiteLLMBackend:
    temp = settings.routing.confidence_logprob_temperature
    return LiteLLMBackend(settings, registry=registry, logprob_temperature=temp)


async def resolve_backend(settings: Settings, registry=None) -> LiteLLMBackend:
    # There is no boot-time health probe any more: per-model reachability is
    # probed at onboard time (registry), so the single LiteLLM egress path is
    # always constructed directly. Never silently degrade in a customer deploy.
    log.info("serving.backend active backend=litellm")
    return make_backend(settings, registry)
