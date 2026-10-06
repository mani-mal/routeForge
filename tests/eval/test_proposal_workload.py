"""P3c-2: the `workload` and `latency_ms` proposal change types (spec §4)."""
from __future__ import annotations

import copy
import json

import pytest

from app.eval.proposal import (ALL_APPS, ProposalError, apply_proposal, assumptions_block,
                               load_proposal, normalized_edits)
from app.eval.scenario import SnapshotError, load_snapshot
from tests.eval.sim_fixtures import BASE_SETTINGS, app_entry, make_snapshot

P = "routeforge.policy-proposal/v1"


def _apply(prop, snap=None):
    return apply_proposal(snap or make_snapshot(), prop, base=BASE_SETTINGS)


# --- Task 3: validation and the assumptions block (spec §4.1, §4.3) ----------

def test_workload_writes_the_assumptions_block():
    snap = make_snapshot(apps=[app_entry("app-a"), app_entry("app-b")])
    out = _apply({"schema": P, "workload": {
        "token_scale": 0.6, "cached_share": 0.25,
        "apps": {"app-b": {"token_scale": 2, "cached_share": 0.5}}}}, snap)
    assert out["assumptions"] == {
        "token_scale": {ALL_APPS: 0.6, "app-b": 2.0},
        "cached_share": {ALL_APPS: 0.25, "app-b": 0.5},
        "latency_ms": {}}
    # nothing else moved, and the input was not mutated
    del out["assumptions"]
    assert out == snap and "assumptions" not in snap


def test_no_workload_means_no_block():
    assert "assumptions" not in _apply({"schema": P})
    assert "assumptions" not in _apply({"schema": P, "workload": {}})
    assert "assumptions" not in _apply({"schema": P, "workload": {"apps": {"app-a": {}}}})
    assert assumptions_block({"schema": P}) is None


def test_per_app_only_and_global_only_blocks():
    out = _apply({"schema": P, "workload": {"apps": {"app-a": {"cached_share": 1}}}})
    assert out["assumptions"] == {"token_scale": {}, "cached_share": {"app-a": 1.0},
                                  "latency_ms": {}}
    out = _apply({"schema": P, "workload": {"token_scale": 10}})
    assert out["assumptions"]["token_scale"] == {ALL_APPS: 10.0}


def test_block_matches_assumptions_block_and_edits_echo_workload():
    prop = {"schema": P, "title": "t", "workload": {"token_scale": 0.5}}
    assert _apply(prop)["assumptions"] == assumptions_block(prop)
    assert normalized_edits(prop) == {"workload": {"token_scale": 0.5}}


@pytest.mark.parametrize("workload,where", [
    ({"token_scale": 0}, "workload.token_scale"),
    ({"token_scale": -1}, "workload.token_scale"),
    ({"token_scale": 10.0001}, "workload.token_scale"),
    ({"token_scale": "0.5"}, "workload.token_scale"),
    ({"token_scale": True}, "workload.token_scale"),          # a bool is not a number
    ({"token_scale": None}, "workload.token_scale"),
    ({"token_scale": float("nan")}, "workload.token_scale"),
    ({"token_scale": float("inf")}, "workload.token_scale"),
    ({"cached_share": -0.01}, "workload.cached_share"),
    ({"cached_share": 1.01}, "workload.cached_share"),
    ({"cached_share": False}, "workload.cached_share"),
    ({"cached_share": [0.5]}, "workload.cached_share"),
    ({"cached_share": float("nan")}, "workload.cached_share"),
    ({"tokenscale": 0.5}, "workload.tokenscale"),              # unknown key
    ({"apps": []}, "workload.apps"),
    ({"apps": {"app-a": 0.5}}, "workload.apps.app-a"),
    ({"apps": {"app-a": {"scale": 0.5}}}, "workload.apps.app-a.scale"),
    ({"apps": {"app-a": {"token_scale": 11}}}, "workload.apps.app-a.token_scale"),
    ({"apps": {"app-a": {"cached_share": 2}}}, "workload.apps.app-a.cached_share"),
    ({"apps": {"ghost": {"token_scale": 0.5}}}, "workload.apps.ghost"),
])
def test_workload_errors_name_the_dotted_path(workload, where):           # Review Focus 2
    with pytest.raises(ProposalError) as exc:
        _apply({"schema": P, "workload": workload})
    assert exc.value.where == where, str(exc.value)


