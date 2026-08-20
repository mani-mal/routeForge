# B3 — Shared Dashboard State Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the operator console (list/detail/stats/fleet + live SSE stream) coherent across 2+ API pods by moving process-local state into the shared Postgres.

**Architecture:** A new `request_snapshots` table holds the already-redacted request summary + full resp JSON; the four `/api/*` read endpoints query it instead of the in-memory `HistoryStore`. A `PgDecisionBridge` fans the live stream across replicas via Postgres `LISTEN/NOTIFY` (inert on SQLite, which is tests/dev only). `UsageRecord` (metering) and `DecisionBus` (local fan-out) are unchanged.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy 2 async, asyncpg (postgres extra), Pydantic v2, pytest + FastAPI TestClient (in-memory SQLite).

**Spec:** `docs/superpowers/specs/2026-08-20-b3-shared-dashboard-state-design.md`

## Global Constraints

- **Redaction (CLAUDE.md rule 2):** `full_json` stores ONLY the resp dict *after* `sanitized_prompt`/`raw_completion` are popped (`routes.py:198-199`, before the record path). It must never contain raw prompt/completion text or CCR contents. Every task that writes or asserts on snapshots must uphold and (where testable) assert this.
- **Fail-open serving (CLAUDE.md pitfalls):** the snapshot write, the `NOTIFY`, and the `LISTEN` bridge all live inside the existing "must never break serving" `try/except` guards in `routes.py`. A DB or bridge failure never breaks a `/v1/chat/completions` response.
- **SQLite is tests/dev only.** Every deploy is Postgres. The bridge must be **inert** on a non-postgres DB URL and must **not** import `asyncpg` at module top (import lazily inside the active branch) so the suite runs without the `postgres` extra.
- **No new YAML/config keys.** Channel name and snapshot capacity are module constants with optional env override — `_DECISION_CHANNEL` (`SP_DECISION_CHANNEL`, default `route_decisions`) and `_SNAPSHOT_CAPACITY` (`SP_SNAPSHOT_CAPACITY`, default `500`). This keeps the cross-file config lint untouched.
- **Global ordering by `ts DESC`,** never the per-process `created_seq` (kept in the summary only for response-shape compatibility, set to `0`).
- **Reuse aggregation math.** `/api/stats` and `/api/fleet` must reuse the extracted pure functions — do NOT reimplement the percentile / token-&-cost-intelligence-density / `onprem_violations` logic.
- Type-hint everything; boring, readable code (it's read on a projector).

---

### Task 1: `RequestSnapshot` model + snapshot store functions

**Files:**
- Modify: `app/apps/models.py` (add model after `UsageRecord`, ~line 177)
- Modify: `app/apps/store.py` (add functions; extend the `models` import at line 15-18)
- Test: `tests/test_request_snapshots.py` (create)

**Interfaces:**
- Produces:
  - `RequestSnapshot` ORM model (`request_snapshots` table).
  - `async record_snapshot(session, *, request_id, application_id, summary_json: dict, full_json: dict) -> RequestSnapshot`
  - `async get_snapshot_full(session, request_id: str) -> dict | None`
  - `async list_snapshot_summaries(session, *, application_id: str | None = None, limit: int = 100) -> list[dict]`
  - `async all_snapshot_summaries(session, *, limit: int = 500) -> list[dict]`
  - `async prune_snapshots(session, *, capacity: int = 500) -> None`

- [ ] **Step 1: Write the failing test**

Create `tests/test_request_snapshots.py`:

```python
import pytest
from app.apps.db import Database
from app.apps import store


def _summary(rid: str, app: str = "acme", **over) -> dict:
    d = {"id": rid, "tenant_id": app, "sensitivity": "public", "onprem_only": False,
         "model": "m", "model_display": "m", "tier": "cheap", "provider": "onprem",
         "total_usd": 0.01, "baseline_usd": 0.1, "routing_saved_usd": 0.09,
         "compression_saved_usd": 0.0, "latency_ms": 12.0, "hops": 1,
         "detected_entities": [], "created_seq": 0, "total_tokens": 5,
         "quality_sum": 0.0, "quality": None, "quality_source": None, "session_id": None}
    d.update(over)
    return d


@pytest.mark.asyncio
async def test_record_get_and_list_newest_first():
    db = Database("sqlite+aiosqlite://")  # in-memory
    async with db.session() as s:
        await store.record_snapshot(s, request_id="r1", application_id="acme",
                                    summary_json=_summary("r1"), full_json={"id": "r1", "trace": []})
        await store.record_snapshot(s, request_id="r2", application_id="beta",
                                    summary_json=_summary("r2", app="beta"), full_json={"id": "r2", "trace": []})
        await s.commit()
    async with db.session() as s:
        rows = await store.list_snapshot_summaries(s, limit=100)
        assert [r["id"] for r in rows] == ["r2", "r1"]              # newest (ts desc) first
        acme = await store.list_snapshot_summaries(s, application_id="acme", limit=100)
        assert [r["id"] for r in acme] == ["r1"]
        full = await store.get_snapshot_full(s, "r2")
        assert full == {"id": "r2", "trace": []}
        assert await store.get_snapshot_full(s, "missing") is None


@pytest.mark.asyncio
async def test_prune_keeps_newest_capacity():
    db = Database("sqlite+aiosqlite://")
    async with db.session() as s:
        for i in range(7):
            await store.record_snapshot(s, request_id=f"r{i}", application_id="acme",
                                        summary_json=_summary(f"r{i}"), full_json={"id": f"r{i}"})
        await s.commit()
    async with db.session() as s:
        await store.prune_snapshots(s, capacity=3)
        await s.commit()
    async with db.session() as s:
        rows = await store.list_snapshot_summaries(s, limit=100)
        assert len(rows) == 3
        assert [r["id"] for r in rows] == ["r6", "r5", "r4"]
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_request_snapshots.py -v`
Expected: FAIL (`AttributeError: module 'app.apps.store' has no attribute 'record_snapshot'`).

- [ ] **Step 3: Add the model**

In `app/apps/models.py`, after the `UsageRecord` class:

```python
class RequestSnapshot(Base):
    """Durable, redaction-safe snapshot of one served request for the operator
    console (list/detail/stats/fleet + live stream rebuild). Replaces the
    process-local in-memory HistoryStore so the console is coherent across API
    replicas (B3). A bounded UI cache — UsageRecord remains the permanent
    metering record. `full_json` holds ONLY the already-redacted resp dict
    (raw prompt/completion popped before the record path; see CLAUDE.md rule 2)."""

    __tablename__ = "request_snapshots"

    request_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    application_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("applications.id", ondelete="CASCADE"), index=True)
    ts: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, index=True)
    summary_json: Mapped[dict] = mapped_column(JSON)
    full_json: Mapped[dict] = mapped_column(JSON)
```

- [ ] **Step 4: Add the store functions**

In `app/apps/store.py`, add `RequestSnapshot` to the `from app.apps.models import (...)` block, then add near the usage functions (after `list_usage`):

```python
async def record_snapshot(session: AsyncSession, *, request_id: str,
                          application_id: str, summary_json: dict,
                          full_json: dict) -> RequestSnapshot:
    """Upsert-by-write one request snapshot (caller commits). Newer writes for
    the same request_id replace the row (merge on PK)."""
    row = RequestSnapshot(
        request_id=request_id, application_id=application_id,
        summary_json=summary_json, full_json=full_json)
    await session.merge(row)
    return row


async def get_snapshot_full(session: AsyncSession, request_id: str) -> dict | None:
    row = await session.get(RequestSnapshot, request_id)
    return row.full_json if row is not None else None


async def list_snapshot_summaries(session: AsyncSession, *,
                                  application_id: str | None = None,
                                  limit: int = 100) -> list[dict]:
    """Newest-first (ts desc) summary dicts, optionally scoped to one app."""
    if limit <= 0:
        return []
    stmt = (select(RequestSnapshot.summary_json)
            .order_by(RequestSnapshot.ts.desc(), RequestSnapshot.request_id.desc())
            .limit(limit))
    if application_id is not None:
        stmt = stmt.where(RequestSnapshot.application_id == application_id)
    return list((await session.execute(stmt)).scalars().all())


async def all_snapshot_summaries(session: AsyncSession, *,
                                 limit: int = 500) -> list[dict]:
    """The current bounded snapshot set as summary dicts, for stats/fleet."""
    return await list_snapshot_summaries(session, limit=limit)


async def prune_snapshots(session: AsyncSession, *, capacity: int = 500) -> None:
    """Keep only the newest `capacity` snapshots (bounded UI cache). Ordering by
    (ts desc, request_id desc) matches the read path; under identical timestamps
    the tie-break keeps prune and list consistent."""
    keep = (select(RequestSnapshot.request_id)
            .order_by(RequestSnapshot.ts.desc(), RequestSnapshot.request_id.desc())
            .limit(capacity)).scalar_subquery()
    await session.execute(
        delete(RequestSnapshot).where(RequestSnapshot.request_id.not_in(keep)))
```

(`delete` and `select` are already imported at `store.py:11`.)

- [ ] **Step 5: Run to verify it passes**

Run: `uv run pytest tests/test_request_snapshots.py -v`
Expected: PASS (both tests).

- [ ] **Step 6: Commit**

```bash
git add app/apps/models.py app/apps/store.py tests/test_request_snapshots.py
git commit -m "feat(b3): request_snapshots table + store fns (durable UI cache)"
```

---

### Task 2: Extract `summarize()` + pure aggregation functions

**Files:**
- Modify: `app/api/history.py` (extract module-level `summarize`, `stats_over`, `fleet_over`; keep `HistoryStore` delegating to them)
- Test: `tests/test_history_aggregation.py` (create)

**Interfaces:**
- Consumes: `RequestSummary` (unchanged), `_LOCAL_PROVIDERS` (unchanged).
- Produces:
  - `summarize(resp: dict, tenant_id: str, tier: str, provider: str = "?") -> RequestSummary`
  - `stats_over(summaries: list[RequestSummary]) -> dict`
  - `fleet_over(summaries: list[RequestSummary]) -> dict` *(returns `list[dict]`, same shape as `HistoryStore.fleet`)*

**Note:** `summarize` is `HistoryStore.record` minus the deque/dict maintenance and the `self._seq` increment. Set `created_seq=0` (ordering is now `ts`-driven in the DB). `HistoryStore.record`/`stats`/`fleet` must be rewritten to call these so the existing `tests/test_history_store.py` still passes unchanged.

- [ ] **Step 1: Write the failing test**

Create `tests/test_history_aggregation.py`:

```python
from app.api.history import summarize, stats_over, fleet_over, RequestSummary


def _resp(rid, model="cheap-1", usd=0.01, base=0.10):
    return {"id": rid, "model": model, "model_display": model,
            "policy": {"sensitivity": "public", "onprem_only": False, "detected_entities": []},
            "usage": {"total_tokens": 10},
            "cost": {"total_usd": usd, "baseline_usd": base,
                     "routing_saved_usd": base - usd, "compression_saved_usd": 0.0},
            "quality": {"sum": 0.0, "chosen": None, "source": None},
            "trace": [{"step": "router", "latency_ms": 5.0},
                      {"step": "verify", "latency_ms": 2.0}]}


def test_summarize_maps_fields_and_counts_one_hop():
    s = summarize(_resp("r1"), tenant_id="acme", tier="cheap", provider="onprem")
    assert isinstance(s, RequestSummary)
    assert s.id == "r1" and s.tenant_id == "acme" and s.tier == "cheap"
    assert s.provider == "onprem" and s.latency_ms == 7.0    # 5 + 2
    assert s.hops == 1                                        # only the "router" record
    assert s.routing_saved_usd == pytest.approx(0.09)
    assert s.created_seq == 0


def test_stats_and_fleet_over_summaries():
    ss = [summarize(_resp("r1"), "acme", "cheap", "onprem"),
          summarize(_resp("r2", usd=0.5, base=0.5), "beta", "frontier", "azure")]
    st = stats_over(ss)
    assert st["count"] == 2
    assert st["tier_distribution"] == {"cheap": 1, "frontier": 1}
    fl = fleet_over(ss)
    assert {r["app"] for r in fl} == {"acme", "beta"}
```

Add `import pytest` at the top.

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_history_aggregation.py -v`
Expected: FAIL (`ImportError: cannot import name 'summarize'`).

- [ ] **Step 3: Refactor `history.py`**

Move the body of `HistoryStore.record` (the `cost/policy/usage/quality/trace/latency/hops` computation and `RequestSummary(...)` construction) into a module-level `summarize(resp, tenant_id, tier, provider="?")` returning the `RequestSummary` with `created_seq=0`. Move the bodies of `stats()` and `fleet()` into module-level `stats_over(summaries)` and `fleet_over(summaries)` taking a `list[RequestSummary]`. Then:

```python
class HistoryStore:
    ...
    def record(self, resp, tenant_id, tier, provider="?"):
        if len(self._summaries) == self._capacity and self._summaries:
            oldest = self._summaries[0]
            self._full.pop(oldest.id, None)
            self._tenant.pop(oldest.id, None)
        self._seq += 1
        summary = summarize(resp, tenant_id, tier, provider)
        summary.created_seq = self._seq        # local ring keeps its own seq
        self._summaries.append(summary)
        self._full[summary.id] = resp
        self._tenant[summary.id] = tenant_id
        return summary

    def stats(self) -> dict:
        return stats_over(list(self._summaries))

    def fleet(self) -> list[dict]:
        return fleet_over(list(self._summaries))
```

Keep `list`/`get`/`get_tenant` as they are.

- [ ] **Step 4: Run both test files**

Run: `uv run pytest tests/test_history_aggregation.py tests/test_history_store.py -v`
Expected: PASS (new file) and PASS (existing behavior preserved).

- [ ] **Step 5: Commit**

```bash
git add app/api/history.py tests/test_history_aggregation.py
git commit -m "refactor(b3): extract summarize/stats_over/fleet_over pure fns"
```

---

### Task 3: `PgDecisionBridge` (NOTIFY + LISTEN + rebuild, inert on SQLite)

**Files:**
- Create: `app/api/pg_bridge.py`
- Test: `tests/test_pg_bridge.py` (create)

**Interfaces:**
- Consumes: `DecisionBus` (`app/api/bus.py`), `Database` (`app/apps/db.py`), `store.get_snapshot_full` (Task 1).
- Produces `PgDecisionBridge`:
  - `__init__(self, db, bus, *, replica_id: str, channel: str = _DECISION_CHANNEL)`
  - `property active: bool` — True iff the DB URL is postgres.
  - `async publish_remote(self, request_id: str) -> None` — NOTIFY other replicas (no-op if inert).
  - `async ensure_listening(self) -> None` — idempotent; start the LISTEN task (no-op if inert).
  - `async stop(self) -> None` — close the listener connection.
  - `rebuild_events(summary_json: dict, full_json: dict) -> list[dict]` — **staticmethod**, pure; the event sequence the serve path emits.

**Note:** `asyncpg` is imported lazily inside `ensure_listening`/`publish_remote`, never at module top. `rebuild_events` and `active` need no Postgres and are the unit-tested surface; live LISTEN/NOTIFY is deferred to the deploy smoke test.

- [ ] **Step 1: Write the failing test**

Create `tests/test_pg_bridge.py`:

```python
from types import SimpleNamespace

import pytest
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


@pytest.mark.asyncio
async def test_inert_publish_and_listen_are_noops():
    db = Database("sqlite+aiosqlite://")
    b = PgDecisionBridge(db, DecisionBus(), replica_id="x")
    await b.publish_remote("r1")      # must not raise, must not import asyncpg
    await b.ensure_listening()
    await b.stop()
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_pg_bridge.py -v`
Expected: FAIL (`ModuleNotFoundError: No module named 'app.api.pg_bridge'`).

- [ ] **Step 3: Implement the bridge**

Create `app/api/pg_bridge.py`:

```python
"""Cross-replica bridge for the live decision stream (B3).

The local `DecisionBus` only reaches SSE subscribers on THIS API pod. With 2+
pods behind the Kubernetes Service, a request served by pod A must also surface
in a browser streaming from pod B. This bridge carries that fan-out over
Postgres LISTEN/NOTIFY: on publish, pod A issues `NOTIFY <channel> '{rid,origin}'`
(tiny payload — no event bodies, well under the 8 KB cap); every pod LISTENs,
loads the snapshot for `rid`, rebuilds the event sequence, and republishes to
its OWN local bus. The originating pod skips its own notifications (it already
emitted locally on the serve path).

Inert on SQLite (tests/offline dev only): `asyncpg` is imported lazily so the
suite runs without the `postgres` extra, and every method is a no-op. Every real
deploy is Postgres, so the bridge is active exactly when multiple pods exist.
Fail-open: callers wrap `publish_remote` in the serve path's serving guard.
"""
from __future__ import annotations

import asyncio
import json
import logging

from app.apps import store

_log = logging.getLogger("serving.obs")


class PgDecisionBridge:
    def __init__(self, db, bus, *, replica_id: str,
                 channel: str = "route_decisions") -> None:
        self._db = db
        self._bus = bus
        self._replica_id = replica_id
        self._channel = channel
        self._conn = None                       # dedicated asyncpg LISTEN connection
        self._started = False
        self._lock = asyncio.Lock()

    @property
    def active(self) -> bool:
        return self._db.engine.url.drivername.startswith("postgresql")

    @staticmethod
    def rebuild_events(summary_json: dict, full_json: dict) -> list[dict]:
        """The exact event sequence the serve path emits locally: one 'request'
        event then one 'step' per trace record."""
        events: list[dict] = [{"type": "request", "summary": summary_json}]
        for index, record in enumerate(full_json.get("trace") or []):
            events.append({"type": "step", "request_id": full_json.get("id"),
                           "record": record, "index": index})
        return events

    async def publish_remote(self, request_id: str) -> None:
        if not self.active:
            return
        from sqlalchemy import text
        payload = json.dumps({"rid": request_id, "origin": self._replica_id})
        # NOTIFY is transactional — it fires on COMMIT, so we must commit.
        async with self._db.session() as s:
            await s.execute(text("SELECT pg_notify(:c, :p)"),
                            {"c": self._channel, "p": payload})
            await s.commit()

    async def ensure_listening(self) -> None:
        if not self.active or self._started:
            return
        async with self._lock:
            if self._started:
                return
            import asyncpg
            url = self._db.engine.url
            self._conn = await asyncpg.connect(
                host=url.host, port=url.port, user=url.username,
                password=url.password, database=url.database)
            await self._conn.add_listener(self._channel, self._on_notify)
            self._started = True

    def _on_notify(self, _conn, _pid, _channel, payload: str) -> None:
        # asyncpg calls this synchronously from its reader; schedule async work.
        try:
            data = json.loads(payload)
        except (ValueError, TypeError):
            return
        if data.get("origin") == self._replica_id:
            return                              # our own request — already emitted locally
        rid = data.get("rid")
        if rid:
            asyncio.get_event_loop().create_task(self._deliver(rid))

    async def _deliver(self, request_id: str) -> None:
        try:
            from app.apps.models import RequestSnapshot
            async with self._db.session() as s:
                snap = await s.get(RequestSnapshot, request_id)
            if snap is None:                       # evicted/never written — nothing to fan out
                return
            for evt in self.rebuild_events(snap.summary_json, snap.full_json):
                await self._bus.publish(evt)
        except Exception:                          # bridge must never crash the listener
            _log.exception("pg_bridge deliver failed (swallowed)")

    async def stop(self) -> None:
        if self._conn is not None:
            try:
                await self._conn.close()
            finally:
                self._conn = None
                self._started = False
```

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest tests/test_pg_bridge.py -v`
Expected: PASS (all four tests). The SQLite tests must not import `asyncpg`.

- [ ] **Step 5: Commit**

```bash
git add app/api/pg_bridge.py tests/test_pg_bridge.py
git commit -m "feat(b3): PgDecisionBridge (LISTEN/NOTIFY cross-replica stream, inert on sqlite)"
```

---

### Task 4: Wire serve path, read endpoints, and app startup

**Files:**
- Modify: `app/api/routes.py` (serve-path record block, ~line 276 and ~319-334)
- Modify: `app/api/ui_routes.py` (4 read endpoints + `/api/stream`)
- Modify: `app/main.py` (construct `app.state.bridge`, replica_id; shutdown hook; drop `app.state.history` use)
- Test: `tests/test_b3_console_shared_state.py` (create)

**Interfaces:**
- Consumes: everything from Tasks 1–3.
- Produces: `app.state.bridge` (PgDecisionBridge); read endpoints backed by `request_snapshots`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_b3_console_shared_state.py` (uses the `client` + `admin_cookie` fixtures from `conftest.py`; follow the auth pattern already used in `tests/test_ui_routes.py`):

```python
def _auth(client):
    r = client.post("/admin/login", json={"email": "admin@routeforge.local",
                                           "password": "changeme"})
    assert r.status_code == 200


def _chat(client, token, content):
    return client.post("/v1/chat/completions", headers={"Authorization": f"Bearer {token}"},
                       json={"model": "auto", "messages": [{"role": "user", "content": content}]})


def test_requests_and_stats_come_from_db(client, seeded_token):
    # two served requests are both visible via the DB-backed console endpoints
    assert _chat(client, seeded_token, "capital of France?").status_code == 200
    assert _chat(client, seeded_token, "capital of Spain?").status_code == 200
    _auth(client)
    reqs = client.get("/api/requests?limit=100").json()
    assert len(reqs) >= 2
    stats = client.get("/api/stats").json()
    assert stats["count"] >= 2
    rid = reqs[0]["id"]
    full = client.get(f"/api/requests/{rid}").json()
    assert full["id"] == rid and "trace" in full


def test_full_json_has_no_raw_pii(client, seeded_token):
    _chat(client, seeded_token, "My SSN is 123-45-6789, is that valid?")
    _auth(client)
    reqs = client.get("/api/requests?limit=10").json()
    rid = reqs[0]["id"]
    full = client.get(f"/api/requests/{rid}").json()
    blob = str(full)
    assert "123-45-6789" not in blob
    assert "sanitized_prompt" not in full and "raw_completion" not in full


def test_stream_still_connects_single_replica(client):
    _auth(client)
    with client.stream("GET", "/api/stream") as r:
        assert r.status_code == 200        # bridge inert on sqlite; local bus serves
```

> Implementer note: reuse whatever seeded-application/token fixture `tests/test_ui_routes.py` or `tests/test_capture_io.py` already use for a working `/v1` bearer token; add a `seeded_token` fixture to `conftest.py` only if none exists. Do not invent an auth flow.

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_b3_console_shared_state.py -v`
Expected: FAIL (`/api/requests` still reads the in-memory store, or endpoints empty on a fresh replica).

- [ ] **Step 3: Rewire the serve path** (`app/api/routes.py`)

Replace the `history.record(...)` block (~271-279) so it builds the summary via `summarize` and writes a snapshot, and update the stream block (~319-334) to also NOTIFY. Keep BOTH inside their existing `try/except ... swallowed` guards:

```python
# ~271: build the redaction-safe summary (no longer via app.state.history)
from app.api.history import summarize            # top-of-file import
...
    provider = next((c.provider for c in pool if c.name == resp["model"]), "?")
    summary = summarize(resp, tenant, tier, provider)
```

Add a snapshot write next to the existing durable `record_usage` block (inside the same `try` guard, after `record_usage` + commit, or its own guarded block):

```python
try:
    async with request.app.state.db.session() as session:
        await store.record_snapshot(
            session, request_id=resp["id"], application_id=tenant,
            summary_json=summary.model_dump(), full_json=resp)
        await store.prune_snapshots(session, capacity=SNAPSHOT_CAPACITY)
        await session.commit()
except Exception:   # snapshot cache must never break serving
    logging.getLogger("serving.obs").exception("snapshot write failed (swallowed)")
```

In the existing stream block, after the local `bus.publish(...)` loop, add:

```python
        await request.app.state.bridge.publish_remote(resp["id"])
```

`SNAPSHOT_CAPACITY` is a module constant: `SNAPSHOT_CAPACITY = int(os.environ.get("SP_SNAPSHOT_CAPACITY", "500"))`.

- [ ] **Step 4: Rewire the read endpoints** (`app/api/ui_routes.py`)

```python
@router.get("/requests")
async def list_requests(request: Request, limit: int = 100, app: str | None = None) -> list:
    async with request.app.state.db.session() as s:
        return await store.list_snapshot_summaries(s, application_id=app, limit=limit)


@router.get("/requests/{request_id}")
async def get_request(request_id: str, request: Request) -> dict:
    async with request.app.state.db.session() as s:
        resp = await store.get_snapshot_full(s, request_id)
    if resp is None:
        raise HTTPException(404, f"request {request_id!r} not found")
    return resp


@router.get("/stats")
async def get_stats(request: Request) -> dict:
    from app.api.history import RequestSummary, stats_over
    async with request.app.state.db.session() as s:
        rows = await store.all_snapshot_summaries(s, limit=SNAPSHOT_CAPACITY)
    return stats_over([RequestSummary(**r) for r in rows])


@router.get("/fleet")
async def fleet(request: Request) -> list:
    from app.api.history import RequestSummary, fleet_over
    async with request.app.state.db.session() as s:
        rows = await store.all_snapshot_summaries(s, limit=SNAPSHOT_CAPACITY)
    return fleet_over([RequestSummary(**r) for r in rows])
```

Update the `admin_feedback` route's tenant lookup: it currently uses `history.get_tenant`. Replace with the snapshot's `application_id` — load the `RequestSnapshot` row (`await s.get(RequestSnapshot, request_id)`) and read `.application_id`. In `/stream`, call `await request.app.state.bridge.ensure_listening()` right before `bus.subscribe()`. Add `SNAPSHOT_CAPACITY` constant (or import it from `routes.py`).

- [ ] **Step 5: Wire startup** (`app/main.py`)

```python
import uuid
from app.api.pg_bridge import PgDecisionBridge
...
    app.state.bus = DecisionBus()
    app.state.replica_id = uuid.uuid4().hex
    app.state.bridge = PgDecisionBridge(
        app.state.db, app.state.bus, replica_id=app.state.replica_id)

    @app.on_event("shutdown")
    async def _stop_bridge() -> None:
        await app.state.bridge.stop()
```

Remove the `app.state.history = HistoryStore(...)` line (and the `HistoryStore` import if now unused). Confirm no other module reads `app.state.history` (grep) — the serve path and `/api/*` no longer do after Steps 3–4.

- [ ] **Step 6: Run the full suite**

Run: `uv run pytest tests/test_b3_console_shared_state.py tests/test_ui_routes.py tests/test_decision_stream.py tests/test_history_store.py -v`
Expected: PASS. Then run the whole suite: `uv run pytest -q` — no regressions.

- [ ] **Step 7: Commit**

```bash
git add app/api/routes.py app/api/ui_routes.py app/main.py tests/test_b3_console_shared_state.py
git commit -m "feat(b3): console reads + live stream from shared Postgres across replicas"
```

---

## Self-Review

- **Spec coverage:** `request_snapshots` table + reads (Task 1, 4) ✓; LISTEN/NOTIFY bridge + rebuild + inert-on-sqlite (Task 3) ✓; serve-path snapshot write + NOTIFY inside guards (Task 4) ✓; retention/prune (Task 1, 4) ✓; redaction assertion (Task 4) ✓; aggregation reuse (Task 2, 4) ✓; `ts`-ordering + `created_seq=0` (Task 2) ✓; no new config keys (Global Constraints, module constants) ✓.
- **Type consistency:** `summarize`/`stats_over`/`fleet_over` signatures match between Task 2 (definition) and Task 4 (use); `record_snapshot`/`get_snapshot_full`/`list_snapshot_summaries`/`all_snapshot_summaries`/`prune_snapshots` match between Task 1 and Tasks 3–4; `PgDecisionBridge.rebuild_events` is the single source of the event shape shared with the serve path.
- **Deferred (documented):** live multi-pod LISTEN/NOTIFY verification → deploy-time smoke (unit-tested rebuild + payload here). `created_seq` retained for response-shape compat; verify the UI doesn't order by it before any later removal.
