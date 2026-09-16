"""Contract: a request's Langfuse trace shows how long it took and which
model generated the answer with how many tokens, not zero-length spans."""
import json
import time

import pytest

pytest.importorskip("opentelemetry.sdk")

from app.obs.export import export_trace
from tests.test_capture_io import _wire

TRACE = [
    {"step": "compression", "latency_ms": 5.0},
    {"step": "router", "chosen": "local-8b", "chosen_tier": "cheap",
     "chosen_provider": "onprem", "reason": "tier cheap", "rejected": [],
     "est_cost_usd": 0.001, "actual_cost_usd": 0.0009, "latency_ms": 1200.0, "hop_index": 0},
    {"step": "verify", "latency_ms": 2.0, "reason": "passed"},
]


def _spans(mem):
    return {s.name: s for s in mem.get_finished_spans()}


def test_spans_have_real_durations(monkeypatch):
    mem, provider = _wire(monkeypatch, redacting=True)
    started = time.time_ns() - 2_000_000_000   # request began 2 s ago
    export_trace(request_id="req-t", tenant_id="acme", model="local-8b",
                 cost={"total_usd": 0.0009, "baseline_usd": 0.01},
                 policy={"sensitivity": "public"}, trace=TRACE,
                 started_ns=started,
                 usage={"prompt_tokens": 900, "completion_tokens": 120, "total_tokens": 1020})
    provider.force_flush()
    spans = _spans(mem)
    root = spans["serving.request"]
    assert root.start_time == started
    assert (root.end_time - root.start_time) >= 1_900_000_000
    router = spans["serving.router"]
    assert (router.end_time - router.start_time) == 1_200_000_000
    assert root.start_time <= router.start_time and router.end_time <= root.end_time


def test_router_hop_is_a_generation_with_model_tokens_and_cost(monkeypatch):
    mem, provider = _wire(monkeypatch, redacting=True)
    export_trace(request_id="req-g", tenant_id="acme", model="local-8b",
                 cost={"total_usd": 0.0009, "baseline_usd": 0.01},
                 policy={"sensitivity": "public"}, trace=TRACE,
                 started_ns=time.time_ns() - 1_500_000_000,
                 usage={"prompt_tokens": 900, "completion_tokens": 120, "total_tokens": 1020})
    provider.force_flush()
    a = dict(_spans(mem)["serving.router"].attributes)
    assert a["langfuse.observation.type"] == "generation"
    assert a["langfuse.observation.model.name"] == "local-8b"
    assert a["gen_ai.request.model"] == "local-8b"
    assert a["gen_ai.usage.input_tokens"] == 900
    assert a["gen_ai.usage.output_tokens"] == 120
    assert json.loads(a["langfuse.observation.usage_details"]) == {"input": 900, "output": 120}
    assert json.loads(a["langfuse.observation.cost_details"]) == {"total": 0.0009}
    other = dict(_spans(mem)["serving.verify"].attributes)
    assert "langfuse.observation.type" not in other


def test_no_started_ns_keeps_old_behaviour(monkeypatch):
    mem, provider = _wire(monkeypatch, redacting=True)
    export_trace(request_id="req-o", tenant_id="acme", model="local-8b",
                 cost={}, policy={}, trace=TRACE)
    provider.force_flush()
    assert "serving.request" in _spans(mem)


def test_http_request_trace_is_timed(monkeypatch, client):
    mem, provider = _wire(monkeypatch, redacting=True)
    r = client.post("/v1/chat/completions", headers={"Authorization": "Bearer demo-key"},
                    json={"messages": [{"role": "user", "content": "hello there"}]})
    assert r.status_code == 200
    provider.force_flush()
    spans = _spans(mem)
    root = spans["serving.request"]
    assert root.end_time > root.start_time
    assert spans["serving.router"].attributes.get("langfuse.observation.type") == "generation"