def test_workload_not_an_object_is_named():
    with pytest.raises(ProposalError) as exc:
        _apply({"schema": P, "workload": 0.5})
    assert exc.value.where == "workload"


def test_boundary_values_are_accepted():
    _apply({"schema": P, "workload": {"token_scale": 10, "cached_share": 0}})
    _apply({"schema": P, "workload": {"token_scale": 1e-9, "cached_share": 1}})


def test_load_proposal_rejects_unknown_workload_key(tmp_path):
    path = tmp_path / "p.json"
    path.write_text(json.dumps({"schema": P, "workload": {"cache_share": 0.4}}))
    with pytest.raises(ProposalError) as exc:
        load_proposal(str(path))
    assert exc.value.where == "workload.cache_share"


def test_json_nan_literal_is_refused(tmp_path):                           # Review Focus 2
    """`json.loads` accepts the bare literal NaN; the proposal must not."""
    path = tmp_path / "p.json"
    path.write_text('{"schema": "%s", "workload": {"token_scale": NaN}}' % P)
    doc, _sha = load_proposal(str(path))
    with pytest.raises(ProposalError) as exc:
        _apply(doc)
    assert exc.value.where == "workload.token_scale"


def test_snapshot_file_with_assumptions_is_a_load_error(tmp_path):
    snap = make_snapshot()
    snap["assumptions"] = {"token_scale": {"*": 0.1}, "cached_share": {}, "latency_ms": {}}
    path = tmp_path / "s.json"
    path.write_text(json.dumps(snap))
    with pytest.raises(SnapshotError) as exc:
        load_snapshot(str(path))
    assert exc.value.where == "assumptions"


def test_applying_a_proposal_twice_is_refused():
    once = _apply({"schema": P, "workload": {"token_scale": 0.5}})
    before = copy.deepcopy(once)
    with pytest.raises(SnapshotError) as exc:
        _apply({"schema": P, "workload": {"cached_share": 0.5}}, once)
    assert exc.value.where == "assumptions" and once == before


# --- Task 4: replay applies token_scale and cached_share (spec §4.1) ---------

import asyncio  # noqa: E402
from pathlib import Path  # noqa: E402

from app.eval.simulate import (CACHED_SHARE_NOTE, render_report, scaled_request,  # noqa: E402
                               simulate, summarize)
from tests.eval.sim_fixtures import make_row, onboarded_row  # noqa: E402

_INPUTS = {"export_sha256": "e", "snapshot_sha256": "s", "proposal_sha256": "p",
           "proposal_title": "t", "routing_policy_version": "v",
           "registry_version": "r", "snapshot_generated_at": "2026-09-24T00:00:00Z"}


def _run(records, snap, prop):
    return asyncio.run(simulate(records, snap, prop, base=BASE_SETTINGS))


def _mixed_rows():
    """Small, mid and saturated complexity; a failure row that recorded no
    prompt tokens; a long prompt."""
    return [make_row("small", "app-a", complexity=0.1, prompt_tokens=200),
            make_row("mid", "app-a", complexity=0.6, prompt_tokens=1200),
            make_row("sat", "app-a", complexity=1.0, prompt_tokens=3000),
            make_row("zero", "app-a", complexity=0.2, prompt_tokens=0,
                     status="backend_failed"),
            make_row("long", "app-a", complexity=1.0, prompt_tokens=9000)]


def test_complexity_literal_matches_the_pipeline():
    """`scaled_request` mirrors `app/pipeline.py`'s complexity formula. If
    the pipeline's divisor ever changes, this fails before a replay lies."""
    root = Path(__file__).resolve().parents[2]
    assert "min(1.0, comp.tokens_after / 2000)" in (root / "app" / "pipeline.py").read_text()


