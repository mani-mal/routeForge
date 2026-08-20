"""Task 3: `PgDecisionBridge` (LISTEN/NOTIFY cross-replica live-stream fan-out).

The suite has no pytest-asyncio/anyio plugin installed (see
tests/test_decision_stream.py's module docstring) — async behavior is driven
with `asyncio.run(...)` inside an ordinary sync `def test_...`, not
`@pytest.mark.asyncio`. Mirrored here rather than the brief's suggested
`@pytest.mark.asyncio` marker, which this suite can't collect.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

from app.api.bus import DecisionBus
from app.api.pg_bridge import PgDecisionBridge
from app.apps.db import Database


def test_rebuild_events_matches_serve_path_shape():
    summary = {"id": "r1", "tenant_id": "acme"}
    full = {"id": "r1", "trace": [{"step": "router", "latency_ms": 5.0},
                                  {"step": "verify", "latency_ms": 2.0}]}
    evts = PgDecisionBridge.rebuild_events(summary, full)
    assert evts[0] == {"type": "request", "summary": summary}
    assert evts[1] == {"type": "step", "request_id": "r1",
                       "record": full["trace"][0], "index": 0}
    assert evts[2]["index"] == 1
    assert len(evts) == 3


def test_inert_on_sqlite():
    db = Database("sqlite+aiosqlite://")
    b = PgDecisionBridge(db, DecisionBus(), replica_id="x")
    assert b.active is False


def test_active_on_postgres_url():
    # `asyncpg` is NOT installed in the test env (postgres extra excluded), and
    # SQLAlchemy imports the dbapi at engine construction — so we cannot build a
    # real postgres Database here. `active` only reads `.engine.url.drivername`,
    # so a stub with that attribute exercises the predicate without asyncpg.
    db = SimpleNamespace(engine=SimpleNamespace(
        url=SimpleNamespace(drivername="postgresql+asyncpg")))
    b = PgDecisionBridge(db, DecisionBus(), replica_id="x")
    assert b.active is True


def test_inert_publish_and_listen_are_noops():
    async def _run():
        db = Database("sqlite+aiosqlite://")
        b = PgDecisionBridge(db, DecisionBus(), replica_id="x")
        await b.publish_remote("r1")      # must not raise, must not import asyncpg
        await b.ensure_listening()
        await b.stop()

    asyncio.run(_run())
