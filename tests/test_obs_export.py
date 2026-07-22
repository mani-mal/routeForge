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

def test_export_defense_in_depth_exporter_redacts(mem_provider):
    """RedactingSpanExporter (2nd line of defense) also scrubs sp.reason on export.

    This does NOT prove export_trace's own redact_value call works -- the
    exporter wrapper would redact even if export.py's redaction were deleted.
    See test_export_own_redaction_removes_pii for the isolated guarantee.
    """
    export_mod.export_trace(**_sample())
    otel._provider.force_flush()
    spans = mem_provider.get_finished_spans()
    assert len(spans) >= 2  # root + one record
    blob = "".join(str(dict(s.attributes)) + s.name for s in spans)
    assert "123-45-6789" not in blob
    assert any(s.name == "serving.request" for s in spans)

def test_export_own_redaction_removes_pii(monkeypatch):
    """Isolates export_trace's OWN redact_value call (app/obs/export.py line 34).

    Wires a BARE InMemorySpanExporter with NO RedactingSpanExporter wrapper, so
    nothing else in the pipeline can scrub PII. If export_trace's redact_value
    call were removed, the planted SSN would leak straight into the captured
    span attributes and this test would fail.
    """
    mem = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(mem))  # no RedactingSpanExporter
    monkeypatch.setattr(otel, "_provider", provider)

    export_mod.export_trace(**_sample())
    provider.force_flush()

    spans = mem.get_finished_spans()
    assert len(spans) >= 2  # root + one record
    blob = "".join(str(dict(s.attributes)) + s.name for s in spans)
    assert "123-45-6789" not in blob
    reason_span = next(s for s in spans if "sp.reason" in s.attributes)
    assert "123-45-6789" not in reason_span.attributes["sp.reason"]

def test_export_noop_and_safe_when_disabled(monkeypatch):
    monkeypatch.setattr(otel, "_provider", None)
    # must not raise even though nothing is configured
    export_mod.export_trace(**_sample())

def test_export_swallows_exporter_errors(mem_provider, monkeypatch, caplog):
    def boom(*a, **k):
        raise RuntimeError("otlp down")
    # export.py does `from app.obs.otel import get_tracer`, so the call site
    # inside export_trace binds to export_mod.get_tracer, not otel.get_tracer.
    # Patching otel.get_tracer would be a no-op; patch the name actually used.
    monkeypatch.setattr(export_mod, "get_tracer", boom)
    with caplog.at_level("ERROR", logger="serving.obs"):
        export_mod.export_trace(**_sample())  # logged + swallowed, no raise
    assert any("trace export failed" in r.message for r in caplog.records)
