"""Console read surface (/api/*). Admin-gated: every /api/* route requires an
admin session cookie (see app/apps/admin_auth.require_admin). Each test logs in
first; test_api_requires_admin_login pins the gate itself."""

_ADMIN = {"email": "admin@routeforge.local", "password": "changeme"}


def _login(client):
    r = client.post("/admin/login", json=_ADMIN)
    assert r.status_code == 200
    return r


def test_api_requires_admin_login(client):
    # No login -> the whole console read surface is 401, not readable.
    assert client.get("/api/requests").status_code == 401
    assert client.get("/api/fleet").status_code == 401
    assert client.get("/api/stats").status_code == 401
    assert client.get("/api/requests/anything").status_code == 401


def test_list_detail_stats_for_clean_request(client):
    _login(client)
    r = client.post("/v1/chat/completions", headers={"Authorization": "Bearer demo-key"},
                     json={"messages": [{"role": "user", "content": "capital of France?"}]})
    assert r.status_code == 200
    request_id = r.json()["id"]

    listing = client.get("/api/requests")
    assert listing.status_code == 200
    summaries = listing.json()
    assert len(summaries) >= 1
    match = next(s for s in summaries if s["id"] == request_id)
    assert match["tier"] == "cheap"
    assert match["model"] == "local-8b"
    assert match["tenant_id"] == "default"

    detail = client.get(f"/api/requests/{request_id}")
    assert detail.status_code == 200
    body = detail.json()
    assert body["id"] == request_id
    assert body["trace"]

    stats = client.get("/api/stats")
    assert stats.status_code == 200
    stats_body = stats.json()
    assert stats_body["count"] >= 1
    assert "cheap" in stats_body["tier_distribution"]


def test_missing_request_returns_404(client):
    _login(client)
    r = client.get("/api/requests/does-not-exist")
    assert r.status_code == 404


def test_pii_request_never_leaks_raw_ssn(client):
    _login(client)
    ssn = "482-73-1946"
    r = client.post("/v1/chat/completions", headers={"Authorization": "Bearer hospital-key"},
                     json={"messages": [{"role": "user", "content": f"verify SSN {ssn}"}]})
    assert r.status_code == 200
    request_id = r.json()["id"]
    assert ssn not in r.text

    listing = client.get("/api/requests")
    match = next(s for s in listing.json() if s["id"] == request_id)
    assert match["onprem_only"] is True
    assert match["detected_entities"]
    for entity in match["detected_entities"]:
        assert ssn not in entity
        assert entity.isupper() or "_" in entity  # types like "US_SSN", not raw values

    detail = client.get(f"/api/requests/{request_id}")
    assert detail.status_code == 200
    assert ssn not in detail.text
    assert "US_SSN" in detail.json()["policy"]["detected_entities"] or \
        detail.json()["policy"]["detected_entities"]


def test_fleet_endpoint(client):
    _login(client)
    client.post("/v1/chat/completions", headers={"Authorization": "Bearer acme-key"},
                json={"messages": [{"role": "user", "content": "hi"}]})
    r = client.get("/api/fleet")
    assert r.status_code == 200
    rows = r.json()
    assert any(row["app"] == "acme" and row["count"] >= 1 for row in rows)


def test_requests_endpoint_filters_by_app(client):
    _login(client)
    # seeded tokens acme-key / hospital-key drive one request each
    client.post("/v1/chat/completions", headers={"Authorization": "Bearer acme-key"},
                json={"messages": [{"role": "user", "content": "hi"}]})
    client.post("/v1/chat/completions", headers={"Authorization": "Bearer hospital-key"},
                json={"messages": [{"role": "user", "content": "hi"}]})
    r = client.get("/api/requests?app=acme")
    assert r.status_code == 200
    assert all(row["tenant_id"] == "acme" for row in r.json())
    assert len(r.json()) >= 1
