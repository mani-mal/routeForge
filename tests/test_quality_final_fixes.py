"""P2b-3 final-review fix wave: cross-key precedence, the `base` prior for a
second calibration round, apply-route validation/audit, refresh ordering,
and B54's onboarded-published-quality case.

Real apps from `create_app()` and a real TestClient, like
`tests/test_quality_calibrations_api.py`; each test builds its own client.
"""
from __future__ import annotations

import asyncio
import json
import logging
from contextlib import contextmanager

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.apps import store
from app.apps.models import RoutingOutcome
from app.config.loader import load_settings
from app.eval.calibration import build_recommendation, estimate
from app.main import create_app, refresh_quality
from app.routing.quality import QualityResolver, QualityScore
from tests._litellm_fake import make_test_registry
from tests.onboarding.harness import disable_seeded_models
from tests.test_admin_api import _login

DEMO = {"Authorization": "Bearer demo-key"}
HELLO = {"messages": [{"role": "user", "content": "hello"}]}
CLAIMS = {**DEMO, "x-routeforge-workload": "claims"}


@contextmanager
def _client():
    with TestClient(create_app(), raise_server_exceptions=False) as c:
        yield c


def _outcomes(c: TestClient) -> list[RoutingOutcome]:
    async def go():
        async with c.app.state.db.session() as session:
            return list((await session.execute(select(RoutingOutcome))).scalars())
    return c.portal.call(go)


def _registry_version(c: TestClient) -> str:
    async def go():
        await c.app.state.registry.ensure_loaded(c.app.state.db)
        return c.app.state.registry.registry_version()
    return c.portal.call(go)


def _write_customer(c: TestClient, model: str, score: float,
                    task_class: str | None = None) -> None:
    async def go():
        async with c.app.state.db.session() as session:
            await store.upsert_model_quality(
                session, model_name=model, task_class=task_class, score=score)
            await session.commit()
        await refresh_quality(c.app)
    c.portal.call(go)


def _write_set(c: TestClient, set_id: str, units: list[dict]) -> None:
    async def go():
        async with c.app.state.db.session() as session:
            await store.write_calibration_set(session, set_id=set_id, units=units)
            await session.commit()
        await refresh_quality(c.app)
    c.portal.call(go)


def _unit(model: str, *, mean: float = 0.9, workload=None,
          status: str = "recommended") -> dict:
    return {
        "model": model, "workload": workload,
        "q0": 0.5, "q0_source": "tier_default",
        "alpha": 27.0, "beta": 3.0, "mean": mean, "ci_low": 0.75, "ci_high": 0.98,
        "shift": round(mean - 0.5, 6), "n_labels": 30, "n_rows": 30,
        "label_coverage": 1.0, "status": status, "reason": None,
    }


def _rec(registry_version: str, units: list[dict]) -> dict:
    return build_recommendation({
        "registry_version": registry_version,
        "options": {"k": 10.0, "min_labels": 20, "max_ci_width": 0.25,
                    "min_shift": 0.02, "ci_level": 0.9, "priors": None},
        "units": units,
    }, export_sha256="a" * 64)


# --------------------------------------------------------------------------- #
# 1. cross-key precedence: customer (m, None) beats calibrated (m, wl)
# --------------------------------------------------------------------------- #
def test_resolver_customer_model_wide_beats_calibrated_per_workload():
    s = load_settings()
    r = QualityResolver(s, registry=make_test_registry(s))
    r.set_overrides({
        ("local-8b", None): QualityScore(0.42, "customer"),
        ("local-8b", "claims"): QualityScore(0.95, "calibrated", "set-1"),
    })
    assert r.resolve("local-8b", "claims") == QualityScore(0.42, "customer")
    assert r.resolve("local-8b") == QualityScore(0.42, "customer")


def test_resolver_full_ladder_order():
    s = load_settings()
    r = QualityResolver(s, registry=make_test_registry(s))
    # one key may carry both sources
    r.set_overrides({
        ("local-8b", "claims"): [QualityScore(0.11, "customer"),
                                 QualityScore(0.22, "calibrated", "s")],
        ("local-8b", None): [QualityScore(0.33, "customer"),
                             QualityScore(0.44, "calibrated", "s")],
        ("local-32b", "claims"): QualityScore(0.55, "calibrated", "s"),
        ("local-32b", None): QualityScore(0.66, "calibrated", "s"),
    })
    assert r.resolve("local-8b", "claims").score == 0.11       # customer (m, wl)
    assert r.resolve("local-8b", "other").score == 0.33        # customer (m, None)
    assert r.resolve("local-32b", "claims").score == 0.55      # calibrated (m, wl)
    assert r.resolve("local-32b", "other").score == 0.66       # calibrated (m, None)
    assert r.resolve("unknown-x").source == "tier_default"


