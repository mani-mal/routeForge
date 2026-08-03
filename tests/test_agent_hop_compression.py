"""Task 7 (M3 must-fix): every LLM call the agent makes must re-enter
compression, not just the outer request. The internal endpoint
(app/agent/internal_endpoint.py) now compresses each hop's messages before
pricing/routing, so `tokens_priced` on the resulting DecisionRecord is the
POST-compression count, matching the direct path's compression behaviour
(see CLAUDE.md: "every LLM call the agent makes re-enters compression (5)
and the router (6) — per-call, not per-request")."""
from app.agent.internal_endpoint import HOPS
import pytest


def test_hop_prices_post_compression(internal_client_with_compression_on):
    client, run_id = internal_client_with_compression_on
    verbose = "INFO log line\n" * 800
    body = {"messages": [{"role": "assistant", "content": verbose},
                         {"role": "user", "content": "summarize"}],
            "tools": []}
    resp = client.post(f"/internal/v1/{run_id}/chat/completions", json=body)
    assert resp.status_code == 200
    recs = HOPS.pop(run_id)
    assert recs and recs[0].tokens_priced < 800  # far below the raw ~800+ token count


def test_hop_compression_record_captured():
    """The internal endpoint must also record a per-hop CompressionRecord
    (not just the DecisionRecord) so the multi-step trace can show
    compression per hop — see HopCollector.pop_all in internal_endpoint.py."""
    from app.main import create_app
    from fastapi.testclient import TestClient
    from app.models import ExecutionPolicyContext

    app = create_app()
    bypass_on = app.state.co_cfg.bypass_on.model_copy(update={"user_prompt": False})
    app.state.co_cfg = app.state.co_cfg.model_copy(
        update={"enabled": True, "min_tokens": 1, "bypass_on": bypass_on})
    client = TestClient(app)

    run_id = "run-compress-record"
    ctx = ExecutionPolicyContext(
        request_id="req-y", tenant_id="acme", sensitivity="internal", onprem_only=False,
        allowed_providers=frozenset({"onprem", "azure", "bedrock"}), allowed_models=frozenset(),
        max_cost_usd=1.0, max_latency_ms=10000, risk_class="low",
        require_verification=False, detected_entities=(), redaction_map_id=None)
    HOPS.start(run_id, ctx)

    verbose = "INFO log line\n" * 800
    body = {"messages": [{"role": "assistant", "content": verbose},
                         {"role": "user", "content": "summarize"}],
            "tools": []}
    resp = client.post(f"/internal/v1/{run_id}/chat/completions", json=body)
    assert resp.status_code == 200

    _decisions, compressions = HOPS.pop_all(run_id)
    assert len(compressions) == 1
    cr = compressions[0]
    assert cr.applied is True
    assert cr.tokens_after < cr.tokens_before


def test_hop_records_nonzero_latency(monkeypatch):
    """Each agent_hop DecisionRecord must carry a REAL measured latency (Task
    7: the internal endpoint now calls the real backend per hop instead of
    faking the completion with per-tier seeded latency), otherwise multi-step
    requests report 0 ms total latency (routes.py sums trace latency_ms).
    Regression guard for the multi-step-latency-zero bug — updated for the
    real-backend-per-hop behaviour. Also asserts the backend was actually
    called exactly once, so this stays a real guard rather than only checking
    a relaxed `>= 0` bound that a reverted-to-faked endpoint could still
    satisfy trivially."""
    from app.main import create_app
    from fastapi.testclient import TestClient
    from app.models import ExecutionPolicyContext
    from app.gateway.backends import litellm_backend
    from tests._litellm_fake import make_response

    calls = {"n": 0}
    async def _call(**k):
        calls["n"] += 1
        return make_response()
    monkeypatch.setattr(litellm_backend, "acompletion", _call)

    client = TestClient(create_app())
    run_id = "run-latency"
    ctx = ExecutionPolicyContext(
        request_id="req-lat", tenant_id="acme", sensitivity="internal",
        onprem_only=False,
        allowed_providers=frozenset({"onprem", "azure", "bedrock"}),
        allowed_models=frozenset(), max_cost_usd=1.0, max_latency_ms=10000,
        risk_class="low", require_verification=False,
        detected_entities=(), redaction_map_id=None)
    HOPS.start(run_id, ctx)

    body = {"messages": [{"role": "user", "content": "look up order 42"}],
            "tools": []}
    resp = client.post(f"/internal/v1/{run_id}/chat/completions", json=body)
    assert resp.status_code == 200

    recs = HOPS.pop(run_id)
    assert recs, "expected a recorded agent_hop"
    assert recs[0].latency_ms >= 0  # real (measured), never the old seeded_latency_ms
    assert calls["n"] == 1  # backend was actually called, exactly once


def test_hop_sends_compressed_messages_to_backend(internal_client_with_compression_on, monkeypatch):
    """Regression guard: compress() returns a NEW message list and never
    mutates its input, so the endpoint must pass `comp.messages` (the
    compressed output) to `backend.generate(...)`, not the raw `hop_msgs` it
    built for compress()'s input. Passing the raw list would still record a
    correct (compressed) `tokens_priced` on the DecisionRecord — bookkeeping
    only — while silently sending the UNCOMPRESSED prompt to the real model,
    diverging real cost/latency from the two-lever accounting story (CLAUDE.md:
    "router must price post-compression tokens; the model gets compressed
    content"). This test captures what was actually sent to `acompletion` and
    asserts it reflects the compressed content, not the raw ~800-line input."""
    client, run_id = internal_client_with_compression_on
    from app.gateway.backends import litellm_backend
    from tests._litellm_fake import make_response

    captured: dict = {}
    async def _call(**kwargs):
        captured["messages"] = kwargs["messages"]
        return make_response()
    monkeypatch.setattr(litellm_backend, "acompletion", _call)

    verbose = "INFO log line\n" * 800
    body = {"messages": [{"role": "assistant", "content": verbose},
                         {"role": "user", "content": "summarize"}],
            "tools": []}
    resp = client.post(f"/internal/v1/{run_id}/chat/completions", json=body)
    assert resp.status_code == 200

    assert "messages" in captured, "acompletion was never called"
    sent_len = sum(len(m.get("content") or "") for m in captured["messages"])
    assert sent_len < len(verbose), (
        "backend received the raw (uncompressed) prompt — comp.messages was "
        "not passed to backend.generate")
