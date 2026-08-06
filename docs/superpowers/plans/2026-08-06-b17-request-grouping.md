# B17 — Client-Workflow Request Grouping Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let a client tag one workflow run's N `/v1/chat/completions` calls with an opaque correlation id (`X-RouteForge-Session`), then group those calls into an expandable parent→children tree in the console and into one Langfuse Session.

**Architecture:** RouteForge reads the header, sanitizes it fail-open, carries it opaquely on the in-memory `RequestSummary` and response dict, exposes it on `/requests` + drilldown, stamps `langfuse.session.id` on the trace root span, and the UI groups rows by session id. No DB column, no policy/routing change — a label only. The reference sample (`test_app`) mints one id per investigation run to prove a real client drives it.

**Tech Stack:** Python 3.12, FastAPI, Pydantic v2, OpenTelemetry (Langfuse export), React + Vite + TypeScript + Tailwind, LangChain (sample), pytest / httpx.MockTransport.

## Global Constraints

- Header name is exactly `X-RouteForge-Session` (read case-insensitively as `x-routeforge-session`). One word `onprem`, tiers `cheap|mid|frontier` — unchanged by this work.
- `session_id` is an **opaque label only**: never a policy/routing/gate/cost input, and it **fails open** — an absent, over-long (>128 chars), or malformed (outside `[A-Za-z0-9._:-]`) header yields `None` and the request is still served.
- Sanitized charset is exactly `[A-Za-z0-9._:-]`; max length 128.
- **In-memory only** — no DB column, no migration. `session_id` lives on `RequestSummary` and the response dict.
- `session_id` is **NOT** added to `app/obs/otel.py` `REDACT_KEYS` — it must reach the UI and Langfuse intact.
- Langfuse session attribute key is exactly `langfuse.session.id`, set on the `serving.request` root span.
- Session granularity = one workflow *run* (per-run nonce), never one app or one user.
- Run backend tests with: `uv run pytest <targets> -q -p no:cacheprovider -W ignore`. UI has no test runner — verify with `./node_modules/.bin/tsc -b --noEmit` then `./node_modules/.bin/vite build` (avoid `2>&1` and `npm run` — the rtk hook mangles them).

---

### Task 1: Server — capture header, store on `RequestSummary`, expose on API

**Files:**
- Modify: `app/api/routes.py` (add `re` import, `_SESSION_ID_RE`, `_sanitize_session_id`, `_read_session_id`; set `resp["session_id"]` after a successful pipeline run)
- Modify: `app/api/history.py:29-49` (add `session_id` field), `app/api/history.py:91-112` (populate it in `record`)
- Test: `tests/test_session_id.py` (new — pure sanitizer), `tests/test_ui_routes.py` (route → API exposure)

**Interfaces:**
- Produces: `app.api.routes._sanitize_session_id(raw: str | None) -> str | None`; `app.api.routes._read_session_id(request: Request) -> str | None`; a `session_id` local in `chat_completions` set immediately after the pipeline-run try/except and `resp["session_id"] = session_id`; `RequestSummary.session_id: str | None`.
- Later tasks rely on: the `session_id` local existing before the `export_trace(...)` call (Task 2), and `RequestSummary.session_id` (Task 3), and the header contract (Task 4).

- [ ] **Step 1: Write the failing sanitizer test**

Create `tests/test_session_id.py`:

```python
from app.api.routes import _sanitize_session_id


def test_valid_id_passes():
    assert _sanitize_session_id("inv-tck-1001-ab12cd34") == "inv-tck-1001-ab12cd34"
    assert _sanitize_session_id("  ok.id:1  ") == "ok.id:1"  # trimmed


def test_absent_or_empty_is_none():
    assert _sanitize_session_id(None) is None
    assert _sanitize_session_id("") is None
    assert _sanitize_session_id("   ") is None


def test_over_128_chars_is_none():
    assert _sanitize_session_id("a" * 128) == "a" * 128
    assert _sanitize_session_id("a" * 129) is None


def test_bad_charset_is_none():
    assert _sanitize_session_id("has space") is None
    assert _sanitize_session_id("bad@char") is None
    assert _sanitize_session_id("123-45-6789 leaked") is None  # embedded PII-ish + space
    assert _sanitize_session_id("tab\tchar") is None
```