def test_store_keeps_customer_and_calibrated_separate_per_key():
    async def go():
        from app.apps.db import Database
        db = Database(url="sqlite+aiosqlite://")
        await db.ensure_ready()
        async with db.session() as s:
            await store.upsert_model_quality(s, model_name="local-8b", score=0.4)
            await store.write_calibration_set(
                s, set_id="set-a",
                units=[{"model": "local-8b", "workload": None, "score": 0.8},
                       {"model": "local-8b", "workload": "claims", "score": 0.9}])
            await s.commit()
        async with db.session() as s:
            return await store.load_quality_overrides(s)
    out = asyncio.run(go())
    assert out[("local-8b", None)] == {"customer": (0.4, None),
                                       "calibrated": (0.8, "set-a")}
    assert out[("local-8b", "claims")] == {"calibrated": (0.9, "set-a")}


def test_upsert_model_quality_refuses_non_customer_source():
    async def go():
        from app.apps.db import Database
        db = Database(url="sqlite+aiosqlite://")
        await db.ensure_ready()
        async with db.session() as s:
            await store.upsert_model_quality(
                s, model_name="local-8b", score=0.4, source="calibrated")
    with pytest.raises(ValueError, match="customer rows only"):
        asyncio.run(go())


def test_cross_key_customer_wins_in_dry_run_and_after_apply():
    with _client() as c:
        _login(c)
        _write_customer(c, "local-8b", 0.42)                 # (m, None) customer
        rec = _rec(_registry_version(c), [_unit("local-8b", workload="claims", mean=0.95)])

        dry = c.post("/admin/quality/calibrations?dry_run=true", json=rec)
        assert dry.status_code == 200, dry.text
        [u] = dry.json()["units"]
        assert u["current"]["source"] == "customer"
        assert u["customer_wins"] is True

        assert c.post("/admin/quality/calibrations", json=rec).status_code == 200
        resolved = c.app.state.quality.resolve("local-8b", "claims")
        assert (resolved.source, resolved.score) == ("customer", 0.42)
        chat = c.post("/v1/chat/completions", headers=CLAIMS, json=HELLO)
        assert chat.status_code == 200, chat.text
        rows = {r.request_id: r for r in _outcomes(c)}
        [cand] = [x for x in rows[chat.json()["id"]].scoring["candidates"]
                  if x["name"] == "local-8b"]
        assert cand["quality"]["src"] == "customer"


def test_dry_run_customer_wins_false_when_only_a_calibrated_row_exists():
    with _client() as c:
        _login(c)
        _write_set(c, "old-set", [{"model": "local-8b", "workload": None, "score": 0.3}])
        rec = _rec(_registry_version(c), [_unit("local-8b", workload="claims")])
        [u] = c.post("/admin/quality/calibrations?dry_run=true", json=rec).json()["units"]
        assert u["current"]["source"] == "calibrated"
        assert u["customer_wins"] is False


# --------------------------------------------------------------------------- #
# minor: dry-run flags units shadowed by a more specific existing row
# --------------------------------------------------------------------------- #
def test_dry_run_flags_shadowing_by_more_specific_rows():
    with _client() as c:
        _login(c)
        _write_set(c, "old-set", [{"model": "local-8b", "workload": "claims", "score": 0.3}])
        _write_customer(c, "local-8b", 0.6, task_class="billing")
        rec = _rec(_registry_version(c), [_unit("local-8b", workload=None),
                                          _unit("local-32b", workload="claims")])
        units = {(u["model"], u["workload"]): u for u in
                 c.post("/admin/quality/calibrations?dry_run=true", json=rec).json()["units"]}
        assert units[("local-8b", None)]["shadowed_by"] == [
            {"workload": "billing", "source": "customer", "set_id": None},
            {"workload": "claims", "source": "calibrated", "set_id": "old-set"},
        ]
        assert units[("local-32b", "claims")]["shadowed_by"] == []


# --------------------------------------------------------------------------- #
# 2. `base` on override-supplied quality terms
# --------------------------------------------------------------------------- #
def test_calibrated_term_carries_base_and_plain_terms_do_not():
    with _client() as c:
        _write_set(c, "set-b", [{"model": "local-8b", "workload": "claims", "score": 0.9}])
        claims = c.post("/v1/chat/completions", headers=CLAIMS, json=HELLO)
        plain = c.post("/v1/chat/completions", headers=DEMO, json=HELLO)
        rows = {r.request_id: r for r in _outcomes(c)}
        [cq] = [x["quality"] for x in rows[claims.json()["id"]].scoring["candidates"]
                if x["name"] == "local-8b"]
        assert cq == {"v": 0.9, "src": "calibrated", "set": "set-b",
                      "base": {"v": 0.5, "src": "tier_default"}}
        for cand in rows[plain.json()["id"]].scoring["candidates"]:
            assert "base" not in cand["quality"]


