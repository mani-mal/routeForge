# FeasibleRouteBench failure injection, failover-aware scoring, online warm arm (B48 → B45)

**Status:** approved design, 2026-09-22.
**Closes:** backlog B48 (all four requirements); answers B45 **for the simulation** (a live run stays pending).
**Builds on:** `docs/superpowers/specs/2026-09-21-feasible-route-bench-design.md` (P2b-1, PR #60) and
`docs/research/feasible-route-bench.md`.

## 1. Problem

B45 asks whether `weights.availability: 0.2` is large enough for a dead endpoint to lose. The first
FeasibleRouteBench run was null by construction: simulated endpoints never fail, so availability is
exactly 1.0 for every candidate. B48 lists what failure injection must get right before any number
is quoted: (a) no outcome leakage in the warm arm, (b) error rows must not make a strategy look
cheaper, (c) injected failures and warm-signal counting must match production's
`counts_as_endpoint_unhealth`, (d) the single-shot vs failover modelling gap.

## 2. Decisions (user, 2026-09-22)

1. **Model failover in scoring** (resolves (b) and (d) together).
2. **Two failure knobs:** contiguous **outages** and independent **flakiness** (`error_rate`).

## 3. Failure model (simulated backend only)

`SimProfile` gains optional fields:

| Field | Meaning |
|---|---|
| `outages: list[{from: int, to: int}]` | inclusive **case-index** ranges (0-based, suite file order) where every call fails |
| `error_rate: float = 0.0` | independent failure probability, drawn from `random.Random(f"{seed}:{case.id}:{model}:err")` |
| `failure_latency_ms: float \| None = None` | latency charged for a failed attempt; `None` ⇒ the profile's `latency_ms.mean` |

- Outage failure raises an exception with **no** `status_code` (connection-refused shape); flaky
  failure raises one with `status_code = 503`. Both must satisfy
  `app/gateway/gateway.py::counts_as_endpoint_unhealth` (asserted by test).
- Outage is checked before the flake draw; an outage always fails.
- Validation: `from <= to`, both `>= 0`; `0 <= error_rate <= 1`; `failure_latency_ms > 0` when set.
- The backend needs the case index: `set_case(case, index)` (the runner passes the suite position).

## 4. Matrix rows stay single-shot facts

Each `(case, endpoint)` row is still one sweep call. `MatrixRow` gains
`counts_unhealth: bool | None` (None on success) = the real `counts_as_endpoint_unhealth(exc)` on the
exception the sweep call raised, and `failure_latency_ms: float | None` (the simulated failed-attempt
latency; None on success or when unknown, e.g. a live failure — then the attempt's latency is
counted as 0 and the report says so). Error rows keep `cost_usd = 0.0`, `quality = 0`, `passed = False`.

## 5. Failover-aware scoring

A strategy now yields an **attempt order** rather than one choice.

- **Router strategies:** the real mechanism — `Router.select(ctx, ..., exclude=frozenset(failed))`
  repeated, at most `1 + settings.routing.max_failover_attempts` attempts (routing.yaml: 2 ⇒ 3
  attempts), stopping at the first success; `NoEligibleCandidate` ends the chain. This mirrors
  `Gateway.run`'s failover loop.
- **Baselines:** their own full ranking order (cheapest → next cheapest …; best quality → next …;
  `fixed` → then `cheapest`'s order), capped at the same attempt count.
- Each attempt's outcome is looked up in the matrix. The **outcome of the strategy on the case**:
  - served endpoint = first successful attempt, or none (**exhausted**, production's 502);
  - quality = the served row's quality, or 0 if exhausted;
  - cost = Σ attempt costs (failed attempts cost 0);
  - latency = Σ failed attempts' `failure_latency_ms` + the served row's latency;
  - `attempts`, `wasted_attempts` (= failed attempts), `exhausted` (bool).
- `Choice` records the attempt list; the oracle is **unchanged** (best eligible endpoint's
  successful single call). Regret is computed from the strategy outcome above. A strategy that
  picks a failing endpoint first now pays the failed attempt's latency, not quality 0 — unless
  every attempt fails.
- New per-strategy metrics: `wasted_attempts` (mean per non-empty case), `exhausted_rate`.
  `mean_chosen_cost` uses the failover-aware cost; a strategy can no longer look cheaper by
  choosing dead endpoints (B48 (b)).
- Violation check covers **every attempt**, not just the first: any attempt outside `E(c)` is a
  violation (exit 2).

## 6. Signal states for the router

| Arm | Store contents before scoring case *k* | Role |
|---|---|---|
| `router_cold` | empty | no-measurement baseline |
| `router_online` | only the attempts **this arm made** on cases `0…k−1` (incl. its failovers), applied in order through `observe_success` / `observe_error`, errors counted only when `counts_unhealth` | **the reported arm** — what production would learn: no leakage, no information production wouldn't have |
| `router_warm_prior` | every matrix row of cases `0…k−1` (all eligible endpoints) via `warm_signals` | full-information upper bound |

- The previous `router_warm` (which replayed the current case's rows) is **removed** — it leaks
  the scored outcome (B48 (a)).
- `warm_signals` counts an error row only when `counts_unhealth` is true (B48 (c)).
- Both non-cold arms read against a frozen clock anchored at the last applied observation
  (existing `_FrozenReadSignalStore` pattern); observations are spaced 1 s apart by case order
  (documented: decay depends on case count, not wall time).
- Weight variants: `router_online:<name>` and `router_warm_prior:<name>` (and `router_cold:<name>`).
  **B45's answer is read from `router_online:<name>`.**

## 7. Shipped outage scenario

`eval/suites/outage.jsonl` (~30 FAQ-style public cases, `residency: any`) and
`eval/endpoints/outage-sim.yaml`: two same-tier endpoints where the **cheaper** one has an outage over
a middle stretch of cases and one endpoint has a small `error_rate`, plus variants
`eval/variants/b45.yaml` (existing). The question it answers: during the outage, does the online
router keep sending first attempts to the dead cheaper endpoint (paying wasted attempts and
latency), and at which availability weight does it stop?

## 8. Invariants and tests

1. Failure injection deterministic per seed; injected exceptions pass `counts_as_endpoint_unhealth`.
2. Failover scoring vs hand-computed attempt chains, incl. an exhausted case and the attempt cap.
3. **No leakage:** `router_online`'s store never holds an observation from case `k` or later when
   scoring case `k` (falsified by an injected leak); the same for `router_warm_prior`.
4. `router_online`'s attempt order equals repeated real `Router.select(..., exclude=...)`.
5. **No-failure invariant:** with no failures, availability is exactly 1.0 in every arm, so all
   availability-weight variants within an arm give identical outcomes; the existing starter run
   still exits 0 with violation rate 0 (its metrics may change only where the arm set changed).
6. Violations are checked on every attempt.
7. `warm_signals` ignores error rows with `counts_unhealth` false.
8. End to end: the outage scenario runs deterministically (byte-identical `metrics.json` per seed)
   and the availability variants of `router_online` differ.

## 9. Documentation

- `docs/research/feasible-route-bench.md`: new section — failure model, failover-aware definitions,
  the three arms and why the leaky arm was removed, and the **B45 result** from the outage
  scenario with its stamp (seed, suite/endpoint hashes, git SHA). Limits: simulated failures;
  outages indexed by case, not wall time; 1 s synthetic spacing drives decay; a live run is pending.
  Re-cite any number from the starter run that the arm change moved.
- `docs/guides/feasible-route-bench.md`: the new profile fields, arm names, metrics.
- `docs/backlog/bugs.md`: B48 resolved; B45 updated with the simulated answer (open until live).

## 10. Out of scope

Live failure injection; wall-clock outage timing; changing production weights (B45's answer is a
recommendation for the owner, not an edit to `routing.yaml`).
