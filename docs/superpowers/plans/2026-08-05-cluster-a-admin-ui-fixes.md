# Cluster A — Admin/UI Usability Fixes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship four small admin/console usability fixes (B6 edit app, B7 archive app, B8 `$` on model prices, B9 un-clip StatTile explanations) with no behaviour changes to routing, policy, privacy, or cost.

**Architecture:** Three of the four are frontend-only (React/Tailwind). B6 reuses the existing `PATCH /admin/applications/{id}` route and `adminApi.updateApp`; B7 is soft-archive via `status="archived"` (auth already fail-closes on any non-`active` app in `resolve_application`), so it needs only UI + list filtering + one backend regression-guard test + a code-comment fix. No DB migration.

**Tech Stack:** Python 3.12 / FastAPI / SQLAlchemy async / pytest (backend); React + Vite + TypeScript + Tailwind (frontend). No UI unit-test runner exists — frontend verification is `npm run typecheck` + `npm run build` + manual check.

## Global Constraints

- **No DB migration / no new columns.** B6/B7 reuse the existing `Application.status` column (`app/apps/models.py:45`, `String(16)`, no enum constraint).
- **Status vocabulary is `active | archived` only.** No `disabled`. Update the model comment accordingly.
- **`app.id` is never editable** — it is the stable key across tokens, policy, and history.
- **No CLAUDE.md hard rule changes.** These are cosmetic/usability only. No routing/policy/privacy/cost behaviour changes.
- **Tooltip idiom:** use the native `title` attribute on a focusable element (matches `ui/src/components/HeadroomToggle.tsx:57-61`). Do not add a tooltip library.
- **Currency idiom:** USD, per-1M framing matches `config/pricing.yaml`. Reuse `formatUsd` from `ui/src/components/common.tsx` for formatted currency; a bare `$` prefix is fine for raw per-1M input labels.
- **Frontend verification commands:** `cd ui && npm run typecheck` then `cd ui && npm run build`. Backend: `uv run pytest <path> -v`.
- **Working-tree note:** an unrelated in-flight feature (`requested_model` / `model_scope_active` across gate/context/router/pipeline) is uncommitted. Do NOT stage or revert it. Only commit files each task names.

---

### Task 1: B9 — Un-clip StatTile explanations via a `?` info tooltip

**Files:**
- Modify: `ui/src/components/common.tsx:39-47` (StatTile)
- Modify: `ui/src/console/sections/CostSection.tsx:135-144` (two density tiles)

**Interfaces:**
- Produces: `StatTile({ label, value, sub?, info? })` — new optional `info?: string` prop rendering a focusable `?` affordance whose `title` carries the full text. `sub` behaviour is unchanged (still truncates, for short captions).

- [ ] **Step 1: Add the `info` prop to StatTile**

In `ui/src/components/common.tsx`, replace the StatTile function (lines 39-47) with:

```tsx
export function StatTile({ label, value, sub, info }: { label: string; value: string; sub?: string; info?: string }) {
  return (
    <div className="bg-ink/60 border border-edge rounded-md px-3 py-2 flex flex-col gap-0.5 min-w-0">
      <span className="flex items-center gap-1 text-[11px] uppercase tracking-wide text-gray-500">
        {label}
        {info && (
          <span
            tabIndex={0}
            role="note"
            aria-label={info}
            title={info}
            className="inline-flex h-3.5 w-3.5 items-center justify-center rounded-full border border-edge text-[9px] text-gray-500 cursor-help hover:text-gray-300 hover:border-accent focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent"
          >
            ?
          </span>
        )}
      </span>
      <span className="text-xl font-bold font-mono text-gray-100 truncate">{value}</span>
      {sub && <span className="text-xs text-gray-500 truncate">{sub}</span>}
    </div>
  );
}
```

- [ ] **Step 2: Move the density explanations from `sub` to `info`**

In `ui/src/console/sections/CostSection.tsx`, change the two density StatTiles (lines 135-144) so the long explanation uses `info` instead of `sub`:

```tsx
        <StatTile
          label="Token Intelligence Density"
          value={formatDensity(tokenIntelligenceDensity)}
          info="quality delivered per 1k tokens — needs a per-model quality score"
        />
        <StatTile
          label="Cost Intelligence Density"
          value={formatDensity(costIntelligenceDensity)}
          info="quality delivered per $ — needs a per-model quality score"
        />
```

- [ ] **Step 3: Typecheck**

Run: `cd ui && npm run typecheck`
Expected: PASS, no errors.

- [ ] **Step 4: Build**

Run: `cd ui && npm run build`
Expected: PASS (tsc + vite build succeed).

- [ ] **Step 5: Manual verification**

Run the console (`/run` or existing dev flow), open the **Cost** section. Confirm each density tile shows a `?` next to its label, the tile no longer shows clipped `sub` text, and hovering/focusing the `?` reveals the full explanation via the browser tooltip.

- [ ] **Step 6: Commit**

```bash
git add ui/src/components/common.tsx ui/src/console/sections/CostSection.tsx
git commit -m "fix(ui): B9 — StatTile ? info tooltip; un-clip cost-density explanations

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 2: B8 — Show `$` on model-onboarding price fields

**Files:**
- Modify: `ui/src/admin/InfrastructurePanel.tsx:156-163` (onboarding form price labels)

**Interfaces:**
- Consumes: nothing from prior tasks.
- Produces: nothing consumed by later tasks.

Context: per-model prices are entered in the onboarding form only (labels `price / 1M input` / `price / 1M output`). They are NOT displayed read-only in the model table row, so the fix is limited to the two form labels. Values are already USD/1M (`config/pricing.yaml` convention) — this only makes the unit visible.

- [ ] **Step 1: Add `$` to the two price labels**

In `ui/src/admin/InfrastructurePanel.tsx`, change the two price label texts (lines 157 and 161):

```tsx
            <label className="flex flex-col gap-1 text-xs text-gray-500">
              price / 1M input ($)
              <input required type="number" step="any" min={0} value={priceInput} onChange={(e) => setPriceInput(e.target.value)} className={inputCls} />
            </label>
            <label className="flex flex-col gap-1 text-xs text-gray-500">
              price / 1M output ($)
              <input required type="number" step="any" min={0} value={priceOutput} onChange={(e) => setPriceOutput(e.target.value)} className={inputCls} />
            </label>
```

- [ ] **Step 2: Typecheck**

Run: `cd ui && npm run typecheck`
Expected: PASS.

- [ ] **Step 3: Build**

Run: `cd ui && npm run build`
Expected: PASS.

- [ ] **Step 4: Manual verification**

Open **Infrastructure → Onboard model**. Confirm both price inputs now read "price / 1M input ($)" and "price / 1M output ($)".

- [ ] **Step 5: Commit**

```bash
git add ui/src/admin/InfrastructurePanel.tsx
git commit -m "fix(ui): B8 — show \$ unit on model-onboarding price fields

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 3: B6 — Edit application (name, owner email, residency, status)

**Files:**
- Modify: `ui/src/admin/ApplicationsPanel.tsx` (`AppDetail`, lines 256-272)

**Interfaces:**
- Consumes: existing `adminApi.updateApp(id, body)` (`ui/src/api.ts:249`) accepting `Partial<{ name; owner_email; status; default_residency }>`, returning `AdminApp`; existing `RESIDENCIES` const (`ApplicationsPanel.tsx:10`).
- Produces: an inline edit form inside `AppDetail`; `AppDetail` still receives `{ app, onChanged }` unchanged.

- [ ] **Step 1: Replace `AppDetail` with an editable header**

In `ui/src/admin/ApplicationsPanel.tsx`, replace the `AppDetail` function (lines 256-272) with:

```tsx
function AppDetail({ app, onChanged }: { app: AdminApp; onChanged: () => void }) {
  const [editing, setEditing] = useState(false);
  const [name, setName] = useState(app.name);
  const [owner, setOwner] = useState(app.owner_email ?? "");
  const [residency, setResidency] = useState(app.default_residency);
  const [status, setStatus] = useState(app.status);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  // Re-seed fields whenever a different app is selected.
  useEffect(() => {
    setEditing(false);
    setName(app.name);
    setOwner(app.owner_email ?? "");
    setResidency(app.default_residency);
    setStatus(app.status);
    setErr(null);
  }, [app.id]);

  async function save() {
    setBusy(true);
    setErr(null);
    try {
      await adminApi.updateApp(app.id, {
        name,
        owner_email: owner,
        default_residency: residency,
        status,
      });
      setEditing(false);
      onChanged();
    } catch {
      setErr("Could not save changes.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="flex flex-col gap-4">
      <div className="flex items-start justify-between gap-2">
        {editing ? (
          <div className="flex flex-col gap-2 flex-1">
            <label className="flex flex-col gap-1 text-xs text-gray-400">
              name
              <input value={name} onChange={(e) => setName(e.target.value)}
                className="rounded bg-ink border border-edge px-2 py-1 text-sm text-gray-100 w-56" />
            </label>
            <label className="flex flex-col gap-1 text-xs text-gray-400">
              owner email
              <input value={owner} onChange={(e) => setOwner(e.target.value)} placeholder="(none)"
                className="rounded bg-ink border border-edge px-2 py-1 text-sm text-gray-100 w-56" />
            </label>
            <div className="flex items-end gap-2">
              <label className="flex flex-col gap-1 text-xs text-gray-400">
                default residency
                <select value={residency} onChange={(e) => setResidency(e.target.value)}
                  className="rounded bg-ink border border-edge px-2 py-1 text-sm text-gray-100">
                  {RESIDENCIES.map((r) => <option key={r} value={r}>{r}</option>)}
                </select>
              </label>
              <label className="flex flex-col gap-1 text-xs text-gray-400">
                status
                <select value={status} onChange={(e) => setStatus(e.target.value)}
                  className="rounded bg-ink border border-edge px-2 py-1 text-sm text-gray-100">
                  <option value="active">active</option>
                  <option value="archived">archived</option>
                </select>
              </label>
            </div>
            <div className="flex items-center gap-2">
              <button onClick={save} disabled={!name || busy}
                className="rounded-md border border-accent bg-accent/20 text-accent px-3 py-1 text-xs disabled:opacity-50">
                {busy ? "saving…" : "Save"}
              </button>
              <button onClick={() => setEditing(false)} disabled={busy}
                className="text-xs text-gray-500 hover:underline">Cancel</button>
              {err && <span className="text-xs text-frontier">{err}</span>}
            </div>
          </div>
        ) : (
          <>
            <div>
              <div className="text-sm font-semibold text-gray-100">{app.name} <span className="font-mono text-gray-500">({app.id})</span></div>
              <div className="text-xs text-gray-500">
                status {app.status} · default residency {app.default_residency}
                {app.langfuse_project_id ? ` · langfuse: ${app.langfuse_project_id}` : " · langfuse: shared"}
              </div>
            </div>
            <button onClick={() => setEditing(true)}
              className="text-xs text-accent hover:underline shrink-0">Edit</button>
          </>
        )}
      </div>

      <AppConfig appId={app.id} onChanged={onChanged} />
    </div>
  );
}
```

- [ ] **Step 2: Typecheck**

Run: `cd ui && npm run typecheck`
Expected: PASS. (`useEffect` is already imported at the top of the file.)

- [ ] **Step 3: Build**

Run: `cd ui && npm run build`
Expected: PASS.

- [ ] **Step 4: Manual verification**

Open **Applications**, select an app, click **Edit**. Change the name and residency, Save; confirm the header updates and the left-list name updates (via `onChanged` → `load`). Click Edit → Cancel; confirm no change. Confirm `id` has no input (not editable).

- [ ] **Step 5: Commit**

