import asyncio
import json

import httpx
import pytest

from app.main import create_app

_ADMIN = {"email": "admin@routeforge.local", "password": "changeme"}
BASE = "/admin/external-metrics"


def _login(client):
    assert client.post("/admin/login", json=_ADMIN).status_code == 200


def _payload(*slugs):
    return [{"slug": s, "name": s.upper(),
             "evaluations": {"artificial_analysis_intelligence_index": 50.0},
             "pricing": {"price_1m_input_tokens": 1.0, "price_1m_output_tokens": 2.0}}
            for s in slugs]


class _Transport:
    """MockTransport that records requests."""

    def __init__(self, handler=None):
        self.requests: list[httpx.Request] = []
        self._handler = handler or (lambda r: httpx.Response(200, json=_payload("a", "b")))
        self.transport = httpx.MockTransport(self._call)

    def _call(self, request):
        self.requests.append(request)
        return self._handler(request)


def _enable(client, monkeypatch, handler=None):
    monkeypatch.setenv("AA_API_KEY", "secret-key")
    monkeypatch.delenv("SP_EXTERNAL_FETCH", raising=False)
    t = _Transport(handler)
    client.app.state.aa_transport = t.transport
    return t


def test_requires_admin(client):
    assert client.get(f"{BASE}/snapshots").status_code == 401
    assert client.get(f"{BASE}/map").status_code == 401
    assert client.put(f"{BASE}/map", json={"model": "x"}).status_code == 401
    assert client.post(f"{BASE}/import", content=b"[]").status_code == 401
    assert client.post(f"{BASE}/refresh").status_code == 401


def test_import_then_snapshots_lists_it(client, monkeypatch):
    monkeypatch.delenv("AA_API_KEY", raising=False)
    _login(client)
    r = client.post(f"{BASE}/import?index_version=v4", content=json.dumps(_payload("a", "b")))
    assert r.status_code == 200
    assert r.json()["row_count"] == 2
    body = client.get(f"{BASE}/snapshots").json()
    (snap,) = body["snapshots"]
    assert snap["id"] == r.json()["snapshot_id"] and snap["origin"] == "import"
    assert snap["index_version"] == "v4" and snap["row_count"] == 2
    assert snap["source"] == "artificial_analysis" and snap["retrieved_at"]
    assert body["fetch_enabled"] is False and "AA_API_KEY" in body["fetch_disabled_reason"]


def test_import_malformed_is_422_and_stores_nothing(client):
    _login(client)
    for bad in ("{}", "[]", "not json", json.dumps([{"name": "no slug"}])):
        assert client.post(f"{BASE}/import", content=bad).status_code == 422
    assert client.get(f"{BASE}/snapshots").json()["snapshots"] == []


def test_import_too_large_is_413(client):
    _login(client)
    big = b"[" + b"0," * (6 * 1024 * 1024) + b"0]"
    assert client.post(f"{BASE}/import", content=big).status_code == 413
    assert client.get(f"{BASE}/snapshots").json()["snapshots"] == []


def test_refresh_without_key_is_409(client, monkeypatch):
    monkeypatch.delenv("AA_API_KEY", raising=False)
    t = _Transport()
    client.app.state.aa_transport = t.transport
    _login(client)
    r = client.post(f"{BASE}/refresh")
    assert r.status_code == 409 and "AA_API_KEY" in r.json()["detail"]
    assert t.requests == []


def test_refresh_disabled_by_env_is_409(client, monkeypatch):
    t = _enable(client, monkeypatch)
    monkeypatch.setenv("SP_EXTERNAL_FETCH", "off")
    _login(client)
    r = client.post(f"{BASE}/refresh")
    assert r.status_code == 409 and "SP_EXTERNAL_FETCH" in r.json()["detail"]
    assert t.requests == []


