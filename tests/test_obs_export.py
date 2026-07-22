import pytest

pytest.importorskip("opentelemetry.sdk.trace")
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
import app.obs.otel as otel
import app.obs.export as export_mod

@pytest.fixture
def mem_provider(monkeypatch):
    mem = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(otel.RedactingSpanExporter(mem)))
    monkeypatch.setattr(otel, "_provider", provider)
    return mem

def _sample():
    return dict(
        request_id="req-1", tenant_id="hospital", model="local-32b",
        cost={"total_usd": 0.0004, "baseline_usd": 0.012, "routing_saved_usd": 0.0116},
        policy={"sensitivity": "restricted", "onprem_only": True,
                "allowed_providers": ["onprem"], "detected_entities": ["US_SSN"]},
        trace=[{"step": "route", "chosen": "local-32b",
                "reason": "leaked SSN 123-45-6789 in reason text", "hop_index": 0,
                "actual_cost_usd": 0.0004, "latency_ms": 12.0}],
    )

def test_export_emits_spans_without_raw_pii(mem_provider):
    export_mod.export_trace(**_sample())
    otel._provider.force_flush()
    spans = mem_provider.get_finished_spans()
    assert len(spans) >= 2  # root + one record
    blob = "".join(str(dict(s.attributes)) + s.name for s in spans)
    assert "123-45-6789" not in blob
    assert any(s.name == "serving.request" for s in spans)

def test_export_noop_and_safe_when_disabled(monkeypatch):
    monkeypatch.setattr(otel, "_provider", None)
    # must not raise even though nothing is configured
    export_mod.export_trace(**_sample())

def test_export_swallows_exporter_errors(mem_provider, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("otlp down")
    monkeypatch.setattr(otel, "get_tracer", boom)
    export_mod.export_trace(**_sample())  # logged + swallowed, no raise
