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

# Multi-step: each router hop carries its OWN tokens (tokens_priced in,
# tokens_out out) on the DecisionRecord, the way the agent runtime's per-call
# router re-entries do.
MULTI_HOP_TRACE = [
    {"step": "router", "chosen": "local-8b", "chosen_tier": "cheap",
     "chosen_provider": "onprem", "reason": "hop1", "rejected": [],
     "actual_cost_usd": 0.0003, "latency_ms": 400.0, "hop_index": 0,
     "tokens_priced": 500, "tokens_out": 50},
    {"step": "router", "chosen": "local-32b", "chosen_tier": "mid",
     "chosen_provider": "onprem", "reason": "hop2", "rejected": [],
     "actual_cost_usd": 0.0006, "latency_ms": 800.0, "hop_index": 1,
     "tokens_priced": 400, "tokens_out": 70},
]

# Escalation: two router hops, neither carries its own tokens_priced/
# tokens_out -- only the request-wide `usage` aggregate is available, and it
# describes the FINAL (served) hop only.
ESCALATION_TRACE = [
    {"step": "router", "chosen": "local-8b", "chosen_tier": "cheap",
     "chosen_provider": "onprem", "reason": "tried cheap, escalating", "rejected": [],
     "actual_cost_usd": 0.0001, "latency_ms": 100.0, "hop_index": 0},
    {"step": "router", "chosen": "local-32b", "chosen_tier": "mid",
     "chosen_provider": "onprem", "reason": "escalated", "rejected": [],
     "actual_cost_usd": 0.0009, "latency_ms": 300.0, "hop_index": 1},
]


def _spans(mem):
    return {s.name: s for s in mem.get_finished_spans()}


def _spans_named(mem, name):
    """Every finished span with this name, sorted by sp.hop_index -- unlike
    `_spans`, safe when a trace has more than one span of the same name (e.g.
    two `serving.router` hops on a multi-step/escalation trace)."""
    matches = [s for s in mem.get_finished_spans() if s.name == name]
    return sorted(matches, key=lambda s: s.attributes.get("sp.hop_index", 0))


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


def test_hop_span_still_ends_when_attribute_setting_raises(monkeypatch):
    """Fix round 1, issue 1: a malformed record value (e.g. a non-numeric
    actual_cost_usd hitting float()) must not orphan the hop span. With
    end_on_exit=False, the SDK only ends a span whose body completes
    normally -- span.end() must run in a `finally` so the span is still
    exported (and export_trace's own outer guard still swallows the
    resulting exception, same as before)."""
    mem, provider = _wire(monkeypatch, redacting=True)
    bad_trace = [
        {"step": "router", "chosen": "local-8b", "chosen_tier": "cheap",
         "chosen_provider": "onprem", "reason": "tier cheap", "rejected": [],
         "actual_cost_usd": "not-a-number", "latency_ms": 5.0, "hop_index": 0},
    ]
    # Must not raise -- export_trace's outer try/except still swallows the
    # ValueError from float("not-a-number") and returns TraceExport(None, None).
    te = export_trace(request_id="req-bad", tenant_id="acme", model="local-8b",
                      cost={"total_usd": 0.0, "baseline_usd": 0.0},
                      policy={"sensitivity": "public"}, trace=bad_trace,
                      started_ns=time.time_ns() - 1_000_000_000)
    assert te.url is None and te.trace_id is None
    provider.force_flush()
    finished = mem.get_finished_spans()
    names = [s.name for s in finished]
    # Every span that was started (root + the one hop) is present in the
    # exporter's FINISHED list -- none left open/orphaned by the exception.
    assert names.count("serving.request") == 1
    assert names.count("serving.router") == 1


def test_multistep_hops_each_carry_own_tokens(monkeypatch):
    """Fix round 1, issue 2: on multi-step, each router hop's own
    tokens_priced/tokens_out are used -- the request-wide `usage` aggregate
    must not be stamped onto any hop when the hop already carries its own."""
    mem, provider = _wire(monkeypatch, redacting=True)
    export_trace(request_id="req-m", tenant_id="acme", model="local-32b",
                 cost={"total_usd": 0.0009, "baseline_usd": 0.01},
                 policy={"sensitivity": "public"}, trace=MULTI_HOP_TRACE,
                 started_ns=time.time_ns() - 1_500_000_000,
                 # Deliberately a very different value from any hop's own
                 # tokens, so a leaked aggregate is unmistakable.
                 usage={"prompt_tokens": 999999, "completion_tokens": 999999,
                        "total_tokens": 1999998})
    provider.force_flush()
    hops = _spans_named(mem, "serving.router")
    assert len(hops) == 2
    a0, a1 = (dict(h.attributes) for h in hops)
    assert a0["langfuse.observation.type"] == "generation"
    assert a1["langfuse.observation.type"] == "generation"
    assert a0["gen_ai.usage.input_tokens"] == 500
    assert a0["gen_ai.usage.output_tokens"] == 50
    assert a1["gen_ai.usage.input_tokens"] == 400
    assert a1["gen_ai.usage.output_tokens"] == 70
    assert json.loads(a0["langfuse.observation.usage_details"]) == {"input": 500, "output": 50}
    assert json.loads(a1["langfuse.observation.usage_details"]) == {"input": 400, "output": 70}
    assert json.loads(a0["langfuse.observation.cost_details"]) == {"total": 0.0003}
    assert json.loads(a1["langfuse.observation.cost_details"]) == {"total": 0.0006}


def test_escalation_only_last_hop_carries_request_usage(monkeypatch):
    """Fix round 1, issue 2: on an escalation (two router hops, neither
    carrying its own tokens_priced/tokens_out), both hops are generations
    with their own cost, but ONLY the last (served) hop gets the request-wide
    usage -- the earlier, escalated-past hop gets no usage attribute."""
    mem, provider = _wire(monkeypatch, redacting=True)
    export_trace(request_id="req-e", tenant_id="acme", model="local-32b",
                 cost={"total_usd": 0.0009, "baseline_usd": 0.01},
                 policy={"sensitivity": "public"}, trace=ESCALATION_TRACE,
                 started_ns=time.time_ns() - 1_000_000_000,
                 usage={"prompt_tokens": 900, "completion_tokens": 120, "total_tokens": 1020})
    provider.force_flush()
    hops = _spans_named(mem, "serving.router")
    assert len(hops) == 2
    first, last = (dict(h.attributes) for h in hops)
    assert first["langfuse.observation.type"] == "generation"
    assert last["langfuse.observation.type"] == "generation"
    assert "gen_ai.usage.input_tokens" not in first
    assert "langfuse.observation.usage_details" not in first
    assert json.loads(last["langfuse.observation.usage_details"]) == {"input": 900, "output": 120}
    assert last["gen_ai.usage.input_tokens"] == 900
    assert last["gen_ai.usage.output_tokens"] == 120
    assert json.loads(first["langfuse.observation.cost_details"]) == {"total": 0.0001}
    assert json.loads(last["langfuse.observation.cost_details"]) == {"total": 0.0009}
