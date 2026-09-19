"""P1b Task 4 — the three endpoint data-handling exclusion rules.

Design: docs/superpowers/specs/2026-09-19-endpoint-data-handling-p1b-design.md §4.

| rule          | condition                                                   | code                          |
|---------------|-------------------------------------------------------------|-------------------------------|
| region        | app sets `allowed_regions` and the endpoint's is not in it   | region_not_allowed            |
| retention     | app sets `require_no_retention` and retention is not `none`  | prompt_retention_not_allowed  |
| raw sensitive | the request sends raw sensitive content and the endpoint     | raw_sensitive_not_accepted    |
|               | does not declare `accepts_raw_sensitive: true`               |                               |

Two properties this file exists to pin, beyond the rules themselves:

* **Unknown never satisfies a requirement, and never blocks a request that
  imposes none.** Every endpoint in every deployment today is `unknown` on all
  three, so an app that sets neither requirement must route byte-identically
  to before this branch landed.
* **An empty feasible set is a REJECT WITH REASONS**, never a relaxation
  (CLAUDE.md rule 5 / IP-9's derived reject).
"""
from __future__ import annotations

import asyncio

import pytest

from app.apps import store
from app.apps.db import Database
from app.apps.policy import AppPolicyView
from app.apps.registry import ModelRegistry
from app.compression.ccr import CCRStore
from app.config.loader import load_settings
from app.errors import NoEligibleCandidate
from app.gateway.gateway import Gateway
from app.models import ChatRequest, Message
from app.pipeline import Pipeline, PipelineRejected
from app.policy.context import build_context, pinned_model_reason, requestable_models
from app.policy.detectors import make_detector
from app.policy.feasible import (
    PROMPT_RETENTION_NOT_ALLOWED,
    RAW_SENSITIVE_NOT_ACCEPTED,
    REGION_NOT_ALLOWED,
    feasible_set,
)
from app.policy.gate import Gate
from app.routing.router import Router
from tests._litellm_fake import _DeterministicLiteLLM
from tests.test_traffic_honesty import RecordingBackend

S = load_settings()


def _run(coro):
    return asyncio.run(coro)


def _onprem(slug, *, tier="cheap", region=None, prompt_retention=None,
            accepts_raw_sensitive=None, price=0.1):
    return dict(display_name=slug, provider="onprem", tier=tier,
                price_input=price, price_output=price,
                endpoint=f"http://{slug}/v1", enabled=True,
                region=region, prompt_retention=prompt_retention,
                accepts_raw_sensitive=accepts_raw_sensitive)


async def _build_registry(rows, settings=S):
    db = Database(url="sqlite+aiosqlite://")
    await db.ensure_ready()
    async with db.session() as s:
        for slug, fields in rows.items():
            await store.upsert_onboarded(s, slug, **fields)
        await s.commit()
    reg = ModelRegistry(settings)
    await reg.refresh(db)
    return reg


def _registry(rows, settings=S):
    return _run(_build_registry(rows, settings))


def _two_regions():
    return _registry({
        "us-8b": _onprem("us-8b", region="us"),
        "eu-8b": _onprem("eu-8b", region="eu"),
    })


def _ctx(registry, policy, *, entities=(), settings=S, model_input=None,
         tenant_id="acme"):
    return build_context(settings, "req-1", tenant_id, list(entities), None,
                         app_policy=policy, registry=registry,
                         model_input=model_input)


def _by_code(fs):
    return {r.name: r.code for r in fs.rejected}


def _by_reason(fs):
    return {r.name: r.reason for r in fs.rejected}


def _names(fs):
    return {c.name for c in fs.eligible}


# --- 1. region ---------------------------------------------------------------


def test_region_requirement_excludes_a_mismatched_endpoint():
    reg = _two_regions()
    policy = AppPolicyView(allowed_regions=frozenset({"us"}))
    fs = feasible_set(S, reg, policy, tenant_id="acme")

    assert _names(fs) == {"us-8b"}
    assert _by_code(fs)["eu-8b"] == REGION_NOT_ALLOWED
    reason = _by_reason(fs)["eu-8b"]
    assert reason.startswith("excluded · region")
    assert "eu" in reason and "us" in reason   # declaration and requirement


def test_region_requirement_excludes_an_endpoint_that_declares_nothing():
    """unknown never satisfies a requirement."""
    reg = _registry({"us-8b": _onprem("us-8b", region="us"),
                     "mystery": _onprem("mystery")})
    policy = AppPolicyView(allowed_regions=frozenset({"us"}))
    fs = feasible_set(S, reg, policy, tenant_id="acme")

    assert _names(fs) == {"us-8b"}
    assert _by_code(fs)["mystery"] == REGION_NOT_ALLOWED
    assert "unknown" in _by_reason(fs)["mystery"]


def test_region_matching_is_case_insensitive_on_both_sides():
    reg = _registry({"us-8b": _onprem("us-8b", region="US")})
    policy = AppPolicyView(allowed_regions=frozenset({"Us"}))
    assert _names(feasible_set(S, reg, policy, tenant_id="acme")) == {"us-8b"}


