"""Feature 1: Prometheus /metrics. prometheus_client ships in the
`observability` extra (installed in CI/test); AVAILABLE is True here."""
from fastapi.testclient import TestClient

from app.main import create_app
from app.obs import metrics


def test_metrics_module_available():
    assert metrics.AVAILABLE is True


def test_metrics_endpoint_renders_all_families():
    metrics.observe_request("local-8b", "ok")
    metrics.observe_tokens("local-8b", 10, 5)
    metrics.observe_routing_decision("cheap", escalated=False)
    with metrics.time_routing():
        pass
    client = TestClient(create_app())
    r = client.get("/metrics")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/plain")
    body = r.text
    for family in ("routeforge_requests_total",
                   "routeforge_routing_decisions_total",
                   "routeforge_routing_overhead_seconds",
                   "routeforge_tokens_total"):
        assert family in body


def test_metrics_carry_no_pii_or_tenant_labels():
    metrics.observe_request("local-8b", "ok")
    body = TestClient(create_app()).get("/metrics").text
    # Label keys must be bounded-cardinality only. Tenant/PII must never appear.
    for forbidden in ('tenant', 'application_id', 'request_id', 'prompt'):
        assert f'{forbidden}=' not in body
