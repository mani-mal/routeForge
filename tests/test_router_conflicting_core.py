"""P3b Task 3: the Router attaches the minimal conflicting core to every
`NoEligibleCandidate` it raises (spec §2.1-§2.4, §3, §5.8).

Two raise sites:

* **policy-empty** -- `feasible_set` left nothing. The core is computed over the
  whole AVAILABLE pool (`violation_matrix`); endpoint state (disabled, prior
  failure, an empty pool) is reported separately and never enters the core.
* **budget-empty** -- the policy set was non-empty but every candidate the
  budget stage could use is over the cap. The core is computed over the §2.3
  set: the policy-eligible candidates at or below the picked tier, each
  violating `max_cost_usd_per_request` and/or `risk_floor`.

The message text is unchanged apart from ` [<core.summary()>]` appended.
"""
from __future__ import annotations

import asyncio
import logging

import pytest

from app.apps import store
from app.apps.db import Database
from app.apps.policy import AppPolicyView
from app.apps.registry import ModelRegistry
from app.config.loader import load_settings
from app.errors import NoEligibleCandidate
from app.models import Entity, ExecutionPolicyContext
from app.policy.context import build_context
from app.routing import router as router_mod
from app.routing.router import Router

# Shipped default: both cloud providers DISABLED, so the pool is exactly the
# onboarded rows each test declares.
S_ONPREM = load_settings()

# Both cloud providers on, so there are cloud candidates to exclude.
_prov = dict(S_ONPREM.serving.providers)
_prov["azure"] = _prov["azure"].model_copy(update={"enabled": True})
_prov["bedrock"] = _prov["bedrock"].model_copy(update={"enabled": True})
S_CLOUD = S_ONPREM.model_copy(
    update={"serving": S_ONPREM.serving.model_copy(update={"providers": _prov})})


def _onprem(slug, *, tier="cheap", enabled=True, region=None,
            prompt_retention=None, price=0.1):
    return dict(display_name=slug, provider="onprem", tier=tier,
                price_input=price, price_output=price,
                endpoint=f"http://{slug}/v1", enabled=enabled,
                region=region, prompt_retention=prompt_retention)


async def _build_registry(settings, rows):
    db = Database(url="sqlite+aiosqlite://")
    await db.ensure_ready()
    async with db.session() as s:
        for slug, fields in rows.items():
            await store.upsert_onboarded(s, slug, **fields)
        await s.commit()
    reg = ModelRegistry(settings)
    await reg.refresh(db)
    return reg


def _registry(settings, rows):
    return asyncio.run(_build_registry(settings, rows))


def _ctx(settings, registry, policy, *, entities=(), requested_model=None):
    ctx = build_context(settings, "req-1", "acme", list(entities), None,
                        app_policy=policy, registry=registry)
    if requested_model is not None:
        ctx = ctx.model_copy(update={"requested_model": requested_model})
    return ctx


def _raise(settings, registry, ctx, **kw) -> NoEligibleCandidate:
    with pytest.raises(NoEligibleCandidate) as exc:
        Router(settings, registry=registry).select(
            ctx, tokens_after=20, complexity=0.1, **kw)
    return exc.value


# --- 1. residency + pinned model ---------------------------------------------

def test_onprem_only_with_a_pin_to_neither_gives_residency_and_requested_model():
    reg = _registry(S_CLOUD, {"local-8b": _onprem("local-8b")})
    ctx = _ctx(S_CLOUD, reg, AppPolicyView(residency="onprem_only"),
               requested_model="azure-gpt-mid")
    err = _raise(S_CLOUD, reg, ctx)

    assert err.core is not None
    assert err.core.ids() == ("residency", "requested_model")
    assert err.core.state_ids() == ()
    by_id = {r.id: r for r in err.core.requirements}
    assert by_id["residency"].source == "app_residency"
    assert by_id["residency"].detail == {"value": "onprem_only"}
    assert by_id["requested_model"].detail == {"value": "azure-gpt-mid"}
    assert err.core.candidates_considered == 3


# --- 2. two data-handling requirements, each met by a different endpoint ------

