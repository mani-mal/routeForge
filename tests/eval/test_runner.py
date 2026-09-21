"""End-to-end tests for `app.eval.runner` + `scripts/feasible_bench.py`
(Task 6, FeasibleRouteBench).

Every run here is SIMULATED (spec §5.1) -- a tiny inline suite and endpoint
file written to `tmp_path`, driven through the REAL gate, `feasible_set`,
pipeline, and `Router.select`. Nothing makes a network call. Every async run
is bounded by `asyncio.wait_for` so a regression cannot hang the suite.
See docs/superpowers/specs/2026-09-21-feasible-route-bench-design.md §5-§8.
"""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path

import yaml

from app.eval import runner
from app.eval.runner import run_bench
from app.eval.simulated import SimulatedBackend

REPO_ROOT = Path(__file__).resolve().parents[2]
SSN = "078-05-1120"
RUN_TIMEOUT_S = 120

_BASE_STRATEGIES = ("cheapest", "best_published_quality", "router_cold", "router_warm")


# --------------------------------------------------------------------------
# fixtures written to tmp_path
# --------------------------------------------------------------------------

def _endpoint(slug, *, provider="onprem", tier="cheap", price=0.1, quality=0.9,
              region="us", retention="none", raw_ok=True, latency=100.0) -> dict:
    return {
        "slug": slug, "provider": provider, "tier": tier,
        "price": {"input": price, "output": price},
        "region": region, "prompt_retention": retention,
        "accepts_raw_sensitive": raw_ok,
        "simulated": {"quality": {"default": quality},
                      "latency_ms": {"mean": latency, "jitter": 20}},
    }


DEFAULT_ENDPOINTS = [
    _endpoint("local-a", tier="cheap", price=0.1, quality=0.8, latency=300),
    _endpoint("local-b", tier="mid", price=0.5, quality=0.95, latency=500),
    _endpoint("cloud-c", provider="azure", tier="frontier", price=5.0, quality=1.0,
              raw_ok=False, latency=900),
]


def _case(case_id, content, *, expected="paris", residency="any",
          policy_mode="enforced", workload="faq", synthetic=False,
          expect_empty=False) -> dict:
    tags = {"workload": workload, "placement": residency, "protection": policy_mode}
    if synthetic:
        tags["synthetic"] = True
    return {
        "id": case_id,
        "messages": [{"role": "user", "content": content}],
        "app_policy": {"residency": residency, "policy_mode": policy_mode,
                       "pii_detection": "regex"},
        "grader": {"kind": "contains", "expected": expected},
        "tags": tags,
        "expect_empty": expect_empty,
    }


DEFAULT_CASES = [
    _case("faq-1", "What is the capital of France?"),
    _case("faq-2", "Name the French capital city.", workload="faq"),
    _case("claim-enforced", f"Summarise the claim for SSN {SSN}. Is it approved?",
          expected="approved", workload="claim", synthetic=True),
    _case("claim-passthrough", f"Summarise the claim for SSN {SSN}. Is it approved?",
          expected="approved", workload="claim", policy_mode="passthrough",
          synthetic=True),
    _case("onprem-1", "Where is the Eiffel Tower?", residency="onprem_only"),
]


def _write_inputs(tmp_path, cases=None, endpoints=None) -> tuple[str, str]:
    suite = tmp_path / "suite.jsonl"
    suite.write_text("\n".join(json.dumps(c) for c in (cases or DEFAULT_CASES)) + "\n")
    eps = tmp_path / "endpoints.yaml"
    eps.write_text(yaml.safe_dump({"endpoints": endpoints or DEFAULT_ENDPOINTS}))
    return str(suite), str(eps)


def _run(suite, eps, out, **kwargs):
    return asyncio.run(asyncio.wait_for(
        run_bench(suite, eps, out_dir=str(out), **kwargs), timeout=RUN_TIMEOUT_S))


def _jsonl(path) -> list[dict]:
    return [json.loads(line) for line in Path(path).read_text().splitlines() if line.strip()]


# --------------------------------------------------------------------------
# 1. end-to-end
# --------------------------------------------------------------------------

def test_end_to_end_writes_all_five_files_and_every_strategy(tmp_path):
    suite, eps = _write_inputs(tmp_path)
    variants = tmp_path / "variants.yaml"
    variants.write_text(yaml.safe_dump({
        "avail0": {"quality": 0.4, "cost": 0.4, "latency": 0.2, "availability": 0.0}}))
    out = tmp_path / "out"
    result = _run(suite, eps, out, variants_path=str(variants), fixed="local-b")

    assert result.exit_code == 0
    for name in ("matrix.jsonl", "choices.jsonl", "metrics.json", "report.md", "run.json"):
        assert (out / name).is_file(), name
    metrics = json.loads((out / "metrics.json").read_text())
    expected = set(_BASE_STRATEGIES) | {
        "fixed:local-b", "router_cold:avail0", "router_warm:avail0"}
    assert set(metrics["strategies"]) == expected
    for name, m in metrics["strategies"].items():
        assert m["violation_rate"] == 0.0, name
    # every strategy has one choice per case
    choices = _jsonl(out / "choices.jsonl")
    assert len(choices) == len(expected) * len(DEFAULT_CASES)
    report = (out / "report.md").read_text()
    assert "None" not in report
    assert "same sweep" in report
    run = json.loads((out / "run.json").read_text())
    for key in ("suite_hash", "endpoints_hash", "routing_policy_version",
                "registry_version", "seed", "git_sha", "mode", "args"):
        assert key in run, key
    assert run["mode"] == "simulated"
    assert "same sweep" in run["notes"]["router_warm"]
    assert result.metrics == metrics


