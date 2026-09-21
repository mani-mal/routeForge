# Measured signals and per-term scoring provenance in an eligibility-constrained LLM router

**Status:** research notes, written as the code landed. 2026-09-19.
**Branch:** `feat/p2a-routing-signal-measurement` (merge base `5a61a0a`). Design:
`docs/superpowers/specs/2026-09-19-routing-signal-measurement-p2a-design.md`.
**Predecessors:** P1a (`docs/research/policy-compiler-noninterference.md`), P1b.
**Successor:** P2b — the evaluation plane. Not built.
**Audience:** whoever writes the paper, and whoever talks to counsel first.

This is the evidence file, not the paper. Its job is to record exactly what was built,
exactly what was demonstrated, and — with more care than the rest of it — **exactly what
was not**. §6 is the section to read before writing any claim sentence.

The honest one-line summary is uncomfortable and should stay in the paper: **this slice
does not make routing better, and no part of it measures whether routing is better.** It
makes a routing decision say what it rested on, and it removes two terms that were
pretending to measure something. That is a precondition for evaluation, not evaluation.

---

## 0. File before you publish

**P2a touches four items in
[`docs/roadmap/ip-and-research-opportunities.md`](../roadmap/ip-and-research-opportunities.md):**

| Item | Overlap with P2a |
|---|---|
| **R1** — "Eligibility-Constrained LLM Routing" | P2a is the *scoring* half of R1's asset list. R1 already names `sp.candidates` (chosen + rejected, with reasons) as ground truth; `sp.scoring` adds, for each candidate that was actually scored, the term values and their provenance. The composition property R1 wants to prove — that a signal cannot escape the eligible set — is §2.1 here. |
| **IP-4 / R7** — decisions bound to content hashes; decision records as compliance artifacts | The scoring record extends the certificate from *what policy applied* to *what the ranking rested on*, including the admission that a term was a constant. R7's "compliance by construction" argument gains a second example. |
| **IP-8 / R8** — zero-traffic cold-start routing | P2a is adjacent but is **not** IP-8. It measures endpoints *after* they serve traffic. IP-8's claim is a probe battery at onboarding time, before any traffic exists. P2a's boot seeding from `usage_records` is a warm restart, not a cold start; §6 says so. |
| **IP-11** — raise-only, residency-clamped advisory signals | ⚠️ **A naming trap.** IP-11 cites `app/routing/signals.py`, which at the time meant the *stage-signals* tier bias, deleted 2026-09-17. That file name is now P2a's measured-signal store, and it is a **different mechanism**: P2a signals never touch a tier floor, never raise anything, and act only as an ordering preference inside an already-filtered set. Do not let a draft conflate the two. |

**Public disclosure — an arXiv preprint, a conference talk, a public repo, a blog post —
starts clocks and, in most jurisdictions outside the US, destroys novelty for a later
filing.** The same two uncomfortable facts P1a recorded still hold: the Tier-3 mechanisms
are already in a public repository, and four patent areas were already found foreclosed by
an admittedly incomplete landscape scan. Treat that scan as a floor on prior art, never a
ceiling. Nothing here is legal advice.

---

## 1. The problem, stated as a systems problem

A router that presents a weighted score implicitly claims each term measures the thing its
name says. RouteForge's step-6 scorer presented four terms. Three did not.

| Term | What the name promises | What the code did, at `90b6043~1` |
|---|---|---|
| `latency` | how fast this endpoint responds | `_L[c.tier]` — a per-tier constant, `{cheap: 0.2, mid: 0.5, frontier: 1.0}` |
| `capacity` | headroom / admission pressure | `weights["capacity"] * 1.0` — the **same constant for every candidate** |
| `quality` | a published or calibrated score | usually `tier_default`; the four-rung provenance ladder existed but was visible only for the chosen candidate |
| `cost` | USD for this call | correct on input/output, but cached prompt tokens were neither captured nor priced anywhere in `app/` |

> **Corrected 2026-09-19 (fix wave).** The `latency` row above is also understated, in
> exactly the way §1.1 accuses B30 of understating `capacity`. `Router.select` filters
> `eligible` down to a **single tier** before scoring (`app/routing/router.py:165-172`,
> and the budget-fallback branch at `:195-199`). Every scored candidate therefore shared
> the same `_L[c.tier]`, so **pre-P2a the latency term also canceled out of the ordering,
> exactly like `capacity`** — only quality and cost ever ordered anything, pre-P2a. The
> table is left as originally written; this is the correction beside it, in the same
> visible style as the dead-endpoint correction in §2.5. The corollary is a point in this
> slice's favor: latency now differentiates between same-tier candidates for the first
> time, a real capability the original framing didn't credit it with.

### 1.1 The `capacity` term was algebraically inert, not merely hardcoded

This is the finding worth a paragraph in the paper, and the repo's own backlog (B30)
understated it. The pre-P2a score was

```python
score = (weights["quality"] * q
         - weights["cost"] * norm
         - weights["latency"] * _L[c.tier]
         + weights["capacity"] * 1.0)          # <- identical for every candidate
```

sorted by `(-score, est_cost)`. Adding a constant to every candidate's score is an
order-preserving transformation, and the tie-break does not see the score at all.
Therefore the `capacity` term **never influenced a single routing decision in the
product's history**, and changing `weights.capacity` in `routing.yaml` from `0.2` to `20`
would have changed nothing. A configuration knob that is documented, weighted, exposed to
operators, and provably inert is a distinct failure mode from a knob that is merely
wrong: no amount of tuning it produces an observable, so nothing in normal operation can
reveal it.

The repair was not to feed the term a value. It was to **rename it**, because
**true capacity is unobservable across an OpenAI-compatible boundary**: the protocol has
no field for queue depth, admission pressure, or throughput headroom. A term named
`capacity` promises a measurement the interface cannot deliver, and would have gone on
promising it whatever value was plugged in. What the boundary *does* expose is whether
calls have been succeeding — including HTTP 429, which is the closest thing to admission
pressure the protocol can express. That is availability, and that is what the term is now
called. `capacity` survives as a deprecated alias in `routing.yaml` so no deployed config
fails to boot.

### 1.2 Why this had to precede evaluation

The successor slice (P2b) is a feasible-oracle benchmark computing regret over the
scorer. Run against the pre-P2a scorer, it would have produced authoritative-looking
numbers that mostly measured RouteForge's own constants — a per-tier latency table and a
term that cancels. Measurement has to precede evaluation, or the evaluation is
self-referential. That is the argument for this slice existing at all, and it is
independent of whether the measurements turn out to help.

---

## 2. The model

### 2.1 Signals are ranking preferences, strictly downstream of an eligibility authority

The load-bearing structural claim, and the one that makes the rest safe:

```
feasible_set : (request, policy, registry) → E        (app/policy/feasible.py)
rank         : (E, signals)                → a permutation of E   (app/routing/strategy.py)
```

`rank` is a *permutation*. It receives the post-residency, post-scope, post-tier,
post-budget set and returns exactly those members, reordered. It has no channel by which
to add a candidate it was not handed, and returning fewer would be a membership
violation. So:

