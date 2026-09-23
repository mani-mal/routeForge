"""Characterisation tests for the eligibility filter — the one decision that
says which pool candidates this request may reach, and the operator-facing
string that explains every candidate it may not.

These were written BEFORE the `FeasibleSet` refactor (P1b Task 3) and pass
against the un-refactored code. They are a safety net, not a red-green cycle:
they pin what the console already renders and what `GET /v1/models` already
lists, so the refactor that moves the decision behind one seam can be shown to
have changed nothing.

They deliberately assert only OBSERVABLE behavior — `Router.select`'s kept and
rejected candidates with their reason strings, `requestable_models`, and the
placement fields of the context — never an internal field's composition.
"""
from __future__ import annotations

import asyncio

import pytest

from app.apps import store
from app.apps.db import Database
from app.apps.policy import AppPolicyView
from app.apps.registry import ModelRegistry
from app.config.loader import load_settings
from app.errors import NoEligibleCandidate
from app.policy.context import build_context, requestable_models
from app.policy.feasible import feasible_set
from app.routing.router import Router

# Both cloud providers on, so residency/app-scope exclusions have something
# real to exclude (the shipped default disables them, which would starve these
# tests of the cloud candidates they are written against -- same reason
# tests/test_router.py enables them).
S = load_settings()
_prov = dict(S.serving.providers)
_prov["azure"] = _prov["azure"].model_copy(update={"enabled": True})
_prov["bedrock"] = _prov["bedrock"].model_copy(update={"enabled": True})
S = S.model_copy(update={"serving": S.serving.model_copy(update={"providers": _prov})})

CLOUD = {"azure-gpt-mid", "bedrock-frontier"}


def _run(coro):
    return asyncio.run(coro)


def _onprem(slug, *, tier="cheap", enabled=True, allowed_app_ids=None):
    return dict(display_name=slug, provider="onprem", tier=tier,
                price_input=0.1, price_output=0.1, endpoint=f"http://{slug}/v1",
                enabled=enabled, allowed_app_ids=allowed_app_ids)


async def _build_registry(rows):
    db = Database(url="sqlite+aiosqlite://")
    await db.ensure_ready()
    async with db.session() as s:
        for slug, fields in rows.items():
            await store.upsert_onboarded(s, slug, **fields)
        await s.commit()
    reg = ModelRegistry(S)
    await reg.refresh(db)
    return reg


def _registry(rows=None):
    return _run(_build_registry(rows if rows is not None else {
        "local-8b": _onprem("local-8b", tier="cheap"),
        "local-32b": _onprem("local-32b", tier="mid")}))


def _ctx(registry, policy, *, tenant_id="acme", requested_model=None):
    ctx = build_context(S, "req-1", tenant_id, [], None,
                        app_policy=policy, registry=registry)
    if requested_model is not None:
        ctx = ctx.model_copy(update={"requested_model": requested_model})
    return ctx


def _reasons(decision):
    return {r.name: r.reason for r in decision.rejected}


def _kept(decision, registry, policy, **kw):
    """Every candidate the eligibility filter kept, whatever the tier/budget
    stages later did with it: the eligible set is what this task is about, so
    read it from the rejection reasons rather than from `chosen` alone."""
    filtered = {"excluded · residency", "not permitted for application",
                "not requested model", "disabled by admin",
                "excluded · prior failure"}
    rejected_by_eligibility = {r.name for r in decision.rejected
                               if r.reason in filtered}
    pool = {c.name for c in registry.effective_pool()}
    return pool - rejected_by_eligibility


# --- 1. an onprem_only app with cloud enabled --------------------------------

def test_onprem_only_excludes_every_cloud_candidate_as_residency():
    reg = _registry()
    policy = AppPolicyView(residency="onprem_only")
    ctx = _ctx(reg, policy)

    assert ctx.onprem_only is True
    assert ctx.allowed_providers == frozenset({"onprem"})

    d = Router(S, registry=reg).select(ctx, tokens_after=20, complexity=0.1)
    assert d.chosen.provider == "onprem"
    reasons = _reasons(d)
    for name in CLOUD:
        assert reasons[name] == "excluded · residency"
    assert _kept(d, reg, policy) == {"local-8b", "local-32b"}


def test_onprem_only_with_no_onprem_model_rejects_and_never_reaches_cloud():
    """The derived reject: an empty (or all-disabled) on-prem pool drops
    `onprem` from the allowed providers, so residency cannot be satisfied and
    the request is refused -- never quietly served from the cloud."""
    reg = _registry({"off": _onprem("off", enabled=False)})
    ctx = _ctx(reg, AppPolicyView(residency="onprem_only"))

    assert ctx.allowed_providers == frozenset()
    assert ctx.allowed_models == frozenset()
    with pytest.raises(NoEligibleCandidate) as exc:
        Router(S, registry=reg).select(ctx, tokens_after=20, complexity=0.1)
    assert "no candidates after residency filter" in str(exc.value)
    assert "excluded · residency" in str(exc.value)


