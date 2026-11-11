"""B86: the request path's database round trips.

- the token lookup is cached per process (`server.auth_cache_ttl_s`), with
  immediate same-process invalidation, a one-TTL bound on other replicas,
  no negative caching, and expiry/binding re-checked on every hit;
- `last_used_at` is stamped at most once a minute per token;
- the console snapshot write is buffered off the response path, flushed
  before this process's console reads and on graceful shutdown, and pruned
  every N writes rather than per request;
- the usage row and the routing outcome share one session and one commit,
  and a failing outcome never costs the usage row.

Driven through the real HTTP boundary (`create_app()` + `TestClient`).
"""
from __future__ import annotations

import asyncio
import datetime as dt
from contextlib import contextmanager

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event, func, select

from app.apps import store
from app.apps.auth_cache import AuthCache
from app.apps.db import Database
from app.apps.models import (
    AppToken, RequestSnapshot, RoutingOutcome, UsageRecord,
)
from app.apps.snapshot_writer import SnapshotWriter
from app.main import create_app

DEMO = "demo-key"
MSG = {"messages": [{"role": "user", "content": "What is 2 + 2?"}]}
ADMIN = {"email": "admin@routeforge.local", "password": "changeme"}


@contextmanager
def _client(**state):
    app = create_app()
    for k, v in state.items():
        setattr(app.state, k, v)
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


def _bearer(raw: str, session: str | None = None) -> dict:
    h = {"Authorization": f"Bearer {raw}"}
    if session is not None:
        h["x-routeforge-session"] = session
    return h


def _chat(c, raw=DEMO, session=None):
    return c.post("/v1/chat/completions", headers=_bearer(raw, session), json=MSG)


def _login(c):
    assert c.post("/admin/login", json=ADMIN).status_code == 200


def _logout(c):
    c.cookies.clear()


def _new_token(c, app_id="acme", **body) -> dict:
    _login(c)
    r = c.post(f"/admin/applications/{app_id}/tokens", json=body)
    assert r.status_code == 200, r.text
    _logout(c)
    return r.json()


def _db(c, fn):
    async def go():
        async with c.app.state.db.session() as s:
            return await fn(s)
    return c.portal.call(go)


def _count_lookups(monkeypatch) -> list:
    calls = []
    orig = store.resolve_token

    async def counting(*a, **k):
        calls.append(1)
        return await orig(*a, **k)
    monkeypatch.setattr(store, "resolve_token", counting)
    return calls


class _Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


# --------------------------------------------------------------- token cache

def test_second_request_skips_the_token_lookup(monkeypatch):
    calls = _count_lookups(monkeypatch)
    with _client() as c:
        assert _chat(c).status_code == 200
        assert _chat(c).status_code == 200
        assert c.get("/v1/models", headers=_bearer(DEMO)).status_code == 200
    assert len(calls) == 1


def test_revoke_on_same_process_rejects_next_request_at_once():
    with _client() as c:
        t = _new_token(c)
        assert _chat(c, t["token"]).status_code == 200      # now cached
        _login(c)
        assert c.post(f"/admin/tokens/{t['id']}/revoke").status_code == 200
        _logout(c)
        assert _chat(c, t["token"]).status_code == 401


def test_app_disable_on_same_process_rejects_next_request_at_once():
    with _client() as c:
        _login(c)
        token = c.post("/admin/applications",
                       json={"id": "b86off", "name": "Off"}).json()["token"]
        _logout(c)
        assert _chat(c, token).status_code == 200
        _login(c)
        assert c.patch("/admin/applications/b86off",
                       json={"status": "disabled"}).status_code == 200
        _logout(c)
        assert _chat(c, token).status_code == 401


def test_policy_edit_on_same_process_is_seen_by_next_request():
    with _client() as c:
        _login(c)
        token = c.post("/admin/applications",
                       json={"id": "b86pol", "name": "Pol"}).json()["token"]
        _logout(c)
        before = [m["id"] for m in c.get("/v1/models", headers=_bearer(token)).json()["data"]]
        assert len(before) >= 2
        _login(c)
        assert c.put("/admin/applications/b86pol/policy",
                     json={"allowed_models": [before[0]]}).status_code == 200
        _logout(c)
        after = [m["id"] for m in c.get("/v1/models", headers=_bearer(token)).json()["data"]]
        assert after == [before[0]]


