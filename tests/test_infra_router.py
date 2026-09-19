"""Router behaviour over the ModelRegistry (replaces the old InfraRoster path).

The registry merges the static YAML pool (cloud built-ins only — there is no
built-in on-prem entry any more) with enabled onboarded on-prem rows.
Disabling an on-prem model now means it is not onboarded/enabled, so it never
enters `effective_pool()` — there is no separate admin disable roster any
more. App-scoping is enforced in the router alongside residency. (The full
"all on-prem gone -> onprem_only rejects" behaviour is tested at both the
build_context level and the Router level in test_derived_onprem_reject.py.)"""
import asyncio

from app.config.loader import load_settings
from app.apps.db import Database
from app.apps import store
from app.apps.registry import ModelRegistry
from app.models import ExecutionPolicyContext
from app.routing.router import Router

S = load_settings()


def _run(c):
    return asyncio.run(c)


async def _reg_with(rows):
    db = Database(url="sqlite+aiosqlite://")
    await db.ensure_ready()
    async with db.session() as s:
        for slug, f in rows.items():
            await store.upsert_onboarded(s, slug, **f)
        await s.commit()
    reg = ModelRegistry(S)
    await reg.refresh(db)
    return reg


def _eligible(pool, providers):
    """The eligible set an app that narrows nothing gets: every candidate in
    the pool whose provider is permitted. Exactly what `build_context` computes
    (placement AND app scope, never the per-model registry gates -- those stay
    the router's to report, which is what these tests assert). Stated because
    `ctx.allowed_models` is binding on the router, with no flag that can turn
    it off (app/policy/feasible.py)."""
    return frozenset(c.name for c in pool if c.provider in providers)


def _ctx(pool, providers, *, tenant_id="t", risk="low", onprem=False):
    return ExecutionPolicyContext(
        request_id="r", tenant_id=tenant_id, sensitivity="public",
        onprem_only=onprem, allowed_providers=frozenset(providers),
        allowed_models=_eligible(pool, providers),
        max_cost_usd=0.05, max_latency_ms=15000,
        risk_class=risk, require_verification=False, detected_entities=(),
        redaction_map_id=None)


def _onprem(slug, *, tier="cheap", enabled=True, allowed_app_ids=None):
    return dict(display_name=slug, provider="onprem", tier=tier,
                price_input=0.1, price_output=0.1, endpoint=f"http://{slug}/v1",
                enabled=enabled, allowed_app_ids=allowed_app_ids)


def test_onprem_only_selects_onprem_candidate():
    # Residency filter: an onprem_only request only ever picks an on-prem model.
    reg = _run(_reg_with({"nem": _onprem("nem", tier="mid")}))
    d = Router(S, registry=reg).select(
        _ctx(reg.effective_pool(), {"onprem"}, onprem=True), tokens_after=20, complexity=0.9)
    assert d.chosen.provider == "onprem"


def test_disabled_onboarded_model_not_a_candidate():
    # A disabled onboarded row never enters the effective pool -> never chosen,
    # and never even appears as a rejected candidate. No built-in on-prem YAML
    # pool member any more, so seed a second (enabled) on-prem row too --
    # otherwise the residency filter would legitimately leave zero candidates
    # for an onprem_only ctx, which is a different failure mode entirely
    # (see test_derived_onprem_reject.py's Router-level case for that one).
    reg = _run(_reg_with({
        "off-cheap": _onprem("off-cheap", enabled=False),
        "on-cheap": _onprem("on-cheap")}))
    d = Router(S, registry=reg).select(
        _ctx(reg.effective_pool(), {"onprem"}, onprem=True), tokens_after=20, complexity=0.1)
    assert d.chosen.name != "off-cheap"
    assert all(rc.name != "off-cheap" for rc in d.rejected)


def test_app_scoped_model_rejected_for_other_app():
    # allowed_app_ids scoping: a model bound to "acme" is dropped (with the
    # app-scope reason) for a different tenant, alongside residency. A second
    # unscoped on-prem row keeps the residency filter from legitimately
    # emptying the candidate pool (see comment above).
    reg = _run(_reg_with({
        "acme-only": _onprem("acme-only", allowed_app_ids=["acme"]),
        "open-cheap": _onprem("open-cheap")}))
    d = Router(S, registry=reg).select(
        _ctx(reg.effective_pool(), {"onprem"}, tenant_id="hospital", onprem=True),
        tokens_after=20, complexity=0.1)
    assert d.chosen.name != "acme-only"
    assert any(rc.name == "acme-only" and "not permitted" in rc.reason
               for rc in d.rejected)


def test_app_scoped_model_permitted_for_its_own_app():
    reg = _run(_reg_with({
        "acme-only": _onprem("acme-only", allowed_app_ids=["acme"])}))
    d = Router(S, registry=reg).select(
        _ctx(reg.effective_pool(), {"onprem"}, tenant_id="acme", onprem=True),
        tokens_after=20, complexity=0.1)
    assert not any("not permitted" in rc.reason for rc in d.rejected)


def test_empty_registry_matches_no_registry():
    # Regression no-op: with no onboarded rows, routing over the registry must
    # match routing straight off the YAML pool -- for the *enabled-provider*
    # portion of the pool. The no-registry path (`Router(S)`, used only when no
    # ModelRegistry is injected at all) reads settings.routing.model_pool raw
    # and is not subject to the B10 provider-enabled filter, which lives in
    # ModelRegistry.effective_pool(); with the default config (azure/bedrock
    # disabled) that raw pool still contains them, so this test enables both
    # here to keep comparing like with like instead of exercising the filter.
    s = S.model_copy(update={"serving": S.serving.model_copy(update={"providers": {
        **S.serving.providers,
        "azure": S.serving.providers["azure"].model_copy(update={"enabled": True}),
        "bedrock": S.serving.providers["bedrock"].model_copy(update={"enabled": True}),
    }})})
    # No onboarded rows either way, so a cold ModelRegistry(s) (no DB refresh
    # needed) is equivalent to _reg_with({}) here and avoids rebuilding it
    # against the wrong settings object.
    reg = ModelRegistry(s)
    every = {"onprem", "azure", "bedrock"}
    with_reg = Router(s, registry=reg).select(
        _ctx(reg.effective_pool(), every), tokens_after=20, complexity=0.1)
    without = Router(s).select(
        _ctx(s.routing.model_pool, every), tokens_after=20, complexity=0.1)
    assert with_reg.chosen.name == without.chosen.name
