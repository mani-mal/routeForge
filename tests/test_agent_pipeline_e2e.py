"""Task 5: task-mode + pipeline integration — a multi-step request posted to
the PUBLIC /v1/chat/completions route must run through the real NAT agent
runtime and come back with a multi-hop trace.

Uses `live_internal_server` (see tests/conftest.py), NOT a bare TestClient:
the agent runtime's NAT `tool_calling_agent` drives itself over real HTTP,
self-calling this app's own internal endpoint at 127.0.0.1:{port} — that
requires an actual listening uvicorn server, and the fixture sets
SP_SERVER_PORT to the ephemeral port it binds so app/main.py wires that exact
port into the Pipeline (see app/main.py's _self_port()).
"""
import httpx
import pytest

pytest.importorskip("nat")


def test_multistep_request_returns_multi_hop_trace(live_internal_server):
    r = httpx.post(
        f"http://127.0.0.1:{live_internal_server.port}/v1/chat/completions",
        headers={"authorization": "Bearer acme-key"},
        json={
            "model": "auto",
            "mock_multistep": True,
            "messages": [
                {"role": "user", "content": "Look up account 42 then compute 12 * 7"}
            ],
        },
        timeout=30.0,
    )
    assert r.status_code == 200, r.text
    body = r.json()

    hops = [t for t in body["trace"] if t.get("step") == "agent_hop"]
    assert len(hops) >= 2, "multi-step should route more than one hop"

    expected_total = sum(
        h.get("actual_cost_usd") or h.get("est_cost_usd", 0) for h in hops)
    assert body["cost"]["total_usd"] == pytest.approx(expected_total)

    # Baseline must be priced on the same per-hop, post-compression token
    # basis as `total_usd` above (summed over the same hops), not on the
    # final answer's token count — see task-5 finding. Since cheap on-prem
    # hops (local-8b, price 0 in mock) route far below the all-frontier
    # baseline model, the summed baseline is genuinely > summed total here,
    # so routing_saved_usd is real, not a trivial 0.0 == 0.0.
    assert body["cost"]["baseline_usd"] > 0
    assert body["cost"]["routing_saved_usd"] > 0

    assert body["choices"][0]["message"]["content"]
