import pytest

otel = pytest.importorskip("opentelemetry.sdk.trace")
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
import app.obs.otel as otel_mod
from app.obs.otel import RedactingSpanExporter, REDACT_KEYS, get_tracer


def test_get_tracer_no_ops_when_provider_is_none(monkeypatch):
    """Regression guard: get_tracer() must never touch `opentelemetry` when
    _provider is None (extra absent, obs disabled, or keys unset). It must
    return the LOCAL no-op tracer, not something imported from opentelemetry."""
    monkeypatch.setattr(otel_mod, "_provider", None)
    tracer = get_tracer()
    assert isinstance(tracer, otel_mod._NoOpTracer)
    with tracer.start_as_current_span("x") as span:
        span.set_attribute("k", "v")


def test_redacting_exporter_strips_pii_from_attributes():
    mem = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(RedactingSpanExporter(mem)))
    tracer = provider.get_tracer("t")
    with tracer.start_as_current_span("root") as span:
        span.set_attribute(REDACT_KEYS[0], "patient SSN 123-45-6789")
    provider.force_flush()
    spans = mem.get_finished_spans()
    assert len(spans) == 1
    dumped = str(dict(spans[0].attributes))
    assert "123-45-6789" not in dumped
