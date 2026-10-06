# arXiv article — critical points

**Purpose.** This is the author's source file for an arXiv article about RouteForge. It collects
the claims, formal model, invariants, evidence and limits from every research slice, and is
updated as each remaining feature is specced and built. It is **not** the article.

**Rules for this file.**
- Every number carries its source (`file §section`, commit/PR, date, simulated vs live).
- A result that has not been measured is written **TO MEASURE** — never estimated.
- Related work marked **TO VERIFY** has not been checked against the actual paper; several
  2026 arXiv ids in the roadmap were leads that may not exist.
- Sources: PC = `docs/research/policy-compiler-noninterference.md`, SP =
  `docs/research/routing-signal-provenance.md`, FB = `docs/research/feasible-route-bench.md`,
  RM = `docs/roadmap/ip-and-research-opportunities.md`, HO = the Priorities 1–3 handoff
  (`docs/new/RouteForge-Priorities-1-3-LLM-Handoff.md`, main checkout only, untracked).

---

## 0. Before anything is posted — IP

- **File before you publish** (PC §0, SP §0, FB §0, RM §4). An arXiv posting is a public
  disclosure: it starts clocks and, outside the US, destroys novelty. The filing decision for
  each candidate precedes the publication decision and belongs to counsel.
- Overlaps with this article: P1a ↔ IP-1 (meet/ratchet), IP-4 (decisions bound to policy
  hashes), IP-9 (derived reject on empty set) (PC §0); P2a ↔ R1, IP-4/R7, IP-8/R8 (SP §0);
  FeasibleRouteBench = "the evaluation half of R1" (FB §0). IP-11 is a naming trap
  (`app/routing/signals.py` now holds a different mechanism) (SP §0).
- **Already public:** Tier-3 mechanisms IP-9..IP-12 are in a public repo (PC §0, RM §4).
- **Foreclosed** (RM §1) — do not frame the novelty around: proxy output PII redaction
  (HiddenLayer US 12,105,844 B1); cheapest-model-above-quality-bar (Cast AI US 12,236,193);
  semantic caching (Inventus US 12,259,913 B1); router+cache+compression (Infobip
  US 2025/0165752 A1); field-level LLM data gating (SAP US 12,541,616 B2); agent action
  verification (Citibank US 12,450,494 B1). Adjacent: Netskope US 12,068,944 B2 (post-hoc,
  not LLM-specific). **What survives: eligibility as a derived, per-request,
  pre-optimization quantity.**
- The patent scan is a floor, not a ceiling — no assignee-scoped search of Microsoft, Google,
  NVIDIA, Salesforce, Adobe, ServiceNow (RM header, §5).

---

## 1. The article in one paragraph

Production LLM routers optimise cost/quality/latency over a **fixed** candidate pool. In a
regulated, multi-tenant, often air-gapped deployment the pool is a **per-request variable**:
residency, retention, region, raw-sensitive acceptance, tenant allowlists, pins and budgets
decide which endpoints may see a request *before* anything is scored. RouteForge makes that
eligible set `E(request)` a first-class, derived, fail-closed quantity; separates *placement*
(where compute may run) from *protection* (what the model, client and telemetry see) with a
noninterference property; evaluates routers against a **feasible oracle** that respects
`E`; records content-free routing outcomes; and replays real traffic through the real
eligibility and routing code to predict the impact of a policy change — with minimal
conflicting cores, constraint shadow prices, side-by-side administrator scenarios,
uncertainty and capacity. Every decision is explainable from its record.

**Candidate titles** (RM §3): R1 "Eligibility-Constrained LLM Routing: When the Candidate Pool
Is a Per-Request Variable" (flagship; MLSys / NeurIPS D&B). HO §33's paper angle for the
simulator half: "static policy tests vs traffic-weighted simulation — how often static tests
underestimate cost, capacity, latency and rejection effects".

