"""Tests for `app.eval.simulated` (Task 3, FeasibleRouteBench).

Covers `SimulatedBackend`'s determinism, quality/latency draws, token/cost
accounting, and the `(case.id, model)` message capture used by the
redaction test. See
the FeasibleRouteBench design spec §5.1.
"""
from __future__ import annotations

import asyncio

import pytest
from pydantic import ValidationError

from app.config.schema import PriceCfg
from app.eval.cases import Case
from app.eval.endpoints import EndpointSpec, LatencyProfile, OutageSpan, SimProfile
from app.eval.graders import GraderSpec
from app.eval.simulated import SimulatedBackend, SimulatedEndpointFailure
from app.gateway.backends.base import count_tokens
from app.gateway.gateway import counts_as_endpoint_unhealth
from app.models import Message
from app.routing.pricing import price_call


def _endpoint(slug="local-llama", quality=None, mean=900.0, jitter=100.0,
              outages=None, error_rate=0.0, failure_latency_ms=None) -> EndpointSpec:
    return EndpointSpec(
        slug=slug,
        provider="onprem",
        tier="cheap",
        price={"input": 0.08, "output": 0.08},
        simulated=SimProfile(
            quality=quality or {"default": 0.9},
            latency_ms=LatencyProfile(mean=mean, jitter=jitter),
            outages=outages or [],
            error_rate=error_rate,
            failure_latency_ms=failure_latency_ms,
        ),
    )


def _case(case_id="case-1", kind="exact", expected="approved", sample=None,
          tags=None) -> Case:
    return Case(
        id=case_id,
        messages=[{"role": "user", "content": "Is the claim approved?"}],
        grader=GraderSpec(kind=kind, expected=expected, sample=sample),
        tags=tags or {},
    )


_PRICE = PriceCfg(input=0.08, output=0.08, tier="cheap", provider="onprem")
_MESSAGES = [Message(role="user", content="Is the claim approved?")]


def _generate(backend: SimulatedBackend, case: Case, model="local-llama",
              messages=None, price=None, index=0):
    backend.set_case(case, index)
    return asyncio.run(backend.generate(
        model, messages or _MESSAGES, price or _PRICE, "cheap", {}))


class TestDeterminism:
    def test_same_seed_same_text_and_latency(self):
        case = _case()
        b1 = SimulatedBackend([_endpoint()], seed=42)
        b2 = SimulatedBackend([_endpoint()], seed=42)
        r1 = _generate(b1, case)
        r2 = _generate(b2, case)
        assert r1.text == r2.text
        assert r1.latency_ms == r2.latency_ms
        assert r1.cost_usd == r2.cost_usd

    def test_different_seed_changes_at_least_one_outcome_over_50_draws(self):
        endpoint_a = _endpoint(quality={"default": 0.5})
        endpoint_b = _endpoint(quality={"default": 0.5})
        ba = SimulatedBackend([endpoint_a], seed=1)
        bb = SimulatedBackend([endpoint_b], seed=2)
        outcomes = []
        for i in range(50):
            case = _case(case_id=f"case-{i}")
            outcomes.append((_generate(ba, case).text, _generate(bb, case).text))
        differing_text = sum(1 for a, b in outcomes if a != b)
        assert differing_text >= 1


class TestQualityThreshold:
    def test_quality_zero_is_always_wrong(self):
        endpoint = _endpoint(quality={"default": 0.0})
        for i in range(20):
            case = _case(case_id=f"c{i}")
            backend = SimulatedBackend([endpoint], seed=7)
            result = _generate(backend, case)
            assert result.text == "SIMULATED-WRONG"

    def test_quality_one_is_always_right(self):
        endpoint = _endpoint(quality={"default": 1.0})
        for i in range(20):
            case = _case(case_id=f"c{i}")
            backend = SimulatedBackend([endpoint], seed=7)
            result = _generate(backend, case)
            assert result.text == "approved"

    def test_workload_tag_selects_quality_over_default(self):
        endpoint = _endpoint(quality={"claim_summary": 1.0, "default": 0.0})
        case = _case(tags={"workload": "claim_summary"})
        backend = SimulatedBackend([endpoint], seed=7)
        result = _generate(backend, case)
        assert result.text == "approved"

    def test_unknown_workload_falls_back_to_default(self):
        endpoint = _endpoint(quality={"claim_summary": 0.0, "default": 1.0})
        case = _case(tags={"workload": "faq"})
        backend = SimulatedBackend([endpoint], seed=7)
        result = _generate(backend, case)
        assert result.text == "approved"


