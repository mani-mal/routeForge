"""P1b Task 4 — the three endpoint data-handling exclusion rules.

Design: docs/superpowers/specs/2026-09-19-endpoint-data-handling-p1b-design.md §4.

| rule          | condition                                                   | code                          | unknown |
|---------------|-------------------------------------------------------------|-------------------------------|---------|
| region        | app sets `allowed_regions` and the endpoint's is not in it   | region_not_allowed            | excluded |
| retention     | app sets `require_no_retention` and retention is not `none`  | prompt_retention_not_allowed  | excluded |
| raw sensitive | the request sends raw sensitive content and the endpoint     | raw_sensitive_not_accepted    | KEPT    |
|               | declares `accepts_raw_sensitive: false`                      |                               |         |

The last column is deliberately not uniform, and the tests below assert both
readings. Region and retention exclude an `unknown` endpoint because the APP
declared a requirement and an endpoint that cannot prove it does not qualify.
The raw-sensitive rule has no app-declared requirement behind it -- it fires
from the request's own shape -- so an undeclared endpoint keeps today's
behaviour and only an operator's explicit `false` is enforced. (§4 of the
design carries the full justification, and records that this supersedes its
original "does not declare `accepts_raw_sensitive: true`" wording.)

Two properties this file exists to pin, beyond the rules themselves:

* **Unknown never satisfies a requirement, and never blocks a request that
  imposes none.** Every endpoint in every deployment today is `unknown` on all
  three, so an app that sets neither requirement must route byte-identically
  to before this branch landed -- including under a `raw` model_input over a
  sensitive request, which is the sharp case
  (`test_an_undeclared_pool_under_raw_sensitive_routes_exactly_as_before`).
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

    # raw + sensitive -> the endpoint that declares `false` is refused. The
    # one that declares NOTHING is kept: see
    # `test_raw_sensitive_rule_keeps_an_endpoint_that_declares_nothing`.
    raw_ctx = _ctx(reg, policy, entities=ssn, model_input="raw")
    assert raw_ctx.sensitivity == "restricted"
    fs_raw = feasible_set(S, reg, raw_ctx, tenant_id="acme")
    assert _names(fs_raw) == {"trusted", "silent"}
    assert _by_code(fs_raw)["untrusted"] == RAW_SENSITIVE_NOT_ACCEPTED
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


def test_raw_sensitive_rule_keeps_an_endpoint_that_declares_nothing():
    """The deliberate asymmetry with region/retention, and the reason this
    branch is upgrade-safe.

    Region and retention exclude an `unknown` endpoint because the APP
    declared a requirement and an undeclared endpoint cannot prove it. The
    raw-sensitive rule has no app-declared requirement behind it -- it fires
    from the request's own shape -- so excluding `unknown` would retire every
    endpoint in every existing deployment by a rule nobody asked for. Only an
    operator's explicit `false` is enforced.
    """
    from app.models import Entity

    reg = _registry({"silent": _onprem("silent"),
                     "refuses": _onprem("refuses", accepts_raw_sensitive=False,
                                        tier="mid")})
    ssn = [Entity(type="US_SSN", start=0, end=11, value="123-45-6789")]
    ctx = _ctx(reg, AppPolicyView(), entities=ssn, model_input="raw")

    fs = feasible_set(S, reg, ctx, tenant_id="acme")
    assert _names(fs) == {"silent"}
    assert _by_code(fs)["refuses"] == RAW_SENSITIVE_NOT_ACCEPTED


def test_passthrough_app_counts_as_sending_raw_sensitive_content():
    """The one app class that by definition sends unredacted content.

    `policy_mode: passthrough` resolves to `model_input: raw` AND
    `detection: off`. With no detector nothing is ever detected, so no
    sensitivity rule matches, the request classifies `public`, and an
    "above public" test alone would leave the rule permanently inert for
    exactly the posture it exists to constrain. An app that classifies nothing
    cannot prove its content is public, so an endpoint that DECLARED it will
    not accept raw sensitive content is refused -- the same fail-closed
    reading as "unknown never satisfies a requirement", on the request side.
    """
    reg = _registry({
        "trusted": _onprem("trusted", accepts_raw_sensitive=True),
        "refuses": _onprem("refuses", accepts_raw_sensitive=False, tier="mid"),
    })
    ctx = _ctx(reg, AppPolicyView(policy_mode="passthrough"))

    # The gate detected nothing (there is no detector) -- the point of the fix.
    assert ctx.sensitivity == "public"
    assert ctx.sends_raw_sensitive is True

    fs = feasible_set(S, reg, ctx, tenant_id="acme")
    assert _names(fs) == {"trusted"}
    assert _by_code(fs)["refuses"] == RAW_SENSITIVE_NOT_ACCEPTED
    assert _by_reason(fs)["refuses"].startswith(
        "excluded · raw sensitive content")


def test_passthrough_app_over_an_undeclared_pool_excludes_nothing():
    """Upgrade safety, for the passthrough posture specifically.

    The rule above can only ever bite on an operator's EXPLICIT
    `accepts_raw_sensitive: false`. Nothing backfills that column, so every
    endpoint in every existing deployment reads `unknown` and stays eligible --
    a passthrough app that was routing before this branch routes identically
    after it.
    """
    reg = _registry({"a-8b": _onprem("a-8b"), "b-32b": _onprem("b-32b", tier="mid")})
    ctx = _ctx(reg, AppPolicyView(policy_mode="passthrough"))

    assert ctx.sends_raw_sensitive is True      # the rule IS armed ...
    fs = feasible_set(S, reg, ctx, tenant_id="acme")
    assert fs.rejected == ()                     # ... and excludes nothing
    assert _names(fs) == {"a-8b", "b-32b"}
    assert Router(S, registry=reg).select(
        ctx, tokens_after=20, complexity=0.1).chosen is not None


def test_an_undeclared_pool_under_raw_sensitive_routes_exactly_as_before():
    """Design §12's upgrade-safety promise, at its sharpest: the whole pool
    declares nothing, the app declares nothing, and the request is the most
    sensitive shape there is (raw model_input + an SSN). Nothing is excluded
    and the request still routes."""
    from app.models import Entity

    reg = _registry({"a-8b": _onprem("a-8b"), "b-32b": _onprem("b-32b", tier="mid")})
    ssn = [Entity(type="US_SSN", start=0, end=11, value="123-45-6789")]
    ctx = _ctx(reg, AppPolicyView(), entities=ssn, model_input="raw")

    assert ctx.sends_raw_sensitive is True     # the rule IS armed ...
    fs = feasible_set(S, reg, ctx, tenant_id="acme")
    assert fs.rejected == ()                    # ... and excludes nothing
    assert _names(fs) == {"a-8b", "b-32b"}
    assert Router(S, registry=reg).select(
        ctx, tokens_after=20, complexity=0.1).chosen is not None


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


def test_a_cache_hit_from_a_data_handling_excluded_model_is_a_miss():
    """A stored entry whose model is NOT in THIS request's eligible set is
    never served -- the lookup is a MISS and the request routes normally.

    This test used to assert the opposite half of the same fact: that the
    cached `eu-8b` answer WAS served, with the certificate honestly recording
    `chosen_in_eligible_set: False`. An honest certificate attached to a
    response that should never have been sent is not a fix -- the cache
    bypasses the router entirely, so the eligibility check at lookup time is
    the ONLY thing standing between a region-excluded endpoint's output and
    the client. So the check now gates the serve instead of annotating it,
    and a served hit's `chosen_in_eligible_set` is True by construction.
    """
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
    assert rec.models_called == ["eu-8b"]

    # 2. the same tenant, now requiring `us`, finds that `eu-8b` entry (same
    #    partition, same query) and must NOT be served it.
    hit = _run(pipe.run("r2", "acme", req,
                        app_policy=AppPolicyView(allowed_regions=frozenset({"us"}))))
    assert hit["model"] == "us-8b"                    # routed, not served
    assert rec.models_called == ["eu-8b", "us-8b"]    # the model really ran
    cache_recs = [r for r in hit["trace"] if r.get("step") == "cache"]
    assert cache_recs and cache_recs[0]["hit"] is False
    assert cache_recs[0]["bypass_reason"] == "entry_not_in_eligible_set"
    assert hit["policy"]["checks"]["chosen_in_eligible_set"] is True


def test_a_refused_cache_entry_is_not_evicted_by_the_refusing_read():
    """A refusal is a read-only judgement about THIS request, not a statement
    that the entry is invalid -- so the read must not delete it.

    **This has to be asserted in a narrow window, and the window is the
    point.** The refusing request goes on to route normally and then STORES
    its own answer under the same partition and the same query key, so a
    `cache.size` (or a later lookup) taken after the request completes cannot
    tell a kept entry from an evicted one -- both read 1. The only moment at
    which the refusal's own effect is observable is between the refused read
    and that store, which is what `_SnapshottingCache` captures.

    That store-over is also why keeping the entry does not, today, preserve
    the sibling's hit: see the write-amplification note in
    docs/architecture/specs/semantic-cache-residency-spec.md C9. What this
    test pins is the narrower, real property -- `lookup` does not mutate.
    """
    from app.cache.store import DeterministicSemanticCache

    class _SnapshottingCache(DeterministicSemanticCache):
        """Records what the store held at the moment each `store()` began."""

        def __init__(self, *a, **kw) -> None:
            super().__init__(*a, **kw)
            self.at_store: list[list[str]] = []

        def store(self, **kw):
            self.at_store.append(sorted(
                e.model for b in self._data.values() for e in b.values()))
            return super().store(**kw)

    s = S.model_copy(update={"serving": S.serving.model_copy(update={
        "semantic_cache": S.serving.semantic_cache.model_copy(
            update={"enabled": True})})})
    reg = _two_regions()
    cache = _SnapshottingCache(ttl_seconds=3600)
    rec = RecordingBackend(_DeterministicLiteLLM(s))
    pipe = _pipeline(rec, s, reg, cache=cache)
    eu_only = AppPolicyView(allowed_models=frozenset({"eu-8b"}))
    req = ChatRequest(messages=[Message(role="user", content="hello there")])

    warm = _run(pipe.run("r1", "acme", req, app_policy=eu_only))
    assert warm["model"] == "eu-8b"
    assert cache.at_store == [[]]          # nothing cached before the first store

    # A sibling app of the same tenant requires `us`: it finds the `eu-8b`
    # entry, refuses it, and routes to `us-8b`.
    hit = _run(pipe.run("r2", "acme", req,
                        app_policy=AppPolicyView(allowed_regions=frozenset({"us"}))))
    assert hit["model"] == "us-8b"
    cache_recs = [r for r in hit["trace"] if r.get("step") == "cache"]
    assert cache_recs[0]["bypass_reason"] == "entry_not_in_eligible_set"

    # THE ASSERTION: at the moment r2 stored its own answer -- i.e. after the
    # refused read -- the `eu-8b` entry was still there. Under an
    # evict-on-refusal policy this list would be empty.
    assert len(cache.at_store) == 2
    assert cache.at_store[1] == ["eu-8b"]


# --- 8. failover and escalation stay inside the feasible set ----------------


class _FailForModels(_DeterministicLiteLLM):
    """Records every model the gateway actually reached, and fails the named
    ones so the failover path runs."""

    def __init__(self, settings, fail: set[str]) -> None:
        super().__init__(settings)
        self._fail = set(fail)
        self.calls: list[str] = []

    async def generate(self, model, messages, price, tier, hint, tools=None,
                       tool_choice=None, params=None):
        self.calls.append(model)
        if model in self._fail:
            raise RuntimeError(f"forced failure for {model}")
        return await super().generate(model, messages, price, tier, hint)


def _gateway(backend, registry, settings=S) -> Gateway:
    return Gateway(settings, backend, Router(settings, registry=registry), registry)


def test_a_data_handling_excluded_endpoint_is_never_tried_on_failover():
    """The first-choice endpoint fails; the excluded one must not be the retry,
    even though it is healthy, cheaper and in the pool."""
    reg = _registry({
        "eu-cheap": _onprem("eu-cheap", region="eu", price=0.01),   # cheapest
        "us-a": _onprem("us-a", region="us", price=0.20),
        "us-b": _onprem("us-b", region="us", price=0.30),
    })
    msgs = [Message(role="user", content="hello there")]

    # Control: with no regional requirement, `eu-cheap` is what the router
    # picks first -- so its absence below is an exclusion, not an accident.
    open_ctx = _ctx(reg, AppPolicyView())
    open_backend = _FailForModels(S, set())
    _run(_gateway(open_backend, reg).run(open_ctx, msgs, 20, 0.1, {}))
    assert open_backend.calls == ["eu-cheap"]

    ctx = _ctx(reg, AppPolicyView(allowed_regions=frozenset({"us"})))
    backend = _FailForModels(S, {"us-a"})
    result, records = _run(_gateway(backend, reg).run(ctx, msgs, 20, 0.1, {}))

    assert result.model == "us-b"
    assert backend.calls == ["us-a", "us-b"]
    assert "eu-cheap" not in backend.calls
    # ... and the retry's own candidate set still names the region exclusion,
    # not a softer reason.
    retry = [r for r in records if r.step == "router"][-1]
    reasons = {c.name: c.reason for c in retry.rejected}
    assert reasons["eu-cheap"].startswith("excluded · region")
    assert reasons["us-a"] == "excluded · prior failure"


def test_escalation_stays_inside_the_feasible_set():
    """A confidence escalation raises the target tier; the higher tier's
    excluded endpoint must stay unreachable, even when it is the only cheap
    way to satisfy the new floor."""
    reg = _registry({
        "us-8b": _onprem("us-8b", region="us", price=0.05),
        "eu-32b": _onprem("eu-32b", tier="mid", region="eu", price=0.01),
        "us-32b": _onprem("us-32b", tier="mid", region="us", price=0.40),
    })
    msgs = [Message(role="user", content="hello there")]

    # Control: with no regional requirement the escalation lands on `eu-32b`
    # (cheapest mid) -- so its absence below is the exclusion doing the work,
    # not the escalation simply never reaching mid.
    open_backend = _FailForModels(S, set())
    _run(_gateway(open_backend, reg).run(
        _ctx(reg, AppPolicyView()), msgs, 20, 0.1, {"mock_confidence": 0.1}))
    assert open_backend.calls == ["us-8b", "eu-32b"]

    ctx = _ctx(reg, AppPolicyView(allowed_regions=frozenset({"us"})))
    backend = _FailForModels(S, set())
    result, records = _run(_gateway(backend, reg).run(
        ctx, msgs, 20, 0.1, {"mock_confidence": 0.1}))

    # cheap served first, confidence below threshold -> escalate to mid.
    assert backend.calls == ["us-8b", "us-32b"]
    assert "eu-32b" not in backend.calls
    assert result.model == "us-32b"
    tiers = [r.chosen_tier for r in records if r.step == "router"]
    assert tiers == ["cheap", "mid"]


# --- 9. the healthcare scenario, end to end at the HTTP boundary -------------
#
# Design §12's definition of done, and the priorities doc's §6 table. Every
# assertion below is made on what a CLIENT receives from
# `POST /v1/chat/completions` -- not on an internal `feasible_set` call -- so
# it pins the whole path: admin onboarding of the four endpoints, the app's
# two requirements, the gate detecting the SSN, stage 2 narrowing the
# candidate set, and the certificate/trace naming the exclusions in the
# operator's own vocabulary.
#
#   | endpoint            | region | retention | accepts_raw_sensitive | verdict |
#   |---------------------|--------|-----------|-----------------------|---------|
#   | local-8b-us         | us     | none      | true                  | eligible |
#   | cloud-us            | us     | none      | FALSE                 | eligible -- AFTER REDACTION |
#   | cloud-eu            | eu     | none      | true                  | region_not_allowed |
#   | cloud-us-retaining  | us     | RETAINED  | true                  | prompt_retention_not_allowed |
#
# `cloud-us` is the priorities doc's "Azure US -- permitted after redaction"
# row, and it is the reason the scenario is asserted twice: under the default
# `enforced` posture the model is shown placeholders, so the raw-sensitive
# rule does not fire and `cloud-us` survives; flip the app to
# `model_input: raw` and the SAME endpoint leaves the set with
# `raw_sensitive_not_accepted`. One declaration, two request shapes.


HEALTHCARE_ENDPOINTS = {
    # slug                  tier     region  retention   accepts_raw_sensitive
    "local-8b-us":        ("cheap", "us",   "none",     True),
    "cloud-us":           ("mid",   "us",   "none",     False),
    "cloud-eu":           ("mid",   "eu",   "none",     True),
    "cloud-us-retaining": ("mid",   "us",   "retained", True),
}

_SSN_PROMPT = {"model": "auto", "messages": [
    {"role": "user",
     "content": "Patient SSN is 123-45-6789 — summarise the intake note."}]}


def _healthcare_client(harness, client):
    harness.disable_seeded_models(client)
    for slug, (tier, region, retention, raw_ok) in HEALTHCARE_ENDPOINTS.items():
        harness.add_model(client, slug, tier=tier, price_input=0.1,
                          price_output=0.1, region=region,
                          prompt_retention=retention,
                          accepts_raw_sensitive=raw_ok)


def _router_record(body: dict) -> dict:
    return [r for r in body["trace"] if r.get("step") == "router"][0]


def _data_handling_exclusions(record: dict) -> dict[str, str]:
    """Only the stage-2 rejections. A rejection with `code: null` is an
    ordinary routing reason (tier, score, budget) and is NOT an exclusion --
    the endpoint was still in the feasible set."""
    return {r["name"]: r["code"] for r in record["rejected"] if r.get("code")}


def test_healthcare_scenario_over_http_two_survivors_two_named_exclusions():
    """Design §12: four endpoints, an SSN request, two survivors, and the two
    exclusions explained in the operator's own vocabulary."""
    from tests.onboarding import harness

    with harness.conformance_client() as client:
        _healthcare_client(harness, client)
        app = harness.onboard(client, "general-health",
                              allowed_regions=["us"], require_no_retention=True)

        r = app.chat(_SSN_PROMPT)
        assert r.status_code == 200, r.text
        body = r.json()

        # The gate ran first and classified the request off the SSN: the
        # data-handling rules are narrowing a set that residency has already
        # decided (rule 3), never the other way round.
        assert body["policy"]["detected_entity_types"] == ["US_SSN"]
        assert body["policy"]["placement"] == "onprem_only"
        assert body["policy"]["model_input_mode"] == "redact"

        record = _router_record(body)
        # The two non-compliant endpoints, each named by its own code.
        assert _data_handling_exclusions(record) == {
            "cloud-eu": REGION_NOT_ALLOWED,
            "cloud-us-retaining": PROMPT_RETENTION_NOT_ALLOWED,
        }
        # ... in the customer's vocabulary, with both sides of the mismatch.
        reasons = {r["name"]: r["reason"] for r in record["rejected"]}
        assert reasons["cloud-eu"] == (
            "excluded · region · endpoint declares 'eu', application allows 'us'")
        assert reasons["cloud-us-retaining"] == (
            "excluded · prompt retention · endpoint declares 'retained', "
            "application requires 'none'")

        # The two compliant endpoints survive. `cloud-us` declares
        # accepts_raw_sensitive: false and is STILL eligible, because the
        # compiled model_input is `redact` -- the priorities doc's "eligible
        # after redaction" row, obtained from P1a's existing axis.
        survivors = {"local-8b-us", "cloud-us"}
        assert survivors.isdisjoint(_data_handling_exclusions(record))
        assert record["chosen"] in survivors
        # Whichever of the two the router did not serve was set aside for an
        # ordinary routing reason (tier), not excluded.
        not_served = (survivors - {record["chosen"]}).pop()
        assert {r["name"]: r["code"] for r in record["rejected"]}.get(
            not_served, "absent") in (None, "absent")

        # The decision names the registry it was made against, and what the
        # SERVED endpoint declared -- with the provenance of each value, so an
        # auditor can see the decision rested on a per-model declaration
        # rather than a provider default (design §6).
        policy = body["policy"]
        assert len(policy["endpoint_registry_version"]) == 12
        assert policy["endpoint_data_handling"]["region"] == "us"
        assert policy["endpoint_data_handling"]["prompt_retention"] == "none"
        assert policy["endpoint_data_handling"]["source"] == {
            "region": "model", "prompt_retention": "model",
            "accepts_raw_sensitive": "model"}
        assert policy["checks"]["chosen_in_eligible_set"] is True


