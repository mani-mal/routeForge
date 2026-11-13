"""B105: the Switchyard advisor shadow report (agreement + counterfactual)."""
from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from pathlib import Path

from app.eval.advisor_report import advisor_report, render_advisor_report
from app.eval.scenario import build_scenario, close_scenario
from app.eval.simulate import advisor_complexity, simulate_row
from app.eval.uncertainty import UncertaintyPlan
from app.routing.router import complexity_tier
from tests.eval.sim_fixtures import (BASE_SETTINGS, app_entry, make_row, make_snapshot,
                                     onboarded_row)

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "policy_simulate.py"


def _adv(tier, role=None, agreed=None, **over):
    role = role or ("efficient" if tier == "cheap" else "capable")
    return {"switchyard_mode": "shadow", "switchyard_role": role, "switchyard_tier": tier,
            "switchyard_confidence": 0.8, "switchyard_agreed": agreed,
            "switchyard_fallback_cause": None, **over}


def _fallback(cause):
    return {"switchyard_mode": "shadow", "switchyard_role": None, "switchyard_tier": None,
            "switchyard_confidence": None, "switchyard_agreed": None,
            "switchyard_fallback_cause": cause}


def _report(records, snap=None, plan=None):
    return asyncio.run(advisor_report(records, snap or make_snapshot(), base=BASE_SETTINGS,
                                      inputs={"t": 1}, plan=plan))


def _ts(i):
    return f"2026-09-20T12:00:{i:02d}+00:00"


def test_agreement_role_fallback_and_unadvised_counts():
    rows = [
        make_row("a1", "app-a", ts=_ts(1), workload="w1", **_adv("cheap", agreed=True)),
        make_row("a2", "app-a", ts=_ts(2), workload="w1", **_adv("cheap", agreed=True)),
        make_row("a3", "app-a", ts=_ts(3), workload="w2", **_adv("frontier", agreed=False)),
        make_row("a4", "app-a", ts=_ts(4), workload="w2", **_fallback("timeout")),
        make_row("a5", "app-a", ts=_ts(5), workload="w2", **_fallback("error")),
        make_row("a6", "app-a", ts=_ts(6), workload="w2", **_fallback("timeout")),
        make_row("b1", "app-b", ts=_ts(7), **_adv("cheap", agreed=False)),
        make_row("b2", "app-b", ts=_ts(8)),   # advisor never ran
    ]
    snap = make_snapshot(apps=[app_entry("app-a"), app_entry("app-b")])
    doc = _report(rows, snap)
    ag = doc["totals"]["agreement"]
    assert ag["rows"] == 8 and ag["advised"] == 4 and ag["advised_served"] == 4
    assert (ag["agreed"], ag["disagreed"]) == (2, 2) and ag["agreement"] == 0.5
    assert ag["role"] == {"efficient": 3, "capable": 1, "other": 0}
    assert ag["fallback_cause"] == {"error": 1, "timeout": 2}
    assert ag["without_advice"] == {"total": 4, "fallback": 3, "not_run": 1}
    assert abs(doc["by_app"]["app-a"]["agreement"]["agreement"] - 2 / 3) < 1e-5
    assert doc["by_app"]["app-b"]["agreement"]["agreement"] == 0.0
    assert doc["by_workload"]["w1"]["agreement"]["agreement"] == 1.0
    assert "(untagged)" in doc["by_workload"]
    assert doc["kind"] == "fixed_feature_replay"
    md = render_advisor_report(doc)
    assert "Fixed-feature replay" in md and "NOT predict task" in md and "app-b" in md


