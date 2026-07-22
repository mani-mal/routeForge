import pytest

otel = pytest.importorskip("opentelemetry.sdk.trace")
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from app.obs.otel import RedactingSpanExporter, REDACT_KEYS

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
