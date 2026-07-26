# Per-Application Admin Console (Plan A) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the demo-era global "Live" dashboard with an app-centric admin console: a fleet overview that drills into a single application's Cost / Privacy / Traffic / Requests, and a two-altitude per-request drill-down.

**Architecture:** The console is fed by the existing in-memory `HistoryStore` (`RequestSummary` already carries the two-lever savings split, `detected_entities`, `hops`, tier, latency, `onprem_only`, `sensitivity`), reached through the existing `/api/*` read endpoints extended with an optional `app` (tenant) filter plus one new `/api/fleet` grouping. App names/config come from the existing admin API (`adminApi.apps()`). No SQL schema change. The four per-app sections are computed client-side by pure functions in a new `aggregate.ts`. The request drill-down consolidates today's FlowVisualizer + DecisionInspector + CompressionView into one component rendered at two altitudes (plain-language summary over technical trace).

**Tech Stack:** FastAPI + Pydantic v2 (backend), pytest (`TestClient`, `asyncio.run` — no pytest-asyncio), React 18 + Vite + Tailwind (`darkMode: class`) + Recharts (frontend). No JS test harness exists — frontend tasks are verified by `npx tsc --noEmit` + `npm run build` + a headless-chrome screenshot.

## Global Constraints

- **Data source:** `HistoryStore` is in-memory, global, capped at 500, resets on restart. Durability (persisting the console onto the SQL `usage_records` store) is an explicit **follow-up, out of scope for Plan A**.
- **Never render raw PII** — entity **types** only (`RequestSummary.detected_entities` is already types).
- **Never render CCR contents** — counts only.
- **Grey excluded providers with the reason** — the privacy visual stays.
- **Both on-prem tiers are the same 7B today** — label representative, don't imply distinct models.
- **Compression is OFF in the deploy** — show compression savings only when a record actually ran.
- **Tier colors, reused everywhere:** `cheap` `#22c55e`, `mid` `#3b82f6`, `frontier` `#ef4444` (from `TIER_COLOR` / Tailwind `cheap|mid|frontier`).
- **No demo surfaces** on product screens: no personas, no traffic-generator controls, no animated 10-step teaching spine on the landing/overview. (The `tests/traffic/` CLI stays.)
- **Tailwind tokens only:** surfaces `bg-ink` / `bg-panel` / `border-edge` / `text-accent`; gray ramp `text-gray-100..600`; active nav = `bg-accent/15 text-accent`; selected row = `border-accent bg-accent/5`; StatTile = `bg-ink/60 border border-edge`; Panel = `bg-panel border border-edge rounded-lg`. Theme via `useTheme()`.
- **No client-side secrets;** UI talks only to our API. Admin endpoints use cookie auth (`credentials: "same-origin"`).

---

### Task 1: Per-app filter on the request list

**Files:**
- Modify: `app/api/history.py` (`HistoryStore.list`)
- Modify: `app/api/ui_routes.py` (`GET /api/requests`)
- Test: `tests/test_history_store.py`, `tests/test_ui_routes.py`

**Interfaces:**
- Produces: `HistoryStore.list(self, limit: int = 100, app: str | None = None) -> list[RequestSummary]` — when `app` is set, only summaries whose `tenant_id == app`. `GET /api/requests?limit=&app=` passes `app` through.

- [ ] **Step 1: Write the failing test** (append to `tests/test_history_store.py`)

```python
def test_list_filters_by_app():
    from app.api.history import HistoryStore
    store = HistoryStore()
    base = {"id": "x", "trace": [], "cost": {}, "policy": {}, "model": "m",
            "model_display": "M", "usage": {}}
    store.record({**base, "id": "a1"}, tenant_id="acme", tier="cheap")
    store.record({**base, "id": "h1"}, tenant_id="hospital", tier="mid")
    store.record({**base, "id": "a2"}, tenant_id="acme", tier="cheap")
    ids = [s.id for s in store.list(app="acme")]
    assert ids == ["a2", "a1"]           # newest-first, hospital excluded
    assert len(store.list()) == 3        # unfiltered unchanged
```

- [ ] **Step 2: Run it, verify it fails**

Run: `uv run pytest tests/test_history_store.py::test_list_filters_by_app -v`
Expected: FAIL — `list()` currently takes no `app` kwarg (`TypeError`).

- [ ] **Step 3: Implement the filter** (`app/api/history.py`, in `HistoryStore.list`)