**Positioning** (RM §3): do not compete on routing-algorithm novelty; win on "a real, deployed,
multi-tenant, policy-gated system with production traces and a hard constraint the
literature doesn't model".

### Candidate contributions (check each against results before claiming)

1. A formal model of policy-derived eligibility and a placement/protection lattice with
   noninterference (NI-1, NI-2), plus a property harness that found real fail-open bugs.
2. A routing-score record sufficient to recompute every ranking offline (provenance per term).
3. FeasibleRouteBench: an offline feasible oracle and regret metrics that never credit an
   ineligible endpoint; failure injection; a closed-form "when does a dead endpoint lose" rule.
4. A traffic-weighted counterfactual policy simulator over content-free records, with
   minimal conflicting cores (exact min hitting set), empirical shadow prices, scenario
   comparison, uncertainty ranges and a capacity model.
5. Negative results: the router is **not** monotone in the eligible set (§3.9); an inert
   scoring term for the product's whole history (§3.2); replay is only as good as its
   recorded inputs (§3.7).
6. (Optional, separate papers) R2 cost of redaction, R4 streaming verification, R3
   per-conversation privacy ratchet — §5.

---

## 2. System model (for the paper's §2–3)

- Pipeline: gate (policy, PII) → immutable execution context → compression → router →
  gateway → backends → output verification. **Nothing external sees the raw request before
  the gate**; `onprem_only` filters cloud providers out **before** scoring (CLAUDE.md rules
  1, 3, 8).
- Effective policy `(P, R)` (PC §3). Placement `residency ∈ {any < private_cloud_ok <
  onprem_only}`. Protection is a product lattice `R = ∏ᵢ Aᵢ` over `model_input`,
  `client_output`, `telemetry_io {full<redacted<off}`, `detection {off<regex<presidio}`,
  `content_integrity`, `stream_mode`. `L = Placement × R`, componentwise.
- Per-axis resolution: ceiling → app → preset(`policy_mode`) → default. Totality: an illegal
  value resolves to `deny(a) = ⊤ᵢ`. Ceilings form a closure operator.
- `apply_signals((P,R),E,rule) = (P ⊔ (escalate ? gate(rule) : ⊥), R)` — raise-only on
  placement, identity on protection.
- **NI-1:** `π_P(compile(p[a↦v],d)) = π_P(compile(p[a↦v'],d))` for any protection axis `a`.
  **NI-2:** symmetric, residency on `π_R`. **NI-1′:** a placement change via `apply_signals`
  is admissible only if the rule is named in both `cross_axis_rules_fired` and
  `placement.sources`.
- Eligibility `elig(P, registry)` is antitone in `P`; `elig = ∅` ⇒ reject (fail closed).
- Score over `E` (SP §2): `w_q·quality − w_c·(est_cost/max_est_cost) −
  w_l·min(latency/scale, cap) + w_a·availability`, tie-break `est_cost`. Shipped weights
  1.0/0.6/0.3/0.2. Latency is EWMA with `α(n) = max(1/(n+1), 0.1)`; availability decays
  `v' = 1 − (1 − v)·0.5^(Δt/half_life)`. Each term carries `src ∈ {measured, tier_default,
  neutral, disabled}`.
- Tier target = length-based complexity `min(1, tokens_after/2000)` (`app/pipeline.py`), not
  semantic; text/chat only. **Say this plainly in the paper** — FB §6.7: "the router
  comparison is mostly the tier rule".

---

## 3. Built slices — claims, evidence, limits

### 3.1 P1a policy compiler + P1b feasible set (PR #52, #56)

- **Invariants** (PC §4, `tests/policy_compiler/test_invariants.py`): monotonicity
  (`test_tightening_never_enlarges_what_is_permitted`), NI-1
  (`test_protection_never_changes_placement`), NI-2
  (`test_placement_never_changes_protection`), ceilings (`test_ceilings_win`,
  `test_malformed_ceiling_closes_the_permission`), empty-set reject
  (`test_empty_eligible_set_rejects`), preset fidelity, `onprem_only` never cloud
  (`test_onprem_only_never_yields_a_cloud_candidate`), totality.