- [ ] **Step 2: Run it to verify it fails**

Run: `uv run pytest tests/test_session_id.py -q -p no:cacheprovider -W ignore`
Expected: FAIL — `ImportError: cannot import name '_sanitize_session_id'`.

- [ ] **Step 3: Add the sanitizer + reader to `app/api/routes.py`**

Add `import re` to the imports (top of file, alongside `import uuid`). After the `router = APIRouter()` line (routes.py:15) and before `_authenticate`, add:

```python
# B17: opaque, client-supplied workflow-run correlation id. A label only —
# never a policy control — so it FAILS OPEN: absent / over-long / malformed
# values yield None (the request is still served, just ungrouped). Bounded at
# intake because it is displayed in the console and stamped onto telemetry;
# the narrow charset keeps it safe to render and to place in an OTel attribute,
# and cannot carry a formatted SSN/phone/email.
_SESSION_ID_RE = re.compile(r"[A-Za-z0-9._:-]+")


def _sanitize_session_id(raw: str | None) -> str | None:
    if not raw:
        return None
    raw = raw.strip()
    if not raw or len(raw) > 128 or not _SESSION_ID_RE.fullmatch(raw):
        return None
    return raw


def _read_session_id(request: Request) -> str | None:
    return _sanitize_session_id(request.headers.get("x-routeforge-session"))
```

- [ ] **Step 4: Run the sanitizer test to verify it passes**

Run: `uv run pytest tests/test_session_id.py -q -p no:cacheprovider -W ignore`
Expected: PASS (4 tests).

- [ ] **Step 5: Wire capture into the handler**

In `app/api/routes.py`, immediately after the pipeline-run try/except block closes (the `except Exception: raise HTTPException(500, "internal error")` at routes.py:128-129) and before the eval-capture `try:` at routes.py:130, insert:

```python
    # B17: capture the client's workflow-run correlation id (opaque label).
    # Threaded onto the response dict so both history.record and the drilldown
    # (which read `resp`) carry it with no signature change, and passed to
    # export_trace below for the Langfuse session stamp.
    session_id = _read_session_id(request)
    resp["session_id"] = session_id
```

(The `export_trace(...)` call keeps its current arguments in this task — Task 2 adds `session_id=session_id` to it. The `session_id` local is in scope for Task 2 because it is defined here.)

- [ ] **Step 6: Add the field to `RequestSummary` and populate it**

In `app/api/history.py`, add to `RequestSummary` (after `quality_source` at history.py:49):

```python
    session_id: str | None = None       # B17: client workflow-run correlation id (opaque)
```

In `HistoryStore.record`, add to the `RequestSummary(...)` construction (after `quality_source=quality.get("source"),` at history.py:111):

```python
            session_id=resp.get("session_id"),
```

- [ ] **Step 7: Write the failing route-exposure tests**

Append to `tests/test_ui_routes.py`:

```python
def test_session_id_captured_and_exposed(client):
    _login(client)
    r = client.post(
        "/v1/chat/completions",
        headers={"Authorization": "Bearer demo-key",
                 "X-RouteForge-Session": "inv-tck-1001-ab12cd34"},
        json={"messages": [{"role": "user", "content": "hi"}]},
    )
    assert r.status_code == 200
    rid = r.json()["id"]

    match = next(s for s in client.get("/api/requests").json() if s["id"] == rid)
    assert match["session_id"] == "inv-tck-1001-ab12cd34"

    detail = client.get(f"/api/requests/{rid}")
    assert detail.status_code == 200
    assert detail.json()["session_id"] == "inv-tck-1001-ab12cd34"


def test_missing_session_header_yields_null(client):
    _login(client)
    r = client.post(
        "/v1/chat/completions",
        headers={"Authorization": "Bearer demo-key"},
        json={"messages": [{"role": "user", "content": "hi"}]},
    )
    assert r.status_code == 200
    rid = r.json()["id"]
    match = next(s for s in client.get("/api/requests").json() if s["id"] == rid)
    assert match["session_id"] is None


def test_malformed_session_header_dropped_not_rejected(client):
    _login(client)
    r = client.post(
        "/v1/chat/completions",
        headers={"Authorization": "Bearer demo-key",
                 "X-RouteForge-Session": "has spaces & @!"},
        json={"messages": [{"role": "user", "content": "hi"}]},
    )
    assert r.status_code == 200  # fail-open: a bad label never fails the request
    rid = r.json()["id"]
    match = next(s for s in client.get("/api/requests").json() if s["id"] == rid)
    assert match["session_id"] is None
```

- [ ] **Step 8: Run it to verify pass**

Run: `uv run pytest tests/test_ui_routes.py tests/test_session_id.py tests/test_history_store.py -q -p no:cacheprovider -W ignore`
Expected: PASS (existing UI-route + history tests still green; three new route tests pass).

- [ ] **Step 9: Commit**

```bash
git add app/api/routes.py app/api/history.py tests/test_session_id.py tests/test_ui_routes.py
git commit -m "feat(b17): capture X-RouteForge-Session header onto RequestSummary + API"
```

---

### Task 2: Langfuse — stamp `langfuse.session.id` on the trace root span

**Files:**
- Modify: `app/obs/export.py:41-45` (add `session_id` param), `app/obs/export.py:73-75` (stamp on root span)
- Modify: `app/api/routes.py:143-152` (pass `session_id=session_id` to `export_trace`)
- Test: `tests/test_obs_export.py`

**Interfaces:**
- Consumes: the `session_id` local set in `chat_completions` (Task 1, Step 5).
- Produces: `export_trace(..., session_id: str | None = None)` sets `langfuse.session.id` on the `serving.request` root span when truthy.

- [ ] **Step 1: Write the failing export tests**

Append to `tests/test_obs_export.py`:

```python
def test_export_stamps_langfuse_session_id(mem_provider):
    export_mod.export_trace(**_sample(), session_id="inv-tck-1001-ab12cd34")
    otel._provider.force_flush()
    root = next(s for s in mem_provider.get_finished_spans() if s.name == "serving.request")
    # present, correct, and NOT redacted (mem_provider wraps RedactingSpanExporter)
    assert root.attributes["langfuse.session.id"] == "inv-tck-1001-ab12cd34"


def test_export_omits_session_id_when_absent(mem_provider):
    export_mod.export_trace(**_sample())  # no session_id kwarg
    otel._provider.force_flush()
    root = next(s for s in mem_provider.get_finished_spans() if s.name == "serving.request")
    assert "langfuse.session.id" not in root.attributes
```

- [ ] **Step 2: Run it to verify it fails**

Run: `uv run pytest tests/test_obs_export.py -q -p no:cacheprovider -W ignore`
Expected: `test_export_stamps_langfuse_session_id` FAILS — `export_trace()` got an unexpected keyword argument `session_id`.

- [ ] **Step 3: Add the param + stamp in `app/obs/export.py`**

Change the signature (export.py:41-45) to add `session_id`:

```python
def export_trace(*, request_id: str, tenant_id: str, model: str,
                 cost: dict, policy: dict, trace: list[dict],
                 application_id: str = "", app_langfuse=None,
                 capture_io: str = "off", prompt: str = "",
                 completion: str = "", recognizers=None,
                 session_id: str | None = None) -> str | None:
```

In the root-span block, after the `sp.detected_entities` set_attribute (export.py:73-74) and before `_set_io(...)` (export.py:75), add:

```python
            if session_id:
                # Langfuse groups traces sharing this attribute into one Session
                # (B17). Opaque, client-supplied; deliberately NOT in REDACT_KEYS
                # so it passes through the RedactingSpanExporter intact.
                root.set_attribute("langfuse.session.id", session_id)
```

- [ ] **Step 4: Thread it from the route**

In `app/api/routes.py`, add `session_id=session_id` to the existing `export_trace(...)` call (after `recognizers=profile.recognizers,` at routes.py:152):

```python
            recognizers=profile.recognizers,
            session_id=session_id)
```

- [ ] **Step 5: Run the export + route tests to verify pass**

Run: `uv run pytest tests/test_obs_export.py tests/test_ui_routes.py -q -p no:cacheprovider -W ignore`
Expected: PASS (both new export tests pass; existing export/route tests still green).

- [ ] **Step 6: Commit**

```bash
git add app/obs/export.py app/api/routes.py
git commit -m "feat(b17): stamp langfuse.session.id on the request root span"
```

---

### Task 3: UI — group rows by session into an expandable parent→children tree

**Files:**
- Modify: `ui/src/api.ts:9-27` (add `session_id` to `RequestSummary`)
- Modify: `ui/src/components/RequestList.tsx` (full rewrite: grouping)
- Verify: `./node_modules/.bin/tsc -b --noEmit`, `./node_modules/.bin/vite build`

**Interfaces:**
- Consumes: `RequestSummary.session_id` from the `/api/requests` payload (Task 1).
- Produces: no exported-symbol change — `RequestList` keeps its `{ requests, selectedId, onSelect }` props; grouping is internal.

- [ ] **Step 1: Add `session_id` to the `RequestSummary` type**

In `ui/src/api.ts`, add after `quality_sum: number; ...` (api.ts:26, the last field before the closing `}` at line 27):

```typescript
  session_id?: string | null; // B17: client workflow-run correlation id (opaque); groups rows
```

- [ ] **Step 2: Rewrite `RequestList.tsx` with grouping**

Replace the entire contents of `ui/src/components/RequestList.tsx` with:

