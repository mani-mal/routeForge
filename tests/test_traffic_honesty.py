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
                       tool_choice: str | dict | None = None, params: dict | None = None):
        self.models_called.append(model)
        return await self.inner.generate(model, messages, price, tier, hint)


def _pipeline(rec: RecordingBackend, settings: Settings, registry=None) -> Pipeline:
    det = make_detector("regex")
    co = settings.serving.context_optimization
    registry = registry or make_test_registry(settings)
    gateway = Gateway(settings, rec, Router(settings, registry=registry), registry)
    return Pipeline(settings, Gate(settings, det, registry=registry), det, gateway,
                    co_cfg_provider=lambda: co, ccr=CCRStore(3600))


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
    """Mutation check with TWO guards (B52, 2026-09-23). Placement under
    onprem_only is enforced twice: `build_context` narrows `allowed_providers`
    to {onprem} (app/policy/context.py), and `feasible_set` intersects the
    providers with {onprem} again at the eligibility seam
    (app/policy/feasible.py, `ONPREM_ONLY_PROVIDERS`).

    Phase 1 deletes ONLY the `build_context` filter (the context says
    onprem_only=True yet lists bedrock -- the B52 inconsistent context): the
    seam must still keep the SSN request off the cloud. Phase 2 deletes BOTH:
    the high-risk request then targets the frontier tier, bedrock-frontier is
    its only candidate, and RecordingBackend DOES record a bedrock call --
    proving the backend-level guard in the tests above is falsifiable by an
    actual residency regression, not vacuous by construction.
    """
    from app.policy import context as context_module
    from app.policy import gate as gate_module

    settings = _with_bedrock_enabled(load_settings())

    # gate.py does `from app.policy.context import build_context`, binding the
    # name into its own module namespace -- patching context_module.build_context
    # alone would not affect the call site inside Gate.evaluate(), so patch it
    # where it's actually looked up.
    original_build_context = gate_module.build_context

    def _leaky_build_context(settings, request_id, tenant_id, entities,
                             redaction_map_id, app_policy=None, app_rules=None,
                             registry=None, escalate_residency=True,
                             content_integrity=None, model_input=None):
        """Reimplements build_context but WITHOUT the onprem residency
        filter, to prove the guard above would catch this exact regression.
        (Signature mirrors build_context, incl. the app_policy/app_rules
        params, the compiled `escalate_residency` switch, and the compiled
        `content_integrity` and `model_input` axes the gate now always passes;
        this leaky version ignores all five for PLACEMENT purposes — the demo
        tenants carry no per-app policy/profile row, so residency still comes
        from policy.yaml and escalation is on. `content_integrity` is a DATA
        axis, orthogonal to the residency filter this test is about, so it is
        threaded straight into the returned context exactly as the real
        build_context does — resolved through the compiler when the caller
        supplies none — rather than being silently dropped.)"""
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
        if content_integrity is None:
            from app.policy.compile import compile_policy
            content_integrity = compile_policy(
                app_policy, settings).protection.content_integrity
        from app.models import ExecutionPolicyContext
        return ExecutionPolicyContext(
            request_id=request_id, tenant_id=tenant_id, sensitivity=rule.sensitivity,
            onprem_only=onprem_only, allowed_providers=frozenset(allowed),
            allowed_models=allowed_models,
            max_cost_usd=settings.policy.budgets["default_max_spend_usd"],
            max_latency_ms=int(settings.policy.budgets["default_max_latency_ms"]),
            risk_class=rule.risk_class, require_verification=rule.require_verification,
            detected_entities=tuple(sorted(etypes)), redaction_map_id=redaction_map_id,
            content_integrity=content_integrity)

    from app.errors import NoEligibleCandidate
    from app.policy import feasible as feasible_module

    def _run_ssn(request_id):
        registry = make_test_registry(settings)
        rec = RecordingBackend(_DeterministicLiteLLM(settings))
        pipe = _pipeline(rec, settings, registry)
        req = ChatRequest(messages=[
            Message(role="user", content="My SSN is 482-73-1946, please summarize this.")])
        try:
            return asyncio.run(pipe.run(request_id, "hospital", req)), None, rec, registry
        except NoEligibleCandidate as exc:
            return None, exc, rec, registry

    gate_module.build_context = _leaky_build_context
    original_onprem_set = feasible_module.ONPREM_ONLY_PROVIDERS
    try:
        # Phase 1: only build_context is broken -> the seam still holds. The
        # request must be REFUSED, and refused because the seam rejected the
        # one cloud candidate as residency -- "no cloud call" alone would be
        # vacuously true of a refusal for any reason.
        out, exc, rec, registry = _run_ssn("r5a")
        assert out is None and exc is not None, (
            "feasible_set must refuse an onprem_only request whose context "
            "still lists a cloud provider (B52), not serve it")
        assert ("bedrock-frontier", "residency") in exc.rejected, exc.rejected
        assert "excluded · residency" in str(exc)
        assert all(_provider_of(m, settings, registry) == "onprem"
                   for m in rec.models_called)

        # Phase 2: the seam's narrowing is neutralised too -> it leaks.
        feasible_module.ONPREM_ONLY_PROVIDERS = frozenset(settings.serving.providers)
        out, exc, rec, registry = _run_ssn("r5b")
        assert exc is None, exc
        # onprem_only is still reported True (the context itself still sets
        # it), but with both filters removed the router can now reach bedrock.
        assert out is not None and out["policy"]["onprem_only"] is True
        assert any(_provider_of(m, settings, registry) != "onprem" for m in rec.models_called), (
            "expected the deliberately-broken residency filters to leak to a "
            "cloud model, proving the real (unbroken) guards above are meaningful")
    finally:
        gate_module.build_context = original_build_context
        feasible_module.ONPREM_ONLY_PROVIDERS = original_onprem_set
