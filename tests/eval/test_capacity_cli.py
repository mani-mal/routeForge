"""P3c-4: `scripts/policy_simulate.py` with a stated capacity, end to end
(spec §3, §7, §8; invariants C1, C2, C8, C9, C13-C15, C20).

Runs the CLI in-process. "Capacity only adds" is checked against the outputs
pinned from before P3c-3 (`tests/eval/test_uncertainty_golden.py`), which
this slice does not regenerate.
"""
from __future__ import annotations

import hashlib
import json

import pytest

from app.eval.capacity import NO_RANGE_NOTE, SCHEMA, caveat_lines, render_capacity_section
from tests.eval.test_capacity import LIMITS, QUIET_THEN_BUSY, one_endpoint, traffic
from tests.eval.test_capacity_report import unpaired_peaks
from tests.eval.test_uncertainty_golden import (EXPECTED, MODES, ROOT, _dump, _export_lines,
                                                load_cli, run_mode)

P = "routeforge.policy-proposal/v1"
#: Limits for the golden `routes` case's two endpoints.
GOLDEN_LIMITS = {"local-mid": {"max_concurrency": 2, "requests_per_minute": 30},
                 "local-cheap": {"tokens_per_minute": 50000}}
_ID = {"proposal": "simulation_id", "proposal_shadow": "simulation_id",
       "scenarios": "comparison_id"}


def _capacity_file(tmp_path, endpoints=None, name="capacity.json", **extra) -> str:
    path = tmp_path / name
    path.write_text(_dump({"schema": SCHEMA, "endpoints":
                           GOLDEN_LIMITS if endpoints is None else endpoints, **extra}),
                    encoding="utf-8")
    return str(path)


def _doc(mode: str, out) -> dict:
    return json.loads((out / MODES[mode][1][0]).read_text())


def _expected(mode: str) -> dict:
    return json.loads((EXPECTED / mode / MODES[mode][1][0]).read_text())


def _strip(doc: dict) -> dict:
    """`doc` without everything a stated capacity adds, and without its id
    (a hash of `inputs`, which capacity extends on purpose)."""
    doc = json.loads(json.dumps(doc))
    doc.pop("capacity", None)
    doc["inputs"].pop("capacity", None)
    doc.pop("simulation_id", None)
    doc.pop("comparison_id", None)
    for row in doc.get("table", []):
        for key in ("peak_utilisation", "peak_mean_window", "peak_endpoint", "peak_limit"):
            row.pop(key, None)
    return doc


def _own_run(tmp_path, body: dict, *extra: str, rows=None, capacity: dict | None = None,
             snapshot: dict | None = None):
    """Run `--proposal` over a hand-made export. Returns `(exit code, out)`."""
    export, snap, prop = tmp_path / "e.ndjson", tmp_path / "s.json", tmp_path / "p.json"
    export.write_text(_export_lines(traffic(QUIET_THEN_BUSY) if rows is None else rows),
                      encoding="utf-8")
    snap.write_text(_dump(snapshot or one_endpoint()), encoding="utf-8")
    prop.write_text(_dump({"schema": P, **body}), encoding="utf-8")
    flags = []
    if capacity is not None:
        flags = ["--capacity", _capacity_file(tmp_path, capacity)]
    out = tmp_path / "out"
    code = load_cli().main(["--export", str(export), "--snapshot", str(snap),
                            "--proposal", str(prop), "--out", str(out),
                            "--config-dir", str(ROOT / "config"), *flags, *extra])
    return code, out


# --- C1: a stated capacity only adds ----------------------------------------------

@pytest.mark.parametrize("mode", ["proposal", "proposal_shadow", "scenarios"])
def test_c1_a_capacity_file_adds_keys_and_changes_no_existing_value(mode, tmp_path):   # C1
    out = tmp_path / "out"
    assert run_mode(mode, out, "--capacity", _capacity_file(tmp_path)) == 0
    doc = _doc(mode, out)
    assert _strip(doc) == _strip(_expected(mode))
    assert "capacity" in doc and "capacity" in doc["inputs"]
    assert doc[_ID[mode]] != _expected(mode)[_ID[mode]]


@pytest.mark.parametrize("mode", ["proposal", "proposal_shadow"])
def test_c1_the_report_is_the_old_report_plus_one_section(mode, tmp_path):             # C1
    out = tmp_path / "out"
    assert run_mode(mode, out, "--capacity", _capacity_file(tmp_path)) == 0
    text = (out / "report.md").read_text()
    section = render_capacity_section(_doc(mode, out)["capacity"])
    assert text.count("## Capacity") == 1 and section in text
    assert text.replace("\n" + section, "", 1) == (EXPECTED / mode / "report.md").read_text()
    if mode == "proposal_shadow":
        assert text.index("## Capacity") < text.index("## Shadow prices")