def test_region_and_retention_each_met_by_a_different_endpoint():
    reg = _registry(S_ONPREM, {
        "eu-keeps": _onprem("eu-keeps", region="eu", prompt_retention="retained"),
        "us-discards": _onprem("us-discards", region="us", prompt_retention="none"),
    })
    policy = AppPolicyView(allowed_regions=frozenset({"eu"}),
                           require_no_retention=True)
    err = _raise(S_ONPREM, reg, _ctx(S_ONPREM, reg, policy))

    assert err.core.ids() == ("allowed_regions", "require_no_retention")
    by_id = {r.id: r for r in err.core.requirements}
    assert by_id["allowed_regions"].detail == {"regions": ["eu"]}


# --- 3/4. the budget raise ---------------------------------------------------

def _budget_registry(with_cheap: bool):
    rows = {"big-frontier": _onprem("big-frontier", tier="frontier", price=1000.0)}
    if with_cheap:
        rows["small-cheap"] = _onprem("small-cheap", tier="cheap", price=0.1)
    return _registry(S_ONPREM, rows)


def _high_risk_ctx(reg):
    # An SSN makes the request risk_class high (floor: frontier) -- and
    # onprem_only, which the all-onprem pool satisfies.
    ssn = [Entity(type="US_SSN", start=0, end=11, value="123-45-6789")]
    return _ctx(S_ONPREM, reg, AppPolicyView(max_cost_usd_per_request=0.01),
                entities=ssn)


def test_budget_floor_over_cap_and_cheap_below_floor():
    reg = _budget_registry(with_cheap=True)
    ctx = _high_risk_ctx(reg)
    assert ctx.risk_class == "high"
    err = _raise(S_ONPREM, reg, ctx)

    assert str(err).startswith("no candidates after budget filter:")
    assert err.core.ids() == ("max_cost_usd_per_request", "risk_floor")
    assert err.core.state_ids() == ()
    by_id = {r.id: r for r in err.core.requirements}
    assert by_id["risk_floor"].detail == {"floor": "frontier", "risk_class": "high"}
    cost = by_id["max_cost_usd_per_request"].detail
    assert cost["cap"] == pytest.approx(0.01)
    # the cheapest estimate among the COST VIOLATORS -- never the below-floor
    # candidate that fits, which would read below the cap beside "violates cost"
    assert cost["cheapest_estimate"] > cost["cap"]
    assert cost["cheapest_estimate"] == pytest.approx((20 + 512) * 1000.0 / 1e6)
    assert err.core.candidates_considered == 2


def test_budget_with_no_below_floor_candidate_is_cost_alone():
    reg = _budget_registry(with_cheap=False)
    err = _raise(S_ONPREM, reg, _high_risk_ctx(reg))

    assert err.core.ids() == ("max_cost_usd_per_request",)
    cost = err.core.requirements[0].detail
    assert cost["cheapest_estimate"] > cost["cap"]


# --- 5/6. state only ---------------------------------------------------------

def test_empty_pool_is_no_endpoints_state():
    reg = _registry(S_ONPREM, {})
    err = _raise(S_ONPREM, reg, _ctx(S_ONPREM, reg, AppPolicyView()))

    assert err.core.ids() == ()
    assert err.core.state_ids() == ("no_endpoints",)
    assert str(err).startswith("no candidates after residency filter: the model pool is empty")


def test_a_disabled_only_pool_reads_disabled_not_no_endpoints():
    """`effective_pool()` drops a disabled row, but the actionable fact is
    "re-enable it", so the core reports it as `disabled` state (via
    `ModelRegistry.disabled_onboarded()`), not `no_endpoints`."""
    reg = _registry(S_ONPREM, {"off": _onprem("off", enabled=False)})
    err = _raise(S_ONPREM, reg, _ctx(S_ONPREM, reg, AppPolicyView(residency="onprem_only")))

    assert err.core.ids() == ()
    assert err.core.state_ids() == ("disabled",)
    assert err.core.state == (("disabled", ("off",)),)


def test_onprem_only_with_its_only_onprem_model_disabled_and_cloud_enabled():
    reg = _registry(S_CLOUD, {"off": _onprem("off", enabled=False)})
    err = _raise(S_CLOUD, reg, _ctx(S_CLOUD, reg, AppPolicyView(residency="onprem_only")))

    assert err.core.ids() == ("residency",)
    assert err.core.state_ids() == ("disabled",)
    assert err.core.state == (("disabled", ("off",)),)
    # explanation only: the disabled row is never a candidate
    assert err.core.candidates_considered == 2   # the two cloud models


