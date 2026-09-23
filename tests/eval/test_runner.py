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

from app.errors import BackendUnavailable, ContextWindowExceeded, NoEligibleCandidate
from app.eval import runner
from app.eval.runner import run_bench
from app.eval.simulated import SimulatedBackend

REPO_ROOT = Path(__file__).resolve().parents[2]
SSN = "078-05-1120"
RUN_TIMEOUT_S = 120

_BASE_STRATEGIES = ("cheapest", "best_published_quality", "router_cold", "router_online",
                    "router_warm_prior")


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
    # router_online:breaker_off (B45 Task 5): the base config's control arm,
    # added whenever the step-7 breaker is enabled (the shipped default).
    expected = set(_BASE_STRATEGIES) | {
        "fixed:local-b", "router_cold:avail0", "router_online:avail0",
        "router_warm_prior:avail0", "router_online:breaker_off"}
    assert set(metrics["strategies"]) == expected
    for name, m in metrics["strategies"].items():
        assert m["violation_rate"] == 0.0, name
    # every strategy has one choice per case
    choices = _jsonl(out / "choices.jsonl")
    assert len(choices) == len(expected) * len(DEFAULT_CASES)
    report = (out / "report.md").read_text()
    assert "None" not in report
    assert "router_warm" not in report.replace("router_warm_prior", "")
    assert "B45" in report and "`router_online:<variant>`" in report
    assert "counts as 0 ms" in report
    assert "violations_total` counts attempts" in report
    assert "seconds_per_case = 30" in report
    assert "wasted_attempts" in report and "exhausted_rate" in report
    run = json.loads((out / "run.json").read_text())
    for key in ("suite_hash", "endpoints_hash", "base_routing_policy_version",
                "registry_version", "seed", "git_sha", "mode", "args",
                "seconds_per_case", "attempt_cap"):
        assert key in run, key
    assert run["mode"] == "simulated"
    assert run["seconds_per_case"] == 30.0
    assert run["attempt_cap"] == 3      # 1 + the deployment's max_failover_attempts
    assert set(run["notes"]) >= {"router_cold", "router_online", "router_warm_prior",
                                 "case_order", "case_clock"}
    assert "router_warm" not in run["notes"]
    for name, m in metrics["strategies"].items():
        assert "wasted_attempts" in m and "exhausted_rate" in m, name
    for c in choices:
        assert isinstance(c["attempts"], list)
        assert c["endpoint"] == (c["attempts"][-1] if c["attempts"] else None)
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
    monkeypatch.setattr(runner, "cheapest_attempts",
                        lambda registry, cap: (lambda inp, failed: ("not-eligible",)))
    suite, eps = _write_inputs(tmp_path)
    out = tmp_path / "out"
    result = _run(suite, eps, out)
    assert result.exit_code == 2
    assert result.metrics["strategies"]["cheapest"]["violation_rate"] > 0
    assert result.metrics["strategies"]["router_cold"]["violation_rate"] == 0.0


def test_cloud_choice_on_onprem_case_exits_2(tmp_path, monkeypatch):
    """A realistic violation: a strategy that always picks the cloud
    endpoint -- eligible for most cases, never for the onprem_only one."""
    monkeypatch.setattr(runner, "cheapest_attempts",
                        lambda registry, cap: (lambda inp, failed: ("cloud-c",)))
    suite, eps = _write_inputs(tmp_path)
    result = _run(suite, eps, tmp_path / "out")
    assert result.exit_code == 2
    assert result.metrics["strategies"]["cheapest"]["violation_rate"] > 0


def test_entries_after_the_served_attempt_exit_2(tmp_path, monkeypatch):
    """Ruling D: a chain that keeps going after a success contradicts the
    gateway's loop. Every attempt is eligible (violations_total stays 0) but
    the per-case violation rate fails the run."""
    monkeypatch.setattr(runner, "cheapest_attempts",
                        lambda registry, cap: (lambda inp, failed: ("local-a", "local-b")))
    cases = [_case("faq-1", "What is the capital of France?")]
    suite, eps = _write_inputs(tmp_path, cases)
    result = _run(suite, eps, tmp_path / "out")
    assert result.exit_code == 2
    assert result.metrics["policy"]["violations_total"] == 0
    assert result.metrics["strategies"]["cheapest"]["violation_rate"] == 1.0


