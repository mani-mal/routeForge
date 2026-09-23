"""Tests for the shipped FeasibleRouteBench data (Task 7): the starter
suite, the §14 healthcare simulation profile, the cloud-only + empty-set
pair, the Spark live example, and the B45 weight variants.

See docs/superpowers/specs/2026-09-21-feasible-route-bench-design.md §9 and
the task-7-brief (`.superpowers/sdd/2026-09-21-feasible-route-bench/`).

Every run here is SIMULATED. Nothing calls the Spark's placeholder host --
that file is only ever loaded with `live=True`, never run.
"""
from __future__ import annotations

import asyncio
import json

from app.config.loader import load_settings
from app.eval.cases import load_suite
from app.eval.endpoints import load_endpoints
from app.eval.runner import run_bench

REPO_EVAL = "eval"
STARTER_SUITE = f"{REPO_EVAL}/suites/starter.jsonl"
EMPTY_SUITE = f"{REPO_EVAL}/suites/empty-set.jsonl"
HEALTHCARE_SIM = f"{REPO_EVAL}/endpoints/healthcare-sim.yaml"
CLOUD_ONLY_SIM = f"{REPO_EVAL}/endpoints/cloud-only-sim.yaml"
SPARK_LIVE_EXAMPLE = f"{REPO_EVAL}/endpoints/spark-live.example.yaml"
SPARK_OUTAGE_EXAMPLE = f"{REPO_EVAL}/endpoints/spark-outage-live.example.yaml"
B45_VARIANTS = f"{REPO_EVAL}/variants/b45.yaml"

RUN_TIMEOUT_S = 180

BASE_SETTINGS = load_settings()
TIERS = BASE_SETTINGS.routing.tiers


def _run(suite, eps, out, **kwargs):
    return asyncio.run(asyncio.wait_for(
        run_bench(suite, eps, out_dir=str(out), **kwargs), timeout=RUN_TIMEOUT_S))


# --------------------------------------------------------------------------
# 1. the suites load
# --------------------------------------------------------------------------

def test_starter_suite_loads_and_covers_the_three_workloads():
    cases, digest = load_suite(STARTER_SUITE)
    assert len(cases) == 22
    assert digest  # sha256 hex, non-empty
    ids = [c.id for c in cases]
    assert len(ids) == len(set(ids)), "duplicate case ids"

    by_workload: dict[str, list] = {}
    for case in cases:
        by_workload.setdefault(case.tags.get("workload"), []).append(case)

    assert len(by_workload["faq"]) == 8
    assert len(by_workload["claim_summary"]) == 10
    assert len(by_workload["onprem_required"]) == 4

    for case in cases:
        assert "workload" in case.tags
        assert "placement" in case.tags
        assert "protection" in case.tags
        assert case.expect_empty is False

    faq_graders = {c.grader.kind for c in by_workload["faq"]}
    assert faq_graders == {"contains", "exact", "numeric"}

    claim_graders = {c.grader.kind for c in by_workload["claim_summary"]}
    assert claim_graders == {"contains", "regex", "json_schema"}

    # every claim case carries synthetic PII, correctly tagged, and the same
    # allowed_regions/require_no_retention requirements -- override and
    # escalated cases differ ONLY in escalate_residency_on_detection.
    for case in by_workload["claim_summary"]:
        assert case.tags.get("synthetic") is True
        assert case.app_policy.get("allowed_regions") == ["us"]
        assert case.app_policy.get("require_no_retention") is True

    # override path (tags.placement == "us_only"): explicitly declares
    # escalate_residency_on_detection: false.
    override_claims = [c for c in by_workload["claim_summary"]
                       if c.tags.get("placement") == "us_only"]
    assert len(override_claims) == 8
    for case in override_claims:
        assert case.app_policy.get("escalate_residency_on_detection") is False

    # production-default path (tags.placement == "escalated"): the field is
    # left UNSET, so it inherits the real default (true) -- config/policy.yaml's
    # sensitivity_rules then escalates a detected SSN to onprem_only
    # (CLAUDE.md rule 8's "hospital persona").
    escalated_claims = [c for c in by_workload["claim_summary"]
                        if c.tags.get("placement") == "escalated"]
    assert len(escalated_claims) == 2
    for case in escalated_claims:
        assert "escalate_residency_on_detection" not in case.app_policy


def test_empty_set_suite_loads_four_expect_empty_onprem_cases():
    cases, digest = load_suite(EMPTY_SUITE)
    assert len(cases) == 4
    assert digest
    for case in cases:
        assert case.expect_empty is True
        assert case.app_policy.get("residency") == "onprem_only"


def test_healthcare_sim_endpoints_load_with_the_four_slugs():
    specs, digest = load_endpoints(HEALTHCARE_SIM, live=False, tiers=TIERS)
    assert digest
    assert {s.slug for s in specs} == {"local-llama", "azure-us", "european", "general-us"}
    for spec in specs:
        assert spec.simulated is not None
        assert spec.live is None


def test_cloud_only_sim_endpoints_have_no_onprem_provider():
    specs, digest = load_endpoints(CLOUD_ONLY_SIM, live=False, tiers=TIERS)
    assert digest
    assert specs
    assert all(spec.provider != "onprem" for spec in specs)


def test_spark_live_example_loads_only_with_live_true():
    # A test may only assert this file LOADS with live=True -- it must never
    # be run live (the "<spark>" host is a literal placeholder).
    specs, digest = load_endpoints(SPARK_LIVE_EXAMPLE, live=True, tiers=TIERS)
    assert digest
    assert {s.slug for s in specs} == {
        "qwen3.8-27b", "nemotron-3.5-lightning", "llama3.2:latest"}
    ports = {"qwen3.8-27b": 8000, "nemotron-3.5-lightning": 8001,
             "llama3.2:latest": 11434}
    for spec in specs:
        assert spec.live is not None
        assert spec.live.endpoint == f"http://<spark>:{ports[spec.slug]}/v1"
        assert spec.tier == "cheap"
        assert spec.simulated is None