def test_a_disabled_row_restricted_to_another_app_is_not_reported():
    rows = {"off": dict(_onprem("off", enabled=False), allowed_app_ids=["other-app"])}
    reg = _registry(S_ONPREM, rows)
    err = _raise(S_ONPREM, reg, _ctx(S_ONPREM, reg, AppPolicyView()))
    assert err.core.state_ids() == ("no_endpoints",)


def test_disabled_rows_do_not_touch_eligibility():
    from app.policy.feasible import feasible_set
    reg = _registry(S_ONPREM, {"on": _onprem("on"), "off": _onprem("off", enabled=False)})
    assert reg.disabled_onboarded() == ("off",)
    fs = feasible_set(S_ONPREM, reg, AppPolicyView(), tenant_id="acme")
    assert {c.name for c in fs.eligible} == {"on"}
    assert fs.rejected == ()


def test_only_a_disabled_model_is_disabled_state():
    """Disabled between the pool listing and the eligibility check -- the one
    way a disabled row is still in the pool the seam iterates (same technique
    as tests/test_policy_violations.py::test_state_is_not_a_violation)."""
    reg = _registry(S_ONPREM, {"off": _onprem("off")})
    ctx = _ctx(S_ONPREM, reg, AppPolicyView(residency="onprem_only"))
    reg.is_enabled = lambda slug: slug != "off"
    err = _raise(S_ONPREM, reg, ctx)

    assert err.core.ids() == ()
    assert err.core.state_ids() == ("disabled",)
    assert err.core.state == (("disabled", ("off",)),)


def test_prior_failure_is_state_not_policy():
    reg = _registry(S_ONPREM, {"local-8b": _onprem("local-8b")})
    err = _raise(S_ONPREM, reg, _ctx(S_ONPREM, reg, AppPolicyView()),
                 exclude=frozenset({"local-8b"}))
    assert err.core.ids() == ()
    assert err.core.state_ids() == ("prior_failure",)


# --- 7. a sensitivity-rule escalation ----------------------------------------

def test_sensitivity_rule_escalation_names_the_entity_types():
    reg = _registry(S_CLOUD, {})
    ssn = [Entity(type="US_SSN", start=0, end=11, value="123-45-6789")]
    ctx = _ctx(S_CLOUD, reg, AppPolicyView(residency="any"), entities=ssn)
    assert ctx.onprem_only is True and ctx.app_residency == "any"
    err = _raise(S_CLOUD, reg, ctx)

    assert err.core.ids() == ("residency",)
    req = err.core.requirements[0]
    assert req.source == "sensitivity_rule"
    assert req.detail["entities"] == ["US_SSN"]
    assert req.detail["value"] == "onprem_only"
    # content-free: the matched text never appears
    assert "123-45-6789" not in str(err.core.to_dict())
    assert "123-45-6789" not in str(err)


def test_escalation_names_only_the_rule_that_raised_residency():
    """SSN + PERSON: the SSN rule raises residency; PERSON is detected but is
    not that rule's entity, so it is not named."""
    reg = _registry(S_CLOUD, {})
    ents = [Entity(type="US_SSN", start=0, end=11, value="123-45-6789"),
            Entity(type="PERSON", start=12, end=20, value="Jane Doe")]
    ctx = _ctx(S_CLOUD, reg, AppPolicyView(residency="any"), entities=ents)
    assert ctx.detected_entities == ("PERSON", "US_SSN")
    assert ctx.escalation_entities == ("US_SSN",)
    err = _raise(S_CLOUD, reg, ctx)
    req = err.core.requirements[0]
    assert req.source == "sensitivity_rule"
    assert req.detail["entities"] == ["US_SSN"]


def test_no_escalation_when_the_rule_does_not_raise_residency():
    reg = _registry(S_CLOUD, {})
    ssn = [Entity(type="US_SSN", start=0, end=11, value="123-45-6789")]
    ctx = build_context(S_CLOUD, "req-1", "acme", ssn, None,
                        app_policy=AppPolicyView(residency="any"), registry=reg,
                        escalate_residency=False)
    assert ctx.escalation_entities == ()