def test_region_requirement_narrows_the_router_and_names_the_reason():
    reg = _two_regions()
    ctx = _ctx(reg, AppPolicyView(allowed_regions=frozenset({"us"})))
    d = Router(S, registry=reg).select(ctx, tokens_after=20, complexity=0.1)

    assert d.chosen.name == "us-8b"
    reasons = {r.name: r.reason for r in d.rejected}
    assert reasons["eu-8b"].startswith("excluded · region")


def test_region_requirement_narrows_GET_v1_models():
    reg = _two_regions()
    policy = AppPolicyView(allowed_regions=frozenset({"us"}))
    assert {c.name for c in requestable_models(S, reg, "acme", policy)} == {"us-8b"}


# --- 2. upgrade safety -------------------------------------------------------


def test_an_app_with_no_requirements_is_unaffected_by_unknown_declarations():
    """Every endpoint in every deployment today. This is the upgrade-safety test."""
    reg = _registry({"a-8b": _onprem("a-8b"), "b-32b": _onprem("b-32b", tier="mid")})
    policy = AppPolicyView()

    fs = feasible_set(S, reg, policy, tenant_id="acme")
    assert _names(fs) == {"a-8b", "b-32b"}
    assert fs.rejected == ()

    # ... and the same through the context + router, with entities detected
    # (so sensitivity is above `public`) and `model_input: raw`: with no app
    # requirement, an `unknown` declaration blocks nothing.
    ctx = _ctx(reg, policy, model_input="raw")
    assert ctx.allowed_models == frozenset({"a-8b", "b-32b"})
    d = Router(S, registry=reg).select(ctx, tokens_after=20, complexity=0.1)
    assert d.chosen.name == "a-8b"
    assert [r for r in d.rejected if "region" in r.reason] == []


# --- 3. retention ------------------------------------------------------------


def test_retention_requirement_excludes_a_retaining_endpoint():
    reg = _registry({
        "keeps": _onprem("keeps", prompt_retention="retained"),
        "discards": _onprem("discards", prompt_retention="none"),
        "silent": _onprem("silent"),
    })
    policy = AppPolicyView(require_no_retention=True)
    fs = feasible_set(S, reg, policy, tenant_id="acme")

    assert _names(fs) == {"discards"}
    assert _by_code(fs)["keeps"] == PROMPT_RETENTION_NOT_ALLOWED
    # unknown never satisfies a requirement
    assert _by_code(fs)["silent"] == PROMPT_RETENTION_NOT_ALLOWED
    reason = _by_reason(fs)["keeps"]
    assert reason.startswith("excluded · prompt retention")
    assert "retained" in reason and "none" in reason


def test_retention_requirement_off_keeps_a_retaining_endpoint():
    reg = _registry({"keeps": _onprem("keeps", prompt_retention="retained")})
    fs = feasible_set(S, reg, AppPolicyView(), tenant_id="acme")
    assert _names(fs) == {"keeps"}


# --- 4. raw sensitive --------------------------------------------------------


def test_raw_sensitive_rule_fires_only_for_raw_model_input_and_a_sensitive_request():
    """model_input: redact ⇒ the endpoint is eligible again (eligible after redaction)."""
    from app.models import Entity

    reg = _registry({
        "trusted": _onprem("trusted", accepts_raw_sensitive=True),
        "untrusted": _onprem("untrusted", accepts_raw_sensitive=False, tier="mid"),
        "silent": _onprem("silent", tier="frontier"),
    })
    policy = AppPolicyView()
    ssn = [Entity(type="US_SSN", start=0, end=11, value="123-45-6789")]

    # raw + sensitive -> only the endpoint that declares `true` survives.
    raw_ctx = _ctx(reg, policy, entities=ssn, model_input="raw")
    assert raw_ctx.sensitivity == "restricted"
    fs_raw = feasible_set(S, reg, raw_ctx, tenant_id="acme")
    assert _names(fs_raw) == {"trusted"}
    assert _by_code(fs_raw)["untrusted"] == RAW_SENSITIVE_NOT_ACCEPTED
    assert _by_code(fs_raw)["silent"] == RAW_SENSITIVE_NOT_ACCEPTED
    reason = _by_reason(fs_raw)["untrusted"]
    assert reason.startswith("excluded · raw sensitive content")
    assert "false" in reason

    # redact + sensitive -> the model only ever sees placeholders; every
    # endpoint is eligible again ("eligible after redaction").
    redact_ctx = _ctx(reg, policy, entities=ssn, model_input="redact")
    assert _names(feasible_set(S, reg, redact_ctx, tenant_id="acme")) == {
        "trusted", "untrusted", "silent"}

    # raw + non-sensitive (public) -> nothing sensitive is being sent at all.
    public_ctx = _ctx(reg, policy, entities=(), model_input="raw")
    assert public_ctx.sensitivity == "public"
    assert _names(feasible_set(S, reg, public_ctx, tenant_id="acme")) == {
        "trusted", "untrusted", "silent"}