def test_healthcare_scenario_over_http_raw_input_drops_the_redaction_only_endpoint():
    """Same four endpoints, same app requirements, same SSN -- but the app is
    configured to show the model raw input. `cloud-us`, which declares
    `accepts_raw_sensitive: false`, now leaves the set as
    `raw_sensitive_not_accepted`; the other two exclusions are unchanged, and
    the endpoint declaring `true` serves the request."""
    from tests.onboarding import harness

    with harness.conformance_client() as client:
        _healthcare_client(harness, client)
        app = harness.onboard(client, "general-health-raw",
                              allowed_regions=["us"], require_no_retention=True,
                              model_input="raw", client_output="redact")

        r = app.chat(_SSN_PROMPT)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["policy"]["model_input_mode"] == "raw"

        record = _router_record(body)
        assert _data_handling_exclusions(record) == {
            "cloud-eu": REGION_NOT_ALLOWED,
            "cloud-us-retaining": PROMPT_RETENTION_NOT_ALLOWED,
            "cloud-us": RAW_SENSITIVE_NOT_ACCEPTED,
        }
        assert record["chosen"] == "local-8b-us"


def test_healthcare_endpoints_are_all_eligible_for_an_app_that_requires_nothing():
    """The upgrade-safety half of design §12, asserted on the same four
    endpoints over HTTP: an app that sets neither requirement reaches all of
    them, so nothing about declaring these fields narrows anybody else."""
    from tests.onboarding import harness

    with harness.conformance_client() as client:
        _healthcare_client(harness, client)
        app = harness.onboard(client, "no-requirements")

        r = app.chat(_SSN_PROMPT)
        assert r.status_code == 200, r.text
        record = _router_record(r.json())
        assert _data_handling_exclusions(record) == {}
