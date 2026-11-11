"""P2b-3 Task 5: admin API to validate, dry-run, apply, list and revert a
quality-calibration recommendation (spec §6).

Each test builds its own `_client()` (fresh app, fresh in-memory DB, matching
`tests/test_quality_serving.py`), so tests never share state.
"""
from __future__ import annotations

from contextlib import contextmanager

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.apps import store
from app.apps.calibration_sets import recommendation_set_id as serving_set_id
from app.apps.models import ModelQuality, ModelQualityBound, RoutingOutcome
from app.eval.calibration import build_recommendation
from app.eval.calibration import recommendation_set_id as eval_set_id
from app.main import create_app, refresh_quality
from tests.test_admin_api import _login

DEMO = {"Authorization": "Bearer demo-key"}
HELLO = {"messages": [{"role": "user", "content": "hello"}]}


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
    """The registry lazy-loads onboarded rows on first use (see
    `ModelRegistry.ensure_loaded`) -- force that here so the version matches
    what the route itself will compute, rather than the YAML-only version
    from before any onboarded row has been read."""
    async def go():
        await c.app.state.registry.ensure_loaded(c.app.state.db)
        return c.app.state.registry.registry_version()
    return c.portal.call(go)


def _write_customer_quality(c: TestClient, model: str, score: float) -> None:
    async def go():
        async with c.app.state.db.session() as session:
            await store.upsert_model_quality(
                session, model_name=model, score=score, source="customer")
            await session.commit()
        await refresh_quality(c.app)
    c.portal.call(go)


def _unit(model: str, *, mean: float = 0.9, workload: str | None = None,
         status: str = "recommended") -> dict:
    return {
        "model": model, "workload": workload,
        "q0": 0.5, "q0_source": "tier_default",
        "alpha": 27.0, "beta": 3.0, "mean": mean, "ci_low": 0.75, "ci_high": 0.98,
        "shift": round(mean - 0.5, 6), "n_labels": 30, "n_rows": 30,
        "label_coverage": 1.0, "status": status, "reason": None,
    }


def _rec(registry_version: str, units: list[dict]) -> dict:
    estimate_dict = {
        "registry_version": registry_version,
        "options": {"k": 10.0, "min_labels": 20, "max_ci_width": 0.25,
                    "min_shift": 0.02, "ci_level": 0.9, "priors": None},
        "units": units,
    }
    return build_recommendation(estimate_dict, export_sha256="a" * 64)


def test_dry_run_shows_current_and_proposed_and_writes_nothing():
    with _client() as c:
        _login(c)
        reg_version = _registry_version(c)
        rec = _rec(reg_version, [_unit("local-8b", mean=0.9)])

        r = c.post("/admin/quality/calibrations?dry_run=true", json=rec)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["set_id"] == rec["set_id"]
        assert body["registry_version_match"] is True
        [unit] = body["units"]
        assert unit["model"] == "local-8b"
        assert unit["workload"] is None
        assert unit["current"] == {"score": 0.5, "source": "tier_default", "set_id": None}
        assert unit["proposed"] == 0.9
        assert unit["customer_wins"] is False

        listed = c.get("/admin/quality/calibrations")
        assert listed.status_code == 200
        assert listed.json() == []


def test_apply_writes_rows_and_routed_request_carries_calibrated_source():
    with _client() as c:
        _login(c)
        reg_version = _registry_version(c)
        rec = _rec(reg_version, [_unit("local-8b", mean=0.97)])

        r = c.post("/admin/quality/calibrations", json=rec)
        assert r.status_code == 200, r.text
        assert r.json() == {"set_id": rec["set_id"], "applied": 1, "refreshed": True,
                            "registry_version_match": True, "forced": False}

        chat = c.post("/v1/chat/completions", headers=DEMO, json=HELLO)
        assert chat.status_code == 200, chat.text
        assert chat.json()["model"] == "local-8b"

        rows = {row.request_id: row for row in _outcomes(c)}
        record = rows[chat.json()["id"]].scoring
        [cand] = [x for x in record["candidates"] if x["name"] == "local-8b"]
        assert cand["quality"]["src"] == "calibrated"
        assert cand["quality"]["set"] == rec["set_id"]
        assert cand["quality"]["v"] == 0.97
        assert cand["quality"]["base"] == {"v": 0.5, "src": "tier_default"}


