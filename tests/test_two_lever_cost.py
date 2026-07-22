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


def test_routing_saved_goes_negative_under_escalation_identity_holds():
    """When a request escalates into a paid cloud tier and priced completion
    tokens push total above the all-frontier prompt baseline, routing_saved_usd
    must go NEGATIVE (not clamp to 0) so the two levers still sum to true
    savings. Re-adding a max(0,...) clamp would fabricate a $0 routing figure
    and hide that the request cost more than baseline — this test guards that.

    The default demo config disables cloud providers (everything routes to free
    on-prem), so this scenario needs bedrock enabled + a private_cloud_ok
    (confidential/email) request + low confidence to force a mid->frontier
    escalation onto bedrock-frontier.
    """
    import asyncio
    from app.compression.ccr import CCRStore
    from app.config.loader import load_settings
    from app.gateway.backends.mock import MockBackend
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
                    Gateway(s, MockBackend(s), Router(s)),
                    self_port=8080, cfg=co, ccr=CCRStore(3600))

    req = ChatRequest(messages=[Message(role="user", content="Email john.doe@example.com re: Q3")])
    object.__setattr__(req, "mock_confidence", 0.1)  # force escalation to frontier
    out = asyncio.run(pipe.run("rid", "acme", req))
    c = out["cost"]

    assert out["model"] == "bedrock-frontier"       # escalated onto the paid cloud tier
    assert c["total_usd"] > c["baseline_usd"]        # cost more than the all-frontier baseline
    assert c["routing_saved_usd"] < 0                # unclamped — the fix this test guards
    # additive identity must hold exactly even in the negative regime
    total_saved = c["baseline_usd"] - c["total_usd"]
    assert abs((c["compression_saved_usd"] + c["routing_saved_usd"]) - total_saved) < 1e-12
