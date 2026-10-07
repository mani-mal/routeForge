"""P3c-3: ranges on shadow prices (spec §5.3, §9.3, §10.3; U1, U4, U7, U11)."""
from __future__ import annotations

import asyncio
import json

from app.eval.shadow import _PROTECTION_LABEL, render_shadow_section, shadow_prices
from app.eval.simulate import simulate
from app.eval.uncertainty import UncertaintyPlan, results_block
from tests.eval.sim_fixtures import (BASE_SETTINGS, app_entry, make_row, make_snapshot,
                                     onboarded_row)
from tests.eval.test_uncertainty import BANNED, _keys, _leaves

P = "routeforge.policy-proposal/v1"
PLAN = UncertaintyPlan(replicates=150)


def _run(records, doc, **kw):
    return asyncio.run(shadow_prices(records, doc, base=BASE_SETTINGS, **kw))


def _rows(app: str, n: int, days: int = 3) -> list[dict]:
    return [make_row(f"{app}-{i:03d}", app, complexity=[0.05, 0.6, 1.0][i % 3],
                     prompt_tokens=[200, 1200, 3000][i % 3] + 5 * i,
                     ts=f"2026-09-{20 + i % days:02d}T12:{i % 60:02d}:00+00:00")
            for i in range(n)]


def _snap():
    """On-prem has a cheap and a mid endpoint; the frontier tier is cloud
    only. `big` is pinned to the mid tier (relaxing it saves money: an
    operational constraint). `safe` is on-prem only, so its largest requests
    are held at the mid tier (relaxing it moves them to the cloud: a
    security constraint). `few` has too few rows for a range."""
    return make_snapshot(
        onboarded=[onboarded_row("local-cheap", "cheap"), onboarded_row("local-mid", "mid")],
        apps=[app_entry("big", allowed_tiers=["mid"]),
              app_entry("few", allowed_tiers=["mid"]),
              app_entry("safe", residency="onprem_only")],
        providers={"azure": {"enabled": True}})


def _records():
    return _rows("big", 60) + _rows("few", 6) + _rows("safe", 45)


def _find(entries, requirement, app):
    return next(e for e in entries
                if e["requirement"] == requirement and e["scope"] == {"app": app})


def test_u1_a_plan_adds_one_key_to_the_block():                            # U1
    plain = _run(_records(), _snap())
    ranged = _run(_records(), _snap(), uncertainty=PLAN)
    assert "uncertainty" not in plain
    assert set(ranged) == set(plain) | {"uncertainty"}
    without = {k: v for k, v in ranged.items() if k != "uncertainty"}
    assert json.dumps(without, sort_keys=True) == json.dumps(plain, sort_keys=True)


def test_ranges_are_parallel_to_constraints_and_about_the_same_figures():
    block = _run(_records(), _snap(), uncertainty=PLAN)
    ranges = block["uncertainty"]
    assert ranges["interval"] == 0.9
    assert len(ranges["constraints"]) == len(block["constraints"]) >= 3
    for entry, r in zip(block["constraints"], ranges["constraints"]):
        assert (r["requirement"], r["scope"], r["security"], r["n"]) == (
            entry["requirement"], entry["scope"], entry["security"], entry["rows_in_scope"])
        if r.get("insufficient_data"):
            continue
        assert r["route_changed"]["point"] == entry["route_changed"]
        assert r["rejections_avoided"]["point"] == entry["rejections_avoided"]
        assert r["newly_rejected"]["point"] == entry["newly_rejected"]
        assert r["cost_usd_both_routed_delta"]["point"] == entry["cost_usd_both_routed"]["delta"]
        assert r["cost_saved_per_day"]["point"] == entry["cost_saved_per_day"]
    big = _find(ranges["constraints"], "allowed_tiers", "big")
    assert big["n"] == 60
    saved = big["cost_saved_per_day"]
    assert 0 < saved["lo"] < saved["point"] < saved["hi"]
    moved = big["route_changed"]
    assert moved["point"] == 20 and moved["lo"] < 20 < moved["hi"]


def test_u7_a_relaxation_with_few_rows_in_scope_gets_no_range():           # U7
    block = _run(_records(), _snap(), uncertainty=PLAN)
    few = _find(block["uncertainty"]["constraints"], "allowed_tiers", "few")
    assert few == {"requirement": "allowed_tiers", "scope": {"app": "few"},
                   "security": False, "n": 6, "insufficient_data": True}
    # The relaxation itself is still listed with its point figures.
    assert _find(block["constraints"], "allowed_tiers", "few")["route_changed"] == 2