```bash
git add ui/src/admin/ApplicationsPanel.tsx
git commit -m "feat(ui): B6 — inline edit for application name/owner/residency/status

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 4: B7 — Archive/Restore application (soft delete) + backend guard

**Files:**
- Modify: `app/apps/models.py:45` (status comment `active|disabled` → `active|archived`)
- Modify: `tests/test_apps_store.py` (add archived-app regression-guard test)
- Modify: `ui/src/admin/ApplicationsPanel.tsx` (`AppDetail` archive/restore button + `ApplicationsPanel` list filter + show-archived toggle)

**Interfaces:**
- Consumes: `adminApi.updateApp(id, { status })` (Task 3 already uses it); `resolve_application` fail-closed on non-`active` status (`app/apps/store.py:44`, pre-existing).
- Produces: nothing consumed by later tasks.

- [ ] **Step 1: Add the archived-app regression-guard test**

In `tests/test_apps_store.py`, add this test (mirrors `test_revoked_token_and_disabled_app_are_rejected`). It locks the guarantee B7's UI relies on — an archived app cannot authenticate. It passes immediately because `resolve_application` already rejects any non-`active` status; it is a guard against that check regressing.

```python
def test_archived_app_is_rejected():
    db = _fresh_db()

    async def go():
        async with db.session() as s:
            # archiving an app must disable its tokens even though they stay active
            await s.execute(update(Application)
                            .where(Application.id == "hospital")
                            .values(status="archived"))
            await s.commit()
            assert await store.resolve_application(s, "hospital-key") is None

    _run(go())
```

- [ ] **Step 2: Run the test**

Run: `uv run pytest tests/test_apps_store.py::test_archived_app_is_rejected -v`
Expected: PASS (the `!= "active"` check in `resolve_application` already covers `archived`).

- [ ] **Step 3: Fix the status comment**

In `app/apps/models.py`, change line 45's trailing comment from `# active|disabled` to `# active|archived` on the `Application.status` column.

- [ ] **Step 4: Run the store tests to confirm nothing broke**

Run: `uv run pytest tests/test_apps_store.py -v`
Expected: PASS (all tests, including the pre-existing `test_revoked_token_and_disabled_app_are_rejected`, which uses `status="disabled"` and still asserts rejection — any non-active value is rejected, so it is unaffected).

- [ ] **Step 5: Add Archive/Restore to `AppDetail`**

In `ui/src/admin/ApplicationsPanel.tsx` `AppDetail` (from Task 3), add an archive/restore control. Inside the non-editing branch, after the `Edit` button, add a second action that flips status via a native confirm (matches the existing `window.confirm` idiom at `InfrastructurePanel.tsx:262`):

```tsx
            <div className="flex flex-col items-end gap-1 shrink-0">
              <button onClick={() => setEditing(true)}
                className="text-xs text-accent hover:underline">Edit</button>
              {app.status === "archived" ? (
                <button
                  onClick={async () => { await adminApi.updateApp(app.id, { status: "active" }); onChanged(); }}
                  className="text-xs text-cheap hover:underline">Restore</button>
              ) : (
                <button
                  onClick={async () => {
                    if (!window.confirm(`Archive ${app.name}? Its tokens stop working; you can restore it later.`)) return;
                    await adminApi.updateApp(app.id, { status: "archived" });
                    onChanged();
                  }}
                  className="text-xs text-frontier hover:underline">Archive</button>
              )}
            </div>
```

Replace the single `Edit` button in Task 3's non-editing branch with this `<div>` block (it contains the Edit button plus the archive/restore action).

- [ ] **Step 6: Filter archived apps out of the list, with a Show-archived toggle**

In `ui/src/admin/ApplicationsPanel.tsx`, update `ApplicationsPanel` to hide archived apps by default. Add a `showArchived` state and filter the list; de-emphasise archived rows when shown; clear the selection if the selected app becomes hidden.

Replace the `ApplicationsPanel` function body's list-rendering region so that:

```tsx
export function ApplicationsPanel() {
  const [apps, setApps] = useState<AdminApp[]>([]);
  const [selected, setSelected] = useState<string | null>(null);
  const [createdToken, setCreatedToken] = useState<string | null>(null);
  const [showArchived, setShowArchived] = useState(false);

  async function load() {
    const rows = await adminApi.apps();
    setApps(rows);
  }
  useEffect(() => { load().catch(() => {}); }, []);

  const visible = apps.filter((a) => showArchived || a.status !== "archived");
  // If the selected app is now hidden, drop the selection so the detail pane
  // never shows an app that isn't in the list.
  useEffect(() => {
    if (selected && !visible.some((a) => a.id === selected)) setSelected(null);
  }, [selected, showArchived, apps]);
  const current = apps.find((a) => a.id === selected) ?? null;

  return (
    <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
      <Panel title="Applications">
        <CreateApp onCreated={(t) => { setCreatedToken(t); load(); }} />
        {createdToken && <div className="mt-2"><TokenReveal token={createdToken} /></div>}
        <label className="mt-3 flex items-center gap-2 text-xs text-gray-500">
          <input type="checkbox" checked={showArchived} onChange={(e) => setShowArchived(e.target.checked)} />
          Show archived
        </label>
        <ul className="mt-2 flex flex-col gap-1">
          {visible.map((a) => (
            <li key={a.id}>
              <button onClick={() => setSelected(a.id)}
                className={`w-full text-left rounded-md border px-3 py-2 transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent ${selected === a.id ? "border-accent bg-accent/5" : "border-edge hover:border-accent"} ${a.status === "archived" ? "opacity-50" : ""}`}>
                <div className="flex items-center justify-between">
                  <span className="text-sm text-gray-100">{a.name}{a.status === "archived" && <span className="ml-1 text-[10px] uppercase tracking-wide text-gray-500">archived</span>}</span>
                  <span className="text-xs font-mono text-gray-500">{a.tokens.filter((t) => t.status === "active").length} tokens</span>
                </div>
                <div className="text-xs text-gray-500">
                  <span className="font-mono">{a.id}</span> · {a.default_residency} · {a.status}
                </div>
              </button>
            </li>
          ))}
          {visible.length === 0 && <li className="text-sm text-gray-500">No applications{showArchived ? "" : " (archived hidden)"}.</li>}
        </ul>
      </Panel>

      <Panel title="Application detail">
        {current ? (
          <AppDetail app={current} onChanged={load} />
        ) : (
          <p className="text-sm text-gray-500">Select an application to manage its tokens and policy.</p>
        )}
      </Panel>
    </div>
  );
}
```

- [ ] **Step 7: Typecheck**

Run: `cd ui && npm run typecheck`
Expected: PASS.

- [ ] **Step 8: Build**

Run: `cd ui && npm run build`
Expected: PASS.

- [ ] **Step 9: Manual verification**

Open **Applications**. Archive an app → confirm the native confirm appears, and on OK the app disappears from the default list. Toggle **Show archived** → the app reappears dimmed with an `archived` badge and a **Restore** action; Restore returns it to `active` and un-dims it. Confirm a token for an archived app is rejected (401) if you exercise the chat path.

- [ ] **Step 10: Commit**

```bash
git add app/apps/models.py tests/test_apps_store.py ui/src/admin/ApplicationsPanel.tsx
git commit -m "feat: B7 — archive/restore applications (soft delete via status=archived)

Auth already fail-closes on non-active apps (resolve_application); archive is
status=archived + client-side list filtering. Adds a regression-guard test and
fixes the status comment (active|archived).

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Self-Review

**Spec coverage:**
- B6 (edit name/owner/residency/status) → Task 3. ✓
- B7 (archive/restore, list filter, no backend cascade, guard test, `active|archived`) → Task 4. ✓
- B8 (`$` on model prices) → Task 2. ✓
- B9 (`?` info tooltip, move density explanations) → Task 1. ✓
- No migration / reuse `status` / no hard-rule change → Global Constraints + observed in each task. ✓

**Placeholder scan:** No TBD/TODO/"handle edge cases"; every step has concrete code or an exact command. ✓

**Type consistency:** `adminApi.updateApp(id, Partial<{name; owner_email; status; default_residency}>)` used identically in Tasks 3 and 4; `StatTile` `info?: string` defined in Task 1 Step 1 and consumed in Task 1 Step 2; `AppDetail({app, onChanged})` and `AppConfig({appId, onChanged})` signatures unchanged. Task 4 Step 5 modifies the `AppDetail` produced by Task 3 (do Task 3 before Task 4). ✓

**Ordering note:** Tasks 1 and 2 are fully independent. Task 4 edits the `AppDetail` created in Task 3 and must run after it.
