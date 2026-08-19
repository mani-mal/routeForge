# Cluster A — Admin/UI usability fixes (B6–B9)

**Date:** 2026-08-05
**Status:** design, ready for implementation plan
**Source items:** `docs/bugs-backlog.md` B6, B7, B8, B9
**Related, out of this spec:** B17 (grouped request tree) logged in `docs/bugs-backlog.md`,
gets its own spec later.

## Purpose

Four small, independent admin/console usability fixes surfaced 2026-08-04 while walking
the per-application console. None change routing, policy, privacy, or cost behaviour —
they are usability/clarity corrections. Grouped into one spec because each is small and
they share the same two UI files.

Scope guard: cosmetic/usability only. **No DB migration** (no new columns — B6/B7 reuse
the existing `Application.status`). No change to any CLAUDE.md hard rule.

## Pre-work findings (grounding)

- **B6 backend already exists.** `PATCH /admin/applications/{id}` (`app/api/admin_routes.py:252`)
  accepts `name` / `owner_email` / `status` / `default_residency`, and the frontend
  client `adminApi.updateApp` (`ui/src/api.ts:249`) already calls it. B6 is a **pure UI
  gap** — `AppDetail` renders the name read-only with no edit control.
- **B7 needs no new backend.** Auth already fail-closes on any non-`active` app:
  `resolve_application` returns `None` when `token.application.status != "active"`
  (`app/apps/store.py:44`), so setting `status="archived"` immediately blocks the app's
  tokens (401). Archiving is therefore `updateApp(id, {status: "archived"})` over the
  existing route + client-side list filtering. `store.update_application` already
  whitelists `status` (`app/apps/store.py:165`).
- **Status vocabulary:** `active | archived` only (owner decision — `disabled` dropped).
  Model comment at `app/apps/models.py:45` currently reads `active|disabled`; update it
  to `active|archived`. `String(16)`, no enum constraint, so no schema change.
- **B8 location:** the unlabeled per-1M prices are the **model-onboarding** form in
  `ui/src/admin/InfrastructurePanel.tsx` (input labels `price / 1M input` / `price / 1M
  output` at lines 157/161, plus the displayed prices ~lines 242–243). The backlog said
  "application setup screen" loosely; the price fields live in the Infrastructure panel.
- **B9 cause:** `StatTile` (`ui/src/components/common.tsx:44`) hard-codes `truncate` on
  the `sub` line, clipping any explanation longer than the tile.

## The four changes

### B6 — Edit application (name, owner email, residency, status)

**Files:** `ui/src/admin/ApplicationsPanel.tsx` (and possibly a tiny `adminApi` type
widen if needed — the method already exists).

`AppDetail` gains an **Edit** affordance. Clicking it swaps the read-only header
(`app.name (app.id)` + status/residency line) for an inline edit form:

- **name** — text input (required, non-empty).
- **owner email** — text input (optional).
- **default residency** — `select` over the existing `RESIDENCIES`
  (`any | private_cloud_ok | onprem_only`).
- **status** — control over `active | archived` (this is the same lever B7's Archive
  button uses; see below). Present as a labelled select or a toggle.

Save calls `adminApi.updateApp(app.id, {...})`; on success, call `onChanged()` to
refresh the list + detail. Cancel restores the read-only header with no call. `app.id`
is **not** editable (it is the stable key across tokens, policy, history).

Reuse existing input styling (`bg-ink border border-edge`), the `busy` disabled pattern,
and an inline error span on failure (mirror `CreateApp`).

### B7 — Archive application (soft delete)

**Files:** `ui/src/admin/ApplicationsPanel.tsx` only. No backend, no store, no cascade.

- **Archive action:** a button in `AppDetail` → `updateApp(id, {status: "archived"})` →
  refresh. Optionally a lightweight confirm (native `confirm()` or an inline
  "Archive? [confirm]" toggle) so it is not a one-click accident.
- **List filtering:** `ApplicationsPanel`'s left list hides `status === "archived"` rows
  by default. Add a **"Show archived"** checkbox/toggle above the list; when on,
  archived rows appear visually de-emphasised (dimmed) with a **Restore** action
  (`updateApp(id, {status: "active"})`).
- **Selection edge case:** if the currently-selected app is archived while "Show
  archived" is off, clear the selection (detail pane returns to its empty state) so the
  detail pane never shows an app hidden from the list.
- Usage/decision history is untouched — rows keep their `app_id`; nothing is destroyed;
  the action is fully reversible via Restore.

### B8 — Show `$` on model prices

**Files:** `ui/src/admin/InfrastructurePanel.tsx` only.

- Onboarding form: relabel the two price inputs so the unit is explicit — e.g.
  `price / 1M input ($)` and `price / 1M output ($)` (or a `$` input adornment). Keep
  the "/ 1M" framing, which matches `pricing.yaml`'s USD-per-1M convention.
- Displayed per-model prices in the onboarded-models list: prefix `$` (reuse `formatUsd`
  from `common.tsx` where a formatted currency string is wanted, or a plain `$` prefix
  for the raw per-1M figure — pick one and apply consistently).
- No numeric/data change — values already are USD/1M; this only makes the unit visible.

### B9 — Un-clip StatTile explanations via a `?` info tooltip

**Files:** `ui/src/components/common.tsx` (StatTile), `ui/src/console/sections/CostSection.tsx`
(the two density tiles).

- Add an optional prop to `StatTile`: `info?: string`. When present, render a small `?`
  affordance next to the label with a hover/focus-revealed tooltip carrying the full
  text. Keep `sub` for short captions that fit; `info` is for the long explanation.
- Accessibility: the `?` is focusable (`tabIndex`/`<button>`), exposes the text via
  `title`/`aria-label`, and the styled tooltip shows on hover **and** keyboard focus.
  Match the existing tooltip pattern used by `HeadroomToggle`.
- `CostSection.tsx`: move the "Token Intelligence Density" / "Cost Intelligence Density"
  explanation strings from `sub` into `info`. Tiles keep a uniform height; the full text
  is available on demand.

## What is explicitly NOT in scope

- No new DB columns, no migration (reuses `Application.status`).
- No hard-delete / cascade logic (owner chose soft archive).
- No `disabled` status (owner chose `active | archived`).
- B17 grouped/tree request view — separate spec.
- The in-flight uncommitted "explicit model-selection" work (`requested_model` /
  `model_scope_active` across gate/context/router/pipeline) is unrelated; this spec
  touches `ApplicationsPanel.tsx`, `InfrastructurePanel.tsx`, `common.tsx`,
  `CostSection.tsx`, and `app/apps/models.py` (comment only) — coordinate but do not
  depend on it.

## Testing

- **Backend:** add one focused test that `resolve_application` returns `None` for an
  `archived` app (locks the B7 archive-disables-auth guarantee that the UI relies on).
  The existing `update_application` route test already covers the PATCH path.
- **Frontend:** no test harness change assumed beyond the existing typecheck/build;
  verify B6 edit round-trips, B7 archive removes the app from the default list and
  Restore returns it, B8 shows `$`, B9 shows the full text in the tooltip. If a UI test
  runner exists, add a render assertion for the archived-list-filter and the tooltip
  prop; otherwise rely on typecheck + manual verification in `/run`.

## Build order (independent; any order works)

1. B9 (`StatTile` `info` prop + CostSection) — smallest, self-contained.
2. B8 (`InfrastructurePanel` `$` labels) — self-contained.
3. B6 (edit control) — sets up the status lever.
4. B7 (archive/restore + list filter + backend test) — builds on B6's status lever.

Each is an independent SDD task; a fresh subagent per task with review between.