def test_app_residency_onprem_only_is_not_attributed_to_a_rule():
    reg = _registry(S_CLOUD, {})
    ssn = [Entity(type="US_SSN", start=0, end=11, value="123-45-6789")]
    ctx = _ctx(S_CLOUD, reg, AppPolicyView(residency="onprem_only"), entities=ssn)
    assert ctx.escalation_entities == ()
    err = _raise(S_CLOUD, reg, ctx)
    req = err.core.requirements[0]
    assert req.source == "app_residency"
    assert "entities" not in req.detail


# --- 8. the message ------------------------------------------------------------

def test_message_keeps_the_old_text_and_appends_the_summary():
    reg = _registry(S_CLOUD, {"local-8b": _onprem("local-8b")})
    ctx = _ctx(S_CLOUD, reg, AppPolicyView(residency="onprem_only"),
               requested_model="azure-gpt-mid")
    err = _raise(S_CLOUD, reg, ctx)
    msg = str(err)
    assert msg.startswith("no candidates after residency filter: ")
    assert "excluded · residency" in msg
    assert msg.endswith(f" [{err.core.summary()}]")
    assert err.rejected   # the P2b-2 rejected tuple is unchanged


def test_budget_message_appends_the_summary():
    reg = _budget_registry(with_cheap=True)
    err = _raise(S_ONPREM, reg, _high_risk_ctx(reg))
    msg = str(err)
    assert "over the per-request cap" in msg
    assert msg.endswith(f" [{err.core.summary()}]")


# --- robustness: the core never changes routing or the exception type --------

def test_core_failure_is_fail_safe(monkeypatch, caplog):
    def boom(*a, **kw):
        raise RuntimeError("core exploded")
    monkeypatch.setattr(router_mod, "build_core", boom)
    reg = _registry(S_CLOUD, {"local-8b": _onprem("local-8b")})
    ctx = _ctx(S_CLOUD, reg, AppPolicyView(residency="onprem_only"),
               requested_model="azure-gpt-mid")
    with caplog.at_level(logging.ERROR):
        err = _raise(S_CLOUD, reg, ctx)
    assert type(err) is NoEligibleCandidate
    assert err.core is None
    assert str(err).startswith("no candidates after residency filter: ")
    assert "no endpoint satisfies all of" not in str(err)   # no summary appended
    assert "conflicting core" in caplog.text


def test_hand_built_context_with_an_unexplained_rejection_does_not_crash(caplog):
    """A hand-built context may refuse a candidate through `allowed_models`
    while leaving `app_allowed_*` None -- that candidate violates no
    requirement the matrix can name. The 422 is still raised; the core is
    built from the explainable candidates only."""
    reg = _registry(S_CLOUD, {"local-8b": _onprem("local-8b")})
    ctx = ExecutionPolicyContext(
        request_id="r", tenant_id="acme", sensitivity="public", onprem_only=True,
        allowed_providers=frozenset({"onprem"}), allowed_models=frozenset(),
        max_cost_usd=0.05, max_latency_ms=15000, risk_class="low",
        require_verification=False, detected_entities=(), redaction_map_id=None)
    with caplog.at_level(logging.WARNING):
        err = _raise(S_CLOUD, reg, ctx)
    assert err.core is not None
    assert err.core.ids() == ("residency",)
    assert "local-8b" in caplog.text


def test_budget_core_asserts_on_a_candidate_violating_neither():
    """By construction the budget raise has no candidate that fits the cap AND
    sits at or above the floor. If one ever appears, that is a router bug:
    the budget-core builder says so loudly rather than silently."""
    reg = _budget_registry(with_cheap=True)
    ctx = _high_risk_ctx(reg)
    r = Router(S_ONPREM, registry=reg)
    allowed = list(reg.effective_pool())
    with pytest.raises(AssertionError, match="violates neither"):
        # floor 'cheap' makes the fitting cheap candidate violate nothing
        r._budget_core(ctx, allowed, tier_pick="frontier", floor="cheap",
                       tokens_after=20, out_tokens=512)
