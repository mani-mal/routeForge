# Cluster D — Durable Dashboards + Burst Mitigations Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Move the console dashboards off process-local memory onto a durable, restart-surviving, replica-coherent DB store, and bound multi-step agent concurrency so a burst can't saturate the single backend.

**Architecture:** A new bounded `request_history` table (create_all-friendly, zero ALTER) stores each request's redacted response blob plus denormalized summary columns. A new async `app/apps/history_store.py` module writes/reads it; the four `/api/*` dashboard endpoints read from the DB (percentiles computed in Python for sqlite/Postgres parity); the write folds into the existing usage-record DB session in `routes.py`. The in-memory `HistoryStore` is deleted. The live SSE `DecisionBus` stays process-local (documented single-replica constraint). B2: a process-wide `asyncio.Semaphore` (config-driven) caps concurrent agent runs.

**Tech Stack:** Python 3.12, FastAPI, Pydantic v2, async SQLAlchemy (aiosqlite dev / Postgres prod), pytest.

## Global Constraints

- **CLAUDE.md rule 2 (redact on egress):** `request_history.redacted_resp` stores the SAME already-redacted `resp` dict the in-memory `_full` holds today — types-only `detected_entities`, no raw PII, no CCR contents. No new raw-PII surface.
- **CLAUDE.md rule 4 (pure router):** no inference deploy/scale. B2 is request-admission control + docs only.
- **CLAUDE.md rule 6 (CCR local):** no CCR contents enter `request_history`.
- **Savings split preserved:** `routing_saved_usd` and `compression_saved_usd` stay separate columns AND separate aggregates in stats/fleet.
- **Single-owner config lint:** `history.retention_rows` is owned only by `serving.yaml`; `agent_budgets.max_concurrent_runs` only by `policy.yaml`. No cross-file duplication.
- **No migrations (memory `routeforge-schema-no-migrations`):** `create_all` creates the new *table* automatically; never rely on ALTER. Do not add columns to `usage_records`.
- **ORM style:** use the `Mapped[...] = mapped_column(...)` style already used in `app/apps/models.py`.
- **Store style:** module-level `async def f(session: AsyncSession, ...)`; **caller commits** (mirrors `app/apps/store.py`).
- **`UsageRecord` is untouched** — it stays the lean, unbounded metering/budget ledger.
- **rtk/test gotchas:** avoid `2>&1` and `npm run` in commands; the full-suite aiosqlite teardown flake (`test_langfuse_provision`, `test_traffic_*`) is not a real failure — confirm suspicious failures in isolation.

---

### Task 1: `RequestHistory` ORM model

**Files:**
- Modify: `app/apps/models.py` (add the model near `UsageRecord`)
- Test: `tests/test_request_history_model.py`

**Interfaces:**
- Produces: `class RequestHistory(Base)` with columns: `seq: Mapped[int]` (PK, autoincrement), `request_id: Mapped[str]` (unique, index), `application_id: Mapped[str]` (index), `created_at: Mapped[dt.datetime]`, and denormalized columns `tenant_id, sensitivity, onprem_only, model, model_display, tier, provider, total_usd, baseline_usd, routing_saved_usd, compression_saved_usd, latency_ms, hops, total_tokens, quality_sum, quality, quality_source, session_id, detected_entities (Text JSON), redacted_resp (Text JSON)`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_request_history_model.py
import json
import pytest

from app.apps.db import Database
from app.apps.models import RequestHistory


@pytest.mark.asyncio
async def test_create_all_makes_table_and_row_roundtrips():
    db = Database(url="sqlite+aiosqlite://")  # in-memory; create_all runs on first session
    async with db.session() as s:
        s.add(RequestHistory(
            request_id="req-1", application_id="acme", created_at=None,
            tenant_id="acme", sensitivity="public", onprem_only=False,
            model="local-8b", model_display="Local 8B", tier="cheap", provider="onprem",
            total_usd=0.001, baseline_usd=0.004, routing_saved_usd=0.003,
            compression_saved_usd=0.0, latency_ms=12.0, hops=1, total_tokens=20,
            quality_sum=0.8, quality=0.8, quality_source="calibrated", session_id=None,
            detected_entities="[]", redacted_resp=json.dumps({"id": "req-1"})))
        await s.commit()
    async with db.session() as s:
        from sqlalchemy import select
        row = (await s.execute(select(RequestHistory))).scalars().one()
        assert row.seq >= 1                       # autoincrement populated
        assert row.request_id == "req-1"
        assert json.loads(row.redacted_resp)["id"] == "req-1"
    await db.dispose()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_request_history_model.py -v`
Expected: FAIL with `ImportError: cannot import name 'RequestHistory'`.

- [ ] **Step 3: Add the model**

In `app/apps/models.py`, after `class UsageRecord(Base): ...`, add (the module already imports `Boolean, DateTime, Float, Integer, String, Text` and defines `utcnow`):

```python
class RequestHistory(Base):
    """Durable, bounded per-request record backing the console dashboards
    (list/stats/fleet) and the drilldown. A NEW table -> create_all, no
    migration (see routeforge-schema-no-migrations). Distinct from
    UsageRecord: that is the lean, unbounded metering/budget ledger; this is
    the rich, pruned dashboard store. `redacted_resp` is the SAME
    already-redacted pipeline response the old in-memory HistoryStore held —
    types-only detected_entities, no raw PII, no CCR contents (CLAUDE.md 2 & 6).
    `seq` is a monotonic autoincrement used for ordering AND as the prune key."""

    __tablename__ = "request_history"

    seq: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    request_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    application_id: Mapped[str] = mapped_column(String(64), index=True)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow)

    # Denormalized summary columns (cheap list/stats/fleet; mirror RequestSummary).
    tenant_id: Mapped[str] = mapped_column(String(64))
    sensitivity: Mapped[str] = mapped_column(String(20), default="?")
    onprem_only: Mapped[bool] = mapped_column(Boolean, default=False)
    model: Mapped[str] = mapped_column(String(100), default="?")
    model_display: Mapped[str] = mapped_column(String(200), default="?")
    tier: Mapped[str] = mapped_column(String(20), default="?")
    provider: Mapped[str] = mapped_column(String(50), default="?")
    total_usd: Mapped[float] = mapped_column(Float, default=0.0)
    baseline_usd: Mapped[float] = mapped_column(Float, default=0.0)
    routing_saved_usd: Mapped[float] = mapped_column(Float, default=0.0)
    compression_saved_usd: Mapped[float] = mapped_column(Float, default=0.0)
    latency_ms: Mapped[float] = mapped_column(Float, default=0.0)
    hops: Mapped[int] = mapped_column(Integer, default=0)
    total_tokens: Mapped[int] = mapped_column(Integer, default=0)
    quality_sum: Mapped[float] = mapped_column(Float, default=0.0)
    quality: Mapped[float | None] = mapped_column(Float, nullable=True)
    quality_source: Mapped[str | None] = mapped_column(String(20), nullable=True)
    session_id: Mapped[str | None] = mapped_column(String(128), index=True, nullable=True)

    detected_entities: Mapped[str] = mapped_column(Text, default="[]")  # JSON array of TYPE strings
    redacted_resp: Mapped[str] = mapped_column(Text)                     # JSON blob (drilldown)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_request_history_model.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/apps/models.py tests/test_request_history_model.py