- **Evidence:** eight invariants, ten property functions; **15 of 18 mutants caught** (PC §4.2).
  Hypothesis profiles: 200 examples (default, derandomized), 2000 (deep).
- **Findings (strong for the paper):**
  - `residency=''` compiled to `any` — fail-open via `_get(...) or "any"`; found by
    Hypothesis on its first run (`4fa0fb7`, PC §5.1).
  - Compiler said `onprem_only` but `build_context` gave
    `allowed_providers={'azure','bedrock','onprem'}` — two authorities disagreed and fell
    open; fixed by one authority `resolve_residency` (`7a18303`, PC §5.2).
  - "A mutation score computed against strategies that cannot reach the mutated line
    overstates adequacy" (closed in `7d7624b`, PC §8).
- **Limits (PC §8):** harness cannot rediscover CE-1..3 from pre-fix code (claim dropped);
  quantifies over declared signals, not prompts; no quantitative claim — "Do not let a paper
  draft imply otherwise."

### 3.2 P2a measured signals + per-term provenance (PR #57) and signal hardening (PR #59)

- **Invariants:** ranking never changes membership; empty signal store reproduces legacy
  scores exactly (rel/abs 1e-12, 200 examples); scoring record sufficient for offline replay
  (20 parametrizations); record contains no free text (SP §2–§5).
- **Findings:**
  - The `capacity` term was **algebraically inert for the product's whole history** — weight
    0.2 or 20 gave identical outcomes (SP §1.1).
  - The spec claimed "a dead endpoint ranks last"; a dead endpoint (availability 4.0e-08,
    n=10) scored **+0.3800** vs a healthy pricier peer at **+0.0400** and ranked first
    (SP §2.5). → motivated B45/B48 and the breaker.
  - An ordering-level oracle survived a set-max mutation that the identity suite caught
    (SP §3, §5; B44) — weak oracles are a finding.
  - Replay seeding bug: rows stamped `time.monotonic()` made 23-hour-old traffic seed as
    fresh (`f8c92d2`, `54f1954`). After the fix: 400 rows over ~3000 s reproduce live state
    with max ts error `4.8e-7` s, availability error ~`1e-11` (SP §3A).
- **Limits (SP §6):** no claim that routing improves; per-replica signals; defaults untuned.

### 3.3 FeasibleRouteBench (P2b-1, PR #60/#62/#63), B48/B45, circuit breaker (PR #67)

- **Definitions** (FB §3): `q*(c) = max_{e∈E(c)} q(c,e)`; passing set
  `P(c) = {e∈E(c): q(c,e) ≥ f(c)}`; `c*(c) = min_{e∈P(c)} cost`, `l*` likewise. Metrics:
  violation rate, quality regret / pass rate, cost regret (passing cases only) with
  floor-miss rate over `P≠∅`, latency regret, empty-set handling, worst group by
  workload/placement/protection. Headline printed only if `|Δpass| ≤ 0.02`.
- **Invariants:** an injected ineligible choice exits 2
  (`test_injected_ineligible_choice_exits_2`, `test_cloud_choice_on_onprem_case_exits_2`);
  same seed ⇒ byte-identical metrics; `onprem_only` cases never sweep cloud; empty-set cases
  rejected with zero calls; no serving module imports `app/eval`.
- **Simulated results** (FB §5, 22 cases, seed 0, `0a92a73`): `cheapest` pass 1.000;
  `best_published_quality` pass 1.000, cost regret 0.000891, latency regret 109.6 ms;
  `router_*` pass 0.909, cost regret 0.000032, latency regret 30.7 ms. Seeds 0–9: headline
  comparable at 4/10 seeds (35.7–36.7% saving) — **"no single-seed headline should be
  quoted"** (FB §5.6).
