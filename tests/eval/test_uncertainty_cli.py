"""P3c-3: `scripts/policy_simulate.py --uncertainty`, end to end, in every
mode (spec §3, §9; invariants U1, U2, U10).

Runs the CLI in-process over the committed golden inputs
(`tests/eval/test_uncertainty_golden.py`), so "the flag only adds" is
checked against the outputs pinned from before P3c-3.
"""
from __future__ import annotations

import json
import subprocess
import sys

import pytest

from app.eval.calibration import build_recommendation, dump_recommendation, estimate
from app.eval.uncertainty import METHOD, NOT_COVERED, UNCERTAINTY_CAVEAT
from tests.eval.test_uncertainty import _leaves
from tests.eval.test_uncertainty_golden import (EXPECTED, EXPORT, MODES, PROPOSAL, ROOT,
                                                SCRIPT, SNAPSHOT, load_cli, run_mode)

FAST = ("--uncertainty", "--bootstrap", "60")
_ID = {"proposal": "simulation_id", "proposal_shadow": "simulation_id",
       "scenarios": "comparison_id"}          # the shadow-only document has no id


def _doc(mode: str, out) -> dict:
    return json.loads((out / MODES[mode][1][0]).read_text())


def _expected(mode: str) -> dict:
    return json.loads((EXPECTED / mode / MODES[mode][1][0]).read_text())


def _strip(doc: dict) -> dict:
    """`doc` without everything `--uncertainty` adds, and without its id
    (the id is a hash of `inputs`, which the flag extends on purpose)."""
    doc = json.loads(json.dumps(doc))
    doc.pop("uncertainty", None)
    doc["inputs"].pop("uncertainty", None)
    if isinstance(doc.get("shadow_prices"), dict):
        doc["shadow_prices"].pop("uncertainty", None)
    doc.pop("simulation_id", None)
    doc.pop("comparison_id", None)
    return doc


# --- U1: the flag only adds -----------------------------------------------------

@pytest.mark.parametrize("mode", sorted(MODES))
def test_u1_the_flag_adds_keys_and_changes_no_existing_value(mode, tmp_path):   # U1
    """With the flag, the document is the pinned pre-P3c-3 document plus
    `uncertainty`, `inputs.uncertainty` and (with shadow prices)
    `shadow_prices.uncertainty`. No existing key moves."""
    out = tmp_path / "out"
    assert run_mode(mode, out, *FAST) == 0
    doc = _doc(mode, out)
    assert _strip(doc) == _strip(_expected(mode))
    assert "uncertainty" in doc and "uncertainty" in doc["inputs"]
    assert ("uncertainty" in doc.get("shadow_prices", {})) == ("shadow" in mode)
    assert sorted(p.name for p in out.iterdir()) == sorted(MODES[mode][1])


# --- U2: deterministic ----------------------------------------------------------

@pytest.mark.parametrize("mode", sorted(MODES))
def test_u2_the_flagged_run_is_byte_identical_twice(mode, tmp_path):       # U2
    first, second = tmp_path / "a", tmp_path / "b"
    assert run_mode(mode, first, *FAST) == 0 and run_mode(mode, second, *FAST) == 0
    for name in MODES[mode][1]:
        assert (first / name).read_bytes() == (second / name).read_bytes(), name


# --- U10: the plan is an input ----------------------------------------------------

def test_u10_plan_values_are_in_inputs_with_their_defaults(tmp_path):      # U10
    out = tmp_path / "out"
    assert run_mode("proposal", out, "--uncertainty", "--bootstrap", "40") == 0
    doc = _doc("proposal", out)
    assert doc["inputs"]["uncertainty"] == {
        "replicates": 40, "interval": 0.9, "seed": 0, "min_rows": 30,
        "quality_posteriors_sha256": []}
    block = doc["uncertainty"]
    assert (block["method"], block["strata"], block["replicates"], block["interval"],
            block["seed"], block["min_rows"]) == (METHOD, "application_id", 40, 0.9, 0, 30)
    assert block["not_covered"] == list(NOT_COVERED)
    assert block["quality_posterior_sets"] == []


