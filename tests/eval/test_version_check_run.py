"""`--candidate` end to end (spec §7; invariants E1, E2, E7, E8, E11, E15,
E16): the runner and the CLI over the tiny simulated suite of
`tests/eval/test_runner.py`, plus the starter suite. Every run is simulated.

The tiny suite has three endpoints, one per tier (`local-a` cheap, `local-b`
mid, `cloud-c` frontier, each dearer than the last). `ALL_FRONTIER` sets both
complexity thresholds to 0, so the candidate targets the frontier tier for
every request: it moves three of the five attempt chains and raises the mean
chosen cost. `SAME` restates the shipped weights and so changes nothing.
"""
from __future__ import annotations

import ast
import hashlib
import json
from pathlib import Path

import pytest
import yaml

from app.config.loader import load_settings
from app.eval import runner
from app.eval.candidate import SCHEMA
from app.eval.strategies import OnlineArm
from app.eval.version_check import CHECK_METRICS, CheckPlan
from tests.eval.test_runner import REPO_ROOT, _case, _cli, _jsonl, _run, _write_inputs

ALL_FRONTIER = {"complexity_thresholds": {"cheap_max": 0.0, "mid_max": 0.0}}
SAME = {"weights": dict(load_settings().routing.weights)}
#: The weight that marks the injected arm below; no shipped weight has it.
MARK = 0.123456
PINNED = ("metrics.json", "matrix.jsonl", "choices.jsonl")
BASE_ARM, CAND_ARM = "router_online", "router_online:candidate"


def _candidate(tmp_path, routing, name="t") -> str:
    path = tmp_path / "candidate.yaml"
    path.write_text(yaml.safe_dump({"schema": SCHEMA, "name": name, "routing": routing}))
    return str(path)


def _plan(**tolerances) -> CheckPlan:
    return CheckPlan(tolerances=tolerances)


def _inject_ineligible(monkeypatch, chain=("cloud-c",)) -> None:
    """Make the CANDIDATE's online arm (and only it) choose `chain` for
    every case. `cloud-c` is eligible for most cases and never for the
    `onprem_only` one -- a realistic violation, not a made-up slug."""
    class Injected(OnlineArm):
        def __init__(self, settings, *args, **kwargs):
            super().__init__(settings, *args, **kwargs)
            self._injected = settings.routing.weights.get("latency") == MARK

        def choose(self, inp, failed, *, case_index):
            real = super().choose(inp, failed, case_index=case_index)
            return tuple(chain) if self._injected else real

        def observe(self, case_index, attempts, rows_by_endpoint):
            super().observe(case_index, () if self._injected else attempts,
                            rows_by_endpoint)

    monkeypatch.setattr(runner, "OnlineArm", Injected)


# --- E1 / E16: the candidate is extra arms over the same sweep -----------------

def test_e1_without_a_candidate_no_check_is_written(tmp_path):
    suite, eps = _write_inputs(tmp_path)
    out = tmp_path / "out"
    result = _run(suite, eps, out)
    assert result.check is None
    assert not (out / "check.json").exists() and not (out / "check.md").exists()
    stamp = json.loads((out / "run.json").read_text())
    assert "candidate" not in stamp and "candidate" not in stamp["args"]


def test_e16_the_candidate_shares_the_sweep_and_moves_no_other_arm(tmp_path):
    suite, eps = _write_inputs(tmp_path)
    plain, flagged = tmp_path / "plain", tmp_path / "flagged"
    before = _run(suite, eps, plain, seed=3)
    after = _run(suite, eps, flagged, seed=3,
                 candidate_path=_candidate(tmp_path, ALL_FRONTIER))
    # One sweep: the same (case, endpoint) outcomes for both arms.
    assert (plain / "matrix.jsonl").read_bytes() == (flagged / "matrix.jsonl").read_bytes()
    # Every arm that existed is untouched; the candidate only ADDS three.
    added = set(after.metrics["strategies"]) - set(before.metrics["strategies"])
    assert added == {"router_cold:candidate", CAND_ARM, "router_warm_prior:candidate"}
    for name, m in before.metrics["strategies"].items():
        assert after.metrics["strategies"][name] == m, name
    old = [c for c in _jsonl(flagged / "choices.jsonl")
           if not c["strategy"].endswith(":candidate")]
    assert old == _jsonl(plain / "choices.jsonl")
    assert after.metrics["cases"] == before.metrics["cases"]
    assert sorted(p.name for p in flagged.iterdir()) == [
        "check.json", "check.md", "choices.jsonl", "matrix.jsonl", "metrics.json",
        "report.md", "run.json"]


