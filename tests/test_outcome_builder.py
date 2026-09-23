"""P2b-2 Task 2: pure outcome builder and workload parsing.

`app/apps/outcomes.py` is a pure, content-free module -- it never imports
`app/eval`, never touches a database, and every function here is a dict-in,
dict-out (or scalar-in, scalar-out) transform. The privacy test at the
bottom is the one that matters most: a `resp` fixture is salted with a raw
SSN and email in every free-text field a real trace carries (`choices`,
DecisionRecord/RejectedCandidate/VerificationRecord `reason`), and the built
row must contain neither, even serialized.
"""
from __future__ import annotations

import json

from app.apps.outcomes import (
    BUCKETS,
    STATUSES,
    VERIFY_MODES,
    build_failure_outcome,
    build_ok_outcome,
    feedback_values,
    outcome_context,
    parse_workload,
    prompt_bucket,
)
from app.models import DecisionRecord, ExecutionPolicyContext, RejectedCandidate, VerificationRecord

SSN = "123-45-6789"
EMAIL = "jane@example.com"


def _ctx(**overrides) -> ExecutionPolicyContext:
    kwargs = dict(
        request_id="req-1",
        tenant_id="app-1",
        sensitivity="internal",
        onprem_only=True,
        allowed_providers=frozenset({"onprem"}),
        allowed_models=frozenset(),
        max_cost_usd=1.0,
        max_latency_ms=30_000,
        risk_class="low",
        require_verification=False,
        detected_entities=("US_SSN", "EMAIL_ADDRESS", "US_SSN"),
        redaction_map_id="map-1",
        app_policy_version="apv-1",
    )
    kwargs.update(overrides)
    return ExecutionPolicyContext(**kwargs)


def _verify(required=True, performed=True, passed=True, reason="") -> dict:
    return VerificationRecord(
        required=required, performed=performed, passed=passed,
        reason=reason, latency_ms=1.0).model_dump()


def _rejected(name="c", code="tier_below_target", reason="has ssn 123-45-6789") -> dict:
    return RejectedCandidate(
        name=name, provider="onprem", tier="cheap", reason=reason, code=code
    ).model_dump()


def _router_hop(chosen, hop_index=0, scoring=None, rejected=None,
                 circuit_bypass=False, latency_ms=5.0, reason="ok") -> dict:
    return DecisionRecord(
        step="router", chosen=chosen, chosen_provider="onprem", chosen_tier="cheap",
        reason=reason, rejected=rejected or [], hop_index=hop_index,
        actual_cost_usd=0.001, latency_ms=latency_ms,
        scoring=scoring, circuit_bypass=circuit_bypass,
    ).model_dump()


def _gateway_failed_hop(failed_name, hop_index=0, reason="a failed; retrying",
                        scoring=None, rejected=None, tier_target=None) -> dict:
    scoring = scoring or {"weights": {}, "signals_enabled": False,
                          "latency_scale_ms": 1.0, "max_latency_norm": 1.0,
                          "candidates": [{"name": failed_name, "score": 0.1}]}
    return DecisionRecord(
        step="gateway", chosen=None, reason=reason, hop_index=hop_index,
        scoring=scoring, rejected=rejected or [], tier_target=tier_target,
    ).model_dump()


def _resp(trace, *, prompt_tokens=500, completion_tokens=50, total_usd=0.002,
          trace_id="trace-1", endpoint_registry_version="erv-1",
          choices_content="hello") -> dict:
    return {
        "id": "req-1",
        "trace_id": trace_id,
        "model": "onprem/qwen-7b",
        "choices": [{"index": 0, "message": {"role": "assistant",
                                              "content": choices_content}}],
        "usage": {"prompt_tokens": prompt_tokens,
                  "completion_tokens": completion_tokens,
                  "total_tokens": prompt_tokens + completion_tokens},
        "cost": {"total_usd": total_usd},
        "policy": {"endpoint_registry_version": endpoint_registry_version},
        "trace": trace,
    }


# --- parse_workload ---------------------------------------------------

def test_parse_workload_header_lowercased_and_client():
    assert parse_workload("Claims.Summary", None) == ("claims.summary", "client")


def test_parse_workload_header_beats_metadata():
    assert parse_workload("Claims.Summary", {"workload": "faq"}) == (
        "claims.summary", "client")


def test_parse_workload_metadata_when_no_header():
    assert parse_workload(None, {"workload": "faq"}) == ("faq", "client")