class TestExpectedAnswerRendering:
    def test_exact_grader_uses_str_expected(self):
        endpoint = _endpoint(quality={"default": 1.0})
        case = _case(kind="exact", expected="approved")
        backend = SimulatedBackend([endpoint], seed=1)
        assert _generate(backend, case).text == "approved"

    def test_numeric_grader_uses_str_expected(self):
        endpoint = _endpoint(quality={"default": 1.0})
        case = _case(kind="numeric", expected=42)
        backend = SimulatedBackend([endpoint], seed=1)
        # `GraderSpec.expected` is typed `str | dict | float`, so pydantic
        # coerces the int 42 to 42.0 -- `str(expected)` therefore renders
        # "42.0", which the numeric grader still parses back to 42.0.
        assert _generate(backend, case).text == "42.0"

    def test_regex_grader_uses_sample(self):
        endpoint = _endpoint(quality={"default": 1.0})
        case = _case(kind="regex", expected=r"\d{3}-\d{2}", sample="123-45")
        backend = SimulatedBackend([endpoint], seed=1)
        assert _generate(backend, case).text == "123-45"


class TestLatency:
    def test_latency_within_mean_plus_minus_jitter(self):
        endpoint = _endpoint(mean=900.0, jitter=100.0)
        case = _case()
        backend = SimulatedBackend([endpoint], seed=3)
        result = _generate(backend, case)
        assert 800.0 <= result.latency_ms <= 1000.0

    def test_latency_floor_is_one_ms(self):
        endpoint = _endpoint(mean=0.1, jitter=0.0)
        case = _case()
        backend = SimulatedBackend([endpoint], seed=3)
        result = _generate(backend, case)
        assert result.latency_ms == 1.0


class TestCostAndTokens:
    def test_cost_equals_price_call_of_counted_tokens(self):
        endpoint = _endpoint(quality={"default": 1.0})
        case = _case(kind="exact", expected="approved")
        backend = SimulatedBackend([endpoint], seed=5)
        result = _generate(backend, case)

        expected_tokens_in = sum(count_tokens(m.content) for m in _MESSAGES)
        expected_tokens_out = count_tokens("approved")
        expected_cost = price_call(
            _PRICE, prompt_tokens=expected_tokens_in, completion_tokens=expected_tokens_out)

        assert result.tokens_in == expected_tokens_in
        assert result.tokens_out == expected_tokens_out
        assert result.cost_usd == expected_cost

    def test_confidence_is_always_one(self):
        endpoint = _endpoint()
        case = _case()
        backend = SimulatedBackend([endpoint], seed=5)
        result = _generate(backend, case)
        assert result.confidence == 1.0


class TestReceivedMessages:
    def test_generate_records_messages_by_case_and_model(self):
        endpoint = _endpoint()
        case = _case(case_id="claim-9")
        backend = SimulatedBackend([endpoint], seed=5)
        _generate(backend, case, model="local-llama", messages=_MESSAGES)
        assert backend.received[("claim-9", "local-llama")] == _MESSAGES


