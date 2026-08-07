# Cluster D — Durable Dashboards + Burst Mitigations — Design

**Date:** 2026-08-07
**Backlog items:** B3 (in-memory dashboard state blocks horizontal API scaling) + B2 (multi-step burst saturates a single vLLM)
**Base branch:** `feat/infra-management-config` (B17 / PR #9 merged in — Cluster D builds on `session_id`)

---

## 1. Problem

Two backlog items, addressed together because they share the same root cause class —
process-local state that assumes exactly one API replica:

- **B3 — dashboards live in process memory.** `app/api/history.py`'s `HistoryStore`
  is a `deque(maxlen=500)` of summaries plus a parallel `dict[id, full_resp]`. The
  console's list / stats / fleet / drilldown views read it directly
  (`app/api/ui_routes.py`). Consequences: (a) **history evaporates on restart**;
  (b) **more than one API replica splits the dashboard** — each pod shows only the
  requests it happened to serve; (c) the 500-entry ring silently drops older history.
  The durable `UsageRecord` table (`app/apps/models.py`) exists but is a **lossy**
  metering row — it has no savings split, no detected-entities, no hops, no
  `model_display`, no `session_id`, no redacted response blob — so it cannot back the
  dashboards faithfully.

- **B2 — a multi-step burst saturates the single backend.** A traffic burst of
  multi-step (agent) requests issues many concurrent, long-lived inference calls with
  no ceiling, overwhelming the one vLLM the operator has onboarded. Requests queue,
  time out, and mislabel.

## 2. Scope — "pragmatic slice"

This is deliberately **not** a full shared-store rearchitecture, and **not**
B2-mitigations-only. The slice:

1. Point the dashboards at a **durable, queryable store** that survives restart and is
   coherent across replicas (new `request_history` table).
2. Close the drilldown + `session_id` gaps by carrying the redacted response blob and
   the B17 correlation id in that store.
3. Keep the live **SSE decision bus process-local** and **document the single-replica
   constraint honestly** rather than building cross-replica pub/sub now.
4. Add B2's **RouteForge-owned** mitigations: bound concurrent agent runs, and document
   the operator-owned levers (timeout, vLLM scaling).

### Explicitly out of scope (by design, not immaturity)

- **Cross-replica live streaming.** The live flow-visualizer animation stays per-pod.
  True multi-replica live streaming needs a shared pub/sub (Redis/NATS); deferred and
  documented.
- **Scaling / replicating vLLM.** RouteForge is a pure router (CLAUDE.md rule 4). It
  never stands up or scales inference. Operators scale their own serving plane.
- **Altering `UsageRecord`.** It stays as-is — the lean, permanent metering/budget
  ledger. `request_history` is a separate, bounded, rich dashboard/drilldown store.
- **Redis for RouteForge's own state.** The Redis in `deploy/compose/langfuse.yml`
  belongs to Langfuse only. On-prem/air-gapped deployments get a DB-backed store, not a
  new infra dependency.

## 3. Two durable per-request tables — distinct concerns

| Table | Purpose | Growth | Contents |
|---|---|---|---|
| `usage_records` (existing, unchanged) | Permanent metering / budget ledger; feeds `day_spend_usd` quota checks | **Unbounded** (never pruned) | Lean: tokens, cost, latency, status |
| `request_history` (**new**) | Dashboard list/stats/fleet + drilldown | **Bounded** (pruned to `retention_rows`) | Rich: savings split, entities, hops, `session_id`, redacted response blob |

Keeping them separate avoids overloading the budget ledger with pruning semantics and a
fat blob column, and avoids an ALTER on the deployed `usage_records` table.

## 4. The new `request_history` table

New ORM model in `app/apps/models.py`. `Base.metadata.create_all` auto-creates it on
fresh boots — **zero ALTER** (see memory `routeforge-schema-no-migrations`: `create_all`
creates missing *tables* but never ALTERs existing ones, so a brand-new table is
migration-friendly while a new column is not).

```
class RequestHistory(Base):
    __tablename__ = "request_history"

    seq            = Column(Integer, primary_key=True, autoincrement=True)  # monotonic; ordering + prune key
    request_id     = Column(String, unique=True, index=True, nullable=False)
    application_id = Column(String, index=True, nullable=False)             # == tenant slug
    created_at     = Column(DateTime, nullable=False)                       # server-side default utcnow

    # --- denormalized summary columns (cheap list/stats/fleet; mirror RequestSummary) ---
    tenant_id              = Column(String, nullable=False)
    sensitivity            = Column(String, nullable=False)
    onprem_only            = Column(Boolean, nullable=False, default=False)
    model                  = Column(String, nullable=False)
    model_display          = Column(String, nullable=False)
    tier                   = Column(String, nullable=False)
    provider               = Column(String, nullable=False, default="?")
    total_usd              = Column(Float, nullable=False, default=0.0)
    baseline_usd           = Column(Float, nullable=False, default=0.0)
    routing_saved_usd      = Column(Float, nullable=False, default=0.0)
    compression_saved_usd  = Column(Float, nullable=False, default=0.0)
    latency_ms             = Column(Float, nullable=False, default=0.0)
    hops                   = Column(Integer, nullable=False, default=0)
    total_tokens           = Column(Integer, nullable=False, default=0)
    quality_sum            = Column(Float, nullable=False, default=0.0)      # Σ per-call quality — TID/CID numerator
    quality                = Column(Float, nullable=True)                    # chosen model's score (display)
    quality_source         = Column(String, nullable=True)
    session_id             = Column(String, index=True, nullable=True)       # B17 correlation id (opaque)

    detected_entities      = Column(Text, nullable=False, default="[]")      # JSON array of entity TYPE strings
    redacted_resp          = Column(Text, nullable=False)                    # JSON blob of the redacted pipeline resp (drilldown)
```

**Privacy (CLAUDE.md rules 2 & 6):** `redacted_resp` is the **same** already-redacted
`resp` dict the in-memory `_full` holds today — types-only `detected_entities`, no raw
PII, no CCR contents. Storing it durably changes *where* it lives, not *what* it
contains. `session_id` is the opaque B17 label (not redacted, by contract).

**Prune key.** `seq` is a monotonic autoincrement. Retention is enforced by a single
indexed DELETE per write — no `COUNT`, portable across sqlite (dev) and Postgres (prod):

```
DELETE FROM request_history WHERE seq <= (:new_seq - :retention_rows)
```

## 5. Data-access module — `app/apps/history_store.py` (new)

Async, DB-backed functions replacing the in-memory `HistoryStore` reads. They return the
**same shapes** the endpoints return today (so `ui_routes.py` contracts and the UI are
unchanged apart from becoming `async`):

- `async record_history(session, resp, *, tenant_id, tier, provider, retention_rows) -> None`
  — insert one row (columns derived exactly as `HistoryStore.record` derives them today:
  `latency_ms` = Σ trace `latency_ms`; `hops` = count of trace records whose `step` is
  `router` or `agent_hop`; `session_id` = `resp.get("session_id")`), then run the prune
  DELETE. Caller commits.
- `async list_history(session, *, limit, app) -> list[RequestSummary]` — newest first
  (`ORDER BY seq DESC`), optional `app` filter, `limit <= 0 -> []`.
- `async get_history(session, request_id) -> dict | None` — returns the parsed
  `redacted_resp` blob (drilldown), or `None`.
- `async fleet(session) -> list[dict]` — one aggregate row per `application_id`, sorted
  by count desc. Preserves today's fields **including `onprem_violations`** (an
  `onprem_only` request served by a provider not in `_LOCAL_PROVIDERS`) and TID/CID.
