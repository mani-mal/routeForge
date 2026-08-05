# Cluster B — Decision-trace honesty (B10, B11, B12)

**Date:** 2026-08-05
**Status:** design, ready for implementation plan
**Source items:** `docs/bugs-backlog.md` B10, B11, B12
**Base:** `feat/infra-management-config` @ `7f2164a` (Cluster A + model-selection merged)

## Purpose

Three medium-severity defects that make the decision trace and cost numbers **lie** to
the operator on the request drilldown — the screens meant to prove the product's privacy
and cost-savings claims. All three are about the UI/trace telling the truth; none change
routing/policy behaviour (B10 changes which candidates *enter* scoring, but the chosen
model and privacy guarantees are unchanged — a disabled provider was never routable).

Grouped because B10 and B11 share one root confusion (deriving eligibility from
`onprem_only` instead of the effective candidate pool), and B12 is the cost-math half of
the same "the drilldown lies" complaint.

## Non-negotiables preserved

- No change to the privacy guarantees (Rules #1–#8). `onprem_only` still filters cloud by
  construction; B10 only removes *disabled* providers from the candidate pool earlier.
- The additive cost identity `compression_saved + routing_saved == baseline_usd − total`
  must still hold after B12 (both the direct and multi-step paths).
- Honesty rules unchanged: entity **types** only, no raw PII, no CCR contents.

## Pre-work findings (grounding, current line numbers)

- `ModelRegistry.effective_pool()` (`app/apps/registry.py:60-67`) merges the **entire**
  static YAML cloud `model_pool` with no check on `providers.{azure,bedrock}.enabled`. So
  a killed cloud model still enters routing.
- `Router.select()` (`app/routing/router.py:60-66`) rejects any candidate whose provider
  is not in `ctx.allowed_providers` with a **hardcoded** `reason="excluded by policy
  (residency)"`. But `ctx.allowed_providers` (`app/policy/context.py:56-72`) drops
  providers for **two** distinct reasons: the infra kill-switch (`providers.*.enabled`)
  and residency (`onprem_only`). The label conflates them.
- The PRIVACY summary card (`ui/src/console/RequestDrilldown.tsx:433-437`) derives its
  headline **only from `p.onprem_only`** (`false` ⇒ "every provider was eligible"), and
  hardcodes "No sensitive data found" even when `p.detected_entities` is non-empty.
- `ProviderStrip` (`ui/src/console/RequestDrilldown.tsx:315-316`) has the same
  two-reason conflation: `excludedReason = onpremOnly ? "excluded — residency" :
  "excluded by policy"`.
- `Pipeline._baseline(tokens)` (`app/pipeline.py:58-60`) prices a **single input-token
  count** at the blended `(input+output)` rate, and is fed `comp.tokens_before` /
  `comp.tokens_after` (prompt tokens only). Call sites: direct path `pipeline.py:150-153`,
  multi-step path `pipeline.py:228-245`. The completion (output) tokens — which `total`
  includes — are excluded from the baseline, so `routing_saved = baseline − total` goes
  negative on a cheap direct request.

## The three fixes

### B10 — stop disabled providers appearing as "residency" spurs

Decision (owner): **hide** kill-switch-disabled providers from the Model Router card
entirely; keep residency-excluded providers as greyed spurs with a precise reason.

1. **Filter `effective_pool()` by provider availability.** In
   `app/apps/registry.py:effective_pool()`, include a static `model_pool` candidate only
   if its provider is enabled in `serving.yaml`:
   ```python
   def effective_pool(self) -> list[PoolCandidate]:
       enabled = {p for p, c in self._s.serving.providers.items() if c.enabled}
       pool = [c for c in self._s.routing.model_pool if c.provider in enabled]
       for r in self._onboarded().values():
           if r.enabled:
               pool.append(PoolCandidate(name=r.slug, provider=r.provider, tier=r.tier,
                                         display_name=r.display_name, quality=r.quality))
       return pool
   ```
   A killed cloud provider's models never enter the pool, so they produce no
   `RejectedCandidate` and vanish from the router card. The SERVING PROVIDERS strip
   (driven by `allowed_providers`) still shows the provider "off". On-prem onboarded rows
   are unaffected (already per-model `r.enabled`; the on-prem infra kill-switch is handled
   separately in `context.py` via `has_healthy_onprem`).

2. **Correct the surviving rejection reason.** After (1), a candidate can only fail the
   `router.py:61` `provider not in ctx.allowed_providers` check for a genuine
   **residency** (`onprem_only`) or **app-policy** (`app_policy.allowed_providers`) reason
   — the kill-switch case is gone. Split the label so it is precise:
   ```python
   if c.provider not in ctx.allowed_providers:
       reason = ("excluded · residency" if ctx.onprem_only
                 else "not permitted for application")
       rejected.append(RejectedCandidate(name=c.name, provider=c.provider, tier=c.tier,
                                         display_name=c.display_name, reason=reason))
       continue
   ```
   (`ExecutionPolicyContext` already carries `onprem_only`.)

3. **`ProviderStrip` reason (`RequestDrilldown.tsx:315-332`).** Distinguish the two cases
   so a disabled provider isn't labeled "residency". A provider is excluded either because
   it's off (`onprem_only == false` ⇒ kill-switch/app-policy) or by residency
   (`onprem_only == true`):
   ```tsx
   const excludedReason = onpremOnly ? "excluded · residency" : "provider off";
   ```

**Scope note:** B10 is the only item that changes backend behaviour — it removes disabled
providers from the candidate pool. The chosen model, cost, and every privacy invariant are
unchanged (a disabled provider was never a legal destination). Covered by a unit test that
`effective_pool()` omits a candidate whose provider is disabled, and that the router emits
no spur for it.

### B11 — PRIVACY headline must reflect the effective pool, not `onprem_only`

Replace the `onprem_only`-only derivation (`RequestDrilldown.tsx:433-437`) with logic
keyed on three signals already present on `p`: `onprem_only`, `allowed_providers`, and
`detected_entities`.

Define (in the component):
```tsx
const hasCloud = p.allowed_providers.some((x) => x !== "onprem" && x !== "mock");
const hasEntities = p.detected_entities.length > 0;
const entityList = p.detected_entities.join(", ");
```

Headline taxonomy (verdict / why / accent):

| Case | Condition | Verdict | Why | Accent |
|---|---|---|---|---|
| Residency | `onprem_only` | "Kept on-prem" | `Detected {entityList} — residency requires on-prem serving; cloud was ineligible.` (or "Residency policy requires on-prem serving." when no entities) | SHIELD (amber — a win) |
| Cloud off | `!onprem_only && !hasCloud` | "On-prem only" | `Cloud providers are disabled — this request was served on-prem.` | NEUTRAL (muted gray) |
| Cloud-eligible, PII | `!onprem_only && hasCloud && hasEntities` | "Cloud-eligible" | `Detected {entityList}, but none require on-prem residency — every provider was eligible.` | CLEAR (green) |
| Cloud-eligible, clean | `!onprem_only && hasCloud && !hasEntities` | "Cloud-eligible" | `No PII detected — every provider was eligible.` | CLEAR (green) |

- Add a **NEUTRAL** accent constant alongside the existing SHIELD/CLEAR (muted gray, e.g.
  the `edge`/`gray-500` family) for the "Cloud off" case — it is neither a residency win
  (amber) nor a cloud-eligible clear (green); config simply turned cloud off.
- The phrase **"No sensitive data found" must never render when `hasEntities`** — the
  taxonomy above already routes entity-bearing requests to entity-aware wording. This
  removes the `PERSON` chip / "no sensitive data" contradiction.
- The **Model card** already states its own routing rationale (unchanged); only the
  Privacy card's verdict/why/accent change.

### B12 — price the all-frontier baseline on the actual input+output tokens

Change `_baseline` to price input and output token counts at their **respective** frontier
rates, matching the basis `total` uses:

```python
def _baseline(self, in_tokens: int, out_tokens: int) -> float:
    p = self._s.pricing.models[self._s.pricing.baseline_model]
    return (in_tokens * p.input + out_tokens * p.output) / 1_000_000
```

Thread the completion (output) token count into every call site:

- **Direct path (`pipeline.py:150-153`).** `baseline_usd` prices pre-compression input +
  actual output; `baseline_after` prices post-compression input + actual output:
  ```python
  out_tokens = <completion tokens for this request>   # same value total already uses
  baseline_usd   = self._baseline(comp.tokens_before, out_tokens)
  baseline_after = self._baseline(comp.tokens_after,  out_tokens)
  compression_saved = max(0.0, baseline_usd - baseline_after)  # = (before-after)*frontier.input, ≥0
  routing_saved     = baseline_after - total
  ```
  Compression only shrinks the **input** token count, so pricing output identically in
  both baselines keeps `compression_saved` equal to the input-token reduction valued at
  the frontier input rate — unchanged in meaning, now on the correct rate model.
- **Multi-step path (`pipeline.py:228-245`).** The per-hop `DecisionRecord`
  (`app/models.py:84-96`) carries `tokens_priced` (input) but **no output-token field** —
  only the transient `GenerationResult` (`app/models.py:126-132`) has `tokens_out`. So:
  1. Add `tokens_out: int = 0` to `DecisionRecord` and populate it where the hop record is
     built (`app/agent/internal_endpoint.py:~177`, from `gen.tokens_out`, alongside the
     existing `tokens_priced=tokens_after`).
  2. `baseline_after = Σ self._baseline(h.tokens_priced, h.tokens_out) for h in result.hops`.
  3. `baseline_before = Σ self._baseline(cr.tokens_before, h.tokens_out)` pairing each
     compression record with its hop by index (`zip(result.compressions, result.hops)` —
     they are 1:1 per the existing comment at `pipeline.py:222`); fall back to
     `baseline_after` when there are no compression records.
- The additive identity `compression_saved + routing_saved == baseline_usd − total` still
  holds by construction (output priced identically in both baselines and in `total`).

**Where output tokens come from (grounded):**
- **Direct path:** `result.tokens_out` (the `GenerationResult`, already used to build
  `usage.completion_tokens` at `pipeline.py:180`). Pass it to both `_baseline` calls.
- **Multi-step path:** the new `DecisionRecord.tokens_out` per hop (step 1 above). Do not
  re-tokenize; do not use the whole-response `count_tokens(result.text)` (that is the final
  answer only, not per-hop).

**Tests (`tests/`):**
- Same-tier request (chosen == baseline_model) ⇒ `routing_saved == 0` (within float
  epsilon).
- Cheaper-tier request ⇒ `routing_saved > 0`.
- Additive identity holds: `compression_saved + routing_saved == baseline_usd − total`.
- A direct request with a large completion (e.g. 128 in / 972 out) to a cheaper tier
  yields `baseline_usd > total` and `routing_saved > 0` (the exact B12 repro).

## What is explicitly NOT in scope

- No change to how the router *scores* or *chooses* (weights, tiers, escalation).
- No new provider, no residency-rule change.
- No change to `total` (actual cost) — only the baseline's token basis/rate model.
- The other backlog clusters (C, D, E, B17).

## Testing summary

- **Backend:** B10 registry/router unit tests (disabled provider omitted from pool + no
  spur; residency spur still emitted and correctly labeled). B12 baseline unit tests
  (same-tier 0, cheaper-tier >0, additive identity, B12 repro). Reuse the existing
  in-memory settings/registry test fixtures.
- **Frontend:** no UI test runner — `tsc -b --noEmit` + `vite build` exit 0, plus manual
  verification of the four B11 headline cases and the B10 router-card/provider-strip
  labels.

## Build order (dependency-aware)

1. **B10 backend** — `effective_pool()` filter + router reason split (+ tests). Foundational;
   B11's "cloud off" case depends on `allowed_providers` already excluding disabled cloud
   (it does — no dependency, but do backend first).
2. **B10 UI** — `ProviderStrip` reason.
3. **B11 UI** — PRIVACY headline taxonomy + NEUTRAL accent.
4. **B12 backend** — `_baseline` rate model + call sites + tests.

B10-UI and B11-UI touch the same file (`RequestDrilldown.tsx`) — can be one task or two
sequential tasks. B12 is independent of B10/B11 and can be built in parallel or last.
