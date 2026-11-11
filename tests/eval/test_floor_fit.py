"""Tests for `app.eval.floor_fit` (B80 Task 9): the seeded, workload-stratified
calibration / held-out split, and bounds fitted from the calibration split's
matrix rows by the REAL calibration estimator (`app.eval.calibration.estimate`,
default gates) over records read back through the REAL outcome-export loader
(`app.eval.outcomes.load_export`).
"""
from __future__ import annotations

import json
import subprocess
import sys
from collections import Counter
from pathlib import Path

import pytest

from app.config.loader import load_settings
from app.eval.calibration import estimate
from app.eval.cases import Case
from app.eval.floor_fit import (
    BOUNDS_SCHEMA,
    export_lines,
    fit_bounds,
    load_floor_bounds,
    load_matrix,
    split_cases,
    suite_hash,
)
from app.eval.oracle import MatrixRow
from app.eval.outcomes import load_export

REPO_ROOT = Path(__file__).resolve().parents[2]


SECRET_TEXT = "zebra-quartz-prompt"


def _case(cid: str, workload: str | None = "faq") -> Case:
    tags = {"workload": workload} if workload is not None else {}
    return Case(id=cid, messages=[{"role": "user", "content": SECRET_TEXT}],
                grader={"kind": "contains", "expected": "x"}, tags=tags)


def _row(cid: str, endpoint: str, quality: float, *, error: str | None = None) -> MatrixRow:
    return MatrixRow(case_id=cid, endpoint=endpoint, quality=quality,
                     passed=error is None and quality >= 1.0,
                     cost_usd=0.0 if error else 0.001,
                     latency_ms=None if error else 100.0, error=error)


@pytest.fixture(scope="module")
def settings():
    return load_settings()


# --------------------------------------------------------------------------
# split_cases
# --------------------------------------------------------------------------

def _mixed_cases() -> list[Case]:
    cases = [_case(f"faq-{i}", "faq") for i in range(11)]
    cases += [_case(f"claim-{i}", "claim") for i in range(6)]
    cases += [_case(f"none-{i}", None) for i in range(3)]
    return cases


def test_split_is_deterministic_disjoint_and_covering():
    cases = _mixed_cases()
    calib, held = split_cases(cases, seed=7)
    assert (calib, held) == split_cases(cases, seed=7)
    assert set(calib).isdisjoint(held)
    assert sorted(calib + held) == sorted(c.id for c in cases)
    # the order of the suite does not change the split
    assert split_cases(list(reversed(cases)), seed=7) == (calib, held)


def test_split_is_stratified_by_workload_within_one():
    cases = _mixed_cases()
    calib, held = split_cases(cases, seed=3)
    wl = {c.id: c.tags.get("workload") for c in cases}
    n_calib = Counter(wl[i] for i in calib)
    n_held = Counter(wl[i] for i in held)
    for w in ("faq", "claim", None):
        assert abs(n_calib[w] - n_held[w]) <= 1, w


def test_split_depends_on_the_seed():
    cases = [_case(f"faq-{i}") for i in range(40)]
    assert split_cases(cases, seed=0) != split_cases(cases, seed=1)


# --------------------------------------------------------------------------
# export-shaped records: round trip through the real loader
# --------------------------------------------------------------------------

def test_export_lines_round_trip_through_load_export(tmp_path, settings):
    cases = [_case("a", "faq"), _case("b", "claim"), _case("c", None)]
    rows = [_row("a", "ep-1", 1.0), _row("b", "ep-1", 0.25),
            _row("c", "ep-2", 0.5), _row("a", "ep-2", 0.0, error="BackendUnavailable: x")]
    lines = export_lines(rows, cases, {"a", "b", "c"}, settings)
    path = tmp_path / "export.ndjson"
    path.write_text("".join(json.dumps(line) + "\n" for line in lines))
    records, warnings = load_export(str(path))
    assert warnings == []
    assert len(records) == 4
    by_id = {r["request_id"]: r for r in records}
    ok = by_id["a:ep-1"]
    assert ok["chosen"] == "ep-1" and ok["status"] == "ok" and ok["workload"] == "faq"
    assert ok["cache_hit"] is False
    assert [(f["score_name"], f["value"]) for f in ok["feedback"]] == [("quality", 1.0)]
    # the prior the estimator reads: the model's published/tier_default term
    (cand,) = ok["scoring"]["candidates"]
    assert cand["name"] == "ep-1" and cand["quality"]["src"] in ("published", "tier_default")
    assert by_id["c:ep-2"]["workload"] is None
    # an error row is a failed, unserved request: the estimator excludes it
    failed = by_id["a:ep-2"]
    assert failed["status"] == "backend_failed" and failed["chosen"] is None
    assert failed["feedback"] == []
    # and the estimator accepts the shape: one unit per (model, workload) + (model, *)
    units = {(u["model"], u["workload"]) for u in estimate(records)["units"]}
    assert units == {("ep-1", "faq"), ("ep-1", "claim"), ("ep-1", None),
                     ("ep-2", None)}


