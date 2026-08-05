"""Task 6: pipeline two-lever cost accounting (direct path). With Headroom
compression enabled, the cost dict must split savings into two independent
levers — compression_saved_usd (baseline_usd priced on tokens_before vs.
tokens_after) and routing_saved_usd (baseline_after vs. actual total) — per
CLAUDE.md's "don't attribute all savings to routing" rule.
"""
from app.models import ChatRequest, Message


async def _run(pipeline, content):
    return await pipeline.run("req-1", "acme",
                              ChatRequest(messages=[Message(role="user", content=content)]))


def test_cost_dict_has_two_levers(compression_on_pipeline):
    import asyncio
    out = asyncio.run(_run(compression_on_pipeline, "INFO log line\n" * 800))
    cost = out["cost"]
    assert "compression_saved_usd" in cost and "routing_saved_usd" in cost
    total_saved = cost["baseline_usd"] - cost["total_usd"]
    assert abs((cost["compression_saved_usd"] + cost["routing_saved_usd"]) - total_saved) < 1e-9
    assert cost["compression_saved_usd"] > 0  # compression actually reduced tokens


def test_escalation_to_baseline_model_yields_zero_routing_saved_identity_holds():
    """After B12 the all-frontier baseline is priced on the SAME input+output
    tokens as `total`. Escalating to bedrock-frontier — which IS baseline_model —
    therefore yields routing_saved == 0 (not the old bug's negative value), and
    the additive identity still holds exactly. routing_saved is never negative
    here because baseline_model is the most expensive frontier tier."""
    import asyncio
    from app.compression.ccr import CCRStore
    from app.config.loader import load_settings
    from tests._litellm_fake import _DeterministicLiteLLM
    from app.gateway.gateway import Gateway
    from app.pipeline import Pipeline
    from app.policy.detectors import make_detector
    from app.policy.gate import Gate
    from app.routing.router import Router
    from app.models import ChatRequest, Message
    s = load_settings()
    prov = dict(s.serving.providers)
    prov["bedrock"] = prov["bedrock"].model_copy(update={"enabled": True})
    s = s.model_copy(update={"serving": s.serving.model_copy(update={"providers": prov})})
    det = make_detector("regex")
    co = s.serving.context_optimization
    pipe = Pipeline(s, Gate(s, det), det,
                    Gateway(s, _DeterministicLiteLLM(s), Router(s)),
                    self_port=8080, co_cfg_provider=lambda: co, ccr=CCRStore(3600))
    req = ChatRequest(messages=[Message(role="user", content="Email john.doe@example.com re: Q3")])
    object.__setattr__(req, "mock_confidence", 0.1)  # force escalation to frontier
    out = asyncio.run(pipe.run("rid", "acme", req))
    c = out["cost"]
    assert out["model"] == "bedrock-frontier"          # escalated onto the frontier tier
    assert abs(c["routing_saved_usd"]) < 1e-9          # chosen == baseline model => 0, not negative
    total_saved = c["baseline_usd"] - c["total_usd"]
    assert abs((c["compression_saved_usd"] + c["routing_saved_usd"]) - total_saved) < 1e-12


def test_cheaper_tier_request_has_positive_routing_saved():
    """A normal request routes to a cheap on-prem model; priced against the
    all-frontier (bedrock-frontier) baseline on the SAME in+out tokens, routing
    must SAVE money (positive), and the identity holds. This is the B12 repro
    (was negative before the fix)."""
    import asyncio
    from app.compression.ccr import CCRStore
    from app.config.loader import load_settings
    from tests._litellm_fake import _DeterministicLiteLLM, make_test_registry
    from app.gateway.gateway import Gateway
    from app.pipeline import Pipeline
    from app.policy.detectors import make_detector
    from app.policy.gate import Gate
    from app.routing.router import Router
    from app.models import ChatRequest, Message
    s = load_settings()
    det = make_detector("regex")
    co = s.serving.context_optimization
    # On-prem models are onboarded-only (not in the static YAML model_pool),
    # so a "routes to cheap on-prem" scenario needs the test registry — same
    # pattern as tests/test_e2e_slice.py — or the pool has no cheap candidate
    # at all (both cloud tiers disabled by default) and routing raises before
    # B12 is ever exercised.
    registry = make_test_registry(s)
    pipe = Pipeline(s, Gate(s, det, registry=registry), det,
                    Gateway(s, _DeterministicLiteLLM(s), Router(s, registry=registry), registry),
                    self_port=8080, co_cfg_provider=lambda: co, ccr=CCRStore(3600),
                    registry=registry)
    req = ChatRequest(messages=[Message(role="user", content="What is the capital of France?")])
    out = asyncio.run(pipe.run("rid", "acme", req))
    c = out["cost"]
    assert c["total_usd"] < c["baseline_usd"]          # cheaper than all-frontier
    assert c["routing_saved_usd"] > 0                  # B12: positive, was negative
    total_saved = c["baseline_usd"] - c["total_usd"]
    assert abs((c["compression_saved_usd"] + c["routing_saved_usd"]) - total_saved) < 1e-12