def test_parse_workload_nothing_given():
    assert parse_workload(None, None) == (None, "none")


def test_parse_workload_header_with_space_is_invalid():
    assert parse_workload("claims summary", None) == (None, "invalid")


def test_parse_workload_header_with_script_tag_is_invalid():
    assert parse_workload("<script>", None) == (None, "invalid")


def test_parse_workload_header_65_chars_is_invalid():
    assert parse_workload("a" * 65, None) == (None, "invalid")


def test_parse_workload_header_64_chars_is_valid():
    tag = "a" * 64
    assert parse_workload(tag, None) == (tag, "client")


def test_parse_workload_empty_header_is_invalid():
    assert parse_workload("", None) == (None, "invalid")


def test_parse_workload_metadata_not_a_dict_is_invalid():
    assert parse_workload(None, "not-a-dict") == (None, "invalid")


def test_parse_workload_metadata_workload_not_a_string_is_invalid():
    assert parse_workload(None, {"workload": 123}) == (None, "invalid")


def test_parse_workload_metadata_dict_without_workload_key_is_none():
    assert parse_workload(None, {"other": "x"}) == (None, "none")


# --- prompt_bucket ------------------------------------------------------

def test_prompt_bucket_boundaries():
    assert prompt_bucket(0) == "<1k"
    assert prompt_bucket(999) == "<1k"
    assert prompt_bucket(1000) == "1k-4k"
    assert prompt_bucket(3999) == "1k-4k"
    assert prompt_bucket(4000) == "4k-16k"
    assert prompt_bucket(15999) == "4k-16k"
    assert prompt_bucket(16000) == "16k-64k"
    assert prompt_bucket(63999) == "16k-64k"
    assert prompt_bucket(64000) == ">64k"
    assert set(BUCKETS) == {"<1k", "1k-4k", "4k-16k", "16k-64k", ">64k"}


# --- outcome_context ------------------------------------------------------

def test_outcome_context_sorts_and_dedupes_entity_types():
    ctx = _ctx()
    got = outcome_context(ctx)
    assert got == {
        "sensitivity": "internal",
        "risk_class": "low",
        "onprem_only": True,
        "detected_entity_types": ["EMAIL_ADDRESS", "US_SSN"],
        "app_policy_version": "apv-1",
    }


# --- build_ok_outcome -----------------------------------------------------

def test_build_ok_outcome_single_hop():
    scoring = {"weights": {"cost": 1.0}, "signals_enabled": False,
               "latency_scale_ms": 1.0, "max_latency_norm": 1.0,
               "candidates": [{"name": "onprem/qwen-7b", "score": 0.9}]}
    trace = [
        _router_hop("onprem/qwen-7b", hop_index=0, scoring=scoring,
                    rejected=[_rejected(name="onprem/qwen-14b")]),
        _verify(),
    ]
    resp = _resp(trace, prompt_tokens=1500)
    ctx = outcome_context(_ctx())
    row = build_ok_outcome(
        resp, request_id="req-1", application_id="app-1", outcome_ctx=ctx,
        routing_policy_version="rpv-1", workload="claims.summary",
        workload_source="client")

    assert row["status"] == "ok"
    assert row["request_id"] == "req-1"
    assert row["application_id"] == "app-1"
    assert row["trace_id"] == "trace-1"
    assert row["endpoint_registry_version"] == "erv-1"
    assert row["routing_policy_version"] == "rpv-1"
    assert row["workload"] == "claims.summary"
    assert row["workload_source"] == "client"
    assert row["chosen"] == "onprem/qwen-7b"
    assert row["eligible"] == ["onprem/qwen-7b"]
    assert row["rejected"] == [{"name": "onprem/qwen-14b", "code": "tier_below_target"}]
    assert row["attempts"] == ["onprem/qwen-7b"]
    assert row["scoring"] == scoring
    assert row["circuit_bypass"] is False
    assert row["cost_usd"] == 0.002
    assert row["prompt_tokens"] == 1500
    assert row["completion_tokens"] == 50
    assert row["prompt_tokens_bucket"] == "1k-4k"
    assert row["latency_ms"] == 6.0  # router hop 5.0 + verify 1.0
    assert row["verify_required"] is True
    assert row["verify_performed"] is True
    assert row["verify_passed"] is True
    assert row["verify_mode"] == "verified"
    assert row["tier_target"] is None  # Task 3 not on this branch yet
    assert row["app_policy_version"] == "apv-1"
    assert row["sensitivity"] == "internal"
    assert row["risk_class"] == "low"
    assert row["onprem_only"] is True
    assert row["detected_entity_types"] == ["EMAIL_ADDRESS", "US_SSN"]