def test_cost_saved_per_day_is_null_under_a_one_day_window():
    rows = _rows("big", 60, days=1)
    block = _run(rows, _snap(), uncertainty=PLAN)
    big = _find(block["uncertainty"]["constraints"], "allowed_tiers", "big")
    assert big["cost_saved_per_day"] is None
    assert big["cost_usd_both_routed_delta"]["lo"] < big["cost_usd_both_routed_delta"]["hi"] < 0


def test_u4_a_relaxation_is_resampled_with_the_same_draws_as_a_proposal():  # U4
    """Relaxing `allowed_tiers` for `big` is the proposal "set big's
    allowed_tiers to null". Its range must be that proposal's `big` bucket,
    figure for figure: the same rows, drawn the same way."""
    records = _records()
    block = _run(records, _snap(), uncertainty=PLAN)
    big = _find(block["uncertainty"]["constraints"], "allowed_tiers", "big")
    results, _excluded = asyncio.run(simulate(
        records, _snap(), {"schema": P, "apps": {"big": {"allowed_tiers": None}}},
        base=BASE_SETTINGS))
    bucket = results_block(results, PLAN)["by_app"]["big"]
    assert big["cost_usd_both_routed_delta"] == bucket["cost_usd_both_routed"]["delta"]
    assert big["route_changed"] == bucket["route_changed"]
    # The relaxation's leaf also carries `zero_bound` (final review F3: a
    # relaxation's "0 newly rejected" is read as a safety claim); in a bucket
    # that bound is on the share. The RANGE is the same, value for value.
    assert big["newly_rejected"] == {**bucket["newly_rejected"],
                                     "zero_bound": bucket["newly_rejected_share"]["zero_bound"]}
    assert big["rejections_avoided"] == bucket["newly_served"]


def test_u2_shadow_ranges_are_deterministic():                             # U2
    first = json.dumps(_run(_records(), _snap(), uncertainty=PLAN), sort_keys=True)
    assert json.dumps(_run(_records(), _snap(), uncertainty=PLAN), sort_keys=True) == first


def test_u3_every_shadow_point_is_inside_its_range_or_flagged():           # U3
    block = _run(_records(), _snap(), uncertainty=PLAN)
    nodes = list(_leaves(block["uncertainty"]))
    assert nodes
    for path, node in nodes:
        assert node["lo"] <= node["point"] <= node["hi"] or node.get("point_outside"), path


def test_u11_a_security_range_keeps_the_protection_label():                # U11
    """A range on the cost of a protection is still the cost of a
    protection: the row is labelled, in the ranges table too, and nothing in
    the block ranks or recommends."""
    block = _run(_records(), _snap(), uncertainty=PLAN)
    safe = _find(block["uncertainty"]["constraints"], "residency", "safe")
    assert safe["security"] is True
    assert not [k for k in _keys(block) if any(word in k.lower() for word in BANNED)]
    report = render_shadow_section(block)
    section = report[report.index("### Ranges"):]
    line = next(l for l in section.split("\n") if l.startswith("| residency | app:safe |"))
    assert line.endswith(f"| {_PROTECTION_LABEL} |")
    operational = next(l for l in section.split("\n")
                       if l.startswith("| allowed_tiers | app:big |"))
    assert operational.endswith("|  |") and _PROTECTION_LABEL not in operational
    assert "sampling variability only" in section


def test_report_ranges_table():
    block = _run(_records(), _snap(), uncertainty=PLAN)
    report = render_shadow_section(block)
    assert report.index("### Cost of protection") < report.index("### Ranges") \
        < report.index("Non-binding:")
    section = report[report.index("### Ranges"):]
    assert "`[lo, hi]` is the 90% range of the figure before it" in section
    big = _find(block["uncertainty"]["constraints"], "allowed_tiers", "big")["cost_saved_per_day"]
    assert f"/day [{big['lo']:.6g}, {big['hi']:.6g}]" in section
    few = next(l for l in section.split("\n") if l.startswith("| allowed_tiers | app:few |"))
    assert "| insufficient data (n = 6) |" in few and "[" not in few
    # One row per binding relaxation, in the block's order.
    rows = [l for l in section.split("\n") if l.startswith("| ") and "requirement" not in l]
    assert len(rows) == len(block["constraints"])


def test_report_without_a_plan_is_unchanged():
    report = render_shadow_section(_run(_records(), _snap()))
    assert "### Ranges" not in report and "[" not in report


def test_no_binding_relaxation_gives_an_empty_ranges_list():
    snap = make_snapshot(apps=[app_entry("big")])
    block = _run(_rows("big", 40), snap, uncertainty=PLAN)
    assert block["constraints"] == [] and block["uncertainty"] == {
        "interval": 0.9, "constraints": [], "points_outside_interval": 0}
    assert "### Ranges\n\n" in render_shadow_section(block)
