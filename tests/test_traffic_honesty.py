"""Phase 7 Task 3: gateway-call-log honesty test — the traffic-and-tests
skill's "Assertions that matter most" #1:

    "A pii_bearing request never reaches a cloud provider — assert on the
    gateway call log, not just the decision record."

Every other privacy test in this suite (e.g. test_e2e_slice.py's
`test_pii_request_excludes_cloud_in_trace`) checks the DecisionRecord — the
router's own account of what it rejected and why. That's the router grading
its own homework. This file instead wraps the *backend* (the thing that
actually calls a model) with a `RecordingBackend` and asserts directly on
what was called, independent of what the DecisionRecord claims.

Discriminating-test note (see `test_bedrock_enabled_pii_stays_onprem_while_
clean_request_can_reach_cloud`): with the shipped config, `providers.azure`
and `providers.bedrock` are both disabled, so *every* request — PII or not —
ends up on-prem regardless of the residency filter. A test asserting
"onprem_only requests only call onprem models" against that config would
pass even if the residency filter were deleted entirely (there'd be no cloud
candidate to reach either way) — worthless as a regression guard. To make it
real, the second test enables `bedrock` and drives two requests through the
*same* cloud-enabled config: a PII/onprem_only one (must still only call
onprem models — proving the residency filter, not provider availability, is
what's keeping it local) and a clean one with low mock_confidence (which
*does* reach bedrock-frontier) — proving the RecordingBackend mechanism
actually observes cloud calls when policy permits them, so its silence in
the PII case is meaningful rather than incidental.
"""
from __future__ import annotations

import asyncio

import httpx
import pytest

from app.compression.ccr import CCRStore
from app.config.loader import load_settings
from app.config.schema import Settings
from app.gateway.gateway import Gateway
from app.models import ChatRequest, Message
from app.pipeline import Pipeline
from app.policy.detectors import make_detector
from app.policy.gate import Gate
from app.routing.router import Router
from tests._litellm_fake import _DeterministicLiteLLM, make_test_registry


class RecordingBackend:
    """Wraps `_DeterministicLiteLLM`, recording every model it's asked to
    generate for. Matches `InferenceBackend.generate`'s exact signature (see
    app/gateway/backends/base.py's Protocol): `generate(self, model: str,
    messages, price, tier, hint, tools=None)`.
    """
    name = "recording"

    def __init__(self, inner: _DeterministicLiteLLM) -> None:
        self.inner = inner
        self.models_called: list[str] = []

    async def generate(self, model, messages, price, tier, hint, tools: list[dict] | None = None,
                       tool_choice: str | dict | None = None):
        self.models_called.append(model)
        return await self.inner.generate(model, messages, price, tier, hint)


def _pipeline(rec: RecordingBackend, settings: Settings, registry=None) -> Pipeline:
    det = make_detector("regex")
    co = settings.serving.context_optimization
    registry = registry or make_test_registry(settings)
    gateway = Gateway(settings, rec, Router(settings, registry=registry), registry)
    return Pipeline(settings, Gate(settings, det, registry=registry), det, gateway,
                    self_port=8080, co_cfg_provider=lambda: co, ccr=CCRStore(3600))


def _provider_of(model: str, settings: Settings, registry=None) -> str:
    pool = registry.effective_pool() if registry else settings.routing.model_pool
    return {c.name: c.provider for c in pool}.get(model, "?")


def _with_bedrock_enabled(settings: Settings) -> Settings:
    """Same recipe as test_two_lever_cost.py's escalation test: enable
    bedrock (leave azure off) without touching the on-disk config."""
    prov = dict(settings.serving.providers)
    prov["bedrock"] = prov["bedrock"].model_copy(update={"enabled": True})
    return settings.model_copy(
        update={"serving": settings.serving.model_copy(update={"providers": prov})})


def test_pii_request_never_calls_a_cloud_provider_at_the_backend():
    settings = load_settings()
    registry = make_test_registry(settings)
    rec = RecordingBackend(_DeterministicLiteLLM(settings))
    pipe = _pipeline(rec, settings, registry)

    req = ChatRequest(messages=[
        Message(role="user", content="My SSN is 482-73-1946, please summarize this.")])
    out = asyncio.run(pipe.run("r1", "hospital", req))

    assert out["policy"]["onprem_only"] is True
    assert rec.models_called, "expected a model to have actually been called"
    assert all(_provider_of(m, settings, registry) == "onprem" for m in rec.models_called), (
        rec.models_called)


