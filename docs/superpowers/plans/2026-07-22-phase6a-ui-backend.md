# Phase 6a — UI-support API (backend) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Add the read/stream/control API the Phase-6 React UI consumes — a redaction-safe in-memory request history with list/detail/stats endpoints, an SSE decision stream for the live flow visualizer, and a runtime Headroom on/off toggle — without weakening any privacy invariant.

**Architecture:** A process-local ring-buffer `HistoryStore` records each completed request's already-redacted response (types-only trace, no raw PII, no CCR contents). A tiny asyncio pub/sub `DecisionBus` broadcasts each completed request's records to connected SSE clients so the visualizer can animate them. A mutable `app.state.co_cfg` (already the source of truth for the internal endpoint) becomes the single runtime headroom switch; the direct pipeline reads it via a callback instead of a frozen value, so a `POST /api/controls` toggle affects both paths.

**Tech Stack:** FastAPI, Pydantic v2, asyncio, `sse-starlette` (or raw `StreamingResponse` with `text/event-stream`), pytest + Starlette `TestClient`.

## Global Constraints

- **Never store or emit raw PII or CCR contents.** History records only the pipeline's response dict, which is already types-only (`detected_entities` are TYPES; `trace` records carry no values; CCR records have no contents field). Do NOT add any field that could carry originals. (CLAUDE.md rules 2 & 6.)
- **The API must stay green offline.** No new network deps that phone home; SSE is local.
- **Do not change routing/gate/compression behavior.** This phase is additive read/stream/control surface only. The one behavioral seam is making headroom-enabled runtime-mutable — it must default to the config value and change nothing when untouched.
- **`/internal/*` stays loopback-only and out of OpenAPI** (unchanged). New `/api/*` routes are customer-facing read surface; keep them read-only except `/api/controls`.
- CORS: allow the Vite dev origin (`http://localhost:5173`) so `npm run dev` can call the API. In production the UI is served same-origin; CORS is a dev convenience.
- Tiers `cheap|mid|frontier`; providers `onprem|azure|bedrock`; keep those canonical everywhere the UI reads them.

---

### Task 1: `HistoryStore` + list/detail/stats endpoints + CORS

**Files:**
- Create: `app/api/history.py`
- Create: `app/api/ui_routes.py` (the `/api/*` router)
- Modify: `app/api/routes.py` (record each completed request into history)
- Modify: `app/main.py` (construct `HistoryStore` on `app.state`, include `ui_routes`, add CORS)
- Test: `tests/test_history_store.py`, `tests/test_ui_routes.py`

**Interfaces:**
- Produces:
  - `class RequestSummary(BaseModel)`: `id, tenant_id, sensitivity, onprem_only, model, tier, total_usd, baseline_usd, routing_saved_usd, compression_saved_usd, latency_ms, hops, detected_entities: list[str], created_seq: int`.
  - `class HistoryStore`: `__init__(self, capacity: int = 500)`; `record(resp: dict, tenant_id: str) -> RequestSummary`; `list(limit: int = 100) -> list[RequestSummary]` (newest first); `get(request_id: str) -> dict | None` (the full stored response dict); `stats() -> dict`.
  - `stats()` returns: `{"count", "total_usd", "baseline_usd", "routing_saved_usd", "compression_saved_usd", "onprem_count", "tier_distribution": {tier: n}, "latency_p50_ms", "latency_p95_ms"}` — all aggregated from stored summaries; empty-state safe (zeros, empty dict).
  - Endpoints on `ui_routes` (prefix `/api`): `GET /api/requests?limit=` → `list[RequestSummary]`; `GET /api/requests/{id}` → full response dict or 404; `GET /api/stats` → stats dict.
- **`created_seq`** is a monotonic counter the store assigns (do NOT use wall-clock — `time`/`Date` are fine at runtime here, but a sequence is deterministic for tests and ordering). Tie latency from the trace's records (sum of `latency_ms` across step records, or the max hop latency — pick sum of gateway/hop `latency_ms`; document it).

- [ ] **Step 1: Failing test** `tests/test_history_store.py`:

```python
from app.api.history import HistoryStore

def _resp(rid, model="local-8b", tier="cheap", total=0.0, base=0.001,
          onprem=True, sens="public", ents=None, comp_saved=0.0, route_saved=0.001):
    return {
        "id": rid, "model": model,
        "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"},
                     "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        "trace": [{"step": "route", "chosen": model, "reason": "tier " + tier,
                   "latency_ms": 200.0, "tokens_priced": 10}],
        "cost": {"total_usd": total, "baseline_usd": base,
                 "routing_saved_usd": route_saved, "compression_saved_usd": comp_saved},
        "policy": {"sensitivity": sens, "onprem_only": onprem,
                   "allowed_providers": ["onprem"], "detected_entities": ents or []},
    }

def test_record_and_list_newest_first():
    h = HistoryStore(capacity=3)
    h.record(_resp("a"), "acme")
    h.record(_resp("b"), "acme")
    ids = [s.id for s in h.list()]
    assert ids == ["b", "a"]

def test_capacity_evicts_oldest():
    h = HistoryStore(capacity=2)
    for r in ("a", "b", "c"):
        h.record(_resp(r), "acme")
    assert [s.id for s in h.list()] == ["c", "b"]
    assert h.get("a") is None

def test_get_returns_full_response():
    h = HistoryStore()
    h.record(_resp("a", model="local-32b"), "acme")
    assert h.get("a")["model"] == "local-32b"

def test_stats_empty_state_safe():
    s = HistoryStore().stats()
    assert s["count"] == 0 and s["total_usd"] == 0.0 and s["tier_distribution"] == {}

def test_stats_aggregate():
    h = HistoryStore()
    h.record(_resp("a", tier="cheap", onprem=True, base=0.002, route_saved=0.002), "acme")
    h.record(_resp("b", tier="frontier", onprem=False, base=0.004, route_saved=0.001,
                   comp_saved=0.001), "acme")
    s = h.stats()
    assert s["count"] == 2
    assert s["tier_distribution"] == {"cheap": 1, "frontier": 1}
    assert s["onprem_count"] == 1
    assert abs(s["baseline_usd"] - 0.006) < 1e-9
    assert abs(s["compression_saved_usd"] - 0.001) < 1e-9
```

- [ ] **Step 2: Run → FAIL.**

- [ ] **Step 3: Implement `app/api/history.py`.** Use a `collections.deque(maxlen=capacity)` for eviction and a dict `id → full resp` kept in sync (evict from dict when the deque drops an item — simplest: on record, if len==capacity before append, pop the oldest id from the dict). Derive `tier` from the last routing/hop record's reason or a `tier` on the chosen candidate — since the trace records don't carry tier explicitly, resolve tier from the chosen model name via the response's model + a passed-in tier map, OR store `tier` by looking at the trace. SIMPLEST + robust: compute tier by matching `resp["model"]` against `settings.routing.model_pool`. But the store shouldn't depend on Settings. Instead: the caller (`routes.py`) passes the resolved `tier` to `record()`. Change `record(resp, tenant_id, tier)` — resolve tier in routes.py from `app.state.settings.routing.model_pool` by model name (fallback `"?"`). Update the test's `record` calls to pass tier (mirror `_resp`'s tier). Latency = sum of `latency_ms` over trace records that have it. `detected_entities` from `resp["policy"]["detected_entities"]` (already types).

- [ ] **Step 4: Run history tests → PASS.**

- [ ] **Step 5: Implement `app/api/ui_routes.py`** — an `APIRouter()` with the three GET endpoints reading `request.app.state.history`. `GET /api/requests/{id}` → 404 via `HTTPException` when `get` returns None.

- [ ] **Step 6: Wire into `routes.py` + `main.py`.** In `chat_completions`, after `export_trace(...)`, call `request.app.state.history.record(resp, tenant, tier)` where `tier` is resolved from the model pool (guard with try/except — history must never break serving). In `main.py`: `from app.api.history import HistoryStore`; `app.state.history = HistoryStore(capacity=500)`; `app.include_router(ui_routes)`; add CORS:

```python
from fastapi.middleware.cors import CORSMiddleware
app.add_middleware(CORSMiddleware, allow_origins=["http://localhost:5173"],
                   allow_methods=["*"], allow_headers=["*"])
```