def test_e2_the_check_is_byte_identical_twice(tmp_path):
    suite, eps = _write_inputs(tmp_path)
    cand = _candidate(tmp_path, ALL_FRONTIER)
    a, b = tmp_path / "a", tmp_path / "b"
    _run(suite, eps, a, seed=7, candidate_path=cand, check_plan=_plan(pass_rate=0.5))
    _run(suite, eps, b, seed=7, candidate_path=cand, check_plan=_plan(pass_rate=0.5))
    for name in ("check.json", "check.md", *PINNED):
        assert (a / name).read_bytes() == (b / name).read_bytes(), name


# --- E8: candidate == baseline -------------------------------------------------

def test_e8_a_candidate_that_restates_the_config_passes_with_zero_tolerance(tmp_path):
    suite, eps = _write_inputs(tmp_path)
    out = tmp_path / "out"
    result = _run(suite, eps, out, candidate_path=_candidate(tmp_path, SAME),
                  check_plan=_plan(pass_rate=0.0, quality_regret=0.0, mean_chosen_cost=0.0,
                                   wasted_attempts=0.0, exhausted_rate=0.0))
    assert result.exit_code == 0 and result.check["verdict"] == "pass"
    assert result.check["changed"] == {"n_cases": 5, "choice_changed": 0, "share": 0.0}
    for metric in ("pass_rate", "quality_regret", "mean_chosen_cost"):
        f = result.check["overall"][metric]
        assert (f["worsening"], f["lo"], f["hi"], f["change"]) == (0.0, 0.0, 0.0, "none")
    m = result.metrics["strategies"]
    assert m[CAND_ARM] == m[BASE_ARM]
    assert result.check == json.loads((out / "check.json").read_text())


# --- E7: the hard checks -------------------------------------------------------

@pytest.mark.parametrize("tolerances", [
    {}, {m: 1e9 for m in CHECK_METRICS}], ids=["no-tolerance", "huge-tolerances"])
def test_e7_an_injected_ineligible_candidate_exits_2_whatever_the_tolerances(
        tmp_path, monkeypatch, tolerances):
    _inject_ineligible(monkeypatch)
    suite, eps = _write_inputs(tmp_path)
    out = tmp_path / "out"
    result = _run(suite, eps, out,
                  candidate_path=_candidate(tmp_path, {"weights": {"latency": MARK}}),
                  check_plan=CheckPlan(tolerances=tolerances))
    assert result.exit_code == 2
    check = result.check
    assert check["verdict"] == "hard_fail" and check["exit_code"] == 2
    assert check["hard"]["failed"] is True and check["hard"]["candidate_violations"] >= 1
    assert check["hard"]["run_exit_code"] == 2
    # The detector fired for the candidate and for nothing else.
    assert result.metrics["strategies"][CAND_ARM]["violation_rate"] > 0
    assert result.metrics["strategies"][BASE_ARM]["violation_rate"] == 0.0
    assert "**HARD FAIL** (exit 2)" in (out / "check.md").read_text()
    assert json.loads((out / "run.json").read_text())["exit_code"] == 2