```tsx
// Panel 1 (bottom half) — newest-first request list. Rows sharing a session_id
// (B17: client workflow-run correlation id) collapse under one expandable
// parent; ungrouped rows (no session_id) render flat as before.
import { useState } from "react";
import type { RequestSummary } from "../api";
import { Panel, TierDot, formatUsdPrecise, formatMs } from "./common";

type Group = { key: string; session_id: string | null; rows: RequestSummary[] };

// requests arrive newest-first; the first time a session id is seen is its
// newest member, so a group's position follows its newest row. Ungrouped rows
// (null session_id) become singleton null-session groups, rendered flat.
function groupBySession(requests: RequestSummary[]): Group[] {
  const groups: Group[] = [];
  const bySession = new Map<string, Group>();
  for (const r of requests) {
    if (!r.session_id) {
      groups.push({ key: r.id, session_id: null, rows: [r] });
      continue;
    }
    let g = bySession.get(r.session_id);
    if (!g) {
      g = { key: `session:${r.session_id}`, session_id: r.session_id, rows: [] };
      bySession.set(r.session_id, g);
      groups.push(g);
    }
    g.rows.push(r);
  }
  return groups;
}

function RequestRow({
  r,
  selected,
  onSelect,
  indent = false,
}: {
  r: RequestSummary;
  selected: boolean;
  onSelect: (id: string) => void;
  indent?: boolean;
}) {
  return (
    <button
      onClick={() => onSelect(r.id)}
      className={`text-left rounded-md border px-3 py-2 text-sm transition-colors ${indent ? "ml-3" : ""} ${
        selected ? "border-cheap bg-cheap/10" : "border-edge bg-ink/40 hover:border-gray-600"
      }`}
    >
      <div className="flex items-center justify-between gap-2">
        <div className="flex items-center gap-2 min-w-0">
          <TierDot tier={r.tier} />
          <span className="font-medium truncate" title={r.model}>{r.model_display ?? r.model}</span>
          {r.onprem_only && (
            <span className="shrink-0 rounded bg-mid/20 text-mid text-[10px] font-semibold px-1.5 py-0.5">
              on-prem
            </span>
          )}
          {r.hops > 1 && (
            <span className="shrink-0 rounded bg-gray-700 text-gray-300 text-[10px] font-semibold px-1.5 py-0.5">
              {r.hops} hops
            </span>
          )}
        </div>
        <span className="shrink-0 text-xs font-mono text-gray-400">{formatUsdPrecise(r.total_usd)}</span>
      </div>
      <div className="mt-1 flex items-center justify-between text-xs text-gray-500">
        <span>{r.sensitivity}</span>
        <span className="font-mono">{formatMs(r.latency_ms)}</span>
      </div>
    </button>
  );
}

function SessionGroup({
  group,
  collapsed,
  onToggle,
  selectedId,
  onSelect,
}: {
  group: Group;
  collapsed: boolean;
  onToggle: () => void;
  selectedId: string | null;
  onSelect: (id: string) => void;
}) {
  const totalUsd = group.rows.reduce((s, r) => s + r.total_usd, 0);
  const totalMs = group.rows.reduce((s, r) => s + r.latency_ms, 0);
  const sensitivity = group.rows[0].sensitivity; // representative (one run = one policy)
  return (
    <div className="rounded-md border border-edge bg-ink/20">
      <button onClick={onToggle} className="w-full text-left px-3 py-2 hover:bg-ink/40 transition-colors">
        <div className="flex items-center justify-between gap-2">
          <div className="flex items-center gap-2 min-w-0">
            <span className="shrink-0 text-gray-500 w-3">{collapsed ? "▸" : "▾"}</span>
            <span className="font-mono text-xs truncate text-gray-300" title={group.session_id ?? ""}>
              {group.session_id}
            </span>
            <span className="shrink-0 rounded bg-gray-700 text-gray-300 text-[10px] font-semibold px-1.5 py-0.5">
              {group.rows.length} calls
            </span>
          </div>
          <span className="shrink-0 text-xs font-mono text-gray-400">{formatUsdPrecise(totalUsd)}</span>
        </div>
        <div className="mt-1 flex items-center justify-between text-xs text-gray-500">
          <span>{sensitivity}</span>
          <span className="font-mono">{formatMs(totalMs)}</span>
        </div>
      </button>
      {!collapsed && (
        <div className="flex flex-col gap-1.5 px-2 pb-2">
          {group.rows.map((r) => (
            <RequestRow key={r.id} r={r} selected={r.id === selectedId} onSelect={onSelect} indent />
          ))}
        </div>
      )}
    </div>
  );
}

export function RequestList({
  requests,
  selectedId,
  onSelect,
}: {
  requests: RequestSummary[];
  selectedId: string | null;
  onSelect: (id: string) => void;
}) {
  const [collapsed, setCollapsed] = useState<Set<string>>(new Set());
  const groups = groupBySession(requests);
  const toggle = (key: string) =>
    setCollapsed((prev) => {
      const next = new Set(prev);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      return next;
    });

  return (
    <Panel title="Requests" right={<span className="text-xs text-gray-500">{requests.length}</span>} className="flex-1 min-h-0">
      {requests.length === 0 ? (
        <p className="text-sm text-gray-500">No requests yet — they appear here as connected applications call the API.</p>
      ) : (
        <div className="flex flex-col gap-1.5 overflow-y-auto max-h-[420px] pr-1">
          {groups.map((g) =>
            g.session_id === null ? (
              <RequestRow key={g.key} r={g.rows[0]} selected={g.rows[0].id === selectedId} onSelect={onSelect} />
            ) : (
              <SessionGroup
                key={g.key}
                group={g}
                collapsed={collapsed.has(g.key)}
                onToggle={() => toggle(g.key)}
                selectedId={selectedId}
                onSelect={onSelect}
              />
            ),
          )}
        </div>
      )}
    </Panel>
  );
}
```