- [ ] **Step 7: Failing→passing `tests/test_ui_routes.py`** using `TestClient`: POST a chat request (with `Authorization: Bearer demo-key`), then `GET /api/requests` returns ≥1 summary with no raw PII, `GET /api/requests/{id}` returns the full trace, `GET /api/stats` returns coherent aggregates, and a PII request's summary has `onprem_only=True` with `detected_entities` being TYPES (e.g. contains `"US_SSN"`, never the digits). Assert the raw SSN string is absent from the entire `/api/requests/{id}` body.

- [ ] **Step 8: Full suite** `uv run pytest -q` → green (baseline 102 passed, 1 skipped). Commit.

---

### Task 2: SSE decision stream (`GET /api/stream`)

**Files:**
- Create: `app/api/bus.py`
- Modify: `app/api/ui_routes.py` (the `/api/stream` endpoint)
- Modify: `app/api/routes.py` (publish each completed request to the bus)
- Modify: `app/main.py` (construct `DecisionBus` on `app.state`)
- Test: `tests/test_decision_stream.py`

**Interfaces:**
- Produces:
  - `class DecisionBus`: `subscribe() -> asyncio.Queue`, `unsubscribe(q)`, `async publish(event: dict)`. Bounded per-subscriber queue (e.g. maxsize 1000); on full, drop-oldest (demo resilience — a slow browser tab must not block serving). Fan-out to all current subscribers.
  - `GET /api/stream` → `text/event-stream`. On connect, replays nothing (or the last N summaries — keep simple: no replay), then streams events as they publish. Each event is one `data: <json>\n\n` line. Event shapes: `{"type": "request", "summary": {...}}` on completion, and `{"type": "step", "request_id": ..., "record": {...}, "index": n}` for each trace record (so the visualizer can light nodes one by one). Terminate cleanly on client disconnect (catch `asyncio.CancelledError`).
- **Publish flow:** in `routes.py`, after recording history, publish a `request` event (the summary) followed by one `step` event per trace record. Wrap in try/except — streaming must never break serving.

- [ ] **Step 1: Failing test** `tests/test_decision_stream.py`. `TestClient` supports streaming via `client.stream(...)`. Concretely: open `GET /api/stream` with a background reader, POST a chat request, assert the reader receives at least one `type:"request"` event whose summary id matches, and at least one `type:"step"` event. If TestClient streaming is awkward, test the `DecisionBus` unit directly (subscribe, publish, assert queue receives) AND a lighter endpoint test that asserts `/api/stream` returns 200 with `content-type: text/event-stream`. Prefer the unit test for the fan-out logic + a 200/content-type check for the endpoint.

```python
import asyncio, pytest
from app.api.bus import DecisionBus

@pytest.mark.asyncio
async def test_bus_fanout_and_drop_oldest():
    bus = DecisionBus()
    q1, q2 = bus.subscribe(), bus.subscribe()
    await bus.publish({"type": "request", "summary": {"id": "a"}})
    assert (await asyncio.wait_for(q1.get(), 1))["summary"]["id"] == "a"
    assert (await asyncio.wait_for(q2.get(), 1))["summary"]["id"] == "a"
    bus.unsubscribe(q2)
    await bus.publish({"type": "step", "request_id": "a"})
    assert q1.qsize() == 1  # q2 unsubscribed, no error
```

(Add `pytest-asyncio` to the dev/test deps if not present — check `pyproject.toml`; the project already runs async tests for the agent runtime, so an async test harness likely exists. If tests use `asyncio.run(...)` instead of the plugin, mirror that pattern and drop the marker.)

- [ ] **Step 2: Run → FAIL.**
- [ ] **Step 3: Implement `app/api/bus.py`** (asyncio fan-out, bounded drop-oldest queues).
- [ ] **Step 4: Implement `/api/stream`** in `ui_routes.py` using `StreamingResponse` (media_type `text/event-stream`) that subscribes, yields `f"data: {json.dumps(evt)}\n\n"` in a loop, and unsubscribes in a `finally`. Send an initial `: connected\n\n` comment and periodic heartbeats (`: ping\n\n` every ~15s) so proxies don't idle-close — keep it simple with a `wait_for(queue.get(), timeout=15)` that emits a heartbeat on timeout.
- [ ] **Step 5: Publish from `routes.py`** (summary event + per-record step events), guarded.
- [ ] **Step 6: Wire `DecisionBus` on `app.state`** in main.py.
- [ ] **Step 7: Run tests → PASS. Full suite green. Commit.**