def test_build_ok_outcome_failover_then_router_hop():
    scoring_a = {"weights": {}, "signals_enabled": False, "latency_scale_ms": 1.0,
                 "max_latency_norm": 1.0, "candidates": [{"name": "a", "score": 0.1}]}
    scoring_b = {"weights": {}, "signals_enabled": False, "latency_scale_ms": 1.0,
                 "max_latency_norm": 1.0, "candidates": [{"name": "b", "score": 0.9}]}
    trace = [
        _gateway_failed_hop("a", hop_index=0),
        _router_hop("b", hop_index=1, scoring=scoring_b),
        _verify(),
    ]
    resp = _resp(trace)
    ctx = outcome_context(_ctx())
    row = build_ok_outcome(
        resp, request_id="req-2", application_id="app-1", outcome_ctx=ctx,
        routing_policy_version="rpv-1", workload=None, workload_source="none")

    assert row["attempts"] == ["a", "b"]
    assert row["chosen"] == "b"
    # the request's FIRST routing decision is the one that picked `a` (the
    # failed gateway hop), not the later router hop that served `b`
    assert row["scoring"]["candidates"] == [{"name": "a", "score": 0.1}]
    assert row["eligible"] == ["a"]


def test_build_ok_outcome_failover_first_decision_is_the_failed_hop():
    """Ruling (final review #4): eligible/scoring/tier_target/rejected all
    describe the request's FIRST routing decision -- after a failover that is
    the failed gateway hop, which carries its own scoring and rejected set."""
    scoring_a = {"weights": {}, "signals_enabled": False, "latency_scale_ms": 1.0,
                 "max_latency_norm": 1.0,
                 "candidates": [{"name": "a", "score": 0.9}, {"name": "b", "score": 0.5}]}
    scoring_b = {"weights": {}, "signals_enabled": False, "latency_scale_ms": 1.0,
                 "max_latency_norm": 1.0, "candidates": [{"name": "b", "score": 0.5}]}
    trace = [
        _gateway_failed_hop("a", hop_index=0, scoring=scoring_a,
                            rejected=[_rejected(name="z", code="residency")],
                            tier_target="cheap"),
        _router_hop("b", hop_index=1, scoring=scoring_b,
                    rejected=[_rejected(name="y", code="circuit_open")]),
        _verify(),
    ]
    row = build_ok_outcome(
        _resp(trace), request_id="req-2", application_id="app-1",
        outcome_ctx=outcome_context(_ctx()), routing_policy_version="rpv-1",
        workload=None, workload_source="none")
    assert row["chosen"] == "b"
    assert row["attempts"] == ["a", "b"]
    assert row["scoring"] == scoring_a
    assert row["eligible"] == ["a", "b"]
    assert row["rejected"] == [{"name": "z", "code": "residency"}]
    assert row["tier_target"] == "cheap"


def test_build_ok_outcome_failover_tier_target_falls_back_to_first_router_hop():
    """A failed gateway hop with no `tier_target` of its own (an older
    trace) takes it from the first router hop."""
    router = _router_hop("b", hop_index=1)
    router["tier_target"] = "mid"
    trace = [_gateway_failed_hop("a", hop_index=0), router, _verify()]
    row = build_ok_outcome(
        _resp(trace), request_id="req-2", application_id="app-1",
        outcome_ctx=outcome_context(_ctx()), routing_policy_version="rpv-1",
        workload=None, workload_source="none")
    assert row["tier_target"] == "mid"


def test_build_ok_outcome_escalation_last_chosen_wins():
    scoring_1 = {"weights": {}, "signals_enabled": False, "latency_scale_ms": 1.0,
                 "max_latency_norm": 1.0,
                 "candidates": [{"name": "cheap-x", "score": 0.5}]}
    trace = [
        _router_hop("cheap-x", hop_index=0, scoring=scoring_1),
        _router_hop("mid-y", hop_index=1),
        _verify(),
    ]
    resp = _resp(trace)
    ctx = outcome_context(_ctx())
    row = build_ok_outcome(
        resp, request_id="req-3", application_id="app-1", outcome_ctx=ctx,
        routing_policy_version="rpv-1", workload=None, workload_source="none")

    assert row["chosen"] == "mid-y"
    assert row["attempts"] == ["cheap-x", "mid-y"]
    # eligible comes from the FIRST router hop's scoring
    assert row["eligible"] == ["cheap-x"]


