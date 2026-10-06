"""P3c-2: the scenario comparison (spec §7-§9).

Task 8: the engine -- one shared `current` arm, every scenario replayed
against it. Task 9 (below): `comparison.json` and `report.md`."""
from __future__ import annotations

import asyncio
import copy

import pytest
from hypothesis import HealthCheck, given, settings as hsettings, strategies as st

import app.eval.scenarios as scenarios_mod
from app.eval.proposal import assumptions_block, normalized_edits
from app.eval.scenario import build_scenario, close_scenario
from app.eval.scenarios import SCHEMA, ScenarioError, apply_scenarios, compare
from app.eval.simulate import arm_results, parse_since, run_rows, simulate, summarize
from tests.eval.sim_fixtures import BASE_SETTINGS, app_entry, make_row, make_snapshot
from tests.eval.test_simulate import _TIGHTEN

P = "routeforge.policy-proposal/v1"


def _doc(*scenarios, title="Healthcare on-prem options"):
    return {"schema": SCHEMA, "title": title,
            "scenarios": [{"name": n, "proposal": p} for n, p in scenarios]}


def _compare(records, snap, doc, **kw):
    return asyncio.run(compare(records, snap, doc, base=BASE_SETTINGS, **kw))


def _snap():
    """Two apps; azure enabled so a residency tightening has somewhere to
    move traffic FROM."""
    return make_snapshot(apps=[app_entry("claims"), app_entry("chat")],
                         providers={"azure": {"enabled": True}})


def _rows(days=3):
    """Twelve rows over `days` days, two apps, three complexities."""
    rows = []
    for i in range(12):
        rows.append(make_row(
            f"r{i:02d}", "claims" if i % 2 else "chat",
            complexity=[0.05, 0.6, 1.0][i % 3], prompt_tokens=[200, 1200, 3000][i % 3],
            ts=f"2026-09-{20 + i % days:02d}T12:00:{i:02d}+00:00"))
    return rows


_TRIAD = (
    ("all-onprem", {"apps": {"claims": {"residency": "onprem_only"}}}),
    ("smaller-prompts", {"workload": {"apps": {"claims": {"token_scale": 0.4}}}}),
    ("onprem+2nd-gpu", {"apps": {"claims": {"residency": "onprem_only"}},
                        "endpoints": {"add": [{
                            "slug": "local-mid-2", "provider": "onprem", "tier": "mid",
                            "price": {"input": 0.05, "output": 0.1, "cached_input": None},
                            "latency_ms": 90}]}}),
)


@pytest.mark.parametrize("name,body", _TRIAD)
def test_s2_single_scenario_equals_single_proposal(name, body):            # S2
    """A scenarios file holding one scenario P gives exactly what
    `--proposal P` gives: same rows, same baseline, same numbers."""
    proposal = {"schema": P, **body}
    results, excluded = asyncio.run(simulate(_rows(), _snap(), proposal, base=BASE_SETTINGS))
    sim = summarize(results, excluded, inputs={}, edits=normalized_edits(proposal),
                    assumptions=assumptions_block(proposal))
    cmp = _compare(_rows(), _snap(), _doc((name, body)))
    (entry,) = cmp.scenarios
    assert (cmp.rows, cmp.baseline, cmp.window) == (
        sim["rows"], sim["baseline"], sim["inputs"]["window"])
    assert (entry["name"], entry["edits"], entry["assumptions"]) == (
        name, sim["edits"], sim["assumptions"])
    assert set(entry["summary"]) == set(sim) - {
        "schema", "simulation_id", "inputs", "rows", "baseline", "edits", "assumptions"}
    for key, value in entry["summary"].items():
        assert value == sim[key], key
    totals = entry["summary"]["totals"]
    assert totals["route_changed"] > 0                         # not vacuous


def test_scenarios_come_back_in_file_order_with_shared_rows_and_baseline():
    cmp = _compare(_rows(), _snap(), _doc(*_TRIAD))
    assert [e["name"] for e in cmp.scenarios] == [n for n, _ in _TRIAD]
    assert cmp.rows["simulated"] == 12 and "before_since" in cmp.rows["excluded"]
    assert cmp.window == {"start": "2026-09-20T12:00:00+00:00",
                          "end": "2026-09-22T12:00:11+00:00"}
    assert set(cmp.baseline) == {"rows_compared", "agreement", "warning", "by_app",
                                 "top_disagreements"}
    for entry in cmp.scenarios:
        assert set(entry) == {"name", "edits", "assumptions", "summary"}
        assert set(entry["summary"]) == {"totals", "by_app", "by_workload", "moves",
                                         "new_rejections", "affected_apps", "sources",
                                         "per_day"}


def test_since_reaches_every_scenario():
    cmp = _compare(_rows(), _snap(), _doc(*_TRIAD), since=parse_since("2026-09-21T00:00:00Z"))
    assert cmp.rows["simulated"] == 8 and cmp.rows["excluded"]["before_since"] == 4
    assert all(e["summary"]["totals"]["requests"] == 8 for e in cmp.scenarios)