git commit -m "feat(history): add durable request_history ORM table"
```

---

### Task 2: History retention config knob + B2 concurrency key

**Files:**
- Modify: `app/config/schema.py` (add `HistoryCfg`, add `history` field to `ServingCfg`)
- Modify: `app/config/loader.py` (wire `history` from `serving.yaml`)
- Modify: `config/serving.yaml` (add `history` block)
- Modify: `config/policy.yaml` (add `agent_budgets.max_concurrent_runs`)
- Test: `tests/test_history_config.py`

**Interfaces:**
- Produces: `settings.serving.history.retention_rows: int` (default 5000);
  `settings.policy.agent_budgets["max_concurrent_runs"]` present (float 8.0).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_history_config.py
import os
from app.config.loader import load_settings


def test_history_retention_default_and_lint_green():
    s = load_settings()  # loads config/*.yaml + runs no exception (lint passes at construction)
    assert s.serving.history.retention_rows == 5000
    assert int(s.policy.agent_budgets["max_concurrent_runs"]) == 8
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_history_config.py -v`
Expected: FAIL with `AttributeError: 'ServingCfg' object has no attribute 'history'`.

- [ ] **Step 3: Add schema, loader, and YAML**

In `app/config/schema.py`, add before `class ServingCfg`:

```python
class HistoryCfg(BaseModel):
    # Bounded dashboard/drilldown store retention. UsageRecord (metering ledger)
    # is the UNBOUNDED durable record; this caps only request_history rows.
    # Owned here (serving.yaml = infra). Pruned on write by app/apps/history_store.
    retention_rows: int = 5000
```

Add the field to `ServingCfg` (defaulted so configs omitting the block still validate):

```python
class ServingCfg(BaseModel):
    providers: dict[str, ProviderCfg]
    pii_detection: str
    reject_stream_requests: bool
    context_optimization: ContextOptimizationCfg
    observability: ObservabilityCfg
    history: HistoryCfg = HistoryCfg()
```

In `app/config/loader.py`, import `HistoryCfg` alongside the other schema imports, and add to the `ServingCfg(...)` construction (after `observability=observability,`):

```python
        history=HistoryCfg(**serving_raw.get("history", {})),
```

In `config/serving.yaml`, add a top-level block (e.g. after the `observability:` block):

```yaml
# Bounded dashboard/drilldown store (request_history). UsageRecord is the
# unbounded metering ledger; this caps only the rich per-request dashboard rows,
# pruned oldest-first on write. Infra concern -> owned here.
history:
  retention_rows: 5000
```

In `config/policy.yaml`, add to the `agent_budgets:` block:

```yaml
agent_budgets:
  max_steps: 5
  max_spend_usd: 0.05
  per_call_timeout_s: 10
  max_concurrent_runs: 8   # B2: cap concurrent multi-step runs so a burst can't
                           # saturate the single onboarded backend (admission control,
                           # not inference scaling — that's the operator's job, rule 4)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_history_config.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/config/schema.py app/config/loader.py config/serving.yaml config/policy.yaml tests/test_history_config.py
git commit -m "feat(config): history.retention_rows + agent_budgets.max_concurrent_runs"
```

---

### Task 3: `history_store.py` — model relocation, `summarize`, `record_history`, `get_history`

**Files:**
- Create: `app/apps/history_store.py`
- Modify: `app/api/history.py` (re-import `RequestSummary`/`_LOCAL_PROVIDERS` from the new module — single source; `HistoryStore` keeps working until Task 5 deletes it)
- Test: `tests/test_history_store_write.py`

**Interfaces:**
- Produces:
  - `RequestSummary(BaseModel)` — the exact fields currently in `app/api/history.py` (moved verbatim, incl. `session_id`).
  - `_LOCAL_PROVIDERS: frozenset[str]` — moved verbatim.
  - `summarize(resp: dict, tenant_id: str, tier: str, provider: str = "?") -> RequestSummary` — pure derivation (no storage); `created_seq=0` placeholder.
  - `async record_history(session, resp, *, tenant_id, tier, provider, retention_rows) -> RequestSummary` — insert one row, flush to get `seq`, set `summary.created_seq = seq`, prune, return the summary.
  - `async get_history(session, request_id: str) -> dict | None` — parsed `redacted_resp` blob.