# --- 2. an app with allowed_tiers --------------------------------------------

def test_allowed_tiers_narrows_to_that_tier_as_not_permitted():
    reg = _registry()
    policy = AppPolicyView(allowed_tiers=frozenset({"cheap"}))
    ctx = _ctx(reg, policy)

    assert ctx.model_scope_active is True
    assert ctx.allowed_models == frozenset({"local-8b"})
    assert {c.name for c in requestable_models(S, reg, "acme", policy)} == {"local-8b"}

    d = Router(S, registry=reg).select(ctx, tokens_after=20, complexity=0.1)
    assert d.chosen.name == "local-8b"
    reasons = _reasons(d)
    for name in ("local-32b", *CLOUD):
        assert reasons[name] == "not permitted for application"
    assert _kept(d, reg, policy) == {"local-8b"}


def test_app_allowed_providers_narrowing_is_not_labeled_residency():
    """An app that permits only on-prem while its residency stays `any` is a
    policy exclusion, not a residency one -- the console tells an operator to
    look at a different setting."""
    reg = _registry()
    ctx = _ctx(reg, AppPolicyView(allowed_providers=frozenset({"onprem"})))

    assert ctx.onprem_only is False
    d = Router(S, registry=reg).select(ctx, tokens_after=20, complexity=0.1)
    for name in CLOUD:
        assert _reasons(d)[name] == "not permitted for application"


# --- 3. a pinned requested_model ---------------------------------------------

def test_requested_model_rejects_every_other_candidate_as_not_requested():
    reg = _registry()
    policy = AppPolicyView()
    ctx = _ctx(reg, policy, requested_model="local-32b")

    d = Router(S, registry=reg).select(ctx, tokens_after=20, complexity=0.1)
    assert d.chosen.name == "local-32b"
    reasons = _reasons(d)
    for name in ("local-8b", *CLOUD):
        assert reasons[name] == "not requested model"
    assert _kept(d, reg, policy) == {"local-32b"}


def test_residency_outranks_the_pin_on_a_cloud_candidate():
    """Ordering is load-bearing: a cloud candidate under onprem_only reads
    `residency`, never `not requested model`."""
    reg = _registry()
    ctx = _ctx(reg, AppPolicyView(residency="onprem_only"),
               requested_model="local-8b")

    d = Router(S, registry=reg).select(ctx, tokens_after=20, complexity=0.1)
    reasons = _reasons(d)
    for name in CLOUD:
        assert reasons[name] == "excluded · residency"
    assert reasons["local-32b"] == "not requested model"


# --- 4. a model scoped to another app via allowed_app_ids --------------------

def test_model_scoped_to_another_app_is_not_permitted_for_this_one():
    reg = _registry({
        "local-8b": _onprem("local-8b", tier="cheap"),
        "other-only": _onprem("other-only", tier="mid",
                              allowed_app_ids=["someone-else"])})
    policy = AppPolicyView()
    ctx = _ctx(reg, policy, tenant_id="acme")

    assert {c.name for c in requestable_models(S, reg, "acme", policy)} \
        == {"local-8b", *CLOUD}

    d = Router(S, registry=reg).select(ctx, tokens_after=20, complexity=0.1)
    assert _reasons(d)["other-only"] == "not permitted for application"
    assert "other-only" not in _kept(d, reg, policy)


def test_the_app_the_model_is_scoped_to_still_reaches_it():
    reg = _registry({
        "local-8b": _onprem("local-8b", tier="cheap"),
        "other-only": _onprem("other-only", tier="mid",
                              allowed_app_ids=["someone-else"])})
    policy = AppPolicyView()
    ctx = _ctx(reg, policy, tenant_id="someone-else")

    d = Router(S, registry=reg).select(ctx, tokens_after=20, complexity=0.1)
    # It survives eligibility; the later tier stage is what passes it over.
    assert _reasons(d)["other-only"] == "above target tier"
    assert "other-only" in _kept(d, reg, policy)
    assert {c.name for c in requestable_models(S, reg, "someone-else", policy)} \
        == {"local-8b", "other-only", *CLOUD}


# --- 5. a disabled on-prem row -----------------------------------------------

