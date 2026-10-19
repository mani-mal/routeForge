"""Attempt-scoped tokens: service credentials, token expiry, session binding,
and scoped revoke (DevFoundry change request 2026-10-16)."""
import datetime as dt

import pytest

from app.apps import store

_ADMIN = {"email": "admin@routeforge.local", "password": "changeme"}
_MSG = {"messages": [{"role": "user", "content": "What is 2 + 2?"}]}


def _login(client):
    assert client.post("/admin/login", json=_ADMIN).status_code == 200


def _logout(client):
    client.cookies.clear()


def _service_credential(client, app_id="acme") -> str:
    _login(client)
    r = client.post(f"/admin/applications/{app_id}/service-credentials",
                    json={"name": "devfoundry"})
    assert r.status_code == 200
    cred = r.json()["credential"]
    assert cred.startswith("rf_svc_")
    _logout(client)
    return cred


def _bearer(raw: str, session: str | None = None) -> dict:
    h = {"Authorization": f"Bearer {raw}"}
    if session is not None:
        h["x-routeforge-session"] = session
    return h


def _chat(client, raw, session=None):
    return client.post("/v1/chat/completions", headers=_bearer(raw, session), json=_MSG)


def test_service_credential_creates_a_working_token_for_its_app(client):
    svc = _service_credential(client)
    r = client.post("/admin/applications/acme/tokens", headers=_bearer(svc),
                    json={"name": "attempt-1"})
    assert r.status_code == 200
    assert _chat(client, r.json()["token"]).status_code == 200


def test_service_credential_cannot_touch_another_application(client):
    svc = _service_credential(client)
    r = client.post("/admin/applications/hospital/tokens", headers=_bearer(svc), json={})
    assert r.status_code == 403


@pytest.mark.parametrize("method,path", [
    ("get", "/admin/applications"),
    ("get", "/admin/applications/acme"),
    ("put", "/admin/applications/acme/policy"),
    ("post", "/admin/applications/acme/service-credentials"),
    ("get", "/admin/applications/acme/service-credentials"),
    ("get", "/admin/usage"),
])
def test_service_credential_cannot_reach_admin_routes(client, method, path):
    svc = _service_credential(client)
    r = getattr(client, method)(path, headers=_bearer(svc),
                                **({"json": {}} if method in ("put", "post") else {}))
    assert r.status_code == 401


def test_service_credential_is_not_an_app_token(client):
    svc = _service_credential(client)
    assert _chat(client, svc).status_code == 401


def test_revoked_service_credential_is_refused(client):
    _login(client)
    r = client.post("/admin/applications/acme/service-credentials", json={})
    svc, cred_id = r.json()["credential"], r.json()["id"]
    listed = client.get("/admin/applications/acme/service-credentials").json()
    assert [c["id"] for c in listed] == [cred_id]
    assert "credential" not in listed[0]
    assert client.post(f"/admin/service-credentials/{cred_id}/revoke").status_code == 200
    _logout(client)
    r = client.post("/admin/applications/acme/tokens", headers=_bearer(svc), json={})
    assert r.status_code == 401


def test_token_is_refused_after_expiry(client, monkeypatch):
    svc = _service_credential(client)
    r = client.post("/admin/applications/acme/tokens", headers=_bearer(svc),
                    json={"expires_in_s": 60})
    assert r.status_code == 200 and r.json()["expires_at"]
    token = r.json()["token"]
    assert _chat(client, token).status_code == 200
    later = dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=61)
    monkeypatch.setattr(store, "utcnow", lambda: later)
    assert _chat(client, token).status_code == 401
    assert client.get("/v1/models", headers=_bearer(token)).status_code == 401
    _login(client)
    app = client.get("/admin/applications/acme").json()
    status = {t["id"]: t["status"] for t in app["tokens"]}
    assert status[r.json()["id"]] == "expired"


