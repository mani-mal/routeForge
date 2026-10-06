"""P2b-2 Task 3: `internals["outcome_ctx"]` wiring into `Pipeline.run`/
`run_stream`. Task 2's `outcome_context()` builder is unit-tested for
purity/shape in tests/test_outcome_builder.py; this file only checks the
pipeline actually populates the out-parameter -- on a normal completion, on
a request that fails at routing (NoEligibleCandidate), and that the wired
dict never carries raw prompt text even when the prompt has an SSN in it.
"""
from __future__ import annotations

import asyncio
import json

import pytest

from app.apps.policy import AppPolicyView
from app.compression.ccr import CCRStore
from app.config.loader import load_settings
from app.errors import NoEligibleCandidate
from app.gateway.gateway import Gateway
from app.models import ChatRequest, Message
from app.pipeline import Pipeline
from app.policy.detectors import make_detector
from app.policy.gate import Gate
from app.routing.router import Router
from tests._litellm_fake import _DeterministicLiteLLM, make_test_registry

SSN = "123-45-6789"


def _pipeline(settings, registry) -> Pipeline:
    det = make_detector(settings.serving.pii_detection)
    gate = Gate(settings, det, registry=registry)
    backend = _DeterministicLiteLLM(settings)
    router = Router(settings, registry=registry)
    gateway = Gateway(settings, backend, router, registry)
    co = settings.serving.context_optimization
    return Pipeline(settings, gate, det, gateway,
                     co_cfg_provider=lambda: co, ccr=CCRStore(3600),
                     registry=registry)


def _empty_registry(settings):
    """A `ModelRegistry` with nothing onboarded -- no on-prem model exists,
    so an `onprem_only` request has no eligible candidate (CLAUDE.md rule
    3) and `Router.select` raises `NoEligibleCandidate`."""
    from app.apps.db import Database
    from app.apps.registry import ModelRegistry

    async def _build():
        db = Database(url="sqlite+aiosqlite://")
        await db.ensure_ready()
        reg = ModelRegistry(settings)
        await reg.refresh(db)
        return reg

    return asyncio.run(_build())


def test_outcome_ctx_populated_on_success():
    s = load_settings()
    reg = make_test_registry(s)
    pipe = _pipeline(s, reg)
    req = ChatRequest(messages=[Message(role="user", content="What is the capital of France?")])
    internals: dict = {}
    asyncio.run(pipe.run("rid-1", "acme", req, internals=internals))
    oc = internals["outcome_ctx"]
    assert oc.keys() == {
        "sensitivity", "risk_class", "onprem_only",
        "detected_entity_types", "app_policy_version", "complexity",
        "requested_model", "max_tokens", "routing_tokens"}
    assert isinstance(oc["complexity"], float)
    # B60: the count the router was given, set on the same line as the
    # complexity derived from it.
    assert type(oc["routing_tokens"]) is int and oc["routing_tokens"] > 0
    assert oc["complexity"] == min(1.0, oc["routing_tokens"] / 2000)


def test_outcome_ctx_survives_no_eligible_candidate_at_routing():
    s = load_settings()
    reg = _empty_registry(s)
    pipe = _pipeline(s, reg)
    req = ChatRequest(messages=[Message(role="user", content="hello")])
    internals: dict = {}
    with pytest.raises(NoEligibleCandidate):
        asyncio.run(pipe.run(
            "rid-2", "acme", req,
            app_policy=AppPolicyView(residency="onprem_only", pii_detection="off"),
            internals=internals))
    assert "outcome_ctx" in internals
    oc = internals["outcome_ctx"]
    assert oc["onprem_only"] is True
    # The failure happens in the gateway/router, strictly after complexity is
    # computed in run() -- so it must already be on the dict.
    assert "complexity" in oc
    # B60: and so is the token count the router rejected the request on.
    assert oc["routing_tokens"] == 1      # "hello": max(1, 5 // 4)


def test_outcome_ctx_has_no_prompt_text_even_with_ssn():
    s = load_settings()
    reg = make_test_registry(s)
    pipe = _pipeline(s, reg)
    req = ChatRequest(messages=[Message(role="user", content=f"my ssn is {SSN}")])
    internals: dict = {}
    asyncio.run(pipe.run("rid-3", "acme", req, internals=internals))
    dumped = json.dumps(internals["outcome_ctx"])
    assert SSN not in dumped
    assert "US_SSN" in internals["outcome_ctx"]["detected_entity_types"]
