"""P3c-4: an arm carries the token counts it priced the call at.

`app/eval/capacity.py` sums them into an endpoint's token demand, so they
must be exactly what `simulate_row` handed to `price_call`.
"""
from __future__ import annotations

import asyncio

from app.config.schema import PriceCfg
from app.eval.simulate import ArmResult, simulate
from app.routing.pricing import price_call
from tests.eval.sim_fixtures import (BASE_SETTINGS, app_entry, make_row, make_snapshot,
                                     onboarded_row)

P = "routeforge.policy-proposal/v1"


def _replay(rows, snapshot, body=None):
    results, _excluded = asyncio.run(simulate(rows, snapshot, {"schema": P, **(body or {})},
                                              base=BASE_SETTINGS))
    return results


def _cheap_only() -> dict:
    return make_snapshot(onboarded=[onboarded_row("local-cheap", "cheap")])


def test_a_routed_arm_carries_the_tokens_it_was_priced_at():
    (r,) = _replay([make_row("r1", "app-a", prompt_tokens=900, completion_tokens=100)],
                   _cheap_only())
    assert (r.current.prompt_tokens, r.current.completion_tokens) == (900, 100)
    assert r.proposed == r.current
    # The same pair reproduces the arm's cost at the fixture's prices (USD per 1M).
    price = PriceCfg(input=0.1, output=0.2, tier="cheap", provider="onprem")
    assert r.current.cost_usd == price_call(price, prompt_tokens=900, completion_tokens=100)


def test_token_scale_scales_the_prompt_of_the_proposed_arm_only():
    (r,) = _replay([make_row("r1", "app-a", prompt_tokens=900, completion_tokens=100)],
                   _cheap_only(), {"workload": {"token_scale": 0.5}})
    assert (r.current.prompt_tokens, r.current.completion_tokens) == (900, 100)
    assert (r.proposed.prompt_tokens, r.proposed.completion_tokens) == (450, 100)


def test_a_row_with_no_backend_usage_carries_the_admission_estimate():
    """No backend token count: the prompt is RouteForge's own routing count
    and the completion is the router's output estimate -- here the client's
    recorded cap."""
    row = make_row("r1", "app-a", prompt_tokens=0, completion_tokens=None, routing_tokens=800,
                   max_tokens=64)
    (r,) = _replay([row], _cheap_only())
    assert (r.current.prompt_tokens, r.current.completion_tokens) == (800, 64)


def test_a_rejected_arm_carries_no_tokens():
    snapshot = make_snapshot(apps=[app_entry("app-a", allowed_models=["local-mid"])])
    (r,) = _replay([make_row("r1", "app-a")], snapshot, {"endpoints": {"remove": ["local-mid"]}})
    assert r.current.outcome == "routed" and r.current.prompt_tokens == 200
    assert r.proposed.outcome == "rejected"
    assert (r.proposed.prompt_tokens, r.proposed.completion_tokens) == (None, None)


def test_the_fields_default_to_none_for_every_existing_caller():
    arm = ArmResult("routed", "m", "onprem", "cheap", ("m",), (), 0.0, 1.0, "observed", 0.5,
                    "tier_default")
    assert (arm.prompt_tokens, arm.completion_tokens) == (None, None)
