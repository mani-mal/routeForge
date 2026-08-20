# B3 — Shared Dashboard State (horizontal API scaling)

**Date:** 2026-08-20
**Backlog item:** B3 (`docs/bugs-backlog.md`)
**Goal:** Make the operator console (list / detail / stats / fleet + live decision
stream) coherent when the API tier runs **2+ replicas** behind a load balancer,
without changing single-node behavior.

---

## Problem

Two structures on `app.state` are **process-local**, so with more than one API
replica each replica only knows about the requests *it* served:

| Structure | File | Backs | Failure with N replicas |
|---|---|---|---|
| `HistoryStore` (ring buffer) | `app/api/history.py` | `GET /api/requests`, `/api/requests/{id}`, `/api/stats`, `/api/fleet` | Dashboard shows a random 1/N slice of traffic |
| `DecisionBus` (asyncio pub/sub) | `app/api/bus.py` | `GET /api/stream` (SSE) | A browser's stream misses every request served by another replica |

This is why the API tier cannot be scaled out — the console fragments. (It is
**not** the fix for B2, which needs vLLM replicas; B3 stands on its own for API-tier
HA/throughput.)

**Key fact:** every deployment runs **Postgres** — single-node compose
(`deploy/compose/docker-compose.yml`) and Helm/PCAI (`deploy/hpe/.../api.yaml`) both
point `DATABASE_URL` at `postgresql+asyncpg://…`. **SQLite is not a deploy target** —
it exists only for the test suite (in-memory `StaticPool`) and offline dev (`db.py`
fallback with no `DATABASE_URL`). A single-node Postgres deploy runs the bridge fully
active (one replica `NOTIFY`s itself and ignores it; the local-bus emit does the work),
so there is no SQLite production path to preserve — only the test/dev path.

## Non-goals

- No new infrastructure component. Postgres is already required for multi-replica;
  Redis is explicitly rejected (new compose/Helm service, new failure mode, extra
  burden on air-gapped customers).
- No change to the durable metering path. `UsageRecord` stays the budget/metering
  source of truth, untouched.
- No stream replay-on-connect (already a documented deferral; `/api/requests`
  backfills).

## Invariants preserved (CLAUDE.md)

- **Rule 2 / redaction:** snapshots persist ONLY the already-redacted resp dict the
  in-memory `HistoryStore` holds today — types-only `detected_entities`, no raw
  prompt/completion (`sanitized_prompt`/`raw_completion` are popped off `resp` at
  `routes.py:198-199`, *before* the record path), no CCR contents. Persisting the
  redacted DecisionRecord trace is exactly what the console is meant to surface.
- **Fail-open telemetry:** the snapshot write, the `NOTIFY`, and the `LISTEN` bridge
  all stay inside the existing "must never break serving" guards. If the DB or bridge
  is down, serving continues and the stream degrades to local-only.

---

## Design

### 1. Reads → `request_snapshots` table

A **new** table (so `create_all` handles it with zero migration — we cannot ALTER
`UsageRecord` cleanly on deployed DBs; see `[[routeforge-schema-no-migrations]]`):

```
request_snapshots
  request_id     String(64)  PRIMARY KEY
  application_id String(64)  FK applications.id ON DELETE CASCADE, index
  ts             DateTime(tz) default utcnow, index          # global ordering key
  summary_json   JSON        # the redacted RequestSummary (list/stats/fleet source)
  full_json      JSON        # the redacted full resp dict (drilldown source)
```

- **Write:** on the serve path, replace the in-memory `history.record(...)` call with
  a snapshot write. The `RequestSummary` construction logic moves out of
  `HistoryStore.record` into a pure `summarize(resp, tenant, tier, provider)` function
  (same latency-sum and hop-count math). `summary_json = summarize(...).model_dump()`,
  `full_json = resp`.
- **Retention:** keep newest `capacity` rows (default 500, preserving the ring-buffer
  feel). Opportunistic prune after insert. Snapshots are a bounded UI cache — bounding
  is about disk, not correctness (`UsageRecord` is the permanent record).
- **Reads:** the 4 endpoints query the table:
  - `/api/requests` → `summary_json` rows, `ts DESC`, optional `app` filter, `limit`.
  - `/api/requests/{id}` → `full_json`, 404 if absent.
  - `/api/stats`, `/api/fleet` → load current `summary_json` rows into `RequestSummary`
    objects and reuse the **existing** aggregation math (extracted from `HistoryStore`
    into pure functions over a `list[RequestSummary]` — do NOT reimplement the
    percentile / TID-CID-density / `onprem_violations` logic).

`HistoryStore` is retired as the read source. Ordering moves from per-process
`created_seq` to `ts DESC` (global across replicas).

### 2. Stream → Postgres `LISTEN/NOTIFY` bridge