```python
def list(self, limit: int = 100, app: str | None = None) -> list[RequestSummary]:
    items = reversed(self._summaries)
    if app is not None:
        items = (s for s in items if s.tenant_id == app)
    out: list[RequestSummary] = []
    for s in items:
        out.append(s)
        if len(out) >= limit:
            break
    return out
```
(Adapt to the existing body — keep the current newest-first source; add the `app` guard and the early-break at `limit`. If the current impl slices a list, filter before slicing.)

- [ ] **Step 4: Add the query param** (`app/api/ui_routes.py`, `GET /api/requests`)

```python
@router.get("/requests")
def list_requests(request: Request, limit: int = 100, app: str | None = None) -> list:
    return request.app.state.history.list(limit=limit, app=app)
```

- [ ] **Step 5: Endpoint test** (append to `tests/test_ui_routes.py`)

```python
def test_requests_endpoint_filters_by_app(client):
    # seeded tokens acme-key / hospital-key drive one request each
    client.post("/v1/chat/completions", headers={"Authorization": "Bearer acme-key"},
                json={"messages": [{"role": "user", "content": "hi"}]})
    client.post("/v1/chat/completions", headers={"Authorization": "Bearer hospital-key"},
                json={"messages": [{"role": "user", "content": "hi"}]})
    r = client.get("/api/requests?app=acme")
    assert r.status_code == 200
    assert all(row["tenant_id"] == "acme" for row in r.json())
    assert len(r.json()) >= 1
```

- [ ] **Step 6: Run both tests, verify pass**

Run: `uv run pytest tests/test_history_store.py::test_list_filters_by_app tests/test_ui_routes.py::test_requests_endpoint_filters_by_app -v`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add app/api/history.py app/api/ui_routes.py tests/test_history_store.py tests/test_ui_routes.py
git commit -m "feat(api): optional app filter on /api/requests"
```

---

### Task 2: Fleet aggregate endpoint

**Files:**
- Modify: `app/api/history.py` (add `HistoryStore.fleet`)
- Modify: `app/api/ui_routes.py` (add `GET /api/fleet`)
- Test: `tests/test_history_store.py`, `tests/test_ui_routes.py`

**Interfaces:**
- Produces: `HistoryStore.fleet(self) -> list[dict]` — one row per `tenant_id` seen, each:
  `{"app": str, "count": int, "total_usd": float, "baseline_usd": float, "routing_saved_usd": float, "compression_saved_usd": float, "onprem_count": int, "pii_count": int, "latency_p50_ms": float, "tier_distribution": dict[str,int]}`. Sorted by `count` desc.
- `GET /api/fleet` returns that list.

- [ ] **Step 1: Write the failing test** (`tests/test_history_store.py`)

```python
def test_fleet_groups_by_app():
    from app.api.history import HistoryStore
    store = HistoryStore()
    def rec(id, tenant, entities):
        store.record({"id": id, "trace": [{"step": "router", "latency_ms": 10}],
                      "cost": {"total_usd": 1.0, "baseline_usd": 3.0,
                               "routing_saved_usd": 1.5, "compression_saved_usd": 0.5},
                      "policy": {"onprem_only": tenant == "hospital",
                                 "detected_entities": entities},
                      "model": "m", "model_display": "M", "usage": {}},
                     tenant_id=tenant, tier="cheap")
    rec("a1", "acme", [])
    rec("h1", "hospital", ["US_SSN"])
    rec("a2", "acme", ["EMAIL"])
    rows = {r["app"]: r for r in store.fleet()}
    assert rows["acme"]["count"] == 2
    assert rows["acme"]["pii_count"] == 1
    assert rows["hospital"]["onprem_count"] == 1
    assert store.fleet()[0]["count"] >= store.fleet()[-1]["count"]  # sorted desc
