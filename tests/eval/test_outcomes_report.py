"""Tests for `app.eval.outcomes` (P2b-2 Task 7): the offline loader and
report over the admin NDJSON outcome export.

`tests/eval/fixtures/outcomes_export.ndjson` is a hand-built 12-line export,
one `endpoint_registry_version` ("reg-v1"), across two named workloads
("chat", "summarize") and one untagged workload (`workload: null`), three
endpoints (ep-cheap, ep-mid, ep-frontier), with one `no_eligible` row
(req-9), one failover row with `len(attempts) > 1` (req-4), and one
`verify_failed` row (req-5). Every expected value below is computed by
hand from that file, not from the implementation.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from app.eval.outcomes import (
    CAVEAT, load_export, render_report, summarize,
)

FIXTURES = Path(__file__).parent / "fixtures"
GOLDEN_EXPORT = FIXTURES / "outcomes_export.ndjson"
REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "outcome_report.py"


# --------------------------------------------------------------------------
# load_export
# --------------------------------------------------------------------------

def test_load_export_reads_every_line():
    records, warnings = load_export(str(GOLDEN_EXPORT))
    assert warnings == []
    assert len(records) == 12
    assert {r["request_id"] for r in records} == {f"req-{i}" for i in range(1, 13)}


def test_load_export_attaches_feedback_to_each_record():
    records, _ = load_export(str(GOLDEN_EXPORT))
    by_id = {r["request_id"]: r for r in records}
    assert by_id["req-1"]["feedback"] == [
        {"score_name": "quality", "value": 1.0, "source": "admin",
         "ts": "2026-09-20T00:05:00+00:00"},
        {"score_name": "route_correct", "value": 1.0, "source": "admin",
         "ts": "2026-09-20T00:05:00+00:00"},
    ]
    assert by_id["req-3"]["feedback"] == []


def test_load_export_skips_blank_lines_and_warns_on_bad_json(tmp_path):
    path = tmp_path / "export.ndjson"
    path.write_text(
        '{"schema":"routeforge.outcome/v1","outcome":{"request_id":"a"},"feedback":[]}\n'
        "\n"
        "not json at all\n"
        '{"schema":"routeforge.outcome/v1","outcome":{"request_id":"b"},"feedback":[]}\n',
        encoding="utf-8")
    records, warnings = load_export(str(path))
    assert [r["request_id"] for r in records] == ["a", "b"]
    assert len(warnings) == 1
    assert "line 3" in warnings[0]


def test_load_export_raises_valueerror_on_unknown_schema_major(tmp_path):
    path = tmp_path / "export.ndjson"
    path.write_text(
        '{"schema":"routeforge.outcome/v2","outcome":{"request_id":"a"},"feedback":[]}\n',
        encoding="utf-8")
    with pytest.raises(ValueError):
        load_export(str(path))


def test_load_export_v1_minor_suffix_is_still_v1(tmp_path):
    """`routeforge.outcome/v1.1` -- an additive minor bump -- must not raise;
    only the major component gates acceptance."""
    path = tmp_path / "export.ndjson"
    path.write_text(
        '{"schema":"routeforge.outcome/v1.1","outcome":{"request_id":"a"},"feedback":[]}\n',
        encoding="utf-8")
    records, warnings = load_export(str(path))
    assert warnings == []
    assert len(records) == 1


def test_load_export_raises_valueerror_on_missing_schema_field(tmp_path):
    """No `schema` key at all is treated the same as an unrecognized one --
    this loader must not guess what an unlabelled line's format is."""
    path = tmp_path / "export.ndjson"
    path.write_text(
        '{"outcome":{"request_id":"a"},"feedback":[]}\n', encoding="utf-8")
    with pytest.raises(ValueError):
        load_export(str(path))


def test_load_export_skips_a_line_missing_the_outcome_key(tmp_path):
    path = tmp_path / "export.ndjson"
    path.write_text(
        '{"schema":"routeforge.outcome/v1","feedback":[]}\n'
        '{"schema":"routeforge.outcome/v1","outcome":{"request_id":"b"},"feedback":[]}\n',
        encoding="utf-8")
    records, warnings = load_export(str(path))
    assert [r["request_id"] for r in records] == ["b"]
    assert len(warnings) == 1
    assert "line 1" in warnings[0]
    assert "outcome" in warnings[0]


