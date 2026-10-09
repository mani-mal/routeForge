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
- **Group report and router-version check** (FB §22, built 2026-10-07): §4.6.

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
  outcomes (served vs rejected), not only the chosen model.* The fix is §4.4.1.
- **Experiment E-P3c2 (TO MEASURE):** the HO §25/§28 healthcare triad as three scenarios
  (all-on-prem / raw-PII-only on-prem / all-on-prem + second endpoint) on a persona-generated
  traffic mix with realistic shares (60/25/10/5). Report the comparison table. Also: static
  4-request test vs traffic-weighted replay — the HO §33 "static tests underestimate" claim.

### 4.2 P3c-3 uncertainty ranges (spec `docs/superpowers/specs/2026-10-06-p3c3-uncertainty-design.md`)

- **Claim:** a counterfactual policy report over recorded traffic can carry a range on every
  figure at the cost of a resample, not a re-simulation — and the range must be published
  with an explicit statement of what it excludes, because the dominant error in such a
  simulator is replay bias, which no resampling of the same rows can see.
- **Method (FB §20.1):** replay each row once under both arms; bootstrap the **rows**,
  stratified by application, with replacement; percentile interval. An application with at
  least `--min-rows` rows (default 30) is its own stratum; the rows of all smaller
  applications form one pooled stratum that feeds the totals only. The draw for a stratum in
  a replicate is a function of (seed, replicate, application, row count) — or (seed,
  replicate, pooled row count) — only, which gives common random numbers across the two arms,
  across every scenario of a comparison and across every shadow-price relaxation without
  holding them in memory together. A delta's range is the range of the paired difference.
- **What the interval covers:** sampling variability of the recorded rows, within strata,
  for the applications in the export, under the replay model. Where a route's quality is a
  P2b-3 calibrated score, the Beta
  posterior is drawn once per unit per replicate (shared by both arms), so the quality range
  also carries label uncertainty (FB §20.3).
- **What it does not cover (state this in the paper, FB §20.2):** replay model error
  (first-decision replay, frozen signals, predicted overflow); the admission estimate for
  rows with no backend token count — the output counts them (1.54× the backend's count on
  the one run that measured it, FB
  §19.4.1); unrecorded inputs (B55, B60); traffic shift; per-request latency variation (replay
  assigns one latency per endpoint). **A narrow range on a biased replay is still biased** —
  the range and the baseline agreement answer different questions.
- **Findings from the design, usable as paper points:**
  - *The bootstrap cannot express a zero count.* "0 newly rejected" has range `[0, 0]` on 24
    rows and on 2 million. An analytic bound `1 − (1 − P)^(1/n)` is reported beside it; by
    Jensen's inequality it holds for the stratified totals as well as per application (FB
    §20.4). 9.2% for 24 rows. It is one-sided and bounds the rate under the replay, not the
    live rate. The mirror case — a share of exactly 1 — gets `(1 − P)^(1/n)`, with the
    caution that a share fixed by policy (an on-prem-only application's on-prem share) is not
    a sampled rate.
  - *Stratifying by a unit with few rows silently shrinks the totals' range* (the review's
    finding; FB §20.7 for the finding and the fix, FB §20.6.1 for the measurement and its
    method). A bootstrap of a stratum of `n_s` rows reproduces `(n_s − 1)/n_s` of its
    variance, so with one stratum per application the totals keep `(n − S)/n` of theirs —
    and a width of exactly zero when every application has one row. Each application's own
    range is unaffected, which is why it went unnoticed. Synthetic measurement, real engine,
    240 rows, 30 datasets per shape, mean 90% width of the total against the normal-theory
    width `2 · 1.645 · √n · s`:

    | apps × rows | theory `√((n − S)/n)` | one stratum per application | small applications pooled |
    |---|---|---|---|
    | 1 × 240 | 0.998 | 0.997 | 0.997 |
    | 8 × 30 | 0.983 | 0.981 | 0.981 |
    | 80 × 3 | 0.816 | 0.818 | 0.992 |
    | 120 × 2 | 0.707 | 0.697 | 0.992 |
    | 240 × 1 | 0.000 | 0.000 | 0.992 |

    The fix pools applications below `--min-rows` into one stratum for the totals, keeps the
    pairing (the pooled draw has its own seed formula), and **discloses rather than
    corrects** the remainder: every block reports the strata and the analytic
    `variance_factor = (n − S)/n`. Paper point: a stratified bootstrap over tenants needs a
    floor on stratum size, and a zero-width interval is the failure mode to test for.
  - *A figure over a subset needs the subset's count.* Latency, quality and on-prem share
    are statistics of the rows an arm routed; the minimum-rows rule has to be applied to
    that count, not to the bucket's.
  - *Label uncertainty cancels in a paired delta.* A request that stays on the same model
    contributes exactly zero to the quality delta in every replicate; label uncertainty
    enters only through the rows that change model.
  - *Quality with no calibration has no interval at all.* The output reports the share of
    rows whose quality has a posterior, rather than inventing a spread for a published score
    or a tier default. This is the honest state of HO open question 10.
  - *A percentile interval need not contain its point.* It is flagged, not widened.
  - *The posterior parameters were recoverable without a serving change*, because the
    calibration file is content-addressed by the same hash the admin API verified at apply
    time.
