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
                                 "top_disagreements", "overflow_contradicted",
                                 # B60: outcome-level agreement, shared like the rest
                                 "outcome_rows_compared", "outcome_agreement",
                                 "outcome_warning", "recorded_rejected",
                                 "recorded_rejected_but_routed",
                                 "recorded_routed_but_rejected", "outcome_by_app"}
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
        # A tightening can never newly serve a row here because this fixture's
        # onboarded rows all declare no context_window (no overflow is
        # possible in this test) -- in general, removing an overflowing
        # endpoint from eligibility can let the bounded re-select loop (spec
        # §5) reach one that fits, newly serving a row a wider eligible set
        # would have overflowed on. compare() agrees with the row either way.
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


# --- Task 9: comparison.json and report.md (spec §8) ---------------------------

import json  # noqa: E402

from app.eval.scenarios import (COMPARISON_SCHEMA, LIKE_FOR_LIKE_NOTE,  # noqa: E402
                                NO_RANKING_NOTE, comparison_doc, render_comparison)
from app.eval.simulate import REPORT_CAVEAT  # noqa: E402

_INPUTS = {"export_sha256": "e", "snapshot_sha256": "s", "scenarios_sha256": "c",
           "title": "Healthcare on-prem options", "routing_policy_version": "v",
           "registry_version": "r", "snapshot_generated_at": "2026-09-24T00:00:00Z",
           "serving_policy": {}, "since": None}

_TABLE_KEYS = {"name", "cost_per_day_delta_usd", "cost_delta_usd", "route_changed",
               "newly_rejected", "newly_served", "latency_p50_delta_ms",
               "latency_p95_delta_ms", "quality_mean_delta", "onprem_share_current",
               "onprem_share_proposed", "overflow_hops", "affected_apps"}


def _document(records, snap, doc, inputs=None, **kw):
    return comparison_doc(_compare(records, snap, doc, **kw), inputs=inputs or _INPUTS)


def test_s1_empty_scenario_is_a_noop():                                    # S1
    doc = _document(_rows(), _snap(), _doc(("empty", {}), ("also-empty", {"title": "x"})))
    assert len(doc["table"]) == 2
    for row in doc["table"]:
        assert row["cost_per_day_delta_usd"] == 0 and row["cost_delta_usd"] == 0
        assert (row["route_changed"], row["newly_rejected"], row["newly_served"]) == (0, 0, 0)
        assert row["latency_p50_delta_ms"] == 0 and row["latency_p95_delta_ms"] == 0
        assert row["quality_mean_delta"] == 0
        assert row["onprem_share_current"] == row["onprem_share_proposed"]
        assert row["overflow_hops"] == 0 and row["affected_apps"] == []
    for entry in doc["scenarios"]:
        assert entry["edits"] == {} and entry["summary"]["moves"] == []
        assert entry["summary"]["totals"]["cost_usd"]["delta"] == 0


def test_s3_order_independence():                                          # S3
    """Permuting the scenarios permutes the table rows and changes nothing
    else: each scenario's block, the rows and the baseline are identical."""
    forward = _document(_rows(), _snap(), _doc(*_TRIAD))
    backward = _document(_rows(), _snap(), _doc(*reversed(_TRIAD)))
    assert [e["name"] for e in forward["scenarios"]] == [n for n, _ in _TRIAD]
    assert [e["name"] for e in backward["scenarios"]] == [n for n, _ in reversed(_TRIAD)]
    assert {e["name"]: e for e in forward["scenarios"]} == \
           {e["name"]: e for e in backward["scenarios"]}
    assert forward["table"] == list(reversed(backward["table"]))
    for key in ("rows", "baseline", "inputs", "comparison_id"):
        assert forward[key] == backward[key], key


def _keys(value):
    if isinstance(value, dict):
        for key, inner in value.items():
            yield key
            yield from _keys(inner)
    elif isinstance(value, list):
        for inner in value:
            yield from _keys(inner)