# --------------------------------------------------------------------------
# 3b. failover-aware scoring end to end (B48 spec §5-§6, rulings A-E)
# --------------------------------------------------------------------------

_OUTAGE_ENDPOINTS = [
    # cheaper, down for cases 0-1; the cost term prefers it when healthy
    _endpoint("cheap-a", price=0.1, quality=0.9, latency=300,
              outages=[{"from": 0, "to": 1}], failure_latency_ms=40.0),
    _endpoint("steady-b", price=0.5, quality=0.9, latency=600),
]
_OUTAGE_CASES = [_case(f"faq-{i}", "What is the capital of France?") for i in range(4)]


def test_failover_chains_record_attempts_and_served_endpoint(tmp_path):
    suite, eps = _write_inputs(tmp_path, _OUTAGE_CASES, _OUTAGE_ENDPOINTS)
    out = tmp_path / "out"
    result = _run(suite, eps, out)
    assert result.exit_code == 0
    by = {(c["case_id"], c["strategy"]): c for c in _jsonl(out / "choices.jsonl")}
    down = by[("faq-0", "cheapest")]
    assert down["attempts"] == ["cheap-a", "steady-b"]
    assert down["endpoint"] == "steady-b"          # the SERVED endpoint
    assert down["wasted_attempts"] == 1 and down["exhausted"] is False
    up = by[("faq-2", "cheapest")]
    assert up["attempts"] == ["cheap-a"] and up["endpoint"] == "cheap-a"
    cold = by[("faq-0", "router_cold")]
    assert cold["attempts"] == ["cheap-a", "steady-b"]
    m = result.metrics["strategies"]["cheapest"]
    assert m["wasted_attempts"] == 0.5 and m["exhausted_rate"] == 0.0


def test_failed_reads_only_the_scored_cases_rows(tmp_path, monkeypatch):
    """Ruling E: `failed(slug)` is case k's matrix row for `slug` -- cheap-a
    is down on cases 0-1 and up on 2-3, whatever other cases say."""
    seen: dict[str, dict[str, bool]] = {}

    def probing(registry, cap):
        def strat(inp, failed):
            seen[inp.case.id] = {c.name: failed(c.name) for c in inp.eligible}
            return ()
        return strat

    monkeypatch.setattr(runner, "cheapest_attempts", probing)
    suite, eps = _write_inputs(tmp_path, _OUTAGE_CASES, _OUTAGE_ENDPOINTS)
    _run(suite, eps, tmp_path / "out")
    assert seen == {
        "faq-0": {"cheap-a": True, "steady-b": False},
        "faq-1": {"cheap-a": True, "steady-b": False},
        "faq-2": {"cheap-a": False, "steady-b": False},
        "faq-3": {"cheap-a": False, "steady-b": False},
    }


def test_online_arm_is_built_on_harness_settings_with_the_deployment_cap(
        tmp_path, monkeypatch):
    """Ruling C: harness settings (emptied model_pool -- no stock model can
    enter a chain) and max_attempts = 1 + the deployment's failover cap."""
    built = []
    real = runner.OnlineArm

    class Spy(real):
        def __init__(self, settings, registry, max_attempts, weights=None, **kw):
            built.append((settings.routing.model_pool, max_attempts, kw))
            super().__init__(settings, registry, max_attempts, weights, **kw)

    monkeypatch.setattr(runner, "OnlineArm", Spy)
    suite, eps = _write_inputs(tmp_path, _OUTAGE_CASES, _OUTAGE_ENDPOINTS)
    assert _run(suite, eps, tmp_path / "out", seconds_per_case=7.5).exit_code == 0
    # router_online, then router_online:breaker_off (B45 Task 5): the
    # breaker's control arm, added for the base config whenever the shipped
    # default (routing.yaml breaker.enabled) is on.
    assert built == [([], 3, {"seconds_per_case": 7.5}),
                     ([], 3, {"seconds_per_case": 7.5, "breaker": False})]