- [ ] **Step 3: Typecheck**

Run: `cd ui && ./node_modules/.bin/tsc -b --noEmit`
Expected: no errors.

- [ ] **Step 4: Build**

Run: `cd ui && ./node_modules/.bin/vite build`
Expected: build succeeds.

- [ ] **Step 5: Commit**

```bash
git add ui/src/api.ts ui/src/components/RequestList.tsx
git commit -m "feat(b17): group requests by session into expandable tree in console"
```

---

### Task 4: Reference integration — sample tags its workflow, and close B17

**Files:**
- Modify: `test_app/routeforge_sample/routeforge.py:39-43` (add `session_id` field), `test_app/routeforge_sample/routeforge.py:122-132` (send header)
- Modify: `test_app/routeforge_sample/agent.py:1-13` (import uuid), `agent.py:55-59` (per-run id in `investigate`)
- Modify: `docs/bugs-backlog.md` (mark B17 resolved)
- Test: `test_app/tests/test_sample.py`

**Interfaces:**
- Consumes: the `X-RouteForge-Session` header contract (Task 1). Runs against the sample's own `httpx.MockTransport`, not the server.
- Produces: `RouteForgeChatModel.session_id: str | None` attribute; `SupportInvestigator.investigate` sets one `inv-<ticket>-<8hex>` id across the run's three calls.

- [ ] **Step 1: Write the failing sample tests**

Append to `test_app/tests/test_sample.py`:

```python
def test_investigation_tags_all_calls_with_one_session() -> None:
    seen: list[str | None] = []
    count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal count
        count += 1
        seen.append(request.headers.get("X-RouteForge-Session"))
        return _response(count, request)

    model = _model(handler)
    try:
        SupportInvestigator(model).investigate("TCK-1001")
    finally:
        model.close()

    assert len(seen) == 3
    assert all(s is not None for s in seen)
    assert len(set(seen)) == 1                      # all three share ONE id
    assert seen[0].startswith("inv-tck-1001-")      # ticket-derived, lowercased


def test_two_runs_get_distinct_sessions() -> None:
    seen: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers.get("X-RouteForge-Session"))
        return _response((len(seen) - 1) % 3 + 1, request)

    model = _model(handler)
    try:
        investigator = SupportInvestigator(model)
        investigator.investigate("TCK-1001")
        investigator.investigate("TCK-1001")
    finally:
        model.close()

    run1, run2 = set(seen[:3]), set(seen[3:])
    assert len(run1) == 1 and len(run2) == 1
    assert run1 != run2                             # per-run nonce -> distinct sessions
```

- [ ] **Step 2: Run it to verify it fails**

Run: `cd test_app && uv run pytest tests/test_sample.py -q -p no:cacheprovider -W ignore`
Expected: FAIL — `seen` holds `None` (no header sent yet), so the `all(... is not None)` / distinct-session asserts fail.

- [ ] **Step 3: Add the `session_id` field + header to `RouteForgeChatModel`**

In `test_app/routeforge_sample/routeforge.py`, add the field after `max_rate_limit_retries` (routeforge.py:42):

```python
    session_id: str | None = None  # B17: per-workflow-run correlation id sent as X-RouteForge-Session
```

In `_post_with_rate_limit_retry`, replace the inline `headers={...}` in the `self._client.post(...)` call (routeforge.py:125-132) with a built dict that conditionally adds the header:

```python
            headers = {
                "Authorization": f"Bearer {self.api_key.get_secret_value()}",
                "Content-Type": "application/json",
            }
            if self.session_id:
                headers["X-RouteForge-Session"] = self.session_id
            response = self._client.post(
                f"{self.base_url}/chat/completions",
                headers=headers,
                json=payload,
            )
```