- **Invariants to cite, with the test that pins each** (full table: FB §20.5):
  U1 `test_u1_output_without_the_flag_is_byte_identical`; U2
  `test_u2_same_plan_same_block_and_the_seed_changes_it`; U3
  `test_u3_every_point_is_inside_its_range_or_is_flagged`; U4
  `test_u4_noop_proposal_has_every_delta_range_exactly_zero`,
  `test_u4_a_relaxation_is_resampled_with_the_same_draws_as_a_proposal`; U5
  `test_u5_ranges_narrow_as_the_rows_are_replicated`; U6
  `test_u6_a_one_row_app_is_present_in_every_replicate`; U7
  `test_u7_a_small_app_gets_no_range_and_says_how_many_rows`; U8
  `test_u8_one_calibrated_unit_gives_the_posteriors_own_interval`; U9
  `test_u9_caveat_is_the_spec_text`; U10 `test_u10_the_id_tells_plans_apart`; U11
  `test_u11_ranges_add_no_ranking_key_and_keep_file_order`; U12
  `test_no_serving_module_imports_app_eval`; U13 `test_engine_figures_equal_bucket_stats`
  and the brute-force `test_f1_every_replicate_is_the_rows_the_seed_formulas_draw`; U14
  `test_f1_many_small_apps_give_the_totals_the_width_of_one_app`; U15
  `test_f3_a_share_of_one_carries_the_lower_bound`; U16
  `test_f4_an_export_with_only_large_apps_gives_the_leaves_it_gave_before`; U17
  `test_f4_the_cli_says_what_each_bootstrap_will_cost`.
- **Synthetic numbers only so far (FB §20.6.1) — do not present as an evaluation:** the
  stratification table above; cost `replicates × (rows × c_row + strata × c_stratum)`, pure
  Python — 1,000 applications × 40 rows × 1000 replicates in 38.4 s with 20 MB of peak-RSS
  growth (44.4 s and 669 MB before the engine was reordered), 10,000 applications × 3 rows ×
  1000 replicates in 10.1 s (166.0 s before pooling), about 99 ms per replicate at 100,000
  rows in three applications, on one laptop core with other processes running; a
  `Beta(41, 9)` posterior's 90% interval reproduced to within 0.01.
- **Experiments for the DGX lab (all TO MEASURE):**
  - **E9a — width against sample size.** Persona-generated traffic at 500, 5,000 and 50,000
    rows through the same six scenarios as E7b; report the width of each delta's range and
    fit against `1/√n`. Include the 24-row export as the "insufficient data" case.
  - **E9b — empirical coverage.** Split a large export into disjoint halves, repeatedly; how
    often does the 90% range from one half contain the other half's point estimate. This is
    the test of the method on real records.
  - **E9c — coverage under dependence.** The same on traffic produced by agent loops (many
    related calls per task). Expected: under-coverage, by an amount worth reporting; it
    motivates a cluster bootstrap over tasks, which needs a session identifier (the same
    missing field as R3, §5).
  - **E9d — label uncertainty.** After one real calibration round, the quality range with and
    without `--quality-posteriors`, and the share of routed rows covered.
- **Limits (FB §20.7):**
  - *Variation between applications is not covered.* The method conditions on the
    applications in the export. With application means drawn from `exp(N(0, 0.7))` and
    redrawn per dataset, the range was 0.09 to 0.29 of the true width whenever some
    applications were large (synthetic, 200 datasets per shape, FB §20.6.1) — under the old
    rule and the new one alike. A claim about "a typical tenant" cannot rest on these
    ranges.
  - *Rows are assumed independent within a stratum*, which agent traffic violates; no block
    or cluster bootstrap (E9c).
  - *Pooling assumes the small applications' rows are exchangeable*; where they differ in
    mean the pooled totals range is up to 14% wider than the fixed-mix truth (synthetic, FB
    §20.6.1).
  - *Stratification still drops `1 − (n − S)/n` of the variance*, disclosed and not
    corrected.
  - *Cost has a per-application term* and is otherwise linear in replicates × rows.
  - Percentile interval, not BCa; `--min-rows 30` is a convention; reproducible per Python
    minor version.
### 4.3 P3c-4 capacity model (spec `docs/superpowers/specs/2026-10-07-p3c4-capacity-design.md`; HO §25 "230% of capacity" example)

- **Claim:** a traffic-weighted counterfactual of a routing policy is incomplete without the
  load it places on each endpoint, and that load can be reported from the same content-free
  records, with no new measurement, **as a comparison of operator-stated capacity with
  replayed demand — and as nothing more than that.** The restraint is part of the claim: a
  router that neither hosts nor measures the serving capacity cannot honestly predict
  overload behaviour, and the report says so instead of deriving a rejection rate.
