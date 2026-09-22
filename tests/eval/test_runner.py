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
              region="us", retention="none", raw_ok=True, latency=100.0,
              outages=None, error_rate=0.0, failure_latency_ms=None) -> dict:
    simulated = {"quality": {"default": quality},
                 "latency_ms": {"mean": latency, "jitter": 20}}
    if outages:
        simulated["outages"] = outages
    if error_rate:
        simulated["error_rate"] = error_rate
    if failure_latency_ms is not None:
        simulated["failure_latency_ms"] = failure_latency_ms
    return {
        "slug": slug, "provider": provider, "tier": tier,
        "price": {"input": price, "output": price},
        "region": region, "prompt_retention": retention,
        "accepts_raw_sensitive": raw_ok,
        "simulated": simulated,
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
    for key in ("suite_hash", "endpoints_hash", "base_routing_policy_version",
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


def test_empty_set_seen_by_runner_but_served_by_pipeline_exits_2(tmp_path, monkeypatch):
    """`rejected_ok` must rest on the REAL pipeline refusing the case, not on
    the runner's own `feasible_set` call. Blind only the runner's copy (the
    pipeline's router keeps the real one): the runner sees an empty set, the
    unpinned empty-probe is served, so the case is NOT rejected_ok and the
    served probe is a violation -> exit 2."""
    from app.policy.feasible import FeasibleSet, Rejection
    real_feasible_set = runner.feasible_set

    def blinded(settings, registry, ctx_or_policy, **kwargs):
        real = real_feasible_set(settings, registry, ctx_or_policy, **kwargs)
        fake_rejections = tuple(
            Rejection(name=c.name, provider=c.provider, tier=c.tier,
                      display_name=None, reason="blinded by test", code="test")
            for c in real.eligible)
        return FeasibleSet(eligible=(), rejected=real.rejected + fake_rejections)

    monkeypatch.setattr(runner, "feasible_set", blinded)
    cases = [_case("faq-1", "What is the capital of France?", expect_empty=True)]
    suite, eps = _write_inputs(tmp_path, cases)
    result = _run(suite, eps, tmp_path / "out")
    assert result.exit_code == 2
    summary = {c["case_id"]: c for c in result.metrics["cases"]}["faq-1"]
    assert summary["eligible_post"] == []
    assert summary["rejected_ok"] is False
    assert result.metrics["policy"]["violations_total"] >= 1


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


_CLEAN = {"s": {"violation_rate": 0.0}, "t": {"violation_rate": None}}


def test_exit_code_all_clear_is_0():
    assert runner._exit_code(0, _CLEAN, 0, 0, 0) == 0


def test_exit_code_each_trigger_alone_is_2():
    dirty = {**_CLEAN, "bad": {"violation_rate": 0.25}}
    assert runner._exit_code(1, _CLEAN, 0, 0, 0) == 2  # raw violation count
    assert runner._exit_code(0, dirty, 0, 0, 0) == 2   # a strategy's rate
    assert runner._exit_code(0, _CLEAN, 1, 0, 0) == 2  # empty-set mismatch
    assert runner._exit_code(0, _CLEAN, 0, 1, 0) == 2  # post-gate widening
    assert runner._exit_code(0, _CLEAN, 0, 0, 1) == 2  # pinned served mismatch


def result_pinned_mismatches(out) -> int:
    return json.loads((out / "metrics.json").read_text())["policy"]["pinned_mismatches"]


def test_low_confidence_does_not_reissue_pinned_sweep_calls(tmp_path, monkeypatch):
    """The sweep is one generate call per (case, endpoint). A low-confidence
    answer must not trigger the gateway's confidence escalation, which with a
    pin clamps to the same tier and calls the SAME endpoint again."""
    real_generate = SimulatedBackend.generate
    calls = {"n": 0}

    async def low_confidence(self, *args, **kwargs):
        calls["n"] += 1
        result = await real_generate(self, *args, **kwargs)
        return result.model_copy(update={"confidence": 0.1})

    monkeypatch.setattr(SimulatedBackend, "generate", low_confidence)
    suite, eps = _write_inputs(tmp_path)
    out = tmp_path / "out"
    result = _run(suite, eps, out)
    rows = _jsonl(out / "matrix.jsonl")
    assert rows
    assert calls["n"] == len(rows)
    assert result.exit_code == 0
    assert all(r["error"] is None for r in rows)
    assert result.metrics["policy"]["sweep_generate_mismatches"] == 0


def test_sweep_call_with_extra_generate_becomes_error_row(tmp_path, monkeypatch):
    """The runner's own invariant, independent of harness_settings: a
    successful sweep call that made != 1 backend generate call is an error
    row and is counted."""
    from app.eval import endpoints as endpoints_mod
    real_harness_settings = endpoints_mod.harness_settings

    def escalating(base, specs):
        settings = real_harness_settings(base, specs)
        settings.routing.max_escalations = 1  # undo the fix, runner must catch it
        return settings

    real_generate = SimulatedBackend.generate

    async def low_confidence(self, *args, **kwargs):
        result = await real_generate(self, *args, **kwargs)
        return result.model_copy(update={"confidence": 0.1})

    monkeypatch.setattr(runner, "harness_settings", escalating)
    monkeypatch.setattr(SimulatedBackend, "generate", low_confidence)
    suite, eps = _write_inputs(tmp_path)
    out = tmp_path / "out"
    result = _run(suite, eps, out)
    rows = _jsonl(out / "matrix.jsonl")
    bad = [r for r in rows if r["error"] and "GenerateCountMismatch" in r["error"]]
    assert bad
    assert result.metrics["policy"]["sweep_generate_mismatches"] == len(bad)
    assert all(r["passed"] is False and r["cost_usd"] == 0.0 for r in bad)


# --------------------------------------------------------------------------
# 6b. matrix rows record endpoint-unhealth (B48c, Task 2)
# --------------------------------------------------------------------------
#
# A PINNED sweep call only ever has one candidate (the pin). After its first
# failure the gateway's retry loop excludes it and calls `Router.select`
# again, which finds an empty pool and raises `NoEligibleCandidate` --
# `Gateway._exhausted(...)` then chains `from` THAT exception, not the
# original backend one, so `app/eval/runner.py::_root_cause` cannot reach
# the real backend exception under the default `max_failover_attempts` (2).
# These tests force `max_failover_attempts=0` so the gateway's very FIRST
# failure already exceeds the cap and `_exhausted` chains directly from the
# real backend exception -- the one shape where the original is reachable.
# This is a genuine limitation of the pinned-sweep design (not a test
# artifact): under the shipped default, `counts_unhealth` still comes out
# correct (the unreachable `NoEligibleCandidate` fallback happens to read as
# unhealth = True, same as a real outage would), but `failure_latency_ms`
# is not recoverable, and a 4xx-shaped failure's root would read as
# unhealth = True too, when it should be False. See task-2-report.md.

def _no_failover(base, specs):
    from app.eval import endpoints as endpoints_mod
    settings = endpoints_mod.harness_settings(base, specs)
    settings.routing.max_failover_attempts = 0
    return settings


def test_outage_row_records_endpoint_unhealth_and_failure_latency(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "harness_settings", _no_failover)
    endpoints = [_endpoint("local-a", outages=[{"from": 0, "to": 10}],
                           failure_latency_ms=250.0)]
    cases = [_case("faq-1", "What is the capital of France?")]
    suite, eps = _write_inputs(tmp_path, cases, endpoints)
    result = _run(suite, eps, tmp_path / "out")
    rows = _jsonl(tmp_path / "out" / "matrix.jsonl")
    assert len(rows) == 1
    row = rows[0]
    assert row["error"] is not None
    assert row["counts_unhealth"] is True
    assert row["failure_latency_ms"] == 250.0
    assert result.metrics["strategies"]["cheapest"]["violation_rate"] == 0.0


def test_400_status_backend_failure_does_not_count_as_unhealth(tmp_path, monkeypatch):
    """A 4xx (other than 429) says the request was wrong, not the endpoint
    (`counts_as_endpoint_unhealth`) -- proven here through the REAL pipeline
    + simulated backend, not a mock of the exception-unwrapping itself."""
    monkeypatch.setattr(runner, "harness_settings", _no_failover)

    async def bad_request(self, *args, **kwargs):
        exc = Exception("simulated bad request")
        exc.status_code = 400
        raise exc

    monkeypatch.setattr(SimulatedBackend, "generate", bad_request)
    cases = [_case("faq-1", "What is the capital of France?")]
    suite, eps = _write_inputs(tmp_path, cases, [_endpoint("local-a")])
    _run(suite, eps, tmp_path / "out")
    rows = _jsonl(tmp_path / "out" / "matrix.jsonl")
    assert len(rows) == 1
    row = rows[0]
    assert row["error"] is not None
    assert row["counts_unhealth"] is False
    assert row["failure_latency_ms"] is None


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


def test_cli_sets_local_cost_map_before_importing_app():
    """Air-gapped runs must not hang on litellm's import-time cost-map fetch:
    the CLI sets LITELLM_LOCAL_MODEL_COST_MAP before any `app.*` import."""
    env = {k: v for k, v in os.environ.items() if k != "LITELLM_LOCAL_MODEL_COST_MAP"}
    code = (
        "import builtins, os, runpy\n"
        "real_import = builtins.__import__\n"
        "seen = {}\n"
        "def spy(name, *a, **kw):\n"
        "    if name.startswith('app') and 'v' not in seen:\n"
        "        seen['v'] = os.environ.get('LITELLM_LOCAL_MODEL_COST_MAP')\n"
        "    return real_import(name, *a, **kw)\n"
        "builtins.__import__ = spy\n"
        f"runpy.run_path({str(REPO_ROOT / 'scripts' / 'feasible_bench.py')!r}, run_name='not_main')\n"
        "print(seen.get('v'))\n")
    proc = subprocess.run([sys.executable, "-c", code], cwd=REPO_ROOT, env=env,
                          capture_output=True, text=True, timeout=RUN_TIMEOUT_S)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip().splitlines()[-1] == "True"
