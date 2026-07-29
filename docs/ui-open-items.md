# Console UI — open items / backlog

Deferred UI work surfaced while walking the per-application console. None are
blocking; listed newest-concern first. See `docs/help/ui-guide.md` for how each
region currently behaves.

---

## 1. Redesign the "What happened" summary band  ← current focus

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

---

## 2. No Headroom (compression) toggle in the console — PARTLY DONE (:0.3.0)

**Per-app half shipped (B1, `:0.3.0`):** the app **Settings** tab now has a tri-state
Headroom control — `(global default) / on / off` — backed by a nullable
`AppPolicy.compression_enabled` that overrides the live global toggle per application
(null = inherit). Safety bypasses (high-risk / restricted / min-tokens) still apply on
top; see `app/compression/__init__.py` `_effective_enabled`.

**Still open:** a **global** master toggle in the console header. The backend live toggle
(`POST /api/controls`, no restart) exists but no header control calls it.

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

## 6. Fleet ⚠️ "sensitive traffic not all kept on-prem" is over-eager

The per-app warning fires on `pii_count > 0 && onprem_count < pii_count`
(`ui/src/console/FleetOverview.tsx:47`), so a `private_cloud_ok` tenant carrying only
`confidential` entities (e.g. Acme: LOCATION/DATE_TIME from innocuous questions → 161 PII,
0 on-prem) warns even though sending redacted low-risk PII to (private) cloud is
**policy-compliant**, not a leak. It conflates "any PII entity detected" with "must be
on-prem", and undersells the nuanced-routing story. Fix = flag only a genuine residency
violation: a request classified **`restricted`/`onprem_only`** that went off-prem — not
every detected entity. Needs the per-request sensitivity/residency (not just
`pii_count`/`onprem_count`) surfaced to the fleet aggregate (`ui/src/console/aggregate.ts`,
`FleetOverview.tsx`). Full explanation in `docs/demo-dashboard-notes.md`. Left as-is on
purpose for now.