def test_customer_term_carries_base_too():
    s = load_settings()
    r = QualityResolver(s, registry=make_test_registry(s))
    r.set_overrides({("local-32b", None): QualityScore(0.2, "customer")})
    q = r.resolve("local-32b")
    assert q.base == (s.routing.tier_quality["mid"], "tier_default")
    assert r.resolve("local-8b").base is None


# --------------------------------------------------------------------------- #
# 3/4. validation: zero units, workload shape, duplicates -> 422
# --------------------------------------------------------------------------- #
def test_zero_recommended_units_is_422_for_dry_run_and_apply():
    with _client() as c:
        _login(c)
        rec = _rec(_registry_version(c), [_unit("local-8b", status="insufficient")])
        for q in ("?dry_run=true", ""):
            r = c.post(f"/admin/quality/calibrations{q}", json=rec)
            assert r.status_code == 422, r.text
            assert r.json()["detail"] == "no recommended units"
        assert c.get("/admin/quality/calibrations").json() == []


@pytest.mark.parametrize("workload", ["*", "Claims", "", " claims", "a" * 65,
                                      {"x": 1}, ["claims"], 7, "claims!"])
def test_invalid_unit_workload_is_422(workload):
    with _client() as c:
        _login(c)
        rec = _rec(_registry_version(c), [_unit("local-8b", workload=workload)])
        for q in ("?dry_run=true", ""):
            r = c.post(f"/admin/quality/calibrations{q}", json=rec)
            assert r.status_code == 422, (workload, r.text)
            assert "workload" in r.json()["detail"]


@pytest.mark.parametrize("workload", [None, "claims", "a" * 64, "v1.claims_x-y"])
def test_valid_unit_workload_is_accepted(workload):
    with _client() as c:
        _login(c)
        rec = _rec(_registry_version(c), [_unit("local-8b", workload=workload)])
        r = c.post("/admin/quality/calibrations?dry_run=true", json=rec)
        assert r.status_code == 200, r.text


def test_duplicate_recommended_units_are_422():
    with _client() as c:
        _login(c)
        rec = _rec(_registry_version(c), [_unit("local-8b", workload="claims", mean=0.8),
                                          _unit("local-8b", workload="claims", mean=0.9)])
        r = c.post("/admin/quality/calibrations", json=rec)
        assert r.status_code == 422
        assert "duplicate" in r.json()["detail"]


# --------------------------------------------------------------------------- #
# 5. apply response fields + one audit line per apply / revert
# --------------------------------------------------------------------------- #
def _audit_lines(caplog) -> list[dict]:
    return [json.loads(r.getMessage()) for r in caplog.records
            if r.name == "serving.admin" and r.getMessage().startswith("{")]


def test_apply_reports_version_match_and_forced_and_audits(caplog):
    with _client() as c:
        _login(c)
        with caplog.at_level(logging.INFO, logger="serving.admin"):
            ok = c.post("/admin/quality/calibrations",
                        json=_rec(_registry_version(c), [_unit("local-8b")]))
            assert ok.status_code == 200, ok.text
            assert ok.json()["registry_version_match"] is True
            assert ok.json()["forced"] is False

            stale = _rec("stale-version", [_unit("local-32b")])
            forced = c.post("/admin/quality/calibrations?force_registry_mismatch=true",
                            json=stale)
            assert forced.status_code == 200, forced.text
            assert forced.json()["registry_version_match"] is False
            assert forced.json()["forced"] is True

            rv = c.delete(f"/admin/quality/calibrations/{stale['set_id']}")
            assert rv.status_code == 200
        lines = _audit_lines(caplog)
        assert [line["event"] for line in lines] == [
            "quality_calibration_applied", "quality_calibration_applied",
            "quality_calibration_reverted"]
        assert lines[1]["forced"] is True and lines[1]["units"] == 1
        assert lines[1]["set_id"] == stale["set_id"]
        assert lines[2]["units"] == 1
        assert all(line["admin_id"] for line in lines)


# --------------------------------------------------------------------------- #
# 6. B54: an onboarded CHEAP model's published quality is now scored
# --------------------------------------------------------------------------- #
def test_b54_onboarded_cheap_model_published_quality_is_scored():
    with _client() as c:
        disable_seeded_models(c)

        async def go():
            async with c.app.state.db.session() as session:
                await store.upsert_onboarded(
                    session, "b54-cheap", display_name="b54-cheap", provider="onprem",
                    tier="cheap", endpoint="http://test-local/v1", api_key_env=None,
                    price_input=0.05, price_output=0.05, quality=0.66,
                    context_window=None, allowed_app_ids=None, notes=None,
                    enabled=True, probe_status="healthy", probe_error=None,
                    region=None, prompt_retention=None, accepts_raw_sensitive=None)
                await session.commit()
            await c.app.state.registry.refresh(c.app.state.db)
        c.portal.call(go)

        r = c.post("/v1/chat/completions", headers=DEMO, json=HELLO)
        assert r.status_code == 200, r.text
        assert r.json()["model"] == "b54-cheap"
        [cand] = [x for x in _outcomes(c)[0].scoring["candidates"]
                  if x["name"] == "b54-cheap"]
        assert cand["quality"] == {"v": 0.66, "src": "published"}


