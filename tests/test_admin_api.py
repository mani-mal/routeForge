"""Admin API: auth gating, app/token lifecycle, policy, usage (Phase 3)."""
import pytest
from fastapi.testclient import TestClient

from app.main import create_app

_ADMIN = {"email": "admin@routeforge.local", "password": "changeme"}
_MSG = {"messages": [{"role": "user", "content": "hi"}]}


def _login(client):
    r = client.post("/admin/login", json=_ADMIN)
    assert r.status_code == 200
    return r


def test_admin_routes_require_login(client):
    assert client.get("/admin/applications").status_code == 401
    assert client.get("/admin/me").status_code == 401


def test_bad_credentials_rejected(client):
    r = client.post("/admin/login", json={"email": _ADMIN["email"], "password": "nope"})
    assert r.status_code == 401


def test_login_then_list_seeded_apps(client):
    _login(client)
    r = client.get("/admin/applications")
    assert r.status_code == 200
    ids = {a["id"] for a in r.json()}
    assert {"acme", "hospital", "default"} <= ids


def test_create_application_issues_a_working_token(client):
    _login(client)
    r = client.post("/admin/applications", json={"id": "newapp", "name": "New App"})
    assert r.status_code == 200
    token = r.json()["token"]
    assert token.startswith("rf_live_")
    # end-to-end proof: the freshly issued token authenticates a real request
    served = client.post("/v1/chat/completions",
                         headers={"Authorization": f"Bearer {token}"}, json=_MSG)
    assert served.status_code == 200


def test_duplicate_application_id_conflicts(client):
    _login(client)
    assert client.post("/admin/applications",
                       json={"id": "dup", "name": "A"}).status_code == 200
    assert client.post("/admin/applications",
                       json={"id": "dup", "name": "B"}).status_code == 409


def test_policy_then_rate_limit_enforced(client):
    _login(client)
    token = client.post("/admin/applications",
                        json={"id": "limited", "name": "Ltd"}).json()["token"]
    assert client.put("/admin/applications/limited/policy",
                      json={"max_rpm": 1}).status_code == 200
    h = {"Authorization": f"Bearer {token}"}
    assert client.post("/v1/chat/completions", headers=h, json=_MSG).status_code == 200
    assert client.post("/v1/chat/completions", headers=h, json=_MSG).status_code == 429


def test_token_revocation_blocks_serving(client):
    _login(client)
    client.post("/admin/applications", json={"id": "revapp", "name": "Rev"})
    made = client.post("/admin/applications/revapp/tokens", json={"name": "t2"}).json()
    h = {"Authorization": f"Bearer {made['token']}"}
    assert client.post("/v1/chat/completions", headers=h, json=_MSG).status_code == 200
    assert client.post(f"/admin/tokens/{made['id']}/revoke").json()["ok"] is True
    assert client.post("/v1/chat/completions", headers=h, json=_MSG).status_code == 401


def test_policy_full_capture_rejected_for_ineligible_app(client):
    _login(client)
    client.post("/admin/applications", json={"id": "onp", "name": "Onp",
                                             "default_residency": "onprem_only"})
    r = client.put("/admin/applications/onp/policy", json={"capture_io": "full"})
    assert r.status_code == 422
    assert "residency" in r.text.lower() or "project" in r.text.lower()
    # redacted is always allowed
    assert client.put("/admin/applications/onp/policy",
                      json={"capture_io": "redacted"}).status_code == 200


def test_policy_rejects_bad_residency(client):
    _login(client)
    client.post("/admin/applications", json={"id": "resapp", "name": "Res"})
    r = client.put("/admin/applications/resapp/policy", json={"residency": "bogus"})
    assert r.status_code == 422
    assert "residency" in r.text.lower()
    # a valid residency is accepted
    assert client.put("/admin/applications/resapp/policy",
                      json={"residency": "private_cloud_ok"}).status_code == 200