- [ ] **Step 4: Set a per-run id in `SupportInvestigator.investigate`**

In `test_app/routeforge_sample/agent.py`, add `import uuid` to the imports (after `import json` at agent.py:5). Replace `investigate` (agent.py:55-59) with:

```python
    def investigate(self, ticket_id: str) -> InvestigationResult:
        audit_start = len(self.model.audit_records)
        # One correlation id for the whole run so RouteForge groups the three
        # routed calls under one session (console tree + Langfuse Session). The
        # uuid nonce makes each run distinct — two runs of the same ticket are
        # two sessions.
        self.model.session_id = f"inv-{ticket_id.lower()}-{uuid.uuid4().hex[:8]}"
        try:
            state = self._workflow.invoke({"ticket_id": ticket_id.upper()})
        finally:
            self.model.session_id = None
        calls = self.model.audit_records[audit_start:]
        return InvestigationResult(ticket_id=state["ticket_id"], answer=state["answer"], calls=calls)
```

- [ ] **Step 5: Run the sample tests to verify pass**

Run: `cd test_app && uv run pytest tests/test_sample.py -q -p no:cacheprovider -W ignore`
Expected: PASS (new grouping tests pass; the existing `test_agent_runs_three_independently_traced_calls` and `test_503_is_exposed_as_fail_closed_policy_error` still green).

- [ ] **Step 6: Mark B17 resolved in the backlog**

In `docs/bugs-backlog.md`, find the B17 entry and add a resolved marker matching the style already used for resolved items (e.g. B5) — a bold `**RESOLVED 2026-08-06**` lead on the entry with a one-line note:

> **RESOLVED 2026-08-06 (this branch).** Client tags a workflow run via `X-RouteForge-Session`; RouteForge stores it on `RequestSummary`, exposes it on `/requests` + drilldown, groups rows into an expandable tree, and stamps `langfuse.session.id`. In-memory only — durable/cross-replica grouping stays B3. Spec: `docs/superpowers/specs/2026-08-06-b17-request-grouping-design.md`.

- [ ] **Step 7: Commit**

```bash
git add test_app/routeforge_sample/routeforge.py test_app/routeforge_sample/agent.py test_app/tests/test_sample.py docs/bugs-backlog.md
git commit -m "feat(b17): sample tags each investigation run with one session id; close B17"
```

---

## Self-Review

**Spec coverage:**
- Header carrier + sanitization (fail-open, ≤128, `[A-Za-z0-9._:-]`) → Task 1.
- `RequestSummary.session_id`, in-memory only, exposed on `/requests` + drilldown → Task 1.
- `langfuse.session.id` on root span, not redacted → Task 2.
- Not added to `REDACT_KEYS` → asserted implicitly by Task 2's "not redacted" test (mem_provider wraps `RedactingSpanExporter`); no `otel.py` change is made, which is correct.
- UI type + expandable parent→children tree, ungrouped flat → Task 3.
- Reference integration (header attr + per-run id) → Task 4.
- Docs close → Task 4 Step 6.
- Out-of-scope items (DB column, server-side multi-step, policy change, stats/fleet) — none touched: `record`/`list`/`get`/`fleet`/`stats` signatures unchanged, no DB code, no policy code. ✓

**Placeholder scan:** No TBD/TODO; every code step has real code. Docs step (Task 4 Step 6) references the resolved-marker style rather than pasting the whole backlog — acceptable, it's a prose doc edit with the exact text to add given inline. ✓

**Type consistency:** `_sanitize_session_id`/`_read_session_id` names match between Task 1 definition and its unit test; `session_id` field name identical across `RequestSummary` (Py), the resp dict key, `export_trace` param, `api.ts` (`session_id`), and the header `X-RouteForge-Session` everywhere; `RouteForgeChatModel.session_id` matches its use in `investigate`. `groupBySession`/`RequestRow`/`SessionGroup` are internal to `RequestList.tsx`, consistent within the file. ✓
