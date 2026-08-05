"""Per-application policy narrowing + quota enforcement (Phase 2)."""
import asyncio
import os

from fastapi.testclient import TestClient

from app.apps.bootstrap import seed_demo_apps
from app.apps.db import Database
from app.apps.models import AppPolicy
from app.apps.policy import AppPolicyView, RateLimiter
from app.config.loader import load_settings
from app.main import create_app
from app.policy.context import build_context
from app.routing.router import Router
from tests._litellm_fake import make_test_registry
import pytest

_CLEAN = {"messages": [{"role": "user", "content": "capital of France?"}]}


# --- build_context narrowing (unit, no DB) ---------------------------------

def _ctx(app_policy):
    settings = load_settings()
    return build_context(settings, "req-x", "acme", [], None,
                         app_policy=app_policy, registry=make_test_registry(settings))


def test_app_policy_restricts_providers():
    # Public request, but the app may only use on-prem -> cloud is excluded
    # even though residency would otherwise allow it.
    ctx = _ctx(AppPolicyView(residency="any",
                             allowed_providers=frozenset({"onprem"})))
    assert set(ctx.allowed_providers) == {"onprem"}
    assert all(m.startswith("local-") for m in ctx.allowed_models)


def test_app_policy_restricts_tiers():
    ctx = _ctx(AppPolicyView(residency="any",
                             allowed_tiers=frozenset({"cheap"})))
    # only the cheap-tier model survives
    assert ctx.allowed_models == frozenset({"local-8b"})


def test_app_policy_model_scope_is_honored_by_router():
    settings = load_settings()
    registry = make_test_registry(settings)
    ctx = _ctx(AppPolicyView(residency="any",
                             allowed_models=frozenset({"local-32b"})))
    ctx = ctx.model_copy(update={"requested_model": "local-32b"})
    decision = Router(settings, registry=registry).select(ctx, 10, 0.1)
    assert decision.chosen.name == "local-32b"


def test_explicit_model_pin_is_not_ignored():
    settings = load_settings()
    registry = make_test_registry(settings)
    ctx = _ctx(AppPolicyView(residency="any"))
    ctx = ctx.model_copy(update={"requested_model": "local-32b"})
    decision = Router(settings, registry=registry).select(ctx, 10, 0.1)
    assert decision.chosen.name == "local-32b"


def test_app_policy_residency_is_stricter():
    ctx = _ctx(AppPolicyView(residency="onprem_only"))
    assert ctx.onprem_only is True
    assert set(ctx.allowed_providers) == {"onprem"}


def test_no_app_policy_keeps_default_behavior():
    ctx = _ctx(None)  # falls back to policy.yaml tenant (acme = private_cloud_ok)
    assert ctx.onprem_only is False


# --- rate limiter (unit) ----------------------------------------------------

def test_rate_limiter_allows_up_to_limit_then_denies():
    rl = RateLimiter(window_s=60.0)
    assert rl.allow("a", 2) is True
    assert rl.allow("a", 2) is True
    assert rl.allow("a", 2) is False        # third within the window
    assert rl.allow("b", 2) is True         # a different app is independent
    assert rl.allow("a", None) is True      # None = unlimited


# --- HTTP enforcement (integration, file DB so we can pre-insert a policy) ---

def _seed_policy(db_url, app_id, **policy_kwargs):
    async def go():
        db = Database(url=db_url, seed=seed_demo_apps)
        async with db.session() as s:        # ensure_ready seeds the demo apps
            s.add(AppPolicy(application_id=app_id, **policy_kwargs))
            await s.commit()
        await db.dispose()
    asyncio.run(go())


def _with_db(tmp_path):
    """Return (db_url, restore_fn) and point the app at a temp file DB."""
    db_url = f"sqlite+aiosqlite:///{tmp_path}/rf.db"
    prev = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = db_url

    def restore():
        if prev is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = prev

    return db_url, restore


def test_http_rate_limit_returns_429(tmp_path):
    db_url, restore = _with_db(tmp_path)
    try:
        _seed_policy(db_url, "acme", max_rpm=2)
        with TestClient(create_app()) as c:
            h = {"Authorization": "Bearer acme-key"}
            assert c.post("/v1/chat/completions", headers=h, json=_CLEAN).status_code == 200
            assert c.post("/v1/chat/completions", headers=h, json=_CLEAN).status_code == 200
            assert c.post("/v1/chat/completions", headers=h, json=_CLEAN).status_code == 429
    finally:
        restore()


def test_http_provider_restriction_reflected_in_policy(tmp_path):
    db_url, restore = _with_db(tmp_path)
    try:
        _seed_policy(db_url, "acme", allowed_providers=["onprem"])
        with TestClient(create_app()) as c:
            r = c.post("/v1/chat/completions",
                       headers={"Authorization": "Bearer acme-key"}, json=_CLEAN)
            assert r.status_code == 200
            assert r.json()["policy"]["allowed_providers"] == ["onprem"]
    finally:
        restore()


def _compression_record(trace):
    # CompressionRecord is the only trace entry carrying tokens_before.
    return next(e for e in trace if "tokens_before" in e)


def test_http_per_app_compression_override(tmp_path):
    # Global Headroom default is OFF (serving.yaml). Forcing it ON for acme must
    # flow DB -> policy_view -> context -> compress: the request's compression
    # record then bypasses on "below_min_tokens" (short prompt) instead of
    # "disabled" — observable proof the per-app column is wired end-to-end.
    db_url, restore = _with_db(tmp_path)
    try:
        _seed_policy(db_url, "acme", compression_enabled=True)
        with TestClient(create_app()) as c:
            r = c.post("/v1/chat/completions",
                       headers={"Authorization": "Bearer acme-key"}, json=_CLEAN)
            assert r.status_code == 200
            rec = _compression_record(r.json()["trace"])
            assert rec["enabled"] is True
            assert rec["bypass_reason"] == "below_min_tokens"
    finally:
        restore()
