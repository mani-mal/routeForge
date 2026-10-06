"""P3c-2 Task 7: the `routeforge.policy-scenarios/v1` file (spec §3)."""
from __future__ import annotations

import copy
import json

import pytest

from app.eval.scenario import SnapshotError
from app.eval.scenarios import (MAX_SCENARIOS, SCHEMA, ScenarioError, apply_scenarios,
                                load_scenarios, validate_scenarios)
from tests.eval.sim_fixtures import BASE_SETTINGS, make_snapshot

P = "routeforge.policy-proposal/v1"


def _doc(*scenarios, **top):
    return {"schema": SCHEMA, "title": "t",
            "scenarios": [{"name": n, "proposal": p} for n, p in scenarios], **top}


def _write(tmp_path, doc):
    path = tmp_path / "scenarios.json"
    path.write_text(json.dumps(doc))
    return str(path)


def test_loads_and_hashes_the_file(tmp_path):
    doc = _doc(("all-onprem", {"apps": {"app-a": {"residency": "onprem_only"}}}),
               ("empty", {}))
    loaded, sha = load_scenarios(_write(tmp_path, doc))
    assert loaded == doc and len(sha) == 64


def test_title_is_optional_and_proposal_schema_is_optional():
    validate_scenarios({"schema": SCHEMA, "scenarios": [{"name": "a", "proposal": {}}]})
    validate_scenarios(_doc(("a", {"schema": P, "title": "with schema"})))


@pytest.mark.parametrize("name", ["a", "onprem+2nd-gpu", "raw.pii_only", "A-1", "x" * 64])
def test_legal_names(name):
    validate_scenarios(_doc((name, {})))


@pytest.mark.parametrize("name", [
    "", "x" * 65, "two words", "tab\tname", "a/b", "name\n", "\nname",   # trailing newline
    "naïve", "ｆｕｌｌｗｉｄｔｈ", "٣", "a,b", "a(b)", None, 7, ["a"],
])
def test_illegal_names_are_refused(name):                                 # Review Focus 5
    with pytest.raises(ScenarioError) as exc:
        validate_scenarios(_doc((name, {})))
    assert exc.value.where == "scenarios[0].name"


def test_duplicate_name_names_the_second_occurrence():                    # Review Focus 5
    with pytest.raises(ScenarioError) as exc:
        validate_scenarios(_doc(("a", {}), ("b", {}), ("a", {})))
    assert exc.value.where == "scenarios[2].name" and "duplicate" in str(exc.value)


@pytest.mark.parametrize("doc,where", [
    ([], "<root>"),
    ({"scenarios": [{"name": "a", "proposal": {}}]}, "schema"),
    ({"schema": "routeforge.policy-scenarios/v2", "scenarios": []}, "schema"),
    ({"schema": P, "scenarios": []}, "schema"),                # a proposal schema is not ours
    (_doc(("a", {}), extra=1), "extra"),
    (_doc(("a", {}), title=5), "title"),
    ({"schema": SCHEMA, "title": "t"}, "scenarios"),
    ({"schema": SCHEMA, "scenarios": {}}, "scenarios"),
    ({"schema": SCHEMA, "scenarios": []}, "scenarios"),
    (_doc(*[(f"s{i}", {}) for i in range(MAX_SCENARIOS + 1)]), "scenarios"),
    ({"schema": SCHEMA, "scenarios": ["a"]}, "scenarios[0]"),
    ({"schema": SCHEMA, "scenarios": [{"name": "a", "proposal": {}, "rank": 1}]},
     "scenarios[0].rank"),
    ({"schema": SCHEMA, "scenarios": [{"name": "a"}]}, "scenarios[0](a).proposal"),
    (_doc(("a", [])), "scenarios[0](a).proposal"),
    (_doc(("a", {}), ("b", {"bogus": 1})), "scenarios[1](b).proposal.bogus"),
    (_doc(("a", {"schema": "routeforge.policy-proposal/v2"})), "scenarios[0](a).proposal.schema"),
    (_doc(("a", {"apps": {"app-a": {"residencyy": "any"}}})),
     "scenarios[0](a).proposal.apps.app-a.residencyy"),
    (_doc(("a", {"workload": {"tokenscale": 1}})), "scenarios[0](a).proposal.workload.tokenscale"),
    (_doc(("a", {"endpoints": {"update": {"m": {"latency": 5}}}})),
     "scenarios[0](a).proposal.endpoints.update.m.latency"),
])
def test_shape_errors_name_the_location(doc, where):
    with pytest.raises(ScenarioError) as exc:
        validate_scenarios(doc)
    assert exc.value.where == where, str(exc.value)