def test_refresh_success_stores_snapshot(client, monkeypatch):
    t = _enable(client, monkeypatch)
    _login(client)
    r = client.post(f"{BASE}/refresh?index_version=v4")
    assert r.status_code == 200 and r.json()["row_count"] == 2
    (req,) = t.requests
    assert req.headers["x-api-key"] == "secret-key"
    assert str(req.url) == "https://artificialanalysis.ai/api/v2/data/llms/models"
    assert req.method == "GET" and req.content == b"" and req.url.query == b""
    assert set(req.headers) <= {"host", "x-api-key", "accept", "accept-encoding",
                                "connection", "user-agent"}
    (snap,) = client.get(f"{BASE}/snapshots").json()["snapshots"]
    assert snap["origin"] == "fetch" and snap["index_version"] == "v4"


@pytest.mark.parametrize("status", [401, 429, 500])
def test_refresh_upstream_error_is_502_and_no_snapshot(client, monkeypatch, status):
    _enable(client, monkeypatch, lambda r: httpx.Response(status))
    _login(client)
    r = client.post(f"{BASE}/refresh")
    assert r.status_code == 502 and str(status) in r.json()["detail"]
    assert client.get(f"{BASE}/snapshots").json()["snapshots"] == []
    # the rate-limit clock was not consumed: a good retry succeeds
    client.app.state.aa_transport = _Transport().transport
    assert client.post(f"{BASE}/refresh").status_code == 200


def test_refresh_timeout_is_502(client, monkeypatch):
    def boom(request):
        raise httpx.ConnectTimeout("slow", request=request)
    _enable(client, monkeypatch, boom)
    _login(client)
    assert client.post(f"{BASE}/refresh").status_code == 502
    assert client.get(f"{BASE}/snapshots").json()["snapshots"] == []


def test_refresh_rate_limited_429(client, monkeypatch):
    t = _enable(client, monkeypatch)
    _login(client)
    assert client.post(f"{BASE}/refresh").status_code == 200
    r = client.post(f"{BASE}/refresh")
    assert r.status_code == 429
    assert 0 < r.json()["detail"]["retry_after_s"] <= 600
    assert r.headers["retry-after"] == str(r.json()["detail"]["retry_after_s"])
    assert len(t.requests) == 1
    # an import is also "the newest snapshot of any origin" for the clock
    assert client.post(f"{BASE}/import", content=json.dumps(_payload("c"))).status_code == 200
    assert client.post(f"{BASE}/refresh").status_code == 429
    assert len(t.requests) == 1


def test_concurrent_refresh_one_snapshot(monkeypatch):
    app = create_app()
    t = _enable(type("C", (), {"app": app})(), monkeypatch)

    async def go():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            assert (await c.post("/admin/login", json=_ADMIN)).status_code == 200
            a, b = await asyncio.gather(c.post(f"{BASE}/refresh"), c.post(f"{BASE}/refresh"))
            snaps = (await c.get(f"{BASE}/snapshots")).json()["snapshots"]
            return sorted([a.status_code, b.status_code]), snaps

    codes, snaps = asyncio.run(go())
    assert codes == [200, 429]
    assert len(snaps) == 1 and len(t.requests) <= 2


def test_refresh_does_not_hold_the_db_session_during_the_fetch(monkeypatch):
    """C1: a slow AA fetch must not block other DB sessions (SQLite's process-wide
    write lock is what chat auth takes in db.session())."""
    app = create_app()
    entered, release = asyncio.Event(), asyncio.Event()

    async def slow(request):
        entered.set()
        await release.wait()
        return httpx.Response(200, json=_payload("a"))

    _enable(type("C", (), {"app": app})(), monkeypatch, slow)

    async def go():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            assert (await c.post("/admin/login", json=_ADMIN)).status_code == 200
            task = asyncio.create_task(c.post(f"{BASE}/refresh"))
            await asyncio.wait_for(entered.wait(), 5)
            try:
                async def other_session():
                    async with app.state.db.session() as s:
                        from sqlalchemy import text
                        await s.execute(text("select 1"))
                await asyncio.wait_for(other_session(), 2)  # TimeoutError before the fix
                # an ordinary admin request also completes while the fetch is in flight
                r = await asyncio.wait_for(c.get(f"{BASE}/snapshots"), 2)
                assert r.status_code == 200 and not task.done()
            finally:
                release.set()
            return (await task).status_code

    assert asyncio.run(go()) == 200