def test_build_ok_outcome_escalation_first_decision_describes_eligible_scoring_tier():
    """After a confidence escalation the first (unserved) router hop is the
    first routing decision: eligible/scoring/tier_target/rejected come from
    it, `chosen` is the escalated, served model."""
    scoring_1 = {"weights": {}, "signals_enabled": False, "latency_scale_ms": 1.0,
                 "max_latency_norm": 1.0,
                 "candidates": [{"name": "cheap-x", "score": 0.5}]}
    scoring_2 = {"weights": {}, "signals_enabled": False, "latency_scale_ms": 1.0,
                 "max_latency_norm": 1.0,
                 "candidates": [{"name": "mid-y", "score": 0.7}]}
    first = _router_hop("cheap-x", hop_index=0, scoring=scoring_1,
                        rejected=[_rejected(name="q", code="residency")])
    first["tier_target"] = "cheap"
    second = _router_hop("mid-y", hop_index=1, scoring=scoring_2)
    second["tier_target"] = "mid"
    row = build_ok_outcome(
        _resp([first, second, _verify()]), request_id="req-3",
        application_id="app-1", outcome_ctx=outcome_context(_ctx()),
        routing_policy_version="rpv-1", workload=None, workload_source="none")
    assert row["chosen"] == "mid-y"
    assert row["scoring"] == scoring_1
    assert row["eligible"] == ["cheap-x"]
    assert row["tier_target"] == "cheap"
    assert row["rejected"] == [{"name": "q", "code": "residency"}]


def test_build_ok_outcome_cache_hit_sets_cache_hit_and_no_chosen():
    trace = [_verify(required=False, performed=False),
             {"step": "cache", "enabled": True, "hit": True, "latency_ms": 2.0}]
    row = build_ok_outcome(
        _resp(trace), request_id="req-c", application_id="app-1",
        outcome_ctx=outcome_context(_ctx()), routing_policy_version="rpv-1",
        workload=None, workload_source="none")
    assert row["cache_hit"] is True
    assert row["chosen"] is None
    assert row["attempts"] == []


def test_build_ok_outcome_cache_miss_is_not_a_cache_hit():
    trace = [_router_hop("a"), _verify(),
             {"step": "cache", "enabled": True, "hit": False}]
    row = build_ok_outcome(
        _resp(trace), request_id="req-c", application_id="app-1",
        outcome_ctx=outcome_context(_ctx()), routing_policy_version="rpv-1",
        workload=None, workload_source="none")
    assert row["cache_hit"] is False


def test_build_ok_outcome_verify_not_required():
    # Plain not-required record: required=False, a reason that is neither
    # a "waived*" nor "incremental" marker.
    trace = [_router_hop("m"), _verify(required=False, reason="not required")]
    row = build_ok_outcome(
        _resp(trace), request_id="r", application_id="a",
        outcome_ctx=outcome_context(_ctx()), routing_policy_version="rpv",
        workload=None, workload_source="none")
    assert row["verify_mode"] == "not_required"


def test_build_ok_outcome_verify_waived_stream():
    # REAL shape (app/pipeline.py run_stream ~1286 + routes.py
    # _finalize_stream's overwrite ~1275-1278): stream_unverified always
    # records required=False alongside the "waived: ..." reason -- the
    # reason prefix must win over `required` for this to classify right.
    trace = [_router_hop("m"),
             _verify(required=False, performed=False,
                     reason="waived: stream_unverified")]
    row = build_ok_outcome(
        _resp(trace), request_id="r", application_id="a",
        outcome_ctx=outcome_context(_ctx()), routing_policy_version="rpv",
        workload=None, workload_source="none")
    assert row["verify_mode"] == "waived_stream"


def test_build_ok_outcome_verify_incremental():
    # REAL shape: incremental_verify also always records required=False
    # (same run_stream floor) alongside reason="incremental".
    trace = [_router_hop("m"),
             _verify(required=False, performed=True, reason="incremental")]
    row = build_ok_outcome(
        _resp(trace), request_id="r", application_id="a",
        outcome_ctx=outcome_context(_ctx()), routing_policy_version="rpv",
        workload=None, workload_source="none")
    assert row["verify_mode"] == "incremental"


