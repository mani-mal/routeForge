# F1 Phase C — Symmetric Display + Admin Reporting + Manual Langfuse Creds

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the console drilldown show request *and* response bodies symmetrically (governed by the effective `capture_io` mode), add a durable cross-app usage rollup for admins, and let an air-gapped operator attach per-app Langfuse credentials by hand.

**Architecture:** Three independent parts sharing the Phase-A/B data model. Part 1 attaches mode-aware `display` bodies to the stored request record (first-party, in-process history only — never a new telemetry egress) and renders them symmetrically. Part 2 adds a Python-aggregated `UsageRecord` rollup grouped by `application_id`, sourced from RouteForge's own durable store (no second Langfuse project). Part 3 adds a per-app `langfuse_host` column + manual-creds admin path so `load_app_langfuse` resolves without the org auto-provision flow.

**Tech Stack:** Python 3.12 / FastAPI / SQLAlchemy async / Pydantic v2; React + Vite + TypeScript. Reuse `app.obs.redact.redact`, `app.apps.langfuse_provision.encrypt_secret`. No new deps.

## Global Constraints

- **Privacy invariants bind (CLAUDE.md #2/#6/#8).** The `display` bodies live only in the in-process `HistoryStore` (same residency boundary as serving) and the admin-gated `/api/requests/{id}`. This is a first-party UI view, modelled separately from Langfuse export (spec §2 invariant 5) — it must **not** change what `export_trace` sends, and must **never** be a new egress path.
- **Display honesty = effective mode.** `off` → neither body (metadata only); `redacted` → both bodies masked (input = gate's sanitized prompt, output = `redact(completion)`); `full` → both raw. Never show more than the mode permits.
- **Secret keys never leave the server.** No API response, serializer, or UI type ever returns `langfuse_secret_key_enc` or a decrypted secret. Manual-creds writes go in; only `project_id` / `host` / a boolean `has_langfuse_keys` come back.
- **Canonical values unchanged.** `capture_io` ∈ `off|redacted|full`; provider `onprem`; tiers `cheap|mid|frontier`; sensitivity enum from `policy.yaml`. No drift; no concern owned by two config files.
- **Portable aggregation.** Group/sum in Python over ORM rows (as `usage_summary` already does), not DB-specific SQL — the suite runs SQLite, production is Postgres.
- **No migration tool.** The new `Application.langfuse_host` column needs a manual `ALTER TABLE applications ADD COLUMN langfuse_host VARCHAR(255)` on deployed DBs (memory `routeforge-schema-no-migrations`); `create_all` covers fresh DBs. Call it out in the PR.
- **UI has no test framework** (Python/pytest only). UI-only tasks gate on a clean `npm run build` (tsc) in `ui/`, not a unit test.
- **The `resp` dict is both the client response and the history record.** Attach `display` additively (consistent with the existing non-standard `langfuse_url` / `session_id` fields) — never mutate or redact `resp["choices"]`, which is the caller's own completion.

---

### Task 1: Server — mode-aware `display` bodies on the request record

**Files:**
- Modify: `app/api/routes.py` (the `/v1/chat/completions` handler, around `:195-248`)
- Test: `tests/test_request_display.py` (create)

**Interfaces:**
- Consumes: effective `capture_io` (computed `routes.py:195-201`), `prompt` (`:202`), `sanitized_prompt` (`:203`), `completion` (`:205-208`); `redact` from `app.obs.redact`.
- Produces: `resp["display"] = {"mode": str, "input": str | None, "output": str | None}` — attached **before** `history.record(...)` (`:248`). `input`/`output` are `None` when `mode == "off"`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_request_display.py
from tests.conftest import _login  # noqa: F401  (pattern reuse; see test_admin_api)


def _make_app_with_mode(client, mode):
    """Create an app, set capture_io=mode, return (app_id, token)."""
    _login(client)
    r = client.post("/admin/applications", json={"name": f"disp-{mode}", "owner_email": "a@b.c"})
    app_id = r.json()["id"]; token = r.json()["token"]
    client.put(f"/admin/applications/{app_id}/policy", json={"capture_io": mode})
    return app_id, token


def _chat(client, token, text):
    r = client.post("/v1/chat/completions",
                    headers={"Authorization": f"Bearer {token}"},
                    json={"model": "auto", "messages": [{"role": "user", "content": text}]})
    assert r.status_code == 200, r.text
    return r.json()


def test_display_redacted_masks_both_bodies(client):
    app_id, token = _make_app_with_mode(client, "redacted")
    resp = _chat(client, token, "my ssn is 123-45-6789 please help")
    rid = resp["id"]
    _login(client)
    detail = client.get(f"/api/requests/{rid}").json()
    disp = detail["display"]
    assert disp["mode"] == "redacted"
    assert disp["input"] is not None and disp["output"] is not None
    assert "123-45-6789" not in disp["input"]   # raw entity never in the display input
    assert "123-45-6789" not in disp["output"]  # completion masked too


def test_display_off_shows_no_bodies(client):
    app_id, token = _make_app_with_mode(client, "off")
    resp = _chat(client, token, "hello there")
    rid = resp["id"]
    _login(client)
    detail = client.get(f"/api/requests/{rid}").json()
    assert detail["display"]["mode"] == "off"
    assert detail["display"]["input"] is None
    assert detail["display"]["output"] is None
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_request_display.py -v`
Expected: FAIL — `KeyError: 'display'` (field not attached yet).

- [ ] **Step 3: Attach the display block in `routes.py`**

Immediately after `completion` is resolved (`routes.py:~208`, before the `export_trace(...)` call), add:

```python
        # First-party console view (Phase C Part 1). Mode-appropriate bodies for
        # the admin drilldown ONLY — this is history/HistoryStore, same residency
        # boundary as serving, NOT a telemetry egress (export_trace is unchanged).
        # `choices[]` stays raw for the calling client; this is additive metadata.
        if capture_io == "full":
            resp["display"] = {"mode": "full", "input": prompt, "output": completion}
        elif capture_io == "redacted":
            resp["display"] = {
                "mode": "redacted",
                "input": sanitized_prompt or redact(prompt),
                "output": redact(completion),
            }
        else:  # "off"
            resp["display"] = {"mode": "off", "input": None, "output": None}
```

Add `redact` to the existing import from `app.obs.redact` at the top of `routes.py` if not already imported (check the import block; `export.py` imports it from `app.obs.redact`).

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest tests/test_request_display.py -v`
Expected: PASS (both tests).

- [ ] **Step 5: Regression — full suite**

Run: `uv run pytest tests/test_capture_io.py tests/test_app_policy.py tests/test_admin_api.py -q`
Expected: PASS (display is additive; nothing else reads `resp["display"]`).

- [ ] **Step 6: Commit**

```bash
git add app/api/routes.py tests/test_request_display.py
git commit -m "feat(console): mode-aware display bodies on request record (F1 Phase C)"
```

---

### Task 2: UI — symmetric drilldown display driven by `display.mode`

**Files:**
- Modify: `ui/src/api.ts` (`ChatResponse` interface, `:69-89`)
- Modify: `ui/src/console/RequestDrilldown.tsx` (request step `:143-154`; response step `:297-309`)
- Verify: `ui/` builds (`npm run build`)

**Interfaces:**
- Consumes: `detail.display: { mode: string; input: string | null; output: string | null }` from Task 1.
- Produces: symmetric request/response panels; the Phase-A caption becomes `display.mode`-keyed.

- [ ] **Step 1: Add `display` to `ChatResponse`**

In `ui/src/api.ts`, extend the `ChatResponse` interface (after `langfuse_url?`):

```ts
  display?: { mode: "off" | "redacted" | "full"; input: string | null; output: string | null };
```

- [ ] **Step 2: Render the request body symmetrically**

In `RequestDrilldown.tsx`, in the `"request"` step body (`:143-154`), replace the mode-agnostic Phase-A caption with a body + mode-keyed caption:

```tsx
{detail?.display?.mode === "off" && (
  <p className="text-xs text-gray-500 pt-2">
    Telemetry capture is off for this application — the request body is not stored.
    Nothing external sees the request until the policy gate clears it.
  </p>
)}
{detail?.display?.input != null && (
  <>
    <div className="pt-2 text-xs text-gray-400 whitespace-pre-wrap break-words max-h-40 overflow-y-auto">
      {detail.display.input}
    </div>
    <p className="text-xs text-gray-500 pt-1">
      {detail.display.mode === "full"
        ? "Raw request stored (this app has its own residency-local Langfuse project)."
        : "Gate-sanitized request — entity values masked; never more than the model received."}
    </p>
  </>
)}
```

- [ ] **Step 3: Render the response body from `display.output`**

In the `"response"` step (`:305-307`), replace the raw `detail.choices[0]?.message.content` render with the mode-appropriate body:

```tsx
{detail?.display?.output != null && (
  <div className="pt-2 text-xs text-gray-400 whitespace-pre-wrap break-words max-h-40 overflow-y-auto">
    {detail.display.output}
  </div>
)}
```

(If `detail.display` is absent — e.g. an old record before this deploy — fall back to the existing `choices[]` render so historical rows don't blank out. Keep the old expression behind a `detail?.display == null && ...` guard.)

- [ ] **Step 4: Typecheck / build**

Run: `cd ui && npm run build`
Expected: clean build, no TS errors.

- [ ] **Step 5: Commit**

```bash
git add ui/src/api.ts ui/src/console/RequestDrilldown.tsx
git commit -m "feat(console): symmetric request/response drilldown by capture mode (F1 Phase C)"
```

---

### Task 3: Server — durable cross-app usage rollup query + endpoint

**Files:**
- Modify: `app/apps/store.py` (add `usage_by_application`, near `usage_summary` `:211-246`)
- Modify: `app/api/admin_routes.py` (add `GET /admin/usage/by-application`, near `:324-332`)
- Test: `tests/test_admin_usage_by_app.py` (create)

**Interfaces:**
- Consumes: `UsageRecord` rows (`app/apps/models.py:146-175`): `application_id, ts, model, tier, cost_usd, baseline_usd, total_tokens, onprem_only, sensitivity, status`.
- Produces: `store.usage_by_application(session, since: datetime | None) -> list[dict]`, one dict per app: `{application_id, count, rejected_count, total_usd, baseline_usd, total_tokens, onprem_count, tier_distribution, model_distribution, sensitivity_distribution}`. Endpoint returns `{"apps": [...], "window_hours": int | None}`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_admin_usage_by_app.py
from tests.conftest import _login  # noqa: F401


def _seed(client, name):
    _login(client)
    r = client.post("/admin/applications", json={"name": name, "owner_email": "a@b.c"})
    return r.json()["id"], r.json()["token"]


def _chat(client, token, text="hello"):
    return client.post("/v1/chat/completions",
                       headers={"Authorization": f"Bearer {token}"},
                       json={"model": "auto", "messages": [{"role": "user", "content": text}]})


def test_by_application_rollup_separates_apps(client):
    a_id, a_tok = _seed(client, "alpha")
    b_id, b_tok = _seed(client, "beta")
    _chat(client, a_tok); _chat(client, a_tok); _chat(client, b_tok)
    _login(client)
    body = client.get("/admin/usage/by-application").json()
    by_id = {row["application_id"]: row for row in body["apps"]}
    assert by_id[a_id]["count"] == 2
    assert by_id[b_id]["count"] == 1
    assert by_id[a_id]["total_usd"] >= 0.0
    assert "tier_distribution" in by_id[a_id]


def test_by_application_reconciles_with_usage_summary(client):
    a_id, a_tok = _seed(client, "gamma")
    _chat(client, a_tok); _chat(client, a_tok)
    _login(client)
    per_app = {r["application_id"]: r for r in client.get("/admin/usage/by-application").json()["apps"]}
    scoped = client.get(f"/admin/usage?application_id={a_id}").json()
    assert per_app[a_id]["count"] == scoped["count"]
    assert abs(per_app[a_id]["total_usd"] - scoped["total_usd"]) < 1e-9
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_admin_usage_by_app.py -v`
Expected: FAIL — 404 (route missing).

- [ ] **Step 3: Add the store query**

In `app/apps/store.py`, add (mirror `usage_summary`'s Python-aggregation style and its `since` filter):

```python
async def usage_by_application(session, since=None) -> list[dict]:
    """App-wise rollup over UsageRecord (durable store; portable Python aggregation)."""
    stmt = select(UsageRecord)
    if since is not None:
        stmt = stmt.where(UsageRecord.ts >= since)
    rows = (await session.execute(stmt)).scalars().all()
    agg: dict[str, dict] = {}
    for r in rows:
        a = agg.setdefault(r.application_id, {
            "application_id": r.application_id, "count": 0, "rejected_count": 0,
            "total_usd": 0.0, "baseline_usd": 0.0, "total_tokens": 0, "onprem_count": 0,
            "tier_distribution": {}, "model_distribution": {}, "sensitivity_distribution": {},
        })
        a["count"] += 1
        if r.status == "rejected":
            a["rejected_count"] += 1
        a["total_usd"] += float(r.cost_usd or 0.0)
        a["baseline_usd"] += float(r.baseline_usd or 0.0)
        a["total_tokens"] += int(r.total_tokens or 0)
        if r.onprem_only:
            a["onprem_count"] += 1
        for key, val in (("tier_distribution", r.tier), ("model_distribution", r.model),
                         ("sensitivity_distribution", r.sensitivity)):
            if val:
                a[key][val] = a[key].get(val, 0) + 1
    return sorted(agg.values(), key=lambda d: d["total_usd"], reverse=True)
```

Confirm `UsageRecord`, `select`, and `datetime` are already imported in `store.py` (they are — `usage_summary` uses them).

- [ ] **Step 4: Add the endpoint**

In `app/api/admin_routes.py`, next to the `/admin/usage` route (`:324-332`), add:

```python
@router.get("/admin/usage/by-application")
async def usage_by_application(hours: int | None = None, _admin=Depends(require_admin)):
    since = None
    if hours is not None:
        since = datetime.utcnow() - timedelta(hours=hours)
    async with get_session() as session:
        apps = await store.usage_by_application(session, since=since)
    return {"apps": apps, "window_hours": hours}
```

Match the exact `Depends`/session-context pattern used by the existing `/admin/usage` handler (`:324-332`) — copy its dependency and session-open idiom verbatim; confirm `datetime`/`timedelta` imports.

- [ ] **Step 5: Run to verify it passes**

Run: `uv run pytest tests/test_admin_usage_by_app.py -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add app/apps/store.py app/api/admin_routes.py tests/test_admin_usage_by_app.py
git commit -m "feat(admin): durable cross-app usage rollup endpoint (F1 Phase C / B15)"
```

---

### Task 4: UI — admin cross-app report panel

**Files:**
- Modify: `ui/src/api.ts` (add `UsageByApp` type + `adminApi.usageByApplication`)
- Create: `ui/src/admin/CrossAppReport.tsx`
- Modify: wherever admin panels mount (follow `ApplicationsPanel` mounting in the admin page; find its parent and add a tab/section)
- Verify: `npm run build`

**Interfaces:**
- Consumes: `GET /admin/usage/by-application?hours=` → `{ apps: UsageByApp[]; window_hours: number | null }`.
- Produces: a table — per app: request count, spend (`total_usd`), baseline, **savings** (`baseline_usd - total_usd`), on-prem share, top tier.

- [ ] **Step 1: Add the client type + function**

In `ui/src/api.ts`:

```ts
export interface UsageByApp {
  application_id: string; count: number; rejected_count: number;
  total_usd: number; baseline_usd: number; total_tokens: number; onprem_count: number;
  tier_distribution: Record<string, number>;
  model_distribution: Record<string, number>;
  sensitivity_distribution: Record<string, number>;
}
```

Add to the `adminApi` object (mirror `usage`'s fetch idiom, `:259-264`):

```ts
  usageByApplication: (hours?: number): Promise<{ apps: UsageByApp[]; window_hours: number | null }> =>
    fetchJson(`/admin/usage/by-application${hours != null ? `?hours=${hours}` : ""}`),
```

(Use the same fetch helper the other `adminApi` methods use — match the file, don't introduce `fetch` directly.)

- [ ] **Step 2: Build the panel**

Create `ui/src/admin/CrossAppReport.tsx` — a component that loads on mount via `adminApi.usageByApplication()`, resolves app names by joining `adminApi.apps()` (as `FleetOverview` does), and renders a table with columns: App, Requests, Spend, Baseline, Saved, On-prem %, Top tier. Format USD to 4 decimals (match existing cost formatting in `CostSection`). Savings = `baseline_usd - total_usd`.

- [ ] **Step 3: Mount it**

Find the admin page that renders `ApplicationsPanel` (grep `ui/src/admin/` and the app router for where admin sections mount) and add `CrossAppReport` as a sibling section/tab labelled "Cross-app usage".

- [ ] **Step 4: Typecheck / build**

Run: `cd ui && npm run build`
Expected: clean build.

- [ ] **Step 5: Commit**

```bash
git add ui/src/api.ts ui/src/admin/CrossAppReport.tsx ui/src/admin/  # + the mount-point file
git commit -m "feat(admin): cross-app usage report panel (F1 Phase C / B15)"
```

---

### Task 5: Server — per-app `langfuse_host` column + manual-creds resolution

**Files:**
- Modify: `app/apps/models.py` (add `langfuse_host` to `Application`, near `:48-51`)
- Modify: `app/apps/langfuse_provision.py` (`load_app_langfuse` host resolution `:69-83`)
- Modify: `app/apps/store.py` (`update_application` allow-list `:165-167`)
- Test: `tests/test_langfuse_provision.py` (add a case near `test_load_app_langfuse` `:68`)

**Interfaces:**
- Consumes: `Application.langfuse_host` (new, nullable); env `LANGFUSE_HOST` (fallback).
- Produces: `load_app_langfuse(app)` returns an `AppLangfuse` whose `host = app.langfuse_host or os.environ.get("LANGFUSE_HOST", "")`; returns `None` only when project_id/public_key/secret unset **or** the resolved host is empty.

- [ ] **Step 1: Write the failing test**

```python
# add to tests/test_langfuse_provision.py
def test_load_app_langfuse_prefers_per_app_host(monkeypatch):
    from types import SimpleNamespace
    from app.apps.langfuse_provision import load_app_langfuse, encrypt_secret
    monkeypatch.setenv("LANGFUSE_HOST", "http://env-host:3000")
    monkeypatch.delenv("ENCRYPTION_KEY", raising=False)
    app = SimpleNamespace(
        langfuse_project_id="proj_x", langfuse_public_key="pk_x",
        langfuse_secret_key_enc=encrypt_secret("sk_x"),
        langfuse_host="http://airgap-host:3000",
    )
    creds = load_app_langfuse(app)
    assert creds is not None
    assert creds.host == "http://airgap-host:3000"   # per-app wins over env
    assert creds.project_id == "proj_x"
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_langfuse_provision.py -k prefers_per_app_host -v`
Expected: FAIL — `AttributeError` / host still from env.

- [ ] **Step 3: Add the column**

In `app/apps/models.py`, on `Application` (after the three existing langfuse columns `:48-51`):

```python
    langfuse_host: Mapped[str | None] = mapped_column(String(255), nullable=True)
```

- [ ] **Step 4: Resolve host per-app in `load_app_langfuse`**

In `app/apps/langfuse_provision.py:69-83`, replace the env-only host read with:

```python
    host = getattr(app, "langfuse_host", None) or os.environ.get("LANGFUSE_HOST", "")
    if not host:
        return None
```

Keep the existing project_id/public_key/secret guards; build `AppLangfuse(..., host=host)`.

- [ ] **Step 5: Allow-list the column for writes**

In `app/apps/store.py:165-167`, add `"langfuse_host"` to the `update_application` allow-list set (next to `langfuse_project_id`, `langfuse_public_key`, `langfuse_secret_key_enc`).

- [ ] **Step 6: Run to verify it passes**

Run: `uv run pytest tests/test_langfuse_provision.py -v`
Expected: PASS (new case + existing round-trip/provision cases still green).

- [ ] **Step 7: Commit**

```bash
git add app/apps/models.py app/apps/langfuse_provision.py app/apps/store.py tests/test_langfuse_provision.py
git commit -m "feat(langfuse): per-app langfuse_host column + manual host resolution (F1 Phase C / B15)"
```

---

### Task 6: Admin API + UI — manual per-app Langfuse creds form

**Files:**
- Modify: `app/api/admin_routes.py` (new `PUT /admin/applications/{app_id}/langfuse`; extend `_app_dict` `:57-66`)
- Modify: `ui/src/api.ts` (`AdminApp` type + `adminApi.setLangfuseCreds`)
- Modify: `ui/src/admin/ApplicationsPanel.tsx` (`AppDetail`/`AppConfig` `:280-402`)
- Test: `tests/test_admin_api.py` (add manual-creds cases)

**Interfaces:**
- Consumes: Task 5's column + resolution; `encrypt_secret`.
- Produces: `PUT /admin/applications/{id}/langfuse` body `{host, public_key, secret_key, project_id}` → stores `langfuse_host`, `langfuse_public_key`, `langfuse_project_id`, `langfuse_secret_key_enc=encrypt_secret(secret_key)`. `_app_dict` gains `langfuse_host` + `has_langfuse_keys: bool` (**never** the secret). After creds are set, the app becomes `capture_io=full`-eligible (existing Phase-B lint keys on `load_app_langfuse(app) is not None`).

- [ ] **Step 1: Write the failing tests**

```python
# add to tests/test_admin_api.py
def test_manual_langfuse_creds_enable_full_capture(client):
    _login(client)
    app_id = client.post("/admin/applications",
                         json={"name": "airgap", "owner_email": "a@b.c"}).json()["id"]
    # Before creds: full is rejected (no own project) — Phase B lint.
    r = client.put(f"/admin/applications/{app_id}/policy", json={"capture_io": "full"})
    assert r.status_code == 422
    # Attach manual creds.
    r = client.put(f"/admin/applications/{app_id}/langfuse", json={
        "host": "http://airgap:3000", "public_key": "pk", "secret_key": "sk",
        "project_id": "proj_air"})
    assert r.status_code == 200, r.text
    # Now full is permitted (residency default is not onprem_only).
    r = client.put(f"/admin/applications/{app_id}/policy", json={"capture_io": "full"})
    assert r.status_code == 200, r.text


def test_app_dict_never_returns_secret(client):
    _login(client)
    app_id = client.post("/admin/applications",
                         json={"name": "nosecret", "owner_email": "a@b.c"}).json()["id"]
    client.put(f"/admin/applications/{app_id}/langfuse", json={
        "host": "http://h:3000", "public_key": "pk", "secret_key": "TOPSECRET",
        "project_id": "p"})
    body = client.get("/admin/applications").json()
    blob = str(body)
    assert "TOPSECRET" not in blob
    app = next(a for a in body if a["id"] == app_id)  # adjust to actual list shape
    assert app["has_langfuse_keys"] is True
    assert app["langfuse_host"] == "http://h:3000"
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/test_admin_api.py -k "manual_langfuse or never_returns_secret" -v`
Expected: FAIL — 404 (route missing) / KeyError.

- [ ] **Step 3: Add the route**

In `app/api/admin_routes.py`, add a Pydantic body model and route (match the existing `set_policy`/`update_app` dependency + session idiom):

```python
class LangfuseCredsIn(BaseModel):
    host: str
    public_key: str
    secret_key: str
    project_id: str


@router.put("/admin/applications/{app_id}/langfuse")
async def set_langfuse_creds(app_id: str, body: LangfuseCredsIn, _admin=Depends(require_admin)):
    async with get_session() as session:
        app = await store.resolve_application(session, app_id)  # or the existing get-by-id
        if app is None:
            raise HTTPException(status_code=404, detail="unknown application")
        await store.update_application(
            session, app_id,
            langfuse_host=body.host, langfuse_public_key=body.public_key,
            langfuse_project_id=body.project_id,
            langfuse_secret_key_enc=encrypt_secret(body.secret_key))
        await session.commit()
    return {"ok": True}
```

Use the **exact** app-lookup helper the other admin routes use (check how `set_policy` at `:266-292` fetches the app) and import `encrypt_secret` from `app.apps.langfuse_provision`.

- [ ] **Step 4: Extend `_app_dict` — expose status, never the secret**

In `_app_dict` (`:57-66`), add:

```python
        "langfuse_host": app.langfuse_host,
        "has_langfuse_keys": bool(app.langfuse_public_key and app.langfuse_secret_key_enc),
```

Do **not** add `langfuse_secret_key_enc` or any decrypted value.

- [ ] **Step 5: Run to verify backend passes**

Run: `uv run pytest tests/test_admin_api.py -k "manual_langfuse or never_returns_secret" -v`
Expected: PASS. Then full admin suite: `uv run pytest tests/test_admin_api.py -q`.

- [ ] **Step 6: UI — manual creds form**

In `ui/src/api.ts`: add `langfuse_host?: string | null` and `has_langfuse_keys?: boolean` to `AdminApp` (`:205-215`); add `adminApi.setLangfuseCreds(appId, {host, public_key, secret_key, project_id})` (PUT). In `ApplicationsPanel.tsx` `AppDetail`/`AppConfig` (`:280-402`), add a small form (host, public key, secret key, project id) posting to `setLangfuseCreds`; on success refetch the app so the `PolicyEditor` `full`-capture gate (`:160-183`, keyed on `app.langfuse_project_id`) unlocks. Show `has_langfuse_keys` as a "credentials set ✓" indicator; never render the secret.

- [ ] **Step 7: Typecheck / build + commit**

Run: `cd ui && npm run build`
```bash
git add app/api/admin_routes.py ui/src/api.ts ui/src/admin/ApplicationsPanel.tsx tests/test_admin_api.py
git commit -m "feat(admin): manual per-app Langfuse creds form + fail-closed secret handling (F1 Phase C / B15)"
```

---

## Self-Review

**Spec coverage** (`docs/superpowers/specs/2026-08-17-capture-substrate-design.md` §Phase C):
- Symmetric display driven by effective mode → Tasks 1–2. ✅
- B15 admin cross-app reporting from own store, no second Langfuse project → Tasks 3–4. ✅
- B15 manual per-app creds, fail-closed, encrypted secret → Tasks 5–6. ✅
- Placeholder-format symmetry follow-up (Phase A): input uses gate's numbered `<MRN_1>`, output uses exporter's bare `redact()` — Task 1 keeps `display.input = sanitized_prompt` (numbered) and `display.output = redact(completion)` (bare). This is a **known residual cosmetic asymmetry inside the display block**; documented, not fixed here (reconciling would require re-numbering the completion, out of scope). Flag in the PR.

**Type consistency:** `display` shape identical in `routes.py` (Task 1), `api.ts` (Task 2), tests. `UsageByApp` keys match `usage_by_application` dict keys (Tasks 3–4). `LangfuseCredsIn` fields match the UI form + `setLangfuseCreds` (Task 6).

**Privacy check:** display bodies are history-only (Task 1 comment), export_trace untouched; secret never serialized (Task 6 Step 4 + `test_app_dict_never_returns_secret`); `full` display only reachable when the Phase-B clamp already permitted `full` (non-onprem + own project).