def test_a_disabled_onprem_row_never_becomes_a_candidate_at_all():
    """A disabled row leaves the effective pool upstream of eligibility, so it
    is not merely rejected -- it is never seen, by the router or by
    /v1/models."""
    reg = _registry({
        "local-8b": _onprem("local-8b", tier="cheap"),
        "off-mid": _onprem("off-mid", tier="mid", enabled=False)})
    policy = AppPolicyView()
    ctx = _ctx(reg, policy)

    assert "off-mid" not in {c.name for c in reg.effective_pool()}
    assert ctx.allowed_models == frozenset({"local-8b", *CLOUD})
    assert "off-mid" not in {c.name
                             for c in requestable_models(S, reg, "acme", policy)}

    d = Router(S, registry=reg).select(ctx, tokens_after=20, complexity=0.1)
    assert "off-mid" not in _reasons(d)


# --- 6. `exclude` after a prior failure --------------------------------------

def test_exclude_after_a_prior_failure_reads_prior_failure():
    reg = _registry()
    policy = AppPolicyView()
    ctx = _ctx(reg, policy)

    d = Router(S, registry=reg).select(ctx, tokens_after=20, complexity=0.1,
                                       exclude=frozenset({"local-8b"}))
    assert _reasons(d)["local-8b"] == "excluded · prior failure"
    assert d.chosen.name != "local-8b"
    assert _kept(d, reg, policy) == {"local-32b", *CLOUD}


def test_residency_outranks_exclude_so_a_cloud_model_still_reads_residency():
    """`exclude` only ever removes candidates, so it can never weaken
    residency -- and it must not relabel one either."""
    reg = _registry()
    ctx = _ctx(reg, AppPolicyView(residency="onprem_only"))

    d = Router(S, registry=reg).select(
        ctx, tokens_after=20, complexity=0.1,
        exclude=frozenset({"local-8b", "azure-gpt-mid"}))
    reasons = _reasons(d)
    assert reasons["azure-gpt-mid"] == "excluded · residency"
    assert reasons["local-8b"] == "excluded · prior failure"
    assert d.chosen.name == "local-32b"


def test_excluding_every_candidate_raises_no_eligible_candidate():
    reg = _registry()
    ctx = _ctx(reg, AppPolicyView(residency="onprem_only"))

    with pytest.raises(NoEligibleCandidate) as exc:
        Router(S, registry=reg).select(
            ctx, tokens_after=20, complexity=0.1,
            exclude=frozenset({"local-8b", "local-32b"}))
    assert "excluded · prior failure" in str(exc.value)
    assert "the model already failed on this request" in str(exc.value)


# --- the empty pool ----------------------------------------------------------

def test_an_empty_pool_says_so_rather_than_listing_rejections():
    prov = {p: c.model_copy(update={"enabled": False})
            for p, c in S.serving.providers.items()}
    settings = S.model_copy(
        update={"serving": S.serving.model_copy(update={"providers": prov})})

    async def _empty():
        db = Database(url="sqlite+aiosqlite://")
        await db.ensure_ready()
        reg = ModelRegistry(settings)
        await reg.refresh(db)
        return reg

    reg = _run(_empty())
    ctx = build_context(settings, "req-1", "acme", [], None,
                        app_policy=AppPolicyView(), registry=reg)

    assert reg.effective_pool() == []
    with pytest.raises(NoEligibleCandidate) as exc:
        Router(settings, registry=reg).select(ctx, tokens_after=20, complexity=0.1)
    assert "the model pool is empty" in str(exc.value)


# --- 7. circuit_open (step-7 breaker) -- stage 7, LAST -----------------------

def test_open_circuit_candidate_is_rejected_as_circuit_open():
    reg = _registry()
    policy = AppPolicyView()
    ctx = _ctx(reg, policy)

    result = feasible_set(S, reg, ctx, tenant_id="acme",
                          open_circuits=frozenset({"local-8b"}))
    assert "local-8b" not in {c.name for c in result.eligible}
    rejected = {r.name: r.code for r in result.rejected}
    assert rejected["local-8b"] == "circuit_open"


def test_placement_excluded_and_open_circuit_reads_residency_not_circuit_open():
    """A candidate that is BOTH placement-excluded (a cloud model under
    onprem_only) and named in `open_circuits` must read `residency` --
    policy reasons win, since circuit_open is the LAST stage (stage 7)."""
    reg = _registry()
    ctx = _ctx(reg, AppPolicyView(residency="onprem_only"))

    result = feasible_set(S, reg, ctx, tenant_id="acme",
                          open_circuits=frozenset({"azure-gpt-mid"}))
    rejected = {r.name: r.code for r in result.rejected}
    assert rejected["azure-gpt-mid"] == "residency"