def test_load_export_empty_file_returns_no_records_and_no_warnings(tmp_path):
    path = tmp_path / "export.ndjson"
    path.write_text("", encoding="utf-8")
    records, warnings = load_export(str(path))
    assert records == []
    assert warnings == []


def test_summarize_of_an_empty_export_is_empty_but_well_formed():
    assert summarize([]) == {"n_records": 0, "registry_versions": {}}


def test_report_of_an_empty_export_still_starts_with_the_caveat():
    report = render_report(summarize([]))
    assert report.startswith(CAVEAT)
    assert "n records: 0" in report


# --------------------------------------------------------------------------
# summarize -- golden values, hand-computed
# --------------------------------------------------------------------------

@pytest.fixture(scope="module")
def golden_summary():
    records, warnings = load_export(str(GOLDEN_EXPORT))
    assert warnings == []
    return summarize(records)


def test_summarize_top_level(golden_summary):
    assert golden_summary["n_records"] == 12
    assert list(golden_summary["registry_versions"]) == ["reg-v1"]
    assert golden_summary["registry_versions"]["reg-v1"]["n"] == 12


def test_summarize_chat_ep_cheap_bucket(golden_summary):
    # req-1, req-2, req-3: all ok, latencies [100, 110, 120] -> nearest-rank
    # p50 = sorted[ceil(0.5*3)-1] = sorted[1] = 110, p95 = sorted[ceil(2.85)-1] = sorted[2] = 120
    bucket = golden_summary["registry_versions"]["reg-v1"]["by_workload_chosen"]["chat"]["ep-cheap"]
    assert bucket == {
        "n": 3,
        "served_ok_rate": 1.0,
        "verify_pass_rate": 1.0,          # 2/2 (req-1, req-2 verify_performed=True, both passed)
        "quality_mean": 0.9,              # mean(1.0, 0.8)
        "quality_n": 2,
        "route_correct_rate": 1.0,        # mean(1.0)
        "route_correct_n": 1,
        "cost_mean": 0.0015,              # mean(0.001, 0.002, 0.0015) -- all 3 rows are ok
        "latency_p50": 110.0,
        "latency_p95": 120.0,
        "failover_rate": 0.0,
    }


def test_summarize_chat_ep_mid_bucket_has_the_failover_and_the_verify_failure(golden_summary):
    # req-4 (ok, failover: attempts=[ep-cheap, ep-mid]), req-5 (verify_failed)
    bucket = golden_summary["registry_versions"]["reg-v1"]["by_workload_chosen"]["chat"]["ep-mid"]
    assert bucket == {
        "n": 2,
        "served_ok_rate": 0.5,            # 1/2 (only req-4 is ok)
        "verify_pass_rate": 0.0,          # 0/2, both verify_performed=True but verify_passed=False
        "quality_mean": None,
        "quality_n": 0,
        "route_correct_rate": 0.0,        # mean(0.0)
        "route_correct_n": 1,
        "cost_mean": 0.01,                # mean(0.01) -- ok rows only, so req-5's
                                           # verify_failed cost (also 0.01) is excluded
        "latency_p50": 200.0,             # only req-4 is ok
        "latency_p95": 200.0,
        "failover_rate": 0.5,             # 1/2
    }


def test_summarize_summarize_ep_frontier_bucket(golden_summary):
    bucket = golden_summary["registry_versions"]["reg-v1"]["by_workload_chosen"]["summarize"]["ep-frontier"]
    assert bucket["n"] == 2
    assert bucket["served_ok_rate"] == 1.0
    assert bucket["verify_pass_rate"] == 1.0
    assert bucket["quality_mean"] == 0.925   # mean(0.9, 0.95)
    assert bucket["quality_n"] == 2
    assert bucket["latency_p50"] == 300.0
    assert bucket["latency_p95"] == 320.0
    assert bucket["failover_rate"] == 0.0