def test_s8_no_ranking_and_file_order():                                   # S8
    """The file is not in cost order, and the output must not put it in one.
    No field anywhere ranks, scores or recommends."""
    doc = _document(_rows(), _snap(), _doc(*_TRIAD))
    assert [row["name"] for row in doc["table"]] == [n for n, _ in _TRIAD]
    assert [e["name"] for e in doc["scenarios"]] == [n for n, _ in _TRIAD]
    costs = [row["cost_delta_usd"] for row in doc["table"]]
    assert costs != sorted(costs) and costs != sorted(costs, reverse=True)   # not sorted by cost
    for row in doc["table"]:
        assert set(row) == _TABLE_KEYS
    banned = ("rank", "recommend", "best", "winner", "prefer", "score")
    assert not [k for k in _keys(doc) if any(word in k.lower() for word in banned)]
    report = render_comparison(doc)
    assert NO_RANKING_NOTE in report
    positions = [report.index(f"| {n} |") for n, _ in _TRIAD]
    assert positions == sorted(positions)
    sections = [report.index(f"## Scenario: {n}") for n, _ in _TRIAD]
    assert sections == sorted(sections)


def test_comparison_doc_shape():
    doc = _document(_rows(), _snap(), _doc(*_TRIAD))
    assert set(doc) == {"schema", "comparison_id", "inputs", "rows", "baseline",
                        "scenarios", "table"}
    assert doc["schema"] == COMPARISON_SCHEMA and len(doc["comparison_id"]) == 16
    assert set(doc["inputs"]) == set(_INPUTS) | {"window"}
    assert doc["inputs"]["window"] == {"start": "2026-09-20T12:00:00+00:00",
                                       "end": "2026-09-22T12:00:11+00:00"}
    assert doc["rows"]["simulated"] == 12
    assert "agreement" in doc["baseline"]                      # lifted out of every scenario
    assert all("baseline" not in e["summary"] for e in doc["scenarios"])


def test_table_values_and_like_for_like_cost():
    """The table reads the scenario's own totals. Its cost columns are the
    both-routed (like-for-like) delta, so a scenario that newly rejects rows
    does not look cheaper for it."""
    snap = make_snapshot(apps=[app_entry("claims", allowed_models=["local-mid"]),
                               app_entry("chat")])
    doc = _document(_rows(), snap, _doc(("drop-mid", {"endpoints": {"remove": ["local-mid"]}})))
    (row,), (entry,) = doc["table"], doc["scenarios"]
    totals = entry["summary"]["totals"]
    assert row["newly_rejected"] == totals["newly_rejected"] == 6
    assert totals["cost_usd"]["delta"] < 0                     # the misleading "saving"
    assert row["cost_delta_usd"] == totals["cost_usd_both_routed"]["delta"]
    assert row["cost_delta_usd"] != totals["cost_usd"]["delta"]    # not the all-routed delta
    assert row["affected_apps"] == entry["summary"]["affected_apps"]
    assert row["overflow_hops"] == totals["overflow_hops"]["proposed"]
    assert row["onprem_share_proposed"] == totals["onprem_share"]["proposed"]
    assert row["latency_p95_delta_ms"] == totals["latency_ms"]["p95"]["delta"]


def test_cost_per_day_divides_the_like_for_like_delta_by_the_window():
    """The window is 2 days and 11 seconds: 2.000127 days."""
    doc = _document(_rows(), _snap(), _doc(*_TRIAD))
    for row in doc["table"]:
        assert row["cost_delta_usd"] != 0
        assert row["cost_per_day_delta_usd"] == pytest.approx(
            row["cost_delta_usd"] / (2 + 11 / 86400), abs=1e-6)


def test_cost_per_day_is_null_under_a_one_day_window():
    doc = _document(_rows(days=1), _snap(), _doc(*_TRIAD))
    assert all(row["cost_per_day_delta_usd"] is None for row in doc["table"])
    assert "| all-onprem | n/a |" in render_comparison(doc)