def test_c1_shadow_only_output_is_untouched_and_takes_no_capacity(tmp_path, capsys):   # C1, C15
    out = tmp_path / "out"
    assert run_mode("shadow", out, "--capacity", _capacity_file(tmp_path)) == 2
    assert "--capacity needs --proposal or --scenarios" in capsys.readouterr().err
    assert not out.exists()


# --- C13, C2: the plan is an input; the run is deterministic -----------------------

def test_c13_the_plan_is_in_inputs_and_the_id_tells_plans_apart(tmp_path):             # C13
    path = _capacity_file(tmp_path)
    sha = hashlib.sha256(open(path, "rb").read()).hexdigest()
    ids = {}
    for label, extra in (("plain", ()), ("file", ("--capacity", path)),
                         ("bucket", ("--capacity", path, "--capacity-bucket", "300")),
                         ("min", ("--capacity", path, "--capacity-min-buckets", "3")),
                         ("other", ("--capacity", _capacity_file(
                             tmp_path, {"local-mid": {"max_concurrency": 3}}, "other.json")))):
        out = tmp_path / label
        assert run_mode("proposal", out, *extra) == 0
        doc = _doc("proposal", out)
        ids[label] = doc["simulation_id"]
        if label == "plain":
            assert "capacity" not in doc["inputs"] and "capacity" not in doc
        if label == "file":
            assert doc["inputs"]["capacity"] == {"file_sha256": sha, "bucket_seconds": 60,
                                                 "min_buckets": 10}
        if label == "bucket":
            assert doc["capacity"]["bucket_seconds"] == 300
    assert len(set(ids.values())) == 5


def test_c2_the_run_is_byte_identical_twice(tmp_path):                                 # C2
    path = _capacity_file(tmp_path)
    for mode in ("proposal", "scenarios"):
        outs = []
        for attempt in ("a", "b"):
            out = tmp_path / f"{mode}-{attempt}"
            assert run_mode(mode, out, "--capacity", path) == 0
            outs.append(sorted((p.name, p.read_bytes()) for p in out.iterdir()))
        assert outs[0] == outs[1], mode


# --- C14: the hand-computed pattern, through the whole CLI --------------------------

def test_c14_the_cli_reports_the_hand_computed_utilisation(tmp_path):                  # C14
    """The pattern of `test_capacity.py::test_c14_utilisation_matches_the_closed_form`,
    from files, with twice the traffic proposed: 120% -> 240% of one
    concurrent request, 53.3% -> 106.7% on average over the window."""
    code, out = _own_run(tmp_path, {"workload": {"volume_scale": 2}}, capacity=LIMITS)
    assert code == 0
    sim = json.loads((out / "simulation.json").read_text())
    entry = sim["capacity"]["endpoints"]["local-cheap"]
    current, proposed = entry["current"]["limits"], entry["proposed"]["limits"]
    for stated, peak, mean in ((current["max_concurrency"], 1.2, 0.533333),
                               (proposed["max_concurrency"], 2.4, 1.066667)):
        assert (stated["peak"], stated["mean_window"], stated["mean_active"]) == (
            peak, mean, mean)
    assert entry["proposed"]["service_time_sources"] == {
        "recorded": 160, "endpoint_mean": 0, "replay": 0, "asserted": 0}
    assert current["max_concurrency"]["buckets_over"] == 10
    assert current["max_concurrency"]["requests_over_share"] == 0.75
    assert sim["capacity"]["peak"]["proposed"] == {
        "endpoint": "local-cheap", "limit": "output_tokens_per_minute", "utilisation": 4.8,
        "bucket": "2026-09-20T12:20:00+00:00", "mean_window": 2.133333,
        "buckets_over_share": 0.333333, "bucket_seconds": 60, "buckets": 30}
    # Volume changed nothing else: no route moved and no cost figure doubled.
    assert sim["totals"]["route_changed"] == 0 and sim["totals"]["cost_usd"]["delta"] == 0.0
    assert sim["assumptions"]["token_scale"] == {} and "volume_scale" not in sim["assumptions"]
    assert sim["edits"] == {"workload": {"volume_scale": 2}}
    text = (out / "report.md").read_text()
    assert ("| max_concurrency | proposed | 1 | 240.0% | 106.7% | 106.7% | 10 of 30 | 75.0% | "
            "2026-09-20T12:20:00+00:00 |") in text
    assert ("Peak load: current peak 240.0% / avg 106.7% (local-cheap, "
            "output_tokens_per_minute, 60 s buckets); proposed peak 480.0% / avg 213.3% "
            "(local-cheap, output_tokens_per_minute, 60 s buckets).") in text
    assert text.split("## Capacity\n\n")[1].split("\n")[:17] == caveat_lines()


