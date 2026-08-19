"""Phase D: the UI's "Langfuse trace" link is a live deep-link, not a dead #.

export_trace returns the Langfuse UI URL for the trace it just emitted (built
from the root span's trace id), or None when telemetry is off. The route
attaches it to the response as `langfuse_url` so the inspector can link to it.
"""
import pytest

pytest.importorskip("opentelemetry.sdk.trace")
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

import app.obs.otel as otel
from app.obs.export import export_trace


def _trace(**over):
    kw = dict(request_id="req-x", tenant_id="acme", model="local-8b",
              cost={"total_usd": 0.0, "baseline_usd": 0.1},
              policy={"sensitivity": "public", "onprem_only": False,
                      "detected_entities": []},
              trace=[{"step": "router", "chosen": "local-8b", "reason": "tier cheap"}])
    kw.update(over)
    return export_trace(**kw).url


def test_export_trace_returns_deeplink_when_enabled(monkeypatch):
    mem = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(mem))
    monkeypatch.setattr(otel, "_provider", provider)
    monkeypatch.setattr(otel, "_trace_base",
                        "http://lf:3000/project/demo-project/traces/")

    url = _trace()
    assert url is not None
    assert url.startswith("http://lf:3000/project/demo-project/traces/")
    # 32-hex trace id suffix
    trace_id_hex = url.rsplit("/", 1)[-1]
    assert len(trace_id_hex) == 32 and int(trace_id_hex, 16) >= 0


def test_export_trace_returns_none_when_disabled(monkeypatch):
    # No provider configured (telemetry off) → no link, and no raise.
    monkeypatch.setattr(otel, "_provider", None)
    monkeypatch.setattr(otel, "_trace_base", None)
    assert _trace() is None


def test_trace_url_needs_both_provider_and_base(monkeypatch):
    monkeypatch.setattr(otel, "_provider", object())
    monkeypatch.setattr(otel, "_trace_base", None)
    assert otel.trace_url(123) is None
    monkeypatch.setattr(otel, "_trace_base", "http://lf/project/p/traces/")
    assert otel.trace_url(0x1a2b) == "http://lf/project/p/traces/" + f"{0x1a2b:032x}"