@pytest.mark.parametrize("expires_in_s", [0, -5, 30 * 24 * 3600 + 1])
def test_expires_in_s_out_of_range_is_422(client, expires_in_s):
    _login(client)
    r = client.post("/admin/applications/acme/tokens",
                    json={"expires_in_s": expires_in_s})
    assert r.status_code == 422


def test_session_bound_token_refused_for_other_or_missing_session(client):
    svc = _service_credential(client)
    token = client.post("/admin/applications/acme/tokens", headers=_bearer(svc),
                        json={"session": "attempt-A"}).json()["token"]
    assert _chat(client, token, session="attempt-A").status_code == 200
    assert _chat(client, token, session="attempt-B").status_code == 401
    assert _chat(client, token).status_code == 401


def test_unbound_token_ignores_session_header(client):
    _login(client)
    token = client.post("/admin/applications/acme/tokens", json={}).json()["token"]
    assert _chat(client, token, session="anything").status_code == 200


@pytest.mark.parametrize("session", ["", "has space", "x" * 129, "semi;colon"])
def test_invalid_session_binding_is_422(client, session):
    _login(client)
    r = client.post("/admin/applications/acme/tokens", json={"session": session})
    assert r.status_code == 422


def test_service_credential_revokes_only_its_own_tokens(client):
    svc = _service_credential(client)
    other_svc = _service_credential(client)
    mine = client.post("/admin/applications/acme/tokens", headers=_bearer(svc),
                       json={}).json()
    theirs = client.post("/admin/applications/acme/tokens", headers=_bearer(other_svc),
                         json={}).json()
    _login(client)
    admins = client.post("/admin/applications/acme/tokens", json={}).json()
    _logout(client)

    for t in (theirs, admins):
        r = client.post(f"/admin/tokens/{t['id']}/revoke", headers=_bearer(svc))
        assert r.status_code == 404
        assert _chat(client, t["token"]).status_code == 200

    r = client.post(f"/admin/tokens/{mine['id']}/revoke", headers=_bearer(svc))
    assert r.status_code == 200
    assert _chat(client, mine["token"]).status_code == 401


def test_admin_can_still_revoke_any_token(client):
    svc = _service_credential(client)
    t = client.post("/admin/applications/acme/tokens", headers=_bearer(svc),
                    json={}).json()
    _login(client)
    assert client.post(f"/admin/tokens/{t['id']}/revoke").status_code == 200
    _logout(client)
    assert _chat(client, t["token"]).status_code == 401


def test_revoking_service_credential_revokes_its_tokens(client):
    _login(client)
    r = client.post("/admin/applications/acme/service-credentials", json={}).json()
    _logout(client)
    t = client.post("/admin/applications/acme/tokens", headers=_bearer(r["credential"]),
                    json={}).json()
    _login(client)
    admins = client.post("/admin/applications/acme/tokens", json={}).json()
    assert client.post(f"/admin/service-credentials/{r['id']}/revoke").status_code == 200
    _logout(client)
    assert _chat(client, t["token"]).status_code == 401
    assert _chat(client, admins["token"]).status_code == 200


def test_service_credential_of_disabled_app_is_refused(client):
    _login(client)
    assert client.post("/admin/applications",
                       json={"id": "svcapp", "name": "S"}).status_code == 200
    svc = client.post("/admin/applications/svcapp/service-credentials",
                      json={}).json()["credential"]
    assert client.patch("/admin/applications/svcapp",
                        json={"status": "disabled"}).status_code == 200
    _logout(client)
    r = client.post("/admin/applications/svcapp/tokens", headers=_bearer(svc), json={})
    assert r.status_code == 401


def test_listed_expires_at_carries_utc_offset(client):
    _login(client)
    tid = client.post("/admin/applications/acme/tokens",
                      json={"expires_in_s": 60}).json()["id"]
    tokens = client.get("/admin/applications/acme").json()["tokens"]
    listed = next(t for t in tokens if t["id"] == tid)
    assert dt.datetime.fromisoformat(listed["expires_at"]).tzinfo is not None