def test_a_capacity_stated_only_in_the_proposal_is_enough(tmp_path):
    code, out = _own_run(tmp_path, {"capacity": {"local-cheap": {"max_concurrency": 2}}})
    assert code == 0
    sim = json.loads((out / "simulation.json").read_text())
    assert sim["inputs"]["capacity"]["file_sha256"] is None
    entry = sim["capacity"]["endpoints"]["local-cheap"]
    assert entry["current"]["limits"] == {}
    assert entry["proposed"]["limits"]["max_concurrency"]["peak"] == 0.6
    assert sim["capacity"]["peak"] == {"current": None, "proposed": {
        "endpoint": "local-cheap", "limit": "max_concurrency", "utilisation": 0.6,
        "bucket": "2026-09-20T12:20:00+00:00", "mean_window": 0.266667,
        "buckets_over_share": 0.0, "bucket_seconds": 60, "buckets": 30}}


def test_since_cuts_the_demand_with_the_rows(tmp_path):
    code, out = _own_run(tmp_path, {}, "--since", "2026-09-20T12:20:00Z", capacity=LIMITS)
    assert code == 0
    capacity = json.loads((out / "simulation.json").read_text())["capacity"]
    assert capacity["window"]["first_bucket"] == "2026-09-20T12:20:00+00:00"
    assert capacity["window"]["arrival_buckets"] == 10 and capacity["window"]["buckets"] == 10
    assert capacity["endpoints"]["local-cheap"]["current"]["requests"] == 120.0


# --- C8: a short window ---------------------------------------------------------------

def test_c8_a_short_export_says_so_in_the_json_the_table_and_the_report(tmp_path):     # C8
    path = _capacity_file(tmp_path)
    out = tmp_path / "out"
    assert run_mode("scenarios", out, "--capacity", path, "--capacity-min-buckets", "5000") == 0
    doc = _doc("scenarios", out)
    assert all(entry["window_too_short"] and entry["endpoints"] == {}
               for entry in doc["capacity"]["scenarios"])
    assert all(row["peak_utilisation"] is None for row in doc["table"])
    text = (out / "report.md").read_text()
    assert text.count("| window too short (") == len(doc["table"])
    single = tmp_path / "single"
    assert run_mode("proposal", single, "--capacity", path, "--capacity-bucket", "86400") == 0
    block = _doc("proposal", single)["capacity"]
    assert block["window_too_short"] is True and block["window"]["arrival_buckets"] == 3
    assert "No utilisation figure: window too short (3 of the 10 buckets needed)." in (
        single / "report.md").read_text()


# --- C20: no peak without the average, in what is written --------------------------------

@pytest.mark.parametrize("mode", ["proposal", "scenarios"])
def test_c20_the_written_report_never_prints_a_peak_alone(mode, tmp_path):             # C20
    out = tmp_path / "out"
    assert run_mode(mode, out, "--capacity", _capacity_file(tmp_path)) == 0
    text = (out / "report.md").read_text()
    assert " / avg " in text and unpaired_peaks(text) == []
    doc = _doc(mode, out)
    blocks = [doc["capacity"]] if mode == "proposal" else doc["capacity"]["scenarios"]
    for block in blocks:
        for arm in ("current", "proposed"):
            assert block["peak"][arm]["mean_window"] is not None
    for row in doc.get("table", []):
        assert (row["peak_utilisation"] is None) == (row["peak_mean_window"] is None)
        assert row["peak_utilisation"] is not None


# --- C9: with --uncertainty ------------------------------------------------------------

FAST = ("--uncertainty", "--bootstrap", "40")


@pytest.mark.parametrize("mode", ["proposal", "scenarios"])
def test_c9_uncertainty_leaves_capacity_as_points(mode, tmp_path):                     # C9
    path = _capacity_file(tmp_path)
    both, capacity_only, ranges_only = (tmp_path / n for n in ("both", "capacity", "ranges"))
    assert run_mode(mode, both, "--capacity", path, *FAST) == 0
    assert run_mode(mode, capacity_only, "--capacity", path) == 0
    assert run_mode(mode, ranges_only, *FAST) == 0
    doc = _doc(mode, both)
    assert doc["capacity"] == _doc(mode, capacity_only)["capacity"]
    assert doc["uncertainty"] == _doc(mode, ranges_only)["uncertainty"]
    assert not [key for key in ("lo", "hi", "point") if f'"{key}"' in json.dumps(doc["capacity"])]
    text = (both / "report.md").read_text()
    assert text.count(NO_RANGE_NOTE) == 1
    assert NO_RANGE_NOTE not in (capacity_only / "report.md").read_text()
    assert text.index("## Capacity") < text.index(NO_RANGE_NOTE)


# --- C15: nothing stated may silently do nothing ---------------------------------------

def _refused(capsys, code: int, out, needle: str) -> None:
    err = capsys.readouterr().err
    assert code == 2 and needle in err and "Traceback" not in err, err
    assert not out.exists()