# --------------------------------------------------------------------------- #
# minor: a slower, older refresh can never overwrite a newer snapshot
# --------------------------------------------------------------------------- #
def test_refresh_quality_installs_in_read_order(monkeypatch):
    with _client() as c:
        old = {("local-8b", None): {"calibrated": (0.1, "old")}}
        new = {("local-8b", None): {"calibrated": (0.9, "new")}}

        async def go():
            release = asyncio.Event()
            calls = 0

            async def fake_load(session):
                nonlocal calls
                calls += 1
                if calls == 1:           # the slow, older read
                    await release.wait()
                    return old
                return new               # the newer read (e.g. after an apply)

            monkeypatch.setattr(store, "load_quality_overrides", fake_load)
            slow = asyncio.create_task(refresh_quality(c.app))
            for _ in range(5):
                await asyncio.sleep(0)
            fast = asyncio.create_task(refresh_quality(c.app))
            for _ in range(5):
                await asyncio.sleep(0)
            release.set()
            assert await slow is True and await fast is True
            return c.app.state.quality.resolve("local-8b")

        q = c.portal.call(go)
        assert (q.score, q.set_id) == (0.9, "new")


# --------------------------------------------------------------------------- #
# minor: shutdown never raises when startup never ran
# --------------------------------------------------------------------------- #
def test_stop_quality_without_startup_does_not_raise():
    app = create_app()
    handlers = [h for h in app.router.on_shutdown if h.__name__ == "_stop_quality"]
    assert handlers
    asyncio.run(handlers[0]())


# --------------------------------------------------------------------------- #
# END TO END: route -> export -> estimate -> apply -> route -> export -> round 2
# --------------------------------------------------------------------------- #
def _export(c: TestClient) -> list[dict]:
    r = c.get("/admin/outcomes/export")
    assert r.status_code == 200, r.text
    records = []
    for line in r.text.splitlines():
        if line.strip():
            obj = json.loads(line)
            records.append({**obj["outcome"], "feedback": obj["feedback"]})
    return records


def test_end_to_end_calibration_round_trip_and_round_two():
    with _client() as c:
        _login(c)
        ids = []
        for _ in range(30):
            r = c.post("/v1/chat/completions", headers=CLAIMS, json=HELLO)
            assert r.status_code == 200, r.text
            ids.append(r.json()["id"])
        rows = {r.request_id: r for r in _outcomes(c)}
        chosen = {rows[i].chosen for i in ids}
        assert len(chosen) == 1
        [model] = chosen

        async def seed():
            async with c.app.state.db.session() as session:
                for i in ids:
                    await store.write_feedback(
                        session, request_id=i, application_id=rows[i].application_id,
                        score_name="quality", value_num=1.0, value_cat=None,
                        source="admin")
                await session.commit()
        c.portal.call(seed)

        # round one: the prior is the logged tier_default/published term
        first = _export(c)
        est = estimate(first)
        unit = next(u for u in est["units"]
                    if u["model"] == model and u["workload"] == "claims")
        assert unit["status"] == "recommended", unit
        rec = build_recommendation(est, export_sha256="e" * 64)

        applied = c.post("/admin/quality/calibrations", json=rec)
        assert applied.status_code == 200, applied.text
        assert applied.json()["registry_version_match"] is True

        after = c.post("/v1/chat/completions", headers=CLAIMS, json=HELLO)
        assert after.status_code == 200, after.text
        rows = {r.request_id: r for r in _outcomes(c)}
        [q] = [x["quality"] for x in rows[after.json()["id"]].scoring["candidates"]
               if x["name"] == model]
        assert q["src"] == "calibrated"
        assert q["set"] == rec["set_id"]
        assert q["base"] == {"v": unit["q0"], "src": unit["q0_source"]}

        # round two, over post-apply records only: every quality term there is
        # `calibrated`, so the prior can come only from `base`.
        second = [r for r in _export(c) if r["request_id"] == after.json()["id"]]
        assert second and second[0]["scoring"]["candidates"]
        est2 = estimate(second)
        u2 = next(u for u in est2["units"] if u["model"] == model and u["workload"] is None)
        assert (u2["q0"], u2["q0_source"]) == (unit["q0"], unit["q0_source"])
        assert u2["reason"] != "no prior"