@pytest.mark.parametrize("args,expected", [
    ((200, 0.1, 1.0), (200, 0.1, False)),            # identity, exactly
    ((0, 1.5, 1.0), (0, 1.5, False)),                # identity even on odd recorded values
    ((200, 0.1, 0.5), (100, 0.05, False)),           # linear below saturation: exact
    ((200, 0.4, 5.0), (1000, 1.0, False)),           # capped at 1
    ((1, 0.0005, 0.1), (1, 0.00005, False)),         # tokens floor at 1
    ((0, 0.2, 0.5), (0, 0.1, False)),                # an unrecorded size stays 0
    ((0, 0.2, 5.0), (0, 1.0, False)),
    ((3000, 1.0, 0.5), (1500, 0.75, True)),          # saturated, scaled down: estimate
    ((9000, 1.0, 0.5), (4500, 1.0, True)),           # still saturated, still an estimate
    ((500, 1.0, 0.9), (450, 0.9, True)),             # never below `scale` (Review Focus 2)
    ((500, 1.0, 2.0), (1000, 1.0, False)),           # scaled up: exactly 1, not an estimate
])
def test_scaled_request(args, expected):                                   # Review Focus 2
    tokens, complexity, estimated = scaled_request(*args)
    assert (tokens, estimated) == (expected[0], expected[2])
    assert complexity == pytest.approx(expected[1])


def test_s4_neutral_workload_changes_nothing():                            # S4 (workload)
    rows = _mixed_rows() + [make_row("odd", "app-a", complexity=1.5)]
    results, excluded = _run(rows, make_snapshot(), {"schema": P, "workload": {
        "token_scale": 1, "cached_share": 0,
        "apps": {"app-a": {"token_scale": 1.0, "cached_share": 0.0}}}})
    assert len(results) == len(rows)
    for r in results:
        assert r.proposed == r.current
    sim = summarize(results, excluded, inputs=_INPUTS, edits={})
    assert sim["totals"]["cost_usd"]["delta"] == 0
    assert sim["assumptions"]["saturated_complexity_estimated"] == 0


def test_s5_cost_monotone_in_token_scale():                                # S5
    """One eligible endpoint, so routing is fixed and only pricing moves."""
    snap = make_snapshot(onboarded=[onboarded_row("local-mid", "mid")])
    rows = _mixed_rows()
    costs = []
    for scale in (0.01, 0.1, 0.5, 1, 1.5, 3, 10):
        results, _ = _run(rows, snap, {"schema": P, "workload": {"token_scale": scale}})
        assert all(r.proposed.model == "local-mid" for r in results)
        costs.append({r.row["request_id"]: r.proposed.cost_usd for r in results})
    for lower, higher in zip(costs, costs[1:]):
        for request_id in lower:
            assert lower[request_id] <= higher[request_id], request_id
    assert costs[0]["sat"] < costs[-1]["sat"]                # and it really moves
    assert costs[0]["zero"] == costs[-1]["zero"]             # an unrecorded size never does


def test_token_scale_moves_tier_and_counts_estimated_rows():
    """At 0.4x the mid-complexity row drops to the cheap tier and the
    saturated 3000-token row to mid (max(0.4, 1200/2000) = 0.6); the
    9000-token row stays saturated. Both saturated rows are estimates."""
    results, excluded = _run(_mixed_rows(), make_snapshot(),
                             {"schema": P, "workload": {"token_scale": 0.4}})
    by = {r.row["request_id"]: r for r in results}
    assert (by["mid"].current.tier, by["mid"].proposed.tier) == ("mid", "cheap")
    assert (by["sat"].current.tier, by["sat"].proposed.tier) == ("frontier", "mid")
    assert (by["long"].current.tier, by["long"].proposed.tier) == ("frontier", "frontier")
    assert by["sat"].proposed.complexity_estimated and by["long"].proposed.complexity_estimated
    assert not by["mid"].proposed.complexity_estimated
    assert not any(r.current.complexity_estimated for r in results)   # current: no assumptions
    sim = summarize(results, excluded, inputs=_INPUTS, edits={},
                    assumptions=assumptions_block({"workload": {"token_scale": 0.4}}))
    assert sim["assumptions"] == {"token_scale": {ALL_APPS: 0.4}, "cached_share": {},
                                  "latency_ms": {}, "saturated_complexity_estimated": 2}
    report = render_report(sim)
    assert "## Assumptions" in report and "- token_scale: *=0.4" in report
    assert "estimated from scaled prompt tokens: 2" in report