def test_export_lines_use_only_calibration_cases(settings):
    cases = [_case("a"), _case("b")]
    rows = [_row("a", "ep-1", 1.0), _row("b", "ep-1", 1.0)]
    lines = export_lines(rows, cases, {"a"}, settings)
    assert [line["outcome"]["request_id"] for line in lines] == ["a:ep-1"]


def test_export_records_are_content_free(settings):
    lines = export_lines([_row("a", "ep-1", 1.0)], [_case("a")], {"a"}, settings)
    blob = json.dumps(lines)
    assert SECRET_TEXT not in blob   # the case's message text never appears


# --------------------------------------------------------------------------
# fit_bounds
# --------------------------------------------------------------------------

def _synthetic(n: int = 60) -> tuple[list[Case], list[MatrixRow]]:
    cases = [_case(f"c{i:02d}") for i in range(n)]
    rows: list[MatrixRow] = []
    for i, c in enumerate(cases):
        rows.append(_row(c.id, "always", 1.0))
        rows.append(_row(c.id, "never", 0.0))
        if i < 12:     # too few labels for the estimator's min_labels=20
            rows.append(_row(c.id, "sparse", 1.0))
    return cases, rows


def test_fit_bounds_always_passing_endpoint_clears_half(settings):
    cases, rows = _synthetic()
    calib, _ = split_cases(cases, seed=0)
    doc = fit_bounds(rows, cases, set(calib), settings, seed=0)
    assert doc["schema"] == BOUNDS_SCHEMA and doc["seed"] == 0
    bounds = {(b["model"], b["workload"]): b["lower"] for b in doc["bounds"]}
    assert bounds[("always", "faq")] > 0.5
    assert bounds[("always", None)] > 0.5
    assert bounds[("never", "faq")] < 0.2


def test_fit_bounds_respects_the_estimators_label_gate(settings):
    cases, rows = _synthetic()
    calib, _ = split_cases(cases, seed=0)
    doc = fit_bounds(rows, cases, set(calib), settings, seed=0)
    assert not [b for b in doc["bounds"] if b["model"] == "sparse"]


def test_fit_bounds_ignores_held_out_rows(settings):
    cases, rows = _synthetic()
    calib, held = split_cases(cases, seed=0)
    # Corrupt every held-out row of `always`: the fit must not change.
    poisoned = [(_row(r.case_id, r.endpoint, 0.0)
                 if r.endpoint == "always" and r.case_id in set(held) else r)
                for r in rows]
    assert (fit_bounds(poisoned, cases, set(calib), settings, seed=0)
            == fit_bounds(rows, cases, set(calib), settings, seed=0))


def test_fit_bounds_equals_estimate_over_the_loaded_export(tmp_path, settings):
    """`fit_bounds` keeps exactly the `ci_low` of the estimator's
    recommended / no_change units -- nothing re-derived."""
    cases, rows = _synthetic()
    calib, _ = split_cases(cases, seed=0)
    path = tmp_path / "export.ndjson"
    path.write_text("".join(json.dumps(line) + "\n"
                            for line in export_lines(rows, cases, set(calib), settings)))
    records, _ = load_export(str(path))
    expected = sorted(
        ({"model": u["model"], "workload": u["workload"], "lower": u["ci_low"]}
         for u in estimate(records)["units"]
         if u["status"] in ("recommended", "no_change")),
        key=lambda b: (b["model"], b["workload"] is None, b["workload"] or ""))
    assert fit_bounds(rows, cases, set(calib), settings, seed=0)["bounds"] == expected


# --------------------------------------------------------------------------
# bounds / matrix files
# --------------------------------------------------------------------------

def test_load_floor_bounds_round_trip(tmp_path, settings):
    cases, rows = _synthetic()
    calib, _ = split_cases(cases, seed=4)
    doc = fit_bounds(rows, cases, set(calib), settings, seed=4)
    path = tmp_path / "bounds.json"
    path.write_text(json.dumps(doc))
    mapping, seed, digest = load_floor_bounds(str(path))
    assert seed == 4 and digest == suite_hash(cases)
    assert mapping == {(b["model"], b["workload"]): b["lower"] for b in doc["bounds"]}