```

- [ ] **Step 2: Run it, verify it fails**

Run: `uv run pytest tests/test_history_store.py::test_fleet_groups_by_app -v`
Expected: FAIL — `fleet` not defined (`AttributeError`).

- [ ] **Step 3: Implement `fleet`** (`app/api/history.py`)

```python
def fleet(self) -> list[dict]:
    from collections import defaultdict
    import statistics
    groups: dict[str, list[RequestSummary]] = defaultdict(list)
    for s in self._summaries:
        groups[s.tenant_id].append(s)
    rows = []
    for app, items in groups.items():
        lat = sorted(s.latency_ms for s in items)
        tiers: dict[str, int] = defaultdict(int)
        for s in items:
            tiers[s.tier] += 1
        rows.append({
            "app": app,
            "count": len(items),
            "total_usd": sum(s.total_usd for s in items),
            "baseline_usd": sum(s.baseline_usd for s in items),
            "routing_saved_usd": sum(s.routing_saved_usd for s in items),
            "compression_saved_usd": sum(s.compression_saved_usd for s in items),
            "onprem_count": sum(1 for s in items if s.onprem_only),
            "pii_count": sum(1 for s in items if s.detected_entities),
            "latency_p50_ms": statistics.median(lat) if lat else 0.0,
            "tier_distribution": dict(tiers),
        })
    rows.sort(key=lambda r: r["count"], reverse=True)
    return rows
```

- [ ] **Step 4: Add the route** (`app/api/ui_routes.py`)

```python
@router.get("/fleet")
def fleet(request: Request) -> list:
    return request.app.state.history.fleet()
```

- [ ] **Step 5: Endpoint test** (`tests/test_ui_routes.py`)

```python
def test_fleet_endpoint(client):
    client.post("/v1/chat/completions", headers={"Authorization": "Bearer acme-key"},
                json={"messages": [{"role": "user", "content": "hi"}]})
    r = client.get("/api/fleet")
    assert r.status_code == 200
    rows = r.json()
    assert any(row["app"] == "acme" and row["count"] >= 1 for row in rows)
```

- [ ] **Step 6: Run, verify pass**

Run: `uv run pytest tests/test_history_store.py::test_fleet_groups_by_app tests/test_ui_routes.py::test_fleet_endpoint -v`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add app/api/history.py app/api/ui_routes.py tests/test_history_store.py tests/test_ui_routes.py
git commit -m "feat(api): /api/fleet per-application aggregate"
```

---

### Task 3: API client — app filter + fleet type

**Files:**
- Modify: `ui/src/api.ts`

**Interfaces:**
- Produces: `interface FleetRow { app: string; count: number; total_usd: number; baseline_usd: number; routing_saved_usd: number; compression_saved_usd: number; onprem_count: number; pii_count: number; latency_p50_ms: number; tier_distribution: Record<string, number> }`
- `api.requests(limit?: number, app?: string)`; `api.fleet(): Promise<FleetRow[]>`.

- [ ] **Step 1: Add the type** (`ui/src/api.ts`, near the other interfaces)

```ts
export interface FleetRow {
  app: string;
  count: number;
  total_usd: number;
  baseline_usd: number;
  routing_saved_usd: number;
  compression_saved_usd: number;
  onprem_count: number;
  pii_count: number;
  latency_p50_ms: number;
  tier_distribution: Record<string, number>;
}
```

- [ ] **Step 2: Extend `api.requests` and add `api.fleet`** (in the `api` object)

```ts
requests: (limit = 100, app?: string) =>
  fetch(`/api/requests?limit=${limit}${app ? `&app=${encodeURIComponent(app)}` : ""}`)
    .then(j<RequestSummary[]>),
fleet: () => fetch(`/api/fleet`).then(j<FleetRow[]>),
```

- [ ] **Step 3: Typecheck**

Run: `cd ui && npx tsc --noEmit`
Expected: no errors.

- [ ] **Step 4: Commit**

```bash
git add ui/src/api.ts
git commit -m "feat(ui): FleetRow type + api.fleet, app filter on api.requests"
```

---

### Task 4: Aggregation helpers (the section logic)

**Files:**
- Create: `ui/src/console/aggregate.ts`

**Interfaces:**
- Produces pure functions over `RequestSummary[]`:
  - `costAgg(rs): { total: number; baseline: number; routingSaved: number; compressionSaved: number; savedPct: number }`
  - `privacyAgg(rs): { pii: number; onprem: number; keptOffCloud: number; entityTypes: string[] }`
  - `trafficAgg(rs): { volume: number; tierMix: Record<string, number>; p50: number; p95: number; multiStepPct: number }`
  - `percentile(sorted: number[], p: number): number`

These are the only non-trivial frontend logic; keep them pure so they're trivially reviewable (and unit-testable if a JS harness is added later).

- [ ] **Step 1: Write the module**

