"""Admin API: probe-gated on-prem model onboarding (POST/PUT/DELETE/GET
/admin/infra/models). See Task 5 of the model-registry-onboarding plan.

Probe outcome is what gates `enabled` — a model that fails its reachability
probe is persisted but never routable (registry.has_healthy_onprem() keys on
`enabled`). Static validation (tier/endpoint/price/duplicate/unknown app id)
runs before the probe so a bad request never touches the network.
"""

_ADMIN = {"email": "admin@routeforge.local", "password": "changeme"}


def _login(client):
    r = client.post("/admin/login", json=_ADMIN)
    assert r.status_code == 200
    return r


async def _ok(*a, **k):
    return True, "ok"


async def _fail(*a, **k):
    return False, "boom"


_BODY = {
    "slug": "nemotron-mid",
    "display_name": "Nemotron",
    "tier": "mid",
    "endpoint": "http://nem/v1",
    "price_input": 0.2,
    "price_output": 0.2,
}


def test_onboard_probe_pass_enables(client, monkeypatch):
    monkeypatch.setattr("app.api.admin_routes.probe_endpoint", _ok)
    _login(client)
    r = client.post("/admin/infra/models", json=_BODY)
    assert r.status_code == 200
    body = r.json()
    assert body["enabled"] is True
    assert body["probe_status"] == "healthy"
    assert body["probe_error"] is None

    lst = client.get("/admin/infra/models").json()
    assert any(m["slug"] == "nemotron-mid" and m["enabled"] for m in lst)


def test_onboard_probe_fail_disables(client, monkeypatch):
    monkeypatch.setattr("app.api.admin_routes.probe_endpoint", _fail)
    _login(client)
    r = client.post("/admin/infra/models", json={**_BODY, "slug": "unreachable-model"})
    assert r.status_code == 200
    body = r.json()
    assert body["enabled"] is False
    assert body["probe_status"] == "unreachable"
    assert body["probe_error"] == "boom"

    lst = client.get("/admin/infra/models").json()
    row = next(m for m in lst if m["slug"] == "unreachable-model")
    assert row["enabled"] is False
    assert row["probe_status"] == "unreachable"


def test_onboard_bad_tier_422(client, monkeypatch):
    monkeypatch.setattr("app.api.admin_routes.probe_endpoint", _ok)
    _login(client)
    r = client.post("/admin/infra/models", json={**_BODY, "slug": "bad-tier-model",
                                                  "tier": "strong"})
    assert r.status_code == 422


def test_onboard_bad_endpoint_422(client, monkeypatch):
    monkeypatch.setattr("app.api.admin_routes.probe_endpoint", _ok)
    _login(client)
    r = client.post("/admin/infra/models", json={**_BODY, "slug": "bad-endpoint-model",
                                                  "endpoint": "ftp://nem/v1"})
    assert r.status_code == 422


def test_onboard_negative_price_422(client, monkeypatch):
    monkeypatch.setattr("app.api.admin_routes.probe_endpoint", _ok)
    _login(client)
    r = client.post("/admin/infra/models", json={**_BODY, "slug": "bad-price-model",
                                                  "price_input": -1})
    assert r.status_code == 422


def test_onboard_unknown_app_id_422(client, monkeypatch):
    monkeypatch.setattr("app.api.admin_routes.probe_endpoint", _ok)
    _login(client)
    r = client.post("/admin/infra/models", json={**_BODY, "slug": "scoped-model",
                                                  "allowed_app_ids": ["no-such-app"]})
    assert r.status_code == 422


def test_onboard_duplicate_409(client, monkeypatch):
    monkeypatch.setattr("app.api.admin_routes.probe_endpoint", _ok)
    _login(client)
    assert client.post("/admin/infra/models", json=_BODY).status_code == 200
    assert client.post("/admin/infra/models", json=_BODY).status_code == 409