def test_c15_a_tuning_flag_with_no_capacity_stated_is_refused(tmp_path, capsys):       # C15
    for flag in ("--capacity-bucket", "--capacity-min-buckets"):
        out = tmp_path / flag.strip("-")
        _refused(capsys, run_mode("proposal", out, flag, "30"), out,
                 f"{flag} given with no capacity stated")


def test_c15_a_volume_scale_with_no_capacity_stated_is_refused(tmp_path, capsys):      # C15
    code, out = _own_run(tmp_path, {"workload": {"apps": {"app-a": {"volume_scale": 4}}}})
    _refused(capsys, code, out, "workload.volume_scale is set but no capacity is stated")


@pytest.mark.parametrize("endpoints,needle", [
    ({"local-typo": {"max_concurrency": 8}}, "endpoints.local-typo: not an onboarded endpoint"),
    ({"local-cheap": {"max_concurrency": 0}}, "endpoints.local-cheap.max_concurrency: must be"),
    ({"local-cheap": {"gpus": 2}}, "endpoints.local-cheap.gpus: unknown key"),
])
def test_c15_a_bad_capacity_file_is_refused(tmp_path, capsys, endpoints, needle):      # C15
    out = tmp_path / "out"
    code = run_mode("proposal", out, "--capacity", _capacity_file(tmp_path, endpoints))
    _refused(capsys, code, out, needle)


def test_c15_a_missing_or_unreadable_capacity_file_is_refused(tmp_path, capsys):       # C15
    out = tmp_path / "out"
    _refused(capsys, run_mode("proposal", out, "--capacity", str(tmp_path / "nope.json")), out,
             "error: capacity:")
    broken = tmp_path / "broken.json"
    broken.write_text("{", encoding="utf-8")
    _refused(capsys, run_mode("proposal", out, "--capacity", str(broken)), out, "invalid JSON")


@pytest.mark.parametrize("flag,value", [("--capacity-bucket", "0"), ("--capacity-bucket", "86401"),
                                        ("--capacity-min-buckets", "0")])
def test_c15_a_flag_out_of_range_is_refused(tmp_path, capsys, flag, value):            # C15
    out = tmp_path / "out"
    code = run_mode("proposal", out, "--capacity", _capacity_file(tmp_path), flag, value)
    _refused(capsys, code, out, f"{flag}: must be an integer >= 1")


def test_c15_a_proposal_capacity_for_an_unknown_endpoint_is_refused(tmp_path, capsys):  # C15
    code, out = _own_run(tmp_path, {"capacity": {"local-typo": {"max_concurrency": 2}}})
    _refused(capsys, code, out, "capacity.local-typo: not an onboarded endpoint")


def test_the_agreement_gate_still_refuses_first(tmp_path, capsys):
    """Rows recorded on an endpoint replay does not pick: agreement 0, exit
    4, nothing written -- capacity input or not."""
    rows = traffic([2] * 12, attempts=["local-gone"], chosen="local-gone")
    code, out = _own_run(tmp_path, {}, "--min-agreement", "0.9", rows=rows, capacity=LIMITS)
    assert code == 4 and "below --min-agreement" in capsys.readouterr().err
    assert not out.exists()


@pytest.mark.parametrize("scenarios", [False, True])
def test_c15_an_asserted_latency_too_long_to_place_is_refused(tmp_path, capsys, scenarios):  # C15
    """More than 24 hours of service time cannot be placed in the window,
    and an operator's claim is never dropped in silence: exit 2, nothing
    written, the endpoint named."""
    body = {"endpoints": {"update": {"local-cheap": {"latency_ms": 1e12}}}}
    if not scenarios:
        code, out = _own_run(tmp_path, body, capacity=LIMITS)
        _refused(capsys, code, out, "error: capacity: endpoints.local-cheap.latency_ms: ")
        return
    export, snap, doc = tmp_path / "e.ndjson", tmp_path / "s.json", tmp_path / "sc.json"
    export.write_text(_export_lines(traffic(QUIET_THEN_BUSY)), encoding="utf-8")
    snap.write_text(_dump(one_endpoint()), encoding="utf-8")
    doc.write_text(_dump({"schema": "routeforge.policy-scenarios/v1", "scenarios": [
        {"name": "slow-box", "proposal": body}]}), encoding="utf-8")
    out = tmp_path / "out"
    code = load_cli().main(["--export", str(export), "--snapshot", str(snap),
                            "--scenarios", str(doc), "--out", str(out),
                            "--config-dir", str(ROOT / "config"),
                            "--capacity", _capacity_file(tmp_path, LIMITS)])
    _refused(capsys, code, out,
             "error: capacity: scenarios[0](slow-box).endpoints.local-cheap.latency_ms: ")
