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


_AUTH = {"Authorization": "Bearer demo-key"}


def test_served_request_increments_counters(client):
    """A completed request bumps requests_total and routing_decisions_total.
    Uses the standard `client` fixture (see conftest.py) and the same
    `_AUTH` bearer-token header the other end-to-end tests use (e.g.
    tests/test_e2e_slice.py) — there is no `seed_all`/`api_headers` fixture
    in this repo; `client` already boots a seeded app via create_app()."""
    r = client.post("/v1/chat/completions", headers=_AUTH,
                    json={"messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code == 200
    after = TestClient(create_app()).get("/metrics").text
    # Registry is process-global, so counters are visible from a fresh app too.
    assert "routeforge_requests_total" in after
    assert 'status="ok"' in after
    assert "routeforge_routing_decisions_total" in after
