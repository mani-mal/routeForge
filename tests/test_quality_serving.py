"""P2b-3 Task 2: `model_quality` rows reach the router, per workload.

One shared `QualityResolver` (`app.state.quality`) is loaded at boot and
refreshed on a timer (`app.main.refresh_quality`). Quality only REORDERS the
eligible set: with no rows every decision and scoring record is unchanged,
and eligibility (residency above all) never reads the workload.
"""
from __future__ import annotations

from contextlib import contextmanager

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.apps import store
from app.apps.models import RoutingOutcome
from app.config.loader import load_settings
from app.main import create_app, refresh_quality
from app.models import ExecutionPolicyContext
from app.routing.quality import QualityResolver, QualityScore
from app.routing.router import Router
from tests._litellm_fake import make_test_registry
from tests.onboarding.harness import add_model, disable_seeded_models

DEMO = {"Authorization": "Bearer demo-key"}
HELLO = {"messages": [{"role": "user", "content": "hello"}]}

S = load_settings()
_prov = dict(S.serving.providers)
_prov["azure"] = _prov["azure"].model_copy(update={"enabled": True})
_prov["bedrock"] = _prov["bedrock"].model_copy(update={"enabled": True})
S_CLOUD = S.model_copy(update={"serving": S.serving.model_copy(update={"providers": _prov})})


def _ctx(reg, providers, *, onprem=False, workload=None):
    providers = frozenset(providers)
    return ExecutionPolicyContext(
        request_id="r", tenant_id="t", sensitivity="public", onprem_only=onprem,
        allowed_providers=providers,
        allowed_models=frozenset(c.name for c in reg.effective_pool()
                                 if c.provider in providers),
        max_cost_usd=0.05, max_latency_ms=15000, risk_class="low",
        require_verification=False, detected_entities=(), redaction_map_id=None,
        workload=workload)


@contextmanager
def _client():
    with TestClient(create_app(), raise_server_exceptions=False) as c:
        yield c


def _outcomes(c: TestClient) -> list[RoutingOutcome]:
    async def go():
        async with c.app.state.db.session() as session:
            return list((await session.execute(
                select(RoutingOutcome))).scalars())
    return c.portal.call(go)


def _write_set(c: TestClient, set_id: str, units: list[dict]) -> None:
    async def go():
        async with c.app.state.db.session() as session:
            await store.write_calibration_set(session, set_id=set_id, units=units)
            await session.commit()
    c.portal.call(go)


def _refresh(c: TestClient) -> bool:
    async def go():
        return await refresh_quality(c.app)
    return c.portal.call(go)


# --------------------------------------------------------------------------- #
# 1. resolution order, with set_id carried through
# --------------------------------------------------------------------------- #
def test_resolver_order_workload_then_scalar_then_published_then_tier():
    s = load_settings()
    reg = make_test_registry(s)
    r = QualityResolver(s, registry=reg)
    tier_q = s.routing.tier_quality["mid"]
    assert r.resolve("local-32b", "claims") == QualityScore(tier_q, "tier_default")

    r.set_overrides({
        ("local-32b", "claims"): QualityScore(0.91, "calibrated", "set-1"),
        ("local-32b", None): QualityScore(0.62, "calibrated", "set-1"),
        ("local-8b", None): QualityScore(0.8, "customer"),
    })
    assert r.resolve("local-32b", "claims") == QualityScore(0.91, "calibrated", "set-1")
    assert r.resolve("local-32b", "claims").set_id == "set-1"
    # unknown workload and no workload fall back to the (model, None) row
    assert r.resolve("local-32b", "billing") == QualityScore(0.62, "calibrated", "set-1")
    assert r.resolve("local-32b") == QualityScore(0.62, "calibrated", "set-1")
    assert r.resolve("local-8b", "claims") == QualityScore(0.8, "customer", None)

    # published beats tier_default when no override exists
    pool = [c.model_copy(update={"quality": 0.61}) if c.name == "azure-gpt-mid" else c
            for c in S_CLOUD.routing.model_pool]
    s_pub = S_CLOUD.model_copy(update={"routing": S_CLOUD.routing.model_copy(
        update={"model_pool": pool})})
    r2 = QualityResolver(s_pub, registry=make_test_registry(s_pub))
    assert r2.resolve("azure-gpt-mid", "claims") == QualityScore(0.61, "published")
    r2.set_overrides({("azure-gpt-mid", "claims"): QualityScore(0.3, "calibrated", "s")})
    assert r2.resolve("azure-gpt-mid", "claims") == QualityScore(0.3, "calibrated", "s")
    assert r2.resolve("azure-gpt-mid") == QualityScore(0.61, "published")


# --------------------------------------------------------------------------- #
# 2. identity: an empty override map changes nothing
# --------------------------------------------------------------------------- #
def test_empty_overrides_are_byte_identical_to_today():
    reg = make_test_registry(S_CLOUD)
    fresh = Router(S_CLOUD, registry=reg)
    shared = QualityResolver(S_CLOUD, registry=reg)
    shared.set_overrides({})
    wired = Router(S_CLOUD, quality_resolver=shared, registry=reg)
    for kw in ({}, {"min_tier": "mid"}):
        a = fresh.select(_ctx(reg, {"onprem", "azure", "bedrock"}),
                         tokens_after=200, complexity=0.1, **kw)
        b = wired.select(_ctx(reg, {"onprem", "azure", "bedrock"}, workload="claims"),
                         tokens_after=200, complexity=0.1, **kw)
        assert (a.chosen, a.est_cost_usd, a.reason, a.rejected, a.quality_score,
                a.quality_source, a.tier_target) == \
               (b.chosen, b.est_cost_usd, b.reason, b.rejected, b.quality_score,
                b.quality_source, b.tier_target)
        assert a.scoring.to_record() == b.scoring.to_record()
        assert all("set" not in c["quality"] for c in b.scoring.to_record()["candidates"])


