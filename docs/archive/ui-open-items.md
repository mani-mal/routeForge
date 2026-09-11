# Console UI — open items / backlog

Deferred UI work surfaced while walking the per-application console. None are
blocking; listed newest-concern first. See `docs/help/ui-guide.md` for how each
region currently behaves.

---

## 1. Redesign the "What happened" summary band — DONE (shipped :0.5.0)

**Built 2026-07-30.** Replaced the run-on sentence in `PlainSummary`
(`ui/src/console/RequestDrilldown.tsx`) with labelled **outcome cards**: two
"story" cards (Privacy, Model) each carry an icon, a bold verdict, and a one-line
plain-language *why*, plus a compact trio (Verification, Cost, Latency). A
colour-coded left accent encodes each verdict's meaning — green = cloud-eligible /
verified clean, amber = sensitive-but-kept-on-prem (a WIN, not an error, so neither
green nor red), red = verification flagged, tier colour on the Model card. The Model
card's *why* now states the routing rationale ("cheapest model in the {tier} tier —
the lowest tier that met this task's risk bar"), so "cloud-eligible but ran local"
stops looking contradictory. Cost uses `formatUsdPrecise` (kills "$0.0000"); latency
sums per-hop (item 3). `AppPage` now passes the request's `RequestSummary` down so the
Model card can name the tier. Honesty rules kept: entity **types** only, no raw PII, no
CCR. The `ProviderStrip` (greyed excluded providers) is retained below the cards.

<details><summary>original write-up (the problem this solved)</summary>

**Where:** request drill-down, top band (`ui/src/console/RequestDrilldown.tsx` →
`PlainSummary`).

**Today** it renders one run-on sentence, e.g.:

> Checked for sensitive data (none) → eligible for cloud → answered by
> Qwen2.5-7B-Instruct (local) → verified clean, in 2888 ms for $0.0000.

**Why it's unclear (the problem to solve):**

- **Four unrelated ideas are mashed into one arrow-chain** — a privacy check, a
  residency verdict, the chosen model, and an output-verification result — plus latency
  and cost tacked on the end. There's no visual hierarchy; it reads as one grey run-on
  line, so nothing stands out as *the* answer to "was this private? which model? what did
  it cost?"
- **"eligible for cloud" then "answered by … (local)" looks contradictory.** A reader
  asks "if cloud was allowed, why did it run locally?" The band never says the router
  *chose* local on cost grounds — the routing rationale is invisible here.
- **"$0.0000" reads as free.** It's really "below $0.00005, rounds to zero at 4 dp" — a
  display artifact, not a claim. (Related to item 4.)
- **Double-negative / jargon:** "sensitive data (none)", "verified clean" are
  system-voice, not owner-voice; the parenthetical "(none)" is awkward.

**Direction (not final — for whoever picks this up):**

- Replace the sentence with a small set of **labelled outcome rows/chips**, each with an
  icon and a one-line *why*:
  - **Privacy** — "No PII detected · cloud-eligible" *or* "PII detected · kept on-prem
    (residency)".
  - **Model** — name + tier + the reason it won ("cheapest tier that met the risk bar"),
    so cloud-eligible-but-served-local stops being surprising.
  - **Verification** — "clean" / "flagged for review" / "not checked".
  - **Cost** — human-readable sub-cent (see item 4).
  - **Latency** — with the multi-step 0 ms caveat resolved (item 3).
- Keep it **owner-voice and scannable**; the full technical trace stays in band 2 below
  it. Honesty rules unchanged: entity **types** only, no raw PII, no CCR contents.
- Use the `frontend-design` skill and the `demo-ui` skill constraints when building.

</details>

---

## 2. No Headroom (compression) toggle in the console — DONE (per-app :0.3.0, global :0.4.0)

**Per-app half (B1, `:0.3.0`):** the app **Settings** tab has a tri-state Headroom
control — `(global default) / on / off` — backed by a nullable
`AppPolicy.compression_enabled` that overrides the live global toggle per application
(null = inherit). Safety bypasses (high-risk / restricted / min-tokens) still apply on
top; see `app/compression/__init__.py` `_effective_enabled`.

**Global half (`:0.4.0`, pending deploy):** a master Headroom switch now lives in the
console header (`ui/src/components/HeadroomToggle.tsx`, mounted in `App.tsx` when signed
in). It reads `GET /api/controls` on mount and flips via `POST /api/controls` (optimistic,
reverts on error), mutating `app.state.co_cfg` live — the next request on either the
direct or per-hop path picks it up, no restart. Per-app overrides win over it; the tooltip
says so. Item closed.

## 3. Multi-step requests report 0 ms latency — FIXED IN CODE (pending :0.4.0)