def test_other_replica_serves_revoked_token_until_ttl_then_rejects(monkeypatch):
    clock = _Clock()
    # A cache that is NOT registered for this process's invalidation: it
    # stands for another replica's cache over the same database.
    other = AuthCache(5.0, clock=clock)
    with _client() as c:
        t = _new_token(c)
        c.app.state.auth_cache = other
        assert _chat(c, t["token"]).status_code == 200
        _login(c)
        assert c.post(f"/admin/tokens/{t['id']}/revoke").status_code == 200
        _logout(c)
        clock.t += 4.9
        assert _chat(c, t["token"]).status_code == 200    # stale, within one TTL
        clock.t += 0.2
        assert _chat(c, t["token"]).status_code == 401    # TTL passed: DB says revoked


def test_ttl_zero_hits_the_database_every_request(monkeypatch):
    calls = _count_lookups(monkeypatch)
    with _client(auth_cache=AuthCache(0)) as c:
        for _ in range(3):
            assert _chat(c).status_code == 200
    assert len(calls) == 3


def test_default_ttl_from_config():
    from app.config.loader import load_settings
    import yaml
    from pathlib import Path
    raw = yaml.safe_load(Path("config/serving.yaml").read_text())
    assert raw["server"]["auth_cache_ttl_s"] == 5.0
    assert load_settings().serving.auth_cache_ttl_s == 5.0


def test_negative_ttl_is_a_named_lint_error():
    from app.config.lint import lint_settings
    from app.config.loader import load_settings
    s = load_settings()
    bad = s.model_copy(update={"serving": s.serving.model_copy(
        update={"auth_cache_ttl_s": -1.0})})
    assert any(e.startswith("AUTH_CACHE_TTL_NEGATIVE") for e in lint_settings(bad))
    assert not any(e.startswith("AUTH_CACHE_TTL") for e in lint_settings(s))


def test_unknown_token_is_not_cached_and_a_new_token_works_at_once():
    raw = "b86-fresh-token"
    with _client() as c:
        assert _chat(c, raw).status_code == 401
        assert len(c.app.state.auth_cache) == 0

        async def add(s):
            await store.add_token(s, application_id="acme", raw_token=raw)
            await s.commit()
        _db(c, add)
        assert _chat(c, raw).status_code == 200


def test_expired_token_rejected_even_when_cached(monkeypatch):
    calls = _count_lookups(monkeypatch)
    with _client() as c:
        t = _new_token(c, expires_in_s=60)
        assert _chat(c, t["token"]).status_code == 200
        n = len(calls)
        later = dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=61)
        monkeypatch.setattr(store, "utcnow", lambda: later)
        assert len(c.app.state.auth_cache) >= 1
        assert _chat(c, t["token"]).status_code == 401
        assert len(calls) == n          # rejected from the cached expiry


def test_session_binding_rechecked_on_cache_hit():
    with _client() as c:
        t = _new_token(c, session="attempt-A")
        assert _chat(c, t["token"], session="attempt-A").status_code == 200
        assert _chat(c, t["token"], session="attempt-B").status_code == 401
        assert _chat(c, t["token"]).status_code == 401
        assert _chat(c, t["token"], session="attempt-A").status_code == 200


def test_cache_error_falls_back_to_database(monkeypatch):
    calls = _count_lookups(monkeypatch)
    with _client() as c:
        assert _chat(c).status_code == 200

        def boom(*a, **k):
            raise RuntimeError("cache broken")
        monkeypatch.setattr(c.app.state.auth_cache, "get", boom)
        monkeypatch.setattr(c.app.state.auth_cache, "put", boom)
        assert _chat(c).status_code == 200
        assert _chat(c, "nope").status_code == 401
    assert len(calls) == 3


def test_lookup_racing_an_invalidation_does_not_repopulate():
    cache = AuthCache(5.0)
    gen = cache.generation
    cache.clear()                                   # an admin write landed meanwhile
    cache.put("h", object(), gen)                   # the stale lookup's result
    assert cache.get("h") is None


def test_cache_is_bounded():
    cache = AuthCache(5.0, max_entries=3)
    for i in range(5):
        cache.put(f"h{i}", object(), cache.generation)
    assert len(cache) == 3 and cache.get("h0") is None and cache.get("h4") is not None


def test_last_used_at_stamp_is_not_an_invalidation():
    with _client() as c:
        assert _chat(c).status_code == 200
        gen = c.app.state.auth_cache.generation
        c.app.state.auth_cache._stamped.clear()     # force a stamp on the next hit
        assert _chat(c).status_code == 200
        assert c.app.state.auth_cache.generation == gen


# --------------------------------------------------------------- last_used_at

def _last_used(c, token_id):
    async def get(s):
        return (await s.get(AppToken, token_id, populate_existing=True)).last_used_at
    return _db(c, get)


