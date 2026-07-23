import pytest
from fastapi.testclient import TestClient

pytest.importorskip("opentelemetry.sdk.trace")
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
import app.obs.otel as otel

@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("SP_PII_DETECTION", "regex")
    from app.main import create_app
    app = create_app()
    return TestClient(app)

def test_pii_request_exports_redacted_trace(client, monkeypatch):
    mem = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(otel.RedactingSpanExporter(mem)))
    monkeypatch.setattr(otel, "_provider", provider)
    r = client.post("/v1/chat/completions",
                    headers={"authorization": "Bearer hospital-key"},
                    json={"model": "auto",
                          "messages": [{"role": "user",
                                        "content": "my SSN is 482-73-1946"}]})
    assert r.status_code == 200
    provider.force_flush()
    blob = "".join(str(dict(s.attributes)) + s.name
                   for s in mem.get_finished_spans())
    assert "482-73-1946" not in blob
    assert "serving.request" in blob

def test_request_succeeds_even_if_export_raises(client, monkeypatch):
    import app.api.routes as routes
    monkeypatch.setattr(routes, "export_trace",
                        lambda **k: (_ for _ in ()).throw(RuntimeError("boom")))
    r = client.post("/v1/chat/completions",
                    headers={"authorization": "Bearer demo-key"},
                    json={"model": "auto",
                          "messages": [{"role": "user", "content": "hello"}]})
    assert r.status_code == 200