- `async stats(session) -> dict` — global aggregate; `latency_p50_ms` / `latency_p95_ms`
  computed **in Python** over the fetched `latency_ms` values (sqlite has no percentile
  function; keeps sqlite + Postgres identical). Reuses the existing `_pctile` logic.

`RequestSummary` (the Pydantic list/stream shape) and `_LOCAL_PROVIDERS` **move** from
`app/api/history.py` into `history_store.py` (or a shared `app/apps/history_model.py`)
so both the DB reads and the bus publish use one definition. A pure
`summarize(resp, tenant_id, tier, provider) -> RequestSummary` helper is extracted for
the bus publish (see §7).

**Aggregation strategy.** `fleet`/`stats` fetch only the needed columns for the bounded
(≤ `retention_rows`) table and aggregate in Python — same math as today, just sourced
from a `SELECT` instead of the deque. At the default cap this is a small scan; the table
is bounded by construction so it never becomes a full-history table-scan.

## 6. Write path — fold into the existing DB-session block

`app/api/routes.py` already opens `async with request.app.state.db.session()` after the
pipeline runs to write the `UsageRecord` (lines ~232–252), guarded by "metering must
never break serving". The `request_history` insert folds into that **same** block and
**same** transaction:

```
async with request.app.state.db.session() as session:
    await store.record_usage(session, ...)                       # existing
    await history_store.record_history(                          # new
        session, resp, tenant_id=tenant, tier=tier, provider=provider,
        retention_rows=s.serving.history.retention_rows)
    await session.commit()
```

