"""Feature 1: Prometheus /metrics. prometheus_client ships in the
`observability` extra (installed in CI/test); AVAILABLE is True here."""
from fastapi.testclient import TestClient
from prometheus_client import REGISTRY

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


def _snapshot(metric_name: str) -> dict[tuple, float]:
    """Label-combo -> value for one metric name, read straight from the
    process-global REGISTRY. Exact `sample.name` match excludes the
    Counter's paired `_created` gauge sample. Used for BEFORE/AFTER deltas
    rather than substring checks against /metrics text — the registry is
    process-global and shared across this whole test file (see
    test_metrics_endpoint_renders_all_families / _no_pii_or_tenant_labels
    above, which already call observe_request/observe_routing_decision), so
    a substring-presence assertion here would pass even if routes.py/
    gateway.py never called the instrumentation at all."""
    out: dict[tuple, float] = {}
    for family in REGISTRY.collect():
        for sample in family.samples:
            if sample.name == metric_name:
                out[tuple(sorted(sample.labels.items()))] = sample.value
    return out


def test_served_request_increments_counters(client):
    """A completed request bumps requests_total (for the served model,
    status=ok) and routing_decisions_total by a real BEFORE/AFTER delta.
    Uses the standard `client` fixture (see conftest.py) and the same
    `_AUTH` bearer-token header the other end-to-end tests use (e.g.
    tests/test_e2e_slice.py) — there is no `seed_all`/`api_headers` fixture
    in this repo; `client` already boots a seeded app via create_app()."""
    before_req = _snapshot("routeforge_requests_total")
    before_dec_total = sum(_snapshot("routeforge_routing_decisions_total").values())

    r = client.post("/v1/chat/completions", headers=_AUTH,
                    json={"messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code == 200
    model = r.json()["model"]  # don't hard-code a slug; assert on what actually served

    after_req = _snapshot("routeforge_requests_total")
    key = tuple(sorted({"model": model, "status": "ok"}.items()))
    assert after_req.get(key, 0.0) == before_req.get(key, 0.0) + 1

    after_dec_total = sum(_snapshot("routeforge_routing_decisions_total").values())
    assert after_dec_total > before_dec_total