```ts
import type { RequestSummary } from "../api";

export function percentile(sorted: number[], p: number): number {
  if (sorted.length === 0) return 0;
  const idx = Math.min(sorted.length - 1, Math.floor((p / 100) * sorted.length));
  return sorted[idx];
}

export function costAgg(rs: RequestSummary[]) {
  const total = rs.reduce((a, r) => a + r.total_usd, 0);
  const baseline = rs.reduce((a, r) => a + r.baseline_usd, 0);
  const routingSaved = rs.reduce((a, r) => a + r.routing_saved_usd, 0);
  const compressionSaved = rs.reduce((a, r) => a + r.compression_saved_usd, 0);
  const savedPct = baseline > 0 ? ((baseline - total) / baseline) * 100 : 0;
  return { total, baseline, routingSaved, compressionSaved, savedPct };
}

export function privacyAgg(rs: RequestSummary[]) {
  const withPii = rs.filter((r) => r.detected_entities.length > 0);
  const onprem = rs.filter((r) => r.onprem_only);
  const entityTypes = [...new Set(rs.flatMap((r) => r.detected_entities))].sort();
  // "kept off cloud" = sensitive requests that were forced on-prem (the privacy proof)
  const keptOffCloud = rs.filter((r) => r.onprem_only && r.detected_entities.length > 0).length;
  return { pii: withPii.length, onprem: onprem.length, keptOffCloud, entityTypes };
}

export function trafficAgg(rs: RequestSummary[]) {
  const tierMix: Record<string, number> = {};
  for (const r of rs) tierMix[r.tier] = (tierMix[r.tier] ?? 0) + 1;
  const lat = rs.map((r) => r.latency_ms).sort((a, b) => a - b);
  const multiStep = rs.filter((r) => r.hops > 1).length;
  return {
    volume: rs.length,
    tierMix,
    p50: percentile(lat, 50),
    p95: percentile(lat, 95),
    multiStepPct: rs.length ? (multiStep / rs.length) * 100 : 0,
  };
}
```

- [ ] **Step 2: Typecheck**

Run: `cd ui && npx tsc --noEmit`
Expected: no errors.

- [ ] **Step 3: Commit**

```bash
git add ui/src/console/aggregate.ts
git commit -m "feat(ui): pure per-app aggregation helpers"
```

---

### Task 5: Console shell — Fleet ⇄ App navigation

**Files:**
- Create: `ui/src/console/ConsoleShell.tsx`
- Modify: `ui/src/App.tsx`

**Interfaces:**
- Consumes: `AdminConsole` auth pattern (`adminApi.me()` gate — reuse), `FleetOverview` (Task 6), `AppPage` (Task 7).
- Produces: `export function ConsoleShell()` — owns `selectedApp: string | null`; `null` → `<FleetOverview onOpenApp={setSelectedApp} />`, else `<AppPage appId={selectedApp} onBack={() => setSelectedApp(null)} />`. Renders a breadcrumb (`Fleet / {appName}`).

The whole product is now one console. `App.tsx` drops the `"live" | "admin"` toggle and the four live panels; the header keeps `RouteForgeLogo`, connection dot, `ThemeToggle`, `CdwMark`. Body becomes: admin auth gate → `<ConsoleShell/>`.

- [ ] **Step 1: Write `ConsoleShell.tsx`**

Structure (real signatures; JSX follows existing token classes):
```tsx
import { useState } from "react";
import { FleetOverview } from "./FleetOverview";
import { AppPage } from "./AppPage";

export function ConsoleShell() {
  const [appId, setAppId] = useState<string | null>(null);
  return appId
    ? <AppPage appId={appId} onBack={() => setAppId(null)} />
    : <FleetOverview onOpenApp={setAppId} />;
}
```

- [ ] **Step 2: Rewire `App.tsx`** — remove `View`/`view`/`setView`, the live-panel imports (FlowVisualizer, RequestList, DecisionInspector, CompressionView, CostDashboard), the live state (requests/stats/controls/liveTraces/selected*), the traffic/controls handlers, and the `<main>` grid. Keep the header, `useTheme`, connection dot. Gate on admin auth (lift the `adminApi.me()` check from `AdminConsole` into `App` or wrap `ConsoleShell` with the existing `AdminLogin`). Body:

```tsx
{authed ? <ConsoleShell /> : <AdminLogin onLogin={() => setAuthed(true)} />}
```

- [ ] **Step 3: Typecheck + build**

Run: `cd ui && npx tsc --noEmit && npm run build`
Expected: builds clean (old components may still exist unused — removed in Task 10).

- [ ] **Step 4: Commit**