Wrapped by the existing try/except so a history-write failure is logged and swallowed —
**history must never break serving** (same guarantee as today's in-memory `record`).

## 7. Read path + bus

- **Endpoints** in `app/api/ui_routes.py` (`/api/requests`, `/api/requests/{id}`,
  `/api/stats`, `/api/fleet`) become `async`, open a DB session, and call the
  `history_store` functions. Return shapes unchanged.
- **Live bus stays process-local.** `app/api/bus.py` `DecisionBus` is untouched.
  `routes.py` still publishes `{"type": "request", "summary": ...}` and per-trace `step`
  events, but the summary now comes from the pure `summarize(...)` helper instead of
  `HistoryStore.record`'s return value. The bus is a *live animation* channel, not a
  store — durable views come from the DB.
- **`app.state.history` (the in-memory `HistoryStore`) is removed** from `main.py`; the
  `HistoryStore` class is deleted. Nothing else reads it after the endpoints move to the
  DB.

## 8. Honest single-replica documentation

New doc `docs/architecture/dashboards-and-scaling.md` (and a pointer from
`docs/bugs-backlog.md`) stating plainly:

- **Durable dashboards are now replica-coherent.** list / stats / fleet / drilldown read
  the shared DB, so every replica shows the same history, and it survives restart.
- **The live SSE animation is per-pod.** With more than one API replica, each pod's
  `/api/stream` only streams the requests that pod served. The `/api/requests` list
  (DB-backed) is the backfill; the live view is best-effort per-pod. Cross-replica live
  streaming needs shared pub/sub — deferred.
- **API-tier throughput is gated on operator serving capacity.** Scaling the API tier
  only helps if the operator has scaled their vLLM to match (rule 4). This is the B2 ↔ B3
  link: B3 unblocks *dashboard* correctness under >1 replica; it does not, by itself,
  raise inference throughput.

## 9. B2 mitigations — RouteForge-owned only

### 9a. Bound concurrent agent runs

A process-wide `asyncio.Semaphore` caps how many multi-step agent runs execute at once,
so a burst can't pile unbounded concurrent long-lived inferences onto the single backend.

- **Config (single-owner):** `config/policy.yaml` `agent_budgets.max_concurrent_runs`
  (default `8`). `agent_budgets` already owns multi-step caps (`max_steps`,
  `per_call_timeout_s`) and is a free `dict[str, float]` in `PolicyCfg`, so no schema
  change is needed — consumed via
  `int(settings.policy.agent_budgets.get("max_concurrent_runs", 8))`.
- **Wiring:** `app/main.py` startup (inside the serving event loop, alongside the other
  `app.state` init) creates `app.state.agent_run_gate = asyncio.Semaphore(n)` and passes
  it into `Pipeline(...)`. The pipeline threads it into
  `agent_run(ctx, req, port=..., run_gate=...)`.
- **Enforcement:** `app/agent/runtime.py:run` wraps the NAT session execution in
  `async with run_gate:` (acquire before building/running the workflow, release after).
  Excess concurrent runs wait for a slot rather than all hitting the backend at once.
  A `None` gate (tests / direct construction) means "unbounded" — no behavior change.
- **Direct (non-agent) requests are unaffected** — only the multi-step path acquires the
  gate.

### 9b. Documented operator levers

`docs/bugs-backlog.md` (B2) and the scaling doc record the levers RouteForge does **not**
own:

- Raise `agent_budgets.per_call_timeout_s` when the backend is slow under load.
- **Scale / replicate vLLM** — the operator's serving-plane job (rule 4, out of scope).
- Trimming the `multi_step` persona share is test tooling, not a product change.

## 10. Config knob for retention

`config/serving.yaml` gains a small infra block (serving.yaml owns infra/observability):

```
history:
  retention_rows: 5000        # bounded dashboard/drilldown store; UsageRecord is the unbounded ledger
```

Schema: add `HistoryCfg(BaseModel)` with `retention_rows: int = 5000` and
`history: HistoryCfg = HistoryCfg()` on `ServingCfg` (defaulted so existing configs that
omit the block still validate). Single-owned → the config lint's "no concern owned by two
files" check stays green.

## 11. Testing

- **Store units** (`tests/`): `record_history` inserts the right columns; retention prune
  keeps exactly `retention_rows` newest rows and drops older by `seq`; `list_history`
  ordering + `app` filter + `limit<=0`; `get_history` returns the blob / `None`;
  `stats`/`fleet` math (savings split, TID/CID, `onprem_violations`, Python p50/p95)
  matches the old in-memory results for the same inputs.
- **Restart durability:** write rows, drop the store handle, re-open a fresh store on the
  same DB → `list_history`/`get_history` still return the persisted rows and blob.
- **Session grouping (B17) from DB:** rows sharing a `session_id` group correctly via the
  DB path.
- **Endpoints:** the four `/api/*` endpoints return DB-sourced data; drilldown 404s on an
  unknown id.
- **B2 concurrency:** with `max_concurrent_runs=2`, a third concurrent agent run waits
  for a slot (assert ≤2 in flight); a `None` gate is unbounded.
- **Config lint stays green** with the new `serving.history` block and the new
  `agent_budgets` key.

## 12. Files touched

- **Create:** `app/apps/history_store.py`, `docs/architecture/dashboards-and-scaling.md`,
  tests for the store + concurrency.
- **Modify:** `app/apps/models.py` (new `RequestHistory`), `app/api/routes.py` (fold in
  history write; summary from `summarize()`), `app/api/ui_routes.py` (async DB reads),
  `app/main.py` (drop `HistoryStore`; add `agent_run_gate`; pass into `Pipeline`),
  `app/pipeline.py` (thread `run_gate` to `agent_run`), `app/agent/runtime.py` (acquire
  gate), `app/config/schema.py` (`HistoryCfg`), `config/serving.yaml` (`history` block),
  `config/policy.yaml` (`agent_budgets.max_concurrent_runs`), `docs/bugs-backlog.md`
  (B2/B3 status).
- **Delete:** `app/api/history.py` `HistoryStore` class (keep/relocate `RequestSummary`
  + `_LOCAL_PROVIDERS` + new `summarize()`).

## 13. Non-negotiable rules honored

- **Rule 2 (redact on egress):** the durable blob is the already-redacted `resp`; no new
  raw-PII surface.
- **Rule 4 (pure router):** no inference deploy/scale; B2 mitigations are request-admission
  control + docs, not serving changes.
- **Rule 6 (CCR stays local):** no CCR contents enter `request_history` (same as today's
  `_full`).
- **Savings split preserved:** `routing_saved_usd` and `compression_saved_usd` remain
  separate columns and separate aggregates.