def test_usage_summary_reflects_served_requests(client):
    _login(client)
    token = client.post("/admin/applications",
                        json={"id": "u1", "name": "U1"}).json()["token"]
    client.post("/v1/chat/completions",
                headers={"Authorization": f"Bearer {token}"}, json=_MSG)
    r = client.get("/admin/usage", params={"application_id": "u1"})
    assert r.status_code == 200
    body = r.json()
    assert body["count"] >= 1
    assert body["total_tokens"] > 0


def test_manual_langfuse_creds_enable_full_capture(client):
    _login(client)
    resp = client.post("/admin/applications",
                       json={"id": "airgap", "name": "Airgap"}).json()
    app_id = resp["application"]["id"]
    # Before creds: full is rejected (no own project) — Phase B lint.
    r = client.put(f"/admin/applications/{app_id}/policy", json={"capture_io": "full"})
    assert r.status_code == 422
    # Attach manual creds.
    r = client.put(f"/admin/applications/{app_id}/langfuse", json={
        "host": "http://airgap:3000", "public_key": "pk", "secret_key": "sk",
        "project_id": "proj_air"})
    assert r.status_code == 200, r.text
    # Now full is permitted (residency default is not onprem_only).
    r = client.put(f"/admin/applications/{app_id}/policy", json={"capture_io": "full"})
    assert r.status_code == 200, r.text


def test_manual_langfuse_creds_404_for_unknown_app(client):
    _login(client)
    r = client.put("/admin/applications/doesnotexist/langfuse", json={
        "host": "http://h:3000", "public_key": "pk", "secret_key": "sk",
        "project_id": "p"})
    assert r.status_code == 404


def test_manual_langfuse_creds_requires_admin_login(client):
    r = client.put("/admin/applications/anything/langfuse", json={
        "host": "http://h:3000", "public_key": "pk", "secret_key": "sk",
        "project_id": "p"})
    assert r.status_code == 401


def test_app_dict_never_returns_secret(client):
    _login(client)
    resp = client.post("/admin/applications",
                       json={"id": "nosecret", "name": "NoSecret"}).json()
    app_id = resp["application"]["id"]
    client.put(f"/admin/applications/{app_id}/langfuse", json={
        "host": "http://h:3000", "public_key": "pk", "secret_key": "TOPSECRET",
        "project_id": "p"})
    body = client.get("/admin/applications").json()
    blob = str(body)
    assert "TOPSECRET" not in blob
    app = next(a for a in body if a["id"] == app_id)
    assert app["has_langfuse_keys"] is True
    assert app["langfuse_host"] == "http://h:3000"


def test_manual_langfuse_creds_rejects_blank_or_bad_input(client):
    _login(client)
    resp = client.post("/admin/applications",
                       json={"id": "badcreds", "name": "BadCreds"}).json()
    app_id = resp["application"]["id"]
    # Blank/whitespace-only secret_key must not silently encrypt to a
    # non-empty Fernet token and unlock capture_io='full'.
    r = client.put(f"/admin/applications/{app_id}/langfuse", json={
        "host": "https://h:3000", "public_key": "pk", "secret_key": "   ",
        "project_id": "p"})
    assert r.status_code == 422
    # Non-http(s) host is rejected too.
    r = client.put(f"/admin/applications/{app_id}/langfuse", json={
        "host": "not-a-url", "public_key": "pk", "secret_key": "sk",
        "project_id": "p"})
    assert r.status_code == 422
    # Neither rejected call actually stored creds.
    body = client.get("/admin/applications").json()
    app = next(a for a in body if a["id"] == app_id)
    assert app["has_langfuse_keys"] is False


def test_policy_pii_detection_downgrade_ok_when_any_residency_and_capture_off(client):
    # (a) valid downgrade on an `any`-residency app with capture explicitly off.
    _login(client)
    client.post("/admin/applications", json={"id": "pii-ok", "name": "PiiOk"})
    r = client.put("/admin/applications/pii-ok/policy",
                   json={"pii_detection": "regex", "capture_io": "off"})
    assert r.status_code == 200, r.text
    assert r.json()["policy"]["pii_detection"] == "regex"