def test_runner_out_of_order_online_call_trips_the_leak_guard(tmp_path, monkeypatch, capsys):
    """Ruling B: were the runner to score a case it has already observed,
    the arm's own guard fires (the run does not silently leak) -- caught and
    reported by `run_bench` itself (final review F2): exit code 2, an
    `integrity error` line on stderr, no exception escapes."""
    real = runner.OnlineArm

    class OutOfOrder(real):
        def choose(self, inp, failed, *, case_index):
            # re-score the previous (already observed) case
            return super().choose(inp, failed, case_index=max(case_index - 1, 0))

    monkeypatch.setattr(runner, "OnlineArm", OutOfOrder)
    suite, eps = _write_inputs(tmp_path, _OUTAGE_CASES, _OUTAGE_ENDPOINTS)
    result = _run(suite, eps, tmp_path / "out")
    assert result.exit_code == 2
    assert "feasible_bench: integrity error:" in capsys.readouterr().err


def test_runner_passing_the_scored_cases_rows_to_warm_prior_is_refused(
        tmp_path, monkeypatch, capsys):
    real = runner.warm_prior_signals

    def leaky(settings, rows, *, case_index, scoring_case, seconds_per_case):
        # pretend the runner scored one case late: its rows now include k-1's
        return real(settings, rows, case_index=case_index,
                    scoring_case=max(scoring_case - 1, 0),
                    seconds_per_case=seconds_per_case)

    monkeypatch.setattr(runner, "warm_prior_signals", leaky)
    suite, eps = _write_inputs(tmp_path, _OUTAGE_CASES, _OUTAGE_ENDPOINTS)
    result = _run(suite, eps, tmp_path / "out")
    assert result.exit_code == 2
    assert "feasible_bench: integrity error:" in capsys.readouterr().err


def test_nonpositive_seconds_per_case_exits_1(tmp_path):
    suite, eps = _write_inputs(tmp_path)
    for bad in (0, -5.0):
        out = tmp_path / f"out{bad}"
        assert _run(suite, eps, out, seconds_per_case=bad).exit_code == 1
        assert not out.exists()


def test_seconds_per_case_too_small_for_endpoint_count_exits_1(tmp_path, capsys):
    """Final review F3: a positive but tiny `seconds_per_case` leaves no room
    for this run's sub-step observations (one per endpoint swept or per
    attempt an online arm makes) -- caught at load time, before any case
    runs, rather than raised mid-run by `strategies._case_time`."""
    suite, eps = _write_inputs(tmp_path)
    out = tmp_path / "out"
    result = _run(suite, eps, out, seconds_per_case=0.001)
    assert result.exit_code == 1
    assert not out.exists()
    assert "seconds_per_case" in capsys.readouterr().err


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
# 6b. matrix rows record endpoint-unhealth (B48c, Task 2 + fix rounds 1-2)
# --------------------------------------------------------------------------
#
# `harness_settings` zeroes `max_failover_attempts` (fix round 1) so a
# pinned sweep call's gateway exhaustion always chains directly from the
# real backend exception; `_backend_exception` (fix round 2) then unwraps
# exactly that one hop. These tests run through the shipped harness
# defaults (no per-test override) end to end.

class TestBackendExceptionUnitLevel:
    """Hand-built exception chains -- fix round 2, reviewer-probed cases."""

    def test_stops_one_hop_below_gateway_exhaustion_never_follows_context(self):
        """A `BadRequestError`-shaped cause (status_code=400) whose OWN
        `__context__` is some deeper, status-less exception (e.g. what a real
        `openai`/`litellm` internal re-raise sets) must be read directly --
        never walked past into `__context__`, which would misclassify a 4xx
        as endpoint-unhealth (True) via a status-less wrapper."""
        deep_context = Exception("httpx.HTTPStatusError-shaped, no status_code")
        bad_request = Exception("bad request")
        bad_request.status_code = 400
        bad_request.__context__ = deep_context
        outer = BackendUnavailable("exhausted", attempted_models=("m",))
        outer.__cause__ = bad_request

        backend_exc = runner._backend_exception(outer)

        assert backend_exc is bad_request
        assert runner.counts_as_endpoint_unhealth(backend_exc) is False

    def test_context_window_exceeded_is_exempt(self):
        original = ContextWindowExceeded("prompt too large for this model")
        outer = ContextWindowExceeded("prompt too large for this model")
        outer.__cause__ = original

        backend_exc = runner._backend_exception(outer)

        assert backend_exc is original
        assert runner.counts_as_endpoint_unhealth(backend_exc) is False

    def test_non_exhaustion_exception_yields_no_endpoint_verdict(self):
        """A bare `NoEligibleCandidate` -- e.g. the pipeline never reached
        the gateway's own exhaustion at all -- carries no endpoint verdict."""
        assert runner._backend_exception(NoEligibleCandidate("no candidates")) is None

    def test_gateway_exhaustion_with_no_cause_yields_no_endpoint_verdict(self):
        assert runner._backend_exception(BackendUnavailable("exhausted")) is None

    def test_exhaustion_caused_by_no_eligible_candidate_yields_no_endpoint_verdict(self):
        """`Gateway.run` raises `_exhausted(...) from exc` when a
        `failed_models` exclusion makes `Router.select` itself raise
        `NoEligibleCandidate` (every reachable candidate already excluded).
        That cause is the router saying "no candidate left", not a verdict
        on the last backend it tried -- final review F1."""
        outer = BackendUnavailable("exhausted", attempted_models=("m",))
        outer.__cause__ = NoEligibleCandidate("no candidates")

        assert runner._backend_exception(outer) is None