class TestErrorPaths:
    def test_generate_without_set_case_raises(self):
        backend = SimulatedBackend([_endpoint()], seed=5)
        with pytest.raises(RuntimeError):
            asyncio.run(backend.generate("local-llama", _MESSAGES, _PRICE, "cheap", {}))

    def test_generate_unknown_model_raises(self):
        backend = SimulatedBackend([_endpoint()], seed=5)
        backend.set_case(_case(), 0)
        with pytest.raises(ValueError):
            asyncio.run(backend.generate("not-onboarded", _MESSAGES, _PRICE, "cheap", {}))

    def test_generate_stream_raises_not_implemented(self):
        backend = SimulatedBackend([_endpoint()], seed=5)
        backend.set_case(_case(), 0)
        with pytest.raises(NotImplementedError):
            backend.generate_stream("local-llama", _MESSAGES, _PRICE, "cheap", {})


class TestOutages:
    def test_raises_on_every_index_in_span_inclusive(self):
        endpoint = _endpoint(outages=[OutageSpan(**{"from": 2, "to": 4})])
        backend = SimulatedBackend([endpoint], seed=1)
        for index in (2, 3, 4):
            with pytest.raises(SimulatedEndpointFailure):
                _generate(backend, _case(case_id=f"c{index}"), index=index)

    def test_does_not_raise_outside_span(self):
        endpoint = _endpoint(outages=[OutageSpan(**{"from": 2, "to": 4})])
        backend = SimulatedBackend([endpoint], seed=1)
        for index in (0, 1, 5, 6):
            _generate(backend, _case(case_id=f"c{index}"), index=index)  # does not raise

    def test_outage_failure_has_no_status_code(self):
        endpoint = _endpoint(outages=[OutageSpan(**{"from": 0, "to": 0})])
        backend = SimulatedBackend([endpoint], seed=1)
        with pytest.raises(SimulatedEndpointFailure) as exc_info:
            _generate(backend, _case(), index=0)
        assert exc_info.value.status_code is None

    def test_outage_checked_before_flake_draw(self):
        # error_rate=0.0 would never flake on its own -- an outage inside a
        # zero-error-rate endpoint must still raise.
        endpoint = _endpoint(outages=[OutageSpan(**{"from": 0, "to": 0})], error_rate=0.0)
        backend = SimulatedBackend([endpoint], seed=1)
        with pytest.raises(SimulatedEndpointFailure):
            _generate(backend, _case(), index=0)

    def test_failed_call_is_still_recorded_in_received(self):
        endpoint = _endpoint(outages=[OutageSpan(**{"from": 0, "to": 0})])
        backend = SimulatedBackend([endpoint], seed=1)
        case = _case(case_id="claim-fail")
        with pytest.raises(SimulatedEndpointFailure):
            _generate(backend, case, index=0)
        assert backend.received[("claim-fail", "local-llama")] == _MESSAGES


class TestErrorRate:
    def test_zero_error_rate_never_flakes(self):
        endpoint = _endpoint(error_rate=0.0)
        backend = SimulatedBackend([endpoint], seed=1)
        for i in range(50):
            _generate(backend, _case(case_id=f"c{i}"), index=i)  # never raises

    def test_full_error_rate_always_flakes(self):
        endpoint = _endpoint(error_rate=1.0)
        backend = SimulatedBackend([endpoint], seed=1)
        for i in range(50):
            with pytest.raises(SimulatedEndpointFailure):
                _generate(backend, _case(case_id=f"c{i}"), index=i)

    def test_flake_failure_has_status_code_503(self):
        endpoint = _endpoint(error_rate=1.0)
        backend = SimulatedBackend([endpoint], seed=1)
        with pytest.raises(SimulatedEndpointFailure) as exc_info:
            _generate(backend, _case(), index=0)
        assert exc_info.value.status_code == 503


