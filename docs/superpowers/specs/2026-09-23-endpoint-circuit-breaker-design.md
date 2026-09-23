# Endpoint circuit breaker (step 7) — design

**Date:** 2026-09-23 · **Status:** approved direction (owner: "go with your recommendations");
**built** on `feat/endpoint-circuit-breaker` and **amended 2026-09-23** in §3, §4, §5, §6, §7
and §8 to match what was built (each amendment is marked and says why) · **Closes:** the open
half of backlog **B45** · **Measured:** `docs/research/feasible-route-bench.md` §12

## 1. Why

B45 is now measured twice, once simulated (research doc §9, 30 runs) and once live (§11.2).
Both runs agree. `weights.availability` is a bounded ranking preference: a dead endpoint loses
at most `w` points of score. With the shipped `w = 0.2`, the online router sent **every**
outage request to a dead endpoint that was 4× cheaper, and the same happened at `w = 1`.
Raising the weight far enough to change that would also override genuine cost differences
between two healthy models on every request.

So "take a dead endpoint out of rotation" is not a scoring question. It belongs to a
**circuit breaker**, which step 7 of the architecture has always named ("adapters, quotas,
**breakers**, fallback") but which was never built. Today the only protection is failover,
which still pays one wasted attempt plus the failure latency on every request during an
outage. Streaming requests have no failover at all, so they fail outright.

## 2. Scope

**In:**
- a per-endpoint breaker in the gateway, fed by the gateway's existing
  `counts_as_endpoint_unhealth` verdicts;
- an open circuit is excluded from routing, through the one eligibility seam, with its own code;
- a bypass when every eligible candidate is open;
- configuration, the console/admin view, `/metrics`, and trace provenance;
- a FeasibleRouteBench arm that models it, plus re-measuring the outage scenario.

**Out:**
- **Cross-replica shared breaker state.** It is per process, like `SignalStore`, so each
  replica trips on its own failures.
- **Active health probing.** Recovery is driven by trial traffic (half-open), not a pinger.
- **Changing `weights.availability`.** It stays 0.2 as a ranking preference.
- **Auto-disabling the registry row.** That is an operator decision, and it stays one.

## 3. State machine (`app/gateway/breaker.py`)

One `CircuitBreaker` holds a small state record per model name. It has three states.

```
closed ──(failure_threshold consecutive unhealth failures)──▶ open
open   ──(cooldown elapsed)────────────────────────────────▶ half_open (admitted to every request)
half_open ──first reported success──▶ closed   (failures and cooldown reset)
half_open ──first reported failure──▶ open     (cooldown doubles, capped at max_cooldown_s)
```

> **Amended 2026-09-23 (as built).** The diagram originally read "first request asks → one
> trial lease". Half-open is **evidence-based** and has **no lease**; see the two amended
> bullets below.

- **What counts.** Only failures for which `counts_as_endpoint_unhealth(exc)` is true:
  no-status transport errors, 5xx and 429. A 4xx, including context overflow, is the
  request's fault and never trips the breaker. This is the same rule the availability
  signal uses, and for the same B36 reason.
- **What resets.** Any success, whether streamed or not, resets the consecutive-failure
  count. A success is the same event that calls `observe_success` /
  `observe_stream_success`.
- **Half-open is evidence-based.** *(Amended 2026-09-23; this bullet originally specified
  a single-request trial lease.)* Before the cooldown elapses, a reported success or failure
  changes nothing: no close, no reopen, no change to the count or the cooldown, and
  `consecutive_failures` stays the count that tripped it. Once the cooldown has elapsed, the
  first success reported for the model closes it and the first failure reopens it with the
  cooldown doubled, whoever reports it. **Why:** the gateway has no per-request lease token,
  and a result reported after the cooldown *is* current evidence of the endpoint's health;
  adding tokens would have changed the router and gateway interfaces. Cost: a straggler
  failure that started before the cooldown ended but reports after it can reopen the circuit
  once (one extra cooldown).
- **No single-trial lease; half-open admits every request.** *(Amended 2026-09-23; the
  original bullet granted one trial lease from `open_set()`, released on the result or after
  `trial_timeout_s`.)* After the cooldown the model is admitted to every request until the
  first reported result. **Why the lease was dropped:** `open_set()` runs before policy and
  scoring, so the lease went to whichever request read it first, including requests that
  could never use the model (narrower app scope, another tier, over budget, or a cheaper peer
  outscoring it). Each such request burned the lease, and a recovered endpoint stayed
  excluded for another `trial_timeout_s` (the Task 4 review reproduced three eligible
  requests diverted this way). With evidence-based half-open the lease added no safety, and
  moving it after feasibility and scoring would have put more code on the privacy seam.
  **Accepted cost:** during the half-open window, concurrent in-flight requests may each
  spend one attempt on a still-dead endpoint, bounded by concurrency × failure latency.
  Failover serves them; a streamed request fails that one call.
- **Clock seam.** Every method takes an injectable `now` (monotonic), as `SignalStore`
  does, so the state machine can be tested without sleeping.
- **Boot state is closed.** Nothing is replayed from `endpoint_observations`. A breaker
  that opened before a restart re-trips after `failure_threshold` failures, which is at
  most three wasted attempts.

## 4. Where it applies: the eligibility seam, never a post-hoc check

- **New `feasible_set` stage.** `feasible_set` gains `open_circuits: frozenset[str]`, a new
  stage `circuit_open` with the reason "excluded · circuit open".
  - It is the **last** stage (stage 7, after `model_scope`), so every policy reason —
    placement, data handling, prior failure, app scope, pinned model, registry gates — wins
    over it, and `circuit_open` only ever labels a candidate policy fully admitted.
    *(Amended 2026-09-23; this bullet originally placed it right after `prior_failure`,
    stage 3.)* **Why:** every policy reason then wins. Placed after `prior_failure`, an open
    model outside the app's scope would have read `circuit_open` instead of `app_scope`.
    With the stage last, the set it removes from is exactly the policy-eligible set, so the
    all-open bypass below plainly restores that set and nothing more.
  - Like `exclude`, it can **only remove** candidates. It is structurally unable to widen
    placement: an `onprem_only` request whose on-prem models are all open never reaches
    cloud.
- **Every selection passes it.** `Gateway.run` and `Gateway.run_stream` pass
  `breaker.open_set(now)` into every `Router.select`, including each failover and
  escalation re-select. `Router.select` threads it through to `feasible_set`.
- **All-open bypass.** Suppose the breaker exclusion is the *only* reason the eligible set
  is empty: the set is non-empty without `open_circuits`, but becomes empty with it.
  - `Router.select` then re-selects **without** `open_circuits` and records it on the
    decision: `reason` gets the suffix `"; circuit bypass: every eligible candidate is open"`,
    and the decision gets `circuit_bypass: true`.
  - Rationale: rejecting would turn an availability problem into a guaranteed failure,
    while trying the endpoint may succeed if it has recovered. That is today's behaviour,
    so the breaker never makes a request worse off than no breaker would.
  - Privacy is unaffected, because the bypass only restores candidates that policy
    already admitted.
- **Streaming benefits most.** A stream has no failover, so excluding an open endpoint up
  front is the only protection a streamed request can get.

## 5. Configuration (`config/routing.yaml`, which owns failover and scoring)

```yaml
breaker:
  enabled: true
  failure_threshold: 3     # consecutive unhealth failures that open the circuit
  cooldown_s: 30           # first open period; doubles on each failed trial
  max_cooldown_s: 300
```

> **Amended 2026-09-23.** `trial_timeout_s` was removed from `BreakerCfg`, `routing.yaml`,
> the lint and the tests along with the trial lease (§3).

- **Validation.** A `BreakerCfg` Pydantic model gets an entry in the config lint: all
  values must be positive, `max_cooldown_s ≥ cooldown_s`, and the block has one owner
  (`routing.yaml`).
- **`enabled: false`** makes `open_set()` always empty, which gives exactly today's
  behaviour. That is the control arm.
- **`routing_policy_version`** already hashes `routing.yaml`, so turning the breaker on or
  off changes the version stamped on every trace.

## 6. Observability

- **Trace.** An excluded open model appears in `rejected` / `sp.candidates` with the code
  `circuit_open`, and a bypass is visible on the router record's reason and
  `circuit_bypass`. Both are emitted regardless of `capture_io`, since they are ids and
  enums only.
- **`/metrics`.** One new bounded family, `routeforge_breaker_transitions_total{to_state}`,
  with `to_state` ∈ `open | half_open | closed`. It has no model label, keeping to the
  exporter's rule of no unbounded labels. The docs' "four families" becomes five.
- **Admin.** `GET /admin/infra/models` adds a `circuit` object per model:
  `{state, consecutive_failures, open_until_s}`. *(Amended 2026-09-23: the field is
  `open_until_s`, seconds until the cooldown ends, set only while `state` is `open` and
  `null` otherwise, because a monotonic timestamp means nothing to a client and a half-open
  model's elapsed deadline would render as a negative countdown.)* It is read-only and
  reflects this replica's view, which the docs say. The console Infrastructure table shows
  the state as a badge.
- **Logs.** Each transition logs one structured line: model, from, to, cooldown.

## 7. FeasibleRouteBench

- **Breaker in the online arm.** `OnlineArm` (`app/eval/strategies.py`) gets a
  `CircuitBreaker` (the same class) fed only by its own prior attempts. Its attempt chains
  pass `open_set(case clock)` to `Router.select`, exactly as the gateway does. The
  existing leak guards cover it: it sees only prior attempts.
- **Control arm.** Breaker on or off follows `settings.routing.breaker.enabled`, so
  `--variants` gains nothing new. *(Amended 2026-09-23: no variant file is needed.)* When the
  base config has the breaker on, the runner adds a control arm,
  `router_online:breaker_off`: the base weights with the breaker disabled. The reported arm
  is `router_online:<variant>`.
- **Case clock.** *(Added 2026-09-23.)* The arm's breaker runs on the same synthetic case
  clock as its signal store, so `seconds_per_case` decides how many cases a cooldown covers.
  At 30 s per case the first 30 s cooldown ends exactly at the next case; at
  `seconds_per_case ≥ max_cooldown_s` the breaker can never exclude anything and the two arms
  are identical. Production uses wall-clock time, so that null is the bench's, not
  production's (research doc §12.2).
- **Re-measure the outage scenario.** Run `outage-sim` at seeds 0–9 × 1/30/300 s per case,
  breaker on vs off, and report wasted attempts, cost and latency regret, and the
  post-recovery lag. The expected shape is about `failure_threshold` wasted attempts at
  the outage start, plus one per failed trial, instead of one per outage case. **This is
  a prediction and must be measured, not assumed.** A live re-run with
  `scripts/outage_proxy.py` follows if the owner wants it.

## 8. Testing (TDD)

- **State machine.** Threshold, reset on success, cooldown doubling and cap, and "4xx never
  trips" (context overflow, 400, 401), with 429 and 5xx tripping. *(Amended 2026-09-23: the
  lease tests were replaced by evidence-rule tests — results before the cooldown elapses
  change nothing, every caller is admitted while half-open, and an ineligible request cannot
  starve a recovered endpoint.)*
- **Seam.** An open model is excluded with the code `circuit_open`. Placement reasons win.
  `onprem_only` with every on-prem model open never yields cloud.
  *(Amended 2026-09-23: this is pinned by the example test
  `tests/test_router_breaker.py::test_bypass_never_widens_placement`, not a Hypothesis
  property test.)*
- **Bypass.** With all candidates open, one is still selected, `circuit_bypass` is set,
  and the candidate set equals the policy-eligible set.
- **Gateway.** Failover excludes the open endpoint up front, so a second request makes no
  wasted attempt. Streaming excludes it too. A half-open trial success closes the circuit.
- **Identity.** With `enabled: false` the decision trace is byte-identical to today's on
  the existing suites.
- **Bench.** The online arm with the breaker lowers wasted attempts on the outage scenario
  versus off. The test asserts only direction and determinism per seed. The magnitude
  goes in the research doc.

## 9. Docs

- `docs/reference/routing-observability.md`: the breaker, the `circuit_open` code, the
  metric, and per-replica state.
- `docs/research/feasible-route-bench.md`: new §12 with the breaker measurement.
- Backlog **B45** marked resolved.
- `routing-and-gateway` skill: the breaker's place in step 7.

## 10. Deploy

- **No schema change**, so no ALTER.
- **The first deploy changes behaviour:** repeated endpoint failures now take a model out
  for 30–300 s. The release notes say so.