def test_twenty_scenarios_is_the_limit_not_past_it():
    validate_scenarios(_doc(*[(f"s{i}", {}) for i in range(MAX_SCENARIOS)]))


def test_invalid_json_is_named(tmp_path):
    path = tmp_path / "s.json"
    path.write_text("{not json")
    with pytest.raises(ScenarioError) as exc:
        load_scenarios(str(path))
    assert exc.value.where == "file"


def test_apply_scenarios_applies_each_in_file_order():
    doc = _doc(("tight", {"apps": {"app-a": {"residency": "onprem_only"}}}),
               ("empty", {}),
               ("scaled", {"schema": P, "workload": {"token_scale": 0.5}}))
    snap = make_snapshot()
    before_snap, before_doc = copy.deepcopy(snap), copy.deepcopy(doc)
    applied = apply_scenarios(snap, doc, base=BASE_SETTINGS)
    assert [a.name for a in applied] == ["tight", "empty", "scaled"]
    assert applied[0].doc["apps"][0]["policy"]["residency"] == "onprem_only"
    assert applied[1].doc == snap                              # an empty body is an identity
    assert applied[2].doc["assumptions"]["token_scale"] == {"*": 0.5}
    assert all(a.proposal["schema"] == P for a in applied)     # bodies become full proposals
    assert (snap, doc) == (before_snap, before_doc)            # inputs never mutated


@pytest.mark.parametrize("body,where", [
    ({"apps": {"ghost": {"residency": "any"}}}, "scenarios[2](third).proposal.apps.ghost"),
    ({"apps": {"app-a": {"residency": "moon"}}},
     "scenarios[2](third).proposal.apps.app-a.residency"),
    ({"endpoints": {"remove": ["ghost"]}}, "scenarios[2](third).proposal.endpoints.remove.ghost"),
    ({"workload": {"token_scale": 0}}, "scenarios[2](third).proposal.workload.token_scale"),
    ({"workload": {"apps": {"ghost": {"cached_share": 0.5}}}},
     "scenarios[2](third).proposal.workload.apps.ghost"),
    ({"endpoints": {"update": {"local-mid": {"latency_ms": -1}}}},
     "scenarios[2](third).proposal.endpoints.update.local-mid.latency_ms"),
])
def test_value_errors_carry_index_name_and_dotted_path(body, where):      # Review Focus 5
    """All-or-nothing: scenarios 0 and 1 are valid, the third is not -- the
    whole file fails, naming exactly where."""
    doc = _doc(("first", {}), ("second", {"workload": {"token_scale": 2}}), ("third", body))
    with pytest.raises(ScenarioError) as exc:
        apply_scenarios(make_snapshot(), doc, base=BASE_SETTINGS)
    assert exc.value.where == where, str(exc.value)
    assert str(exc.value).startswith(where + ": ")


def test_workload_apps_wildcard_is_rejected_inside_a_scenarios_file():    # F3 final review
    """The same ALL_APPS collision guard (app/eval/proposal.py), named by
    the scenario-file dotted path -- `scenarios[<i>](<name>).proposal.
    workload.apps.*` -- even when an app literally named "*" exists in the
    snapshot, so this is not merely "unknown application"."""
    from tests.eval.sim_fixtures import app_entry
    snap = make_snapshot(apps=[app_entry("*"), app_entry("app-a")])
    doc = _doc(("first", {}),
              ("second", {"workload": {"apps": {"*": {"token_scale": 0.5}}}}))
    with pytest.raises(ScenarioError) as exc:
        apply_scenarios(snap, doc, base=BASE_SETTINGS)
    assert exc.value.where == "scenarios[1](second).proposal.workload.apps.*"


def test_a_snapshot_problem_is_not_blamed_on_a_scenario():
    snap = make_snapshot()
    snap["settings"]["providers"]["mystery"] = {
        "enabled": True, "region": None, "prompt_retention": None,
        "accepts_raw_sensitive": None}
    with pytest.raises(SnapshotError) as exc:
        apply_scenarios(snap, _doc(("a", {})), base=BASE_SETTINGS)
    assert exc.value.where == "settings.providers.mystery"
