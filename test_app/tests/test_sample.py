from __future__ import annotations

import json

import httpx
from pydantic import SecretStr

from routeforge_sample.agent import SupportInvestigator
from routeforge_sample.routeforge import RouteForgeChatModel, RouteForgePolicyError


def _response(call_number: int, request: httpx.Request) -> httpx.Response:
    body = json.loads(request.content)
    assert body["model"] == "auto"
    assert body["stream"] is False
    content = ("Inspect the customer and service records." if call_number == 1 else
               "The evidence supports a router authentication reset." if call_number == 2 else
               "Reset the router authentication binding; escalate if the loop continues.")
    policy = {
        "sensitivity": "low" if call_number == 1 else "high",
        "onprem_only": call_number > 1,
        "allowed_providers": ["onprem"],
        "detected_entities": [] if call_number == 1 else ["EMAIL_ADDRESS", "PHONE_NUMBER"],
    }
    return httpx.Response(
        200,
        json={
            "id": f"req-{call_number}",
            "model": "local-8b",
            "model_display": "Local test model",
            "choices": [{"message": {"role": "assistant", "content": content}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
            "trace": [{"step": "router", "chosen": "local-8b", "reason": "test"}],
            "cost": {"total_usd": 0.001, "baseline_usd": 0.003},
            "quality": {"chosen": 0.5},
            "policy": policy,
        },
        request=request,
    )


def _model(handler) -> RouteForgeChatModel:
    model = RouteForgeChatModel(
        base_url="http://routeforge.test/v1",
        api_key=SecretStr("rf_live_test"),
        max_rate_limit_retries=0,
    )
    model._client.close()
    model._client = httpx.Client(transport=httpx.MockTransport(handler))
    return model


def test_agent_runs_three_independently_traced_calls() -> None:
    count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal count
        count += 1
        return _response(count, request)

    model = _model(handler)
    try:
        result = SupportInvestigator(model).investigate("TCK-1001")
    finally:
        model.close()

    assert count == 3
    assert len(result.calls) == 3
    assert result.calls[0]["policy"]["onprem_only"] is False
    assert result.calls[1]["policy"]["onprem_only"] is True
    assert result.total_cost_usd == 0.003
    assert "Reset the router" in result.answer


def test_503_is_exposed_as_fail_closed_policy_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"detail": "policy evaluation unavailable"}, request=request)

    model = _model(handler)
    try:
        try:
            model.invoke("hello")
        except RouteForgePolicyError as exc:
            assert "declined" not in str(exc)
            assert "policy evaluation unavailable" in str(exc)
        else:
            raise AssertionError("expected RouteForgePolicyError")
    finally:
        model.close()


def test_investigation_tags_all_calls_with_one_session() -> None:
    seen: list[str | None] = []
    count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal count
        count += 1
        seen.append(request.headers.get("X-RouteForge-Session"))
        return _response(count, request)

    model = _model(handler)
    try:
        SupportInvestigator(model).investigate("TCK-1001")
    finally:
        model.close()

    assert len(seen) == 3
    assert all(s is not None for s in seen)
    assert len(set(seen)) == 1                      # all three share ONE id
    assert seen[0].startswith("inv-tck-1001-")      # ticket-derived, lowercased


def test_two_runs_get_distinct_sessions() -> None:
    seen: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers.get("X-RouteForge-Session"))
        return _response((len(seen) - 1) % 3 + 1, request)

    model = _model(handler)
    try:
        investigator = SupportInvestigator(model)
        investigator.investigate("TCK-1001")
        investigator.investigate("TCK-1001")
    finally:
        model.close()

    run1, run2 = set(seen[:3]), set(seen[3:])
    assert len(run1) == 1 and len(run2) == 1
    assert run1 != run2                             # per-run nonce -> distinct sessions

