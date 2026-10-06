# Priorities 1–3: what is built and what remains

**As of 2026-10-06**, `main` is through PR #86 (B60, replayable rejections). The DGX
Spark runs `4bfb0b3` (through PR #79). The DGX lab (Run:ai) cluster was rebuilt and
redeployed by PR #83, and is the deployment target for P3c-2 onward (api/ui `0.27.0`).

This is the status of the program set out in `docs/new/RouteForge-Priorities-1-3-LLM-Handoff.md`
(the "handoff"). Each shipped slice went through the same pipeline: design spec
(`docs/superpowers/specs/`), plan (`docs/superpowers/plans/`), subagent-driven build with
per-task and whole-branch review, merge, deploy to the Spark, and a live run recorded in the
research notes. Section numbers such as "research §17" refer to
`docs/research/feasible-route-bench.md` unless another file is named.

---

## 1. Built

### Phase 0 and Priority 1: the constraint-isolated policy compiler

| Slice | PR | What it gives the operator | Where |
|---|---|---|---|
| **P1a** policy compiler | #52 | `policy_mode` split into independent compiled axes (`model_input`, `client_output`, `capture_io`, `pii_detection`, `content_integrity`, `stream_mode`); one pure `compile_policy`; a per-axis decision certificate on every response and trace; a Hypothesis noninterference harness that produces counterexamples | `app/policy/compile.py`, `docs/research/policy-compiler-noninterference.md` |
| **P1b** endpoint declarations | #56 | Per-endpoint `region` / `prompt_retention` / `accepts_raw_sensitive`; app requirements `allowed_regions` / `require_no_retention`; **one** eligibility seam (`feasible_set`) with exclusion reasons | `app/policy/feasible.py`, `docs/guides/onboarding-models.md` §(e2) |

Handoff §37 Priority 1 criteria met:
- tightening never enlarges the eligible set;
- protection never moves placement;
- missing metadata excludes the endpoint;
- empty sets fail closed with reasons (made actionable by P3b below);
- fallback stays inside the feasible set;
- the harness produces reproducible counterexamples.

### Phase 0 and Priority 2: the feasible-set router and evaluation plane

| Slice | PR | What it gives the operator | Where |
|---|---|---|---|
| **P2a** measured signals | #57 | Measured latency and availability in scoring, cached-token pricing, per-term scoring provenance (`sp.scoring`) | `docs/reference/routing-observability.md` |
| Signal hardening | #59 | Endpoint observation log with replay seeding across replicas; error rows; stream availability (B38–B41) | research §8–§9 |
| **P2b-1** FeasibleRouteBench | #60, #62, #63 | Offline feasible-oracle harness plus failure injection (B48) | `scripts/feasible_bench.py`, `docs/guides/feasible-route-bench.md` |
| B49 / B50 | #64 | OpenAI error envelope; context limits published on `/v1/models` | |
| B51 | #65 | Same-tier peer latency prior, so a new model doesn't pay a quality-blind warm-up | |
| Circuit breaker | #67 | Cross-request, per-endpoint circuit breaker, on by default; never lowers the target tier (closes B45) | `app/gateway/` |
| **P2b-2** routing outcomes | #68, #69, #70 | Content-free `routing_outcomes` and `outcome_feedback` records, export, and report; nothing reads them back into routing | `docs/reference/routing-outcomes.md` |
| **P2b-3** quality calibration | #71 | Offline, human-approved quality calibration: the estimator, a replay diff, `scripts/quality_recommend.py` (B54 fixed alongside) | `docs/reference/quality-calibration.md` |

Handoff §37 Priority 2 criteria met:
- ineligible endpoints are masked before scoring;
- the deterministic violation rate is zero under test;
- empty sets reject;
- outcomes carry policy and registry versions;
- the feasible oracle exists offline;
- **no** learning or exploration runs online.

### Priority 3: the traffic-weighted policy counterfactual simulator

| Slice | PR | What it gives the operator | Where |
|---|---|---|---|
| **P3a** policy simulator | #72, #74, #75 | `GET /admin/config-snapshot`, a content-free, secret-free snapshot. `scripts/policy_simulate.py` replays recorded traffic under the current and a proposed policy, with a baseline agreement check. | `docs/reference/policy-simulator.md`, research §15 |
| B55 | #73 | Outcome rows record the client's pinned model and `max_tokens`; replay agreement went from 0.02 to 1.0 | research §15.4 |
| **P3b** minimal conflicting core | #76, #77 | An empty feasible set's 422, the outcome row, and the simulator name the smallest set of requirements no endpoint meets together. Endpoint state (disabled, circuit open, …) is kept separate. Remediation hints never relax a security requirement. | `docs/reference/conflicting-core.md`, research §16 |
| B56 / B57 | #78 | The exact app scope is enforced at the router; the `serving` logger actually prints in deployment | |
| **P3c-1** shadow prices | #79, #80 | `policy_simulate.py --shadow-prices` relaxes one constraint at a time, per app, and prices it (cost/day, rejections avoided, newly rejected, quality, latency, on-prem share). Security constraints appear only as "cost of protection", never as a recommendation. | `docs/reference/policy-simulator.md` §8, research §17 |
| **P3c-2** alternative scenarios | #84 | `policy_simulate.py --scenarios` replays several administrator-written scenarios against one shared baseline and writes one comparison table, in file order, never ranked. Proposals can assume a token scale, a cached-prompt share and a per-endpoint latency. Replay mirrors the gateway's context-window overflow re-selection. `--since` cuts old rows; `--min-agreement` refuses a run whose baseline disagrees with the record. | `docs/reference/policy-simulator.md` §9, research §18 |
| B60 replayable rejections | #86 | Every outcome row records `routing_tokens`, the token count the router was given, so a request rejected for its size replays as rejected and a proposal that raises a budget shows what it rescues. The baseline check reports outcome agreement (routed or rejected against the recorded status) next to model agreement, and `--min-agreement` gates on the lower. Rejected rows from before the fix are excluded and counted. One `ALTER` before deploy. | `docs/reference/policy-simulator.md` §5–§6, `docs/reference/routing-outcomes.md` §1, research §19 |

Handoff §37 Priority 3 criteria met:
- simulation never activates anything;
- replay uses derived features only, never raw prompts;
- results reflect real recorded traffic;
- reports show route changes and multi-objective impact;
- empty sets carry a minimal core;
- security constraints are never relaxed automatically.

The one open Priority 3 criterion is administrator review of remediation choices. It is only
partly met: nothing is ever applied, but there is no approval workflow (see §2.3).

### Findings worth keeping from the build

- **The "relaxing never rejects" invariant was false** (P3c-1). The real router's rule-8
  floor clamp combined with the budget fallback means that widening the allowed set can newly
  reject a request as `over_budget`. Only the eligible-superset property is a hard invariant.
  The counterexample is in research §17.3.
- **A replay is only as good as its baseline agreement.** Rows recorded before B55 replay
  unpinned. On the Spark they produced a fake shadow price (research §17.4). Cut exports at
  the first B55-era row: 2026-09-25 01:25 UTC on the Spark.
- **Agreement over served rows is not enough.** A request rejected for its size replayed as
  routed, because the row did not record its size, and model agreement stayed 1.0 (B60,
  research §18.4.1). Agreement now also compares outcomes (research §19).
- **Real Spark traffic today is one app (`kit-gtm`, Qwen Code).** Its only binding constraint
  is its own model pin: $0 difference, and about 26 s lower replayed p50 without it.

---

## 2. Still to build

### 2.1 Priority 3 (in progress: P3c-3 is next)

The owner-approved order is scenarios → uncertainty → capacity. Each needs its own
brainstorm, spec, plan and build.

1. **P3c-3 uncertainty ranges** (handoff §36, open question 11). Today every figure is a
   point estimate from one replay. Candidates:
   - bootstrap over recorded rows for cost, latency and rejections;
   - propagation of the P2b-3 quality confidence intervals;
   - a clear "insufficient data" state when an app has too few rows.
2. **P3c-4 capacity model** (handoff §26, §31). Nothing today says "on-prem demand reaches
   230% of capacity". This needs a declared per-endpoint capacity (requests/s or tokens/s),
   either operator-declared or measured from the observation log. The simulator would then
   aggregate simulated load per endpoint per time bucket, and turn overload into a
   predicted-rejection figure.
3. **Administrator review workflow** (handoff §36, Phase 3, last bullet). Simulator output
   is a report file today. There is no console surface, sign-off record, or link from an
   approved simulation to the admin write that applies it.
4. **Replay fidelity gaps** (reference `policy-simulator.md` §6), each a possible later
   slice:
   - no failover or escalation replay (first decision only);
   - no breaker state;
   - no re-running of PII detection;
   - no daily-spend or RPM admission;
   - a recorded `context_overflow` row is reproduced only on an endpoint with a declared
     context window (research §19.5). Rejected rows now carry their size (B60);
   - a rescued request is costed at its admission estimate, not a measured cost;
   - quality for routes never historically served falls back to the tier default
     (handoff open question 10).

### 2.2 Priority 2 (deliberately paused)

- **An LLM judge for quality labels.** Deferred by owner decision until a suitable open
  "System-One" judge is available. Until then, quality comes only from voluntary feedback
  and human-approved calibration.
- **Online learning and exploration.** Not built. Its prerequisites are listed in research
  §14.4:
  - propensities logged at decision time;
  - the full feasible set on the record;
  - a denser quality label;
  - a workload taxonomy that clients actually send;
  - FeasibleRouteBench wired in as a pre-apply gate;
  - the served model credited on cache hits.
- **Router-version promotion gates** (handoff §19, §36 Phase 2). The bench can compare
  strategies offline, but nothing compares alternative router versions or blocks a promotion
  on it.
- **Handoff §18 metrics not yet reported:**
  - calibration of routing confidence;
  - policy-churn and endpoint-churn robustness as standing metrics;
  - worst-group results by placement, protection, tenant and workload.

### 2.3 Open owner decisions

- **Tenant-facing 422 disclosure.** Should the conflicting core hide
  `endpoint_app_allowlist` information about other apps' endpoints? It is unanswered, so it
  stays as built. The existing 422 already lists rejected candidates.
- **Spark `kit-gtm` Langfuse key** is stored unencrypted and needs rotation by the owner.

### 2.4 Operational and packaging follow-ups

- **Helm charts don't pass `SP_LOG_LEVEL`.** Serving logs default to INFO there.
- **Open backlog defects** are in `docs/backlog/bugs.md`. None blocks Priority 3. Most
  relevant:
  - **B37:** `/health` reports a dead endpoint as available for up to 22 h.
  - **B42:** `SignalStore` reports `samples: 0` when disabled.
  - **B43:** a flat `cached_prompt_tokens` in `usage`.
  - **B44** and **B46:** weak test oracles.
  - **B47:** no usage row when a stream fails after the client disconnected.
  - **Older UI and admin items:** B26–B29, B31–B33.
  - **Feature requests:** B15, B16, B17, B25.

---

## 3. How to pick up the next slice

1. Read the handoff §28–§31, then `docs/reference/policy-simulator.md` §1–§8 and research
   §15–§17.
2. Start P3c-3 (uncertainty ranges) with a design brainstorm, then a spec, plan and subagent
   build.
3. Before quoting any simulator number from the Spark, check both baseline agreements, and
   exclude rows recorded before B55. Rejected rows recorded before B60 are excluded
   automatically (`missing_routing_tokens`).
