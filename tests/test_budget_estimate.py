import pytest

from app.apps.policy import AppPolicyView
from app.config.loader import load_settings
from app.policy.context import build_context
from app.routing.router import Router, expected_output_tokens
from tests._litellm_fake import make_test_registry
from tests.onboarding.harness import (
    add_model, conformance_client, disable_seeded_models, onboard,
)


def _router_ctx():
    s = load_settings()
    reg = make_test_registry(s)
    ctx = build_context(s, "req-b", "acme", [], None,
                        app_policy=AppPolicyView(residency="any"), registry=reg)
    return s, reg, ctx


def test_estimate_prices_output_tokens_at_output_price():
    s, reg, ctx = _router_ctx()
    d = Router(s, registry=reg).select(ctx, 1000, 0.1, output_tokens=8192)
    assert d.chosen.name == "local-8b"            # $0.05 in / $0.05 out per 1M
    assert d.est_cost_usd == pytest.approx((1000 * 0.05 + 8192 * 0.05) / 1_000_000)


def test_estimate_uses_default_output_tokens_when_unset():
    s, reg, ctx = _router_ctx()
    default = int(s.policy.budgets["default_estimated_output_tokens"])
    d = Router(s, registry=reg).select(ctx, 1000, 0.1)
    assert d.est_cost_usd == pytest.approx((1000 * 0.05 + default * 0.05) / 1_000_000)


def test_expected_output_tokens_prefers_max_completion_tokens():
    s = load_settings()
    assert expected_output_tokens(s, {"max_completion_tokens": 100, "max_tokens": 9}) == 100
    assert expected_output_tokens(s, {"max_tokens": 9}) == 9
    assert expected_output_tokens(s, None) == int(s.policy.budgets["default_estimated_output_tokens"])


def test_max_tokens_counts_toward_the_budget_over_http():
    # 1 prompt token * $10 + 8192 output * $10 = $0.0819 > $0.05 default cap.
    with conformance_client() as c:
        disable_seeded_models(c)
        add_model(c, "conf-model", price_input=10.0, price_output=10.0)
        app = onboard(c, "est-http")
        big = app.chat({"max_tokens": 8192, "messages": [{"role": "user", "content": "hi"}]})
        small = app.chat({"messages": [{"role": "user", "content": "hi"}]})
    assert big.status_code == 422, big.text
    assert small.status_code == 200, small.text


@pytest.mark.parametrize("field", ["max_tokens", "max_completion_tokens"])
@pytest.mark.parametrize("bad", ["abc", [5], 0, -100_000_000])
def test_invalid_output_token_cap_is_a_clean_4xx(client, field, bad):
    r = client.post("/v1/chat/completions", headers={"Authorization": "Bearer demo-key"},
                    json={field: bad, "messages": [{"role": "user", "content": "hi"}]})
    assert 400 <= r.status_code < 500, r.text
    assert field in r.text


def test_negative_max_tokens_cannot_bypass_the_cost_cap():
    # A 25K-token prompt on a $10/$10 model is over the $0.05 cap on its own; a
    # negative max_tokens must not drive the estimate below the cap.
    prompt = "word " * 100_000
    with conformance_client() as c:
        disable_seeded_models(c)
        add_model(c, "conf-model", price_input=10.0, price_output=10.0)
        app = onboard(c, "est-negative")
        r = app.chat({"max_tokens": -100_000_000,
                      "messages": [{"role": "user", "content": prompt}]})
    assert r.status_code != 200, r.text
    assert 400 <= r.status_code < 500, r.text


def test_tool_schemas_count_as_prompt_tokens(client):
    tools = [{"type": "function", "function": {"name": f"t{i}", "description": "x" * 400,
              "parameters": {"type": "object", "properties": {}}}} for i in range(5)]
    r = client.post("/v1/chat/completions", headers={"Authorization": "Bearer demo-key"},
                    json={"tools": tools, "messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code == 200, r.text
    comp = next(e for e in r.json()["trace"] if "tokens_before" in e)
    assert comp["tokens_before"] >= 500     # 5 x ~400 chars of schema / 4
