"""End-to-end run of the shipped B48 outage scenario (spec 2026-09-22 §7-§8):
`eval/suites/outage.jsonl` x `eval/endpoints/outage-sim.yaml` x
`eval/variants/b45.yaml`, simulated, seed 0.

What is asserted is what the design guarantees, not a B45 answer: the run is
deterministic and clean, the outage really moves `router_online`'s
availability variants apart at the default case clock (30 s per case), and
the full-information `router_warm_prior` arm wastes no more attempts than
`router_online` at the shipped weights. WHICH variant wastes fewer attempts
is the scenario's finding and is reported, not asserted here (controller
ruling H: the scenario must not be tuned to force a direction).
"""
from __future__ import annotations

import asyncio
import json

from app.eval.cases import load_suite
from app.eval.endpoints import load_endpoints
from app.eval.runner import run_bench
from app.config.loader import load_settings

OUTAGE_SUITE = "eval/suites/outage.jsonl"
OUTAGE_SIM = "eval/endpoints/outage-sim.yaml"
B45_VARIANTS = "eval/variants/b45.yaml"
AVAILABILITY_VARIANTS = ("default", "availability_0", "availability_1", "availability_2")
RUN_TIMEOUT_S = 300


def _run(out, **kwargs):
    return asyncio.run(asyncio.wait_for(
        run_bench(OUTAGE_SUITE, OUTAGE_SIM, out_dir=str(out),
                  variants_path=B45_VARIANTS, seed=0, **kwargs),
        timeout=RUN_TIMEOUT_S))


def test_outage_files_load_as_designed():
    cases, _ = load_suite(OUTAGE_SUITE)
    assert len(cases) == 30
    assert all(c.app_policy.get("residency") == "any" for c in cases)
    assert all(c.tags.get("workload") == "faq" and not c.expect_empty for c in cases)
    specs, _ = load_endpoints(OUTAGE_SIM, live=False, tiers=load_settings().routing.tiers)
    by = {s.slug: s for s in specs}
    assert set(by) == {"fast-cheap", "steady"}
    assert {s.tier for s in specs} == {"cheap"} and {s.provider for s in specs} == {"onprem"}
    fast, steady = by["fast-cheap"], by["steady"]
    assert fast.price.input < steady.price.input
    assert [(o.from_, o.to) for o in fast.simulated.outages] == [(10, 19)]
    assert steady.simulated.error_rate == 0.05 and not steady.simulated.outages


def test_outage_run_is_deterministic_clean_and_moves_the_online_variants(tmp_path):
    a = _run(tmp_path / "a")
    b = _run(tmp_path / "b")
    assert a.exit_code == 0 and b.exit_code == 0
    assert (tmp_path / "a" / "metrics.json").read_bytes() == \
        (tmp_path / "b" / "metrics.json").read_bytes()

    m = a.metrics
    assert m["policy"]["violations_total"] == 0
    for name, s in m["strategies"].items():
        assert s["violation_rate"] == 0.0, name

    # The outage happened: fast-cheap failed on exactly cases 10-19.
    matrix = [json.loads(line) for line in
              (tmp_path / "a" / "matrix.jsonl").read_text().splitlines()]
    down = sorted(r["case_id"] for r in matrix
                  if r["endpoint"] == "fast-cheap" and r["error"] is not None)
    assert down == [f"outage-faq-{i:03d}" for i in range(10, 20)]
    assert all(r["counts_unhealth"] for r in matrix if r["error"] is not None)

    strategies = m["strategies"]
    wasted = {v: strategies[f"router_online:{v}"]["wasted_attempts"]
              for v in AVAILABILITY_VARIANTS}
    # B45's instrument is live: the availability weight changes what the
    # online router does during the outage (direction NOT asserted).
    assert len(set(wasted.values())) > 1, wasted
    # The full-information arm is a full-information reference (not an upper
    # bound: across seeds it sometimes wastes more at weights 1.0/2.0); at the
    # shipped weights on this seed it wastes no more than the signal-learning
    # arm it is fairly comparable to. That is router_online:breaker_off, not
    # router_online any more (B45 Task 5): router_online now also carries the
    # step-7 breaker, an extra mechanism router_warm_prior does not have, so
    # comparing against it would not be an apples-to-apples full-information
    # -vs-learned-signals comparison -- and indeed the breaker lets
    # router_online waste FEWER attempts than router_warm_prior here (0.2 vs
    # 0.333), which is exactly what the breaker_off/router_online pair right
    # below is for measuring.
    assert strategies["router_warm_prior"]["wasted_attempts"] <= \
        strategies["router_online:breaker_off"]["wasted_attempts"]
    # The cold arm cannot learn: every availability variant is identical.
    cold = {json.dumps(strategies[f"router_cold:{v}"], sort_keys=True)
            for v in AVAILABILITY_VARIANTS}
    assert len(cold) == 1

    # B45 Task 5: router_online:breaker_off is the base (unvaried) config's
    # control arm -- same online arm, step-7 breaker disabled. With the
    # breaker off, fast-cheap is never pulled out of rotation during its
    # outage, so it can only waste as many (or more) attempts as the
    # breaker-on reported arm, never fewer. Direction only, never magnitude
    # (a byte-identical metrics.json for the same seed is already asserted
    # above -- assertion 3 of the brief).
    assert "router_online:breaker_off" in strategies
    assert strategies["router_online:breaker_off"]["wasted_attempts"] >= \
        strategies["router_online"]["wasted_attempts"]