- **Method (FB §21.1):** every row an arm routes is in service on the endpoint it is served
  from, from its estimated arrival (the record's write time minus its recorded latency) for
  its service time. Requests and tokens are counted in the epoch-aligned bucket of arrival
  (default 60 s); service time is spread over the buckets the request runs through, so
  offered concurrency in a bucket is the time-average number in flight, and one request is
  never more than one. (For recorded requests that is the number in flight at the router
  **as recorded, waiting included** — a recorded latency contains any time in the endpoint's
  queue — not "had none waited"; say it that way.) Service time is the row's own recorded
  latency when the arm serves it on the endpoint that served it alone; otherwise that
  endpoint's mean recorded latency; otherwise replay's latency for the endpoint; a latency
  the scenario asserts overrides these. **Where the only latency for an endpoint is a tier
  default — a constant of the routing score — no concurrency figure is computed at all**:
  it is a reported state, like a record that is too short. Four demands per bucket — requests/min, prompt+completion
  tokens/min, completion tokens/min, offered concurrency — each divided by a limit the
  operator states. Reported per endpoint per arm: the peak bucket **always with** the mean
  over the whole window (the time-average), the share of the window's buckets above 100%,
  and the mean over the endpoint's active buckets, the share of requests above 100%, the
  peak's time, and the share of the arm's service time that falls after the window. A volume scale
  (stated for both arms, and separately for the proposed arm) multiplies demand without
  changing a route or a stated limit. Arithmetic is exact in integers and independent of
  record order.
- **What it models:** where the replayed traffic lands and when; how long each recorded
  request was in service; the policy change's effect on that through the real eligibility
  and routing code; linear scaling of volume.
- **What it does not model (state this in the paper, FB §21.3, §21.6):** queueing, blocking
  or delay; batching efficiency; autoscaling; the serving engine's real concurrency
  behaviour; load that does not pass through the router; bursts shorter than a bucket;
  failover load (replay is first-decision); the service time of a request **moved** to an
  endpoint it never ran on (it gets that endpoint's mean). A recorded latency is end-to-end
  at the router, not backend service time. Capacity is stated, never measured.
- **How any utilisation figure must be quoted in the paper:** as a triple — peak,
  time-average and the share of buckets above the limit — with the limit, who stated it, the
  bucket length and the window: "peak X% / average Y% of a stated `max_concurrency` of N,
  above it in Z% of W one-minute buckets". **Never a bare peak, and never the pair without
  the share.** A peak is the maximum of the bucket averages: for a steady Poisson stream at
  a mean of 2.0 in service it was 1.6–1.7× the mean in 60 s buckets and 3.0–3.2× in 10 s
  buckets (synthetic, FB §21.5.2 F3), and it grows with the window. And the pair cannot say
  how often: one stationary week read "peak 355% / avg 59%" against a limit of 1 and
  "peak 178% / avg 30%" against a limit of 2 — the same shape — while the first limit was
  exceeded in 17.6% of the minutes and the second in 1.2% (synthetic, FB §21.5.5 FR4). If
  the figure is for a scenario that moves traffic or asserts a latency, quote
  `service_after_window_share` with it. The headline is a maximum over endpoints and limits
  too: say which limits were stated.
- **Findings from building it (usable as a methods subsection; all synthetic, FB §21.5.2).**
  The first model was the obvious one — one latency per endpoint, a request's service time
  counted in the bucket it arrives in, a mean over the buckets with traffic, a headline
  peak — and it was implemented exactly and tested. An independent review, before any
  figure was quoted, showed its numbers were unsafe; each finding was re-measured before and
  after the revision on the same synthetic streams:
  - *F2, attribution.* Counting service time at arrival overstated the peak by 1.39× at a
    300 s service time and 3.75× at 900 s (60 s buckets), and was off by +6% and −11% at
    service times of half a bucket and one bucket, where its own warning flag was silent. Twelve two-minute requests spread over a
    day against a limit of one concurrent request read "peak 230%, mean 230%, every request
    over the limit"; no more than one was ever in service. Revised: peak 100%, time-average
    2.1%, none over.
  - *F4, median service time.* With lognormal service times (σ = 1), request rate × the
    endpoint's median latency understated mean concurrency by 38% and the peak by 48%.
    Revised (each request's own recorded time): 0.0% on the mean.
  - *F3, a peak without context.* The conditional mean read 0.85 on a stream whose
    time-average was 0.59, with no field naming the denominator. Revised: two labelled
    means, and no output prints a peak without the time-average.
  - *F1, determinism.* Summing volume-scaled floats in record order gave 29 distinct outputs
    for 60 orderings of the same 3,000 records. Revised: exact integer accumulation; one
    output.

  The general point for the paper: **a load figure that is exactly what its specification
  says can still be unquotable**, and the checks that caught it were against an independent
  truth (an event sweep over raw arrival and service times), not against the specification.
- **A second round, on presentation (all synthetic, FB §21.5.5).** With the engine equal to
  that independent truth, a whole-branch review still found outputs that would have been
  misread:
  - *FR1, a default read as a measurement.* Traffic moved to an endpoint with no recorded
    request, no latency signal and no asserted latency was placed at the routing score's
    tier default (200 ms): ten-second requests read "peak 2.0% / avg 1.9%" of a limit the
    current arm filled ("100.0% / 96.8%"). Now a state — no concurrency figure — and the
    headline names the endpoint it was computed without.
  - *FR3, right-truncation.* A record ends; service time a scenario places after its end is
    in no bucket. With an asserted service time of 600 s on a 30-minute record 16.4% of the
    proposed arm's service time was outside the window (mean 50.1 against a steady state of
    60); at 7,200 s, 87.4%, and the figures no longer responded to the asserted value. Kept
    (the alternative makes the baseline depend on the proposal) and disclosed as a share.
  - *FR4, how often.* The pair above. The share of buckets over the limit is now part of
    every printed peak. A modelled "expected peak of a steady stream" was considered as a
    reference and **not** added: it needs an arrival assumption the router has no ground to
    make about a tenant's traffic.
  - *FR5, exact arithmetic is not the same as stable arithmetic.* Under one non-dyadic
    volume scale shared by three applications, `0.7a + 0.7b + 0.7c ≠ 0.7(a + b + c)` in
    floating point moved a peak to another, equally loaded, bucket. Equal factors are now
    summed as integers first.

  The point for the paper: **after the numbers are right, the remaining errors are in what
  a maximum, a default and a truncated record are allowed to look like.**
- **Design points usable in the paper:**
  - *No overload-to-rejection step.* A fluid "excess over the limit" count is a lower bound
    that reads as an estimate (the loss formula for a limit with no queue is Erlang-B, above
    zero below 100%), and the gateway under study would fail a refused request over rather
    than lose it. The exposure is reported instead: time and traffic above the stated limit.
  - *No interval from the row bootstrap.* §4.2's bootstrap treats rows as exchangeable in
    time; capacity is a function of their order. The figures are points, and the report
    states why (FB §21.3).
  - *A short record is a state, not a number* (`window_too_short`, below ten buckets with an
    arrival by default).
  - *The capacity output names no remedy* and never presents a security requirement as the
    thing to change; scenarios stay in file order (HO §37).
- **The experiment to run (E10a — the handoff's healthcare example, HO §25, §31):** on a
  deployment with one on-prem endpoint and one in-region cloud endpoint, generate persona
  traffic in the handoff's mix (public questions 60%, redacted claim summaries 25%, raw
  patient data 10%, legal and audit 5%) at a known arrival rate. State the on-prem
  endpoint's `max_concurrency` from its serving configuration. Compare two scenarios against
  the current policy: "all healthcare-related requests on-prem" and "on-prem only for
  requests containing raw PII". Report, per scenario and arm: on-prem peak **and**
  time-average utilisation, the share of the window and of requests above 100%, the bucket
  length and the window, and the share of service time by source (recorded, endpoint mean,
  replay), next to the cost, latency and rejection figures the simulator already gives. The
  handoff's illustrative row is "required on-premises utilisation 55% → 230%"; **the
  measured values are TO MEASURE** and must not be quoted from the handoff.
- **First run on recorded rows (DGX lab, 2026-10-07, FB §21.5.4a) — usable as an
  illustration only.** One app, one endpoint, 96 rows that are four bursts of the same 24
  test requests, limits invented for the run. At the default 60 s bucket the tool refuses a
  figure (`window too short (3 of the 10 buckets needed)`); at 5 s buckets the current arm
  reads `peak 331.7% / avg 0.4%, over 100% in 0.2% of 3880 buckets (qwen3-coder-next,
  tokens_per_minute, 5 s buckets)`, and a scenario that raises the per-request budget (28
  requests newly served) reads `peak 1272.5% / avg 2.6%, over 100% in 0.4%`. Two things worth
  a sentence in the paper: the peak alone would have read as a 3× overload where the average
  and the share show a burst of test traffic; and capacity demand moves with the POLICY
  scenario (the rescued requests are the large ones), which is why it is computed from the
  replayed arm. Artifacts: `docs/research/results/p3c4-dgx-lab-2026-10-07/`.
- **Experiments that test the model rather than use it:** E10b stated against observed (does
  utilisation by this definition track latency or refusals on a real endpoint?); E10c
  recorded latency against backend time in service — in two steps, because on a served row
  the recorded figure is the **sum of the pipeline steps' latencies**, not wall-clock: the
  sum against wall-clock, then wall-clock against backend time; E10d bucket-length
  sensitivity of peak and average on recorded traffic; E10e the arrival estimate, which
  subtracts that same sum from the write time; E10f the error of giving a moved request its
  new endpoint's mean service time. All **TO MEASURE** (FB §21.5.4).
- **Evidence so far:** synthetic verification only — two hand-computable arrival patterns
  reproduced exactly; the before/after of F1–F4 on synthetic Poisson streams; engine
  mutations each caught by a named test (run before the second round and not re-run); the
  before/after of FR1–FR5 (FB §21.5.1–§21.5.3, §21.5.5). Nothing has been run on recorded
  traffic.
- **Limits (FB §21.6):** no queue; recorded latency is end-to-end, includes endpoint
  queueing, and on a served row is a sum of step latencies rather than wall-clock; a request
  recorded as failed, failed-over or rejected gets the endpoint's mean service time (a
  120 s timeout on one request in ten: mean concurrency 0.968 where holding the slot would
  give 2.03 — synthetic); a moved request has no service time of its own; the record is
  right-truncated (FR3); a peak depends on bucket and window, and the headline is also a
  maximum over endpoints and limits; calendar-minute buckets understate a sliding-window
  rate limit (20 requests in 10 s across a minute boundary against 10/min read "peak 100%";
  on steady streams the busiest sliding 60 s held 1.00–1.10× the busiest calendar minute —
  synthetic); client retries are counted
  as independent demand; demand refused before a record is written, or excluded from
  simulation, is censored (counted where it can be; the direction is understatement); a
  congested recording compounds with a volume scale or moved traffic, with no feedback;
  estimated arrival; a stated limit is one number although real capacity depends on the
  lengths served; volume scale multiplies and does not generate traffic; no range.

### 4.4 Replay fidelity

#### 4.4.1 Replayable rejections (B60; spec `docs/superpowers/specs/2026-10-06-replayable-rejections-design.md`)

- **Claim:** a request the gateway rejected is replayable from a content-free record, once
  the record carries the one number the admission decision read: the token count the router
  was given. A fidelity check must compare *outcomes* (routed or rejected), not only the
  chosen model.
- **What changed (FB §19.1):** every outcome row records `routing_tokens` (RouteForge's own
  post-gate, post-compression count), separate from the backend's billed `prompt_tokens`.
  Replay routes on the first and prices on the second. A rejected row is priced at the
  admission estimate and counted as an estimate. A rejected row with no recorded size is
  excluded and counted, never replayed as an empty prompt.
- **Lesson to state (extends §3.7):** *every input the admission and routing decision read
  must be on the record for rejected requests too; agreement must compare outcomes, not only
  the chosen model.* B55 and B60 are the same defect found twice: a field the decision read
  was missing from the row, and replay substituted a default that looked plausible. The two
  agreement measures are each blind to the other's failure (FB §19.1), which is why both are
  reported and the gate takes the lower.
- **Second finding (FB §19.2):** a pooled outcome rate dilutes rejections, which are a small
  share of traffic. The count of unreproduced rejections out of the recorded rejections is
  the trustworthy number; report it beside the rate.
- **Invariants to cite, with the test that pins each** (full table: FB §19.3):
  R1 `test_schema_is_content_free` and
  `test_r1_rejected_row_with_pii_carries_a_count_and_no_content`; R2
  `test_r2_served_row_replay_is_unchanged_when_the_counts_coincide`; R3
  `test_r3_budget_rejected_row_is_rejected_in_the_current_arm`; R4
  `test_r4_raising_the_cap_newly_serves_it_at_the_admission_estimate`; R5
  `test_live_round_trip_budget_rejections_replay_as_rejected`; R6
  `test_r6_unsized_rows_are_excluded_and_counted_never_routed`; R7
  `test_no_serving_module_imports_app_eval`; R8
  `test_outcome_recording_adds_nothing_to_the_success_wire`; R9
  `test_r9_export_without_the_field_still_loads_and_replays_served_rows`; R10
  `tests/test_outcome_routing_tokens.py`; R11
  `test_r11_every_status_has_a_recorded_outcome_class`; R12
  `test_r12_gate_takes_the_lower_of_the_two`; R13
  `test_r13_unmigrated_table_serving_continues_and_the_documented_alter_repairs_it`.
- **Privacy argument for the paper:** the added field is an integer length, measured after
  the gate. It is the same class of value as the token counts already recorded, and for
  requests under 2000 tokens it was already recoverable from `complexity`. It adds a length
  side channel for rejected requests, and that is all it adds (reference
  `routing-outcomes.md` §2).
- **Experiment E7b — done on the DGX lab, 2026-10-06 (FB §19.4.1).** Same app, endpoint,
  cap, 24 requests and six scenarios as the run that found the defect, on rows recorded after
  the upgrade (artifacts: `docs/research/results/b60-dgx-lab-2026-10-06/`). 7 of 24 requests
  rejected live for budget; the current arm rejects 7 of 7; outcome agreement 1.0 over 24
  rows, model agreement 1.0 over 17. `raise-request-budget` newly serves **7** (before the
  fix: 0); a 95% price cut also newly serves 7; `token_scale: 0.6` newly serves 3. The
  pre-upgrade export replayed with the new code puts its 7 rejections under
  `missing_routing_tokens` instead of routing them. Usable as a before/after pair for the
  paper; it is one app and one endpoint, so present it as a defect-closure check.
- **Measured gap between the two token counts (FB §19.4.1) — and a second finding (B64).**
  On the 17 served rows RouteForge's `len // 4` count was 1.54× the backend's (21392 vs
  13849; 1.53–1.56 for prompts of 600+ backend tokens, 0.70 for a 23-token prompt). The
  admission estimate is built on the larger number, so the budget check is conservative: the
  smallest rejected request (estimate $0.05344, cap $0.05) would probably have cost about
  $0.035 at the backend's count. That is an inference from the served rows' ratio — the
  rejected requests were never sent — and the ratio is for repeated English prose with one
  tokenizer. Paper angle: *a cost gate is only as accurate as its token estimate; a
  conservative estimate turns into silent over-rejection, which is invisible unless both
  counts are on the record.* Real-traffic ratio is still **TO MEASURE**.
- **Limits:** a rescued request is costed at its admission estimate; a served row an
  endpoint reported no usage for (e.g. a stream whose endpoint sent no usage block) is
  replayed the same way, at its admission estimate, not a bill; a recorded `context_overflow`
  needs a declared window; RouteForge's token count is not the backend's.

#### 4.4.2 Still pending: failover/escalation, breaker state, spend/RPM admission — *spec pending*
### 4.5 Administrator review workflow — *spec pending* (closes HO §37 P3 "admin approval")
### 4.6 P2 evaluation metrics (spec `docs/superpowers/specs/2026-10-07-p2-eval-metrics-design.md`; HO §18 metrics 6–9, §19, §36 Phase 2)

Two plans. **Built (FB §22):** per-case scores, the group report, the offline
router-version check. **Designed, not built:** churn robustness and confidence calibration.

**What the built half lets the paper claim.**

- *A feasible-set evaluation can report per-group results honestly at small n.* Each figure
  carries its own denominator; the overall figure is the groups combined by those
  denominators (a worked counter-example shows case-count weights are wrong for a
  conditional metric: 0.31875 against 0.24); a figure over fewer than a stated minimum
  prints a count and no rate, while violation counts are never suppressed (FB §22.1).
- *Two router configurations can be compared with everything else held fixed.* Because the
  eligible set is derived before scoring and a sweep row is a pinned call, a candidate
  routing config scored as extra arms of one run shares the cases, the eligible sets, the
  seed and every endpoint outcome with the baseline (FB §22.2). This is a property of the
  eligibility-first architecture, and worth stating as one.
- *A change to the router cannot produce a policy violation.* A candidate that moves 16 of
  22 choices makes no ineligible choice at any of ten seeds (FB §22.5.2–§22.5.3). The check
  treats any such choice as a hard failure that no tolerance can buy back, and a test
  injects one to show the detector fires.
- *An explicit, conservative decision rule*: a figure is judged on its **whole**
  bootstrap range against the tolerance, never on the point estimate alone — `lo > tol`
  regresses it, `hi <= tol` clears it, and anything straddling the tolerance is
  inconclusive (FB §22.2, §22.7). An earlier, point-estimate-only rule was replaced the
  same day it shipped after a simulated counterexample showed it calling an
  identical-in-truth candidate `regressed` in most draws on a small suite (FB §22.7); that
  replacement, not the original rule, is what fifteen single-rule mutations each caught by
  a named test confirm (FB §22.5.4).

**What it does not let the paper claim.**

- Nothing about routing **quality**: on the simulated backend quality is an input
  (FB §6.1), and the measured consequence is in FB §22.5.3 — the same candidate passes at
  eight seeds and is inconclusive at two, on pass rate alone.
- Nothing about **which** router configuration to use. The check judges against tolerances
  an operator sets; it recommends nothing, ranks nothing and promotes nothing.
- No per-group claim about `onprem_only` placement or about protection posture from the
  starter suite: those groups are below the minimum, or there is only one (FB §22.5.1).
- No statement that the bootstrap range has its nominal coverage (unmeasured), and no
  multiplicity control — the SYNTHETIC null-rate table (FB §22.7) shows an
  identical-in-truth candidate still reads `regressed` in a quarter to a third of draws on
  22 cases at zero multiplicity correction.
- No "promotion gate" in the sense of something that promotes. Say "offline pre-change
  check"; nothing is applied (CLAUDE.md rule 4).

**Still to build, and what each must deliver for the paper.**

- *Churn robustness* (spec §6): a declared set of single changes to the policy and the
  registry; per change, violations (the headline: 0), newly empty feasible sets and whether
  each was rejected with a conflicting core, the share of choices that moved, and regret
  deltas; and each change's expected effect on eligibility checked ("a tightening never
  widens") — the bench-side witness of the P1 monotonicity result.
- *Confidence calibration* (spec §9). The finding to state first is negative and
  structural: **RouteForge's router emits no confidence.** The only confidence in the
  system is the backend's figure for one answer (a temperature-softened geometric-mean
  token probability, or a constant 0.9 when the provider returns no logprobs), which the
  gateway's escalation threshold reads and nothing records. The measurement is that
  figure's reliability and the threshold's operating point on live endpoints. In
  simulation it is not measurable by construction (the simulated backend reports 1.0), and
  the paper should say that rather than omit the metric.

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
| E4 | Breaker wasted attempts | FB §12.4 | done (simulated, FB §12.4) and **observed live once** (FB §24.3): one 181.5 s outage of one endpoint name; 3 consecutive failures opened the breaker, 8 of 58 routed mid-tier rows in the window touched the dead endpoint, 65 rows diverted, breaker open ~38 s past recovery, 2 requests failed 502 with no affordable failover candidate (B70). One outage: a mechanism observation, not a rate. Caveat: three names, one model behind a test proxy. |
| E5 | Replay agreement before/after B55 | FB §15.4 | done |
| E6 | Shadow-price non-monotonicity counterexample, live | FB §17.3 | **not exercised live** (FB §24.6): 900 rows, three endpoint names, newly rejected = 0 on every relaxation. Remains a constructed counterexample; a purpose-built fixture **TO MEASURE** |
| E7 | Context-window replay effect on agreement | P3c-2, FB §18.4.1 | overflow: first run done (no overflow in the export: agreement 1.0, 0 hops), real overflow failovers **TO MEASURE**. Breaker: measured (FB §24.4) — model agreement 0.926 over 880 served rows, outcome agreement 1.0 over 900; all 65 disagreements are breaker-diverted rows replay sent to the down endpoint (replay has no breaker state, B70). Caveat: three names, one model behind a test proxy. |
| E7b | Rejected rows replay as routed (B60) | FB §18.4.1, §19.4.1 | done: defect observed (7 of 7 rejections replayed as routed), fixed, re-run on post-upgrade rows — 7 of 7 rejected in replay, outcome agreement 1.0 over 24, raise-budget newly serves 7 |
| E7c | Token-estimate gap and over-rejection (B64) | FB §19.4.1, §23.4 | measured on synthetic text, 645 served rows (GLM tokenizer): routing/backend median 1.72 on prose, 0.79 on code, 0.70 on JSON, 0.52 on log lines; summed ratio 0.955 hides it. The estimate over-counts prose and under-counts logs. Real documents **TO MEASURE** First real agent-loop rows (FB §24.9): 21 served calls from one coding-agent session, 13,962–27,480 backend prompt tokens — summed ratio 0.976, per row 0.960–1.022. One session, dependent rows: a data point, not a distribution. |
| E8 | Healthcare scenario triad, traffic-weighted vs static | P3c-2 | analogue done on declarations only (FB §24.5): four scenarios over 900 rows with 90% ranges; two tightenings a static check would call free rejected 4 [1, 7] and 5 [2, 9] requests on budget. Caveat: three names, one model behind a test proxy. Healthcare triad on different models and a cloud provider **TO MEASURE** |
| E9a | Range width against sample size | §4.2, FB §20.6.2–§20.6.3, §23.7 | done at n = 50–800 on 912 independent lab rows: width × √n / n flat from n = 200 (1/√n), narrower than that below it (rare events squeezed against 0). 5,000 / 50,000 rows **TO MEASURE** |
| E9b | Split halves of a real export | §4.2, FB §23.7 | done (100 splits of 912 rows): half-A 90% range contains the half-B point 66–76% of the time (mean 71.5%); the reference for a calibrated range is 75.5%, not 90% — the planned test was mis-specified. Direct coverage against a larger export **TO MEASURE** |
| E9c | Coverage under dependence (agent-loop traffic) | §4.2, FB §20.6.3 | **TO MEASURE** |
| E9d | Quality range with and without calibration posteriors | §4.2, FB §20.6.3 | **TO MEASURE** |
| E10a | Healthcare scenario pair with stated on-prem capacity: peak and time-average utilisation per arm, with bucket length and window | §4.3, FB §21.5.4 | analogue done (FB §24.7): illustrative stated limits, 60 s buckets — no-change peak 77.5% / average 48.4%; three of four scenarios put one endpoint name over its limit (peak 115.0%, 127.5%, 132.5%; over in 17.6%, 47.1%, 52.9% of 17 buckets). Limits are assumptions and all names are one server; with real stated capacity **TO MEASURE** |
| E10b | Stated capacity against observed endpoint behaviour at 50% / 100% / 150% average | §4.3, FB §21.5.4 | **TO MEASURE** |
| E10c | Recorded latency against wall-clock and backend time | §4.3, FB §21.5.4, §23.5 | first half done: recorded = 0.75 × client wall-clock (median), short by a near-constant ~160 ms, 876 rows. Wall-clock against backend time in service **TO MEASURE** |
| E10d | Peak and time-average utilisation against bucket length (10 / 60 / 300 / 3600 s) | §4.3, FB §23.6 | done on recorded traffic (illustrative stated limits): peak 212% / 88% / 64% / 16%, average 27% / 57% / 49% / 16%; the binding limit changes from concurrency to requests per minute |
| E10e | Arrival estimate | §4.3, FB §23.5 | done: bucket changes for 4.2% of rows at 10 s, 0.9% at 60 s, 0 at 300 s; `ts − latency` is ~0.2 s after the client start (includes an unmeasured clock offset) |
| E10f | Error of giving a moved request its new endpoint's mean service time, by request size | §4.3, FB §21.5.4 | **TO MEASURE** |
| E11a | Group report on the simulated starter suite: figures with denominators, groups below the minimum | §4.6, FB §22.5.1 | done (simulated, seed 0) |
| E11b | Router-version check: negative control, a cost-regressing candidate, a tier-threshold candidate; verdicts at seeds 0–9 | §4.6, FB §22.5.2–§22.5.3 | done (simulated) |
| E11c | The check's detectors under single-rule mutation | §4.6, FB §22.5.4 | done (15 of 15 caught) |
| E11d | Group report and check on live endpoints, ≥ 5 cases per placement and protection group, two placements | §4.6, FB §22.5.5 | **partly done** (FB §24.8): 22 cases, three endpoint names, 66 live calls — check PASS, 16 figures within tolerance, but the candidate changed 0 of 22 attempt chains, so it shows the tool runs live, not that it detects a regression; `escalated` (n = 2) and `onprem_only` (n = 4) below the minimum. A suite where weights change a route, ≥ 5 per group, **TO MEASURE** |
| E11e | Coverage of the paired case bootstrap on split halves of a larger suite | §4.6, FB §22.5.5 | **TO MEASURE** |
| E11f | Churn robustness and confidence calibration | §4.6 | not built (second P2 plan) |
| E12+ | fidelity, R2/R3/R4 | §4–§5 | specs pending |