def test_e7_a_candidate_that_serves_an_empty_feasible_set_is_a_hard_fail(
        tmp_path, monkeypatch):
    """One case is `onprem_only` with no on-prem endpoint: its feasible set
    is empty and the real pipeline rejects it. The injected candidate names
    an endpoint for it anyway."""
    _inject_ineligible(monkeypatch)
    cases = [_case("faq-1", "What is the capital of France?"),
             _case("onprem-empty", "Where is Paris?", residency="onprem_only",
                   expect_empty=True)]
    cloud_only = [{"slug": "cloud-c", "provider": "azure", "tier": "frontier",
                   "price": {"input": 5.0, "output": 5.0}, "region": "us",
                   "prompt_retention": "none", "accepts_raw_sensitive": False,
                   "simulated": {"quality": {"default": 1.0},
                                 "latency_ms": {"mean": 900, "jitter": 20}}}]
    suite, eps = _write_inputs(tmp_path, cases, cloud_only)
    result = _run(suite, eps, tmp_path / "out",
                  candidate_path=_candidate(tmp_path, {"weights": {"latency": MARK}}),
                  check_plan=_plan(pass_rate=1.0))
    assert result.exit_code == 2 and result.check["verdict"] == "hard_fail"
    assert result.check["hard"]["candidate_served_on_empty"] == 1
    summary = {c["case_id"]: c for c in result.metrics["cases"]}["onprem-empty"]
    assert summary["rejected_ok"] is True and summary["calls"] == 0   # nothing was SERVED


# --- regressed / inconclusive ---------------------------------------------------

def test_a_candidate_that_costs_more_than_the_tolerance_exits_3(tmp_path):
    suite, eps = _write_inputs(tmp_path)
    out = tmp_path / "out"
    result = _run(suite, eps, out, candidate_path=_candidate(tmp_path, ALL_FRONTIER),
                  check_plan=_plan(mean_chosen_cost=1e-6))
    assert result.exit_code == 3 and result.check["verdict"] == "regressed"
    f = result.check["overall"]["mean_chosen_cost"]
    m = result.metrics["strategies"]
    assert f["baseline"] == m[BASE_ARM]["mean_chosen_cost"]
    assert f["candidate"] == m[CAND_ARM]["mean_chosen_cost"]
    assert f["worsening"] == pytest.approx(f["candidate"] - f["baseline"], abs=1e-12)
    assert f["worsening"] > 1e-6 and f["state"] == "regressed"
    assert result.check["changed"]["choice_changed"] == 3
    assert result.check["hard"]["failed"] is False
    # `metrics.json` keeps the bench run's own integrity code; the process
    # exit code is in `check.json` and `run.json`.
    assert json.loads((out / "metrics.json").read_text())["exit_code"] == 0
    stamp = json.loads((out / "run.json").read_text())
    assert stamp["exit_code"] == 3
    assert stamp["candidate"] == {"id": result.check["candidate"]["id"], "name": "t",
                                  "verdict": "regressed"}
    assert "**REGRESSED** (exit 3)" in (out / "check.md").read_text()


def test_the_same_candidate_with_no_tolerance_is_shown_and_not_judged(tmp_path):
    suite, eps = _write_inputs(tmp_path)
    result = _run(suite, eps, tmp_path / "out",
                  candidate_path=_candidate(tmp_path, ALL_FRONTIER))
    assert result.exit_code == 0 and result.check["verdict"] == "no_hard_failure"
    assert result.check["overall"]["mean_chosen_cost"]["worsening"] > 0


def test_a_named_metric_with_too_few_cases_is_inconclusive_and_exits_4(tmp_path):
    """Five cases against a minimum of six: nothing can be judged."""
    suite, eps = _write_inputs(tmp_path)
    result = _run(suite, eps, tmp_path / "out", min_group_size=6,
                  candidate_path=_candidate(tmp_path, SAME),
                  check_plan=_plan(pass_rate=0.5))
    assert result.exit_code == 4 and result.check["verdict"] == "inconclusive"
    assert result.check["plan"]["min_group_size"] == 6


# --- E11: nothing is applied ---------------------------------------------------

def _digest(paths) -> dict[str, str]:
    return {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(paths)}


