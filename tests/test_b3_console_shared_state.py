"""B3 Task 4: serve path + console read endpoints backed by the shared
`request_snapshots` table (Postgres in prod, sqlite in tests) instead of the
process-local `HistoryStore`. See `app/apps/store.py` (record/read fns) and
`app/api/pg_bridge.py` (cross-replica NOTIFY, inert on sqlite)."""

_TOKEN = "demo-key"  # seeded bearer token for the "default" application (see app/apps/bootstrap.py)


def _auth(client):
    r = client.post("/admin/login", json={"email": "admin@routeforge.local",
                                           "password": "changeme"})
    assert r.status_code == 200


def _chat(client, token, content):
    return client.post("/v1/chat/completions", headers={"Authorization": f"Bearer {token}"},
                       json={"model": "auto", "messages": [{"role": "user", "content": content}]})


def test_requests_and_stats_come_from_db(client):
    # two served requests are both visible via the DB-backed console endpoints
    assert _chat(client, _TOKEN, "capital of France?").status_code == 200
    assert _chat(client, _TOKEN, "capital of Spain?").status_code == 200
    _auth(client)
    reqs = client.get("/api/requests?limit=100").json()
    assert len(reqs) >= 2
    stats = client.get("/api/stats").json()
    assert stats["count"] >= 2
    rid = reqs[0]["id"]
    full = client.get(f"/api/requests/{rid}").json()
    assert full["id"] == rid and "trace" in full


def test_full_json_has_no_raw_pii(client):
    _chat(client, _TOKEN, "My SSN is 123-45-6789, is that valid?")
    _auth(client)
    reqs = client.get("/api/requests?limit=10").json()
    rid = reqs[0]["id"]
    full = client.get(f"/api/requests/{rid}").json()
    blob = str(full)
    assert "123-45-6789" not in blob
    assert "sanitized_prompt" not in full and "raw_completion" not in full


def test_stream_still_connects_single_replica(live_internal_server):
    # NOTE: deliberately NOT `client.stream(...)` — httpx's ASGITransport (which
    # backs TestClient) fully drains the ASGI app callable before it constructs
    # any Response, so it hangs forever against `/api/stream`'s intentionally
    # infinite generator. Same reasoning/pattern as
    # tests/test_decision_stream.py::test_stream_endpoint_returns_event_stream —
    # use a real socket via `live_internal_server` instead.
    import http.client

    from tests.conftest import admin_cookie

    cookie = admin_cookie(live_internal_server.port)
    conn = http.client.HTTPConnection("127.0.0.1", live_internal_server.port, timeout=5)
    try:
        conn.request("GET", "/api/stream", headers={"Cookie": cookie})
        resp = conn.getresponse()
        assert resp.status == 200          # bridge inert on sqlite; local bus serves
        assert b"connected" in resp.readline()
    finally:
        conn.close()