def test_comparison_is_deterministic_and_id_follows_inputs():
    one = _document(_rows(), _snap(), _doc(*_TRIAD))
    two = _document(_rows(), _snap(), _doc(*_TRIAD))
    assert json.dumps(one, sort_keys=True) == json.dumps(two, sort_keys=True)
    assert render_comparison(one) == render_comparison(two)
    other = _document(_rows(), _snap(), _doc(*_TRIAD),
                      inputs={**_INPUTS, "since": "2026-09-01T00:00:00+00:00"})
    assert other["comparison_id"] != one["comparison_id"]


# --- report.md -------------------------------------------------------------------

def test_report_structure():
    report = render_comparison(_document(_rows(), _snap(), _doc(*_TRIAD)))
    assert report.startswith("> " + REPORT_CAVEAT.split("\n")[0])
    assert "plus deterministic context-window overflow" in report
    order = ["# Scenario comparison: Healthcare on-prem options", "## Baseline agreement",
             "## Comparison", NO_RANKING_NOTE, LIKE_FOR_LIKE_NOTE, "## Assumptions",
             "## Scenario: all-onprem", "## Scenario: smaller-prompts",
             "## Scenario: onprem+2nd-gpu", "## Excluded rows"]
    positions = [report.index(marker) for marker in order]
    assert positions == sorted(positions)
    assert report.count("## Comparison") == 1                  # ONE table
    assert "- **smaller-prompts** -- token_scale: claims=0.4; rows whose saturated" in report
    assert "- **onprem+2nd-gpu** -- latency_ms: local-mid-2=90" in report
    assert "- **all-onprem**" not in report                    # it assumes nothing
    assert "### Moves" in report and "### New rejections" in report
    assert "Affected apps: claims" in report


def test_report_names_the_since_cut():
    since = parse_since("2026-09-21T00:00:00Z")
    doc = _document(_rows(), _snap(), _doc(*_TRIAD), since=since,
                    inputs={**_INPUTS, "since": since.isoformat()})
    report = render_comparison(doc)
    assert "Rows before `--since 2026-09-21T00:00:00+00:00` excluded: 4." in report
    assert "| before_since | 4 |" in report


def test_report_agreement_banner_warns_and_marks_apps():
    """`chat` rows record the model replay picks; `claims` rows record one it
    never would. Overall 50%: a warning, and `claims` is marked."""
    rows = []
    for row in _rows():
        if row["application_id"] == "claims":
            row.update(attempts=["local-big"], chosen="local-big", complexity=0.05)
        else:
            row.update(complexity=0.05, attempts=["local-cheap"], chosen="local-cheap")
        rows.append(row)
    doc = _document(rows, _snap(), _doc(("empty", {})))
    assert doc["baseline"]["by_app"] == {"chat": 1.0, "claims": 0.0}
    assert doc["baseline"]["warning"] is True
    report = render_comparison(doc)
    assert "**Baseline agreement: 50.0%** over 12 compared rows." in report
    assert "⚠ **Warning:** below the 90% threshold" in report
    assert "Per app: chat 100.0% · claims 0.0% ⚠" in report
    assert "Rows before `--since" not in report


def test_report_agreement_banner_without_comparable_rows():
    rows = [make_row("r", "chat", status="backend_failed")]
    doc = _document(rows, _snap(), _doc(("empty", {})))
    assert doc["baseline"]["agreement"] is None
    assert "**Baseline agreement: n/a**" in render_comparison(doc)


def test_report_cached_share_note_and_no_title():
    doc = _document(_rows(), _snap(),
                    _doc(("cached", {"workload": {"cached_share": 0.5}}), title=None),
                    inputs={**_INPUTS, "title": None})
    report = render_comparison(doc)
    assert "# Scenario comparison\n" in report
    assert "- **cached** -- cached_share: *=0.5" in report
    assert "`cached_share` is an assumption" in report


def test_report_has_no_assumptions_section_when_nothing_is_assumed():
    report = render_comparison(_document(_rows(), _snap(), _doc(("empty", {}))))
    assert "## Assumptions" not in report