def test_reapplying_same_set_gives_409():
    with _client() as c:
        _login(c)
        reg_version = _registry_version(c)
        rec = _rec(reg_version, [_unit("local-8b", mean=0.8)])

        first = c.post("/admin/quality/calibrations", json=rec)
        assert first.status_code == 200, first.text

        second = c.post("/admin/quality/calibrations", json=rec)
        assert second.status_code == 409


def test_revert_restores_previous_resolution():
    with _client() as c:
        _login(c)
        reg_version = _registry_version(c)
        rec = _rec(reg_version, [_unit("local-8b", mean=0.8)])

        apply = c.post("/admin/quality/calibrations", json=rec)
        assert apply.status_code == 200, apply.text
        assert c.app.state.quality.resolve("local-8b").source == "calibrated"

        revert = c.delete(f"/admin/quality/calibrations/{rec['set_id']}")
        assert revert.status_code == 200, revert.text
        # one score row + the unit's lower-bound row
        assert revert.json() == {"set_id": rec["set_id"], "deleted": 2}
        assert c.app.state.quality.resolve("local-8b").source == "tier_default"

        listed = c.get("/admin/quality/calibrations")
        assert listed.json() == []


def test_hash_mismatch_gives_422_and_writes_nothing():
    with _client() as c:
        _login(c)
        reg_version = _registry_version(c)
        rec = _rec(reg_version, [_unit("local-8b", mean=0.8)])
        rec["set_id"] = "deadbeefdeadbeef"

        r = c.post("/admin/quality/calibrations", json=rec)
        assert r.status_code == 422
        assert "set_id" in r.json()["detail"]

        listed = c.get("/admin/quality/calibrations")
        assert listed.json() == []


def test_unknown_model_gives_422():
    with _client() as c:
        _login(c)
        reg_version = _registry_version(c)
        rec = _rec(reg_version, [_unit("no-such-model", mean=0.8)])

        r = c.post("/admin/quality/calibrations", json=rec)
        assert r.status_code == 422
        assert "no-such-model" in r.json()["detail"]

        listed = c.get("/admin/quality/calibrations")
        assert listed.json() == []


def test_registry_mismatch_gives_422_without_force_and_200_with_force():
    with _client() as c:
        _login(c)
        rec = _rec("stale-registry-version", [_unit("local-8b", mean=0.8)])

        blocked = c.post("/admin/quality/calibrations?dry_run=true", json=rec)
        assert blocked.status_code == 422
        assert "registry_version" in blocked.json()["detail"]

        forced = c.post(
            "/admin/quality/calibrations?dry_run=true&force_registry_mismatch=true",
            json=rec)
        assert forced.status_code == 200, forced.text
        assert forced.json()["registry_version_match"] is False


def test_customer_row_wins_in_dry_run_and_after_apply():
    with _client() as c:
        _login(c)
        _write_customer_quality(c, "local-8b", 0.42)
        reg_version = _registry_version(c)
        rec = _rec(reg_version, [_unit("local-8b", mean=0.9)])

        dry = c.post("/admin/quality/calibrations?dry_run=true", json=rec)
        assert dry.status_code == 200, dry.text
        [unit] = dry.json()["units"]
        assert unit["current"]["source"] == "customer"
        assert unit["current"]["score"] == 0.42
        assert unit["customer_wins"] is True

        apply = c.post("/admin/quality/calibrations", json=rec)
        assert apply.status_code == 200, apply.text
        resolved = c.app.state.quality.resolve("local-8b")
        assert resolved.source == "customer"
        assert resolved.score == 0.42


def test_no_admin_session_gives_401():
    with _client() as c:
        reg_version = _registry_version(c)
        rec = _rec(reg_version, [_unit("local-8b", mean=0.8)])

        assert c.post("/admin/quality/calibrations", json=rec).status_code == 401
        assert c.get("/admin/quality/calibrations").status_code == 401
        assert c.delete(f"/admin/quality/calibrations/{rec['set_id']}").status_code == 401


def test_serving_and_eval_set_id_implementations_agree():
    rec = _rec("some-registry-version", [_unit("local-8b", mean=0.8),
                                         _unit("local-32b", mean=0.3, workload="claims")])
    assert serving_set_id(rec) == eval_set_id(rec) == rec["set_id"]