**Data caveat for every live number:** the Spark serves one app (`kit-gtm`, Qwen Code), all
models priced $0 (FB §17.4). The DGX lab has **one** priced on-prem endpoint at a time
(`qwen3-coder-next` to 2026-10-07, `glm-5.3-flash` from 2026-10-08) and synthetic test apps.
The 2026-10-08 run (FB §23) is the first with independent rows: 912 rows, three apps, one
endpoint, synthetic text. No request has yet had two eligible endpoints, so no live result
exercises ranking, failover, or a residency or region difference between endpoints. That
needs a second endpoint — plan it before writing the evaluation section.
**Artifacts:** `docs/research/data/`. The `docs/research/results/...` folders cited for the
2026-10-06 and 2026-10-07 runs were git-ignored and never committed (FB §23, correction).

---

## 7. Threats to validity (collect here)

- Tier selection is length-based; the "router" contribution is mostly the eligibility seam,
  not learned routing (FB §6.7).
- Simulated quality is an input (FB §6.1); deterministic graders; synthetic single-turn cases.
- The router-version check (§4.6) judges one arm on one suite, one simulation seed and one
  case order. Its range is a percentile bootstrap over 22 cases: coarse, of unmeasured
  coverage, and sensitive to the seed — the same candidate gets a different verdict at two
  of ten seeds when the verdict rests on simulated pass rate (FB §22.5.3). Group results
  rest on labels the case author wrote, and five of twelve groups are below the five-case
  minimum. No multiplicity correction is made.