- **Closed-form dead-endpoint rule** (FB §9.5): a dead endpoint loses only if
  `w·(1−availability) > 0.6·(1−p)`; at w=0.2 its price must exceed ~2/3 of the peer's.
  Deficit `D* = 0.1r/(1−0.9r)`: 0.992 (1 s), 0.811 (30 s), 0.278 (300 s).
  Simulated outage (30 cases, seeds 0–9): weight 0.2 ≈ weight 0 → 10.5 wasted attempts/run;
  w=2 → 5.5 wasted, +39% cost.
- **Live Spark** (FB §10–§11): `router_online` pass 0.636 → **0.955** after the B51
  peer-prior fix (2026-09-22 → 2026-09-23); live B45 outage agrees with the simulated
  answer. One seed each — "signals, not quotable results".
- **Breaker** (FB §12.4, simulated): wasted attempts 4.4 vs 10.5 (1 s/case, +51% cost),
  7.0 vs 10.5 (30 s/case, +39% cost), no effect at 300 s. Never lowers the target tier
  (`test_high_risk_open_frontier_keeps_the_frontier_target`), never widens placement
  (`test_bypass_never_widens_placement`).
- **Counterexamples worth a paragraph:** `router_warm` outcome leakage (removed `8344b21`);
  breaker lowering the target tier below the risk floor (`75e5ae4`); mid-cooldown success
  closing a circuit (`2d0d029`); `-0.0` regret (`820bec2`).
- **Limits:** simulated quality is an input, not a measurement; deterministic graders;
  synthetic single-turn cases; one designed outage scenario; no live breaker run.

### 3.4 P2b-2 content-free outcome records (PR #68–#70)

- One record per routed request: context, versions (app, registry, routing), first decision,
  action (`chosen`, `attempts`), outcome (status, cost, latency, verification, feedback).
  No content, no propensity, no judge, no counterfactual.
- Invariants: `test_schema_is_content_free`, `test_outcome_row_carries_no_request_content`,
  `test_cross_app_feedback_is_refused_and_writes_no_row`,
  `test_mixed_registry_versions_produce_separate_groups`.
- **Limit for the paper:** the deterministic logger gives no overlap, so off-policy
  evaluation is impossible from these records (FB §13.3). This is why the simulator diffs
  simulated vs simulated.

### 3.5 P2b-3 human-approved quality calibration (PR #71)

- Beta-Binomial per `(model, workload)`; prior from published/tier default only; gate
  `min_labels 20`, 90% CI width ≤ 0.25, shift ≥ 0.02; content-hashed set; human applies.
- Invariant: calibrated cloud quality never lifts an `onprem_only` request
  (`test_calibrated_cloud_quality_never_lifts_onprem_only`).
- No results (not deployed).

### 3.6 P3a policy counterfactual simulator (PR #72/#74/#75)

- Replays outcome rows through the real `build_context`, `feasible_set`, `Router.select`
  under `current` (snapshot) and `proposed` (snapshot + proposal). First decision only,
  frozen signals. **Diff is simulated vs simulated**; agreement with the log is a fidelity
  check, not the headline (FB §15.2).
- Invariants 9.1–9.8: empty proposal ⇒ zero change; stricter never widens (Hypothesis);
  residency absolute in simulation; sole-model removal rejects exactly those rows; exact
  repricing; sensitivity rules replay recorded entity types; live round-trip baseline
  perfect; determinism.

### 3.7 B55 — the replay-fidelity finding (PR #73)

- Live Spark 2026-09-25, empty proposal over 194 rows: **baseline agreement 0.0206**
  (190/194 mismatched). Cause: the client's model pin was not recorded, so replay routed
  unpinned. After recording pins: 5 rows, agreement **1.0**. A plausible-looking report hid
  the gap (FB §15.4).
- **Lesson to state:** every input the router reads must be on the outcome record, or replay
  is silently wrong. P3c-2 turns this into `--since` + `--min-agreement`.