- A dead endpoint **is penalized, never removed** — and the penalty is bounded. See
  §2.5: availability can cost a candidate at most `w.availability`, so a dead endpoint
  can and does still rank **first** when another term favours it. There is no circuit
  breaker, deliberately; failover remains the only recovery mechanism.
- A fast endpoint that residency excluded is **not revived by being fast.**
- No signal can affect placement, a cost cap, or `onprem_only`. CLAUDE.md rules 3 and 8
  are enforced before `rank` is called and are untouched by this slice.

The membership property is pinned by
`test_ranking_never_changes_membership_whatever_the_signals_say` (Hypothesis, 100
examples, random success/error histories). It is a weak property — that is the point.
The strength of the design is that the *only* property signals need to satisfy is a weak
one, because they sit downstream of the authority that enforces the strong ones.

### 2.2 The two signals

Both live in `app/routing/signals.py`, per-model, in-process. `SignalValue(value, source,
samples)` is what a read returns; the same three fields cross into the strategy as
`TermValue`.

**Latency** — an EWMA over observed wall-clock latency of *successful* calls. A failed
call's duration says nothing about serving speed, so `observe_error` does not touch
latency. Smoothing is `α(n) = max(1/(n+1), alpha_floor)`: a running mean while samples
are few, a fixed-width EWMA (`alpha_floor = 0.1`) once `n` exceeds ~9.

Two gates, both reported rather than hidden:

- below `min_samples` (default 5) → the tier default, `source="tier_default"`, with the
  **true** sample count still reported;
- older than `staleness_s` (default 3600) → the tier default, `source="tier_default"`.

A stale latency falls back **outright** rather than drifting toward the default. A drifted
value would be neither measured nor default, and could not be labelled honestly. That is
the same argument as §4's, applied to a number instead of a string.

**Availability** — an EWMA over per-call outcome (success `1.0`, failure `0.0`) that
**decays toward `1.0` on wall-clock time**, not per request:

```
v' = 1 − (1 − v) · 0.5 ^ (Δt / half_life_s)
```

Wall-clock decay closes a starvation trap that sample-driven decay cannot. Down-weighting
on failure and failover are a feedback pair: a model that erred is *less preferred*, so —
whenever that preference is actually decisive, which per §2.5 is not always — it receives
less traffic, and a purely sample-driven EWMA would never see the successes it needs to
recover. Time decay returns it to neutral and earns it a retry regardless of traffic. The
trap is real where the feedback bites; the decay costs nothing where it does not. Below `min_samples` the
value is `1.0` with `source="neutral"` — which is exactly the pre-P2a constant, and the
reason §3 works.

### 2.3 What counts as a failure

Three rulings, each enforced in a **different** place — worth stating precisely, because
they are three separate mechanisms and only one of them is a classifier. All three are
covered by `tests/routing/test_signal_wiring.py`.

1. **`status == 'rejected'` does not count** — the `status.in_(("ok","error"))` filter in
   `app/apps/store.py::aggregate_model_signals` (~`:134`), i.e. on the **boot-seeding**
   path, not in the live classifier. A rejection is the policy gate doing its
   job. Counting it as endpoint unhealth would make a strict application look like broken
   infrastructure — and would let a *protection posture* silently influence routing
   preference, which is adjacent to the cross-axis inference CLAUDE.md rule 2a forbids.
   This is the one place P2a touches P1a's noninterference argument, and it resolves the
   same way: protection must not leak into any other axis, including a ranking one.
2. **A client-caused error does not count** — `counts_as_endpoint_unhealth`
   (`app/gateway/gateway.py`), the only one of the three that is a classifier.
   `ContextWindowExceeded` and any upstream 4xx mean the *request* was wrong. An
   oversized prompt must not make a healthy endpoint look sick — that is the exact shape
   of the Qwen Code retry-storm incident (backlog B36). 429 is the exception and *does*
   count: it is the one admission-pressure signal the protocol offers.
3. **A failover observes the model that failed, not the model that succeeded second** —
   the `observe_error(cand.name)` call site inside the failure branch of the failover
   loop (`app/gateway/gateway.py:175`), which names *this hop's* candidate rather than
   the eventual winner. A property of the call site, not of any classifier.

### 2.4 The formula, and the fixed-scale normalization argument

```
score = w.quality      · quality
      − w.cost         · (est_cost / max_est_cost_over_eligible)
      − w.latency      · min(latency_ms / latency_scale_ms, max_latency_norm)
      + w.availability · availability
```

Tie-break is raw `est_cost` ascending, unchanged.

**Latency normalizes against a fixed `latency_scale_ms`, never the candidate-set
maximum, and cost keeps set-max.** The two terms genuinely want different
normalizations:

- **Cost is comparative.** Dollars per token vary by orders of magnitude between an
  on-prem 8B and a frontier API, and "is this the expensive option *here*" is the
  meaningful question. Set-max is right.
- **Latency is absolute.** A millisecond means the same thing in every candidate set.
  Under set-max, a two-tier eligible set would rescale the latency term relative to a
  three-tier one, changing that term's weight against cost and quality **for reasons
  unrelated to any measurement** — and it would destroy the identity property of §3.

`max_latency_norm` (default 3.0) clamps the term so one pathologically slow endpoint
cannot swamp quality and cost entirely.

