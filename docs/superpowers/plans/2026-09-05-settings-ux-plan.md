# Plan — make the app Settings page usable by a non-expert

**Owner directive (2026-09-05), after MillWise was configured successfully:**
"the UI is very crowded. It will be difficult for anyone to use the UI for config
setting for an app… design the UI/UX so that anyone can understand, use it to set the
config. May be add numbering 1, 2, etc to set each config data." Plus: document the
settings so new apps know what to use.

## What is actually wrong

The Settings tab (`AppConfig`, `ui/src/admin/ApplicationsPanel.tsx`) renders eight
regions as one flat ~1800px scroll. Every field is expanded, at the same visual weight,
with permanently-visible 10px explanatory prose. Specific defects:

1. **No hierarchy.** `TOKENS`, `LANGFUSE CREDENTIALS`, `POLICY`, `PRIVACY RULES` are all
   the same small grey uppercase label. Nothing says which matter or in what order.
2. **Help text is the bulk of the page.** Nearly every control carries 1–3 lines of
   always-on grey prose. The text is accurate and hard-won — it must not be deleted,
   only moved behind a per-field disclosure.
3. **Nothing is collapsed.** `exempt types` alone renders 16 chips. Under
   `policy_mode: passthrough` that whole Detection & narrowing group is *inert* and
   still occupies a full screen of dimmed controls.
4. **Related things are far apart.** Langfuse credentials are at the very top;
   `capture_io` is ~1000px below — yet `full` is refused without those credentials.
   The dependency is invisible.
5. **Three separate save buttons** (credentials / policy / privacy rules) with no
   indication of which fields each covers, or which have unsaved edits.
6. **No answer to "what is this app set to?"** without reading the whole page.

## Design

**Keep the existing palette and typography** (`ui/src/tokens.ts`, the console's dark
theme). This is one page inside a console; a new palette would fracture it. The UI skill
run returned a landing-page pattern — ignored as irrelevant. What is taken from it: the
accessibility rules (labels tied to controls, `focus-visible` rings, `aria-label` on
icon-only buttons, `aria-expanded` on disclosures, keyboard operability, 150–300ms
transitions, no emoji as icons).

### Numbered steps, collapsed by default, each with a summary line

```
Millwise Agent    onprem_only · passthrough · capture full · detection off
[ Start from a recipe ▾ ]

1  Access            4 active tokens                                    [edit]
2  Placement         onprem_only · all providers · all tiers            [edit]
3  Protection        passthrough — protection off                       [edit]
4  Telemetry         full → own Langfuse project ✓                      [edit]
5  Delivery & limits no streaming · Headroom inherit · no limits        [edit]
6  Privacy rules     global defaults                            advanced [edit]
```

- The **summary strip** answers "what is this app set to?" without opening anything.
- A collapsed step shows its current value, so the page is scannable at a glance.
- **Langfuse credentials move into step 4 next to `capture_io`**, since they gate it.
- Step 6 is marked advanced and stays closed unless the app has custom rules.
- Under `passthrough`, step 3's Detection & narrowing sub-group **collapses** to a single
  line ("inert while passthrough — 3 exempt types kept") instead of a dimmed wall.

### Help on demand
Each field keeps ONE short line inline. The longer explanation moves behind a small `?`
toggle (`aria-expanded`, keyboard-operable). Nothing is deleted — the prose is the
product's guidance and the reason the clamps are understandable.

### Recipes (this is the documentation, made executable)
A picker that **fills the form without saving**, so the operator reviews and then saves:

| Recipe | Sets | For |
|---|---|---|
| **Protected** (default) | `policy_mode: enforced`, `capture_io: redacted` | any app whose input may hold real personal data |
| **Router only — no PII checks** | `policy_mode: passthrough`, `capture_io: full`, `residency: onprem_only` | customer's own data, every field load-bearing (MillWise) |
| **Protected, no telemetry I/O** | `policy_mode: enforced`, `capture_io: off` | air-gapped / strictest |

Recipes must never silently widen placement, and must show what they will change before
applying. `Router only` requires the app's own Langfuse project — say so up front rather
than letting the save 422.

### Saving
Each step owns its save (the endpoints genuinely differ). A step with unsaved edits shows
a dot; the summary strip warns while anything is dirty. Save feedback stays as-is — the
server's 422 `detail` is already surfaced and must remain so.

## Tasks

1. **T-A — docs.** `docs/help/choosing-app-settings.md`: the decision guide for a new
   app, defining the three recipes and when each is right. Link from the FAQ,
   `per-app-config.md` and `ui-guide.md`.
2. **T-B — restructure `AppConfig`** into the numbered collapsible steps with summary
   lines, move Langfuse into step 4, add per-field help disclosure, collapse the inert
   detection group.
3. **T-C — recipes picker**, built on T-B's structure.
4. **T-D — build, deploy, verify.**

## Invariants
- No control may become unreachable. Everything settable today stays settable.
- The server clamps remain the only authority; the UI explains, never re-encodes
  (this is bug class B22 — do not reintroduce it).
- `passthrough` must never appear to touch placement.
- Accessibility is not optional: labels, focus-visible, aria on every disclosure.
