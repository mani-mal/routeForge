"""P3a Task 2: building a scenario from a config snapshot (spec §6)."""
from __future__ import annotations

import asyncio
import json

import pytest

from app.eval.scenario import (SnapshotError, build_scenario, close_scenario,
                               load_snapshot, validate_snapshot)
from app.policy.feasible import feasible_set
from tests.eval.sim_fixtures import BASE_SETTINGS, app_entry, make_snapshot, onboarded_row


def _build(doc):
    async def go():
        scn = await build_scenario(doc, base=BASE_SETTINGS)
        try:
            return {
                "pool": sorted(c.name for c in scn.registry.effective_pool()),
                "apps": sorted(scn.apps),
                "price": scn.registry.price_for("local-mid").input,
                "lat": scn.signals.latency("local-mid", "mid"),
                "q": scn.quality.resolve("local-mid", None),
                "eff_residency": scn.apps["app-a"].eff.placement.residency,
            }
        finally:
            await close_scenario(scn)
    return asyncio.run(go())


def test_builds_pool_apps_prices():
    out = _build(make_snapshot())
    assert {"local-cheap", "local-mid", "local-big"} <= set(out["pool"])
    assert out["apps"] == ["app-a"]
    assert out["price"] == 0.5


def test_disabled_onboarded_row_is_not_in_pool():
    doc = make_snapshot(onboarded=[onboarded_row("local-cheap", "cheap"),
                                   onboarded_row("local-mid", "mid", enabled=False)])
    assert "local-mid" not in _build(doc)["pool"]


def test_frozen_signals_measured_and_default():
    doc = make_snapshot(signals={"local-mid": {"latency_ms": 321.0, "latency_n": 50,
                                               "availability": 0.9, "availability_n": 50}})
    lat = _build(doc)["lat"]
    assert (lat.value, lat.source) == (321.0, "measured")
    lat2 = _build(make_snapshot())["lat"]
    assert lat2.source in ("tier_default", "disabled")


def test_quality_overrides_loaded():
    doc = make_snapshot(quality={"customer": [], "calibrated": [
        {"model": "local-mid", "workload": None, "score": 0.91, "set_id": "set-1"}]})
    q = _build(doc)["q"]
    assert (q.score, q.source) == (0.91, "calibrated")


def test_app_policy_compiled():
    doc = make_snapshot(apps=[app_entry("app-a", residency="onprem_only")])
    assert _build(doc)["eff_residency"] == "onprem_only"


@pytest.mark.parametrize("mutate,where", [
    (lambda d: d.update(schema="routeforge.config-snapshot/v2"), "schema"),
    (lambda d: d.pop("apps"), "apps"),
    (lambda d: d["settings"]["routing"].pop("tiers"), "settings.routing"),
    (lambda d: d["settings"]["providers"].update(nosuch={"enabled": True, "region": None,
        "prompt_retention": None, "accepts_raw_sensitive": None}), "settings.providers.nosuch"),
    (lambda d: d["apps"][0]["policy"].update(bogus=1), "apps.app-a.policy.bogus"),
])
def test_load_errors_name_the_location(mutate, where):
    """The first two cases raise from `validate_snapshot` (structural); the
    rest raise from `build_scenario` -- a section-specific Pydantic
    validation failure (`settings.routing`), an undeclared provider
    (`settings.providers.nosuch`), and an unknown app-policy field
    (`apps.app-a.policy.bogus`). Each names its own location, not a generic
    `settings.<section>` placeholder."""
    doc = make_snapshot()
    mutate(doc)
    with pytest.raises(SnapshotError) as exc:
        validate_snapshot(doc)
        asyncio.run(build_scenario(doc, base=BASE_SETTINGS))
    assert str(exc.value).startswith(where)


def test_load_snapshot_hashes_file(tmp_path):
    p = tmp_path / "s.json"
    p.write_text(json.dumps(make_snapshot()))
    doc, sha = load_snapshot(str(p))
    assert doc["schema"] == "routeforge.config-snapshot/v1" and len(sha) == 64


def test_round_trip_matches_live_feasible_set():
    """Spec §9.10: a scenario built from a LIVE snapshot reproduces the live
    app's policy-side feasible set."""
    from fastapi.testclient import TestClient
    from app.main import create_app
    from tests.test_admin_api import _login
    with TestClient(create_app()) as c:
        _login(c)
        doc = c.get("/admin/config-snapshot").json()
        st = c.app.state
        async def live():
            from app.apps.policy import load_policy_view
            async with st.db.session() as s:
                view = await load_policy_view(s, "default")
            return sorted(x.name for x in feasible_set(
                st.settings, st.registry, view, tenant_id="default").eligible)
        live_names = c.portal.call(live)

    async def sim():
        scn = await build_scenario(doc, base=BASE_SETTINGS)
        try:
            return sorted(x.name for x in feasible_set(
                scn.settings, scn.registry, scn.apps["default"].view,
                tenant_id="default").eligible)
        finally:
            await close_scenario(scn)
    assert asyncio.run(sim()) == live_names