Per-hop latency was never summed into the request total because the internal agent
endpoint fakes the completion (never calls `generate`) and so recorded no `latency_ms`.
Fixed: each `agent_hop` DecisionRecord now carries the per-tier modeled latency
(`seeded_latency_ms`, single-sourced in `app/gateway/backends/base.py`), which
`routes.py` already sums. In the working tree, not yet on the live `:0.3.0` image.

## 4. Per-request cost shows "$0.0000" for cheap requests — FIXED IN CODE (pending :0.4.0)

Added `formatUsdPrecise` (`ui/src/components/common.tsx`): sub-$0.0001 per-request
figures now render in cents (e.g. `0.003¢`) instead of `$0.0000`; aggregate totals keep
4-dp `formatUsd`. Applied to the request row, the per-hop cost, the actual-spend headline,
and the "What happened" sentence. In the working tree, not yet on the live image.

## 5. Two blues collide — FIXED IN CODE (pending :0.4.0)

`ROUTING_COLOR` recoloured sky-blue `#38bdf8` → violet `#8b5cf6`
(`ui/src/console/sections/CostSection.tsx`) so it no longer reads as related to the
mid-tier dot `#3b82f6`. In the working tree, not yet on the live image.

## 6. Fleet ⚠️ "sensitive traffic not all kept on-prem" is over-eager — DONE (shipped :0.5.0)

**Fixed 2026-07-30.** The warning now fires only on a genuine residency breach — an
`onprem_only` request served **off-prem** — instead of the old
`pii_count > 0 && onprem_count < pii_count`, which conflated "any PII entity detected"
with "must be on-prem" and cried wolf on `private_cloud_ok` tenants (Acme: 161 low-risk
PII, 0 on-prem → wrongly warned).

Implementation: `HistoryStore.RequestSummary` gained a `provider` field (the chosen
model's serving provider, computed in `routes.py` and threaded through `history.record`);
`fleet()` now emits `onprem_violations = count(onprem_only AND provider ∉ local set)`
where local = `{onprem, mock, ?, -, ""}` (unknown never counts — pool integrity is the
config lint's job). `FleetOverview.warn = onprem_violations > 0`, and the glyph is now
`text-frontier` (red) with a "Residency breach" tooltip, since it's a real alarm, not a
soft nudge. `onprem_only` requests can't reach cloud by construction (Rule 3), so this is
0 in the demo — the point is it no longer misfires, and it *would* catch a real leak.
Covered by `tests/test_history_store.py::test_fleet_onprem_violation_flags_only_real_breach`
and `::test_fleet_unknown_provider_not_a_violation`.

## 7. Application id is not explicitly labelled / copyable — DONE (shipped ui:0.17.0)

**Built 2026-09-05.** The Application Detail header now renders the name on its own
line with an `APPLICATION ID` label, the id in selectable monospace, and a **copy**
button (`CopyableId` / `CopyButton` in `ui/src/admin/ApplicationsPanel.tsx`). List rows
gained an explicit `id` prefix. The one-time `TokenReveal` banner — which told the
operator to "copy it now" while giving them no way to — got the same copy button.

**The part that mattered more than the button.** `navigator.clipboard` is only defined
in a **secure context**. This console is routinely served over plain http on a NodePort
(the DGX lab reaches it at `http://<node>:30808`), where the entire API is `undefined`,
so a button wired straight to it would have done nothing at all in exactly the
environment operators use — and done it silently. `copyText()` falls back to the
`execCommand("copy")` textarea path and the button reports `copy failed` rather than
showing a "copied" tick that didn't happen.

<details><summary>original write-up (the problem this solved)</summary>

**Where:** Applications list rows and the Application Detail header
(`ui/src/admin/ApplicationsPanel.tsx` — list row line renders
`{a.id} · {residency} · {status}`; detail header renders `{app.name} ({app.id})`).

**Problem:** the app **id** is the value operators must use for every admin API call
(`PUT /admin/applications/{id}/policy`, tokens, etc.) and for scripted config, but the
UI only shows it as unlabelled grey monospace — in a list row it reads as
`millwise · onprem_only · active`, and in the header it's a parenthetical after the
name. Customers don't recognise which token *is* the id (a real operator asked "where's
the app id?" while trying to set a per-app policy denylist over curl). Nothing marks it
as "Application ID" and there's no one-click copy.

**Direction:** in the Application Detail header, add an explicit **`Application ID`**
label with the value in monospace and a copy-to-clipboard button (mirror the token-row
affordance). Optionally show a small `id: …` label in the list row too. Pure display —
no API change. Use the `demo-ui` / `frontend-design` skill constraints.

</details>
