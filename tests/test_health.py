def test_health_ok(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def _loaded(client):
    """Onboard one model through the admin API so the registry is loaded."""
    from app.apps.probe import ProbeResult

    async def _ok(*a, **k):
        return ProbeResult(True, "ok")

    import app.api.admin_routes as ar
    orig = ar.probe_endpoint
    ar.probe_endpoint = _ok
    try:
        client.post("/admin/login", json={"email": "admin@routeforge.local",
                                          "password": "changeme"})
        r = client.post("/admin/infra/models", json={
            "slug": "h-model", "display_name": "H", "tier": "mid",
            "endpoint": "http://h/v1", "price_input": 0.1, "price_output": 0.1})
        assert r.status_code == 200
    finally:
        ar.probe_endpoint = orig


def test_health_lists_healthy_model_and_no_unavailable(client):
    _loaded(client)
    body = client.get("/health").json()
    assert "h-model" in body["models"]
    assert body["unavailable"] == []


def test_health_excludes_open_circuit_but_status_stays_ok(client):
    _loaded(client)
    breaker = client.app.state.breaker
    for _ in range(10):
        breaker.record_failure("h-model")
    body = client.get("/health").json()
    assert body["status"] == "ok"
    assert "h-model" not in body["models"]
    assert {"model": "h-model", "reason": "circuit open"} in body["unavailable"]


def test_health_excludes_model_with_unhealthy_probe(client):
    from dataclasses import replace
    _loaded(client)
    reg = client.app.state.registry
    reg._rows["h-model"] = replace(reg._rows["h-model"], probe_status="unauthorized")
    body = client.get("/health").json()
    assert body["status"] == "ok"
    assert "h-model" not in body["models"]
    assert {"model": "h-model", "reason": "probe: unauthorized"} in body["unavailable"]
    # an unknown/garbage value is also not healthy (fail closed)
    reg._rows["h-model"] = replace(reg._rows["h-model"], probe_status="weird")
    assert "h-model" not in client.get("/health").json()["models"]


def test_health_is_side_effect_free_on_breaker(client, monkeypatch, caplog):
    """A liveness poll must not flip the half-open dedup flag or emit the
    transition log/metric."""
    import logging
    from app.obs import metrics
    _loaded(client)
    breaker = client.app.state.breaker
    for _ in range(10):
        breaker.record_failure("h-model", now=0.0)
    # Cooldown elapsed (monotonic clock is far past the 0.0 open time).
    assert breaker.state("h-model").state == "half_open"
    before = dict(breaker._m)
    calls: list[str] = []
    monkeypatch.setattr(metrics, "observe_breaker_transition", calls.append)
    caplog.set_level(logging.INFO)
    caplog.clear()
    client.get("/health")
    client.get("/health")
    assert breaker._m == before
    assert calls == []
    assert not [r for r in caplog.records if "circuit transition" in r.getMessage()]