`DecisionBus` (local asyncio fan-out) stays exactly as-is. A new
`app/api/pg_bridge.py :: PgDecisionBridge` connects the replicas:

- **Publish (serve path), after the snapshot write:**
  1. emit events to the **local** bus (today's code — immediate, always works locally);
  2. `bridge.publish_remote(request_id)` → `NOTIFY route_decisions, '<payload>'` where
     payload is small JSON `{"rid": request_id, "origin": <replica_id>}` (well under
     the 8 KB NOTIFY cap; no event bodies on the wire).
- **Listener (lazy):** started idempotently when the first `/api/stream` subscriber
  connects (matches the existing lazy `traffic_task` pattern; a replica nobody is
  watching holds no LISTEN connection). Opens a **dedicated** asyncpg connection
  (`add_listener`). On notification:
  - if `origin == my replica_id` → skip (this replica already emitted locally);
  - else → load the snapshot for `rid`, rebuild the event sequence
    (`{"type":"request","summary":summary_json}` then one
    `{"type":"step","request_id",...,"index":i}` per `full_json["trace"]` record) and
    publish to the **local** bus, which fans out to this replica's SSE subscribers.
- Each replica generates `replica_id = uuid4().hex` at startup.

### 3. SQLite-inert branch (tests + offline dev only)

`PgDecisionBridge` detects the backend from the resolved DB URL. On SQLite (tests,
offline dev), `publish_remote` and `ensure_listening` are **no-ops** so the suite and
`uv run` keep working without a live Postgres — the local-bus emit plus the DB-backed
reads are the whole story there. This is **not** a supported deploy mode; every real
deployment (compose and Helm alike) is Postgres, where the bridge is always active.

### Config

- `serving.yaml` (infra): decision-stream channel name (default `route_decisions`) and
  snapshot `capacity` (default 500). No new required keys — sensible defaults so
  existing configs boot unchanged. (Confirm the config lint's one-owner rule — these
  are infra switches, so they live in `serving.yaml` only.)

---

## Components / files

| File | Change |
|---|---|
| `app/apps/models.py` | Add `RequestSnapshot` model |
| `app/apps/store.py` | `record_snapshot`, `get_snapshot_full`, `list_snapshot_summaries`, `all_snapshot_summaries`, `prune_snapshots` |
| `app/api/history.py` | Extract `summarize(...)` + pure aggregation fns (`stats_over`, `fleet_over`) over `list[RequestSummary]`; `HistoryStore` retained only as the home of those pure fns (or removed if fully lifted) |
| `app/api/pg_bridge.py` | **New.** `PgDecisionBridge` (publish_remote / ensure_listening / stop; inert on SQLite) |
| `app/api/routes.py` | Serve path: write snapshot + `publish_remote` inside the existing guards |
| `app/api/ui_routes.py` | 4 read endpoints query the DB; `/api/stream` calls `bridge.ensure_listening()` on subscribe |
| `app/main.py` | Construct `app.state.bridge` (replica_id, db url, bus); wire shutdown to `bridge.stop()` |
| `config/serving.yaml` + settings | channel name + snapshot capacity |
| tests | read-coherence via DB, `full_json` redaction-safety, SQLite degrade (bridge inert), bridge rebuild-from-snapshot |

## Testing

- **Read coherence:** two snapshot writes "from different replicas" (just two DB
  writes) are both visible to `list`/`stats`/`fleet`/`get` through the DB. Aggregation
  output matches the old `HistoryStore` math for the same inputs (golden compare).
- **Redaction safety:** `full_json` for a request that carried PII contains no raw
  entity text and no `sanitized_prompt`/`raw_completion` keys (mirrors the existing
  `HistoryStore` redaction-safety assumption).
- **Degrade:** with a SQLite URL, `PgDecisionBridge` is inert (`publish_remote` /
  `ensure_listening` no-op, no asyncpg import required) and the local bus path is
  unchanged.
- **Bridge rebuild:** given a snapshot row, the listener callback reconstructs the same
  event sequence the serve path emits locally (unit test on the rebuild function, no
  live Postgres needed).
- **Retention:** after `capacity + k` writes, only the newest `capacity` snapshots
  remain; `UsageRecord` count is unaffected.

## Open questions / deferrals

- Live Postgres LISTEN/NOTIFY integration test needs a real PG; unit-test the rebuild
  and NOTIFY-payload shaping, and leave the end-to-end multi-replica check to a
  deploy-time smoke (mirrors how the DGX smoke test already validates the stack).
- `created_seq` becomes per-replica and is no longer a global order key; keep the field
  for response-shape compatibility but drive ordering from `ts`. Verify the UI does not
  rely on `created_seq` for cross-request logic before removing it.
