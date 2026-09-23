# Endpoint circuit breaker (step 7) — design

**Date:** 2026-09-23 · **Status:** approved direction (owner: "go with your recommendations"),
spec awaiting review · **Closes:** the open half of backlog **B45**

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
open   ──(cooldown elapsed, first request asks)────────────▶ half_open (one trial lease)
half_open ──trial succeeds──▶ closed   (failures and cooldown reset)
half_open ──trial fails─────▶ open     (cooldown doubles, capped at max_cooldown_s)
```

- **What counts.** Only failures for which `counts_as_endpoint_unhealth(exc)` is true:
  no-status transport errors, 5xx and 429. A 4xx, including context overflow, is the
  request's fault and never trips the breaker. This is the same rule the availability
  signal uses, and for the same B36 reason.
- **What resets.** Any success, whether streamed or not, resets the consecutive-failure
  count. A success is the same event that calls `observe_success` /
  `observe_stream_success`.
- **Half-open allows one request at a time.** When the cooldown has elapsed, the first
  `open_set()` read grants a single **trial lease** to one request, and the model is
  reported as not-open for that one selection only. Every other request still sees it as
  open until the trial reports its result. The lease is released on success or failure
  and also expires after `trial_timeout_s`. That covers a trial whose caller vanished,
  such as a client disconnect.
- **Clock seam.** Every method takes an injectable `now` (monotonic), as `SignalStore`
  does, so the state machine can be tested without sleeping.
- **Boot state is closed.** Nothing is replayed from `endpoint_observations`. A breaker
  that opened before a restart re-trips after `failure_threshold` failures, which is at
  most three wasted attempts.

## 4. Where it applies: the eligibility seam, never a post-hoc check

- **New `feasible_set` stage.** `feasible_set` gains `open_circuits: frozenset[str]`, a new
  stage `circuit_open` with the reason "excluded · circuit open".
  - It sits right after `prior_failure` (stage 3), so placement and data-handling reasons
    are never relabelled by it.
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
  trial_timeout_s: 60      # a half-open trial lease expires after this
```

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
  `{state, consecutive_failures, open_until}`. It is read-only and reflects this replica's
  view, which the docs say. The console Infrastructure table shows the state as a badge.
- **Logs.** Each transition logs one structured line: model, from, to, cooldown.

## 7. FeasibleRouteBench

- **Breaker in the online arm.** `OnlineArm` (`app/eval/strategies.py`) gets a
  `CircuitBreaker` (the same class) fed only by its own prior attempts. Its attempt chains
  pass `open_set(case clock)` to `Router.select`, exactly as the gateway does. The
  existing leak guards cover it: it sees only prior attempts.
- **Control arm.** Breaker on or off follows `settings.routing.breaker.enabled`, so
  `--variants` gains nothing new. A `breaker_off` variant file sets it off as the control
  arm.
- **Re-measure the outage scenario.** Run `outage-sim` at seeds 0–9 × 1/30/300 s per case,
  breaker on vs off, and report wasted attempts, cost and latency regret, and the
  post-recovery lag. The expected shape is about `failure_threshold` wasted attempts at
  the outage start, plus one per failed trial, instead of one per outage case. **This is
  a prediction and must be measured, not assumed.** A live re-run with
  `scripts/outage_proxy.py` follows if the owner wants it.

## 8. Testing (TDD)

- **State machine.** Threshold, reset on success, cooldown doubling and cap, one trial
  lease, lease expiry, and "4xx never trips" (context overflow, 400, 401), with 429 and
  5xx tripping.
- **Seam.** An open model is excluded with the code `circuit_open`. Placement reasons win.
  `onprem_only` with every on-prem model open never yields cloud: the property test reuses
  the P1 Hypothesis harness style.
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
