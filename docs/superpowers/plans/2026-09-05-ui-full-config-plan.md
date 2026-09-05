# Plan — every per-app setting configurable from the console UI

**Owner directive (2026-09-05):** "I want ALL the config setup through UI. Along the
way fix other UI bugs that was documented."

**Trigger:** MillWise needs `policy_mode: passthrough` + `capture_io: full`. Neither is
settable in the console — `policy_mode` has no control at all, and the `full` option is
greyed out by a stale rule that `policy_mode` replaced. The owner does not want to use
curl.

## Current gap (verified 2026-09-05 against `app/api/admin_routes.py`)

`PolicyIn` / `_policy_dict` expose 14 fields. `AdminPolicy` in `ui/src/api.ts` declares 8.
Missing from the UI entirely:

| Field | Since | Consequence today |
|---|---|---|
| `policy_mode` | PR #34 (`0.24.0`) | **Blocker.** The passthrough posture is unreachable from the UI. |
| `pii_detection` | PR #17 (`0.14.0`) | Backend shipped; no control was ever added. |
| `stream_mode` | PR #17 (`0.14.0`) | Same. |
| `pii_entity_denylist` | `0.20.x` | Set on MillWise over curl; invisible and un-editable in the console. |
| `pii_score_threshold` | `0.20.x` | Same. |
| `pii_exempt_patterns` | `0.22.0` | Same. |

## Documented UI bugs folded in

- **`ui-open-items.md` #7** — the Application ID is unlabelled and not copyable. Still TODO.
- **`bugs-backlog.md` B21** — an onboarded model can't be edited in the console (only
  enable/disable/delete). `PUT /admin/infra/models/{slug}` exists; the fix is UI-only.

## Bugs found while reading the code (not previously documented)

- **Stale `capture_io=full` gate.** `ApplicationsPanel.tsx:161-163` disables `full` when
  `residency === "onprem_only"`. That mirrors the pre-`policy_mode` server rule. The
  server now permits `onprem_only` + `passthrough` + `full` — the UI must not forbid what
  the API allows.
- **`PolicyEditor.save()` swallows errors.** No `catch`, no error state: a 422 from the
  admin clamps (the authority on every posture decision) leaves the button returning to
  "Save policy" with nothing changed and nothing said.

## Tasks

1. **T1 — plumbing.** Complete `AdminPolicy` (6 fields); add save-error surfacing to
   `PolicyEditor`; correct the `capture_io` eligibility logic to mirror the server.
2. **T2 — protection + streaming controls.** `policy_mode`, `pii_detection`,
   `pii_entity_denylist`, `pii_score_threshold`, `pii_exempt_patterns`, `stream_mode`.
3. **T3 — `ui-open-items` #7.** Application ID label + copy button.
4. **T4 — B21.** Edit an onboarded model in `InfrastructurePanel`.
5. **T5 — docs.** Close #7 and B21; record the two new bugs as fixed; refresh
   `docs/help/ui-guide.md` and `docs/per-app-config.md` with the UI path.
6. **T6 — build, deploy `serving-plane-ui`, verify, roll.**

## Invariants the UI must not contradict

The server clamps are the authority; the UI's job is to *explain* them, never to invent
a stricter or looser rule of its own.

- `passthrough` must never appear to touch placement. Residency, providers and models
  stay independently settable and visibly unaffected (CLAUDE.md rule 2a).
- `capture_io=full` requires the app's own Langfuse project, always.
- `capture_io=redacted` is invalid whenever nothing is sanitized (`passthrough`, or
  `pii_detection` `off`/`regex`).
- `US_SSN` can never be denylisted or exempted (`PROTECTED_ENTITIES`).
- A no-protection posture is never silent — the UI must show the app's posture where an
  operator will see it, not bury it in a dropdown.