def test_e11_the_check_writes_only_under_out_and_touches_no_config(tmp_path):
    suite, eps = _write_inputs(tmp_path)
    cand = _candidate(tmp_path, ALL_FRONTIER)
    config = _digest((REPO_ROOT / "config").rglob("*.yaml"))
    inputs = _digest(p for p in tmp_path.iterdir() if p.is_file())
    out = tmp_path / "out"
    _run(suite, eps, out, candidate_path=cand, check_plan=_plan(mean_chosen_cost=1e-6),
         group_report=True)
    assert _digest((REPO_ROOT / "config").rglob("*.yaml")) == config
    assert _digest(p for p in tmp_path.iterdir() if p.is_file()) == inputs
    assert sorted(p.name for p in tmp_path.iterdir()) == sorted(
        [Path(suite).name, Path(eps).name, "candidate.yaml", "out"])
    assert sorted(p.name for p in out.iterdir()) == [
        "check.json", "check.md", "choices.jsonl", "groups.json", "groups.md",
        "matrix.jsonl", "metrics.json", "report.md", "run.json"]


#: module -> every non-stdlib module it may import. None of them is an admin
#: route, the application store, the pipeline or an HTTP client.
_MAY_IMPORT = {
    "groups.py": {"app.eval.oracle"},
    "version_check.py": {"app.eval.groups", "app.eval.oracle", "app.eval.uncertainty"},
    "candidate.py": {"yaml", "app.config.lint", "app.config.schema",
                     "app.eval.strategies", "app.routing.strategy"},
}
_STDLIB = {"__future__", "copy", "dataclasses", "hashlib", "json", "pathlib"}
_WRITES = {"write_text", "write_bytes", "unlink", "mkdir", "rename",
           "post", "put", "patch", "delete", "request", "commit", "execute", "open"}