@hsettings(max_examples=15, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(tightenings=st.lists(_TIGHTEN, min_size=1, max_size=3),
       disable=st.lists(st.sampled_from(["local-cheap", "local-mid", "local-big"]),
                        unique=True, max_size=2),
       etypes=st.lists(st.sampled_from(["US_SSN", "EMAIL_ADDRESS", "PERSON"]),
                       unique=True, max_size=2),
       complexity=st.floats(0.0, 1.0))
def test_s9_scenario_proposals_never_widen_eligibility(tightenings, disable, etypes,  # S9
                                                       complexity):
    """The P3a stricter-never-widens property, per scenario of a generated
    scenarios file -- and `onprem_only` stays absolute in every scenario."""
    snap = make_snapshot(providers={"azure": {"enabled": True}})
    bodies = []
    for tighten in tightenings:
        body = {"apps": {"app-a": tighten}}
        if disable:
            body["endpoints"] = {"disable": disable}
        bodies.append(body)
    doc = _doc(*[(f"s{i}", body) for i, body in enumerate(bodies)])
    rows = [make_row("h", "app-a", detected_entity_types=etypes, complexity=complexity)]
    cmp = _compare(rows, snap, doc)
    for item, entry in zip(apply_scenarios(snap, doc, base=BASE_SETTINGS), cmp.scenarios):
        (r,), _ = asyncio.run(simulate(rows, snap, item.proposal, base=BASE_SETTINGS))
        assert set(r.proposed.eligible) <= set(r.current.eligible)
        if item.proposal["apps"]["app-a"].get("residency") == "onprem_only":
            assert r.proposed.provider in (None, "onprem")
        # a tightening can never newly serve a row, and compare() agrees with the row
        totals = entry["summary"]["totals"]
        assert totals["newly_served"] == 0
        assert totals["newly_rejected"] == int(
            r.current.outcome == "routed" and r.proposed.outcome == "rejected")


# --- the shared current arm -----------------------------------------------------

def test_current_arm_is_simulated_once_per_row(monkeypatch):
    """Three scenarios over 12 rows: 12 `current` simulations, not 36."""
    import app.eval.simulate as sim_mod
    seen: dict[int, int] = {}
    real = sim_mod.simulate_row

    def counting(scn, row, observed):
        seen[id(scn)] = seen.get(id(scn), 0) + 1
        return real(scn, row, observed)
    monkeypatch.setattr(sim_mod, "simulate_row", counting)
    _compare(_rows(), _snap(), _doc(*_TRIAD))
    # four scenarios were built (current + three proposed), each simulated 12 times
    assert sum(seen.values()) == 48
    assert all(count % 12 == 0 for count in seen.values())


def test_precomputed_current_arm_equals_recomputing_it():
    async def both(records):
        current = await build_scenario(_snap(), base=BASE_SETTINGS)
        (item,) = apply_scenarios(_snap(), _doc(_TRIAD[0]), base=BASE_SETTINGS)
        proposed = await build_scenario(item.doc, base=BASE_SETTINGS)
        try:
            plain = run_rows(records, current, proposed)
            pre = run_rows(records, current, proposed,
                           current_results=arm_results(records, current))
        finally:
            await close_scenario(proposed)
            await close_scenario(current)
        return plain, pre
    plain, pre = asyncio.run(both(_rows()))
    assert plain == pre
    # Review Focus 5: two overlapping exports concatenated repeat request ids.
    doubled = _rows() + copy.deepcopy(_rows()[:5])
    plain, pre = asyncio.run(both(doubled))
    assert plain == pre and len(plain[0]) == 17


def test_only_one_proposed_scenario_is_open_at_a_time(monkeypatch):
    opened = {"now": 0, "max": 0}
    real_build, real_close = scenarios_mod.build_scenario, scenarios_mod.close_scenario

    async def build(doc, *, base):
        opened["now"] += 1
        opened["max"] = max(opened["max"], opened["now"])
        return await real_build(doc, base=base)

    async def close(scn):
        opened["now"] -= 1
        await real_close(scn)
    monkeypatch.setattr(scenarios_mod, "build_scenario", build)
    monkeypatch.setattr(scenarios_mod, "close_scenario", close)
    _compare(_rows(), _snap(), _doc(*_TRIAD))
    assert opened == {"now": 0, "max": 2}            # current + one proposed


def test_invalid_scenario_fails_before_any_replay(monkeypatch):           # Review Focus 5
    """The third scenario is invalid: nothing is built, nothing is replayed."""
    async def boom(*_a, **_k):
        raise AssertionError("build_scenario must not be reached")
    monkeypatch.setattr(scenarios_mod, "build_scenario", boom)
    doc = _doc(_TRIAD[0], _TRIAD[1], ("bad", {"workload": {"cached_share": 7}}))
    with pytest.raises(ScenarioError) as exc:
        _compare(_rows(), _snap(), doc)
    assert exc.value.where == "scenarios[2](bad).proposal.workload.cached_share"


def test_scenarios_are_closed_when_a_replay_raises(monkeypatch):
    closed = []
    real_close = scenarios_mod.close_scenario

    async def close(scn):
        closed.append(scn)
        await real_close(scn)

    def boom(*_a, **_k):
        raise RuntimeError("replay failed")
    monkeypatch.setattr(scenarios_mod, "close_scenario", close)
    monkeypatch.setattr(scenarios_mod, "run_rows", boom)
    with pytest.raises(RuntimeError, match="replay failed"):
        _compare(_rows(), _snap(), _doc(*_TRIAD))
    assert len(closed) == 2                          # the one proposed scenario, then current