def test_summarize_no_eligible_row_lands_in_unchosen_bucket_with_no_latency(golden_summary):
    # req-9: workload=summarize, chosen=None -> "(none)"; status=no_eligible so
    # served_ok_rate=0 and it is excluded from the ok-only latency percentiles.
    bucket = golden_summary["registry_versions"]["reg-v1"]["by_workload_chosen"]["summarize"]["(none)"]
    assert bucket["n"] == 1
    assert bucket["served_ok_rate"] == 0.0
    assert bucket["verify_pass_rate"] is None
    # req-9's cost_usd is 0.0 but its status is no_eligible, not ok, so it is
    # excluded from cost_mean too -- an unserved row must not read as a free win.
    assert bucket["cost_mean"] is None
    assert bucket["latency_p50"] is None
    assert bucket["latency_p95"] is None
    assert bucket["failover_rate"] == 0.0


def test_summarize_untagged_workload_bucket(golden_summary):
    # req-10 (ok, cost 0.001), req-11 (backend_failed, cost 0.0 -- excluded
    # from both cost_mean and latency, which are ok-rows-only)
    bucket = golden_summary["registry_versions"]["reg-v1"]["by_workload_chosen"]["(untagged)"]["ep-cheap"]
    assert bucket["n"] == 2
    assert bucket["served_ok_rate"] == 0.5
    assert bucket["verify_pass_rate"] == 1.0
    assert bucket["quality_mean"] == 1.0
    assert bucket["quality_n"] == 1
    assert bucket["cost_mean"] == 0.001      # mean(0.001) -- req-11's cost_usd=0.0
                                              # is dropped since its status != ok
    assert bucket["latency_p50"] == 90.0
    assert bucket["latency_p95"] == 90.0


def test_summarize_by_workload_chat(golden_summary):
    # rows 1-5; feedback present on req-1, req-2, req-4 -> 3/5
    wl = golden_summary["registry_versions"]["reg-v1"]["by_workload"]["chat"]
    assert wl == {
        "n": 5,
        "no_eligible_rate": 0.0,
        "backend_failed_rate": 0.0,
        "label_coverage": 0.6,
    }


def test_summarize_by_workload_summarize_has_the_no_eligible_row(golden_summary):
    # rows 6-9; req-9 is no_eligible; feedback present on req-6, req-7 -> 2/4
    wl = golden_summary["registry_versions"]["reg-v1"]["by_workload"]["summarize"]
    assert wl == {
        "n": 4,
        "no_eligible_rate": 0.25,
        "backend_failed_rate": 0.0,
        "label_coverage": 0.5,
    }


def test_summarize_by_workload_untagged_has_the_backend_failed_row(golden_summary):
    # rows 10-12; req-11 is backend_failed; feedback present on req-10, req-12 -> 2/3
    wl = golden_summary["registry_versions"]["reg-v1"]["by_workload"]["(untagged)"]
    assert wl == {
        "n": 3,
        "no_eligible_rate": 0.0,
        "backend_failed_rate": round(1 / 3, 6),
        "label_coverage": round(2 / 3, 6),
    }


def test_summarize_floats_are_rounded_to_6dp():
    records = [
        {"endpoint_registry_version": "v", "workload": "w", "chosen": "c",
         "status": "ok", "cost_usd": 1 / 3, "latency_ms": 1.0, "attempts": ["c"],
         "feedback": []},
        {"endpoint_registry_version": "v", "workload": "w", "chosen": "c",
         "status": "ok", "cost_usd": 1 / 3, "latency_ms": 1.0, "attempts": ["c"],
         "feedback": []},
        {"endpoint_registry_version": "v", "workload": "w", "chosen": "c",
         "status": "ok", "cost_usd": 1 / 3, "latency_ms": 1.0, "attempts": ["c"],
         "feedback": []},
    ]
    summary = summarize(records)
    bucket = summary["registry_versions"]["v"]["by_workload_chosen"]["w"]["c"]
    assert bucket["cost_mean"] == round(1 / 3, 6)


# --------------------------------------------------------------------------
# registry-version grouping
# --------------------------------------------------------------------------