### 3.8 P3b minimal conflicting core (PR #76/#77)

- `V(c)` = requirement ids candidate `c` violates; a core `K` hits every `V(c)`; the minimal
  core is a minimum hitting set (hence irreducible). Exact search over ≤ 11 ids (2047
  subsets) — exact at this size (FB §16.2). Endpoint *state* (disabled, circuit open, …) is
  never in the core; remediation hints never relax a security requirement.
- Invariants: `test_covering_minimum_irreducible` (Hypothesis vs brute force),
  `test_order_independent`, `test_state_only_core_is_empty`,
  `test_security_hints_never_relax`, `test_conflicting_core_is_content_free`.
- Live Spark (`dd3790c`): `allowed_tiers:[frontier]` → core `allowed_tiers`;
  `allowed_regions:[eu]` → `allowed_regions` (security).

### 3.9 P3c-1 shadow prices (PR #79/#80) — includes the main negative result

- Empirical shadow price: relax one requirement for one app's rows, replay, diff (no LP
  dual). Security constraints reported as "cost of protection — not a recommendation".
- **Counterexample (design `9205c18`, FB §17.3):** "a routed row is never rejected under
  relaxation" is **false**. Eligibility is monotone; **the router is not monotone in the
  eligible set**. Mechanism: the rule-8 residency clamp + floor-respecting budget fallback.
  Hospital persona (Case B): `onprem_only`, $0.002 cap, `US_SSN` — relaxing residency makes
  the request `over_budget`. Only the eligible-superset property is a hard invariant
  (`test_eligible_superset_for_random_policies`,
  `test_case_b_hospital_residency_relaxation_newly_rejects_over_budget`).
- Live Spark (2026-09-26, `4bfb0b3`, 521 rows, one app): the first 221 pre-B55 rows produced
  an **artefactual** shadow price (221 route changes, −0.66 USD/day). On the 300 B55-era
  rows: the only binding constraint is the app's own pin — $0, Δp50 −26.3 s; no security
  constraint binds.
- Limits: as good as baseline agreement (not re-checked — fixed in P3c-2); Δp50 is from
  frozen measured medians, not a controlled comparison; single tenant, all models $0.

---

## 4. Slices being built now — what each must deliver for the paper

### 4.1 P3c-2 alternative scenarios (spec `docs/superpowers/specs/2026-10-05-p3c2-scenarios-design.md`)

- **Claim:** administrator-written joint scenarios are compared over the same recorded
  traffic and the same baseline; RouteForge never ranks or recommends (HO §28).
- **New levers** usable in experiments: `workload.token_scale` (post-redaction token change —
  the R2 lever), `workload.cached_share` (assumption, labelled), per-endpoint `latency_ms`
  (vLLM↔Dynamo or hardware change, operator-asserted), and deterministic context-window
  overflow re-selection in both arms.
- **Invariants to cite, with the test that pins each** (full table: FB §18.3):
  S1 `test_s1_empty_scenario_is_a_noop`; S2 `test_s2_single_scenario_equals_single_proposal`;
  S3 `test_s3_order_independence`; S4 `test_s4_neutral_workload_changes_nothing` and
  `test_s4_neutral_latency_changes_no_route_or_cost`; S5 `test_s5_cost_monotone_in_token_scale`;
  S6 `test_s6_cached_share_never_raises_cost`; S7 `test_s7_overflowing_pick_is_never_served`;
  S8 `test_s8_no_ranking_and_file_order`; S9
  `test_s9_scenario_proposals_never_widen_eligibility`; S10
  `test_s10_since_excludes_and_counts_old_rows`; S11
  `test_s11_min_agreement_exits_4_and_writes_nothing`.
- **Findings from the build (FB §18.2):** the saturated-complexity rule needs a lower bound of
  `scale`, or scaling tokens up can lower complexity; an asserted latency must be scored as a
  measurement, or re-asserting the current value can move a route through the peer prior.
