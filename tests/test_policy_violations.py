"""P3b Task 1: candidate_violations agrees with the first-fail seam (spec §5.4).

`candidate_rejection` answers "the FIRST stage that refused this candidate";
`candidate_violations` answers "EVERY requirement this candidate violates".
Both call the same per-stage predicates in `app/policy/feasible.py`, and the
Hypothesis property at the bottom is the proof that they cannot drift: for
every candidate with no STATE (disabled / circuit_open / prior_failure),
`candidate_rejection` is `None` exactly when `candidate_violations` is empty,
and when it is not `None` its code maps to one of the violated ids.

Fixtures are the repo's own: the registry/context builders of
`tests/test_feasible_set.py` (both cloud providers enabled) and the
declaration-aware row builder of `tests/test_endpoint_data_handling.py`.
"""
from __future__ import annotations

from hypothesis import HealthCheck, given, settings as hsettings, strategies as st

from app.apps.policy import AppPolicyView
from app.models import Entity
from app.policy.feasible import (
    DataHandling,
    Scope,
    _resolve_placement,
    _ctx_scope,
    _policy_scope,
    candidate_rejection,
    candidate_state,
    candidate_violations,
    feasible_set,
    violation_matrix,
)
from tests.test_endpoint_data_handling import S as _DH_S
from tests.test_endpoint_data_handling import _ctx as _dh_ctx
from tests.test_endpoint_data_handling import _onprem as _declared_onprem
from tests.test_endpoint_data_handling import _registry as _dh_registry
from tests.test_feasible_set import CLOUD, S, _ctx, _registry, _run

CODE_TO_IDS = {
    "residency": {"residency", "allowed_providers"},
    "region_not_allowed": {"allowed_regions"},
    "prompt_retention_not_allowed": {"require_no_retention"},
    "raw_sensitive_not_accepted": {"raw_sensitive"},
    "app_scope": {"allowed_tiers", "allowed_models"},
    "not_requested": {"requested_model"},
    "model_scope": {"endpoint_app_allowlist"},   # "disabled" model_scope is STATE, see below
}

# The only ids `candidate_violations` may ever return (spec §2.1 minus the two
# budget ids, which the router -- not this seam -- decides).
REQUIREMENT_IDS = {"residency", "allowed_providers", "allowed_regions",
                   "require_no_retention", "raw_sensitive", "allowed_tiers",
                   "allowed_models", "endpoint_app_allowlist", "requested_model"}
STATE_IDS = {"no_endpoints", "disabled", "circuit_open", "prior_failure"}

_SSN = [Entity(type="US_SSN", start=0, end=11, value="123-45-6789")]


def _violations(c, ctx, registry, *, tenant_id="acme"):
    providers, onprem = _resolve_placement(S, registry, ctx)
    return candidate_violations(
        c, allowed_providers=providers, onprem_only=onprem,
        scope=Scope.from_context(ctx), requested_model=ctx.requested_model,
        registry=registry, tenant_id=tenant_id,
        data_handling=DataHandling.from_context(ctx))


def _rejection(c, ctx, registry, *, tenant_id="acme"):
    providers, onprem = _resolve_placement(S, registry, ctx)
    return candidate_rejection(
        c, allowed_providers=providers, onprem_only=onprem,
        in_scope=_ctx_scope(ctx), requested_model=ctx.requested_model,
        exclude=frozenset(), registry=registry, tenant_id=tenant_id,
        data_handling=DataHandling.from_context(ctx))


def _pool(registry):
    return {c.name: c for c in registry.effective_pool()}


# --- stage 1: residency vs allowed_providers ---------------------------------

def test_residency_vs_allowed_providers_split():
    """onprem_only + cloud candidate -> 'residency'; non-onprem_only residency with the
    provider outside allowed_providers -> 'allowed_providers'."""
    reg = _registry()
    pool = _pool(reg)

    onprem_ctx = _ctx(reg, AppPolicyView(residency="onprem_only"))
    for name in CLOUD:
        assert _violations(pool[name], onprem_ctx, reg) == {"residency"}
        assert _rejection(pool[name], onprem_ctx, reg).code == "residency"
    assert _violations(pool["local-8b"], onprem_ctx, reg) == frozenset()

    narrow_ctx = _ctx(reg, AppPolicyView(residency="any",
                                         allowed_providers=frozenset({"onprem", "azure"})))
    assert _violations(pool["bedrock-frontier"], narrow_ctx, reg) == {"allowed_providers"}
    # The first-fail seam still reports its one stage-1 code for both.
    assert _rejection(pool["bedrock-frontier"], narrow_ctx, reg).code == "residency"
    assert _violations(pool["azure-gpt-mid"], narrow_ctx, reg) == frozenset()