def test_high_risk_request_never_calls_a_cloud_provider_at_the_backend():
    """high_risk persona: US_BANK_NUMBER-shaped content -> restricted /
    onprem_only / risk_class high / require_verification. Same call-level
    guard as the SSN case, for the other onprem_only-triggering entity
    family (bank numbers), not just SSNs."""
    settings = load_settings()
    registry = make_test_registry(settings)
    rec = RecordingBackend(_DeterministicLiteLLM(settings))
    pipe = _pipeline(rec, settings, registry)

    req = ChatRequest(messages=[
        Message(role="user", content="Please review account number 12345678901 for fraud.")])
    out = asyncio.run(pipe.run("r2", "hospital", req))

    assert out["policy"]["onprem_only"] is True
    assert rec.models_called
    assert all(_provider_of(m, settings, registry) == "onprem" for m in rec.models_called), (
        rec.models_called)


def test_bedrock_enabled_pii_stays_onprem_while_clean_request_can_reach_cloud():
    """The discriminating pair. With cloud disabled (the shipped default),
    a PII request calling only onprem models is unfalsifiable — there is no
    cloud candidate to reach regardless of whether the residency filter
    runs at all. Enabling bedrock removes that confound:

    - the PII/onprem_only request must STILL only call onprem models even
      though a cloud candidate (bedrock-frontier) now exists and is
      otherwise reachable -- proving the residency filter, not provider
      availability, keeps it local;
    - a clean acme request (no onprem_only entities) in the SAME
      bedrock-enabled settings, with low mock_confidence forcing an
      escalation to the frontier tier (mirrors
      test_two_lever_cost.py::test_escalation_to_baseline_model_yields_zero_routing_saved_identity_holds),
      DOES reach bedrock-frontier -- proving RecordingBackend actually
      observes cloud calls when policy permits them, so its silence above
      is meaningful, not just because nothing cloud-shaped was ever
      offered to it.
    """
    settings = _with_bedrock_enabled(load_settings())
    registry = make_test_registry(settings)

    # --- PII / onprem_only, cloud available but must not be touched ---
    rec_pii = RecordingBackend(_DeterministicLiteLLM(settings))
    pipe_pii = _pipeline(rec_pii, settings, registry)
    pii_req = ChatRequest(messages=[
        Message(role="user", content="My SSN is 482-73-1946, please summarize this.")])
    pii_out = asyncio.run(pipe_pii.run("r3", "hospital", pii_req))

    assert pii_out["policy"]["onprem_only"] is True
    assert rec_pii.models_called
    assert all(_provider_of(m, settings, registry) == "onprem" for m in rec_pii.models_called), (
        "PII request reached a non-onprem model even though onprem_only was set: "
        f"{rec_pii.models_called}")

    # --- clean acme request, same cloud-enabled settings, CAN escalate ---
    rec_clean = RecordingBackend(_DeterministicLiteLLM(settings))
    pipe_clean = _pipeline(rec_clean, settings, registry)
    clean_req = ChatRequest(messages=[
        Message(role="user", content="Email john.doe@example.com re: Q3 planning")])
    object.__setattr__(clean_req, "mock_confidence", 0.1)  # forces escalation
    clean_out = asyncio.run(pipe_clean.run("r4", "acme", clean_req))

    assert clean_out["policy"]["onprem_only"] is False
    assert clean_out["model"] == "bedrock-frontier"
    assert any(_provider_of(m, settings, registry) != "onprem" for m in rec_clean.models_called), (
        "expected the recorder to observe at least one cloud call for the clean "
        f"request once cloud was enabled and policy permitted it: {rec_clean.models_called}")

    # Together: RecordingBackend demonstrably CAN see cloud calls (clean case),
    # and demonstrably did NOT for the PII case in the identical settings —
    # the onprem guard is discriminating, not vacuous.