def test_refresh_whole_call_timeout_is_502(client, monkeypatch):
    from app.api import external_metrics_routes as routes
    monkeypatch.setattr(routes, "AA_TIMEOUT_S", 0.05)

    async def slow(request):
        await asyncio.sleep(2)
        return httpx.Response(200, json=_payload("a"))
    _enable(client, monkeypatch, slow)
    _login(client)
    r = client.post(f"{BASE}/refresh")
    assert r.status_code == 502 and "timed out" in r.json()["detail"]
    assert client.get(f"{BASE}/snapshots").json()["snapshots"] == []


def test_refresh_oversized_response_is_502(client, monkeypatch):
    big = b"[" + b"0," * (6 * 1024 * 1024) + b"0]"
    _enable(client, monkeypatch, lambda r: httpx.Response(200, content=big))
    _login(client)
    r = client.post(f"{BASE}/refresh")
    assert r.status_code == 502 and "10 MB" in r.json()["detail"]
    assert client.get(f"{BASE}/snapshots").json()["snapshots"] == []


def test_import_too_large_streamed_without_content_length_is_413(client):
    _login(client)

    def gen():
        for _ in range(11):
            yield b"0," * (512 * 1024)
    r = client.post(f"{BASE}/import", content=gen())  # chunked: no Content-Length
    assert r.status_code == 413
    assert client.get(f"{BASE}/snapshots").json()["snapshots"] == []


def test_import_envelope_date_is_used(client):
    _login(client)
    body = {"routeforge_fetched_at": "2026-09-01T10:00:00+00:00", "data": _payload("a")}
    r = client.post(f"{BASE}/import", content=json.dumps(body))
    assert r.status_code == 200 and r.json()["dated_at_upload"] is False
    (snap,) = client.get(f"{BASE}/snapshots").json()["snapshots"]
    assert snap["retrieved_at"].startswith("2026-09-01T10:00:00") and snap["dated_at_upload"] is False


def test_import_query_date_and_precedence(client):
    _login(client)
    r = client.post(f"{BASE}/import?retrieved_at=2026-08-01", content=json.dumps(_payload("a")))
    assert r.status_code == 200
    env = {"routeforge_fetched_at": "2026-09-01T00:00:00+00:00", "data": _payload("b")}
    assert client.post(f"{BASE}/import?retrieved_at=2026-08-01",
                       content=json.dumps(env)).status_code == 200
    snaps = client.get(f"{BASE}/snapshots").json()["snapshots"]
    assert snaps[0]["retrieved_at"].startswith("2026-09-01")  # envelope wins
    assert snaps[1]["retrieved_at"].startswith("2026-08-01")


def test_import_without_date_is_marked_dated_at_upload(client):
    _login(client)
    assert client.post(f"{BASE}/import", content=json.dumps(_payload("a"))).json()[
        "dated_at_upload"] is True
    (snap,) = client.get(f"{BASE}/snapshots").json()["snapshots"]
    assert snap["dated_at_upload"] is True


def test_import_future_or_bad_date_is_422(client):
    _login(client)
    future = {"routeforge_fetched_at": "2999-01-01T00:00:00+00:00", "data": _payload("a")}
    assert client.post(f"{BASE}/import", content=json.dumps(future)).status_code == 422
    assert client.post(f"{BASE}/import?retrieved_at=2999-01-01",
                       content=json.dumps(_payload("a"))).status_code == 422
    assert client.post(f"{BASE}/import?retrieved_at=yesterday",
                       content=json.dumps(_payload("a"))).status_code == 422
    bad = {"routeforge_fetched_at": "nope", "data": _payload("a")}
    assert client.post(f"{BASE}/import", content=json.dumps(bad)).status_code == 422
    assert client.get(f"{BASE}/snapshots").json()["snapshots"] == []