def test_onboard_duplicate_of_yaml_pool_409(client, monkeypatch):
    monkeypatch.setattr("app.api.admin_routes.probe_endpoint", _ok)
    _login(client)
    # local-8b is seeded as an onboarded row by SP_SEED_TEST_MODELS (see
    # conftest.py), which is enough to collide via store.get_onboarded — no
    # need to reach into the static YAML pool for this duplicate check.
    r = client.post("/admin/infra/models", json={**_BODY, "slug": "local-8b"})
    assert r.status_code == 409


def test_put_reprobes_and_updates(client, monkeypatch):
    monkeypatch.setattr("app.api.admin_routes.probe_endpoint", _ok)
    _login(client)
    assert client.post("/admin/infra/models", json=_BODY).status_code == 200

    monkeypatch.setattr("app.api.admin_routes.probe_endpoint", _fail)
    r = client.put("/admin/infra/models/nemotron-mid",
                   json={**_BODY, "display_name": "Nemotron v2"})
    assert r.status_code == 200
    body = r.json()
    assert body["enabled"] is False
    assert body["probe_status"] == "unreachable"

    lst = client.get("/admin/infra/models").json()
    row = next(m for m in lst if m["slug"] == "nemotron-mid")
    assert row["display_name"] == "Nemotron v2"
    assert row["enabled"] is False


def test_put_can_disable_healthy_model(client, monkeypatch):
    # Regression: enabled must be admin intent ANDed with probe result, not the
    # probe result alone — otherwise a healthy model can never be turned off.
    monkeypatch.setattr("app.api.admin_routes.probe_endpoint", _ok)
    _login(client)
    assert client.post("/admin/infra/models", json=_BODY).status_code == 200

    r = client.put("/admin/infra/models/nemotron-mid", json={**_BODY, "enabled": False})
    assert r.status_code == 200
    body = r.json()
    assert body["enabled"] is False
    assert body["probe_status"] == "healthy"

    lst = client.get("/admin/infra/models").json()
    row = next(m for m in lst if m["slug"] == "nemotron-mid")
    assert row["enabled"] is False
    assert row["probe_status"] == "healthy"

def test_put_cannot_enable_unreachable_model(client, monkeypatch):
    # Probe-gating preserved: intent=True can never overcome a failing probe.
    monkeypatch.setattr("app.api.admin_routes.probe_endpoint", _fail)
    _login(client)
    assert client.post("/admin/infra/models", json={**_BODY, "slug": "flaky-model"}).status_code == 200

    r = client.put("/admin/infra/models/flaky-model", json={**_BODY, "slug": "flaky-model", "enabled": True})
    assert r.status_code == 200
    assert r.json()["enabled"] is False

    lst = client.get("/admin/infra/models").json()
    row = next(m for m in lst if m["slug"] == "flaky-model")
    assert row["enabled"] is False

def test_onboard_disabled_intent(client, monkeypatch):
    monkeypatch.setattr("app.api.admin_routes.probe_endpoint", _ok)
    _login(client)
    r = client.post("/admin/infra/models", json={**_BODY, "slug": "intent-off-model", "enabled": False})
    assert r.status_code == 200
    body = r.json()
    assert body["enabled"] is False
    assert body["probe_status"] == "healthy"

def test_put_missing_slug_404(client, monkeypatch):
    monkeypatch.setattr("app.api.admin_routes.probe_endpoint", _ok)
    _login(client)
    r = client.put("/admin/infra/models/does-not-exist", json=_BODY)
    assert r.status_code == 404


def test_delete_onboarded(client, monkeypatch):
    monkeypatch.setattr("app.api.admin_routes.probe_endpoint", _ok)
    _login(client)
    assert client.post("/admin/infra/models", json=_BODY).status_code == 200

    r = client.delete("/admin/infra/models/nemotron-mid")
    assert r.status_code == 200

    lst = client.get("/admin/infra/models").json()
    assert not any(m["slug"] == "nemotron-mid" for m in lst)


def test_delete_missing_slug_404(client):
    _login(client)
    r = client.delete("/admin/infra/models/does-not-exist")
    assert r.status_code == 404


def test_onboard_requires_admin_auth(client):
    r = client.post("/admin/infra/models", json=_BODY)
    assert r.status_code == 401
    assert client.get("/admin/infra/models").status_code == 401
