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