def test_context_scope_is_exact_for_a_placement_refused_candidate():
    """`ctx.allowed_models` already dropped every placement-refused name (it is
    built from first-fail codes), so it cannot say whether app scope ALSO
    refuses one. `Scope.from_context` reads the app's own lists instead: a
    cloud candidate outside `allowed_models` under onprem_only reports both
    `residency` and `allowed_models` -- exactly what the policy-side scope
    reports -- and one inside it reports `residency` alone."""
    reg = _registry()
    pool = _pool(reg)
    policy = AppPolicyView(residency="onprem_only",
                           allowed_models=frozenset({"local-8b", "azure-gpt-mid"}))
    ctx = _ctx(reg, policy)
    assert "azure-gpt-mid" not in ctx.allowed_models   # the first-fail set

    assert _violations(pool["bedrock-frontier"], ctx, reg) == {
        "residency", "allowed_models"}
    assert _violations(pool["azure-gpt-mid"], ctx, reg) == {"residency"}
    assert _violations(pool["local-32b"], ctx, reg) == {"allowed_models"}

    providers, onprem = _resolve_placement(S, reg, policy)
    from_policy = candidate_violations(
        pool["bedrock-frontier"], allowed_providers=providers, onprem_only=onprem,
        scope=Scope.from_policy(policy), requested_model=None, registry=reg,
        tenant_id="acme", data_handling=DataHandling.from_policy(S, policy))
    assert from_policy == {"residency", "allowed_models"}


def test_stage2_refused_candidate_still_reports_allowed_models():
    """Review regression (P3b Task 1, fix round 1). onprem_only,
    allowed_regions={eu}, allowed_models={a model that is neither}; A declares
    us, B declares eu. A is refused first by region, so it STAYS in the
    first-fail `ctx.allowed_models` -- reading scope off that set reported
    A={allowed_regions}, B={allowed_models}, whose minimum core is
    {allowed_regions, allowed_models}. The true minimum is {allowed_models}:
    app scope refuses both."""
    reg = _dh_registry({
        "a-8b": _declared_onprem("a-8b", region="us"),
        "b-8b": _declared_onprem("b-8b", region="eu"),
    })
    policy = AppPolicyView(residency="onprem_only",
                           allowed_regions=frozenset({"eu"}),
                           allowed_models=frozenset({"x-model"}))
    ctx = _dh_ctx(reg, policy)
    assert "a-8b" in ctx.allowed_models    # first-fail authority, unchanged

    m = violation_matrix(_DH_S, reg, ctx, tenant_id="acme")
    assert m.state == {}
    assert m.available == {"a-8b": {"allowed_regions", "allowed_models"},
                           "b-8b": {"allowed_models"}}
    # Every available candidate is hit by {allowed_models} alone.
    assert all("allowed_models" in v for v in m.available.values())
    # And the first-fail seam is untouched: A still reads region first.
    fs = feasible_set(_DH_S, reg, ctx, tenant_id="acme")
    assert {r.name: r.code for r in fs.rejected} == {
        "a-8b": "region_not_allowed", "b-8b": "app_scope"}


# --- stage 2: every data-handling rule, together -----------------------------

def test_all_three_data_handling_rules_reported_together():
    """An endpoint failing region AND retention AND raw-sensitive reports all three ids,
    while candidate_rejection still reports only the first code (region_not_allowed)."""
    reg = _registry({
        "bad": _declared_onprem("bad", region="us", prompt_retention="retained",
                                accepts_raw_sensitive=False),
        "good": _declared_onprem("good", region="eu", prompt_retention="none",
                                 accepts_raw_sensitive=True),
    })
    pool = _pool(reg)
    ctx = _ctx(reg, AppPolicyView(allowed_regions=frozenset({"eu"}),
                                  require_no_retention=True, model_input="raw"))
    # The context must actually carry all three requirements.
    ctx = ctx.model_copy(update={"sends_raw_sensitive": True})

    assert _violations(pool["bad"], ctx, reg) == {
        "allowed_regions", "require_no_retention", "raw_sensitive"}
    r = _rejection(pool["bad"], ctx, reg)
    assert r.code == "region_not_allowed"
    assert r.reason.startswith("excluded · region")
    assert _violations(pool["good"], ctx, reg) == frozenset()
    assert _rejection(pool["good"], ctx, reg) is None


# --- state is not a violation ------------------------------------------------

