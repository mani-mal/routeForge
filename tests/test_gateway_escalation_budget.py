"""Escalation after a budget fallback must not discard a served answer.

Sequence: mid is over the per-request cap -> the router falls back to cheap and
cheap serves; its confidence is below the threshold, so escalation sets
min_tier=mid; mid is then the floor and still over budget -> NoEligibleCandidate.
The gateway must return the answer it already has (and meter it), with a
DecisionRecord saying why escalation was skipped -- not a misleading 422."""
import asyncio

import pytest

from app.config.loader import load_settings
from app.errors import NoEligibleCandidate
from app.gateway.gateway import Gateway
from app.models import ExecutionPolicyContext, Message
from app.routing.router import Router
from tests._litellm_fake import _DeterministicLiteLLM, make_test_registry

# local-8b: cheap $0.05/$0.05; local-32b: mid $0.10/$0.10 (per 1M).
# 2000 prompt + 100 output tokens: mid est 0.00021, cheap est 0.000105.
CAP = 0.00015


def _ctx(max_cost: float = CAP) -> ExecutionPolicyContext:
    return ExecutionPolicyContext(
        request_id="r", tenant_id="t", sensitivity="public", onprem_only=False,
        allowed_providers=frozenset({"onprem"}),
        # The two seeded on-prem models above -- the eligible set this test has
        # always assumed (an app that narrows nothing, on-prem only). Stated
        # because `ctx.allowed_models` is binding on the router, with no flag
        # that can turn it off (app/policy/feasible.py).
        allowed_models=frozenset({"local-8b", "local-32b"}),
        max_cost_usd=max_cost, max_latency_ms=15000, risk_class="low",
        require_verification=False, detected_entities=(), redaction_map_id=None)


def _run(ctx, hint):
    s = load_settings()
    reg = make_test_registry(s)
    gw = Gateway(s, _DeterministicLiteLLM(s), Router(s, registry=reg), reg)
    msgs = [Message(role="user", content="hello")]
    return asyncio.run(gw.run(ctx, msgs, 2000, 1.0, hint, params={"max_tokens": 100}))


def test_escalation_blocked_by_budget_returns_the_served_answer():
    result, records = _run(_ctx(), {"mock_confidence": 0.1})
    served = [r for r in records if r.step == "router"]
    assert [r.chosen for r in served] == ["local-8b"]
    assert "budget fallback from mid" in served[0].reason
    assert result is not None and result.cost_usd == served[0].actual_cost_usd
    skip = [r for r in records if r.step == "gateway" and r.chosen is None]
    assert len(skip) == 1
    assert skip[0].reason.startswith("escalation skipped: no eligible candidate at mid (")
    assert "no candidates after budget filter" in skip[0].reason


def test_no_prior_result_still_raises():
    # Nothing served yet -> today's behaviour: the NoEligibleCandidate surfaces.
    with pytest.raises(NoEligibleCandidate, match="no candidates after budget filter"):
        _run(_ctx(max_cost=0.00001), {"mock_confidence": 0.1})