def test_last_used_at_stamped_at_most_once_a_minute():
    clock = _Clock()
    with _client(auth_cache=AuthCache(5.0, clock=clock)) as c:
        c.app.state.db.on_auth_change(c.app.state.auth_cache.clear)
        t = _new_token(c)
        assert _last_used(c, t["id"]) is None
        assert _chat(c, t["token"]).status_code == 200
        first = _last_used(c, t["id"])
        assert first is not None
        clock.t += 30          # cache expired (TTL 5) -> DB lookup, but no stamp
        assert _chat(c, t["token"]).status_code == 200
        clock.t += 2           # cache hit, no stamp
        assert _chat(c, t["token"]).status_code == 200
        assert _last_used(c, t["id"]) == first
        clock.t += 29          # 61 s after the first stamp, on a cache hit
        assert _chat(c, t["token"]).status_code == 200
        assert _last_used(c, t["id"]) > first


def test_ttl_zero_stamps_every_request():
    with _client(auth_cache=AuthCache(0)) as c:
        t = _new_token(c)
        assert _chat(c, t["token"]).status_code == 200
        first = _last_used(c, t["id"])
        assert _chat(c, t["token"]).status_code == 200
        assert _last_used(c, t["id"]) > first


# --------------------------------------------------------------- snapshots

def _snapshot_rows(c) -> int:
    async def n(s):
        return (await s.execute(select(func.count()).select_from(RequestSnapshot))).scalar_one()
    return _db(c, n)


def test_request_returns_before_the_snapshot_write_and_console_sees_it():
    app = create_app()
    app.state.snapshot_writer.flush_interval_s = 3600
    with TestClient(app) as c:
        r = _chat(c)
        assert r.status_code == 200
        rid = r.json()["id"]
        assert app.state.snapshot_writer.pending() == 1
        assert _snapshot_rows(c) == 0              # not written on the response path
        _login(c)
        listed = c.get("/api/requests?limit=10").json()
        assert [x["id"] for x in listed] == [rid]   # this process flushes before reading
        assert c.get(f"/api/requests/{rid}").json()["id"] == rid
        assert _snapshot_rows(c) == 1


def test_background_loop_writes_within_a_flush_interval():
    app = create_app()
    app.state.snapshot_writer.flush_interval_s = 0.05
    with TestClient(app) as c:
        assert _chat(c).status_code == 200

        async def wait():
            for _ in range(200):
                if app.state.snapshot_writer.pending() == 0:
                    async with app.state.db.session() as s:
                        n = (await s.execute(select(func.count()).select_from(
                            RequestSnapshot))).scalar_one()
                    if n == 1:
                        return n
                await asyncio.sleep(0.01)
            return 0
        assert c.portal.call(wait) == 1


def test_graceful_shutdown_drains_pending_snapshots(monkeypatch, tmp_path):
    url = f"sqlite+aiosqlite:///{tmp_path}/b86.db"
    monkeypatch.setenv("DATABASE_URL", url)
    app = create_app()
    app.state.snapshot_writer.flush_interval_s = 3600
    with TestClient(app) as c:
        ids = [_chat(c).json()["id"] for _ in range(3)]
        assert app.state.snapshot_writer.pending() == 3

    async def read():
        db = Database(url)
        try:
            async with db.engine.connect() as conn:
                rows = await conn.execute(select(RequestSnapshot.request_id))
                return {r[0] for r in rows}
        finally:
            await db.dispose()
    assert asyncio.run(read()) == set(ids)


def test_prune_runs_every_n_writes_not_per_request(monkeypatch):
    prunes = []
    orig = store.prune_snapshots

    async def counting(*a, **k):
        prunes.append(1)
        return await orig(*a, **k)
    monkeypatch.setattr(store, "prune_snapshots", counting)
    app = create_app()
    w = app.state.snapshot_writer
    w.flush_interval_s = 3600
    w.prune_every = 4
    with TestClient(app) as c:
        for _ in range(3):
            assert _chat(c).status_code == 200
            c.portal.call(w.flush)
        assert prunes == []                    # 3 requests, 3 flushes, no prune
        assert _chat(c).status_code == 200
        c.portal.call(w.flush)
        assert len(prunes) == 1                # the 4th write
        for _ in range(3):
            assert _chat(c).status_code == 200
            c.portal.call(w.flush)
        assert len(prunes) == 1


def test_prune_still_bounds_the_table(monkeypatch):
    app = create_app()
    w = app.state.snapshot_writer
    w.flush_interval_s = 3600
    w.capacity, w.prune_every = 2, 1
    with TestClient(app) as c:
        for _ in range(4):
            assert _chat(c).status_code == 200
        c.portal.call(w.flush)
        assert _snapshot_rows(c) == 2


