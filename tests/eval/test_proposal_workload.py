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