- **Expected finding, revised:** the design expected context-window replay to *raise* baseline
  agreement. By construction it should leave it unchanged: the baseline compares first
  decisions, and failure rows record `prompt_tokens: 0`, so recorded overflows cannot be
  reproduced (FB §18.2). Measured once so far (DGX lab, 2026-10-06, FB §18.4.1): agreement 1.0 over
  18 `ok` rows, 0 overflow hops — an export with no overflow at all, so the before/after
  comparison on real overflow failovers is still **TO MEASURE**.
- **Final-review finding: a served pick's own predicted overflow was invisible to agreement.**
  The revised expectation above is about a reproduced FAILOVER, not about a record proving the
  first pick alone served the row while replay excludes that same pick as a predicted
  overflow — that case still "agreed" by the first-decision measure, silently, even though the
  overflow prediction (§4.1's new levers) contradicts what the record proves happened.
  `_baseline` now treats it as a disagreement and reports the count separately as
  `overflow_contradicted`: a direct fidelity check on each endpoint's declared
  `context_window`, not a routing disagreement (FB §18.2 final-review addendum). First
  measurement (DGX lab, 2026-10-06): 0 of 18, with a 262144-token declared window; an export
  from an endpoint with a tight declared window is still **TO MEASURE**.
- **First live run (DGX lab, 2026-10-06, FB §18.4.1) — usable facts.** One app, one priced
  endpoint (`qwen3-coder-next`, $10/$10 per 1M), 25 rows, six scenarios; artifacts in
  `docs/research/results/p3c2-dgx-lab-2026-10-06/`. `no-change` is zero in every column on
  live data (S1). `token_scale: 0.6` → cost −39.0% with no route change; repricing to
  0.5/1.5 → −94.7%; shrinking the declared window to 2000 → 2 predicted `context_window`
  rejections; asserted latency 5000 ms → Δp50 +4848.6 ms. Live 422s carried the minimal
  conflicting core `max_cost_usd_per_request` (cap 0.05, estimate 0.05344). This is a
  plumbing check, not a workload study — do not quote it as an evaluation.
- **Negative result from that run (paper-worthy, B60): rejected requests are not replayable
  today.** Rejected outcome rows record `prompt_tokens: 0`, so replay routes rows the record
  says were rejected: the `raise-request-budget` scenario reported 0 newly served where live
  it would have served 7, and baseline agreement stayed 1.0 because it only compares `ok`
  rows. Lesson, same family as B55 (§3.7): *every input the admission and routing decision
  read must be on the record for rejected requests too, and agreement must compare
  outcomes (served vs rejected), not only the chosen model.* Fix is scoped into the
  replay-fidelity slice (§4.4).
- **Experiment E-P3c2 (TO MEASURE):** the HO §25/§28 healthcare triad as three scenarios
  (all-on-prem / raw-PII-only on-prem / all-on-prem + second endpoint) on a persona-generated
  traffic mix with realistic shares (60/25/10/5). Report the comparison table. Also: static
  4-request test vs traffic-weighted replay — the HO §33 "static tests underestimate" claim.

### 4.2 P3c-3 uncertainty ranges — *spec pending*
### 4.3 P3c-4 capacity model — *spec pending* (HO §25 "230% of capacity" example)
### 4.4 Replay fidelity: failover/escalation, breaker state, spend/RPM admission — *spec pending*
### 4.5 Administrator review workflow — *spec pending* (closes HO §37 P3 "admin approval")
### 4.6 P2 evaluation metrics: confidence calibration, churn robustness, worst-group, router-version promotion gate — *spec pending* (FB §6.5 lists these as not yet demonstrated)

---

## 5. Separate paper measurements (scope chosen 2026-10-05)

- **R2 "The Price of Redaction"** — over-redaction → token count → tier → price → cache key.
  P3c-2's `token_scale` gives the replay lever; a dedicated measurement slice is pending.
- **R4 "What Streaming Verification Can and Cannot Guarantee"** — impossibility of a
  whole-response predicate without unbounded hold-back; dual-guard release oracle tested at
  every split offset; TTFT per `stream_mode`. *Spec pending.*
- **R3 "Monotone Privacy Contexts for Multi-Hop Agents"** — re-targeted 2026-09-17 to a
  per-conversation ratchet (agent loops are client-driven); needs a session identifier, a
  serving-path change. *Spec pending.*

---

## 6. Experiments checklist (fill after testing)

| id | what | where | status |
|---|---|---|---|
| E1 | Noninterference harness: mutants caught, examples, time | PC §4.2, §7 | done (15/18) |
| E2 | FeasibleRouteBench multi-seed table, simulated | FB §5.6 | done |
| E3 | Dead-endpoint rule: simulated vs closed form vs live | FB §9.5, §11.2 | done |
| E4 | Breaker wasted attempts | FB §12.4 | done (simulated only); live **TO MEASURE** |
| E5 | Replay agreement before/after B55 | FB §15.4 | done |
| E6 | Shadow-price non-monotonicity counterexample, live | FB §17.3 | **TO MEASURE** (not exercised live) |
| E7 | Context-window replay effect on agreement | P3c-2, FB §18.4.1 | first run done (no overflow in the export: agreement 1.0, 0 hops); with real overflow failovers **TO MEASURE** |
| E7b | Rejected rows replay as routed (B60) | FB §18.4.1 | observed live: 7 of 7 budget rejections; fix pending |
| E8 | Healthcare scenario triad, traffic-weighted vs static | P3c-2 | **TO MEASURE** |
| E9+ | uncertainty, capacity, fidelity, P2 metrics, R2/R3/R4 | §4–§5 | specs pending |

**Data caveat for every live number:** the Spark serves one app (`kit-gtm`, Qwen Code), all
models priced $0 (FB §17.4). The DGX lab (the test target from 2026-10-06) has one priced
on-prem endpoint (`qwen3-coder-next`) and a synthetic test app. Neither has two endpoints
with different placement, so no live result yet exercises a residency or region constraint.
Multi-tenant, multi-endpoint results need persona traffic plus a second endpoint — plan this
before writing the evaluation section.

---

## 7. Threats to validity (collect here)

- Tier selection is length-based; the "router" contribution is mostly the eligibility seam,
  not learned routing (FB §6.7).
- Simulated quality is an input (FB §6.1); deterministic graders; synthetic single-turn cases.
- Single tenant, single seed live runs; $0 prices on the Spark.
- Replay is first-decision only (until §4.4), with frozen signals; cached tokens not recorded.
- Endpoint declarations (region, retention) are operator-declared; RouteForge enforces the
  declaration and cannot verify a provider's contractual claim (CLAUDE.md).
- Patent/prior-art scan incomplete; arXiv leads unverified.

---

## 8. Related work — ALL TO VERIFY

- Routing: RouteLLM, RouterBench, RouterEval, UniRoute, WISERouter; Agentic Routing
  `2607.11399`, ACRouter `2606.22902`, TRACE-Router `2607.22465`, Agora `2607.09600`,
  OCELOT `2606.12341` (per-hop checking misses accumulating risk — supports R3).
- Privacy/redaction: SlotGuard `2607.17147`, MathEd-PII `2602.16571`.
- Policy/governance: Traccia `2607.14309`, Policy-as-Prompt `2509.23994`, Strategic Auditee
  Gaming `2605.06340`; XACML, OPA/Rego, AWS Zelkova; configuration-error literature.
- Information flow: Sabelfeld & Myers 2003, Jif/FlowCaml, declassification.
- Testing: QuickCheck/Hypothesis, mutation testing.
- Load balancing/health: EWMA, P2C (Envoy, Finagle), outlier detection, circuit breakers.
- Evaluation: off-policy / IPS, constrained and safe bandits, cold start.