# --------------------------------------------------------------------------- #
# B80 Task 4: calibrated lower bounds are stored beside the scores
# --------------------------------------------------------------------------- #
def _table_rows(c: TestClient, model) -> list:
    async def go():
        async with c.app.state.db.session() as session:
            return list((await session.execute(select(model))).scalars())
    return c.portal.call(go)


def test_apply_writes_one_score_row_and_two_bound_rows():
    with _client() as c:
        _login(c)
        rec = _rec(_registry_version(c), [
            _unit("local-8b", mean=0.9),
            _unit("local-32b", mean=0.8, status="no_change")])
        r = c.post("/admin/quality/calibrations", json=rec)
        assert r.status_code == 200, r.text
        assert r.json()["applied"] == 1
        assert len(_table_rows(c, ModelQuality)) == 1
        bounds = _table_rows(c, ModelQualityBound)
        assert {(b.model_name, b.lower, b.ci_level, b.set_id) for b in bounds} == {
            ("local-8b", 0.75, 0.9, rec["set_id"]),
            ("local-32b", 0.75, 0.9, rec["set_id"])}


def test_dry_run_lists_bounds_and_writes_nothing():
    with _client() as c:
        _login(c)
        rec = _rec(_registry_version(c), [
            _unit("local-8b", mean=0.9, workload="claims"),
            _unit("local-32b", mean=0.8, status="no_change")])
        r = c.post("/admin/quality/calibrations?dry_run=true", json=rec)
        assert r.status_code == 200, r.text
        assert sorted(r.json()["bounds"], key=lambda b: b["model"]) == [
            {"model": "local-32b", "workload": None, "lower": 0.75, "ci_level": 0.9},
            {"model": "local-8b", "workload": "claims", "lower": 0.75, "ci_level": 0.9}]
        assert _table_rows(c, ModelQualityBound) == []


def test_all_no_change_file_applies_as_bounds_only_set_listed_and_revertable():
    with _client() as c:
        _login(c)
        rec = _rec(_registry_version(c), [
            _unit("local-8b", status="no_change"),
            _unit("local-32b", status="no_change", workload="claims")])
        r = c.post("/admin/quality/calibrations", json=rec)
        assert r.status_code == 200, r.text
        assert r.json()["applied"] == 0
        assert _table_rows(c, ModelQuality) == []
        assert len(_table_rows(c, ModelQualityBound)) == 2

        [listed] = c.get("/admin/quality/calibrations").json()
        assert listed["set_id"] == rec["set_id"]
        assert listed["units"] == 0 and listed["bounds"] == 2

        assert c.post("/admin/quality/calibrations", json=rec).status_code == 409

        d = c.delete(f"/admin/quality/calibrations/{rec['set_id']}")
        assert d.status_code == 200, d.text
        assert d.json()["deleted"] == 2
        assert _table_rows(c, ModelQualityBound) == []
        assert c.get("/admin/quality/calibrations").json() == []


def test_ci_low_above_mean_is_422():
    with _client() as c:
        _login(c)
        bad = _unit("local-8b", mean=0.6)
        bad["ci_low"] = 0.7
        rec = _rec(_registry_version(c), [bad])
        r = c.post("/admin/quality/calibrations", json=rec)
        assert r.status_code == 422
        assert "ci_low" in r.json()["detail"]
        assert _table_rows(c, ModelQualityBound) == []


def test_file_without_ci_low_still_applies_scores_only():
    with _client() as c:
        _login(c)
        u = _unit("local-8b", mean=0.9)
        del u["ci_low"]
        rec = _rec(_registry_version(c), [u])
        r = c.post("/admin/quality/calibrations", json=rec)
        assert r.status_code == 200, r.text
        assert r.json()["applied"] == 1
        assert _table_rows(c, ModelQualityBound) == []


def test_ci_level_absent_from_options_defaults_to_point_nine():
    with _client() as c:
        _login(c)
        rec = _rec(_registry_version(c), [_unit("local-8b")])
        del rec["options"]["ci_level"]
        rec["set_id"] = serving_set_id(rec)
        assert c.post("/admin/quality/calibrations", json=rec).status_code == 200
        [b] = _table_rows(c, ModelQualityBound)
        assert b.ci_level == 0.9