def test_old_imported_date_does_not_reset_refresh_clock(client, monkeypatch):
    t = _enable(client, monkeypatch)
    _login(client)
    old = {"routeforge_fetched_at": "2026-01-01T00:00:00+00:00", "data": _payload("a")}
    assert client.post(f"{BASE}/import", content=json.dumps(old)).status_code == 200
    assert client.post(f"{BASE}/refresh").status_code == 200 and len(t.requests) == 1


def test_oversized_slug_import_is_422(client):
    _login(client)
    assert client.post(f"{BASE}/import", content=json.dumps(_payload("x" * 129))).status_code == 422


def test_aa_fetch_script_wraps_payload(tmp_path, monkeypatch):
    import importlib.util, io, pathlib
    spec = importlib.util.spec_from_file_location(
        "aa_fetch", pathlib.Path(__file__).resolve().parents[2] / "scripts" / "aa_fetch.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    raw = json.dumps(_payload("a")).encode()

    class R(io.BytesIO):
        def __enter__(self): return self
        def __exit__(self, *a): return False
    monkeypatch.setenv("AA_API_KEY", "k")
    monkeypatch.setattr(mod.urllib.request, "urlopen", lambda *a, **k: R(raw))
    out = tmp_path / "o.json"
    assert mod.main(["x", str(out)]) == 0
    got = json.loads(out.read_text())
    assert set(got) == {"routeforge_fetched_at", "data"} and got["data"][0]["slug"] == "a"


@pytest.fixture
def started(client):
    """Run startup so the model registry is populated (seeded test models)."""
    with client:
        _login(client)  # first DB use runs the (test-model) seed
        st = client.app.state
        client.portal.call(st.registry.refresh, st.db)
        yield client


def _pool_name(client) -> str:
    return sorted(c.name for c in client.app.state.registry.effective_pool())[0]


def test_map_lists_pool_with_suggestions(started):
    client = started
    _login(client)
    name = _pool_name(client)
    empty = client.get(f"{BASE}/map").json()
    assert empty["snapshot_id"] is None
    assert name in {m["model"] for m in empty["models"]}
    slug = name.upper() + "-v2"
    other = ["zzz-unrelated", slug, slug + "-b", slug + "-c", slug + "-d"]
    sid = client.post(f"{BASE}/import", content=json.dumps(_payload(*other))).json()["snapshot_id"]
    body = client.get(f"{BASE}/map").json()
    assert body["snapshot_id"] == sid
    row = next(m for m in body["models"] if m["model"] == name)
    assert row["ext_model_slug"] is None and row["placement"] in ("onprem", "cloud")
    assert row["suggestions"] == [slug, slug + "-b", slug + "-c"]


def test_map_put_unknown_slug_422(started):
    client = started
    _login(client)
    name = _pool_name(client)
    r = client.put(f"{BASE}/map", json={"model": name, "ext_model_slug": "nope"})
    assert r.status_code == 422  # no snapshot yet
    client.post(f"{BASE}/import", content=json.dumps(_payload("real")))
    assert client.put(f"{BASE}/map", json={"model": name, "ext_model_slug": "nope"}).status_code == 422
    assert client.put(f"{BASE}/map", json={"model": "no-such-model",
                                           "ext_model_slug": "real"}).status_code == 404


def test_map_put_and_clear(started):
    client = started
    _login(client)
    name = _pool_name(client)
    client.post(f"{BASE}/import", content=json.dumps(_payload("real")))
    assert client.put(f"{BASE}/map", json={"model": name, "ext_model_slug": "real"}).status_code == 200
    row = next(m for m in client.get(f"{BASE}/map").json()["models"] if m["model"] == name)
    assert row["ext_model_slug"] == "real"
    assert client.put(f"{BASE}/map", json={"model": name, "ext_model_slug": None}).status_code == 200
    row = next(m for m in client.get(f"{BASE}/map").json()["models"] if m["model"] == name)
    assert row["ext_model_slug"] is None