This paragraph exists in the code as well (`DefaultRoutingStrategy`'s docstring), because
"make latency consistent with cost" is exactly the change a future refactor would make in
good faith.

### 2.5 How much a signal can actually move a decision — and why a dead endpoint can still win

An earlier draft of this document, and the design spec it was written from, both said *"a
dead endpoint ranks last."* **That is false, and it was false in the most load-bearing
place: it was the reassurance offered for why having no circuit breaker is safe.** The
correction is recorded rather than quietly edited, because it is the kind of claim a
reviewer will check.

Availability enters as an additive term `+ w.availability · availability` with
`availability ∈ [0, 1]`. So swinging a model from perfectly healthy to completely dead
changes its score by **exactly `w.availability`, and no more** — with the shipped weights
(`config/routing.yaml`: quality 1.0, cost 0.6, latency 0.3, **availability 0.2**), that
is a `0.2` penalty. Meanwhile the cost term spans `w.cost = 0.6` across the eligible set,
and quality spans `w.quality = 1.0`. A dead endpoint therefore outranks a healthy peer
whenever the peer is worse on cost by more than a third of the set maximum, or worse on
quality by more than 0.2 — neither of which is exotic.

The Task 9 reviewer demonstrated it directly rather than arguing it: a fully dead endpoint
(`availability = 4.0e-08`, `src: measured`, `n = 10`) scored **+0.3800** against a
healthy but pricier peer at **+0.0400**, and **ranked first**.

The accurate statement, which is what §6 and `docs/reference/routing-observability.md`
now carry:

> Availability penalizes a candidate by **at most `w.availability`**. It never makes a
> candidate ineligible, and a dead endpoint **may still rank first**. Failover remains
> the only recovery mechanism.

This is not a bug in the formula; it is what a bounded additive preference *is*. It is
only a problem if one expects the preference to function as protection, which the false
sentence invited. Whether `0.2` is the right magnitude — large enough for a dead endpoint
to lose against a plausible cost delta, small enough not to swamp cost and quality — is
an empirical question this slice deliberately does **not** answer, and must not: changing
a weight here would break the premise of §3, that nothing in routing moves without a
measurement. It is filed as backlog **B45** for the successor slice to *measure*, since
P2b is the first thing in the programme able to say what the right magnitude is.

---

## 3. The identity property

**Statement.** With `signals.enabled: true` and an **empty** signal store, the new
formula produces scores **numerically identical** to the pre-P2a formula — equal scores,
not merely equal ordering.

**Why it holds.** The defaults are chosen to make it hold, and only those defaults do:

```
tier_latency_ms   = {cheap: 200, mid: 500, frontier: 1000}
latency_scale_ms  = 1000
```

so an unmeasured candidate's latency term is `200/1000, 500/1000, 1000/1000` =
`0.2, 0.5, 1.0` — the pre-P2a `_L` table exactly. An unmeasured availability is `1.0` —
the pre-P2a hardcoded constant exactly. Set-max normalization on latency would break
this, which is why §2.4 is not a style preference.

**Why it matters.** Three reasons, in increasing order of importance:

1. **Deployment safety.** Turning the feature on changes nothing until a measurement
   exists. No deployment's routing moves on upgrade.
2. **Attribution.** Every behaviour change after P2a is attributable to a measurement
   actually existing, rather than to a rewritten formula. When routing changes, there is
   exactly one candidate explanation.
3. **A real control arm for the successor.** `signals.enabled: false` is not a
   mock — it restores the same numbers with `src: "disabled"`, and the identity property
   is what guarantees the two arms of a P2b comparison differ *only* by the presence of
   measurement. Without it, "signals on" vs "signals off" would confound the measurement
   with a formula change and the comparison would be uninterpretable.

**What pins it.** `tests/routing/test_strategy_identity.py::
test_empty_signal_store_reproduces_the_legacy_scores_exactly` — Hypothesis, 200 examples
over candidate sets of 1–6 members with random tiers, costs and quality scores, asserting
`pytest.approx(rel=1e-12, abs=1e-12)` against a verbatim transcription of the pre-P2a
formula.

**Falsification.** The Task 6 reviewer mutated the latency term to candidate-set-max
normalization and confirmed the identity suite goes red (2 failed, 16 passed), then
reverted.

**And the finding worth recording.** The repo's *older* golden-oracle test,
`tests/test_routing_strategy.py::test_default_strategy_matches_pre_refactor_scoring`,
**survives that same mutation.** It compares *orderings* over a single-tier eligible set,
and within one tier a set-max latency term is constant across candidates and cannot
change an ordering. So an ordering-level oracle looks like coverage of a score-level
property and is not. The new test is the only thing in the repository pinning the
identity property. That generalizes into a methodological sentence for the paper:
**a characterization test's strength is bounded by the granularity of what it compares,
and an ordering comparison silently tolerates any monotone perturbation of the score.**
(Filed as backlog B44 so the older oracle is not mistaken for equivalent cover.)

> **Corrected 2026-09-19 (fix wave).** The "two-tier eligible set" scenario the set-max
> argument above warns against is one `Router.select` cannot actually produce — it
> filters `eligible` down to a single tier before scoring (see §1's correction), so a
> multi-tier rescaling never arises in production. The argument for fixed-scale over
> set-max still holds on its own terms (latency is absolute, cost is comparative), so
> this does not change the identity property or the decision it defends. It does mean
> the Hypothesis grid above (tiers sampled per-candidate from all three) generates
> candidate sets **wider than production ever exercises through this strategy**. That
> does not invalidate the test — it correctly pins that the formula reproduces pre-P2a
> scores for whatever tier mix `rank()` is handed, which is the honest scope of a
> strategy-level property (`RoutingStrategy` takes whatever eligible set its caller
> built, per `app/routing/strategy.py`'s module docstring) — but do not read the grid as
> evidence that production calls `rank()` with a multi-tier set today; it does not.

---

## 3A. Seeding by replay (signal hardening, 2026-09-21)

A follow-up slice (`docs/superpowers/specs/2026-09-21-signal-hardening-design.md`)
replaced boot seeding's source entirely: instead of aggregating `usage_records` — a
table that records a *different* quantity than the live signal observes — seeding now
replays a new per-attempt log, `endpoint_observations`, written by the same sink the live
signal already logs through. Branch `feat/signal-hardening`, commits oldest first:
`3519dc5` (observation-writer config knobs + positivity lint) · `f8c92d2` (the
`ObservationSink`, the wall-clock↔monotonic mapping, `observe_stream_success`,
`SignalStore.replay`) · `6a60244` (clock seam on `RoutingStrategy.rank`, closing B40) ·
`3286adc` (the `endpoint_observations` table; `write_observations` /
`load_seed_observations` / `prune_observations`; deletes `aggregate_model_signals` and
drops `usage_records` as a seeding source) · `ce626c1` (tie-break test for
`load_seed_observations`'s ordering) · `54f1954` (wires replay into boot, the background
flusher, the shutdown flush) · `f582181` (`Gateway.run_stream` observes availability +
TTFT; `BackendUnavailable.attempted_models`, closing B41) · `c3782cc` (error
`usage_records` rows + `error_count`, closing B38).

### The stale-as-fresh counterexample

Before this slice, `app/apps/store.py::aggregate_model_signals` stamped every seed row
`last_ts = time.monotonic()` — *now*, at boot time, regardless of when the underlying
`usage_records` row was actually written. A model whose most recent traffic was 23 hours
old therefore seeded as **fresh**: the staleness cliff (`signals.staleness_s`, default
3600s) never fired on seeded state, because every seeded row looked brand new the moment
the process booted. Seeded availability fared the same way — its wall-clock decay is
computed from elapsed time since the last observation, and every seeded observation
claimed to be from the instant of boot, so it experienced zero decay.

**Closed by `f8c92d2` and `54f1954`.** `f8c92d2` builds `SignalStore.replay`, which maps
each replayed row to a monotonic time that preserves the row's **true age** relative to
boot — `mono_i = row.ts_wall - wall_offset`, where `wall_offset = time.time() -
time.monotonic()` is captured once at process construction — rather than stamping "now".
`54f1954` wires that replay into the boot-seeding path in place of
`aggregate_model_signals`, which `3286adc` deletes outright: there is no longer a code
path that can stamp a seed row's age incorrectly, because there is no longer a separate
aggregation step computing an age at all.

### The replay-identity property

**Statement (spec §3.5).** For any sequence of observations, the state reached by
replaying the logged sequence equals the state reached by observing it live, up to a
constant shift of the monotonic clock. Every read (`latency()`, `availability()` — value,
source, and sample count) at the same age returns the same result whether the store got
there by live observation or by replay.

**Why it holds.** Every `SignalStore` update depends on time only through *differences*
(`now - ts` for the staleness cliff, `Δt` for availability decay), never on an absolute
clock reading. The replay clock (`mono_i = row.ts_wall - wall_offset`) preserves those
differences exactly — shifting every timestamp by the same constant leaves every
difference between two timestamps unchanged. So a store fed the sequence live and a
second store fed the identical sequence by replay land in the same state, modulo that one
constant shift.

**The Hypothesis test that pins it.**
`tests/routing/test_observations.py::test_replay_reproduces_the_live_state_up_to_a_clock_shift`
(200 examples): random interleavings of success, error, and stream-success observations
at increasing timestamps, replayed into one store and observed live into a second,
compared on every read at several later ages.

**The three falsifications (Task 2 Step 5, each applied by hand, confirmed red, then
reverted):**

| Mutation | Property expected to catch it | Result |
|---|---|---|
| `replay` uses `t = time.monotonic()` instead of the `ts - wall_offset` age mapping | replay identity + the stale-seed test | **caught** — `test_replay_reproduces_the_live_state_up_to_a_clock_shift` and `test_replay_honours_age_so_an_old_latency_is_stale` both failed (`2 failed, 6 deselected`) |
| the sink is left attached during `replay`, so replay re-logs what it reads | log-equals-live | **caught** — `test_replay_logs_nothing` failed (`1 failed, 7 deselected`) |
| `observe_stream_success` also updates the latency EWMA (mirroring `observe_success`'s math on `ttft_ms`) | stream-observation isolation | **caught** — `test_stream_success_moves_availability_and_leaves_latency_untouched` failed (`1 failed, 7 deselected`) |

### The cap bound

Replaying loads only the most recent `signals.max_seed_obs_per_model` rows per model
(default 200), not the full history — an unbounded seed query was itself one of the
defects this slice closes (noted, not yet named, at the end of P2a). The bound is
principled, not arbitrary: with `alpha_floor = 0.1`, once a signal has seen `n >= 9`
observations, each further update multiplies the influence of everything before it by
`0.9`. Replaying only the last `K` observations therefore differs from replaying the full
history by at most `0.9^(K-9) × range` of the observed values — for `K = 200` that factor
is ~2×10⁻⁹.
`tests/routing/test_observations.py::test_replaying_only_the_last_K_is_within_the_EWMA_bound`
asserts this bound computed from `K` and `alpha_floor`, not a hardcoded tolerance.

### The cross-replica consequence

`endpoint_observations` is one table shared by every API replica — each replica's live
traffic writes into it through its own flusher, and the seed a booting replica replays is
drawn from the union of *all* replicas' writes, interleaved by timestamp. That is a state
no single replica ever held live: the **live** signal store stays strictly per-process
(P2a's stance, unchanged — see `docs/reference/routing-observability.md`'s "per-replica
drift is real" caveat), but the **seed** a fresh or restarted replica boots with is
cross-replica. This is intended: it is what makes a restarted replica's seed more
informative than "whatever that one process happened to observe before it died."

### B39/B41 resolved by construction, not by patch

Both defects turn out to share a root cause with the stale-as-fresh one: each was a
property of *reading `usage_records` for seeding* — a table that records a different
quantity than the live signal observes. Once seeding reads `endpoint_observations`
instead — the exact log the live signal writes to through its own sink — both defects
stop being *possible*, rather than merely being patched:

- **B39** (whole-trace latency blending a semantic-cache hit's ~3 ms lookup, or
  Headroom's compression step, into a served model's seeded latency): a cache hit never
  calls a backend, so `SignalStore` never applies an observation for it and no
  `endpoint_observations` row is written for it. There is nothing to seed from, because
  there was nothing to log.
- **B41** (streaming contributing nothing): resolved directly — `Gateway.run_stream` now
  calls `observe_stream_success` on its terminal chunk (`f582181`) — but the same
  "resolved by construction" shape explains *why stream timing can never leak into the
  latency EWMA*: `observe_stream_success` only ever calls the availability-update path
  (`_apply_available`); there is no code path by which a stream's TTFT can reach
  `latency_ms`, `latency_n`, or `latency_ts`. Pinned by
  `test_stream_success_moves_availability_and_leaves_latency_untouched` and the third
  falsification above — this is the same test doing double duty as both a unit pin and a
  falsification probe.

---

## 4. Provenance

### 4.1 Four values

Every scoring term now reports where its value came from, per candidate, per router hop.

| `src` | Applies to | Means |
|---|---|---|
| `measured` | latency, availability | ≥ `min_samples` observations, not stale. `n` is the count. |
| `tier_default` | latency | no usable measurement; value is `tier_latency_ms[tier]`. `n` still reports the **true** count, which may be non-zero. |
| `neutral` | availability | fewer than `min_samples` observations; value is `1.0`. |
| `disabled` | both | `signals.enabled: false`. |

`quality.src` reuses the existing four-rung ladder (`customer` / `calibrated` /
`published` / `tier_default`), now emitted for every eligible candidate rather than only
the chosen one. `cost.cached` is `priced` / `unpriced` — whether the operator declared a
`price_cached_input` for that endpoint.

Both enum sets are closed **at the source**, not merely pinned by tests: `QUALITY_SOURCES`
is a module constant in `app/routing/quality.py`, and the four signal literals are the
only strings `SignalStore` can construct.

**The admission is the contribution.** A router that reports
`latency: 200ms (tier_default, n=0)` and one that reports
`latency: 342ms (measured, n=128)` have made very different claims, and before P2a they
were indistinguishable on the wire — both appeared as a number in a weighted sum. Most
routers are structurally unable to say the first sentence, because the placeholder and
the measurement occupy the same field with no accompanying label. Saying "I preferred
this endpoint on latency, and my latency figure was a tier default with zero samples" is
the thing this slice adds.

### 4.2 Reconstructibility

The trace record (`sp.scoring`) carries `weights`, `signals_enabled`,
`latency_scale_ms`, `max_latency_norm`, and per candidate `{name, score, quality{v,src},
latency{ms,src,n}, availability{v,src,n}, cost{est_usd,norm,cached}}`.

**The claim: recomputing every candidate's score from the record alone — no
`SignalStore`, no live config — reproduces the order the router returned.**

`tests/routing/test_scoring_record_replay.py` implements a deliberately naive replay that
reads *only* the serialized dict, after a `json.dumps`/`json.loads` round-trip (so the
property is pinned on the wire form that `sp.scoring` and the response certificate
actually carry, not on a Python object). 20 seeded parametrizations over random candidate
sets, tiers, costs and observation histories.

**Falsification (Task 8 review).** Deleting `max_latency_norm` from `to_record()` turned
all 20 cases red; repeating with `cost.norm` did the same. The replay helper genuinely
depends on the record being complete — it is not incidentally passing because it has a
back channel.

### 4.3 Placement: trace carries all, response carries the chosen one

| Surface | Carries | Why |
|---|---|---|
| Trace, per router hop | every **eligible** candidate | audit and replay surface |
| HTTP response certificate | the **chosen** candidate only, plus `weights`, `signals_enabled`, `latency_scale_ms`, `max_latency_norm` | on every chat completion; has to stay small |

Three consequences worth stating precisely:

- **Per hop, never collapsed.** A failover or confidence escalation produces a second hop
  with a different eligible set and different signal state. Collapsing them into one
  record would misdescribe the second.
- **A hop whose backend call *failed* also carries a record.** Its `chosen` is `null`,
  but the failed candidate is recoverable as `scoring.candidates[0].name` — not
  coincidentally: `router.py` takes `ordered[0]` and the strategy sorts best-first, so
  `decision.chosen` *is* `ordered[0]` by construction. Without this, a failed candidate's
  pre-failure provenance would be lost permanently, and failover is precisely where the
  availability signal is supposed to earn its keep.
- **Rejected candidates get no scoring entry.** They were never scored; inventing terms
  for them would be the same overstatement this slice exists to remove. Why they were
  rejected is already in `sp.candidates`.
- **The response form carries `latency_scale_ms`/`max_latency_norm` too (fix wave,
  2026-09-19),** alongside `weights`/`signals_enabled`, so a client can recompute the
  *chosen* candidate's score from the response alone. It still carries only one
  candidate, never the full eligible set, so it can verify that one score but can never
  reproduce the *ordering* among candidates it was never shown. Full replay of the
  ranking stays a trace property — do not overstate this into "the response is
  replayable."

The record is emitted **regardless of `capture_io`**, on the same footing as
`sp.candidates`, and is safe to do so because it is ids, enums and numbers only — pinned
by `test_the_record_contains_no_free_text` — and still runs through `redact_value`
defensively at emit time.

---

## 5. Results — the test suite as evidence

There is **no quantitative result here**, because there is nothing this slice claims to
improve. The evidence is coverage, falsification probes, and the three structural findings
of §1.1, §3 and §2.5.

**Suite.** Two different numbers get quoted for a pytest suite and they must be kept
apart, so both are given explicitly here:

- **Test *functions*** — what a reader counts by opening the files.
- **Collected test *cases*** — what pytest reports, with `@parametrize` and repeated
  fixtures expanded.

`tests/routing/` is **54 test functions** across 8 files, which pytest collects as **80
cases**. The slice added **67 test functions in total** — those 54, plus 5 in the new
`tests/test_onboard_cached_price.py`, 3 in `tests/test_config_lint.py`, 4 in
`tests/test_decision_certificate.py` and 1 in `tests/test_gateway_lateral_failover.py`.
`tests/test_routing_strategy.py` (11 functions / 13 collected) is **pre-existing and was
modified, not added** — it is the older golden oracle of §3 and is not part of the 67. The full suite
stands at **2,682 passed, 1 skipped, exit 0**.

| File | Functions | Pins |
|---|---|---|
| `test_signals.py` | 13 | store semantics: gates, staleness cliff, wall-clock decay, seeding, unknown tier, snapshot |
| `test_signal_wiring.py` | 15 | failure classification (see below); end-to-end gateway observation; the seed aggregate; one shared store across Router and Gateway through the real ASGI lifespan |
| `test_strategy_identity.py` | 7 (2 Hypothesis: 200 + 100 examples) | the identity property, the membership property, the clamp, `cached_pricing` keying |
| `test_scoring_record_replay.py` | 3 (20 parametrizations) | replay sufficiency on the wire form; per-term provenance; no free text |
| `test_pricing.py` | 6 | one pricing function; no invented discount; clamping |
| `test_cached_tokens.py` / `..._persistence.py` | 4 + 3 | capture from both usage shapes; persistence to `usage_records`; honest `0` on a cache hit |
| `test_signals_config.py` | 3 | the `capacity` alias; explicit `availability` wins; the defaults that produce the identity property |
| **total** | **54** | (80 collected cases) |

**Failure classification is covered in the interior of each branch, not at its
boundaries.** `counts_as_endpoint_unhealth` decides on `code == 429 or not (400 <= code
< 500)`, and the status codes the suite actually exercises are **400, 401, 403, 404, 422**
(client errors, expected `False`), **500, 502, 503, 504** (server errors, expected
`True`), **429** (the carve-out, expected `True`), a no-status exception, and
`ContextWindowExceeded`. **The boundary values — 399, 428, 430 and 499 — are untested**,
and those are precisely the edges of the range comparison and of the 429 carve-out. An
off-by-one in either (`400 < code`, `code <= 500`, `code >= 429`) would not be caught by
this suite. Filed as backlog **B46**. An earlier draft of this document claimed those
boundaries were covered; they are not, and the correction is recorded rather than quietly
edited.

**Falsification probes actually run** (each mutant applied by hand, verified red, then
reverted). Every verdict below was **re-run first-hand at Task 9 review** and confirmed;
the pass/fail counts are that reviewer's, not an earlier transcription:

| Mutant | Property expected to catch it | Verdict |
|---|---|---|
| latency term switched to candidate-set-max normalization | identity | **caught** — `tests/routing/test_strategy_identity.py`: 2 failed, 5 passed |
| …the same mutant, against the older ordering-only oracle | — | **survived** — see §3 |
| `max_latency_norm` deleted from `to_record()` | replay | **caught** (all 20 red) |
| `cost.norm` deleted from `to_record()` | replay | **caught** (all 20 red) |
| `cached_pricing` keyed by `display_name` instead of `name` | `test_cost_cached_pricing_is_keyed_by_name_through_router_select` | **caught** |
| `scoring=` removed from the successful `step="router"` record | certificate wiring | **caught** (2 of 4 new wiring tests red; the span test, built on a hand-crafted sample, correctly stayed green) |
| `app/api/admin_routes.py` reverted, model/registry changes kept | onboarding round-trip | **caught** (all 5 red) |

**Sample sizes, honestly.** The Hypothesis properties run at 200 and 100 examples over
candidate sets of at most 6; the replay property runs 20 seeded cases. This is a
regression net, not a search. Nothing here was run at the `deep` profile P1a defines, and
no property in this slice has ever found a bug that was not already known — unlike P1a's
harness, which found two.

**Structural findings, which are the real results:**

1. The `capacity` term was provably order-irrelevant for the product's entire history
   (§1.1).
2. An ordering-level characterization oracle silently tolerates any monotone perturbation
   of the score, and looked like coverage of a score-level property (§3).
3. **A committed design spec asserted a safety property the code does not provide** —
   *"a dead endpoint ranks last"* — and it was the stated reassurance for shipping with no
   circuit breaker. It survived the spec review, eight implementation tasks and their
   reviews, and the first draft of this document, and was caught only by someone computing
   two scores by hand (§2.5). The three findings share a shape worth a sentence in the
   paper: **each is a claim that no test could falsify, because nothing was comparing at
   the granularity the claim was made at** — a weight nothing observed, an ordering that
   hid a score, and a prose assertion with no executable counterpart at all. That third
   category is the one a test suite structurally cannot reach, and it is the argument for
   a document like this one existing.

---

## 6. What is NOT demonstrated

Read this section before writing any claim sentence. Every item below was verified in
this codebase during implementation review; none is hypothetical.

- **No claim that routing quality improves. Nothing here measures it.** Not latency saved,
  not cost saved, not answer quality, not regret against any oracle. The slice is shaped
  so P2b can ask the question honestly — that is its entire relationship to quality.

- **Streaming contributes no observations at all.** `observe_success` / `observe_error`
  appear only in `Gateway.run()`, never in `run_stream()`. A **streaming-only application
  therefore contributes nothing to the signal**, however much traffic it serves. Be
  precise about the second half of that sentence: the store is keyed **per model, not per
  application**, so whether those endpoints *report* a default depends on the rest of the
  deployment — any other application sending non-streaming traffic to the same model makes
  it `measured`, on that other application's traffic. It is the **single-tenant or
  all-streaming** deployment whose endpoints stay at `tier_default` / `neutral`
  indefinitely. (An earlier draft asserted the stronger, unqualified version.) This
  affects real clients today — the Qwen Code / kit-gtm agent clients on the Spark
  stream. Note the flip side, which is its own small hazard: an endpoint's signal may be
  measured entirely from traffic shaped nothing like the requests being ranked. It was
  a deliberate ruling, not an oversight: `run_stream` has no retry loop, so recording
  streaming successes without a comparable failure path would **bias** the signal, and a
  stream's time-to-last-token is semantically a different quantity from a non-streaming
  call's latency, so mixing them would corrupt the EWMA. Absent is honest; biased would
  not be. But absent is still absent, and a paper must not describe the signal as covering
  the deployment's traffic. (Backlog B41.)

  > **Update 2026-09-21 (signal hardening, `f582181`) — resolved.** `Gateway.run_stream`
  > now observes its terminal chunk as an availability-only success
  > (`observe_stream_success`) and observes an endpoint error under the same classifier
  > non-streaming calls use; it never touches the latency EWMA (§3A above, "B39/B41
  > resolved by construction"). The Qwen Code / kit-gtm correction two paragraphs above
  > stands unchanged — it was never about the streaming path, only about `tools` routing
  > those requests through `Gateway.run()` already. What was genuinely blind, and is now
  > fixed, was plain-chat streaming with no tools. Streaming still contributes nothing to
  > the *latency* signal specifically — that remains a deliberate ruling, not a gap.

- **Seeded availability is always neutral, because nothing ever writes
  `UsageRecord.status = "error"`.** `app/api/routes.py` writes exactly three values:
  `"ok"`, `"rejected"` and `"cancelled"`. When `pipeline.run()` raises —
  `BackendUnavailable`, `NoEligibleCandidate`, precisely what a failing endpoint
  produces — the top-level handler returns an `HTTPException` and `record_usage` is never
  reached, so **no row is written at all**. A request that succeeds via failover persists
  only the winning model's row. So `aggregate_model_signals`'s
  `status.in_(("ok","error"))` filter is correct against the *documented* enum while its
  error branch is **dead against real traffic**: latency seeds correctly, availability
  always seeds at `1.0`. After a restart a badly-failing endpoint starts neutral and needs
  `min_samples` live observations to be down-weighted again. Bounded, but it means the
  boot-seeding mechanism is half as capable as the design describes. (Backlog B38.)

  > **Update 2026-09-21 (signal hardening, `c3782cc`) — resolved.** A failed request now
  > writes one `usage_records` row with `status = "error"` (`app/api/routes.py::
  > _record_failed`), and — separately — boot seeding no longer reads `usage_records` at
  > all; it replays `endpoint_observations`, populated by the same sink the live signal
  > writes through. Availability now seeds correctly. One narrower gap remains, filed as
  > backlog B47: a backend failure on a real-token stream *after* the client has already
  > disconnected still writes no row of either kind. Also note, orthogonally: the error
  > row's `sensitivity` field is `"-"` on the non-streamed and keepalive paths (the route
  > doesn't hold the gate's policy context there); only the real-token stream failure path
  > has it available and records the true value.

- **The whole-trace `latency_ms` sum blends three different sources into one boot-seeded
  EWMA, not just confidence escalation.** `_finalize` sums `latency_ms` across the whole
  trace and attributes the sum to the final model. The confidence-escalation case is the
  one first recorded: an escalated-after-success request charges the earlier hop's time to
  the model that answered second. But the same sum also folds in `CompressionRecord
  .latency_ms` and `CacheRecord.latency_ms` whenever those steps ran, so what gets
  **seeded** is whole-pipeline latency while `Gateway.run`'s `observe_success` records
  **backend-only** latency per hop — two different quantities under one `measured` label.
  With the shipped defaults (Headroom off, semantic cache off) it collapses to the
  escalation case. **With Headroom on — a shipped, advertised feature — every compressed
  request's compression-step latency adds to the served model's seeded figure, uniformly,
  not only on escalations: a systematic upward bias, not just an escalation artifact.**
  A semantic-cache hit is a related but distinct pollution source, separately findable: it
  writes an `ok` row stamped with the served model's slug but carrying only the cache
  lookup's ~3 ms latency, which can seed a heavily-cached model at an artificially low
  `measured` latency that outranks a genuinely faster never-cached peer (mitigated today
  only by `semantic_cache.enabled: false` being the default). Live per-hop observation is
  correct in all cases; only the *seeded* value is affected, and pure failover is
  unaffected (failed-hop records are hardcoded `latency_ms = 0.0`). Self-correcting as live
  observations accumulate. (Backlog B39, widened in the fix wave that reviewed this
  artifact.)

  > **Update 2026-09-21 (signal hardening, `3286adc`) — resolved, not patched.** Boot
  > seeding no longer averages `usage_records.latency_ms` at all — `aggregate_model_
  > signals` is deleted, and seeding reads `endpoint_observations` instead, which holds
  > only `Gateway.run`'s own per-hop, backend-only latency. Both blending sources named
  > above stop being possible rather than being special-cased: a semantic-cache hit never
  > calls a backend and so is never logged (§3A, "B39/B41 resolved by construction"), and
  > a compression or escalation hop's latency was never something `Gateway.run`'s
  > `observe_success` included in the first place — only `_finalize`'s whole-trace sum did,
  > and that sum is no longer what gets seeded.

- **The signals are per-replica, and no claim of global consistency is made.** The store
  is per-process; each API replica learns from its own traffic, so two replicas can hold
  different latency estimates for the same model and order the same eligible set
  differently. This is acceptable *because* of §2.1 — a ranking preference cannot affect
  eligibility, cost caps or residency — and boot seeding bounds the divergence. Cross-replica
  sharing is deliberately not built. A paper must not describe this as a fleet-level signal.

- **Cached-token capture depends on the provider reporting
  `prompt_tokens_details.cached_tokens`.** A provider that does not report it yields `0`,
  **indistinguishable from a genuine cache miss.** There is no way, from the response
  alone, to tell "this provider has no prompt cache", "this provider has one and it
  missed", and "this provider has one, it hit, and it does not tell you". All three read
  as zero savings.

- **`min_samples`, `half_life_s`, `staleness_s` and `alpha_floor` are chosen defaults,
  not tuned or validated against production traffic.** `alpha_floor` is not even exposed
  in `config/routing.yaml` — it exists only as a schema default. No sensitivity analysis
  was run on any of them. The claim "5 observations is enough to call a latency measured"
  has no evidence behind it in this work.

- **The identity property is proven against the pre-P2a *implementation*, not against
  any external notion of correct routing.** It says "we changed nothing without a
  measurement". It says nothing about whether the pre-P2a scoring was right, and
  reproducing a formula exactly is equally consistent with reproducing a bad formula
  exactly. Two further narrowings: the oracle is a *verbatim transcription* of the pre-P2a
  formula into the test file, not the pre-P2a code checked out and executed (the same
  narrowing P1a records in its §6); and the property is stated over the three canonical
  tiers only — for an off-enum tier the pre-P2a code raised `KeyError` and the new code
  returns `latency_scale_ms`, a deliberate divergence covered by
  `test_an_unknown_tier_falls_back_without_raising`.

- **No test can exercise staleness or availability decay *through* the strategy.**
  `DefaultRoutingStrategy.rank` calls the store with no injectable clock, so both paths
  are covered at the store level only. Production is correct (writer and reader share
  `time.monotonic()`), and it forced two strategy tests to observe on the real clock, but
  the seam between the two layers is unexercised for time-dependent behaviour. (Backlog B40.)

  > **Update 2026-09-21 (signal hardening, `6a60244`) — resolved.** `rank` on the
  > `RoutingStrategy` Protocol and `DefaultRoutingStrategy` gained `now: float | None =
  > None`, forwarded to both `SignalStore` reads; `Router.select` passes nothing, so
  > production still reads the real clock. Two new tests exercise the staleness cliff and
  > the availability decay *through* `rank(now=...)` for the first time.

- **`SignalStore` reports `samples = 0` on the `disabled` path** even when observations
  exist, unlike `tier_default` / `neutral`, which report the true count. So the record's
  own honesty convention is not uniform across its four provenance values. (Backlog B42.)

- **The evidence is qualitative and the properties are a regression net.** 200/100/20
  examples, no `deep`-profile search, no mutation score computed over a seeded fault
  population, no comparison against an example-based suite of matched effort. Unlike P1a,
  no property in this slice found a previously unknown bug. Do not let a draft imply a
  measured adequacy figure.

- **This is not cold-start routing.** Boot seeding reads recorded traffic — `usage_records`
  as originally built, `endpoint_observations` since the 2026-09-21 signal-hardening
  slice (§3A) — traffic that already happened, either way. An endpoint onboarded one
  minute ago has no signal and reports defaults, which is exactly the setting IP-8 / R8
  proposes to address and this slice does not.

- **TTFT is logged, not validated as a routing signal.** `Gateway.run_stream` records
  time-to-first-token on every streamed success and persists it in
  `endpoint_observations.ttft_ms`, but nothing reads it back into a score — `latency()`
  and `availability()` are the only two signals `DefaultRoutingStrategy.rank` consults,
  and neither is computed from `ttft_ms`. It is logged for P2b to evaluate, not something
  this slice claims works as a ranking input. (Spec §3, out of scope.)

- **A flusher crash loses at most `flush_interval_s` of observations, and the loss is
  asymmetric.** The background flusher (`app/main.py::_observation_loop`) drains the
  in-memory sink into `endpoint_observations` every `signals.flush_interval_s` seconds
  (default 5.0), with a final flush on clean shutdown. A process crash between two
  flushes — not a graceful shutdown, which is covered — loses whatever the sink held
  unflushed. The loss is asymmetric with what the *live* store already knows: the
  observations were already applied to the in-process `SignalStore` before being handed
  to the sink, so the crashing replica's own ranking reflected them; only a *future* boot's
  replay-seed misses them, and only for the window since the last successful flush.

- **No circuit breaker, and availability is a far weaker substitute than it looks.**
  Availability never removes a candidate, and the penalty it applies is bounded by
  `w.availability` — `0.2` with the shipped weights. **A dead endpoint may still rank
  first**, and the reviewer demonstrated exactly that: a fully dead endpoint at `+0.3800`
  beat a healthy pricier peer at `+0.0400` (§2.5). Failover is the only recovery
  mechanism, and CLAUDE.md's step-7 "breakers" is still unimplemented. Whether `0.2` is
  the right magnitude is unmeasured and is filed as backlog B45 for the successor slice.
  **Do not write, in any draft, that a dead endpoint is de-preferred, deprioritized or
  ranked last** — the honest verb is *penalized, boundedly*.

- **The console does not surface any of this.** API and trace only, on the same footing
  as P1a's console-controls gap.

---

## 7. Relationship to P1a and P1b

The three slices compose into one claim, and it is narrower than it first sounds.

| Slice | Question it makes answerable from the record |
|---|---|
| **P1a** — policy compiler | *What protection and placement applied, per axis, and where did each value come from?* (ceiling / app / preset / default), bound into `app_policy_version` |
| **P1b** — endpoint declarations + one eligibility seam | *Who was admissible, and which declared endpoint attribute excluded each one?* (`sp.candidates` with stable exclusion codes; `sp.endpoint_registry_version`) |
| **P2a** — this slice | *Among the admissible, how was each one scored, on what values, and was each value measured or a constant?* (`sp.scoring`) |

**The combined claim, stated no wider than §6 permits:**

> A RouteForge routing decision is reconstructible from its own emitted record — the
> policy that applied and where each axis's value came from, the eligible set and the
> reason each rejected candidate was excluded, and every scored candidate's term values
> with each term's provenance, *including the explicit admission that a term was a
> constant.*

Three things that claim does **not** say, each of which a reader will try to read into it:

1. **Not that the decision was good.** Reconstructibility is orthogonal to quality. A
   fully documented bad decision is still bad, and this work would document it just as
   faithfully.
2. **Not that the record is complete for a *conversation*.** It is complete per request
   and per hop. Cross-request state — an agent loop's accumulated context, a session's
   escalation history — is outside all three slices. (P1a's §9 names the per-conversation
   ratchet as future work for the same reason.)
3. **Not that the terms measure what they name.** P2a made two of four terms measurements
   and labelled the other two honestly. `availability` is a success-rate EWMA, not
   capacity; `quality` is usually still a tier default; `cost` is an estimate at ranking
   time and a recorded actual afterward. The record does not claim otherwise — that is
   what the provenance field is for — but a paper sentence saying "measured signals"
   without qualification would.

**What this buys the successor.** P2b needs three things P2a supplies and P1a/P1b do not:
a replayable per-hop scoring record to compute regret over (§4.2), a control arm that
differs from the treatment arm *only* by the presence of measurement (§3), and a
provenance field that lets the analysis exclude decisions in which the term under study
was a constant — which, given §6's streaming and seeding limitations, will be a large
fraction of real traffic and must not be silently pooled with the measured ones.

---

## 8. Reproducing it

```bash
git clone <repo> && cd routeForge
git checkout feat/p2a-routing-signal-measurement     # merge base 5a61a0a
uv sync --group dev

# the P2a suite: 54 test functions, 80 collected cases, ~4 s
LITELLM_LOCAL_MODEL_COST_MAP=True uv run pytest tests/routing -o addopts="" -q

# with the older golden oracle alongside it (65 functions, 93 collected)
LITELLM_LOCAL_MODEL_COST_MAP=True uv run pytest \
    tests/routing tests/test_routing_strategy.py -o addopts="" -q

# the wiring the record depends on
LITELLM_LOCAL_MODEL_COST_MAP=True uv run pytest \
    tests/test_decision_certificate.py tests/test_candidate_export.py \
    tests/test_gateway_lateral_failover.py tests/test_onboard_cached_price.py \
    -o addopts="" -q
```

`LITELLM_LOCAL_MODEL_COST_MAP=True` is not optional: without it LiteLLM fetches a cost map
over the network at import and pytest wedges in D-state on an offline machine. Note also
that `pyproject`'s `addopts="-q"` plus an explicit `-q` yields `-qq`, which suppresses the
summary line — hence `-o addopts=""` wherever a count is wanted.

**Layout.**

| File | Contents |
|---|---|
| `app/routing/signals.py` | `SignalStore`, `SignalValue`, `SeedRow` — the two EWMAs, the gates, the decay |
| `app/routing/strategy.py` | `TermValue` / `ScoredCandidate` / `RankResult` / `to_record()`, and `DefaultRoutingStrategy` |
| `app/routing/pricing.py` | `price_call` — the one cost formula (was three copies) |
| `app/apps/store.py::aggregate_model_signals` | the boot-seed aggregate |
| `app/gateway/gateway.py::counts_as_endpoint_unhealth` | the failure classifier of §2.3 |
| `app/obs/export.py` | `sp.scoring` emission |
| `app/pipeline.py::_chosen_scoring` | the response-certificate narrowing |
| `tests/routing/` | the suite table of §5 |

**Mutation protocol.** Each mutant is a single edit to live code that breaks exactly the
behaviour one property asserts over; "caught" means that property went red. Mutants were
applied by hand and reverted immediately. Verdicts and the reviewer notes behind them are
in `.superpowers/sdd/2026-09-19-routing-signal-measurement-p2a/progress.md` and the
per-task reports.

---

## 9. Provenance

| | |
|---|---|
| Design | `docs/superpowers/specs/2026-09-19-routing-signal-measurement-p2a-design.md` |
| Per-task briefs, reports, and every ruling | `.superpowers/sdd/2026-09-19-routing-signal-measurement-p2a/` |
| Operator-facing contract | [`docs/reference/routing-observability.md`](../reference/routing-observability.md) §3 |
| Endpoint pricing declaration | [`docs/guides/onboarding-models.md`](../guides/onboarding-models.md) §(b2) |
| Limitations, as backlog items | [`docs/backlog/bugs.md`](../backlog/bugs.md) B38–B46; B30 resolved |
| Predecessor evidence file | [`docs/research/policy-compiler-noninterference.md`](policy-compiler-noninterference.md) |
| IP / research menu, and its verification caveat | [`docs/roadmap/ip-and-research-opportunities.md`](../roadmap/ip-and-research-opportunities.md) |

Commits, oldest first: `02122ee` (spec) · `67d4900` (plan) · `570494c` (one `price_call`
with an optional cached-input rate) · `6c8776b` (capture and persist provider cached
prompt tokens) · `fc15437` (thread them from `GenerationResult` into `resp["usage"]` — the
chain was broken between the backend and the meter) · `942cfc3` (declare a cached-input
rate per onboarded endpoint) · `736d170` (the per-model signal store) · `90b6043` (signals
config, `capacity`→`availability`, lint rules) · `bff7913` (score on measured signals, with
per-term provenance) · `6f03b8b` (observe latency and availability; seed them at boot) ·
`f3206f1` (emit a replayable scoring record per hop) · `3df3f9d` (wiring coverage;
failed-attempt provenance).

### Related work — TO VERIFY BEFORE CITING

⚠️ **Nothing below has been verified.** Every arXiv identifier in the roadmap came from
automated search and is a lead, not a citation; that document's own caveat says a
meaningful share of the 2026-dated IDs could not be independently confirmed. Check each
against arxiv.org before it appears in a draft. "Nobody does X" means "not found by
targeted search in one pass".

Threads, with what each needs to establish:

1. **Learned and predictive LLM routing** — RouteLLM, RouterBench, RouterEval, UniRoute,
   WISERouter. *Establish:* that these optimize over a **fixed** candidate set with
   offline-trained predictors, and that none reports per-term provenance or distinguishes
   a measured term from a constant on its output. The contribution framing depends on
   that distinction being unclaimed.
2. **Observability and provenance in ML systems** — data/model lineage, ML metadata
   stores, "why did the model do that" tooling. *Establish:* whether anything treats the
   *scoring function's own inputs* as first-class provenance, as opposed to the model
   artifact's lineage.
3. **Adaptive load balancing and EWMA-based peer selection** — least-outstanding-request,
   EWMA and P2C in service meshes (Envoy, Finagle), outlier detection and passive health
   checking. *Establish:* the closest prior art for §2.2 by far, and be honest that the
   EWMA machinery is standard. The claim is not the estimator; it is the estimator's
   subordination to an eligibility authority (§2.1) and the labelling of unmeasured values
   (§4.1).
4. **Configuration knobs with no observable effect** — dead/inert configuration, feature
   flags that never branch, the systems literature on misconfiguration. *Establish:*
   support for §1.1's framing that an inert-but-weighted knob is a distinct defect class
   from a wrong knob, and that nothing in normal operation can reveal it.
5. **Cold-start and zero-label routing** — the IP-8 / R8 cluster. *Establish:* the boundary
   between P2a (warm restart from recorded traffic) and IP-8 (probe battery before any
   traffic). They are complementary, and a draft that blurs them overclaims.
6. **Regret, oracles and offline evaluation of routers.** *Establish:* what P2b will need,
   and that replayability of a per-hop decision record is the substrate those methods
   assume and rarely get from a production system.

**Future work, honestly scoped.** (a) P2b: the feasible-oracle benchmark and regret
analysis, with the control arm §3 provides and the measured/unmeasured partition §4.1
makes possible. (b) Close B38 so availability can seed, and B41 so streaming traffic
contributes at all — until both land, the signal describes a subset of the deployment's
traffic. (c) A clock seam on `rank` (B40), which is what would let the staleness and decay
behaviour be exercised through the layer that consumes it. (d) **The magnitude of
`weights.availability` (B45)** — a weight sweep against the benchmark, with injected
endpoint failures, reporting the regret curve. This is the first question P2b should be
pointed at, because §2.5 shows the shipped `0.2` does not do what the design spec claimed
it did, and it is the only open item here that is a *product* question rather than a
coverage or plumbing one. (e) The four missing boundary cases in the failure classifier
(B46) — four lines, no production change. (f) Cross-replica sharing — only worth building
if P2b shows per-replica drift materially affects outcomes. Measure first.