- Single tenant, single seed live runs; $0 prices on the Spark.
- Replay is first-decision only (until §4.4), with frozen signals; cached tokens not recorded.
- Uncertainty ranges (§4.2) are sampling variability under an independence assumption that
  agent traffic violates; they exclude replay bias by construction. Coverage on real records
  is unmeasured (E9b, E9c). They condition on the applications in the export: variation
  between applications is outside them (synthetic measurement, FB §20.6.1). Stratifying by
  application understated the **totals** range with many small applications; small
  applications are now pooled, and the remaining `(n − S)/n` effect is disclosed, not
  corrected (FB §20.7).
- Capacity figures (§4.3) compare **operator-stated** limits with replayed demand. They
  contain no queueing model and no measured capacity, take service time from the router's
  end-to-end recorded latency (a mean for a request that moves), estimate arrival from the
  record's write time, and carry no interval. A peak is a maximum over buckets and depends
  on the bucket and window lengths; it is quoted only with the time-average and the share
  of buckets above the limit. Whether utilisation so defined tracks real endpoint behaviour
  is unmeasured (E10b). Any "N% of capacity" in the paper is a statement about the stated
  limit, not about the hardware. Specific threats, each a known direction of error (FB
  §21.6; the figures there are synthetic):
  - *Right-truncation.* A scenario with longer service times than the recorded ones is
    understated, by the reported `service_after_window_share`.
  - *The recorded latency is not wall-clock.* On a served row it is the sum of the pipeline
    steps' latencies; it also contains router overhead and any endpoint queueing. Arrival
    and service time both inherit this (E10c, E10e).
  - *Failures are counted at the mean.* A request recorded as failed, failed-over or
    rejected, which a scenario routes, gets the endpoint's mean service time; a long
    timeout that held a slot is understated.
  - *Calendar buckets against sliding limits.* A provider quota over any 60 seconds is
    understated by the busiest calendar minute.
  - *A maximum over more than buckets.* The headline is the largest figure over endpoints
    and stated limits too; stating more limits can only raise it.
  - *Retries.* Client retries after live failures are replayed as independent demand a
    healthier scenario would not have had: overstatement.
  - *Censoring.* Demand refused before a record is written, and rows excluded from
    simulation, are missing: understatement (the excluded count is printed).
  - *Congestion compounds.* Latencies recorded under congestion are inflated; a volume
    scale or moved traffic multiplies them with no feedback.
  - *An endpoint with no service time has no concurrency figure.* A scenario that moves
    traffic to a new endpoint must assert its latency to get one; the assertion is then the
    operator's, and is to be reported as such.
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