def test_build_ok_outcome_verify_failed():
    trace = [_router_hop("m"), _verify(passed=False, reason="entities leaked")]
    row = build_ok_outcome(
        _resp(trace), request_id="r", application_id="a",
        outcome_ctx=outcome_context(_ctx()), routing_policy_version="rpv",
        workload=None, workload_source="none")
    assert row["verify_mode"] == "failed"


def test_build_ok_outcome_verify_verified():
    trace = [_router_hop("m"), _verify()]
    row = build_ok_outcome(
        _resp(trace), request_id="r", application_id="a",
        outcome_ctx=outcome_context(_ctx()), routing_policy_version="rpv",
        workload=None, workload_source="none")
    assert row["verify_mode"] == "verified"
    assert set(VERIFY_MODES) == {
        "not_required", "verified", "failed", "waived_stream", "incremental"}


def test_build_ok_outcome_circuit_bypass_true_if_any_hop():
    trace = [_router_hop("m", circuit_bypass=True), _verify()]
    row = build_ok_outcome(
        _resp(trace), request_id="r", application_id="a",
        outcome_ctx=outcome_context(_ctx()), routing_policy_version="rpv",
        workload=None, workload_source="none")
    assert row["circuit_bypass"] is True


def test_build_ok_outcome_reads_complexity_from_outcome_ctx():
    # complexity is not a resp key (Pipeline.run computes it as a local
    # variable and never places it on resp) -- it flows through outcome_ctx
    # instead, so a later task's caller can add it there.
    trace = [_router_hop("m"), _verify()]
    ctx = outcome_context(_ctx())
    ctx["complexity"] = 0.37
    row = build_ok_outcome(
        _resp(trace), request_id="r", application_id="a", outcome_ctx=ctx,
        routing_policy_version="rpv", workload=None, workload_source="none")
    assert row["complexity"] == 0.37


def test_build_ok_outcome_complexity_absent_is_none():
    trace = [_router_hop("m"), _verify()]
    row = build_ok_outcome(
        _resp(trace), request_id="r", application_id="a",
        outcome_ctx=outcome_context(_ctx()), routing_policy_version="rpv",
        workload=None, workload_source="none")
    assert row["complexity"] is None


# --- privacy ---------------------------------------------------------------

def test_build_ok_outcome_is_content_free():
    scoring = {"weights": {}, "signals_enabled": False, "latency_scale_ms": 1.0,
               "max_latency_norm": 1.0, "candidates": [{"name": "m", "score": 0.5}]}
    trace = [
        _router_hop("m", hop_index=0, scoring=scoring,
                    rejected=[_rejected(reason=f"blocked, saw {SSN} and {EMAIL}")],
                    reason=f"chosen despite {SSN}"),
        _verify(reason=f"entities: {SSN} {EMAIL}"),
    ]
    resp = _resp(trace, choices_content=f"Your SSN is {SSN} and email {EMAIL}",
                 trace_id="trace-pii")
    row = build_ok_outcome(
        resp, request_id="req-pii", application_id="app-1",
        outcome_ctx=outcome_context(_ctx()), routing_policy_version="rpv-1",
        workload=None, workload_source="none")

    blob = json.dumps(row, default=str)
    assert SSN not in blob
    assert EMAIL not in blob


# --- build_failure_outcome --------------------------------------------------

def test_build_failure_outcome_no_eligible_stores_only_name_and_code():
    rejected = (
        RejectedCandidate(name="x", provider="onprem", tier="cheap",
                          reason="has ssn 123-45-6789", code="residency").model_dump(),
    )
    row = build_failure_outcome(
        status="no_eligible", request_id="req-4", application_id="app-1",
        outcome_ctx=outcome_context(_ctx()), routing_policy_version="rpv-1",
        endpoint_registry_version="erv-1", workload=None, workload_source="none",
        rejected=rejected)

    assert row["status"] == "no_eligible"
    assert row["rejected"] == [{"name": "x", "code": "residency"}]
    assert row["chosen"] is None
    assert row["eligible"] == []
    assert row["scoring"] is None
    assert row["trace_id"] is None
    assert row["cost_usd"] == 0.0
    assert row["prompt_tokens"] == 0
    assert row["completion_tokens"] == 0
    assert row["verify_mode"] is None
    assert row["sensitivity"] == "internal"
    assert row["app_policy_version"] == "apv-1"
    assert SSN not in json.dumps(row, default=str)


