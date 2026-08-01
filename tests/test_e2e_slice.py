_AUTH = {"Authorization": "Bearer demo-key"}


def test_simple_qa_uses_cheap_tier(client):
    r = client.post("/v1/chat/completions", headers=_AUTH, json={
        "messages": [{"role": "user", "content": "capital of France?"}]})
    body = r.json()
    assert r.status_code == 200
    router_hop = next(t for t in body["trace"] if t["step"] == "router")
    assert router_hop["chosen"] in {"local-8b"}
    assert body["cost"]["baseline_usd"] >= body["cost"]["total_usd"]


def test_pii_request_excludes_cloud_in_trace(client):
    r = client.post("/v1/chat/completions", headers=_AUTH, json={
        "messages": [{"role": "user", "content": "verify SSN 482-73-1946"}]})
    body = r.json()
    assert body["policy"]["onprem_only"] is True
    router_hop = next(t for t in body["trace"] if t["step"] == "router")
    assert any(rej["provider"] in {"azure", "bedrock"}
               and "residency" in rej["reason"] for rej in router_hop["rejected"])
    assert "482-73-1946" not in r.text


def test_cost_accumulates_and_baseline_present(client):
    r = client.post("/v1/chat/completions", headers=_AUTH, json={
        "messages": [{"role": "user", "content": "hello"}]})
    cost = r.json()["cost"]
    # On-prem is cheap-but-not-free (representative amortized cost), and far
    # below the all-frontier baseline — that gap is the routing savings story.
    assert cost["total_usd"] > 0.0
    assert cost["baseline_usd"] > cost["total_usd"]


def test_direct_path_quality_sum_counts_only_served_attempt_not_escalation():
    """Regression for the metric-correctness bug found in the whole-branch
    review: Gateway.run appends one step="router" DecisionRecord PER ATTEMPT,
    so a confidence escalation (config/routing.yaml max_escalations: 1) leaves
    TWO quality-bearing router records in `records` for a single served
    response. usage.total_tokens reflects only the FINAL served attempt, so
    summing quality across attempts (the old `sum(r.quality_score for r in
    qrecs)`) inflated Token Intelligence Density against a denominator that
    never grew. Only the served (last) attempt's quality should count.

    Uses `_DeterministicLiteLLM` + `mock_confidence` (same escalation-forcing
    seam as tests/test_gateway.py's test_low_confidence_escalates_exactly_once
    and tests/test_two_lever_cost.py) directly against the Pipeline, since the
    `client` fixture's real LiteLLMBackend fake (`_fake_acompletion`) doesn't
    honor `hint["mock_confidence"]` at all -- only `_DeterministicLiteLLM`
    does, so escalation must be forced at this layer, below the HTTP API.

    mock_confidence=0.3 is below confidence_escalation_threshold (0.60), so
    this forces exactly one cheap->mid escalation
    (config/routing.yaml tier_quality: cheap=0.5, mid=0.75). Pre-fix this
    would report quality.sum == 1.25 (0.5 + 0.75); post-fix it must be 0.75 —
    the mid tier's score alone, matching the tier that actually served the
    response.
    """
    import asyncio

    from app.compression.ccr import CCRStore
    from app.config.loader import load_settings
    from app.gateway.gateway import Gateway
    from app.models import ChatRequest, Message
    from app.pipeline import Pipeline
    from app.policy.detectors import make_detector
    from app.policy.gate import Gate
    from app.routing.router import Router
    from tests._litellm_fake import _DeterministicLiteLLM

    s = load_settings()
    det = make_detector("regex")
    co = s.serving.context_optimization
    pipe = Pipeline(s, Gate(s, det), det,
                    Gateway(s, _DeterministicLiteLLM(s), Router(s)),
                    self_port=8080, co_cfg_provider=lambda: co, ccr=CCRStore(3600))

    req = ChatRequest(messages=[Message(role="user", content="capital of France?")])
    object.__setattr__(req, "mock_confidence", 0.3)  # forces one cheap->mid escalation
    out = asyncio.run(pipe.run("req-1", "acme", req))

    router_hops = [t for t in out["trace"] if t["step"] == "router"]
    assert len(router_hops) == 2  # initial cheap attempt + one escalation to mid
    served = router_hops[-1]
    assert served["chosen"].startswith("local")  # sanity: on-prem mid tier served it
    quality = out["quality"]
    assert quality["chosen"] == served["quality_score"]
    # The bug: naive sum across attempts (0.5 + 0.75 == 1.25) double-counts a
    # retried, never-delivered attempt. The fix: only the served attempt's
    # quality (0.75) counts, matching usage.total_tokens (final attempt only).
    assert quality["sum"] == served["quality_score"]
    assert quality["sum"] != sum(h["quality_score"] for h in router_hops)