def test_state_is_not_a_violation():
    """A disabled onprem endpoint, an open circuit, a prior failure: candidate_violations
    ignores them; violation_matrix lists them under state and not in available."""
    reg = _registry({
        "local-8b": _declared_onprem("local-8b", tier="cheap"),
        "local-32b": _declared_onprem("local-32b", tier="mid"),
        "local-70b": _declared_onprem("local-70b", tier="frontier"),
    })
    ctx = _ctx(reg, AppPolicyView(residency="onprem_only"))
    # Disabled between the pool listing and the eligibility check: the one way a
    # disabled row is still in the pool the seam iterates (effective_pool drops
    # rows that were already disabled when it was read).
    reg.is_enabled = lambda slug: slug != "local-8b"
    pool = _pool(reg)

    for name in ("local-8b", "local-32b", "local-70b"):
        assert _violations(pool[name], ctx, reg) == frozenset()

    exclude = frozenset({"local-70b"})
    circuits = frozenset({"local-32b"})
    assert candidate_state(pool["local-8b"], exclude=exclude, registry=reg,
                           open_circuits=circuits) == "disabled"
    assert candidate_state(pool["local-32b"], exclude=exclude, registry=reg,
                           open_circuits=circuits) == "circuit_open"
    assert candidate_state(pool["local-70b"], exclude=exclude, registry=reg,
                           open_circuits=circuits) == "prior_failure"
    assert candidate_state(pool["azure-gpt-mid"], exclude=exclude, registry=reg,
                           open_circuits=circuits) is None

    m = violation_matrix(S, reg, ctx, tenant_id="acme", exclude=exclude,
                         open_circuits=circuits)
    assert m.state == {"disabled": ("local-8b",), "circuit_open": ("local-32b",),
                       "prior_failure": ("local-70b",)}
    assert set(m.available) == CLOUD
    assert all(v == {"residency"} for v in m.available.values())
    assert set(m.candidates) == set(pool)
    assert m.onprem_only is True
    assert m.allowed_providers == frozenset({"onprem"})


def test_empty_pool_state():
    """Empty effective pool -> state == {'no_endpoints': ()} and available == {}."""
    prov = {p: c.model_copy(update={"enabled": False})
            for p, c in S.serving.providers.items()}
    settings = S.model_copy(
        update={"serving": S.serving.model_copy(update={"providers": prov})})
    # Same construction as test_feasible_set's empty-pool test.
    from app.apps.db import Database
    from app.apps.registry import ModelRegistry
    from app.policy.context import build_context

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

    m = violation_matrix(settings, reg, ctx, tenant_id="acme")
    assert m.state == {"no_endpoints": ()}
    assert m.available == {}
    assert m.candidates == {}


# --- the agreement property --------------------------------------------------
#
# Declarations vary per example on EVERY candidate, cloud included, and a
# fresh registry is built for each example from them -- so the context
# (`build_context`, which precomputes the first-fail `ctx.allowed_models` from
# the pool's declarations) and the candidates it is evaluated against always
# agree, exactly as they do on the request path.

_ONPREM_ROWS = {"local-8b": "cheap", "local-32b": "mid"}   # local-32b is allowlisted to acme
_CLOUD_NAMES = sorted(c.name for c in S.routing.model_pool)
_H_NAMES = sorted([*_ONPREM_ROWS, *_CLOUD_NAMES])
_TIERS = ["cheap", "mid", "frontier"]
_PROVIDERS = ["onprem", "azure", "bedrock"]

_opt_subset = lambda xs: st.none() | st.frozensets(st.sampled_from(xs))  # noqa: E731
_region = st.sampled_from([None, "us", "eu", "unknown"])
_retention = st.sampled_from([None, "none", "retained", "unknown"])
# An onboarded row stores accepts_raw_sensitive as a bool; a YAML pool entry
# as a token. Both include "declared nothing".
_onprem_decl = st.fixed_dictionaries({
    "region": _region, "prompt_retention": _retention,
    "accepts_raw_sensitive": st.sampled_from([None, True, False])})
_cloud_decl = st.fixed_dictionaries({
    "region": _region, "prompt_retention": _retention,
    "accepts_raw_sensitive": st.sampled_from([None, "true", "false", "unknown"])})


def _world(onprem_decls, cloud_decls):
    """Settings + a real registry whose effective pool carries exactly these
    declarations, built with the repo's own row builder and registry builder."""
    pool = [c.model_copy(update=cloud_decls[c.name]) for c in S.routing.model_pool]
    settings = S.model_copy(update={
        "routing": S.routing.model_copy(update={"model_pool": pool})})
    rows = {}
    for slug, tier in _ONPREM_ROWS.items():
        rows[slug] = _declared_onprem(slug, tier=tier, **onprem_decls[slug])
    rows["local-32b"]["allowed_app_ids"] = ["acme"]
    return settings, _dh_registry(rows, settings)


@hsettings(max_examples=200, deadline=None,
           suppress_health_check=[HealthCheck.too_slow])
