"""`--group-report` end to end (spec §4.4; invariants E1-E3, E5): the runner
and the CLI, over the same tiny simulated suite `tests/eval/test_runner.py`
uses. Every run is simulated; nothing makes a network call.
"""
from __future__ import annotations

import json

import pytest

from app.eval import runner
from app.eval.groups import DIMENSIONS, INSUFFICIENT
from tests.eval.test_runner import (DEFAULT_CASES, _case, _cli, _run, _write_inputs)

PINNED = ("metrics.json", "matrix.jsonl", "choices.jsonl")
#: groups.json metric -> the same figure's key in metrics.json.
SHARED = ("violation_rate", "pass_rate", "quality_regret", "cost_regret",
          "latency_regret", "floor_miss_rate")


def _figure(cell: dict):
    return cell.get("value")


def test_e1_without_the_flag_nothing_new_is_written(tmp_path):
    suite, eps = _write_inputs(tmp_path)
    out = tmp_path / "out"
    result = _run(suite, eps, out)
    assert result.groups is None
    assert sorted(p.name for p in out.iterdir()) == [
        "choices.jsonl", "matrix.jsonl", "metrics.json", "report.md", "run.json"]
    stamp = json.loads((out / "run.json").read_text())
    assert "group_report" not in stamp["args"] and "min_group_size" not in stamp["args"]


def test_e1_the_flag_adds_two_files_and_changes_no_other(tmp_path):
    suite, eps = _write_inputs(tmp_path)
    plain, flagged = tmp_path / "plain", tmp_path / "flagged"
    _run(suite, eps, plain, seed=3)
    result = _run(suite, eps, flagged, seed=3, group_report=True)
    for name in PINNED:
        assert (plain / name).read_bytes() == (flagged / name).read_bytes(), name
    assert sorted(p.name for p in flagged.iterdir()) == [
        "choices.jsonl", "groups.json", "groups.md", "matrix.jsonl", "metrics.json",
        "report.md", "run.json"]
    assert result.groups == json.loads((flagged / "groups.json").read_text())
    stamp = json.loads((flagged / "run.json").read_text())
    assert stamp["args"]["group_report"] is True and stamp["args"]["min_group_size"] == 5


def test_e2_the_group_report_is_byte_identical_twice(tmp_path):
    suite, eps = _write_inputs(tmp_path)
    a, b = tmp_path / "a", tmp_path / "b"
    _run(suite, eps, a, seed=7, group_report=True, min_group_size=1)
    _run(suite, eps, b, seed=7, group_report=True, min_group_size=1)
    for name in ("groups.json", "groups.md"):
        assert (a / name).read_bytes() == (b / name).read_bytes(), name


def test_e3_the_run_partitions_its_cases(tmp_path):
    suite, eps = _write_inputs(tmp_path)
    doc = _run(suite, eps, tmp_path / "out", group_report=True).groups
    assert doc["n_cases"] == len(DEFAULT_CASES) == 5
    for dim in DIMENSIONS:
        assert sum(doc["group_sizes"][dim].values()) == 5, dim
    assert doc["group_sizes"]["workload"] == {"claim": 2, "faq": 3}
    assert doc["group_sizes"]["placement"] == {"any": 4, "onprem_only": 1}
    assert doc["group_sizes"]["protection"] == {"enforced": 4, "passthrough": 1}
    # No case sets tags.app: an application is a distinct app_policy. Three
    # here -- any/enforced (3 cases), any/passthrough, onprem_only/enforced.
    sizes = doc["group_sizes"]["application"]
    assert sorted(sizes.values()) == [1, 1, 3]
    assert all(label.startswith("policy-") for label in sizes)


def test_the_app_tag_names_the_application(tmp_path):
    cases = [json.loads(json.dumps(c)) for c in DEFAULT_CASES]
    for case, app in zip(cases, ("billing", "billing", "claims", "claims", "claims")):
        case["tags"]["app"] = app
    suite, eps = _write_inputs(tmp_path, cases)
    doc = _run(suite, eps, tmp_path / "out", group_report=True).groups
    assert doc["group_sizes"]["application"] == {"billing": 2, "claims": 3}


def test_overall_figures_are_the_metrics_json_figures(tmp_path):
    """One set of numbers: `groups.json`'s overall block restates
    `metrics.json`'s strategies, with each figure's denominator beside it."""
    suite, eps = _write_inputs(tmp_path)
    result = _run(suite, eps, tmp_path / "out", group_report=True, min_group_size=1)
    assert set(result.groups["strategies"]) == set(result.metrics["strategies"])
    for name, block in result.groups["strategies"].items():
        for metric in SHARED:
            assert (_figure(block["overall"]["metrics"][metric])
                    == result.metrics["strategies"][name][metric]), (name, metric)
        assert block["overall"]["n_cases"] == result.metrics["strategies"][name]["n_cases"]


def test_tag_groups_agree_with_the_existing_groups_block(tmp_path):
    """`metrics.json` already carries `groups` for the three tags. The report
    extends that block; it must not disagree with it."""
    suite, eps = _write_inputs(tmp_path)
    result = _run(suite, eps, tmp_path / "out", group_report=True, min_group_size=1)
    for name, block in result.groups["strategies"].items():
        for tag in ("workload", "placement", "protection"):
            old = result.metrics["groups"][name][tag]
            new = block["dimensions"][tag]
            assert set(old) == set(new), (name, tag)
            for label in old:
                for metric in SHARED:
                    assert (_figure(new[label]["metrics"][metric])
                            == old[label][metric]), (name, tag, label, metric)


