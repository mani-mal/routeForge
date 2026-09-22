"""Admin API: `price_cached_input` on onboarded endpoints (P2a Task 3).

Follows tests/test_onboard_api.py's client/auth pattern (sync TestClient +
`client`/`_login` fixtures) rather than the async `admin_client`/`onboard_body`/
`registry` fixtures named in the task brief -- those do not exist in this repo.
`onboard_model`/`edit_onboarded_model` already refresh `request.app.state.registry`
before returning, so `client.app.state.registry.price_for(...)` after a POST/PUT
reads the live, post-refresh registry with no extra wiring.

P1b's `region`/`prompt_retention`/`accepts_raw_sensitive` round-trip is proven via
a POST then a GET /admin/infra/models readback (the onboard/edit response itself
is a small {slug, enabled, probe_status, probe_error, warnings} dict, not the full
row) -- this file follows that same established pattern rather than inventing a
different response shape.
"""
from app.apps.probe import ProbeResult

_ADMIN = {"email": "admin@routeforge.local", "password": "changeme"}

_BODY = {
    "slug": "nemotron-mid",
    "display_name": "Nemotron",
    "tier": "mid",
    "endpoint": "http://nem/v1",
    "price_input": 0.2,
    "price_output": 0.2,
}


def _login(client):
    r = client.post("/admin/login", json=_ADMIN)
    assert r.status_code == 200
    return r


async def _ok(*a, **k):
    return ProbeResult(True, "ok")


def _row(client, slug):
    return next(m for m in client.get("/admin/infra/models").json()
                if m["slug"] == slug)


def test_onboard_accepts_and_reads_back_price_cached_input(client, monkeypatch):
    monkeypatch.setattr("app.api.admin_routes.probe_endpoint", _ok)
    _login(client)
    r = client.post("/admin/infra/models", json={**_BODY, "price_cached_input": 0.30})
    assert r.status_code == 200, r.text

    assert _row(client, "nemotron-mid")["price_cached_input"] == 0.30


def test_registry_price_for_carries_the_cached_rate(client, monkeypatch):
    monkeypatch.setattr("app.api.admin_routes.probe_endpoint", _ok)
    _login(client)
    r = client.post("/admin/infra/models", json={**_BODY, "price_cached_input": 0.30})
    assert r.status_code == 200, r.text

    # onboard_model already refreshes request.app.state.registry before
    # returning -- no extra refresh needed to prove the value reached the
    # pricing layer, not just the DB row.
    price = client.app.state.registry.price_for("nemotron-mid")
    assert price.cached_input == 0.30


def test_omitting_the_field_on_edit_preserves_it(client, monkeypatch):
    """Same rule as P1b's accepts_raw_sensitive: omission preserves, explicit
    null clears. A price silently reverting to 'no discount' on an unrelated
    edit would overstate spend with no operator action."""
    monkeypatch.setattr("app.api.admin_routes.probe_endpoint", _ok)
    _login(client)
    assert client.post("/admin/infra/models",
                       json={**_BODY, "price_cached_input": 0.30}).status_code == 200

    # This PUT body has no "price_cached_input" key at all -- an omission.
    r = client.put("/admin/infra/models/nemotron-mid",
                   json={**_BODY, "quality": 0.7})
    assert r.status_code == 200, r.text

    assert _row(client, "nemotron-mid")["price_cached_input"] == 0.30


def test_explicit_null_on_edit_clears_it(client, monkeypatch):
    monkeypatch.setattr("app.api.admin_routes.probe_endpoint", _ok)
    _login(client)
    assert client.post("/admin/infra/models",
                       json={**_BODY, "price_cached_input": 0.30}).status_code == 200

    r = client.put("/admin/infra/models/nemotron-mid",
                   json={**_BODY, "price_cached_input": None})
    assert r.status_code == 200, r.text

    assert _row(client, "nemotron-mid")["price_cached_input"] is None


def test_a_negative_cached_price_is_rejected(client, monkeypatch):
    monkeypatch.setattr("app.api.admin_routes.probe_endpoint", _ok)
    _login(client)
    r = client.post("/admin/infra/models",
                    json={**_BODY, "price_cached_input": -1.0})
    assert r.status_code == 422
