# P2a — Routing Signal Measurement and Scoring Provenance

**Date:** 2026-09-19
**Status:** design approved, not implemented
**Priority source:** `docs/archive/design-handoffs/RouteForge-Priorities-1-3-LLM-Handoff.md` — Priority 2, slice A
**Predecessors:** P1a (`2026-09-18-policy-compiler-p1a-design.md`), P1b (`2026-09-19-endpoint-data-handling-p1b-design.md`)
**Successor:** P2b — the evaluation plane (feasible-oracle benchmark / regret analysis). Not this spec.

---

## 1. Why this slice exists

The step-6 router presents a four-term score. Three of those terms do not
measure what their names claim:

| Term | What the name promises | What the code does today |
|---|---|---|
| `latency` | how fast this endpoint responds | `_L[c.tier]` — a per-tier constant, `{cheap: 0.2, mid: 0.5, frontier: 1.0}` (`app/routing/strategy.py:26,60`) |
| `capacity` | headroom / admission pressure | `weights["capacity"] * 1.0` — the **same constant for every candidate**, so it cancels out of the ordering entirely (`app/routing/strategy.py:60`) |
| `quality` | a published or calibrated score | often `tier_default`; the four-way provenance ladder exists (`app/routing/quality.py`) but is invisible outside the chosen candidate |
| `cost` | USD for this call | correct on the input/output axes, but **cached prompt tokens are neither captured nor priced anywhere in `app/`** — the provider's prompt-cache discount is invisible and spend is overstated |

> **Corrected 2026-09-19 (fix wave).** The table's `latency` row is also understated,
> in exactly the way this section accuses B30 of understating `capacity`. `Router
> .select` filters `eligible` down to a **single tier** before scoring
> (`app/routing/router.py:165-172`, and the budget-fallback branch at `:195-199`
> likewise). Every scored candidate therefore shared the same `_L[c.tier]` —
> **pre-P2a the latency term also canceled out of the ordering, exactly like
> `capacity`.** Only quality and cost ever ordered anything, pre-P2a; the table above
> is left as originally written, with this correction beside it, for the same reason
> §3's dead-endpoint correction is left visible rather than silently edited. Note the
> corollary in §6.2's own correction below: this also **strengthens** what this slice
> delivers — latency now differentiates between same-tier candidates for the first
> time, which is a real capability the original framing didn't credit it with.

The `capacity` finding deserves emphasis because the backlog understates it.
B30 records it as "capacity is hardcoded to 1.0". It is worse than hardcoded:
adding an identical constant to every candidate's score leaves the ordering
unchanged, so the term has never influenced a single routing decision, and
changing `weights.capacity` in `routing.yaml` from `0.2` to `20` would change
nothing. There is also no circuit breaker in the tree to feed it — `app/gateway/`
is `gateway.py` plus `backends/`, and nothing in `app/` mentions a breaker,
despite CLAUDE.md listing "quotas, breakers" under step 7.

The consequence for the successor slice is the reason to do this one first. A
feasible-oracle benchmark computing regret over the current scorer would
produce authoritative-looking numbers that mostly measure RouteForge's own
constants. Measurement has to precede evaluation, or the evaluation is
self-referential.

### 1.1 What this slice claims, and what it does not

**Claims.** After P2a, a routing decision is fully reconstructible from its own
emitted record: the eligible set, each candidate's scoring terms, the value used
for each term, and whether that value was measured or defaulted — including the
explicit admission that a term was a constant.

**Does not claim.** That routing quality improves. Nothing here measures that.
The slice is deliberately shaped so that P2b can measure it, and so that the
measurement has a real control arm (`signals.enabled: false`).

---

## 2. Scope

### In scope

1. A per-model signal store holding measured **latency** and **availability**.
2. Cached-prompt-token capture and pricing.
3. A rewritten scoring formula consuming both, numerically identical to today's
   when no measurements exist.
4. A per-router-hop **scoring record** carrying every eligible candidate's term
   values and their provenance, on the trace; the chosen candidate's terms on
   the HTTP response.
5. Config, config-lint, and migration changes for the above.
6. A research artifact, `docs/research/routing-signal-provenance.md`.

### Out of scope — deliberately, not by omission

- **Cache-aware affinity** (backlog F2, `docs/architecture/caching-cost-feature-fit.md`).
  It needs a hit-rate predictor; building one on a signal never once measured is
  how you get a confident model of nothing. P2a produces the measurement F2 needs.