- Consumes: `RequestHistory` (Task 1); `settings.serving.history.retention_rows` (Task 2, passed in by the caller).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_history_store_write.py
import pytest
from sqlalchemy import func, select

from app.apps.db import Database
from app.apps.models import RequestHistory
from app.apps import history_store as hs


def _resp(rid, model="local-8b", total=0.0, base=0.001, route_saved=0.001,
          comp_saved=0.0, onprem=False, entities=None, session_id=None,
          trace=None):
    return {
        "id": rid, "model": model, "model_display": model,
        "cost": {"total_usd": total, "baseline_usd": base,
                 "routing_saved_usd": route_saved, "compression_saved_usd": comp_saved},
        "policy": {"sensitivity": "public", "onprem_only": onprem,
                   "detected_entities": entities or []},
        "usage": {"total_tokens": 20},
        "quality": {"sum": 0.8, "chosen": 0.8, "source": "calibrated"},
        "trace": trace if trace is not None else [{"step": "router", "latency_ms": 12.0}],
        "session_id": session_id,
    }


def test_summarize_derives_hops_latency_savings_session():
    s = hs.summarize(_resp("a", total=0.001, route_saved=0.003,
                           session_id="wf-1",
                           trace=[{"step": "router", "latency_ms": 5.0},
                                  {"step": "verify", "latency_ms": 2.0},
                                  {"step": "agent_hop", "latency_ms": 7.0}]),
                     "acme", "cheap", "onprem")
    assert s.hops == 2                    # router + agent_hop only, not verify
    assert s.latency_ms == 14.0           # sum of all latency_ms
    assert s.routing_saved_usd == 0.003
    assert s.session_id == "wf-1"


@pytest.mark.asyncio
async def test_record_history_inserts_and_get_roundtrips():
    db = Database(url="sqlite+aiosqlite://")
    async with db.session() as s:
        summary = await hs.record_history(
            s, _resp("req-1", entities=["US_SSN"]), tenant_id="acme",
            tier="cheap", provider="onprem", retention_rows=5000)
        await s.commit()
    assert summary.id == "req-1"
    assert summary.created_seq >= 1
    async with db.session() as s:
        blob = await hs.get_history(s, "req-1")
        assert blob["id"] == "req-1"
        assert await hs.get_history(s, "nope") is None
    await db.dispose()


@pytest.mark.asyncio
async def test_retention_prunes_oldest_by_seq():
    db = Database(url="sqlite+aiosqlite://")
    async with db.session() as s:
        for i in range(5):
            await hs.record_history(s, _resp(f"r{i}"), tenant_id="acme",
                                    tier="cheap", provider="onprem", retention_rows=3)
        await s.commit()
    async with db.session() as s:
        n = (await s.execute(select(func.count()).select_from(RequestHistory))).scalar_one()
        assert n == 3
        ids = [r.request_id for r in
               (await s.execute(select(RequestHistory).order_by(RequestHistory.seq))).scalars()]
        assert ids == ["r2", "r3", "r4"]     # oldest two pruned
    await db.dispose()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_history_store_write.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.apps.history_store'`.

- [ ] **Step 3: Create the module**

Create `app/apps/history_store.py`:

```python
"""Durable, DB-backed request history for the console dashboards (B3).

Replaces the process-local in-memory HistoryStore (app/api/history.py) as the
source for list/stats/fleet/drilldown, so history survives restart and is
coherent across API replicas. Stores each completed request's already-redacted
pipeline response (types-only detected_entities, no raw PII, no CCR contents;
CLAUDE.md 2 & 6) plus denormalized summary columns. Percentiles are computed in
Python (sqlite has no percentile function) for sqlite/Postgres parity.

`RequestSummary` (the list/stream shape) and `_LOCAL_PROVIDERS` live here now;
app/api/history.py re-imports them until it is deleted.
"""
from __future__ import annotations

import json

from pydantic import BaseModel
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.apps.models import RequestHistory

# Providers that keep a request inside the residency boundary. Anything else
# serving an `onprem_only` request is a genuine Rule-3 breach (CLAUDE.md #3).
_LOCAL_PROVIDERS = frozenset({"onprem", "mock", "?", "-", ""})


class RequestSummary(BaseModel):
    id: str
    tenant_id: str
    sensitivity: str
    onprem_only: bool
    model: str
    model_display: str
    tier: str
    provider: str = "?"
    total_usd: float
    baseline_usd: float
    routing_saved_usd: float
    compression_saved_usd: float
    latency_ms: float
    hops: int
    detected_entities: list[str]
    created_seq: int
    total_tokens: int = 0
    quality_sum: float = 0.0
    quality: float | None = None
    quality_source: str | None = None
    session_id: str | None = None


def summarize(resp: dict, tenant_id: str, tier: str,
              provider: str = "?") -> RequestSummary:
    """Pure derivation of a RequestSummary from a redacted pipeline response.
    `created_seq` is 0 here — record_history sets the real value from the DB
    row seq. Used directly for the live bus publish (created_seq irrelevant to
    the SSE animation; the DB-backed list carries the authoritative seq)."""
    cost = resp.get("cost", {})
    policy = resp.get("policy", {})
    usage = resp.get("usage", {})
    quality = resp.get("quality", {})
    trace = resp.get("trace") or []
    latency_ms = sum(
        float(r.get("latency_ms", 0.0)) for r in trace
        if r.get("latency_ms") is not None)
    hops = sum(1 for r in trace if r.get("step") in ("router", "agent_hop"))
    return RequestSummary(
        id=resp["id"],
        tenant_id=tenant_id,
        sensitivity=policy.get("sensitivity", "?"),
        onprem_only=bool(policy.get("onprem_only", False)),
        model=resp.get("model", "?"),
        model_display=resp.get("model_display", resp.get("model", "?")),
        tier=tier,
        provider=provider,
        total_usd=float(cost.get("total_usd", 0.0)),
        baseline_usd=float(cost.get("baseline_usd", 0.0)),
        routing_saved_usd=float(cost.get("routing_saved_usd", 0.0)),
        compression_saved_usd=float(cost.get("compression_saved_usd", 0.0)),
        latency_ms=latency_ms,
        hops=hops,
        detected_entities=list(policy.get("detected_entities", [])),
        created_seq=0,
        total_tokens=int(usage.get("total_tokens", 0)),
        quality_sum=float(quality.get("sum", 0.0)),
        quality=quality.get("chosen"),
        quality_source=quality.get("source"),
        session_id=resp.get("session_id"),
    )


async def record_history(session: AsyncSession, resp: dict, *, tenant_id: str,
                         tier: str, provider: str = "?",
                         retention_rows: int = 5000) -> RequestSummary:
    """Persist one request_history row (caller commits) and prune to
    `retention_rows` newest by seq. Returns the summary with created_seq set to
    the real DB row seq (for the live bus publish)."""
    s = summarize(resp, tenant_id, tier, provider)
    row = RequestHistory(
        request_id=s.id, application_id=tenant_id, tenant_id=tenant_id,
        sensitivity=s.sensitivity, onprem_only=s.onprem_only, model=s.model,
        model_display=s.model_display, tier=s.tier, provider=s.provider,
        total_usd=s.total_usd, baseline_usd=s.baseline_usd,
        routing_saved_usd=s.routing_saved_usd,
        compression_saved_usd=s.compression_saved_usd, latency_ms=s.latency_ms,
        hops=s.hops, total_tokens=s.total_tokens, quality_sum=s.quality_sum,
        quality=s.quality, quality_source=s.quality_source, session_id=s.session_id,
        detected_entities=json.dumps(s.detected_entities),
        redacted_resp=json.dumps(resp))
    session.add(row)
    await session.flush()          # populate row.seq
    s.created_seq = row.seq
    # Prune: single indexed DELETE, no COUNT, portable (sqlite + Postgres).
    threshold = row.seq - retention_rows
    if threshold > 0:
        await session.execute(
            delete(RequestHistory).where(RequestHistory.seq <= threshold))
    return s


async def get_history(session: AsyncSession, request_id: str) -> dict | None:
    """The redacted response blob for the drilldown, or None if absent/pruned."""
    row = await session.get(RequestHistory, {"request_id": request_id}) \
        if False else None
    from sqlalchemy import select
    row = (await session.execute(
        select(RequestHistory).where(RequestHistory.request_id == request_id)
    )).scalars().first()
    if row is None:
        return None
    return json.loads(row.redacted_resp)
```

> Note: `RequestHistory` PK is `seq`, not `request_id`, so `get_history` selects by the unique `request_id` column (the `session.get(...)` line above is dead — delete it; shown only to flag the pitfall). Implementer: write `get_history` as the plain `select(...).where(request_id == ...)` form.

Then edit `app/api/history.py` to source the shared names from the new module instead of redefining them — replace the local `class RequestSummary(...)` definition and the `_LOCAL_PROVIDERS = frozenset(...)` line with:

```python
from app.apps.history_store import RequestSummary, _LOCAL_PROVIDERS  # noqa: F401
```