@pytest.mark.parametrize("module", sorted(_MAY_IMPORT))
def test_e11_the_new_modules_have_no_way_to_apply_anything(module):
    """No import of a serving surface that could change a deployment, and no
    call that writes a file, a database row or an HTTP request. The only
    writes of this slice are the runner's, into `--out`."""
    tree = ast.parse((REPO_ROOT / "app" / "eval" / module).read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {a.name for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            imported.add(node.module or "")
    assert imported - _STDLIB == _MAY_IMPORT[module]
    calls = {(n.func.attr if isinstance(n.func, ast.Attribute) else
              getattr(n.func, "id", "")) for n in ast.walk(tree) if isinstance(n, ast.Call)}
    assert not calls & _WRITES, calls & _WRITES


def test_e11_no_admin_route_or_setting_was_added():
    """The slice adds no way in: `app/` outside `app/eval/` does not mention
    the candidate schema, the check, or the group report."""
    needles = ("router-candidate", "router-version-check", "bench-group-report",
               "version_check", "candidate_settings")
    for path in (REPO_ROOT / "app").rglob("*.py"):
        if (REPO_ROOT / "app" / "eval") in path.parents:
            continue
        text = path.read_text(encoding="utf-8")
        for needle in needles:
            assert needle not in text, (str(path), needle)


# --- E15: load errors ----------------------------------------------------------

@pytest.mark.parametrize("doc, message", [
    ({"schema": SCHEMA, "routing": {"max_escalations": 2}}, "cannot be evaluated"),
    ({"schema": SCHEMA, "routing": {"wieghts": {"cost": 1}}}, "routing.wieghts"),
    ({"schema": "nope", "routing": {"strategy": "default"}}, "schema"),
    ({"schema": SCHEMA, "routing": {"strategy": "no-such"}}, "routing.strategy"),
])
def test_e15_a_bad_candidate_is_a_load_error_before_anything_is_written(
        tmp_path, capsys, doc, message):
    suite, eps = _write_inputs(tmp_path)
    path = tmp_path / "candidate.yaml"
    path.write_text(yaml.safe_dump(doc))
    out = tmp_path / "out"
    result = _run(suite, eps, out, candidate_path=str(path))
    assert result.exit_code == 1 and not out.exists()
    assert message in capsys.readouterr().err


def test_e15_a_missing_candidate_file_is_a_load_error(tmp_path, capsys):
    suite, eps = _write_inputs(tmp_path)
    result = _run(suite, eps, tmp_path / "out",
                  candidate_path=str(tmp_path / "absent.yaml"))
    assert result.exit_code == 1 and "load error" in capsys.readouterr().err


def test_e15_a_variant_may_not_take_the_candidates_name(tmp_path, capsys):
    suite, eps = _write_inputs(tmp_path)
    variants = tmp_path / "variants.yaml"
    variants.write_text(yaml.safe_dump({"candidate": dict(SAME["weights"])}))
    result = _run(suite, eps, tmp_path / "out", variants_path=str(variants),
                  candidate_path=_candidate(tmp_path, SAME))
    assert result.exit_code == 1 and "reserved" in capsys.readouterr().err
    # Without a candidate the same variants file still runs, as it always did.
    assert _run(suite, eps, tmp_path / "ok", variants_path=str(variants)).exit_code == 0


def test_e15_a_plan_without_a_candidate_is_a_load_error(tmp_path, capsys):
    suite, eps = _write_inputs(tmp_path)
    result = _run(suite, eps, tmp_path / "out", check_plan=_plan(pass_rate=0.1))
    assert result.exit_code == 1 and "without a candidate" in capsys.readouterr().err


# --- with the other outputs -----------------------------------------------------

def test_the_group_report_covers_the_candidates_arms(tmp_path):
    suite, eps = _write_inputs(tmp_path)
    result = _run(suite, eps, tmp_path / "out", group_report=True, min_group_size=1,
                  candidate_path=_candidate(tmp_path, ALL_FRONTIER))
    assert CAND_ARM in result.groups["strategies"]
    assert result.check["plan"]["min_group_size"] == 1
    # One set of numbers: the check's overall figure is the group report's.
    for arm, side in ((BASE_ARM, "baseline"), (CAND_ARM, "candidate")):
        assert (result.groups["strategies"][arm]["overall"]["metrics"]["pass_rate"]["value"]
                == result.check["overall"]["pass_rate"][side])


def test_a_stale_check_is_removed_by_a_run_without_a_candidate(tmp_path):
    suite, eps = _write_inputs(tmp_path)
    out = tmp_path / "out"
    _run(suite, eps, out, candidate_path=_candidate(tmp_path, SAME))
    assert (out / "check.json").is_file()
    _run(suite, eps, out)
    assert not (out / "check.json").exists() and not (out / "check.md").exists()


# --- the starter suite ----------------------------------------------------------

STARTER = ("eval/suites/starter.jsonl", "eval/endpoints/healthcare-sim.yaml")


def test_starter_restating_the_config_passes(tmp_path):
    result = _run(*STARTER, tmp_path / "out", candidate_path=_candidate(tmp_path, SAME),
                  check_plan=_plan(pass_rate=0.0, mean_chosen_cost=0.0))
    assert result.exit_code == 0 and result.check["verdict"] == "pass"
    assert result.check["changed"]["choice_changed"] == 0


def test_starter_a_latency_only_candidate_regresses_on_cost(tmp_path):
    """Cost weight 0, latency weight 2: the candidate prefers the faster,
    dearer cloud endpoint wherever both are eligible. It makes no ineligible
    choice -- the eligible set is not the router's to change -- and it costs
    more than a tolerance of $0.0001 a request allows."""
    result = _run(*STARTER, tmp_path / "out",
                  candidate_path=_candidate(tmp_path, {"weights": {"cost": 0.0,
                                                                   "latency": 2.0}}),
                  check_plan=_plan(pass_rate=0.02, mean_chosen_cost=0.0001))
    assert result.exit_code == 3
    check = result.check
    assert check["hard"] == {"failed": False, "candidate_violations": 0,
                             "candidate_served_on_empty": 0, "run_exit_code": 0}
    assert check["overall"]["mean_chosen_cost"]["state"] == "regressed"
    assert check["overall"]["mean_chosen_cost"]["change"] == "worse"
    assert {"scope": "overall", "metric": "mean_chosen_cost",
            "state": "regressed"} in check["regressed"]
    # The two groups below the minimum are listed, never judged.
    assert check["groups"]["placement"]["escalated"] == {
        "n_cases": 2, "state": "insufficient_cases"}
    assert not [r for r in check["regressed"] if "escalated" in r["scope"]]


# --- CLI ------------------------------------------------------------------------

def test_cli_a_regressing_candidate_exits_3_and_says_nothing_was_applied(tmp_path):
    suite, eps = _write_inputs(tmp_path)
    out = tmp_path / "cli-out"
    proc = _cli("--suite", suite, "--endpoints", eps, "--out", str(out),
                "--candidate", _candidate(tmp_path, ALL_FRONTIER),
                "--tolerance", "mean_chosen_cost=0.000001", "--tolerance", "pass_rate=0.5",
                "--check-replicates", "200", "--check-interval", "0.8", "--check-seed", "4")
    assert proc.returncode == 3, proc.stderr
    assert "router-version check: regressed (nothing was applied)" in proc.stdout
    plan = json.loads((out / "check.json").read_text())["plan"]
    assert plan == {"method": "paired_case_bootstrap", "replicates": 200, "interval": 0.8,
                    "seed": 4, "min_group_size": 5,
                    "tolerances": {"mean_chosen_cost": 1e-06, "pass_rate": 0.5}}


def test_cli_a_candidate_that_changes_nothing_exits_0(tmp_path):
    suite, eps = _write_inputs(tmp_path)
    proc = _cli("--suite", suite, "--endpoints", eps, "--out", str(tmp_path / "o"),
                "--candidate", _candidate(tmp_path, SAME), "--tolerance", "pass_rate=0")
    assert proc.returncode == 0, proc.stderr
    assert "router-version check: pass (nothing was applied)" in proc.stdout


def test_cli_too_few_cases_exits_4(tmp_path):
    suite, eps = _write_inputs(tmp_path)
    proc = _cli("--suite", suite, "--endpoints", eps, "--out", str(tmp_path / "o"),
                "--candidate", _candidate(tmp_path, SAME), "--tolerance", "pass_rate=0.5",
                "--min-group-size", "6")
    assert proc.returncode == 4, proc.stderr
    assert "router-version check: inconclusive" in proc.stdout


@pytest.mark.parametrize("flags, message", [
    (["--tolerance", "pass_rate=0.1"], "--tolerance needs --candidate"),
    (["--check-replicates", "50"], "--check-replicates needs --candidate"),
    (["--check-interval", "0.5", "--check-seed", "1"],
     "--check-interval, --check-seed needs --candidate"),
])
def test_cli_a_check_flag_without_a_candidate_is_refused(tmp_path, flags, message):
    suite, eps = _write_inputs(tmp_path)
    out = tmp_path / "o"
    proc = _cli("--suite", suite, "--endpoints", eps, "--out", str(out), *flags)
    assert proc.returncode == 1 and message in proc.stderr
    assert not out.exists()


@pytest.mark.parametrize("flags, message", [
    (["--tolerance", "accuracy=0.1"], "unknown metric"),
    (["--tolerance", "pass_rate=-1"], "finite number >= 0"),
    (["--tolerance", "pass_rate"], "METRIC=VALUE"),
    (["--check-replicates", "0"], "replicates"),
    (["--check-interval", "1.5"], "interval"),
    (["--check-seed", "-2"], "seed"),
])
def test_cli_a_bad_check_value_is_refused(tmp_path, flags, message):
    suite, eps = _write_inputs(tmp_path)
    out = tmp_path / "o"
    proc = _cli("--suite", suite, "--endpoints", eps, "--out", str(out),
                "--candidate", _candidate(tmp_path, SAME), *flags)
    assert proc.returncode == 1 and message in proc.stderr
    assert not out.exists()