@given(residency=st.sampled_from(["any", "private_cloud_ok", "onprem_only"]),
       allowed_providers=_opt_subset(_PROVIDERS),
       allowed_tiers=_opt_subset(_TIERS),
       allowed_models=_opt_subset(_H_NAMES),
       allowed_regions=_opt_subset(["us", "eu"]),
       require_no_retention=st.sampled_from([None, False, True]),
       model_input=st.sampled_from([None, "raw", "redact"]),
       sensitive=st.booleans(),
       pin=st.none() | st.sampled_from(_H_NAMES),
       tenant_id=st.sampled_from(["acme", "other"]),
       onprem_decls=st.fixed_dictionaries({n: _onprem_decl for n in _ONPREM_ROWS}),
       cloud_decls=st.fixed_dictionaries({n: _cloud_decl for n in _CLOUD_NAMES}),
       exclude=st.frozensets(st.sampled_from(_H_NAMES)),
       open_circuits=st.frozensets(st.sampled_from(_H_NAMES)),
       via_policy=st.booleans())
def test_agreement_with_candidate_rejection(residency, allowed_providers, allowed_tiers,
                                            allowed_models, allowed_regions,
                                            require_no_retention, model_input, sensitive,
                                            pin, tenant_id, onprem_decls, cloud_decls,
                                            exclude, open_circuits, via_policy):
    """For every candidate with no state: candidate_rejection is None  <=>  violations empty;
    when not None, CODE_TO_IDS[code] intersects violations. And the context-side
    violation matrix is EXACT: it equals the violation sets computed with the
    policy view's own scope."""
    settings, reg = _world(onprem_decls, cloud_decls)
    policy = AppPolicyView(residency=residency, allowed_providers=allowed_providers,
                           allowed_tiers=allowed_tiers, allowed_models=allowed_models,
                           allowed_regions=allowed_regions,
                           require_no_retention=require_no_retention,
                           model_input=model_input)
    from app.policy.context import build_context
    # The raw-sensitive rule needs a classified-sensitive request.
    ctx = build_context(settings, "req-1", tenant_id, list(_SSN) if sensitive else [],
                        None, app_policy=policy, registry=reg, model_input=model_input)
    if pin is not None:
        ctx = ctx.model_copy(update={"requested_model": pin})

    if via_policy:
        # The policy-side authority: no pin, no request-shaped raw-sensitive.
        providers, onprem = _resolve_placement(settings, reg, policy)
        scope, in_scope = Scope.from_policy(policy), _policy_scope(policy)
        dh, requested = DataHandling.from_policy(settings, policy), None
    else:
        providers, onprem = _resolve_placement(settings, reg, ctx)
        scope, in_scope = Scope.from_context(ctx), _ctx_scope(ctx)
        dh, requested = DataHandling.from_context(ctx), ctx.requested_model

    pool = reg.effective_pool()
    assert sorted(c.name for c in pool) == _H_NAMES
    for c in pool:
        if candidate_state(c, exclude=exclude, registry=reg,
                           open_circuits=open_circuits) is not None:
            continue
        v = candidate_violations(
            c, allowed_providers=providers, onprem_only=onprem, scope=scope,
            requested_model=requested, registry=reg, tenant_id=tenant_id,
            data_handling=dh)
        r = candidate_rejection(
            c, allowed_providers=providers, onprem_only=onprem, in_scope=in_scope,
            requested_model=requested, exclude=exclude, registry=reg,
            tenant_id=tenant_id, data_handling=dh, open_circuits=open_circuits)
        assert v <= REQUIREMENT_IDS
        assert not (v & STATE_IDS)
        assert (r is None) == (not v), (c.name, r, v)
        if r is not None:
            assert CODE_TO_IDS[r.code] & v, (c.name, r.code, v)
        # Rule 3 by construction: under onprem_only a cloud candidate always
        # carries `residency`.
        if onprem and c.provider != "onprem":
            assert "residency" in v

    if via_policy:
        return
    # The matrix over the registry's own pool agrees with the live seam.
    m = violation_matrix(settings, reg, ctx, tenant_id=tenant_id, exclude=exclude,
                         open_circuits=open_circuits)
    fs = feasible_set(settings, reg, ctx, tenant_id=tenant_id, exclude=exclude,
                      open_circuits=open_circuits)
    assert {n for n, v in m.available.items() if not v} == {c.name for c in fs.eligible}
    in_state = {n for names in m.state.values() for n in names}
    assert in_state.isdisjoint(m.available)
    assert in_state | set(m.available) == set(m.candidates) == set(_H_NAMES)
    assert (m.allowed_providers, m.onprem_only) == (providers, onprem)
    # EXACT: for every available candidate the context-side set equals the one
    # computed with the policy view's own scope (same placement and data
    # handling as the context -- its residency may have been escalated by a
    # detected entity, which the stored policy does not show).
    exact = Scope.from_policy(policy)
    for name, v in m.available.items():
        assert v == candidate_violations(
            m.candidates[name], allowed_providers=providers, onprem_only=onprem,
            scope=exact, requested_model=ctx.requested_model, registry=reg,
            tenant_id=tenant_id, data_handling=DataHandling.from_context(ctx)), name