def test_pinned_rows_are_counted_separately_and_excluded():
    """B115: new-style `pinned` fallbacks and legacy pinned rows that carry a
    proposal are counted as pinned, and stay out of the agreement rate, the
    fallback causes and the counterfactual."""
    rows = [
        make_row("a1", "app-a", ts=_ts(1), **_adv("cheap", agreed=True)),
        make_row("a2", "app-a", ts=_ts(2), **_adv("frontier", agreed=False)),
        make_row("a3", "app-a", ts=_ts(3), **_fallback("timeout")),
        # New-style: the pipeline recorded `pinned` without calling the advisor.
        make_row("p1", "app-a", ts=_ts(4), requested_model="local-cheap",
                 **_fallback("pinned")),
        # Legacy (pre-B115): a proposal recorded on a pinned request -- would
        # read as a disagreement and move the counterfactual.
        make_row("p2", "app-a", ts=_ts(5), requested_model="local-cheap",
                 **_adv("frontier", agreed=False)),
        make_row("p3", "app-a", ts=_ts(6), requested_model="local-cheap",
                 **_adv("cheap", agreed=True)),
        # Legacy pinned fallback: also pinned, not a `timeout` cause.
        make_row("p4", "app-a", ts=_ts(7), requested_model="local-cheap",
                 **_fallback("timeout")),
        # Pinned but the advisor never ran: stays "not run".
        make_row("n1", "app-a", ts=_ts(8), requested_model="local-cheap"),
    ]
    doc = _report(rows)
    ag = doc["totals"]["agreement"]
    assert ag["rows"] == 8 and ag["pinned"] == 4
    assert ag["advised"] == 2 and (ag["agreed"], ag["disagreed"]) == (1, 1)
    assert ag["agreement"] == 0.5
    assert ag["fallback_cause"] == {"timeout": 1}
    assert ag["without_advice"] == {"total": 2, "fallback": 1, "not_run": 1}
    assert ag["role"] == {"efficient": 1, "capable": 1, "other": 0}
    assert doc["totals"]["counterfactual"]["replayed"] == 2
    assert doc["by_app"]["app-a"]["agreement"]["pinned"] == 4
    assert "pinned: 4 (excluded)" in render_advisor_report(doc)
    # The uncertainty ranges see the same unpinned rows only.
    plan = UncertaintyPlan(replicates=20, interval=0.9, seed=1, min_rows=1)
    unc = _report(rows, plan=plan)["uncertainty"]
    assert unc["agreement"]["totals"]["point"] == 0.5


def test_pinned_only_rows_have_no_agreement_or_counterfactual():
    rows = [make_row(f"p{i}", "app-a", ts=_ts(i), requested_model="local-cheap",
                     **_adv("frontier", agreed=False)) for i in range(3)]
    ag = _report(rows)["totals"]["agreement"]
    assert ag["pinned"] == 3 and ag["advised"] == 0 and ag["agreement"] is None
    assert _report(rows)["totals"]["counterfactual"]["replayed"] == 0


def test_pre_migration_rows_without_switchyard_columns():
    rows = [make_row(f"r{i}", "app-a", ts=_ts(i)) for i in range(3)]   # no switchyard_* keys
    doc = _report(rows)
    ag = doc["totals"]["agreement"]
    assert ag["advised"] == 0 and ag["agreement"] is None
    assert ag["without_advice"] == {"total": 3, "fallback": 0, "not_run": 3}
    assert doc["totals"]["counterfactual"]["replayed"] == 0
    assert doc["totals"]["counterfactual"]["cost_usd"]["delta"] == 0
    assert "n/a" in render_advisor_report(doc)


def test_efficient_vs_served_frontier_saves_cost():
    # Long request served by the frontier model; the advisor says efficient.
    row = make_row("x", "app-a", complexity=0.95, attempts=["local-big"], chosen="local-big",
                   **_adv("cheap", agreed=False))
    cf = _report([row])["totals"]["counterfactual"]
    assert cf["both_routed"] == 1
    assert cf["cost_usd"]["delta"] < 0 and cf["cost_usd"]["advisor"] < cf["cost_usd"]["baseline"]
    assert cf["tier_vs_baseline"]["lower_tier"] == 1 and cf["model_changed"] == 1


def test_capable_vs_served_cheap_costs_more():
    row = make_row("x", "app-a", complexity=0.05, **_adv("frontier", agreed=False))
    cf = _report([row])["totals"]["counterfactual"]
    assert cf["cost_usd"]["delta"] > 0 and cf["tier_vs_baseline"]["higher_tier"] == 1