def test_onprem_only_case_never_sweeps_cloud(tmp_path):
    suite, eps = _write_inputs(tmp_path)
    out = tmp_path / "out"
    assert _run(suite, eps, out).exit_code == 0
    swept = {r["endpoint"] for r in _jsonl(out / "matrix.jsonl") if r["case_id"] == "onprem-1"}
    assert swept == {"local-a", "local-b"}


# --------------------------------------------------------------------------
# 2. determinism
# --------------------------------------------------------------------------

def test_same_seed_gives_byte_identical_metrics(tmp_path):
    suite, eps = _write_inputs(tmp_path)
    variants = tmp_path / "variants.yaml"
    variants.write_text(yaml.safe_dump({
        "lat_heavy": {"quality": 0.2, "cost": 0.2, "latency": 0.5, "availability": 0.1}}))
    a, b = tmp_path / "a", tmp_path / "b"
    _run(suite, eps, a, seed=7, variants_path=str(variants))
    _run(suite, eps, b, seed=7, variants_path=str(variants))
    assert (a / "metrics.json").read_bytes() == (b / "metrics.json").read_bytes()
    assert (a / "matrix.jsonl").read_bytes() == (b / "matrix.jsonl").read_bytes()


# --------------------------------------------------------------------------
# 3. violation guard, falsified
# --------------------------------------------------------------------------

def test_injected_ineligible_choice_exits_2(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "cheapest", lambda registry: (lambda inp: "not-eligible"))
    suite, eps = _write_inputs(tmp_path)
    out = tmp_path / "out"
    result = _run(suite, eps, out)
    assert result.exit_code == 2
    assert result.metrics["strategies"]["cheapest"]["violation_rate"] > 0
    assert result.metrics["strategies"]["router_cold"]["violation_rate"] == 0.0


def test_cloud_choice_on_onprem_case_exits_2(tmp_path, monkeypatch):
    """A realistic violation: a strategy that always picks the cloud
    endpoint -- eligible for most cases, never for the onprem_only one."""
    monkeypatch.setattr(runner, "cheapest", lambda registry: (lambda inp: "cloud-c"))
    suite, eps = _write_inputs(tmp_path)
    result = _run(suite, eps, tmp_path / "out")
    assert result.exit_code == 2
    assert result.metrics["strategies"]["cheapest"]["violation_rate"] > 0


# --------------------------------------------------------------------------
# 4. empty set
# --------------------------------------------------------------------------

_CLOUD_ONLY = [_endpoint("cloud-c", provider="azure", tier="frontier", price=5.0,
                         quality=1.0, raw_ok=False)]


def test_empty_set_expected_is_rejected_without_calls(tmp_path):
    cases = [_case("faq-1", "What is the capital of France?"),
             _case("onprem-empty", "Where is Paris?", residency="onprem_only",
                   expect_empty=True)]
    suite, eps = _write_inputs(tmp_path, cases, _CLOUD_ONLY)
    out = tmp_path / "out"
    result = _run(suite, eps, out)
    assert result.exit_code == 0
    rows = _jsonl(out / "matrix.jsonl")
    assert not [r for r in rows if r["case_id"] == "onprem-empty"]
    summary = {c["case_id"]: c for c in result.metrics["cases"]}["onprem-empty"]
    assert summary["eligible_post"] == []
    assert summary["rejected_ok"] is True
    assert summary["calls"] == 0
    assert summary["rejected"]
    for c in _jsonl(out / "choices.jsonl"):
        if c["case_id"] == "onprem-empty":
            assert c["endpoint"] is None


def test_empty_set_unexpected_exits_2(tmp_path):
    cases = [_case("onprem-empty", "Where is Paris?", residency="onprem_only",
                   expect_empty=False)]
    suite, eps = _write_inputs(tmp_path, cases, _CLOUD_ONLY)
    result = _run(suite, eps, tmp_path / "out")
    assert result.exit_code == 2
    assert result.metrics["policy"]["empty_set_mismatches"] == 1


# --------------------------------------------------------------------------
# 5. model-facing view
# --------------------------------------------------------------------------

