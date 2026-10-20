# Priorities 1–3: what is built and what remains

**As of 2026-10-08**, `main` is through PR #92 (P2 evaluation metrics, part 1). The DGX
Spark runs `4bfb0b3` (through PR #79). The DGX lab (Run:ai) cluster was rebuilt and
redeployed by PR #83, and is the deployment target for P3c-2 onward (api/ui `0.33.0`).

This is the status of the program set out in `docs/archive/design-handoffs/RouteForge-Priorities-1-3-LLM-Handoff.md`
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
| **P2 metrics (1 of 2)** group report and router-version check | pending | `feasible_bench.py --group-report`: every metric by placement, protection, application and workload, each figure with its own denominator; a figure over fewer than `--min-group-size` cases prints a count and no rate; violation counts are never suppressed. `feasible_bench.py --candidate FILE`: an **offline** router-version check — a candidate routing overlay scored beside the current config over the same sweep; any ineligible choice is exit 2 whatever the tolerances; a named metric's whole range beyond its tolerance is exit 3; a range that straddles the tolerance is exit 4 (inconclusive). **It applies nothing and promotes nothing.** Both opt-in; without the flags the bench's output is byte-identical, now pinned by a golden test. | `docs/guides/feasible-route-bench.md` §6.2–§6.3, research §22 |

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
| **P3c-3** uncertainty ranges | #88 | `policy_simulate.py --uncertainty` adds a range to every headline figure of all three modes: a percentile interval from a bootstrap over the replayed rows, resampled within each application (applications below `--min-rows` are pooled for the totals) and paired across arms, scenarios and relaxations. Opt-in; without the flag every output is byte-identical. `--quality-posteriors` adds label uncertainty for calibrated qualities from the P2b-3 recommendation file. Below `--min-rows` a bucket says "insufficient data"; a zero count carries a bound instead of reading as no risk. The range is sampling variability only and every report says so. | `docs/reference/policy-simulator.md` §10, research §20 |
| **P3c-4** capacity | #90 | `policy_simulate.py --capacity FILE` (or a `capacity` key in a proposal or scenario) adds, per endpoint and arm, the replayed demand per time bucket against limits the **operator states**: for a concurrency, request-rate, token-rate and output-token-rate limit, the peak bucket **always with** the average over the whole window and over the endpoint's active buckets, the share of the window and of requests above 100%, and one more comparison-table column (`peak X% / avg Y%, over 100% in Z% of N buckets`). An endpoint whose only latency is a tier default gets no concurrency figure (a state, not a number), and service time a scenario places after the end of the record is reported as a share. A request's service time is its own recorded latency where it stays on its endpoint, and is spread over the buckets it runs through. `workload.volume_scale` asks the same at another traffic volume. Opt-in; with no capacity stated every output is byte-identical. It predicts no rejection and no delay, carries no range, and names no remedy; a short export says "window too short". The first model built was revised after review before any figure was quoted (research §21.5.2), and its presentation again after the final review (research §21.5.5). | `docs/reference/policy-simulator.md` §11, research §21 |
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

### 2.1 Priority 3 (the three P3c slices after shadow prices are built)

The owner-approved order was scenarios → uncertainty → capacity. Scenarios (P3c-2),
uncertainty (P3c-3) and capacity (P3c-4) are built.

1. **Follow-ups to P3c-3** (research §20.7), none blocking:
   - a cluster or day-block bootstrap. Rows are resampled as if independent; the calls of one
     agent loop are not. A cluster bootstrap needs a session or task identifier on the record;
   - calibration posteriors in the snapshot. Today the operator passes the P2b-3
     recommendation file; exporting `alpha`/`beta` would need two `model_quality` columns and
     a snapshot schema bump;
   - measured coverage on real records (`docs/research/arxiv.md` E9a–E9d);
   - variation between applications. The ranges take the applications in the export as
     given; a two-stage bootstrap that also resamples applications would cover a claim about
     other tenants, at the cost of the pairing with per-application shadow prices.
2. **Follow-ups to P3c-4** (research §21.6, backlog B66), none blocking:
   - capacity declared on the onboarded endpoint and exported in the snapshot, so the
     `--capacity` file is generated rather than hand-written. An admin and schema change;
     still a declaration RouteForge records and cannot verify;
   - a time-block bootstrap, the only honest way to put a range on a peak;
   - a service-time model for requests that move. A request that stays on its recorded
     endpoint uses its own recorded latency; one that moves gets the new endpoint's mean;
   - a p95-bucket figure beside the peak, a difference-array accumulation, and a
     backend-time field on the outcome record (B66 items 7–9);
   - the measurements: the healthcare scenario pair with a stated on-prem capacity, and
     stated capacity against what an endpoint actually does (`docs/research/arxiv.md`
     E10a–E10f). Nothing has been run on recorded traffic;
   - **not** planned: turning overload into a predicted-rejection figure. RouteForge does not
     know what an endpoint does above a limit, and its gateway would fail a refused request
     over, which replay does not follow (design spec §9.2).
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
- **Handoff §18 metrics not yet reported** — designed
  (`docs/superpowers/specs/2026-10-07-p2-eval-metrics-design.md` §6, §9), not built; the
  second plan of that spec:
  - **policy-churn and endpoint-churn robustness**: one declared change at a time, the
    suite re-run, violations required to stay 0;
  - **calibration of confidence**. The router emits no confidence. The one that exists is
    the backend's figure for an answer, which the gateway's escalation threshold reads and
    nothing records; that is what the design measures. In simulation it is constant, so
    the simulated result will be a stated "not measurable".

Built from this list on 2026-10-07 (§1): worst-group results (as the group report; "tenant"
is `application`, because the bench has one tenant id) and the router-version check. The
check is the handoff's "promotion gate" in the only form the product boundary allows: it
blocks nothing in process and promotes nothing. An operator, or their CI, reads its exit
code before changing `config/routing.yaml`.

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
2. The simulator slices are built. The next Priority 3 item is the administrator review
   workflow (§2.1 item 3), or a fidelity gap from item 4; each starts with a design
   brainstorm, then a spec, plan and subagent build.
2a. The next Priority 2 item is the second plan of the P2 metrics spec (churn robustness and
   confidence calibration). Write that plan from the spec's §6 and §9; do not start from
   the handoff. Before quoting a group figure or a check verdict, quote the suite, the
   seed and each figure's `n`; a verdict that rests on simulated pass rate changes with
   the seed (research §22.5.3).
3. Before quoting any simulator number from the Spark, check both baseline agreements, and
   exclude rows recorded before B55. Rejected rows recorded before B60 are excluded
   automatically (`missing_routing_tokens`). A range from `--uncertainty` is sampling
   variability only: quote it with the agreements, never instead of them. A utilisation from
   the capacity section is against a limit somebody **stated**: quote the limit and its
   source with it, and never a peak without its average, the share of buckets above the
   limit, the bucket length and the window.