---

### Task 3: Runtime Headroom toggle (`GET/POST /api/controls`)

**Files:**
- Modify: `app/pipeline.py` (read the effective compression cfg via a callback, not a frozen value)
- Modify: `app/main.py` (pass the callback; keep `app.state.co_cfg` as the single mutable source)
- Modify: `app/api/ui_routes.py` (`GET`/`POST /api/controls`)
- Test: `tests/test_controls.py`

**Interfaces:**
- `Pipeline.__init__` currently takes `cfg: ContextOptimizationCfg`. Change it to `co_cfg_provider: Callable[[], ContextOptimizationCfg]` and use `self._co_cfg_provider()` wherever it read `self._co_cfg`. main.py passes `lambda: app.state.co_cfg`. The internal endpoint already reads `request.app.state.co_cfg`, so both paths now share the single mutable source.
- `GET /api/controls` → `{"headroom_enabled": bool, "min_tokens": int, "cost_optimization_note": "...", ...}` reflecting the current `app.state.co_cfg`.
- `POST /api/controls` body `{"headroom_enabled": bool}` → replaces `app.state.co_cfg` with `app.state.co_cfg.model_copy(update={"enabled": value})` and returns the new state. (Only `headroom_enabled` is mutable this phase; document that provider toggles / cost-weight are deferred — the router selects tiers by complexity/risk, not cost, so a cost slider wouldn't shift tiers, and per-request `rejected[]` already greys excluded providers in the visualizer.)

- [ ] **Step 1: Failing test** `tests/test_controls.py` (`TestClient`):

```python
def test_toggle_headroom_runtime(client):
    assert client.get("/api/controls").json()["headroom_enabled"] is False
    r = client.post("/api/controls", json={"headroom_enabled": True})
    assert r.status_code == 200 and r.json()["headroom_enabled"] is True
    assert client.get("/api/controls").json()["headroom_enabled"] is True
```

Add a direct behavioral test: with headroom toggled ON via `/api/controls`, a verbose multi-step request's compression trace record shows `applied=True` (i.e. the toggle actually reached the pipeline). Mirror how `tests/test_agent_hop_compression.py` / the compression e2e drives a compressible request. If that's heavy, at minimum assert the direct pipeline path honors the runtime cfg by toggling and checking a compressible request's `compression_saved_usd > 0` after enabling. (Use `min_tokens` small enough or a long body.)

- [ ] **Step 2: Run → FAIL.**
- [ ] **Step 3: Refactor `Pipeline`** to the callback. Grep every `self._co_cfg` use and every `Pipeline(` call site (main.py + conftest fixtures + any test) — update all. The `compression_on_pipeline` conftest fixture builds its own cfg; give it `co_cfg_provider=lambda: co` to match. Keep behavior identical when nothing toggles.
- [ ] **Step 4: Implement `/api/controls`** GET/POST in ui_routes.py.
- [ ] **Step 5: Run tests → PASS. Full suite green.** Watch for broken Pipeline call sites. Commit.

---

## Self-Review notes (controller)
- Privacy: history stores only the already-redacted response; `test_ui_routes` asserts raw SSN absent from the detail body. CCR contents structurally absent. Types-only entities.
- Additivity: stats sum per-request `baseline/routing_saved/compression_saved` — coherent with the per-request two-lever fields from Phase 5b.
- Deferred (documented, non-blocking): cost-weight slider (router is complexity/risk-tiered, not cost-tiered — a slider wouldn't shift tiers honestly); runtime provider toggles (per-request `rejected[]` greying already carries the exclusion story); SSE replay-on-connect (fresh connect starts live — the history list endpoint covers backfill). Scripted-request buttons live in the frontend (canned request bodies), not a backend endpoint.