def test_e5_small_groups_print_no_rate_at_the_default_minimum(tmp_path):
    suite, eps = _write_inputs(tmp_path)
    out = tmp_path / "out"
    doc = _run(suite, eps, out, group_report=True).groups
    block = doc["strategies"]["router_online"]
    assert "value" in block["overall"]["metrics"]["pass_rate"]          # 5 cases
    onprem = block["dimensions"]["placement"]["onprem_only"]
    assert onprem["metrics"]["pass_rate"] == {"state": INSUFFICIENT, "n": 1}
    text = (out / "groups.md").read_text()
    assert "insufficient cases (n = 1)" in text and "None" not in text
    assert "## Every group: `router_online`" in text


def test_e5_an_injected_violation_shows_in_its_group_however_small(tmp_path, monkeypatch):
    """A strategy that always picks the cloud endpoint violates on the one
    `onprem_only` case. That group has one case: no rate, and the count."""
    monkeypatch.setattr(runner, "cheapest_attempts",
                        lambda registry, cap: (lambda inp, failed: ("cloud-c",)))
    suite, eps = _write_inputs(tmp_path)
    result = _run(suite, eps, tmp_path / "out", group_report=True)
    assert result.exit_code == 2
    cheapest = result.groups["strategies"]["cheapest"]
    onprem = cheapest["dimensions"]["placement"]["onprem_only"]
    assert onprem["n_cases"] == 1 and onprem["violations"] == 1
    assert onprem["metrics"]["violation_rate"] == {"state": INSUFFICIENT, "n": 1}
    # Counts partition like cases do: the groups of a dimension sum to the total.
    assert cheapest["overall"]["violations"] == sum(
        g["violations"] for g in cheapest["dimensions"]["placement"].values()) >= 1
    assert result.groups["strategies"]["router_online"]["overall"]["violations"] == 0


def test_an_empty_case_is_in_its_group_and_in_no_quality_figure(tmp_path):
    cases = [_case("faq-1", "What is the capital of France?"),
             _case("onprem-empty", "Where is Paris?", residency="onprem_only",
                   expect_empty=True)]
    cloud_only = [{"slug": "cloud-c", "provider": "azure", "tier": "frontier",
                   "price": {"input": 5.0, "output": 5.0}, "region": "us",
                   "prompt_retention": "none", "accepts_raw_sensitive": False,
                   "simulated": {"quality": {"default": 1.0},
                                 "latency_ms": {"mean": 900, "jitter": 20}}}]
    suite, eps = _write_inputs(tmp_path, cases, cloud_only)
    result = _run(suite, eps, tmp_path / "out", group_report=True, min_group_size=1)
    assert result.exit_code == 0
    onprem = result.groups["strategies"]["router_online"]["dimensions"]["placement"][
        "onprem_only"]
    assert onprem["n_cases"] == 1 and onprem["empty_cases"] == 1
    assert onprem["empty_set_mismatches"] == 0 and onprem["violations"] == 0
    assert onprem["metrics"]["pass_rate"] == {"state": INSUFFICIENT, "n": 0}
    assert onprem["metrics"]["empty_set_mismatch_rate"] == {"value": 0.0, "n": 1}


def test_a_stale_group_report_is_removed_by_an_unflagged_run(tmp_path):
    suite, eps = _write_inputs(tmp_path)
    out = tmp_path / "out"
    _run(suite, eps, out, group_report=True)
    assert (out / "groups.json").is_file()
    _run(suite, eps, out)
    assert not (out / "groups.json").exists() and not (out / "groups.md").exists()


@pytest.mark.parametrize("bad", [0, -3, True, 2.5])
def test_a_bad_minimum_is_a_load_error(tmp_path, bad, capsys):
    suite, eps = _write_inputs(tmp_path)
    out = tmp_path / "out"
    result = _run(suite, eps, out, group_report=True, min_group_size=bad)
    assert result.exit_code == 1 and not out.exists()
    assert "min_group_size" in capsys.readouterr().err


def test_the_starter_suite_groups(tmp_path):
    doc = _run("eval/suites/starter.jsonl", "eval/endpoints/healthcare-sim.yaml",
               tmp_path / "out", group_report=True).groups
    assert doc["group_sizes"]["placement"] == {
        "any": 8, "escalated": 2, "onprem_only": 4, "us_only": 8}
    assert doc["group_sizes"]["workload"] == {
        "claim_summary": 10, "faq": 8, "onprem_required": 4}
    assert sorted(doc["group_sizes"]["application"].values()) == [2, 4, 8, 8]
    for block in doc["strategies"].values():
        assert block["overall"]["violations"] == 0
        escalated = block["dimensions"]["placement"]["escalated"]
        assert escalated["metrics"]["pass_rate"] == {"state": INSUFFICIENT, "n": 2}


# --- CLI ------------------------------------------------------------------------

def test_cli_group_report_writes_both_files(tmp_path):
    suite, eps = _write_inputs(tmp_path)
    out = tmp_path / "cli-out"
    proc = _cli("--suite", suite, "--endpoints", eps, "--out", str(out),
                "--group-report", "--min-group-size", "2")
    assert proc.returncode == 0, proc.stderr
    doc = json.loads((out / "groups.json").read_text())
    assert doc["min_group_size"] == 2
    assert (out / "groups.md").read_text().startswith("# FeasibleRouteBench group report")


def test_cli_a_minimum_without_the_report_is_refused(tmp_path):
    suite, eps = _write_inputs(tmp_path)
    out = tmp_path / "o"
    proc = _cli("--suite", suite, "--endpoints", eps, "--out", str(out),
                "--min-group-size", "2")
    assert proc.returncode == 1 and "--min-group-size needs" in proc.stderr
    assert not out.exists()