def test_policy_pii_detection_downgrade_rejected_for_onprem_only(client):
    # (b) pii_detection="off" on an onprem_only app -> 422.
    _login(client)
    client.post("/admin/applications", json={"id": "pii-onprem", "name": "PiiOnprem",
                                              "default_residency": "onprem_only"})
    r = client.put("/admin/applications/pii-onprem/policy", json={"pii_detection": "off"})
    assert r.status_code == 422
    assert "onprem_only" in r.text.lower()


def test_policy_pii_detection_downgrade_rejected_when_capture_enabled(client):
    # (c) pii_detection="off" while capture_io="redacted" is in effect -> 422.
    _login(client)
    client.post("/admin/applications", json={"id": "pii-captured", "name": "PiiCaptured"})
    assert client.put("/admin/applications/pii-captured/policy",
                      json={"capture_io": "redacted"}).status_code == 200
    r = client.put("/admin/applications/pii-captured/policy", json={"pii_detection": "off"})
    assert r.status_code == 422
    assert "capture_io" in r.text.lower()


def test_policy_stream_unverified_rejected_for_onprem_only(client):
    # (d) stream_mode="stream_unverified" on an onprem_only app -> 422.
    _login(client)
    client.post("/admin/applications", json={"id": "stream-onprem", "name": "StreamOnprem",
                                              "default_residency": "onprem_only"})
    r = client.put("/admin/applications/stream-onprem/policy",
                   json={"stream_mode": "stream_unverified"})
    assert r.status_code == 422
    assert "onprem_only" in r.text.lower()


def test_policy_rejects_bad_enum_values(client):
    # (e) values outside the canonical enums -> 422.
    _login(client)
    client.post("/admin/applications", json={"id": "bad-enum", "name": "BadEnum"})
    r = client.put("/admin/applications/bad-enum/policy",
                   json={"pii_detection": "bogus"})
    assert r.status_code == 422
    r = client.put("/admin/applications/bad-enum/policy",
                   json={"stream_mode": "bogus"})
    assert r.status_code == 422


def test_policy_deployment_ceiling_blocks_pii_downgrade(monkeypatch):
    # (f) allow_app_pii_downgrade: false blocks a downgrade even when residency
    # and capture_io would otherwise permit it.
    monkeypatch.setenv("SP_ALLOW_APP_PII_DOWNGRADE", "false")
    with TestClient(create_app()) as c:
        _login(c)
        c.post("/admin/applications", json={"id": "pii-locked", "name": "PiiLocked"})
        r = c.put("/admin/applications/pii-locked/policy",
                  json={"pii_detection": "regex", "capture_io": "off"})
        assert r.status_code == 422
        assert "disabled" in r.text.lower()


def test_policy_pii_and_stream_mode_round_trip(client):
    # (g) GET after PUT reflects the persisted values.
    _login(client)
    client.post("/admin/applications", json={"id": "roundtrip", "name": "Roundtrip"})
    r = client.put("/admin/applications/roundtrip/policy",
                   json={"pii_detection": "presidio",
                         "stream_mode": "verified_then_stream"})
    assert r.status_code == 200, r.text
    assert r.json()["policy"]["pii_detection"] == "presidio"
    assert r.json()["policy"]["stream_mode"] == "verified_then_stream"
    got = client.get("/admin/applications/roundtrip").json()
    assert got["policy"]["pii_detection"] == "presidio"
    assert got["policy"]["stream_mode"] == "verified_then_stream"


def test_onprem_only_cannot_reach_full_even_with_manual_creds(client):
    _login(client)
    client.post("/admin/applications", json={"id": "onprem-app", "name": "OnpremApp",
                                             "default_residency": "onprem_only"})
    # Valid, well-formed manual creds — proves the block below is the
    # residency clamp, not the missing-project check (CLAUDE.md rule 8:
    # residency beats eval; onprem_only must never be lifted to unlock 'full').
    r = client.put("/admin/applications/onprem-app/langfuse", json={
        "host": "https://onprem-langfuse:3000", "public_key": "pk",
        "secret_key": "sk", "project_id": "proj_onprem"})
    assert r.status_code == 200, r.text
    r = client.put("/admin/applications/onprem-app/policy", json={"capture_io": "full"})
    assert r.status_code == 422
    assert "residency" in r.text.lower() or "onprem" in r.text.lower()