def _replay(snap, row, tier):
    async def go():
        scn = await build_scenario(snap, base=BASE_SETTINGS)
        try:
            return simulate_row(scn, row, {}, advisor_tier=tier), simulate_row(scn, row, {})
        finally:
            await close_scenario(scn)
    return asyncio.run(go())


def test_efficient_on_high_risk_row_stays_at_floor():
    # Stock rule: US_SSN -> high risk + onprem_only; high floor is frontier.
    snap = make_snapshot(providers={"azure": {"enabled": True}})
    row = make_row("h", "app-a", complexity=0.05, detected_entity_types=["US_SSN"])
    cf, base = _replay(snap, row, "cheap")
    assert cf.tier == "frontier" and cf.provider == "onprem"
    assert (cf.model, cf.cost_usd) == (base.model, base.cost_usd)


def test_capable_on_onprem_only_clamps_to_best_onprem_tier_no_cloud():
    # No on-prem frontier: the best on-prem tier is mid. Cloud is enabled but
    # must stay unreachable.
    snap = make_snapshot(
        onboarded=[onboarded_row("local-cheap", "cheap"), onboarded_row("local-mid", "mid")],
        apps=[app_entry("app-a", residency="onprem_only")],
        providers={"azure": {"enabled": True}})
    row = make_row("c", "app-a", complexity=0.05)
    cf, base = _replay(snap, row, "frontier")
    assert base.tier == "cheap"
    assert cf.outcome == "routed" and cf.tier == "mid" and cf.provider == "onprem"
    assert set(cf.eligible) == {"local-cheap", "local-mid"}


def test_advisor_complexity_lands_on_each_tier_and_default_is_identity():
    th = BASE_SETTINGS.routing.complexity_thresholds
    for t in ("cheap", "mid", "frontier"):
        assert complexity_tier(advisor_complexity(th, t), th) == t
    row = make_row("i", "app-a", complexity=0.5)
    a, b = _replay(make_snapshot(), row, None)
    assert a == b


def test_uncertainty_ranges_and_insufficient_data():
    rows = [make_row(f"r{i}", "app-a", ts=_ts(i % 60), complexity=0.95,
                     attempts=["local-big"], chosen="local-big",
                     **_adv("cheap", agreed=i % 4 == 0)) for i in range(40)]
    plan = UncertaintyPlan(replicates=50, min_rows=30)
    unc = _report(rows, plan=plan)["uncertainty"]
    d = unc["cost_usd_delta"]["totals"]
    assert d["lo"] <= d["point"] <= d["hi"] and d["hi"] < 0
    a = unc["agreement"]["totals"]
    assert 0.0 <= a["lo"] <= a["hi"] <= 1.0
    small = _report(rows[:5], plan=plan)["uncertainty"]
    assert small["agreement"]["totals"]["insufficient_data"] is True


def test_cli_switchyard_report(tmp_path):
    rows = [make_row("x", "app-a", complexity=0.95, attempts=["local-big"], chosen="local-big",
                     **_adv("cheap", agreed=False)), make_row("y", "app-a", ts=_ts(1))]
    exp, snap = tmp_path / "e.ndjson", tmp_path / "s.json"
    exp.write_text("".join(json.dumps({"schema": "routeforge.outcome/v1", "outcome": r,
                                       "feedback": []}) + "\n" for r in rows))
    snap.write_text(json.dumps(make_snapshot()))
    out = tmp_path / "out"
    proc = subprocess.run([sys.executable, str(SCRIPT), "--export", str(exp), "--snapshot",
                           str(snap), "--switchyard-report", "--out", str(out)],
                          capture_output=True, text=True, cwd=ROOT)
    assert proc.returncode == 0, proc.stderr
    doc = json.loads((out / "switchyard.json").read_text())
    assert doc["schema"] == "routeforge.switchyard-report/v1"
    assert (out / "switchyard.md").read_text().startswith("> **Fixed-feature replay")
    bad = subprocess.run([sys.executable, str(SCRIPT), "--export", str(exp), "--snapshot",
                          str(snap), "--switchyard-report", "--shadow-prices", "--out",
                          str(out)], capture_output=True, text=True, cwd=ROOT)
    assert bad.returncode == 2
