"""B56: the first-fail `ctx.allowed_models` must not admit an out-of-scope model
when an endpoint declaration changes between the gate and the router.

`build_context` builds `ctx.allowed_models` from first-fail CODES: it subtracts
only `residency` / `app_scope`. A model outside the app's `allowed_models` that
stage 2 refused first (here: its region) therefore STAYS in that set. If the
operator then fixes that endpoint's declaration before the router runs
`feasible_set` on the same context, the old `_ctx_scope` (which read only
`ctx.allowed_models`) admitted it -- the app's own scope was bypassed.

The fix makes `_ctx_scope` also require the app's EXACT scope
(`Scope.from_context(ctx)`), which can only narrow and is a no-op without the
race. Real registry, real `build_context`, real `Router.select`.
"""
from __future__ import annotations

import asyncio

from app.apps import store
from app.apps.db import Database
from app.apps.policy import AppPolicyView
from app.apps.registry import ModelRegistry
from app.policy.context import build_context
from app.policy.feasible import APP_SCOPE, Scope, feasible_set, model_in_scope
from app.routing.router import Router
from tests.test_endpoint_data_handling import S, _onprem


def _run(coro):
    return asyncio.run(coro)


async def _world():
    db = Database(url="sqlite+aiosqlite://")
    await db.ensure_ready()
    async with db.session() as s:
        # A is in the app's scope; B is NOT, and is the cheaper one, so the
        # router would pick it if it were admitted.
        await store.upsert_onboarded(s, "a-8b", **_onprem("a-8b", region="eu", price=1.0))
        await store.upsert_onboarded(s, "b-8b", **_onprem("b-8b", region="us", price=0.01))
        await s.commit()
    reg = ModelRegistry(S)
    await reg.refresh(db)
    return db, reg


async def _redeclare_b_eu(db, reg):
    async with db.session() as s:
        await store.upsert_onboarded(s, "b-8b", **_onprem("b-8b", region="eu", price=0.01))
        await s.commit()
    await reg.refresh(db)


def test_declaration_change_between_gate_and_router_does_not_admit_out_of_scope_model():
    db, reg = _run(_world())
    policy = AppPolicyView(allowed_models=frozenset({"a-8b"}),
                           allowed_regions=frozenset({"eu"}))
    ctx = build_context(S, "req-1", "acme", [], None,
                        app_policy=policy, registry=reg)
    # The precondition of the race: B was refused first by REGION, so the
    # first-fail set still carries it although the app's scope excludes it.
    assert "b-8b" in ctx.allowed_models
    assert not model_in_scope(
        next(c for c in reg.effective_pool() if c.name == "b-8b"),
        Scope.from_context(ctx))

    # The operator fixes B's declaration after the gate, before the router.
    _run(_redeclare_b_eu(db, reg))

    # Pre-fix, `_ctx_scope` read only `ctx.allowed_models` (which contains B),
    # stage 2 now passes, so B was ELIGIBLE here and -- being cheaper -- chosen.
    fs = feasible_set(S, reg, ctx, tenant_id="acme")
    assert "b-8b" not in {c.name for c in fs.eligible}
    rej = {r.name: r for r in fs.rejected}
    assert rej["b-8b"].code == APP_SCOPE
    assert rej["b-8b"].reason == "not permitted for application"

    d = Router(S, registry=reg).select(ctx, tokens_after=20, complexity=0.1)
    assert d.chosen.name == "a-8b"
    assert {r.name: r.reason for r in d.rejected}["b-8b"] == "not permitted for application"


def test_no_race_is_a_no_op():
    """Without a declaration change the result is exactly what it was: B is
    region-refused first (its first-fail code is unchanged)."""
    db, reg = _run(_world())
    policy = AppPolicyView(allowed_models=frozenset({"a-8b"}),
                           allowed_regions=frozenset({"eu"}))
    ctx = build_context(S, "req-1", "acme", [], None,
                        app_policy=policy, registry=reg)
    fs = feasible_set(S, reg, ctx, tenant_id="acme")
    assert {c.name for c in fs.eligible} == {"a-8b"}
    assert {r.name: r.code for r in fs.rejected}["b-8b"] == "region_not_allowed"


def test_context_without_app_lists_is_unaffected():
    """Internal / hand-built contexts carry app_allowed_* = None: no constraint."""
    db, reg = _run(_world())
    ctx = build_context(S, "req-1", "acme", [], None,
                        app_policy=None, registry=reg)
    assert ctx.app_allowed_models is None and ctx.app_allowed_tiers is None
    fs = feasible_set(S, reg, ctx, tenant_id="acme")
    assert {"a-8b", "b-8b"} <= {c.name for c in fs.eligible}
