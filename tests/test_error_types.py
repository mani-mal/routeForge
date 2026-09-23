import pytest

from app.errors import BackendUnavailable, NoEligibleCandidate, PolicyGateError


def test_subclass_relationships_hold():
    # Load-bearing: existing pytest.raises(ValueError/RuntimeError) callers must
    # keep passing (tests/test_derived_onprem_reject.py, test_router.py,
    # test_litellm_egress.py).
    assert issubclass(NoEligibleCandidate, ValueError)
    assert issubclass(BackendUnavailable, RuntimeError)
    assert issubclass(PolicyGateError, Exception)


def test_router_raises_no_eligible_candidate_on_empty_pool():
    from app.config.loader import load_settings
    from app.models import ExecutionPolicyContext
    from app.routing.router import Router
    s = load_settings()
    # onprem_only with NO permitted provider -> residency filter empties the pool
    ctx = ExecutionPolicyContext(
        request_id="r", tenant_id="t", sensitivity="restricted", onprem_only=True,
        allowed_providers=frozenset(), allowed_models=frozenset(),
        max_cost_usd=1.0, max_latency_ms=10000, risk_class="high",
        require_verification=True, detected_entities=(), redaction_map_id=None)
    with pytest.raises(NoEligibleCandidate, match="no candidates after residency filter"):
        Router(s).select(ctx, tokens_after=100, complexity=0.1)


def test_no_eligible_candidate_carries_rejected_with_residency_code():
    # P2b-2 Task 3: an onprem_only context with only cloud models in the pool
    # must carry `rejected` as (name, code) tuples, code="residency".
    from app.config.loader import load_settings
    from app.models import ExecutionPolicyContext
    from app.routing.router import Router
    s = load_settings()
    prov = dict(s.serving.providers)
    prov["azure"] = prov["azure"].model_copy(update={"enabled": True})
    s = s.model_copy(update={"serving": s.serving.model_copy(update={"providers": prov})})
    ctx = ExecutionPolicyContext(
        request_id="r", tenant_id="t", sensitivity="restricted", onprem_only=True,
        allowed_providers=frozenset({"onprem"}), allowed_models=frozenset(),
        max_cost_usd=1.0, max_latency_ms=10000, risk_class="high",
        require_verification=True, detected_entities=(), redaction_map_id=None)
    with pytest.raises(NoEligibleCandidate) as exc_info:
        Router(s).select(ctx, tokens_after=100, complexity=0.1)
    rejected = exc_info.value.rejected
    assert rejected
    assert all(isinstance(r, tuple) and len(r) == 2 for r in rejected)
    names = {name for name, _code in rejected}
    assert "azure-gpt-mid" in names
    assert dict(rejected)["azure-gpt-mid"] == "residency"
