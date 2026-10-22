"""Task C: `GET /admin/outcomes/export` accepts a service credential, scoped
to its own application. The 401/403 cases are the point."""
from __future__ import annotations

import datetime as dt

from tests.test_admin_api import _login
from tests.test_attempt_scoped_tokens import _bearer, _logout, _service_credential
from tests.test_outcome_export import _lines, _seed, _write_feedback

_URL = "/admin/outcomes/export"


def _setup(client) -> str:
    """Two apps with outcome rows + feedback; returns an `acme` credential."""
    now = dt.datetime.now(dt.timezone.utc)
    _seed(client.app, [
        ("a1", "acme", now - dt.timedelta(hours=1)),
        ("a2", "acme", now - dt.timedelta(hours=2)),
        ("h1", "hospital", now - dt.timedelta(hours=3)),
    ])
    _write_feedback(client.app, "a1", "acme", score_name="thumbs",
                    value_num=1.0, value_cat=None, source="app")
    _write_feedback(client.app, "h1", "hospital", score_name="hospital_only",
                    value_num=0.0, value_cat=None, source="app")
    return _service_credential(client, "acme")


def _check_own_only(r):
    assert r.status_code == 200
    lines = _lines(r)
    assert {ln["outcome"]["request_id"] for ln in lines} == {"a1", "a2"}
    assert {ln["outcome"]["application_id"] for ln in lines} == {"acme"}
    names = {f["score_name"] for ln in lines for f in ln["feedback"]}
    assert names == {"thumbs"}


def test_service_credential_omitted_app_is_forced_to_own(client):
    svc = _setup(client)
    _check_own_only(client.get(_URL, headers=_bearer(svc)))


def test_service_credential_explicit_own_app(client):
    svc = _setup(client)
    _check_own_only(client.get(_URL, params={"application_id": "acme"},
                               headers=_bearer(svc)))


def test_service_credential_other_app_is_403(client):
    svc = _setup(client)
    r = client.get(_URL, params={"application_id": "hospital"}, headers=_bearer(svc))
    assert r.status_code == 403
    assert "scoped to another application" in r.json()["detail"]
    assert "h1" not in r.text


def test_revoked_service_credential_is_401(client):
    svc = _setup(client)
    _login(client)
    cred_id = client.get("/admin/applications/acme/service-credentials").json()[0]["id"]
    assert client.post(f"/admin/service-credentials/{cred_id}/revoke").status_code == 200
    _logout(client)
    assert client.get(_URL, headers=_bearer(svc)).status_code == 401


def test_app_token_is_401(client):
    _setup(client)
    _login(client)
    tok = client.post("/admin/applications/acme/tokens", json={}).json()["token"]
    assert tok.startswith("rf_live_")
    _logout(client)
    assert client.get(_URL, headers=_bearer(tok)).status_code == 401


def test_no_auth_is_401(client):
    _setup(client)
    assert client.get(_URL).status_code == 401


def test_admin_cookie_still_sees_both_apps(client):
    _setup(client)
    _login(client)
    r = client.get(_URL)
    assert r.status_code == 200
    assert {ln["outcome"]["request_id"] for ln in _lines(r)} == {"a1", "a2", "h1"}
    r = client.get(_URL, params={"application_id": "hospital"})
    assert {ln["outcome"]["request_id"] for ln in _lines(r)} == {"h1"}