def test_open_circuits_defaults_to_empty_set_no_change():
    reg = _registry()
    policy = AppPolicyView()
    ctx = _ctx(reg, policy)

    default = feasible_set(S, reg, ctx, tenant_id="acme")
    explicit = feasible_set(S, reg, ctx, tenant_id="acme", open_circuits=frozenset())
    assert default == explicit


# --- 8. B52: onprem_only holds at the seam itself ----------------------------
#
# The real gate (`build_context`) already narrows `allowed_providers` to
# {onprem} under onprem_only. These pin that `feasible_set` does not RELY on
# it: an inconsistent authority -- onprem_only set, a cloud provider still in
# `allowed_providers` -- must still never yield a cloud candidate (rule 3).

_B52_REG = _registry()
_ALL_PROVIDERS = frozenset({"onprem", "azure", "bedrock"})


def test_b52_inconsistent_context_never_reaches_cloud():
    ctx = _ctx(_B52_REG, AppPolicyView()).model_copy(
        update={"onprem_only": True, "allowed_providers": _ALL_PROVIDERS})

    result = feasible_set(S, _B52_REG, ctx, tenant_id="acme")

    assert result.eligible, "the on-prem candidates stay eligible"
    assert {c.provider for c in result.eligible} == {"onprem"}
    rejected = {r.name: r for r in result.rejected}
    for name in CLOUD:
        assert rejected[name].code == "residency"
        assert rejected[name].reason == "excluded · residency"


def test_b52_explicit_onprem_only_arg_narrows_explicit_providers():
    """The policy-side path: `build_context` hands over placement it resolved
    itself. `onprem_only=True` must bind even if the providers it passed alongside
    would not."""
    result = feasible_set(S, _B52_REG, AppPolicyView(), tenant_id="acme",
                          allowed_providers=_ALL_PROVIDERS, onprem_only=True)

    assert {c.provider for c in result.eligible} == {"onprem"}
    assert {r.name: r.code for r in result.rejected}["azure-gpt-mid"] == "residency"


from hypothesis import HealthCheck, given, settings as hyp_settings, strategies as st  # noqa: E402

_BASE_CTX = _ctx(_B52_REG, AppPolicyView())
_POOL_NAMES = sorted(c.name for c in _B52_REG.effective_pool())


@hyp_settings(max_examples=200, deadline=None,
              suppress_health_check=[HealthCheck.too_slow])
@given(onprem_only=st.booleans(),
       providers=st.frozensets(st.sampled_from(sorted(_ALL_PROVIDERS))),
       exclude=st.frozensets(st.sampled_from(_POOL_NAMES)),
       open_circuits=st.frozensets(st.sampled_from(_POOL_NAMES)),
       requested=st.none() | st.sampled_from(_POOL_NAMES),
       via_arg=st.booleans())
def test_b52_property_onprem_only_admits_only_onprem(onprem_only, providers, exclude,
                                                      open_circuits, requested, via_arg):
    """For ANY context (consistent or not) and any per-request exclusions: when
    the resolved onprem_only is true, every eligible candidate is on-prem, and
    every cloud candidate reads `residency`."""
    if via_arg:
        result = feasible_set(S, _B52_REG, AppPolicyView(), tenant_id="acme",
                              allowed_providers=providers, onprem_only=onprem_only,
                              exclude=exclude, open_circuits=open_circuits)
    else:
        ctx = _BASE_CTX.model_copy(update={
            "onprem_only": onprem_only, "allowed_providers": providers,
            "requested_model": requested})
        result = feasible_set(S, _B52_REG, ctx, tenant_id="acme",
                              exclude=exclude, open_circuits=open_circuits)

    if onprem_only:
        assert all(c.provider == "onprem" for c in result.eligible)
        for r in result.rejected:
            if r.name in CLOUD:
                assert r.code == "residency"
    else:
        # Off, placement is exactly `providers` -- nothing extra is narrowed.
        assert all(c.provider in providers for c in result.eligible)


def test_b52_pinned_cloud_model_under_inconsistent_onprem_only_reads_residency():
    """`pinned_model_reason` asks the same seam, so it must narrow the same
    way: a pinned cloud model under onprem_only is refused for residency, not
    explained as "disabled", even from a context that still lists cloud."""
    from app.policy.context import pinned_model_reason

    ctx = _ctx(_B52_REG, AppPolicyView()).model_copy(
        update={"onprem_only": True, "allowed_providers": _ALL_PROVIDERS,
                "requested_model": "azure-gpt-mid"})

    reason = pinned_model_reason(_B52_REG.effective_pool(), ctx, _B52_REG,
                                 "acme", "azure-gpt-mid")
    assert "excluded by residency" in reason
    assert reason != "it is disabled"