def test_outage_row_records_endpoint_unhealth_and_failure_latency(tmp_path):
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
    assert row["error_class"] == "SimulatedEndpointFailure"
    assert result.metrics["strategies"]["cheapest"]["violation_rate"] == 0.0


def test_flake_row_records_endpoint_unhealth(tmp_path):
    """The independent `error_rate` flake path (status_code=503, spec §3) --
    distinct injection mechanism from an outage, same endpoint-unhealth
    verdict."""
    endpoints = [_endpoint("local-a", error_rate=1.0)]
    cases = [_case("faq-1", "What is the capital of France?")]
    suite, eps = _write_inputs(tmp_path, cases, endpoints)
    _run(suite, eps, tmp_path / "out")
    rows = _jsonl(tmp_path / "out" / "matrix.jsonl")
    assert len(rows) == 1
    row = rows[0]
    assert row["error"] is not None
    assert row["counts_unhealth"] is True
    assert row["error_class"] == "SimulatedEndpointFailure"


def test_400_status_backend_failure_does_not_count_as_unhealth(tmp_path, monkeypatch):
    """A 4xx (other than 429) says the request was wrong, not the endpoint
    (`counts_as_endpoint_unhealth`) -- proven here through the REAL pipeline
    + simulated backend, not a mock of the exception-unwrapping itself, and
    through the shipped `harness_settings` defaults (no override)."""

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
    assert row["error_class"] == "Exception"


def test_non_gateway_pipeline_failure_is_not_an_endpoint_verdict(tmp_path, monkeypatch):
    """B48c fix round 2: a pipeline failure that never reached gateway
    exhaustion (`PipelineRejected`, `NoEligibleCandidate`, `PolicyGateError`,
    ...) is not a statement about endpoint health -- `counts_unhealth` must
    be `False`, not the pre-fix-round-2 default of `True`."""
    from app.pipeline import Pipeline, PipelineRejected

    async def rejected(self, *args, **kwargs):
        raise PipelineRejected("blocked by a fake output-controls failure")

    monkeypatch.setattr(Pipeline, "run", rejected)
    cases = [_case("faq-1", "What is the capital of France?")]
    suite, eps = _write_inputs(tmp_path, cases, [_endpoint("local-a")])
    _run(suite, eps, tmp_path / "out")
    rows = _jsonl(tmp_path / "out" / "matrix.jsonl")
    assert len(rows) == 1
    row = rows[0]
    assert row["error"] is not None and "PipelineRejected" in row["error"]
    assert row["counts_unhealth"] is False
    assert row["failure_latency_ms"] is None
    assert row["error_class"] is None


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
    proc = _cli("--suite", suite, "--endpoints", eps, "--out", str(out), "--seed", "3",
                "--seconds-per-case", "300")
    assert proc.returncode == 0, proc.stderr
    assert (out / "metrics.json").is_file()
    assert json.loads((out / "run.json").read_text())["seconds_per_case"] == 300.0


def test_cli_nonpositive_seconds_per_case_exits_1(tmp_path):
    suite, eps = _write_inputs(tmp_path)
    proc = _cli("--suite", suite, "--endpoints", eps, "--out", str(tmp_path / "o"),
                "--seconds-per-case", "0")
    assert proc.returncode == 1
    assert "seconds_per_case" in proc.stderr


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