- **The evaluation plane / regret metric / FeasibleRouteBench.** That is P2b.
- **Cross-replica signal sharing.** The store is per-process. See §4.5.
- **A circuit breaker.** Availability here is a ranking *preference*, and a
  **bounded** one: it never removes a candidate, it can cost a candidate at most
  `w.availability`, and a dead endpoint may still rank first. Failover remains
  the only recovery mechanism. (Corrected 2026-09-19 — see §3's note; the
  original wording implied a stronger de-preference than the formula provides.)
- **Predicting cached tokens at estimate time.** See §5.3.
- **Console UI for the scoring record.** API- and trace-only, on the same
  footing as P1a's console-controls gap.

---

## 3. Non-negotiable boundaries this slice must respect

Restated from CLAUDE.md because signals are exactly the kind of feature that
erodes them:

- **Eligibility never consults a signal.** `feasible_set` (`app/policy/feasible.py`)
  remains the one authority on who is admissible. The signal store is injected
  into the *strategy*, which by protocol can only reorder the set it is handed
  (`app/routing/strategy.py` module docstring). A dead endpoint is **penalized,
  never removed**; it does not become ineligible. An ineligible endpoint is not
  revived by being fast.

  > **Corrected 2026-09-19 (Task 9 review).** This bullet originally read *"A dead
  > endpoint ranks last."* **That is false.** Availability is an additive term in
  > `[0, w.availability]`, so a dead endpoint loses **at most `w.availability`**
  > — `0.2` with the shipped weights, against `0.6` for cost and `1.0` for
  > quality. A dead endpoint therefore **can and does still rank first** when it
  > is enough cheaper or higher-quality than a healthy peer; the review
  > demonstrated a fully dead endpoint at `+0.3800` beating a healthy pricier peer
  > at `+0.0400`. The corrected sentence is above. The false one is left visible
  > here rather than deleted, because it was quoted into
  > `docs/research/routing-signal-provenance.md` and is exactly the kind of
  > safety claim a reader will rely on. Whether `0.2` is the right magnitude is
  > unmeasured — see backlog **B45**; it must not be tuned inside this slice,
  > because changing a weight here would break §6.2's premise that nothing moves
  > without a measurement.
- **Rule 3 (`onprem_only` absolute) and rule 8 (residency beats the risk floor)**
  are untouched: both are enforced before the strategy is called.
- **No PII.** Signal values are model ids, floats, and enum strings. The scoring
  record is emitted regardless of `capture_io`, exactly like `sp.candidates`
  (`app/obs/export.py:29-33`), and runs through the same defensive egress
  redaction at emit time.
- **Pure-router boundary (rule 4).** Measuring an endpoint's observed latency is
  consumption of a signal RouteForge already produces. It is not hosting,
  scaling, or managing inference.

---

## 4. The signal store

### 4.1 Module and shape

New: `app/routing/signals.py`.

```python
@dataclass(frozen=True)
class SignalValue:
    value: float            # ms for latency; [0,1] for availability
    source: str             # "measured" | "tier_default" | "neutral" | "disabled"
    samples: int            # observations behind `value`; 0 when not measured

class SignalStore:
    def __init__(self, cfg: SignalsCfg) -> None: ...
    def observe_success(self, model: str, latency_ms: float, *, now: float | None = None) -> None: ...
    def observe_error(self, model: str, *, now: float | None = None) -> None: ...
    def latency(self, model: str, tier: str, *, now: float | None = None) -> SignalValue: ...
    def availability(self, model: str, *, now: float | None = None) -> SignalValue: ...
    def seed(self, rows: Iterable[SeedRow]) -> None: ...
    def snapshot(self) -> dict[str, dict]:   # for /metrics and debugging
        ...
```

`now` is an injectable clock on every method so the decay behaviour is testable
without sleeping. Production callers omit it.

### 4.2 Latency

- Plain EWMA over observed `latency_ms`, one per model.
- **Below `min_samples`** (default 5): return the tier default,
  `source="tier_default"`, `samples=n`. The sample count is reported even when
  the value is not used — that is the honest form.
- **Staler than `staleness_s`** (default `4 × half_life_s`): return the tier
  default with `source="tier_default"`. A measurement from two days ago is not a
  measurement.
- **No decay toward a neutral value.** A stale latency becomes the tier default
  outright rather than drifting toward one; drifting would produce a number that
  is neither measured nor default and cannot be labelled honestly.

