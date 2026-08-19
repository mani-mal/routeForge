"""F1 Phase D task 2: the chat response carries the raw 32-hex Langfuse trace
id (not just the UI deep-link URL), so an external eval/RLHF consumer can link
feedback to a trace without parsing `langfuse_url`.
"""
import pytest
from fastapi.testclient import TestClient

pytest.importorskip("opentelemetry.sdk.trace")
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

import app.obs.otel as otel


def _chat(client: TestClient) -> dict:
    r = client.post("/v1/chat/completions",
                    headers={"authorization": "Bearer demo-key"},
                    json={"model": "auto",
                          "messages": [{"role": "user", "content": "hello"}]})
    assert r.status_code == 200
    return r.json()


def test_chat_response_includes_trace_id(client: TestClient, monkeypatch):
    mem = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(mem))
    monkeypatch.setattr(otel, "_provider", provider)
    monkeypatch.setattr(otel, "_trace_base",
                        "http://lf:3000/project/demo-project/traces/")

    resp = _chat(client)
    assert resp.get("trace_id") and len(resp["trace_id"]) == 32
    int(resp["trace_id"], 16)  # is hex


def test_trace_id_absent_when_telemetry_off(client: TestClient, monkeypatch):
    monkeypatch.setattr(otel, "_provider", None)
    monkeypatch.setattr(otel, "_trace_base", None)

    resp = _chat(client)
    assert resp.get("trace_id") in (None, "")
