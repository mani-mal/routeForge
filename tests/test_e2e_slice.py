def test_simple_qa_uses_cheap_tier(client):
    r = client.post("/v1/chat/completions", json={
        "messages": [{"role": "user", "content": "capital of France?"}]})
    body = r.json()
    assert r.status_code == 200
    router_hop = next(t for t in body["trace"] if t["step"] == "router")
    assert router_hop["chosen"] in {"local-8b"}
    assert body["cost"]["baseline_usd"] >= body["cost"]["total_usd"]


def test_pii_request_excludes_cloud_in_trace(client):
    r = client.post("/v1/chat/completions", json={
        "messages": [{"role": "user", "content": "verify SSN 482-73-1946"}]})
    body = r.json()
    assert body["policy"]["onprem_only"] is True
    router_hop = next(t for t in body["trace"] if t["step"] == "router")
    assert any(rej["provider"] in {"azure", "bedrock"}
               and "residency" in rej["reason"] for rej in router_hop["rejected"])
    assert "482-73-1946" not in r.text


def test_cost_accumulates_and_baseline_present(client):
    r = client.post("/v1/chat/completions", json={
        "messages": [{"role": "user", "content": "hello"}]})
    cost = r.json()["cost"]
    # On-prem is cheap-but-not-free (representative amortized cost), and far
    # below the all-frontier baseline — that gap is the routing savings story.
    assert cost["total_usd"] > 0.0
    assert cost["baseline_usd"] > cost["total_usd"]