@pytest.mark.parametrize("doc", [
    {"schema": "routeforge.floor-bounds/v2", "seed": 0, "bounds": []},
    {"schema": BOUNDS_SCHEMA, "bounds": []},
    {"schema": BOUNDS_SCHEMA, "seed": 0, "bounds": [{"model": "m", "workload": None,
                                                     "lower": 1.5}]},
    {"schema": BOUNDS_SCHEMA, "seed": 0, "bounds": [{"workload": None, "lower": 0.5}]},
    {"schema": BOUNDS_SCHEMA, "seed": 0, "suite_hash": 7, "bounds": []},
    {"schema": BOUNDS_SCHEMA, "seed": 0, "bounds": [
        {"model": "m", "workload": "a", "lower": 0.5},
        {"model": "m", "workload": "a", "lower": 0.6}]},
])
def test_load_floor_bounds_refuses_bad_files(tmp_path, doc):
    path = tmp_path / "bounds.json"
    path.write_text(json.dumps(doc))
    with pytest.raises(ValueError):
        load_floor_bounds(str(path))


def test_load_matrix_reads_runner_lines(tmp_path):
    path = tmp_path / "matrix.jsonl"
    path.write_text(json.dumps({
        "case_id": "a", "endpoint": "ep", "served": "ep", "quality": 1.0, "passed": True,
        "cost_usd": 0.1, "latency_ms": 5.0, "prompt_tokens": 3,
        "cached_prompt_tokens": 0, "error": None, "error_class": None,
        "counts_unhealth": None, "failure_latency_ms": None}) + "\n")
    assert load_matrix(str(path)) == [MatrixRow(
        case_id="a", endpoint="ep", quality=1.0, passed=True, cost_usd=0.1,
        latency_ms=5.0)]


def test_cli_writes_a_bounds_file(tmp_path):
    cases, rows = _synthetic()
    suite = tmp_path / "suite.jsonl"
    suite.write_text("".join(c.model_dump_json() + "\n" for c in cases))
    matrix = tmp_path / "matrix.jsonl"
    matrix.write_text("".join(json.dumps({
        "case_id": r.case_id, "endpoint": r.endpoint, "quality": r.quality,
        "passed": r.passed, "cost_usd": r.cost_usd, "latency_ms": r.latency_ms,
        "error": r.error}) + "\n" for r in rows))
    out = tmp_path / "bounds.json"
    proc = subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / "floor_fit.py"),
         "--matrix", str(matrix), "--suite", str(suite), "--seed", "5",
         "--out", str(out)],
        cwd=REPO_ROOT, capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stderr
    doc = json.loads(out.read_text())
    assert doc["schema"] == BOUNDS_SCHEMA and doc["seed"] == 5
    assert any(b["model"] == "always" for b in doc["bounds"])


def test_cli_missing_matrix_exits_1(tmp_path):
    proc = subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / "floor_fit.py"),
         "--matrix", str(tmp_path / "nope.jsonl"), "--suite", str(tmp_path / "s.jsonl"),
         "--seed", "0", "--out", str(tmp_path / "b.json")],
        cwd=REPO_ROOT, capture_output=True, text=True, timeout=120)
    assert proc.returncode == 1
    assert not (tmp_path / "b.json").exists()


# --------------------------------------------------------------------------
# suite_hash
# --------------------------------------------------------------------------

def test_suite_hash_is_order_independent_and_content_sensitive():
    cases = _mixed_cases()
    assert suite_hash(cases) == suite_hash(list(reversed(cases)))
    changed = [c.model_copy(update={"quality_floor": 0.5}) if c.id == "faq-0" else c
               for c in cases]
    assert suite_hash(changed) != suite_hash(cases)
    assert suite_hash(cases[1:]) != suite_hash(cases)


def test_fit_bounds_stamps_the_suite_hash(settings):
    cases, rows = _synthetic()
    calib, _ = split_cases(cases, seed=0)
    doc = fit_bounds(rows, cases, set(calib), settings, seed=0)
    assert doc["suite_hash"] == suite_hash(cases)


def test_load_floor_bounds_without_suite_hash_returns_none(tmp_path):
    path = tmp_path / "bounds.json"
    path.write_text(json.dumps({"schema": BOUNDS_SCHEMA, "seed": 0, "bounds": []}))
    assert load_floor_bounds(str(path)) == ({}, 0, None)