def test_residency_filter_is_what_keeps_pii_onprem_not_provider_availability():
    """Mutation check (documented, not run against production code): if the
    residency filter in app/policy/context.py were deleted -- i.e.
    `allowed = enabled` unconditionally instead of
    `allowed = enabled & onprem_providers` when `onprem_only` -- then, in the
    bedrock-enabled settings above, the SSN request's risk_class ("high")
    would target the frontier tier, bedrock-frontier would be the only
    frontier-tier candidate, and RecordingBackend WOULD record a bedrock
    call, failing the PII assertion in the test above. That confirms the
    guard above is falsifiable by an actual residency-filter regression, not
    just by construction of the test. See app/policy/context.py:
    `allowed = enabled & onprem_providers if onprem_only else enabled`.
    """
    from app.policy import context as context_module
    from app.policy import gate as gate_module

    settings = _with_bedrock_enabled(load_settings())

    # gate.py does `from app.policy.context import build_context`, binding the
    # name into its own module namespace -- patching context_module.build_context
    # alone would not affect the call site inside Gate.evaluate(), so patch it
    # where it's actually looked up (same pitfall documented in
    # test_agent_runtime.py for app.agent.mock_agent.decide_agent_step).
    original_build_context = gate_module.build_context

    def _leaky_build_context(settings, request_id, tenant_id, entities,
                             redaction_map_id, app_policy=None, app_rules=None,
                             registry=None):
        """Reimplements build_context but WITHOUT the onprem residency
        filter, to prove the guard above would catch this exact regression.
        (Signature mirrors build_context, incl. the app_policy/app_rules
        params the gate now always passes; this leaky version ignores both —
        the demo tenants carry no per-app policy/profile row, so residency
        still comes from policy.yaml.)"""
        etypes = {e.type for e in entities}
        rule = context_module._match_rule(settings.policy.sensitivity_rules, etypes)
        tenant = settings.policy.tenants.get(
            tenant_id, settings.policy.tenants["default"])
        residency = context_module._stricter(rule.residency, tenant.default_residency)
        onprem_only = residency == "onprem_only"
        enabled = {p for p, c in settings.serving.providers.items() if c.enabled}
        allowed = enabled  # BUG: residency filter removed on purpose
        allowed_models = frozenset(
            c.name for c in settings.routing.model_pool if c.provider in allowed)
        from app.models import ExecutionPolicyContext
        return ExecutionPolicyContext(
            request_id=request_id, tenant_id=tenant_id, sensitivity=rule.sensitivity,
            onprem_only=onprem_only, allowed_providers=frozenset(allowed),
            allowed_models=allowed_models,
            max_cost_usd=settings.policy.budgets["default_max_spend_usd"],
            max_latency_ms=int(settings.policy.budgets["default_max_latency_ms"]),
            risk_class=rule.risk_class, require_verification=rule.require_verification,
            detected_entities=tuple(sorted(etypes)), redaction_map_id=redaction_map_id)

    gate_module.build_context = _leaky_build_context
    try:
        registry = make_test_registry(settings)
        rec = RecordingBackend(_DeterministicLiteLLM(settings))
        pipe = _pipeline(rec, settings, registry)
        req = ChatRequest(messages=[
            Message(role="user", content="My SSN is 482-73-1946, please summarize this.")])
        out = asyncio.run(pipe.run("r5", "hospital", req))
        # onprem_only is still reported True (the context itself still sets
        # it), but with the filter removed the router can now reach bedrock
        # -- demonstrating the assertion in the tests above is genuinely
        # falsifiable, not vacuous.
        assert out["policy"]["onprem_only"] is True
        assert any(_provider_of(m, settings, registry) != "onprem" for m in rec.models_called), (
            "expected the deliberately-broken residency filter to leak to a "
            "cloud model, proving the real (unbroken) guard above is meaningful")
    finally:
        gate_module.build_context = original_build_context


@pytest.mark.usefixtures("live_internal_server")
def test_multistep_pii_request_stays_onprem_at_decision_level(live_internal_server):
    """multi_step + PII: drives mock_multistep with an SSN in the content
    against a live server (the agent runtime's NAT tool_calling_agent makes
    real HTTP self-calls to the internal per-hop endpoint, so wiring a
    RecordingBackend into that path would require intercepting the agent's
    own HTTP client -- too invasive for this task; see the plan's Task 3
    note). Instead this asserts at the DecisionRecord level: every
    `agent_hop` DecisionRecord's `chosen` model maps to an onprem provider.
    This is a weaker guarantee than the direct-path RecordingBackend
    assertions above (which observe the actual backend call), but it is the
    same style of check already used by test_e2e_slice.py's
    `test_pii_request_excludes_cloud_in_trace` for the direct path, now
    exercised in the multi-step Router-per-hop path. The direct-path
    RecordingBackend test remains the primary #1 guard.
    """
    pytest.importorskip("nat")
    settings = load_settings()
    # live_internal_server boots a real create_app(), which seeds the same
    # SP_SEED_TEST_MODELS onboarded rows (its own isolated in-memory DB) --
    # build an independent registry with the identical seed here just to
    # resolve model->provider for the assertion below.
    registry = make_test_registry(settings)

    r = httpx.post(
        f"http://127.0.0.1:{live_internal_server.port}/v1/chat/completions",
        headers={"authorization": "Bearer hospital-key"},
        json={
            "model": "auto",
            "mock_multistep": True,
            "messages": [
                {"role": "user",
                 "content": "My SSN is 482-73-1946. Look up account 42 then summarize."},
            ],
        },
        timeout=30.0,
    )
    assert r.status_code == 200, r.text
    body = r.json()

    assert body["policy"]["onprem_only"] is True
    hops = [t for t in body["trace"] if t.get("step") == "agent_hop"]
    assert hops, "expected at least one agent_hop DecisionRecord"
    assert all(_provider_of(h["chosen"], settings, registry) == "onprem"
               for h in hops if h["chosen"]), (hops)