```bash
git add ui/src/console/ConsoleShell.tsx ui/src/App.tsx
git commit -m "feat(ui): single app-centric console shell, drop Live/Admin toggle"
```

---

### Task 6: Fleet overview — aggregate strip + sortable table

**Files:**
- Create: `ui/src/console/FleetOverview.tsx`

**Interfaces:**
- Consumes: `api.fleet()` → `FleetRow[]`, `adminApi.apps()` → `AdminApp[]` (for names + apps with zero traffic), `Panel`/`StatTile`/`formatUsd`/`formatMs`/`TierDot` from `components/common`, `TIER_COLOR`.
- Produces: `export function FleetOverview({ onOpenApp }: { onOpenApp: (appId: string) => void })`.

Layout:
1. **Aggregate strip** — a `StatTile` row summing all `FleetRow`s: total requests, total spend, total saved (`baseline-total`) with `savedPct`, total kept on-prem.
2. **Per-app table** — join `adminApi.apps()` (all apps, names, policy.residency) with `FleetRow` by `app === AdminApp.id`; apps with no traffic show zeros. Columns: App (name + id), Requests, Spend, Saved (%), PII, On-prem, p50 latency, tier-mix mini-bar, ⚠ flag. **Sortable** by clicking a column header (local `sortKey`/`sortDir` state). Each row is a `<button>` → `onOpenApp(app.id)` (active/hover: `hover:border-gray-600`, `bg-ink/40`).
   - ⚠ attention flag heuristic (v1): `pii_count > 0 && onprem_count < pii_count` (sensitive traffic that wasn't all kept on-prem) → amber ⚠ with title. Refine later.

- [ ] **Step 1: Write the component** (poll `api.fleet()` every 3s like the existing stats backstop; use `TIER_COLOR` for the mini-bar segments; tier colors per Global Constraints).
- [ ] **Step 2: Typecheck + build** — `cd ui && npx tsc --noEmit && npm run build`
- [ ] **Step 3: Screenshot check** — run the UI (or `npm run preview`), headless-chrome the landing; confirm aggregate strip + sortable table render, clicking a row calls `onOpenApp`.
- [ ] **Step 4: Commit**

```bash
git add ui/src/console/FleetOverview.tsx
git commit -m "feat(ui): fleet overview — aggregate strip + sortable app table"
```

---

### Task 7: Application page — tabs + per-app data

**Files:**
- Create: `ui/src/console/AppPage.tsx`

**Interfaces:**
- Consumes: `api.requests(500, appId)`, `subscribeStream` (filter events by `summary.tenant_id === appId`; step events by whether their `request_id` is in the app's known ids), `adminApi.apps()` (find this app's `AdminApp` for name/policy), the section components (Task 8), `RequestDrilldown` (Task 9), the existing `ApplicationsPanel` policy/token UI (Task 10).
- Produces: `export function AppPage({ appId, onBack }: { appId: string; onBack: () => void })` — owns `requests: RequestSummary[]` (seeded from `api.requests(500, appId)`, appended from the filtered SSE), `tab` (`overview|cost|privacy|traffic|requests|policy|tokens`), `selectedRequestId`, `selectedDetail` (`api.request(id)`).

- [ ] **Step 1: Write the shell** — header: `← Fleet` back button + app name + `id · residency`. Tab bar (same `bg-accent/15 text-accent` active style as `AdminConsole`). Body switches on `tab`; `requests` tab renders the per-app request list (reuse `RequestList` semantics) and, on selection, `<RequestDrilldown detail={selectedDetail} .../>`.
- [ ] **Step 2: SSE tenant filter** — subscribe on mount; in the `"request"` handler `if (evt.summary.tenant_id !== appId) return;` before prepend. Track a `Set` of this app's request ids so `"step"` events can be matched (or simply refetch detail on selection). Unsubscribe on unmount / appId change.
- [ ] **Step 3: Typecheck + build** — `cd ui && npx tsc --noEmit && npm run build`
- [ ] **Step 4: Commit**

```bash
git add ui/src/console/AppPage.tsx
git commit -m "feat(ui): application page shell with tabs + per-app SSE filter"
```

---

### Task 8: The four sections

**Files:**
- Create: `ui/src/console/sections/OverviewSection.tsx`, `CostSection.tsx`, `PrivacySection.tsx`, `TrafficSection.tsx`

**Interfaces:**
- Each consumes `{ requests: RequestSummary[] }` and the Task-4 aggregators; render with `Panel`/`StatTile` and (Cost/Traffic) Recharts via `useTheme`.
- `OverviewSection` = a compact roll-up: headline saved% + 3–4 StatTiles pulling from all three aggregators (the "answer desk at a glance").

Section contents:
- **CostSection** — `costAgg`: headline spend + `formatPct(savedPct)`; a two-bar "savings by lever" (routing `#38bdf8` vs compression `#f59e0b`, matching `CostDashboard`'s existing `ROUTING_COLOR`/`COMPRESSION_COLOR`); note "compression shows only when Headroom ran" per Global Constraints (if `compressionSaved === 0`, label it *off*). Cost-over-time line reused from `CostDashboard` pattern (cumulative by `created_seq`).
- **PrivacySection** — `privacyAgg`: StatTiles for "requests with PII", "forced on-prem", "kept off cloud"; a chip list of `entityTypes` (types only — Global Constraint). Plain-language caption: *"Sensitive requests kept on your infrastructure."*
- **TrafficSection** — `trafficAgg`: tier-mix pie (`TIER_COLOR`), p50/p95 StatTiles, "% multi-step" StatTile.

- [ ] **Step 1: Write the four section files** (reuse `CostDashboard.tsx` chart idioms for Cost/Traffic).
- [ ] **Step 2: Typecheck + build** — `cd ui && npx tsc --noEmit && npm run build`
- [ ] **Step 3: Screenshot** — open an app with traffic; confirm each tab renders real numbers; a PII request shows entity **types** only.
- [ ] **Step 4: Commit**

```bash
git add ui/src/console/sections
git commit -m "feat(ui): Overview/Cost/Privacy/Traffic app sections"
```

---

### Task 9: Request drill-down — two altitudes

**Files:**
- Create: `ui/src/console/RequestDrilldown.tsx`

**Interfaces:**
- Consumes: `ChatResponse` (`detail`), `TraceRecord`, `STEPS`/`stepMeta`, `TIER_COLOR`, the provider-eligibility idiom from `FlowVisualizer` (greyed providers), and `DecisionInspector`'s rejected-candidate rendering (chosen vs rejected + reasons).
- Produces: `export function RequestDrilldown({ detail, loading }: { detail: ChatResponse | null; loading: boolean })`.

Two stacked bands over the same `detail`:
1. **Plain-language summary (top, owner-facing)** — no step numbers/jargon. Compose a sentence from `detail.policy` + chosen model + cost + latency + verify record, e.g.:
   *"Checked for sensitive data ({entities ? `found ${types}` : "none"}) → {onprem_only ? "kept on your infrastructure" : "eligible for cloud"} → answered by {model_display} → {verify.passed ? "verified clean" : "flagged"}, in {latency} for {cost}."*
   Plus the **greyed-provider strip** (the privacy visual) from `detail.policy.allowed_providers`.
2. **Technical trace (below, admin-facing)** — the full per-step detail: reuse `DecisionInspector`'s `StepDetail` switch (chosen vs rejected candidates with reasons, per-hop + cumulative cost, entity **types**, compression record if present, verification/escalation), and the live `langfuse_url` deep-link. A `hops > 1` request shows the per-hop router timeline.

This **replaces** FlowVisualizer + DecisionInspector + CompressionView as three separate panels — they collapse into this one component (removed in Task 10). Preserve their honesty rules (types only, CCR counts only, greyed providers).

- [ ] **Step 1: Write the component** — lift the reusable render helpers (`StepDetail`, `Chosen`, `Row`, provider strip) from `DecisionInspector.tsx`/`FlowVisualizer.tsx` into this file (or a shared `drilldownParts.tsx`), rather than importing the old panels.
- [ ] **Step 2: Typecheck + build** — `cd ui && npx tsc --noEmit && npm run build`
- [ ] **Step 3: Screenshot** — select a clean request and a PII request; confirm the plain summary reads correctly, greyed providers show for the PII one, and the technical trace shows chosen-vs-rejected + Langfuse link.
- [ ] **Step 4: Commit**

```bash
git add ui/src/console/RequestDrilldown.tsx
git commit -m "feat(ui): two-altitude request drill-down (consolidates flow+inspector+compression)"
```

---

### Task 10: Policy/Tokens tabs + remove dead demo surfaces

**Files:**
- Modify: `ui/src/admin/ApplicationsPanel.tsx` (extract `PolicyEditor` + token UI as named exports, or export a `<AppConfig appId>` wrapper)
- Modify: `ui/src/console/AppPage.tsx` (render the extracted config in Policy/Tokens tabs)
- Delete: `ui/src/components/FlowVisualizer.tsx`, `DecisionInspector.tsx`, `CompressionView.tsx`, `RequestList.tsx` (superseded), and `ui/src/admin/UsagePanel.tsx` (usage now lives in the app sections). Remove `SCRIPTED` + traffic fns from `api.ts` if now unused.
- Modify: `ui/src/App.tsx` — ensure no dangling imports.

**Interfaces:**
- Consumes: existing `adminApi.setPolicy`/`createToken`/`revokeToken`.
- Produces: `export function AppConfig({ appId }: { appId: string })` in `ApplicationsPanel.tsx` (the policy editor + token list/issue/revoke for one app), reused by AppPage's Policy and Tokens tabs.

- [ ] **Step 1: Extract `AppConfig`** from `ApplicationsPanel` (the `PolicyEditor` + `AppDetail` token section) as a named export taking `appId`; keep `ApplicationsPanel` working by consuming it, OR retire `ApplicationsPanel`'s standalone use if fully replaced.
- [ ] **Step 2: Wire Policy/Tokens tabs** in `AppPage` to `<AppConfig appId={appId} />`.
- [ ] **Step 3: Delete superseded files** and remove now-unused exports (`SCRIPTED`, `startTraffic`, `trafficStatus`, `TrafficStartResult`, `TrafficStatus`) if nothing references them (`grep` first).
- [ ] **Step 4: Typecheck + build** — `cd ui && npx tsc --noEmit && npm run build`
- [ ] **Step 5: Full screenshot pass** — Fleet → open app → all seven tabs → drill into a request. Confirm no demo surfaces remain (no personas/traffic/teaching-spine).
- [ ] **Step 6: Commit**

```bash
git add -A ui/src
git commit -m "refactor(ui): reuse app config in console tabs; remove demo panels"
```

---

## Self-Review

**Spec coverage** (against `2026-07-26-routeforge-per-application-console-design.md`):
- §4 IA (Fleet → App → Request): Tasks 5 (shell), 6 (fleet), 7 (app page), 9 (drill-down). ✓
- §5 four sections + Policy/Tokens: Tasks 8 + 10. ✓ (Requests tab = Task 7.)
- §5 Fleet = sortable table + all-apps aggregate: Task 6. ✓
- §6 two-altitude drill-down consolidating flow+inspector+compression: Task 9. ✓
- §7 value weighting (core steps 2/6/9/10 surface; compression conditional): Cost/Privacy/Traffic sections + drill-down honor it. ✓ (Step-9 escalation *rate* aggregate depends on Plan B's durable events — noted; drill-down still shows the per-request verify record from the live trace.)
- §8 `?app=` filter + app identity on rows: Tasks 1, 6, 7. ✓
- §8 step-9/step-7 event **exposure**: **Plan B** (out of scope here) — noted, not silently dropped.
- §9 LiteLLM: **Plan C** (out of scope here). ✓
- §11 honesty rules: Global Constraints + Tasks 8/9. ✓

**Placeholder scan:** backend tasks (1,2) carry full test+impl code. Frontend tasks give exact signatures, the real aggregation logic (Task 4), exact reuse sources, and exact Tailwind tokens; presentational JSX is specified structurally + verified by tsc/build/screenshot because **no JS test harness exists** (stated in Tech Stack). This is a deliberate, disclosed verification substitution, not a placeholder.

**Type consistency:** `FleetRow` (Task 3) matches `HistoryStore.fleet()` dict keys (Task 2). `costAgg`/`privacyAgg`/`trafficAgg` field names (Task 4) are consumed by Task 8 sections. `AppConfig({appId})` (Task 10) consumed by `AppPage` (Task 7). `RequestDrilldown({detail, loading})` (Task 9) consumed by `AppPage`.

**Known follow-ups (explicitly deferred, not gaps):**
1. **Durability** — persist the console onto SQL `usage_records` (add two-lever split + `pii_types` + `hops` columns) so fleet/sections survive restart and exceed 500 rows.
2. **Plan B** — step-9 verification/escalation + step-7 quota/breaker event exposure → Quality escalation-rate + attention flags.
3. **Plan C** — LiteLLM gateway swap (steps 7–8).