def test_spark_outage_example_routes_llama_through_the_outage_proxy():
    """The B45 live example (research §11.2): Llama goes through
    scripts/outage_proxy.py on :11500, never straight to Ollama, and keeps
    outage-sim's 4x cost lead so the live run is the simulated scenario."""
    specs, _ = load_endpoints(SPARK_OUTAGE_EXAMPLE, live=True, tiers=TIERS)
    by = {s.slug: s for s in specs}
    assert set(by) == {"llama3.2:latest", "nemotron-3.5-lightning"}
    assert by["llama3.2:latest"].live.endpoint == "http://<spark>:11500/v1"
    assert by["nemotron-3.5-lightning"].live.endpoint == "http://<spark>:8001/v1"
    assert all(s.tier == "cheap" and s.simulated is None for s in specs)


# --------------------------------------------------------------------------
# 2. simulated end-to-end run: starter x healthcare-sim x b45 variants
# --------------------------------------------------------------------------

def test_starter_run_against_healthcare_sim_is_clean(tmp_path):
    result = _run(STARTER_SUITE, HEALTHCARE_SIM, tmp_path / "out",
                  variants_path=B45_VARIANTS, seed=0)

    assert result.exit_code == 0
    assert result.metrics["policy"]["violations_total"] == 0
    assert result.metrics["policy"]["empty_set_mismatches"] == 0
    for name, m in result.metrics["strategies"].items():
        assert m["violation_rate"] == 0.0, name

    # b45 variants are present for every router arm
    variants = ("default", "availability_0", "availability_1", "availability_2")
    arms = ("router_cold", "router_online", "router_warm_prior")
    for arm in arms:
        for variant in variants:
            assert f"{arm}:{variant}" in result.metrics["strategies"]
    assert not any(n == "router_warm" or n.startswith("router_warm:")
                   for n in result.metrics["strategies"])

    # No-failure invariant (B48 spec §8.5): nothing fails in this profile,
    # so availability is exactly 1.0 everywhere and every availability
    # variant within an arm makes the SAME attempt chains (weights other
    # than availability are identical across b45.yaml's variants).
    matrix = [json.loads(line) for line in
              (tmp_path / "out" / "matrix.jsonl").read_text().splitlines()]
    assert all(r["error"] is None for r in matrix)
    choices = [json.loads(line) for line in
               (tmp_path / "out" / "choices.jsonl").read_text().splitlines()]
    for arm in arms:
        chains = {v: [(c["case_id"], c["attempts"]) for c in choices
                      if c["strategy"] == f"{arm}:{v}"] for v in variants}
        assert all(chains[v] == chains["default"] for v in variants), arm
        assert all(json.dumps(result.metrics["strategies"][f"{arm}:{v}"], sort_keys=True)
                   == json.dumps(result.metrics["strategies"][f"{arm}:default"],
                                 sort_keys=True) for v in variants), arm

    # european / general-us are never eligible for a claim case -- residency
    # (onprem-preferring, redaction-driven) and retention exclude them
    # respectively, regardless of which strategy is scored.
    claim_cases = [c for c in result.metrics["cases"] if c["case_id"].startswith("claim-")]
    assert len(claim_cases) == 10
    for c in claim_cases:
        assert "european" not in c["eligible_post"]
        assert "general-us" not in c["eligible_post"]

    # override cases (claim-001..008, escalate_residency_on_detection: false):
    # both same-tier §14 endpoints ARE eligible, so cheapest /
    # best_published_quality / router have a real choice to make.
    override_ids = {f"claim-{n:03d}" for n in range(1, 9)}
    override_cases = [c for c in claim_cases if c["case_id"] in override_ids]
    assert len(override_cases) == 8
    for c in override_cases:
        assert set(c["eligible_post"]) == {"local-llama", "azure-us"}

    # escalated cases (claim-009/010, the field left unset -- production
    # DEFAULT): config/policy.yaml's sensitivity_rules escalates the detected
    # SSN straight to residency: onprem_only, so ONLY local-llama is
    # eligible -- the same claim content, the opposite eligible set, side by
    # side with the override cases above.
    escalated_ids = {"claim-009", "claim-010"}
    escalated_cases = [c for c in claim_cases if c["case_id"] in escalated_ids]
    assert len(escalated_cases) == 2
    for c in escalated_cases:
        assert c["eligible_post"] == ["local-llama"]
        assert any(name == "azure-us" and code == "residency"
                   for name, code in c["rejected"])

    # on-prem-required cases: only the on-prem endpoint is ever eligible.
    onprem_cases = [c for c in result.metrics["cases"]
                    if c["case_id"].startswith("onprem-")]
    assert len(onprem_cases) == 4
    for c in onprem_cases:
        assert c["eligible_post"] == ["local-llama"]


def test_empty_set_run_against_cloud_only_sim_all_reject_cleanly(tmp_path):
    result = _run(EMPTY_SUITE, CLOUD_ONLY_SIM, tmp_path / "out", seed=0)

    assert result.exit_code == 0
    assert result.metrics["policy"]["empty_set_mismatches"] == 0
    assert len(result.metrics["cases"]) == 4
    for c in result.metrics["cases"]:
        assert c["expect_empty"] is True
        assert c["eligible_post"] == []
        assert c["rejected_ok"] is True
        assert c["calls"] == 0