### 4.3 Availability

- EWMA over per-call outcome (success `1.0`, failure `0.0`), one per model.
- **Decays toward `1.0` on wall-clock time**, not per request:
  `v' = 1.0 - (1.0 - v) * 0.5 ** (Δt / half_life_s)`.

  This closes the starvation trap. Down-weighting on failure plus failover is a
  feedback pair: a model that erred once gets less traffic, and a purely
  sample-driven EWMA would then never receive the successes it needs to recover.
  Time decay returns it to neutral and earns it a retry regardless of traffic.
- **Below `min_samples`**: `value=1.0`, `source="neutral"`. This is today's
  constant, so an unmeasured model scores exactly as it does now.

### 4.4 What counts as a failure — three rulings

1. **`usage_records.status == 'error'` counts. `status == 'rejected'` does not.**
   A rejection is the policy gate doing its job. Counting it as endpoint
   unhealth would make a strict application look like broken infrastructure and
   would let a protection posture silently influence routing preference —
   adjacent to the cross-axis inference CLAUDE.md rule 2a forbids.
2. **A client-caused error does not count.** In `app/gateway/gateway.py`'s
   failure branch, `ContextWindowExceeded` (and any upstream 4xx) means the
   *request* was wrong, not the endpoint. Only connection failures, timeouts,
   and upstream 5xx call `observe_error`. An oversized prompt must not make a
   healthy endpoint look sick — this is the exact shape of the Qwen Code
   retry-storm incident (backlog B36).
3. **A failover attempt observes the model that failed, not the model that
   succeeded second.** Each hop reports its own outcome.

### 4.5 Lifecycle, seeding, and the multi-replica caveat