# --- 5. the empty set is a reject, never a relaxation ------------------------


def test_empty_feasible_set_is_a_reject_with_reasons_not_a_relaxation():
    reg = _two_regions()
    ctx = _ctx(reg, AppPolicyView(allowed_regions=frozenset({"ap-south"})))

    assert feasible_set(S, reg, ctx, tenant_id="acme").eligible == ()
    with pytest.raises(NoEligibleCandidate) as exc:
        Router(S, registry=reg).select(ctx, tokens_after=20, complexity=0.1)
    assert "no candidates after residency filter" in str(exc.value)
    assert "excluded · region" in str(exc.value)


def test_a_data_handling_requirement_can_never_widen_placement():
    """Rule 3 is untouched: a region requirement that names a cloud region
    cannot reach a cloud provider an `onprem_only` residency excluded."""
    s = S.model_copy(update={"serving": S.serving.model_copy(update={
        "providers": {**S.serving.providers,
                      "azure": S.serving.providers["azure"].model_copy(
                          update={"enabled": True})}})})
    reg = _registry({"local": _onprem("local", region="onprem")}, settings=s)
    ctx = build_context(s, "req-1", "acme", [], None,
                        app_policy=AppPolicyView(residency="onprem_only",
                                                 allowed_regions=frozenset({"us"})),
                        registry=reg)
    assert ctx.allowed_providers == frozenset({"onprem"})
    fs = feasible_set(s, reg, ctx, tenant_id="acme")
    assert fs.eligible == ()
    # Placement still decides every cloud candidate, and says so: an app
    # requiring region `us` did not thereby make a us-hosted azure endpoint
    # reachable. Only the on-prem candidate reaches the data-handling stage.
    codes = {r.name: r.code for r in fs.rejected}
    assert codes["local"] == REGION_NOT_ALLOWED
    assert all(code == "residency" for name, code in codes.items()
               if name != "local")


# --- 6. the pinned model -----------------------------------------------------


def test_pinned_model_reason_names_the_data_handling_rule_that_refused_it():
    reg = _two_regions()
    ctx = _ctx(reg, AppPolicyView(allowed_regions=frozenset({"us"})))
    ctx = ctx.model_copy(update={"requested_model": "eu-8b"})

    reason = pinned_model_reason(reg.effective_pool(), ctx, reg, "acme", "eu-8b")
    assert "region" in reason
    assert "eu" in reason
    assert "not permitted for application" not in reason


def test_the_pipeline_refuses_a_pinned_data_handling_excluded_model():
    reg = _two_regions()
    rec = RecordingBackend(_DeterministicLiteLLM(S))
    pipe = _pipeline(rec, S, reg)
    req = ChatRequest(model="eu-8b",
                      messages=[Message(role="user", content="hello there")])

    with pytest.raises(PipelineRejected) as exc:
        _run(pipe.run("r1", "acme", req,
                      app_policy=AppPolicyView(allowed_regions=frozenset({"us"}))))
    assert "region" in str(exc.value)


# --- 7. the semantic-cache hit ----------------------------------------------


def _pipeline(rec, settings, registry, cache=None):
    det = make_detector(settings.serving.pii_detection)
    co = settings.serving.context_optimization
    gate = Gate(settings, det, registry=registry)
    gateway = Gateway(settings, rec, Router(settings, registry=registry), registry)
    return Pipeline(settings, gate, det, gateway, co_cfg_provider=lambda: co,
                    ccr=CCRStore(3600), registry=registry, cache=cache)


def test_a_cache_hit_from_a_data_handling_excluded_model_is_not_certified_eligible():
    """The certificate's `chosen_in_eligible_set` is checked for a model the
    router never chose. A cached answer produced by an endpoint this request's
    own rules exclude must read False, not True."""
    from app.cache.store import DeterministicSemanticCache

    s = S.model_copy(update={"serving": S.serving.model_copy(update={
        "semantic_cache": S.serving.semantic_cache.model_copy(
            update={"enabled": True})})})
    reg = _two_regions()
    cache = DeterministicSemanticCache(ttl_seconds=3600)
    rec = RecordingBackend(_DeterministicLiteLLM(s))
    pipe = _pipeline(rec, s, reg, cache=cache)
    req = ChatRequest(messages=[Message(role="user", content="hello there")])

    # 1. an app with no regional requirement warms the cache from `eu-8b`.
    warm = _run(pipe.run("r1", "acme", req,
                         app_policy=AppPolicyView(allowed_models=frozenset({"eu-8b"}))))
    assert warm["model"] == "eu-8b"
    assert warm["policy"]["checks"]["chosen_in_eligible_set"] is True

    # 2. the same tenant, now requiring `us`, hits that cached `eu-8b` answer.
    hit = _run(pipe.run("r2", "acme", req,
                        app_policy=AppPolicyView(allowed_regions=frozenset({"us"}))))
    assert hit["model"] == "eu-8b"
    assert any(r.get("step") == "cache" and r.get("hit") for r in hit["trace"])
    assert hit["policy"]["checks"]["chosen_in_eligible_set"] is False