(Keep the `HistoryStore` class as-is for now; it still references `RequestSummary`/`_LOCAL_PROVIDERS`, which now resolve to the imported names. Task 5 deletes this file.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_history_store_write.py tests/test_history_store.py -v`
Expected: PASS (new write tests pass; the existing in-memory `test_history_store.py` still passes via the re-import).

- [ ] **Step 5: Commit**

```bash
git add app/apps/history_store.py app/api/history.py tests/test_history_store_write.py
git commit -m "feat(history): DB-backed summarize/record_history/get_history + prune"
```

---

### Task 4: `history_store` reads — `list_history`, `stats`, `fleet`

**Files:**
- Modify: `app/apps/history_store.py` (add the three read functions)
- Test: `tests/test_history_store_read.py`

**Interfaces:**
- Produces:
  - `async list_history(session, *, limit: int = 100, app: str | None = None) -> list[RequestSummary]` — newest first (`seq desc`); `app` filters `tenant_id`; `limit <= 0` → `[]`; each summary's `created_seq` = the row's `seq`.
  - `async stats(session) -> dict` — same keys the old `HistoryStore.stats()` returned (`count, total_usd, baseline_usd, routing_saved_usd, compression_saved_usd, onprem_count, tier_distribution, latency_p50_ms, latency_p95_ms, token_intelligence_density, cost_intelligence_density`), percentiles in Python.
  - `async fleet(session) -> list[dict]` — one row per `tenant_id`, sorted by `count` desc, same keys as the old `HistoryStore.fleet()` incl. `onprem_violations`, `tier_distribution`, TID/CID, `latency_p50_ms`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_history_store_read.py
import pytest

from app.apps.db import Database
from app.apps import history_store as hs
from tests.test_history_store_write import _resp   # reuse the builder


async def _seed(db, rows):
    async with db.session() as s:
        for r in rows:
            await hs.record_history(s, r, tenant_id=r["policy"].get("_tenant", "acme"),
                                    tier=r.pop("_tier", "cheap"),
                                    provider=r.pop("_provider", "onprem"),
                                    retention_rows=5000)
        await s.commit()


@pytest.mark.asyncio
async def test_list_newest_first_and_app_filter_and_limit():
    db = Database(url="sqlite+aiosqlite://")
    async with db.session() as s:
        await hs.record_history(s, _resp("a1"), tenant_id="acme", tier="cheap", provider="onprem", retention_rows=5000)
        await hs.record_history(s, _resp("h1"), tenant_id="hospital", tier="mid", provider="onprem", retention_rows=5000)
        await hs.record_history(s, _resp("a2"), tenant_id="acme", tier="cheap", provider="onprem", retention_rows=5000)
        await s.commit()
    async with db.session() as s:
        assert [x.id for x in await hs.list_history(s)] == ["a2", "h1", "a1"]  # newest first
        assert [x.id for x in await hs.list_history(s, app="acme")] == ["a2", "a1"]
        assert await hs.list_history(s, limit=0) == []
        assert (await hs.list_history(s))[0].created_seq >= 1
    await db.dispose()


@pytest.mark.asyncio
async def test_stats_empty_and_aggregate():
    db = Database(url="sqlite+aiosqlite://")
    async with db.session() as s:
        assert (await hs.stats(s))["count"] == 0            # empty-safe
        await hs.record_history(s, _resp("a", total=0.001, base=0.002, route_saved=0.001,
                                         onprem=True), tenant_id="acme", tier="cheap",
                                provider="onprem", retention_rows=5000)
        await hs.record_history(s, _resp("b", total=0.003, base=0.004, route_saved=0.001,
                                         comp_saved=0.002), tenant_id="acme", tier="frontier",
                                provider="onprem", retention_rows=5000)
        await s.commit()
    async with db.session() as s:
        st = await hs.stats(s)
        assert st["count"] == 2
        assert st["onprem_count"] == 1
        assert st["tier_distribution"] == {"cheap": 1, "frontier": 1}
        assert st["compression_saved_usd"] == pytest.approx(0.002)
        assert st["latency_p50_ms"] > 0
    await db.dispose()


@pytest.mark.asyncio
async def test_fleet_groups_and_flags_onprem_violation():
    db = Database(url="sqlite+aiosqlite://")
    async with db.session() as s:
        # onprem_only request served OFF-prem (azure) -> a genuine breach.
        await hs.record_history(s, _resp("v", onprem=True), tenant_id="hospital",
                                tier="frontier", provider="azure", retention_rows=5000)
        await hs.record_history(s, _resp("ok", onprem=True), tenant_id="hospital",
                                tier="cheap", provider="onprem", retention_rows=5000)
        await s.commit()
    async with db.session() as s:
        rows = await hs.fleet(s)
        row = next(r for r in rows if r["app"] == "hospital")
        assert row["count"] == 2
        assert row["onprem_violations"] == 1        # azure-served onprem_only
    await db.dispose()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_history_store_read.py -v`
Expected: FAIL with `AttributeError: module 'app.apps.history_store' has no attribute 'list_history'`.

- [ ] **Step 3: Add the read functions**

Append to `app/apps/history_store.py` (add `import statistics` and `from collections import defaultdict` and `from sqlalchemy import select` at the top with the other imports):

```python
def _pctile(sorted_vals: list[float], pct: float) -> float:
    if not sorted_vals:
        return 0.0
    idx = min(len(sorted_vals) - 1, int(round(pct * (len(sorted_vals) - 1))))
    return sorted_vals[idx]


def _to_summary(row: RequestHistory) -> RequestSummary:
    return RequestSummary(
        id=row.request_id, tenant_id=row.tenant_id, sensitivity=row.sensitivity,
        onprem_only=row.onprem_only, model=row.model, model_display=row.model_display,
        tier=row.tier, provider=row.provider, total_usd=row.total_usd,
        baseline_usd=row.baseline_usd, routing_saved_usd=row.routing_saved_usd,
        compression_saved_usd=row.compression_saved_usd, latency_ms=row.latency_ms,
        hops=row.hops, detected_entities=json.loads(row.detected_entities or "[]"),
        created_seq=row.seq, total_tokens=row.total_tokens, quality_sum=row.quality_sum,
        quality=row.quality, quality_source=row.quality_source, session_id=row.session_id)


async def list_history(session: AsyncSession, *, limit: int = 100,
                       app: str | None = None) -> list[RequestSummary]:
    """Newest first (seq desc). `app` filters tenant_id; limit<=0 -> []."""
    if limit <= 0:
        return []
    stmt = select(RequestHistory).order_by(RequestHistory.seq.desc()).limit(limit)
    if app is not None:
        stmt = stmt.where(RequestHistory.tenant_id == app)
    return [_to_summary(r) for r in (await session.execute(stmt)).scalars()]


async def _all_summaries(session: AsyncSession) -> list[RequestSummary]:
    # Bounded table (retention_rows) -> a full scan here is bounded by construction.
    stmt = select(RequestHistory).order_by(RequestHistory.seq.desc())
    return [_to_summary(r) for r in (await session.execute(stmt)).scalars()]


async def stats(session: AsyncSession) -> dict:
    rows = await _all_summaries(session)
    count = len(rows)
    if count == 0:
        return {"count": 0, "total_usd": 0.0, "baseline_usd": 0.0,
                "routing_saved_usd": 0.0, "compression_saved_usd": 0.0,
                "onprem_count": 0, "tier_distribution": {}, "latency_p50_ms": 0.0,
                "latency_p95_ms": 0.0, "token_intelligence_density": 0.0,
                "cost_intelligence_density": 0.0}
    tier_distribution: dict[str, int] = {}
    for s in rows:
        tier_distribution[s.tier] = tier_distribution.get(s.tier, 0) + 1
    latencies = sorted(s.latency_ms for s in rows)
    q_sum = sum(s.quality_sum for s in rows)
    tok_sum = sum(s.total_tokens for s in rows)
    usd_sum = sum(s.total_usd for s in rows)
    return {
        "count": count,
        "total_usd": sum(s.total_usd for s in rows),
        "baseline_usd": sum(s.baseline_usd for s in rows),
        "routing_saved_usd": sum(s.routing_saved_usd for s in rows),
        "compression_saved_usd": sum(s.compression_saved_usd for s in rows),
        "onprem_count": sum(1 for s in rows if s.onprem_only),
        "tier_distribution": tier_distribution,
        "latency_p50_ms": _pctile(latencies, 0.50),
        "latency_p95_ms": _pctile(latencies, 0.95),
        "token_intelligence_density": (q_sum / tok_sum * 1000.0) if tok_sum > 0 else 0.0,
        "cost_intelligence_density": (q_sum / usd_sum) if usd_sum > 0 else 0.0,
    }


async def fleet(session: AsyncSession) -> list[dict]:
    rows = await _all_summaries(session)
    groups: dict[str, list[RequestSummary]] = defaultdict(list)
    for s in rows:
        groups[s.tenant_id].append(s)
    out = []
    for app, items in groups.items():
        lat = sorted(s.latency_ms for s in items)
        tiers: dict[str, int] = defaultdict(int)
        for s in items:
            tiers[s.tier] += 1
        tok = sum(s.total_tokens for s in items)
        usd = sum(s.total_usd for s in items)
        qsum = sum(s.quality_sum for s in items)
        out.append({
            "app": app,
            "count": len(items),
            "total_usd": usd,
            "baseline_usd": sum(s.baseline_usd for s in items),
            "routing_saved_usd": sum(s.routing_saved_usd for s in items),
            "compression_saved_usd": sum(s.compression_saved_usd for s in items),
            "onprem_count": sum(1 for s in items if s.onprem_only),
            "pii_count": sum(1 for s in items if s.detected_entities),
            "onprem_violations": sum(
                1 for s in items
                if s.onprem_only and s.provider not in _LOCAL_PROVIDERS),
            "latency_p50_ms": statistics.median(lat) if lat else 0.0,
            "tier_distribution": dict(tiers),
            "token_intelligence_density": (qsum / tok * 1000.0) if tok > 0 else 0.0,
            "cost_intelligence_density": (qsum / usd) if usd > 0 else 0.0,
        })
    out.sort(key=lambda r: r["count"], reverse=True)
    return out
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_history_store_read.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/apps/history_store.py tests/test_history_store_read.py
git commit -m "feat(history): DB-backed list_history/stats/fleet (Python percentiles)"
```

---

### Task 5: Rewire write + endpoint reads; delete the in-memory store

**Files:**
- Modify: `app/api/routes.py` (fold `record_history` into the usage DB session; build the bus summary from its return)
- Modify: `app/api/ui_routes.py` (make the four endpoints async DB reads)
- Modify: `app/main.py` (drop `HistoryStore` import + `app.state.history`)
- Delete: `app/api/history.py`
- Modify: `tests/test_history_store.py` (drop the deleted in-memory tests; the DB store is covered by Tasks 3–4) — replace its body with a one-line skip or delete the file
- Test: existing `tests/test_ui_routes.py` (now traverses the DB end-to-end) + new `tests/test_history_persist_restart.py`

**Interfaces:**
- Consumes: `history_store.record_history/list_history/stats/fleet/get_history` (Tasks 3–4); `settings.serving.history.retention_rows` (Task 2).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_history_persist_restart.py
import pytest

from app.apps.db import Database
from app.apps import history_store as hs
from tests.test_history_store_write import _resp


@pytest.mark.asyncio
async def test_rows_survive_new_store_handle_same_db(tmp_path):
    url = f"sqlite+aiosqlite:///{tmp_path/'h.db'}"
    db1 = Database(url=url)
    async with db1.session() as s:
        await hs.record_history(s, _resp("keep", session_id="wf-9"), tenant_id="acme",
                                tier="cheap", provider="onprem", retention_rows=5000)
        await s.commit()
    await db1.dispose()
    # Fresh handle on the SAME file -> data persisted (restart durability).
    db2 = Database(url=url)
    async with db2.session() as s:
        listed = await hs.list_history(s)
        assert [x.id for x in listed] == ["keep"]
        assert listed[0].session_id == "wf-9"     # B17 grouping works from DB
        assert (await hs.get_history(s, "keep"))["id"] == "keep"
    await db2.dispose()
```

- [ ] **Step 2: Run test to verify it fails/passes appropriately**

Run: `uv run pytest tests/test_history_persist_restart.py -v`
Expected: PASS already (store exists) — this pins durability. Then proceed to rewire the app so `test_ui_routes.py` exercises the DB path.

- [ ] **Step 3: Rewire `routes.py`**

In `app/api/routes.py`, replace the in-memory record block (currently around lines 199–216, the `summary = request.app.state.history.record(...)` block) so `summary` starts `None` and is produced by the DB write. Specifically:

Remove the standalone `summary = ...history.record(...)` try/except block. In the existing usage-record DB block (the `async with request.app.state.db.session() as session:` around lines 232–252), after `await store.record_usage(...)` and before `await session.commit()`, add:

```python
            from app.apps import history_store
            summary = await history_store.record_history(
                session, resp, tenant_id=tenant, tier=tier, provider=provider,
                retention_rows=s.serving.history.retention_rows)
```

Keep `summary = None` initialized before the try (so a DB failure leaves `summary` None and the bus block below safely skips — unchanged guard semantics). Ensure `tier`/`provider` computed in that block (already are, lines 223–224) are reused. The bus block (`if summary is not None: ... bus.publish({"type": "request", "summary": summary.model_dump()})`) is unchanged and now publishes the DB-derived summary (with real `created_seq`).

Remove the now-unused `provider`/`tier` computation in the deleted block and the `getattr(...history...)` reference.

- [ ] **Step 4: Rewire `ui_routes.py` endpoints to async DB reads**

Replace the four handlers:

```python
from app.apps import history_store


@router.get("/requests")
async def list_requests(request: Request, limit: int = 100, app: str | None = None) -> list:
    async with request.app.state.db.session() as session:
        return await history_store.list_history(session, limit=limit, app=app)


@router.get("/requests/{request_id}")
async def get_request(request_id: str, request: Request) -> dict:
    async with request.app.state.db.session() as session:
        resp = await history_store.get_history(session, request_id)
    if resp is None:
        raise HTTPException(404, f"request {request_id!r} not found")
    return resp


@router.get("/stats")
async def get_stats(request: Request) -> dict:
    async with request.app.state.db.session() as session:
        return await history_store.stats(session)


@router.get("/fleet")
async def fleet(request: Request) -> list:
    async with request.app.state.db.session() as session:
        return await history_store.fleet(session)
```

- [ ] **Step 5: Drop the in-memory store from `main.py` and delete `history.py`**

In `app/main.py`: remove `from app.api.history import HistoryStore` (line 13) and the `app.state.history = HistoryStore(capacity=500)` line plus its comment (lines 129–132). Then:

```bash
git rm app/api/history.py
```

Replace `tests/test_history_store.py` (it imported the deleted `HistoryStore`): delete it — its coverage moved to `test_history_store_write.py` / `test_history_store_read.py`.

```bash
git rm tests/test_history_store.py
```

Fix the stale docstring reference in `app/api/ui_routes.py` (the module docstring line mentioning `HistoryStore on app.state.history`) to name the DB store. Update the `bus.py` doc-comment reference if it names the module path (comment-only).

- [ ] **Step 6: Run the affected suites**

Run: `uv run pytest tests/test_ui_routes.py tests/test_history_persist_restart.py tests/test_token_auth.py -v`
Expected: PASS (`test_ui_routes.py` now writes on POST and reads the four endpoints from the DB, incl. drilldown blob + 404 + no-raw-SSN).

Guard: `grep -rn "state.history" app/` returns nothing; `grep -rn "app.api.history" app/ tests/` returns nothing.

- [ ] **Step 7: Commit**

```bash
git add app/api/routes.py app/api/ui_routes.py app/main.py tests/test_history_persist_restart.py
git rm app/api/history.py tests/test_history_store.py
git commit -m "feat(history): dashboards read the durable store; delete in-memory HistoryStore"
```

---

### Task 6: B2 — bound concurrent agent runs with a config-driven semaphore

**Files:**
- Modify: `app/main.py` (create `app.state.agent_run_gate`; pass into `Pipeline`)
- Modify: `app/pipeline.py` (accept `run_gate`; thread it to `agent_run`)
- Modify: `app/agent/runtime.py` (accept `run_gate`; wrap the NAT session in `async with run_gate`)
- Test: `tests/test_agent_run_gate.py`

**Interfaces:**
- Produces: `app.state.agent_run_gate: asyncio.Semaphore` sized from
  `int(settings.policy.agent_budgets.get("max_concurrent_runs", 8))`.
- Consumes: `Pipeline(..., run_gate=...)` → `agent_run(ctx, req, *, port, run_gate)`.
- `run_gate=None` means unbounded (direct construction / tests) — no behavior change.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_agent_run_gate.py
import asyncio
import pytest


@pytest.mark.asyncio
async def test_semaphore_bounds_concurrent_sections():
    """The gate wrapper caps concurrency: with Semaphore(2), at most 2 of the
    guarded sections run at once. Mirrors how runtime.run wraps the NAT session."""
    gate = asyncio.Semaphore(2)
    active = 0
    peak = 0

    async def guarded():
        nonlocal active, peak
        async with gate:          # the exact construct runtime.run uses
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(0.02)
            active -= 1

    await asyncio.gather(*(guarded() for _ in range(6)))
    assert peak <= 2


def test_gate_size_from_config():
    from app.config.loader import load_settings
    s = load_settings()
    n = int(s.policy.agent_budgets.get("max_concurrent_runs", 8))
    assert n == 8
```

- [ ] **Step 2: Run test to verify it fails/passes**

Run: `uv run pytest tests/test_agent_run_gate.py -v`
Expected: PASS (these pin the mechanism/config). Proceed to wire the real path so it's actually enforced.

- [ ] **Step 3: Wire the semaphore**

In `app/main.py`, after `self_port = _self_port()` and before/near the `Pipeline(...)` construction, add:

```python
    # B2: cap concurrent multi-step agent runs so a traffic burst can't pile
    # unbounded long-lived inferences onto the single onboarded backend. This is
    # request-admission control, NOT inference scaling (rule 4 — operators scale
    # their own serving). On 3.12 asyncio.Semaphore binds to the loop on first
    # acquire, so constructing it here (outside the serving loop) is safe.
    _max_runs = int(settings.policy.agent_budgets.get("max_concurrent_runs", 8))
    app.state.agent_run_gate = asyncio.Semaphore(_max_runs)
```

Pass it into the `Pipeline(...)` call:

```python
    app.state.pipeline = Pipeline(
        settings, gate, detector, gateway, self_port=self_port,
        co_cfg_provider=lambda: app.state.co_cfg, ccr=ccr, registry=registry,
        run_gate=app.state.agent_run_gate)
```

In `app/pipeline.py` `Pipeline.__init__`, add a keyword param and store it:

```python
    def __init__(self, settings: Settings, gate: Gate, detector: Detector,
                 gateway: Gateway, self_port: int,
                 co_cfg_provider: Callable[[], ContextOptimizationCfg],
                 ccr: CCRStore, registry: "ModelRegistry | None" = None,
                 run_gate: "asyncio.Semaphore | None" = None) -> None:
        ...
        self._run_gate = run_gate
```

(Add `import asyncio` to `app/pipeline.py` if not already imported — used only for the type annotation; a string annotation avoids a hard requirement, but the import is harmless.)

In `_run_multi_step`, thread it into the agent call:

```python
        result = await agent_run(ctx, sanitized_request, port=self._self_port,
                                 run_gate=self._run_gate)
```

In `app/agent/runtime.py` `run(...)`, add the keyword param and wrap the NAT execution. Change the signature:

```python
async def run(ctx: ExecutionPolicyContext, request: ChatRequest, *, port: int,
              run_gate: "asyncio.Semaphore | None" = None) -> AgentResult:
```

Add `import asyncio` and a null-safe async context. Introduce a tiny helper at module scope:

```python
import contextlib

def _gate(run_gate):
    """The concurrency gate as an async context; a no-op when unbounded (None)."""
    return run_gate if run_gate is not None else contextlib.nullcontext()
```

Then wrap the workflow execution (the `async with WorkflowBuilder... / session ... / runner` block) so the gate is held for the duration of the run:

```python
        async with _gate(run_gate):
            async with WorkflowBuilder.from_config(config=config) as builder:
                session_manager = await SessionManager.create(config=config, shared_builder=builder)
                try:
                    async with session_manager.session() as session:
                        async with session.run(user_text) as runner:
                            text = await runner.result(to_type=str)
                finally:
                    await session_manager.shutdown()
```

The outer `HOPS.start(...)` / `finally: HOPS.pop_all(...)` stay exactly as they are — only the NAT execution is gated (acquire around the actual inference work, released before `pop_all`).

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_agent_run_gate.py tests/test_agent_runtime.py tests/test_agent_hop_compression.py -v`
Expected: PASS (existing agent tests unaffected — default gate size 8; `None` unbounded for any direct construction).

- [ ] **Step 5: Commit**

```bash
git add app/main.py app/pipeline.py app/agent/runtime.py tests/test_agent_run_gate.py
git commit -m "feat(agent): bound concurrent agent runs via config-driven semaphore (B2)"
```

---

### Task 7: Docs — scaling boundary + backlog status

**Files:**
- Create: `docs/architecture/dashboards-and-scaling.md`
- Modify: `docs/bugs-backlog.md` (mark B3 pragmatic-slice done + B2 mitigations done / operator-owned levers)

**Interfaces:** none (documentation).

- [ ] **Step 1: Write the scaling doc**

Create `docs/architecture/dashboards-and-scaling.md` covering:
- **Durable, replica-coherent dashboards.** list/stats/fleet/drilldown read the shared `request_history` DB table; history survives restart and is identical across API replicas. `UsageRecord` remains the unbounded metering ledger; `request_history` is bounded by `serving.history.retention_rows`.
- **The live SSE animation stays per-pod.** `DecisionBus` is in-process; with >1 API replica each pod's `/api/stream` streams only the requests it served. The DB-backed `/api/requests` list is the backfill; cross-replica live streaming needs shared pub/sub (Redis/NATS) and is deferred (avoids a new infra dependency for air-gapped installs; the Redis in `deploy/compose/langfuse.yml` belongs to Langfuse only).
- **API-tier throughput is gated on operator serving capacity (B2↔B3).** Scaling the API tier only helps if the operator scaled their vLLM to match (rule 4). B3 unblocks dashboard correctness under >1 replica; it does not by itself raise inference throughput.
- **B2 mitigations.** `agent_budgets.max_concurrent_runs` bounds concurrent agent runs (admission control). Operator-owned levers: raise `agent_budgets.per_call_timeout_s`; scale/replicate vLLM (their serving-plane job, out of scope); trimming the `multi_step` persona share is test tooling.

- [ ] **Step 2: Update the backlog**

In `docs/bugs-backlog.md`, update the B3 and B2 entries: B3 — dashboards now durable/replica-coherent via `request_history`; live bus stays per-pod (documented); cross-replica live streaming deferred. B2 — concurrent-run cap shipped; vLLM scaling + timeout are operator-owned. Point both at `docs/architecture/dashboards-and-scaling.md`.

- [ ] **Step 3: Commit**

```bash
git add docs/architecture/dashboards-and-scaling.md docs/bugs-backlog.md
git commit -m "docs(cluster-d): dashboards-and-scaling boundary; B2/B3 backlog status"
```

---

## Self-Review

- **Spec coverage:** new table (§4→T1), history_store write/prune/get (§5→T3), reads with Python percentiles (§5→T4), write-path fold + endpoint rewrite + in-memory deletion (§6/§7→T5), single-replica docs (§8→T7), B2 semaphore + config (§9/§10→T2,T6), UsageRecord untouched (constraint), savings split preserved (T3/T4 columns + aggregates). All covered.
- **Placeholder scan:** none — every step carries real code. One deliberate pitfall call-out in T3 Step 3 (the dead `session.get(...)` line) is flagged with the correct form to write.
- **Type consistency:** `RequestSummary` fields identical across T3 (definition), T4 (`_to_summary`), and the old model. `record_history`/`list_history`/`stats`/`fleet`/`get_history` signatures match between the interface blocks and call sites (`routes.py`, `ui_routes.py`). `run_gate` param name consistent across `main.py`/`pipeline.py`/`runtime.py`. `retention_rows` sourced from `settings.serving.history.retention_rows` everywhere.
- **Ordering:** model → config → write/prune → reads → rewire/delete → semaphore → docs. Each task's tree stays green (T3 keeps `history.py` working via re-import; T5 deletes it only after endpoints move to the DB).