- **Boot seeding:** one aggregate query over `usage_records` for the last
  `seed_window_hours` (default 24), grouped by `model`, yielding
  `(model, avg_latency_ms, error_rate, n)` for rows with `status IN ('ok','error')`.
  The store is seeded before the router serves traffic; a failed or empty seed
  query logs a warning and leaves the store empty (every model then reports
  `tier_default` / `neutral`, i.e. today's behaviour — fail to the boring side).
- **No new table. No write on the request hot path.** Observations mutate
  in-process state only; durability comes from `usage_records`, which is already
  written.
- **Per-replica drift is real and documented.** Each API replica learns from its
  own traffic, so two replicas can hold slightly different latency estimates for
  the same model. This is acceptable for a ranking *preference* — it cannot
  affect eligibility, cost caps, or residency — and re-seeding at boot bounds the
  divergence. It is stated plainly in the docs rather than implied away. (B3's
  shared-state work covered dashboards; this is deliberately not on that path.)
- **Thread/async safety:** the store is mutated from request-handling coroutines
  on one event loop per process. Updates are single-statement replacements of an
  immutable per-model record; no lock is required, and none is added.

---

## 5. Cost: cached tokens

### 5.1 One pricing function

The cost formula is currently written three times: `Router._est_cost`
(`app/routing/router.py:97`) and twice in `app/gateway/backends/litellm_backend.py`
(lines 140 and 202, non-streaming and streaming). Adding a cached rate to three
copies guarantees drift.

New, in `app/routing/pricing.py`:

```python
def price_call(price: PriceCfg, *, prompt_tokens: int, completion_tokens: int,
               cached_prompt_tokens: int = 0) -> float:
    """USD for one call. `cached_prompt_tokens` are billed at
    `price.cached_input` when the operator declared one, and at the full
    `price.input` rate otherwise -- RouteForge never invents a discount."""
```

All three sites call it. `PriceCfg` (`app/config/schema.py:108`) gains
`cached_input: float | None = None`. That single type is already the convergence
point for both worlds — `ModelRegistry.price_for` (`app/apps/registry.py:187-195`)
constructs a `PriceCfg` for onboarded rows — so one change covers the cloud YAML
path and the on-prem DB path.

### 5.2 Capture

`usage.prompt_tokens_details.cached_tokens`, read in `_usage_dict`
(`app/gateway/backends/litellm_backend.py:39`, so the streaming terminal-usage
chunk is covered) and in the non-streaming path; carried on `GenerationResult`;
persisted to `usage_records.cached_prompt_tokens`.

A provider that does not report the field yields `0`, which is indistinguishable
from "no cache hit". Documented as a limitation, not smoothed over.

### 5.3 The estimate stays conservative

`Router._est_cost` assumes **zero** cached tokens. The router cannot know the hit
rate before the call, and guessing one biases routing toward whichever model the
guess was generous about. Estimate high, record true, and let the certificate
show both (`est_cost_usd` vs. the recorded actual). Budget enforcement therefore
remains at least as strict as today.

### 5.4 No invented discount

`cached_input is None` ⇒ cached tokens bill at the full `input` rate. Every
existing deployment's cost figures are unchanged by the upgrade until an operator
declares a rate. On-prem is explicitly *not* free: a vLLM prefix-cache hit saves
latency, not GPU amortization, so the natural on-prem declaration is
`price_cached_input == price_input` — the operator still gains the latency signal
from §4.2, which is where the on-prem win actually appears.

---

## 6. Scoring

### 6.1 The formula

```
score = w.quality      · quality
      − w.cost         · (est_cost / max_est_cost_over_eligible)
      − w.latency      · min(latency_ms / latency_scale_ms, max_latency_norm)
      + w.availability · availability
```

Tie-break remains raw `est_cost` ascending, unchanged.

### 6.2 The identity property — the safety argument for the whole slice

Latency normalizes against a **fixed** `latency_scale_ms`, not the
candidate-set maximum. With the defaults

```
tier_latency_ms = {cheap: 200, mid: 500, frontier: 1000}
latency_scale_ms = 1000
```

an unmeasured candidate's latency term evaluates to `0.2 / 0.5 / 1.0` — exactly
today's `_L` constants. An unmeasured availability is `1.0` — exactly today's
hardcoded constant.

**Therefore: with an empty signal store, the new formula produces numerically
identical scores to the current one.** Equal scores, not merely equal ordering.
Every behaviour change in this slice is attributable to a measurement actually
existing, which is also precisely what lets P2b isolate the effect of
measurement.

Set-max normalization would break this: a two-tier eligible set would rescale
the latency term relative to a three-tier one, changing the term's weight
against cost and quality for reasons unrelated to any measurement. Cost keeps
set-max because cost is inherently comparative (dollars per model vary by orders
of magnitude); latency is absolute (milliseconds mean the same thing everywhere).
The two terms want different normalizations, and conflating them is the bug this
paragraph exists to prevent a future refactor from introducing.

> **Corrected 2026-09-19 (fix wave).** The "two-tier eligible set" in the paragraph
> above describes a set `Router.select` cannot actually produce — it filters
> `eligible` down to a single tier before scoring (see §1's correction), so a
> multi-tier rescaling scenario never arises in production. The *argument* for fixed-
> scale over set-max still holds on its own terms (latency is absolute, cost is
> comparative), so this does not change the identity property or the decision it
> defends. But `tests/routing/test_strategy_identity.py`'s Hypothesis grid generates
> candidate sets spanning all three tiers, which is a wider domain than production
> ever exercises through this strategy. That does not invalidate the test — it still
> correctly pins that the *formula* reproduces pre-P2a scores for any tier mix it is
> given, which is the honest scope of a strategy-level property (a `RoutingStrategy`
> is handed whatever `eligible` set its caller built, per the module docstring in
> `app/routing/strategy.py`) — but a reader should not conclude from it that
> production ever calls `rank()` with a multi-tier set today.

`max_latency_norm` (default 3.0) clamps the term so one pathologically slow
endpoint cannot swamp quality and cost entirely.

### 6.3 The `capacity` → `availability` rename

`weights.availability` replaces `weights.capacity`. `capacity` remains accepted
in `routing.yaml` as a deprecated alias with a boot warning, so no existing
deployment fails to start.

The rename is the honest part. **True capacity is unobservable across an
OpenAI-compatible boundary** — the protocol has no field for queue depth,
admission pressure, or throughput headroom, so a term named `capacity` promises
a measurement the interface cannot deliver. What RouteForge can observe is
whether calls to an endpoint have been succeeding. That is availability.

### 6.4 Strategy protocol change

`RoutingStrategy.rank` gains a `signals: SignalStore` parameter and returns a
richer result:

```python
@dataclass(frozen=True)
class TermValue:
    value: float
    source: str
    samples: int = 0

@dataclass(frozen=True)
class ScoredCandidate:
    candidate: PoolCandidate
    est_cost_usd: float
    score: float
    quality: TermValue          # source from QUALITY_SOURCES
    latency: TermValue          # ms;  source measured|tier_default|disabled
    availability: TermValue     # [0,1]; source measured|neutral|disabled
    cost_norm: float
    cost_cached_pricing: str    # "priced" | "unpriced"

@dataclass(frozen=True)
class RankResult:
    ordered: list[ScoredCandidate]      # best first
    weights: dict[str, float]
    signals_enabled: bool
    latency_scale_ms: float
```

**The membership invariant is unchanged and now binds harder:** `ordered` must
contain exactly the members of `eligible`, reordered. A strategy ranks; it never
filters in or out.

> **Implementation hazard — read this.** Changing a `Protocol` signature has bitten
> this codebase before: a `TypeError` from a stale implementation was swallowed by
> `app/gateway/gateway.py` and surfaced as `NoEligibleCandidate`, which looks like a
> policy rejection (see the OpenAI tool-calling fix, PR merged 2026-08-29). Every
> implementation and every test fake of `RoutingStrategy` must be updated in the
> same commit, and one test must assert the new signature is actually exercised
> end-to-end rather than only through a fake.

`Router.select` threads `RankResult` into `RoutingDecision` as a new
`scoring: RankResult | None` field, then onto the `DecisionRecord`.

---

## 7. The scoring record

### 7.1 Placement

**Per router hop**, beside `sp.candidates`. Failover produces a second hop with a
different eligible set and different signal state; collapsing them into one
record would misdescribe the second.

**Trace** (`sp.scoring`, JSON-encoded like `sp.candidates`): terms for **every
eligible candidate**.

**HTTP response** (inside the existing certificate block): terms for the **chosen
candidate only**. The response is a client-facing surface on every chat
completion and must stay small; the trace is the audit and replay surface.

**Rejected candidates get no scoring entry.** They were never scored. Inventing
terms for them would be the same overstatement this slice exists to remove.

### 7.2 Shape

```json
{"weights": {"quality": 1.0, "cost": 0.6, "latency": 0.3, "availability": 0.2},
 "signals_enabled": true,
 "latency_scale_ms": 1000,
 "candidates": [
   {"name": "qwen3-8b", "score": 0.71,
    "quality":      {"v": 0.5,    "src": "tier_default"},
    "latency":      {"ms": 342.0, "src": "measured", "n": 128},
    "availability": {"v": 0.99,   "src": "measured", "n": 128},
    "cost":         {"est_usd": 0.00012, "norm": 0.4, "cached": "unpriced"}}]}
```

### 7.3 The replay property

The record must be **sufficient**: recomputing every candidate's score from the
record alone — no `SignalStore`, no live config — must reproduce the order the
router returned. This is the substrate P2b needs. It is also the one property
whose failure would silently invalidate the successor slice, so it is tested
directly (§8).

---

## 8. Testing

| # | Test | Why it exists |
|---|---|---|
| T1 | **Replay.** Recompute scores from the emitted record alone; assert the order equals the router's. | If this fails, the record is insufficient and P2b measures a fiction. The most important test in the slice. |
| T2 | **Characterisation / identity.** Old inline scoring vs. new, over a generated candidate grid (Hypothesis), with an empty store: assert equal **scores**, not just equal ordering. | The §6.2 safety argument. Fails loudly if a future refactor swaps in set-max normalization. |
| T3 | **EWMA decay.** Availability returns to neutral on wall-clock with zero traffic (injected clock). | The starvation trap (§4.3). |
| T4 | **Gates.** Below `min_samples` ⇒ `tier_default`/`neutral` with the true sample count; beyond `staleness_s` ⇒ `tier_default`. | Never silently present a defaulted value as measured. |
| T5 | **Failure classification.** `rejected` rows and `ContextWindowExceeded` do not move availability; a 5xx/timeout does. | §4.4 rulings 1 and 2. |
| T6 | **Pricing.** `cached_input` unset reproduces today's cost exactly; set, applies the discount; the three former call sites agree because they are one function. | §5.4. |
| T7 | **Membership property.** For arbitrary signal state, `ordered` membership equals `eligible` membership. | Signals are the first thing that could plausibly breach the rank-never-filters invariant. |
| T8 | **Kill switch.** `signals.enabled: false` ⇒ tier defaults everywhere, `src: "disabled"`, scores identical to today. | Operator escape hatch and P2b's control arm. |
| T9 | **Seeding.** Boot seed populates from `usage_records`; a failing seed query leaves the store empty and the router serving. | Fail to the boring side. |
| T10 | **Protocol.** Every `RoutingStrategy` implementation and fake accepts the new signature, exercised end-to-end at least once. | §6.4 hazard. |

Run the suite with `LITELLM_LOCAL_MODEL_COST_MAP=True`. One pre-existing failure
(`presidio_analyzer` not installed) is expected and unrelated.

---

## 9. Configuration

All in `config/routing.yaml` — one owner per concern.

```yaml
weights: {quality: 1.0, cost: 0.6, latency: 0.3, availability: 0.2}

signals:
  enabled: true
  tier_latency_ms:   {cheap: 200, mid: 500, frontier: 1000}
  latency_scale_ms:  1000
  max_latency_norm:  3.0
  half_life_s:       900
  min_samples:       5
  staleness_s:       3600
  seed_window_hours: 24
```

`config/pricing.yaml` model entries take an optional `cached_input`.

**Config lint** gains one rule: `tier_latency_ms` keys must be exactly the
canonical `tiers` list — the same coverage check `tier_quality` already receives,
catching the same class of drift.

**Note on a near-duplicate table.** `app/gateway/backends/base.py:24` already
defines `TIER_LATENCY_MS = {cheap: 200, mid: 500, frontier: 2000}`. That table is
**test-only** — the deterministic LiteLLM fake reads it so tests get stable
non-zero latencies — and it stays where it is. The new `signals.tier_latency_ms`
is the production default and differs at `frontier` (1000, chosen so the
normalized term reproduces `_L` exactly per §6.2). Each gets a comment pointing
at the other so a future reader does not "fix" the discrepancy.

---

## 10. Schema and migration

| Table | Column | Type | Notes |
|---|---|---|---|
| `usage_records` | `cached_prompt_tokens` | `INTEGER` default 0 | |
| `onboarded_models` | `price_cached_input` | `FLOAT` nullable | `NULL` ⇒ bill at `price_input` |

Both added to `scripts/migrate_app_policies.py`, following the P1a/P1b pattern:
idempotent, one transaction per table, "table absent, skipped".

Nullable, no backfill, no behaviour change on upgrade.

**Deploy-count warning.** A deployment still on pre-P1a code now needs **eleven**
`ALTER`s before rolling the image — P1a's 4, P1b's 5, and P2a's 2. Stated
explicitly here so the count does not creep up quietly across slices. The
migration script has still only been exercised against SQLite; the Spark and DGX
run Postgres, and a dry run against a restored snapshot remains a prerequisite
(carried forward from P1b).

---

## 11. Documentation

- `docs/reference/routing-observability.md` — the scoring record, its fields, and
  the per-replica caveat.
- `docs/reference/per-app-config.md` — the two new columns in the migration
  section, and the running `ALTER` count.
- `docs/guides/onboarding-models.md` — `price_cached_input`, including the
  on-prem "set it equal to `price_input`" guidance from §5.4.
- `docs/backlog/bugs.md` — B30 resolved as **renamed and made real**, with the
  "the term was inert, not merely hardcoded" correction recorded.
- `docs/research/routing-signal-provenance.md` — the research artifact (§12).

---

## 12. Research artifact

`docs/research/routing-signal-provenance.md`, on the P1a precedent
(`docs/research/policy-compiler-noninterference.md`): the model, the invariants,
the counterexamples with commit references, and an explicit section on what is
**not** demonstrated.

The defensible claim is narrow: eligibility provenance (P1a/P1b) plus scoring
provenance (P2a) makes a routing decision fully reconstructible from its own
record, *including the admission that a term was a constant*. A router that can
report "I preferred this endpoint on latency, and my latency figure was a tier
default with zero samples" is saying something most routers structurally cannot.

The claim explicitly **not** made: that any of this improves routing quality.
That is P2b's question, and P2a is shaped to let P2b ask it honestly —
`signals.enabled: false` is a real control arm, and §6.2's identity property
means the two arms differ only by the presence of measurement.

---

## 13. Open questions carried forward

1. **Console surface for the scoring record** — API/trace-only in this slice,
   alongside P1a's console-controls gap. One follow-up should close both.
2. **`/metrics` exposure of `SignalStore.snapshot()`** — natural fit for the
   existing in-process Prometheus exporter, but adds per-model label cardinality.
   Deferred pending a decision on that bound.
3. **Cross-replica signal sharing** — only worth building if P2b shows
   per-replica drift materially affects outcomes. Measure first.