def test_per_app_token_scale_overrides_the_global_one():
    snap = make_snapshot(apps=[app_entry("app-a"), app_entry("app-b")])
    rows = [make_row("a", "app-a", prompt_tokens=1000), make_row("b", "app-b", prompt_tokens=1000)]
    results, _ = _run(rows, snap, {"schema": P, "workload": {
        "token_scale": 2, "apps": {"app-b": {"token_scale": 0.5}}}})
    by = {r.row["request_id"]: r for r in results}
    assert by["a"].proposed.cost_usd > by["a"].current.cost_usd      # global 2x
    assert by["b"].proposed.cost_usd < by["b"].current.cost_usd      # its own 0.5x


def test_s6_cached_share_never_raises_cost():                              # S6
    snap = make_snapshot(onboarded=[onboarded_row("local-mid", "mid", price_cached_input=0.05)])
    rows = _mixed_rows()
    costs = []
    for share in (0, 0.25, 0.5, 1):
        results, _ = _run(rows, snap, {"schema": P, "workload": {"cached_share": share}})
        for r in results:
            assert r.proposed.model == r.current.model           # a share never moves a route
        costs.append({r.row["request_id"]: r.proposed.cost_usd for r in results})
    for lower_share, higher_share in zip(costs, costs[1:]):
        for request_id in lower_share:
            assert higher_share[request_id] <= lower_share[request_id], request_id
    # 3000 prompt tokens, all cached: (3000 * 0.05 + 100 * 1.0) / 1e6
    assert costs[-1]["sat"] == pytest.approx((3000 * 0.05 + 100 * 1.0) / 1_000_000)
    assert costs[-1]["sat"] < costs[0]["sat"]


def test_s6_cached_share_without_declared_price_is_neutral():              # S6
    snap = make_snapshot(onboarded=[onboarded_row("local-mid", "mid")])   # cached price: None
    results, excluded = _run(_mixed_rows(), snap,
                             {"schema": P, "workload": {"cached_share": 1}})
    for r in results:
        assert r.proposed.cost_usd == r.current.cost_usd
    sim = summarize(results, excluded, inputs=_INPUTS, edits={},
                    assumptions=assumptions_block({"workload": {"cached_share": 1}}))
    assert CACHED_SHARE_NOTE in render_report(sim)


def test_report_has_no_assumptions_section_without_assumptions():
    results, excluded = _run(_mixed_rows(), make_snapshot(), {"schema": P})
    sim = summarize(results, excluded, inputs=_INPUTS, edits={})
    assert sim["assumptions"] == {"token_scale": {}, "cached_share": {}, "latency_ms": {},
                                  "saturated_complexity_estimated": 0}
    assert "## Assumptions" not in render_report(sim)


def test_malformed_assumptions_block_is_a_named_snapshot_error():
    from app.eval.scenario import build_scenario
    for block, where in (([], "assumptions"),
                         ({"token_scale": {}, "bogus": {}}, "assumptions.bogus"),
                         ({"token_scale": {"*": "big"}}, "assumptions.token_scale.*"),
                         ({"cached_share": 3}, "assumptions.cached_share")):
        doc = make_snapshot()
        doc["assumptions"] = block
        with pytest.raises(SnapshotError) as exc:
            asyncio.run(build_scenario(doc, base=BASE_SETTINGS))
        assert exc.value.where == where