@pytest.mark.parametrize("mode", sorted(_ID))
def test_u10_the_id_tells_plans_apart(mode, tmp_path):                     # U10
    """Same files, different plan: a different id. And a flagged run never
    shares an id with the unflagged one."""
    ids = {}
    for label, extra in (("base", FAST), ("seed", (*FAST, "--seed", "1")),
                         ("n", ("--uncertainty", "--bootstrap", "61")),
                         ("level", (*FAST, "--interval", "0.8")),
                         ("min", (*FAST, "--min-rows", "5"))):
        out = tmp_path / label
        assert run_mode(mode, out, *extra) == 0
        ids[label] = _doc(mode, out)[_ID[mode]]
    ids["unflagged"] = _expected(mode)[_ID[mode]]
    assert len(set(ids.values())) == len(ids), ids


def test_the_seed_moves_the_ranges_and_nothing_else(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    assert run_mode("proposal", a, *FAST) == 0
    assert run_mode("proposal", b, *FAST, "--seed", "7") == 0
    one, two = _doc("proposal", a), _doc("proposal", b)
    assert _strip(one) == _strip(two)
    assert one["uncertainty"]["totals"] != two["uncertainty"]["totals"]
    assert ([(path, node["point"]) for path, node in _leaves(one["uncertainty"])]
            == [(path, node["point"]) for path, node in _leaves(two["uncertainty"])])


# --- what each mode's documents carry -------------------------------------------

def test_proposal_block_has_totals_apps_and_per_day(tmp_path):
    out = tmp_path / "out"
    assert run_mode("proposal", out, *FAST) == 0
    doc = _doc("proposal", out)
    block = doc["uncertainty"]
    assert block["totals"]["n"] == doc["rows"]["simulated"] == 111
    assert set(block["by_app"]) == set(doc["by_app"]) == {"big", "few", "safe"}
    assert block["by_app"]["few"] == {"insufficient_data": True, "n": 6}
    for arm in ("current", "proposed", "delta"):
        assert block["totals"]["cost_usd"][arm]["point"] == doc["totals"]["cost_usd"][arm]
        assert block["per_day"]["cost_usd"][arm]["point"] == doc["per_day"]["cost_usd"][arm]
    for name in ("route_changed", "newly_rejected", "newly_served"):
        assert block["totals"][name]["point"] == doc["totals"][name]
    for path, node in _leaves(block):
        assert node["lo"] is None or node["lo"] <= node["point"] <= node["hi"] \
            or node.get("point_outside"), path


def test_proposal_report_gets_the_section_between_the_report_and_shadow_prices(tmp_path):
    out = tmp_path / "out"
    assert run_mode("proposal_shadow", out, *FAST) == 0
    report = (out / "report.md").read_text()
    expected = (EXPECTED / "proposal" / "report.md").read_text()
    assert report.startswith(expected)                 # the P3a report, untouched
    order = [report.index(h) for h in ("## Excluded rows", "## Uncertainty ranges",
                                       "### Totals (n = 111)", "## Shadow prices",
                                       "### Ranges")]
    assert order == sorted(order)
    for line in UNCERTAINTY_CAVEAT.split("\n"):
        assert "> " + line in report
    assert "#### few (n = 6)\n\ninsufficient data (n = 6)" in report
    assert "90% percentile intervals from 60 bootstrap resamples" in report


def test_shadow_only_run_has_the_block_head_and_shadow_ranges(tmp_path):
    out = tmp_path / "out"
    assert run_mode("shadow", out, *FAST) == 0
    doc = _doc("shadow", out)
    assert set(doc) == {"schema", "inputs", "shadow_prices", "uncertainty"}
    assert "totals" not in doc["uncertainty"] and doc["uncertainty"]["replicates"] == 60
    ranges = doc["shadow_prices"]["uncertainty"]
    assert len(ranges["constraints"]) == len(doc["shadow_prices"]["constraints"]) > 0
    report = (out / "report.md").read_text()
    assert report.startswith("> **Simulation only.**")
    assert report.index("## Uncertainty ranges") < report.index("## Shadow prices") \
        < report.index("### Ranges")
    assert "### Totals" not in report


def test_scenarios_documents_carry_one_block_per_scenario(tmp_path):
    out = tmp_path / "out"
    assert run_mode("scenarios", out, *FAST) == 0
    doc = _doc("scenarios", out)
    names = [entry["name"] for entry in doc["uncertainty"]["scenarios"]]
    assert names == [row["name"] for row in doc["table"]] == [
        "no-change", "drop-mid", "half-tokens"]
    report = (out / "report.md").read_text()
    assert "## Uncertainty ranges" in report and report.count("### Ranges") == 3
    assert "RouteForge does not rank or recommend scenarios." in report


# --- flags ------------------------------------------------------------------------

@pytest.mark.parametrize("extra,needle", [
    (("--uncertainty", "--bootstrap", "0"), "replicates"),
    (("--uncertainty", "--bootstrap", "20001"), "replicates"),
    (("--uncertainty", "--interval", "1"), "interval"),
    (("--uncertainty", "--interval", "0"), "interval"),
    (("--uncertainty", "--interval", "nan"), "interval"),
    (("--uncertainty", "--seed", "-1"), "seed"),
    (("--uncertainty", "--min-rows", "0"), "min_rows"),
    (("--bootstrap", "100"), "--bootstrap given without --uncertainty"),
    (("--interval", "0.8"), "--interval given without --uncertainty"),
    (("--seed", "3"), "--seed given without --uncertainty"),
    (("--min-rows", "5"), "--min-rows given without --uncertainty"),
    (("--seed", "3", "--min-rows", "5"), "--seed, --min-rows given without --uncertainty"),
])
def test_bad_or_orphan_flags_exit_2_and_write_nothing(extra, needle, tmp_path, capsys):
    out = tmp_path / "out"
    assert run_mode("proposal", out, *extra) == 2
    assert needle in capsys.readouterr().err
    assert not out.exists()


def test_limits_are_accepted(tmp_path):
    out = tmp_path / "out"
    assert run_mode("proposal", out, "--uncertainty", "--bootstrap", "1", "--interval",
                    "0.99", "--min-rows", "1", "--seed", "0") == 0
    block = _doc("proposal", out)["uncertainty"]
    assert "insufficient_data" not in block["by_app"]["few"]     # --min-rows 1
    assert block["points_outside_interval"] > 0                  # one replicate


def test_help_names_the_flags():
    proc = subprocess.run([sys.executable, str(SCRIPT), "--help"], capture_output=True,
                          text=True, cwd=ROOT,
                          env={**__import__("os").environ,
                               "LITELLM_LOCAL_MODEL_COST_MAP": "True"})
    assert proc.returncode == 0
    for flag in ("--uncertainty", "--bootstrap", "--interval", "--seed", "--min-rows",
                 "--quality-posteriors"):
        assert flag in proc.stdout
    assert "Sampling variability only" in " ".join(proc.stdout.split())


# --- the gate runs before the bootstrap ---------------------------------------------

@pytest.mark.parametrize("mode", sorted(MODES))
def test_min_agreement_refuses_before_any_bootstrap(mode, tmp_path, monkeypatch, capsys):
    """The golden export's model agreement is far below 0.99. The run must
    exit 4, write nothing and never start a bootstrap -- in `--scenarios`
    mode too, where the gate otherwise runs after `compare`."""
    import app.eval.uncertainty as uncertainty_mod

    def boom(*_a, **_kw):
        raise AssertionError("bootstrap started before the --min-agreement gate")

    monkeypatch.setattr(uncertainty_mod, "run_bootstrap", boom)
    out = tmp_path / "out"
    assert run_mode(mode, out, *FAST, "--min-agreement", "0.99") == 4
    assert "below --min-agreement" in capsys.readouterr().err
    assert not out.exists()


# --- --quality-posteriors -------------------------------------------------------------

def _recommendation(tmp_path, model="local-cheap"):
    scoring = {"candidates": [{"name": model, "quality": {"v": 0.5, "src": "tier_default"}}]}
    records = [{"request_id": f"q{i}", "ts": "2026-09-20T00:00:00+00:00", "status": "ok",
                "endpoint_registry_version": "test", "workload": None, "chosen": model,
                "cache_hit": False, "scoring": scoring,
                "feedback": [{"score_name": "route_correct", "value": 1 if i < 36 else 0,
                              "source": "admin", "ts": "2026-09-20T00:00:00+00:00"}]}
               for i in range(40)]
    rec = build_recommendation(estimate(records, max_ci_width=1.0), export_sha256="e" * 64)
    path = tmp_path / "recommendation.json"
    path.write_text(dump_recommendation(rec), encoding="utf-8")
    return rec, path


def _run_with_snapshot(snapshot: dict, tmp_path, *extra) -> tuple[int, object]:
    snap = tmp_path / "snapshot.json"
    snap.write_text(json.dumps(snapshot), encoding="utf-8")
    out = tmp_path / "out"
    code = load_cli().main(["--export", str(EXPORT), "--snapshot", str(snap),
                            "--proposal", str(PROPOSAL), "--out", str(out),
                            "--config-dir", str(ROOT / "config"), *extra])
    return code, out


def test_quality_posteriors_end_to_end(tmp_path, capsys):
    rec, path = _recommendation(tmp_path)
    snapshot = json.loads(SNAPSHOT.read_text())
    snapshot["quality"]["calibrated"] = [{"model": "local-cheap", "workload": None,
                                          "score": 0.82, "set_id": rec["set_id"]}]
    code, out = _run_with_snapshot(snapshot, tmp_path, *FAST, "--quality-posteriors", str(path))
    assert code == 0 and "warning" not in capsys.readouterr().err
    doc = json.loads((out / "simulation.json").read_text())
    assert doc["uncertainty"]["quality_posterior_sets"] == [rec["set_id"]]
    assert len(doc["inputs"]["uncertainty"]["quality_posteriors_sha256"]) == 1
    coverage = doc["uncertainty"]["quality_posterior"]["proposed"]
    assert coverage["posterior"] == coverage["calibrated"] > 0
    quality = doc["uncertainty"]["totals"]["quality_mean"]["proposed"]
    assert quality["hi"] - quality["lo"] > 0.05
    report = (out / "report.md").read_text()
    covered = doc["uncertainty"]["quality_posterior"]["current"]["posterior"]
    assert f"Quality uncertainty covers {covered} of" in report
    assert "the rest use a point prior." in report


def test_a_posterior_file_for_another_set_warns_and_adds_nothing(tmp_path, capsys):
    rec, path = _recommendation(tmp_path)
    out = tmp_path / "out"
    assert run_mode("proposal", out, *FAST, "--quality-posteriors", str(path)) == 0
    err = capsys.readouterr().err
    assert f"--quality-posteriors set {rec['set_id']} is not a calibration set" in err
    coverage = _doc("proposal", out)["uncertainty"]["quality_posterior"]
    assert coverage["current"]["posterior"] == coverage["proposed"]["posterior"] == 0


@pytest.mark.parametrize("text,needle", [("not json", "invalid JSON"),
                                         ('{"schema": "x"}', "schema")])
def test_a_bad_posterior_file_exits_2_naming_it(text, needle, tmp_path, capsys):
    bad = tmp_path / "bad.json"
    bad.write_text(text)
    out = tmp_path / "out"
    assert run_mode("proposal", out, "--uncertainty", "--quality-posteriors", str(bad)) == 2
    err = capsys.readouterr().err
    assert needle in err and str(bad) in err and "Traceback" not in err
    assert not out.exists()


def test_a_missing_posterior_file_exits_2(tmp_path, capsys):
    out = tmp_path / "out"
    assert run_mode("proposal", out, "--uncertainty", "--quality-posteriors",
                    str(tmp_path / "absent.json")) == 2
    assert "absent.json" in capsys.readouterr().err and not out.exists()


def test_quality_posteriors_without_uncertainty_exits_2(tmp_path, capsys):
    _rec, path = _recommendation(tmp_path)
    out = tmp_path / "out"
    assert run_mode("proposal", out, "--quality-posteriors", str(path)) == 2
    assert "--quality-posteriors given without --uncertainty" in capsys.readouterr().err