def test_empty_table_after_refresh_matches_a_fresh_app_at_http_boundary():
    with _client() as c1:
        r1 = c1.post("/v1/chat/completions", headers=DEMO, json=HELLO)
        assert r1.status_code == 200, r1.text
        rec1 = _outcomes(c1)[0].scoring
    with _client() as c2:
        assert _refresh(c2) is True
        r2 = c2.post("/v1/chat/completions",
                     headers={**DEMO, "x-routeforge-workload": "claims"}, json=HELLO)
        assert r2.status_code == 200, r2.text
        rec2 = _outcomes(c2)[0].scoring
    assert r1.json()["model"] == r2.json()["model"]
    assert rec1 == rec2


# --------------------------------------------------------------------------- #
# 3. a workload-scoped row changes only that workload's routing
# --------------------------------------------------------------------------- #
def test_workload_scoped_override_changes_only_that_workload():
    with _client() as c:
        disable_seeded_models(c)
        add_model(c, "qa-a", tier="cheap", price_input=0.05, price_output=0.05)
        add_model(c, "qa-b", tier="cheap", price_input=0.05, price_output=0.05)
        base = c.post("/v1/chat/completions", headers=DEMO, json=HELLO)
        assert base.status_code == 200, base.text
        assert base.json()["model"] == "qa-a"   # tie-break: A ranks first

        _write_set(c, "set-claims", [{"model": "qa-b", "workload": "claims", "score": 0.95},
                                     {"model": "qa-a", "workload": "claims", "score": 0.10}])
        assert _refresh(c) is True

        claims = c.post("/v1/chat/completions",
                        headers={**DEMO, "x-routeforge-workload": "claims"}, json=HELLO)
        plain = c.post("/v1/chat/completions", headers=DEMO, json=HELLO)
        assert claims.status_code == 200 and plain.status_code == 200
        assert claims.json()["model"] == "qa-b"
        assert plain.json()["model"] == "qa-a"

        rows = {r.request_id: r for r in _outcomes(c)}
        claims_q = [cand["quality"] for cand in
                    rows[claims.json()["id"]].scoring["candidates"]]
        plain_q = [cand["quality"] for cand in
                   rows[plain.json()["id"]].scoring["candidates"]]
        assert claims_q and all(q.get("set") == "set-claims" for q in claims_q)
        assert all(q["src"] == "calibrated" for q in claims_q)
        assert plain_q and all("set" not in q for q in plain_q)


# --------------------------------------------------------------------------- #
# 4. eligibility never reads quality: onprem_only never reaches cloud
# --------------------------------------------------------------------------- #
def test_calibrated_cloud_quality_never_lifts_onprem_only():
    reg = make_test_registry(S_CLOUD)
    q = QualityResolver(S_CLOUD, registry=reg)
    q.set_overrides({
        ("azure-gpt-mid", "claims"): QualityScore(1.0, "calibrated", "set-x"),
        ("azure-gpt-mid", None): QualityScore(1.0, "calibrated", "set-x"),
        ("local-32b", None): QualityScore(0.0, "calibrated", "set-x"),
    })
    r = Router(S_CLOUD, quality_resolver=q, registry=reg)
    # Control: with cloud allowed, the calibration does flip the mid tier.
    open_ = r.select(_ctx(reg, {"onprem", "azure", "bedrock"}, workload="claims"),
                     tokens_after=200, complexity=0.1, min_tier="mid")
    assert open_.chosen.name == "azure-gpt-mid"
    # onprem_only: the cloud model is filtered before scoring, whatever its score.
    d = r.select(_ctx(reg, {"onprem"}, onprem=True, workload="claims"),
                 tokens_after=200, complexity=0.1, min_tier="mid")
    assert d.chosen.provider == "onprem"
    assert all(c["name"] != "azure-gpt-mid" for c in d.scoring.to_record()["candidates"])


# --------------------------------------------------------------------------- #
# 5. refresh: picks up new rows; a failed load keeps the last snapshot
# --------------------------------------------------------------------------- #
def test_refresh_picks_up_new_rows_and_keeps_snapshot_on_failure(monkeypatch):
    with _client() as c:
        q = c.app.state.quality
        assert c.app.state.model_router._quality is q
        assert q.resolve("local-8b").source == "tier_default"
        _write_set(c, "set-r", [{"model": "local-8b", "workload": None, "score": 0.77}])
        assert _refresh(c) is True
        assert q.resolve("local-8b") == QualityScore(0.77, "calibrated", "set-r")

        async def boom(session):
            raise RuntimeError("db down")
        monkeypatch.setattr(store, "load_quality_overrides", boom)
        assert _refresh(c) is False
        assert q.resolve("local-8b") == QualityScore(0.77, "calibrated", "set-r")


# --------------------------------------------------------------------------- #
# 6. a model onboarded after boot resolves to its tier default
# --------------------------------------------------------------------------- #
def test_model_onboarded_after_boot_resolves_to_its_tier_default():
    with _client() as c:
        q = c.app.state.quality
        assert q.resolve("late-mid").score == 0.5   # unknown: the neutral floor
        add_model(c, "late-mid", tier="mid", price_input=0.1, price_output=0.1)
        mid_q = c.app.state.settings.routing.tier_quality["mid"]
        assert mid_q != 0.5
        assert q.resolve("late-mid") == QualityScore(mid_q, "tier_default")