class TestFailurePatternDeterminism:
    def test_same_seed_gives_same_failure_pattern_over_50_cases(self):
        endpoint = _endpoint(error_rate=0.5)
        b1 = SimulatedBackend([endpoint], seed=42)
        b2 = SimulatedBackend([endpoint], seed=42)
        pattern1, pattern2 = [], []
        for i in range(50):
            for backend, pattern in ((b1, pattern1), (b2, pattern2)):
                try:
                    _generate(backend, _case(case_id=f"c{i}"), index=i)
                except SimulatedEndpointFailure:
                    pattern.append(True)
                else:
                    pattern.append(False)
        assert pattern1 == pattern2

    def test_different_seed_gives_different_failure_pattern_over_50_cases(self):
        endpoint = _endpoint(error_rate=0.5)
        ba = SimulatedBackend([endpoint], seed=1)
        bb = SimulatedBackend([endpoint], seed=2)
        pattern_a, pattern_b = [], []
        for i in range(50):
            for backend, pattern in ((ba, pattern_a), (bb, pattern_b)):
                try:
                    _generate(backend, _case(case_id=f"c{i}"), index=i)
                except SimulatedEndpointFailure:
                    pattern.append(True)
                else:
                    pattern.append(False)
        assert pattern_a != pattern_b


class TestCountsAsEndpointUnhealth:
    def test_outage_failure_counts_as_unhealth(self):
        endpoint = _endpoint(outages=[OutageSpan(**{"from": 0, "to": 0})])
        backend = SimulatedBackend([endpoint], seed=1)
        with pytest.raises(SimulatedEndpointFailure) as exc_info:
            _generate(backend, _case(), index=0)
        assert counts_as_endpoint_unhealth(exc_info.value) is True

    def test_flake_failure_counts_as_unhealth(self):
        endpoint = _endpoint(error_rate=1.0)
        backend = SimulatedBackend([endpoint], seed=1)
        with pytest.raises(SimulatedEndpointFailure) as exc_info:
            _generate(backend, _case(), index=0)
        assert counts_as_endpoint_unhealth(exc_info.value) is True


class TestFailureLatency:
    def test_default_failure_latency_is_profile_mean(self):
        endpoint = _endpoint(mean=750.0, outages=[OutageSpan(**{"from": 0, "to": 0})])
        backend = SimulatedBackend([endpoint], seed=1)
        with pytest.raises(SimulatedEndpointFailure) as exc_info:
            _generate(backend, _case(), index=0)
        assert exc_info.value.failure_latency_ms == 750.0

    def test_explicit_failure_latency_overrides_mean(self):
        endpoint = _endpoint(mean=750.0, failure_latency_ms=42.0,
                              outages=[OutageSpan(**{"from": 0, "to": 0})])
        backend = SimulatedBackend([endpoint], seed=1)
        with pytest.raises(SimulatedEndpointFailure) as exc_info:
            _generate(backend, _case(), index=0)
        assert exc_info.value.failure_latency_ms == 42.0


class TestFailureFieldValidation:
    def test_outage_from_greater_than_to_raises(self):
        with pytest.raises(ValidationError):
            OutageSpan(**{"from": 5, "to": 2})

    def test_outage_negative_from_raises(self):
        with pytest.raises(ValidationError):
            OutageSpan(**{"from": -1, "to": 2})

    def test_error_rate_above_one_raises(self):
        with pytest.raises(ValidationError):
            _endpoint(error_rate=1.5)

    def test_error_rate_below_zero_raises(self):
        with pytest.raises(ValidationError):
            _endpoint(error_rate=-0.1)

    def test_failure_latency_ms_zero_raises(self):
        with pytest.raises(ValidationError):
            _endpoint(failure_latency_ms=0.0)

    def test_failure_latency_ms_negative_raises(self):
        with pytest.raises(ValidationError):
            _endpoint(failure_latency_ms=-5.0)


class TestHealth:
    def test_health_reports_ok_and_known_models(self):
        backend = SimulatedBackend([_endpoint(slug="local-llama"), _endpoint(slug="other")],
                                    seed=1)
        health = asyncio.run(backend.health())
        assert health.ok is True
        assert health.models == ["local-llama", "other"]

    def test_name_is_simulated(self):
        assert SimulatedBackend([], seed=1).name == "simulated"