def test_mixed_registry_versions_produce_separate_groups():
    records = [
        {"endpoint_registry_version": "v1", "workload": "a", "chosen": "x",
         "status": "ok", "cost_usd": 1.0, "latency_ms": 1.0, "attempts": ["x"],
         "feedback": []},
        {"endpoint_registry_version": "v2", "workload": "a", "chosen": "x",
         "status": "ok", "cost_usd": 2.0, "latency_ms": 2.0, "attempts": ["x"],
         "feedback": []},
        {"endpoint_registry_version": None, "workload": "a", "chosen": "x",
         "status": "ok", "cost_usd": 3.0, "latency_ms": 3.0, "attempts": ["x"],
         "feedback": []},
    ]
    summary = summarize(records)
    assert set(summary["registry_versions"]) == {"v1", "v2", "(unknown)"}
    # never pooled: each group has exactly its own row, not all three
    assert summary["registry_versions"]["v1"]["n"] == 1
    assert summary["registry_versions"]["v1"]["by_workload_chosen"]["a"]["x"]["cost_mean"] == 1.0
    assert summary["registry_versions"]["v2"]["by_workload_chosen"]["a"]["x"]["cost_mean"] == 2.0
    assert summary["registry_versions"]["(unknown)"]["by_workload_chosen"]["a"]["x"]["cost_mean"] == 3.0


# --------------------------------------------------------------------------
# render_report
# --------------------------------------------------------------------------

def test_report_starts_with_the_caveat_verbatim(golden_summary):
    report = render_report(golden_summary)
    assert report.startswith(CAVEAT)
    assert CAVEAT == (
        "Production observes only the served endpoint for each request, so "
        "oracle regret is not computable from these records and is not "
        "reported. Endpoint comparisons are confounded by the router's own "
        "choices; treat them as descriptive, not causal.")


def test_report_never_mentions_regret(golden_summary):
    report = render_report(golden_summary)
    # the caveat itself names "regret" to explain why it's absent; nothing
    # else in the report may compute or label a regret figure.
    after_caveat = report[len(CAVEAT):]
    assert "regret" not in after_caveat.lower()


def test_report_lists_the_registry_version_and_workloads(golden_summary):
    report = render_report(golden_summary)
    assert "endpoint_registry_version: reg-v1" in report
    assert "chat" in report
    assert "summarize" in report
    assert "(untagged)" in report
    assert "(none)" in report


# --------------------------------------------------------------------------
# isolation: app/eval/outcomes.py must not import app.api or a DB
# --------------------------------------------------------------------------

def test_outcomes_module_has_no_db_or_api_imports():
    import ast
    src = (REPO_ROOT / "app" / "eval" / "outcomes.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    names = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names += [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.append(node.module)
    forbidden = [n for n in names if n.startswith("app.api") or "sqlalchemy" in n
                 or n.startswith("app.apps.store") or n == "app.config.loader"]
    assert forbidden == []


# --------------------------------------------------------------------------
# CLI: scripts/outcome_report.py
# --------------------------------------------------------------------------

def _run_cli(export: Path, out: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--export", str(export), "--out", str(out)],
        capture_output=True, text=True, check=False)


def test_cli_writes_summary_and_report(tmp_path):
    out = tmp_path / "out"
    result = _run_cli(GOLDEN_EXPORT, out)
    assert result.returncode == 0, result.stderr
    assert (out / "summary.json").exists()
    assert (out / "report.md").exists()
    summary = json.loads((out / "summary.json").read_text(encoding="utf-8"))
    assert summary["n_records"] == 12
    report = (out / "report.md").read_text(encoding="utf-8")
    assert report.startswith(CAVEAT)


def test_cli_summary_json_is_deterministic_across_runs(tmp_path):
    out1, out2 = tmp_path / "run1", tmp_path / "run2"
    r1 = _run_cli(GOLDEN_EXPORT, out1)
    r2 = _run_cli(GOLDEN_EXPORT, out2)
    assert r1.returncode == 0 and r2.returncode == 0
    bytes1 = (out1 / "summary.json").read_bytes()
    bytes2 = (out2 / "summary.json").read_bytes()
    assert bytes1 == bytes2


def test_cli_exits_1_on_unknown_schema_major(tmp_path):
    bad = tmp_path / "bad.ndjson"
    bad.write_text(
        '{"schema":"routeforge.outcome/v2","outcome":{"request_id":"a"},"feedback":[]}\n',
        encoding="utf-8")
    result = _run_cli(bad, tmp_path / "out")
    assert result.returncode == 1
    assert "load error" in result.stderr