def test_model_facing_view_redacted_for_enforced_raw_for_passthrough(tmp_path, monkeypatch):
    instances: list[SimulatedBackend] = []

    class Capturing(SimulatedBackend):
        def __init__(self, *a, **kw):
            super().__init__(*a, **kw)
            instances.append(self)

    monkeypatch.setattr(runner, "SimulatedBackend", Capturing)
    suite, eps = _write_inputs(tmp_path)
    assert _run(suite, eps, tmp_path / "out").exit_code == 0
    (backend,) = instances

    def seen(case_id) -> list[str]:
        texts = [m.content or "" for (cid, _model), msgs in backend.received.items()
                 if cid == case_id for m in msgs]
        assert texts, f"no calls recorded for {case_id}"
        return texts

    assert not any(SSN in t for t in seen("claim-enforced"))
    assert any(SSN in t for t in seen("claim-passthrough"))


# --------------------------------------------------------------------------
# 6. pinned sweep serves the pin
# --------------------------------------------------------------------------

def test_every_row_served_its_pinned_endpoint(tmp_path):
    suite, eps = _write_inputs(tmp_path)
    out = tmp_path / "out"
    assert _run(suite, eps, out).exit_code == 0
    rows = _jsonl(out / "matrix.jsonl")
    assert rows
    for row in rows:
        assert row["error"] is None, row
        assert row["served"] == row["endpoint"], row
    assert result_pinned_mismatches(out) == 0


def result_pinned_mismatches(out) -> int:
    return json.loads((out / "metrics.json").read_text())["policy"]["pinned_mismatches"]


# --------------------------------------------------------------------------
# 7. keep-text
# --------------------------------------------------------------------------

def test_text_only_persisted_with_keep_text(tmp_path):
    suite, eps = _write_inputs(tmp_path)
    plain, kept = tmp_path / "plain", tmp_path / "kept"
    _run(suite, eps, plain)
    _run(suite, eps, kept, keep_text=True)
    assert all("text" not in r for r in _jsonl(plain / "matrix.jsonl"))
    kept_rows = _jsonl(kept / "matrix.jsonl")
    assert kept_rows and all("text" in r for r in kept_rows)


# --------------------------------------------------------------------------
# load errors -> exit 1
# --------------------------------------------------------------------------

def test_bad_variant_weights_exit_1(tmp_path):
    suite, eps = _write_inputs(tmp_path)
    variants = tmp_path / "variants.yaml"
    variants.write_text(yaml.safe_dump({"broken": {"quality": 1.0}}))
    out = tmp_path / "out"
    result = _run(suite, eps, out, variants_path=str(variants))
    assert result.exit_code == 1
    assert not (out / "metrics.json").exists()


def test_unknown_fixed_slug_exit_1(tmp_path):
    suite, eps = _write_inputs(tmp_path)
    assert _run(suite, eps, tmp_path / "out", fixed="nope").exit_code == 1


# --------------------------------------------------------------------------
# 9. CLI
# --------------------------------------------------------------------------

def _cli(*args, stdin: str | None = None) -> subprocess.CompletedProcess:
    env = {**os.environ, "LITELLM_LOCAL_MODEL_COST_MAP": "True",
           "SP_PII_DETECTION": "regex"}
    return subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / "feasible_bench.py"), *args],
        cwd=REPO_ROOT, env=env, input=stdin, capture_output=True, text=True,
        timeout=RUN_TIMEOUT_S)


def test_cli_simulated_run_exits_0(tmp_path):
    suite, eps = _write_inputs(tmp_path)
    out = tmp_path / "cli-out"
    proc = _cli("--suite", suite, "--endpoints", eps, "--out", str(out), "--seed", "3")
    assert proc.returncode == 0, proc.stderr
    assert (out / "metrics.json").is_file()


def _live_endpoints(tmp_path) -> str:
    path = tmp_path / "live.yaml"
    path.write_text(yaml.safe_dump({"endpoints": [{
        "slug": "live-a", "provider": "onprem", "tier": "cheap",
        "price": {"input": 0.1, "output": 0.1},
        # Port 9 (discard) on loopback: never contacted -- the runs below
        # stop before any call is made.
        "live": {"endpoint": "http://127.0.0.1:9/v1", "api_key_env": "SP_ONPREM_API_KEY"},
    }]}))
    return str(path)


def test_cli_live_endpoint_without_live_flag_exits_1(tmp_path):
    suite, _ = _write_inputs(tmp_path)
    proc = _cli("--suite", suite, "--endpoints", _live_endpoints(tmp_path),
                "--out", str(tmp_path / "o"))
    assert proc.returncode == 1
    assert not (tmp_path / "o" / "matrix.jsonl").exists()


def test_cli_live_declined_at_prompt_makes_no_calls(tmp_path):
    suite, _ = _write_inputs(tmp_path)
    out = tmp_path / "o"
    proc = _cli("--suite", suite, "--endpoints", _live_endpoints(tmp_path),
                "--out", str(out), "--live", stdin="n\n")
    assert proc.returncode == 1
    assert "live-a" in proc.stdout
    assert "calls" in proc.stdout
    assert not (out / "matrix.jsonl").exists()