def test_flush_keeps_request_order_and_notifies_in_the_same_transaction():
    notified = []

    class Bridge:
        async def notify_in(self, session, ids):
            assert session.in_transaction()
            notified.extend(ids)
    app = create_app()
    w = app.state.snapshot_writer
    w.flush_interval_s = 3600
    w._bridge = Bridge()
    with TestClient(app) as c:
        ids = [_chat(c).json()["id"] for _ in range(3)]
        _login(c)
        listed = [x["id"] for x in c.get("/api/requests?limit=10").json()]
    assert notified == ids
    assert listed == list(reversed(ids))       # newest first, as before


def test_buffer_is_bounded():
    w = SnapshotWriter(db=None, capacity=2)
    for i in range(5):
        w.enqueue(request_id=f"r{i}", application_id="a", summary_json={}, full_json={})
    assert w.pending() == 2 and w.dropped == 3


def test_snapshot_copies_the_response():
    w = SnapshotWriter(db=None, capacity=5)
    resp = {"id": "r", "x": [1]}
    w.enqueue(request_id="r", application_id="a", summary_json={}, full_json=resp)
    resp["x"].append(2)
    assert w._buf[0]["full_json"] == {"id": "r", "x": [1]}


# --------------------------------------------------------------- usage + outcome

def _commits(c) -> list:
    seen = []
    event.listen(c.app.state.db.engine.sync_engine, "commit", lambda conn: seen.append(1))
    return seen


def test_a_cached_request_makes_one_commit():
    with _client() as c:
        c.app.state.snapshot_writer.flush_interval_s = 3600
        assert _chat(c).status_code == 200       # first: lookup + last_used_at stamp
        commits = _commits(c)
        assert _chat(c).status_code == 200
        assert len(commits) == 1                 # usage + outcome, together

        async def rows(s):
            u = (await s.execute(select(func.count()).select_from(UsageRecord))).scalar_one()
            o = (await s.execute(select(func.count()).select_from(RoutingOutcome))).scalar_one()
            return u, o
        assert _db(c, rows) == (2, 2)


def test_failing_outcome_write_keeps_the_usage_row(monkeypatch):
    async def bad_outcome(session, row):
        # A DB-level failure: a NULL primary key fails at flush, inside the
        # outcome's savepoint.
        session.add(RoutingOutcome(**{**row, "request_id": None}))
    monkeypatch.setattr(store, "write_outcome", bad_outcome)
    with _client() as c:
        r = _chat(c)
        assert r.status_code == 200
        rid = r.json()["id"]

        async def rows(s):
            u = (await s.execute(select(UsageRecord.request_id))).scalars().all()
            o = (await s.execute(select(func.count()).select_from(RoutingOutcome))).scalar_one()
            return list(u), o
        usage, outcomes = _db(c, rows)
    assert rid in usage and outcomes == 0


def test_raising_outcome_write_keeps_the_usage_row(monkeypatch):
    async def boom(session, row):
        raise RuntimeError("outcome broken")
    monkeypatch.setattr(store, "write_outcome", boom)
    with _client() as c:
        rid = _chat(c).json()["id"]
        usage = _db(c, lambda s: _usage_ids(s))
    assert rid in usage


async def _usage_ids(s):
    return list((await s.execute(select(UsageRecord.request_id))).scalars().all())


def test_failing_usage_write_still_writes_the_outcome(monkeypatch):
    async def bad_usage(session, **kw):
        session.add(UsageRecord(id=None, **kw))      # fails at flush
    monkeypatch.setattr(store, "record_usage", bad_usage)
    with _client() as c:
        r = _chat(c)
        assert r.status_code == 200
        rid = r.json()["id"]

        async def rows(s):
            return (await s.execute(select(RoutingOutcome.request_id))).scalars().all()
        assert rid in _db(c, rows)


def test_record_snapshots_replaces_by_request_id():
    async def go():
        db = Database("sqlite+aiosqlite://")
        try:
            await db.ensure_ready()
            async with db.session() as s:
                await store.create_application(s, app_id="a", name="A")
                await store.record_snapshot(s, request_id="r1", application_id="a",
                                            summary_json={"v": 0}, full_json={"v": 0})
                await s.commit()
            async with db.session() as s:
                await store.record_snapshots(s, [
                    {"request_id": "r1", "application_id": "a",
                     "summary_json": {"v": 1}, "full_json": {"v": 1}},
                    {"request_id": "r2", "application_id": "a",
                     "summary_json": {"v": 2}, "full_json": {"v": 2}},
                    {"request_id": "r2", "application_id": "a",
                     "summary_json": {"v": 3}, "full_json": {"v": 3}},
                ])
                await s.commit()
            async with db.session() as s:
                return {rid: await store.get_snapshot_full(s, rid) for rid in ("r1", "r2")}
        finally:
            await db.dispose()
    assert asyncio.run(go()) == {"r1": {"v": 1}, "r2": {"v": 3}}