def test_build_failure_outcome_defaults_are_null_or_empty():
    row = build_failure_outcome(
        status="backend_failed", request_id="req-5", application_id="app-1",
        outcome_ctx=outcome_context(_ctx()), routing_policy_version="rpv-1",
        endpoint_registry_version=None, workload=None, workload_source="none")
    assert row["attempts"] == []
    assert row["rejected"] == []
    assert row["latency_ms"] == 0.0
    assert row["tier_target"] is None
    assert row["complexity"] is None
    assert row["prompt_tokens_bucket"] is None


def test_build_failure_outcome_reads_complexity_from_outcome_ctx():
    ctx = outcome_context(_ctx())
    ctx["complexity"] = 0.81
    row = build_failure_outcome(
        status="backend_failed", request_id="req-6", application_id="app-1",
        outcome_ctx=ctx, routing_policy_version="rpv-1",
        endpoint_registry_version=None, workload=None, workload_source="none")
    assert row["complexity"] == 0.81


def test_build_failure_outcome_verify_failed_reads_the_stashed_trace():
    """Ruling (final review #1): a step-9 failure served a model before it
    was refused, so the verify_failed row derives attempts/chosen/scoring/
    verify_*/cost/tokens from the pipeline's stashed trace -- through the
    SAME trace-reading logic as `build_ok_outcome`."""
    scoring = {"weights": {}, "signals_enabled": False, "latency_scale_ms": 1.0,
               "max_latency_norm": 1.0, "candidates": [{"name": "m", "score": 0.9}]}
    hop = _router_hop("m", scoring=scoring, rejected=[_rejected(name="n")],
                      reason=f"routed; {SSN}")
    hop["tier_target"] = "mid"
    trace = [hop, _verify(required=True, performed=True, passed=False,
                          reason=f"failed closed {EMAIL}")]
    row = build_failure_outcome(
        status="verify_failed", request_id="req-v", application_id="app-1",
        outcome_ctx=outcome_context(_ctx()), routing_policy_version="rpv-1",
        endpoint_registry_version="erv-1", workload=None, workload_source="none",
        trace=trace, prompt_tokens=1500, completion_tokens=40, cost_usd=0.003,
        latency_ms=12.0)
    assert row["status"] == "verify_failed"
    assert row["chosen"] == "m"
    assert row["attempts"] == ["m"]
    assert row["eligible"] == ["m"]
    assert row["scoring"] == scoring
    assert row["tier_target"] == "mid"
    assert row["rejected"] == [{"name": "n", "code": "tier_below_target"}]
    assert row["verify_required"] is True
    assert row["verify_performed"] is True
    assert row["verify_passed"] is False
    assert row["verify_mode"] == "failed"
    assert row["cost_usd"] == 0.003
    assert row["prompt_tokens"] == 1500
    assert row["completion_tokens"] == 40
    assert row["prompt_tokens_bucket"] == "1k-4k"
    assert row["latency_ms"] == 12.0
    assert row["cache_hit"] is False
    dumped = json.dumps(row, default=str)
    assert SSN not in dumped and EMAIL not in dumped


# --- feedback_values -----------------------------------------------------

def test_feedback_values_accepts_a_slashed_model_slug():
    slug = "nvidia/llama-3.1-nemotron-nano-8b-v1"
    assert feedback_values("preferred_model", slug) == (None, slug)


def test_feedback_values_categorical_length_limit_is_100():
    assert feedback_values("preferred_model", "a" * 100) == (None, "a" * 100)
    assert feedback_values("preferred_model", "a" * 101) is None


def test_feedback_values_still_refuses_spaces_and_markup():
    assert feedback_values("failure_type", "bad value!") is None
    assert feedback_values("failure_type", "<script>") is None


def test_feedback_values_refuses_non_finite_numbers():
    assert feedback_values("quality", float("nan")) is None
    assert feedback_values("quality", float("inf")) is None
    assert feedback_values("quality", float("-inf")) is None
    assert feedback_values("quality", 0.5) == (0.5, None)


def test_statuses_constant():
    assert set(STATUSES) == {
        "ok", "backend_failed", "verify_failed", "no_eligible", "context_overflow"}
