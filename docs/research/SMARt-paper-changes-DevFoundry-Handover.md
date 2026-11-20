# SMARt in the RouteForge paper: what changed, and what it means for DevFoundry

**Date:** 2026-11-20
**From:** RouteForge
**To:** DevFoundry
**Paper:** `docs/research/paper/main.tex` (and `main.pdf`, `arxiv-submission.tar.gz`), new section
"Governed autonomy along a trajectory: a design" (`\label{sec:smart}`)
**Read with:** [`SMARt-DevFoundry-Handoff.md`](./SMARt-DevFoundry-Handoff.md) (2026-11-10). That note
settled **who owns what**; this one records **what the paper now says**, so that what DevFoundry
builds matches the published design.

## 1. Summary

Srini Ramaswamy (DNRS.ai) is now a co-author. Following his recommendations, the paper adds a
**design** section that maps his SMARt managed-autonomy framework onto RouteForge's eligible set,
router and replay. The section is explicitly **not implemented and not evaluated**: it adds no
results, and the "Not measured" list now names it.

Most of what the section describes is the harness-side work the 2026-11-10 handoff already gave
to DevFoundry: the S/M/A/R controller, anchors, anchor replay, review and release. The paper is
more precise than the earlier guide in several places, listed in §3. Treat those as requirements
when you build, because the published text will be checked against what ships.

What changed in the paper:

| Where | Change |
|---|---|
| Byline, `arxiv-metadata.md` | Second author: Srini Ramaswamy, Ph.D., DNRS.ai, USA |
| Abstract | One sentence: a SMARt-based design, not yet implemented or evaluated, that extends eligibility to governance state, replaces length tiering with calibrated suitability, and replays anchor changes over ordered trajectories |
| Contributions | A fourth item: "A design for governed autonomy (not evaluated)" |
| New section `sec:smart` | Four subsections: authority as part of the envelope; calibrated suitability instead of length; replaying changes to invariant anchors; properties and experiments. Two new tables, `tab:smart` and `tab:smartreplay` |
| Limitations ("Not measured") | "Any part of the design of Section `sec:smart`" |
| Related work | New paragraph: managed autonomy and uncertainty-aware routing |
| Conclusion | One sentence on the SMARt outline |
| Bibliography | `smartma`, `smartbounded`, `smartgears` (Ramaswamy et al.), plus semantic entropy (Farquhar et al., Nature 2024), uncertainty routing (Zhang et al.), "Dynamic LLM Routers are Often Misguided" (Wang et al.) |
| Size | 28 → 33 pages, 16 → 18 tables |

## 2. The three SMARt concepts the paper uses

- **Four governance modes** (from *Intelligence as Managed Autonomy*): **S**table,
  **M**eta-cognitive, **A**ssisted, **R**egulated. Evidence drives the transitions. Only Stable
  mode may produce authoritative external output.
- **Invariant anchors** (from *Bounded Autonomy and Verifiable Safety*): defined by subject-matter
  experts (SMEs), over a finite governance abstraction with convergence under stated assumptions.
  The paper also discusses external enforcement for black-box agents, which is where a gateway
  and harness sit.
- **Execution gears are not governance states** (from *Managed Autonomy at Runtime*). Do not
  merge them into one enum.

The paper keeps three variables apart that are easy to confuse: **governance state**, **execution
permission**, and **model tier**. Moving down the hierarchy means *less autonomous authority*. It
does **not** mean a smaller or cheaper model. Investigating a hard case may need a *more* capable
local model.

## 3. What the paper now specifies, and who builds it

Owners follow the 2026-11-10 handoff: **DF** = DevFoundry, **RF** = RouteForge (backlog item).

### 3.1 Authority as part of the envelope

| Paper requirement | Owner |
|---|---|
| **Per-task state record**: application, opaque task key, state σ ∈ {S, M, A, R}, state version, active anchor-bundle version, reason codes, deadlines, recovery-attempt count | DF |
| **Pure transition function** σₜ = δ(σₜ₋₁, evidenceₜ, 𝒜), where 𝒜 is the active anchor bundle | DF |
| **First triggers are observable only**: missing required fields, disagreeing trusted tool results, failed validators, stale evidence, repeated failed attempts, explicit human revocation. Model uncertainty is added **only once calibrated**. **No hidden chain of thought is collected.** | DF (gateway-observed signals: RF B96) |
| **Example rules**: missing evidence S→M; a failed bounded diagnosis M→A; a critical anchor breach or an unanswered request for assistance → R **from any state** | DF |
| **Timing**: the state is updated from evidence available *before* the call it affects is dispatched. A new restriction never waits for the next request. | DF |
| **Dispatch set is an intersection**: E_dispatch = E_policy ∩ E_gov(σ) ∩ E_cap ∩ E_adm. The governance term **only narrows** and never grants a cloud endpoint that policy excludes. | E_policy, E_cap: RF (exists). E_gov: RF B95 (enforces), DF (requests). E_adm: RF B82 (per endpoint), DF (task-level reservations) |
| **Tool actions have their own permitted set Tₜ.** A held purchase can coexist with a permitted local recovery call or a read-only lookup. A hold is **not** encoded as "no model exists". | DF |
| **Capability shortfall rejects; it does not clamp.** If a hard capability requirement leaves no local endpoint, reject with that requirement in the conflicting core. | RF (P3b core; B95 names the hint in it) |
| **Enforcement points**: model dispatch, failover, cache hits, output release, tool execution, resume. Output that must be checked is buffered; a streamed token cannot be recalled. Revocation stops *future* mediated effects only. | Dispatch, failover, cache hit: RF B95 acceptance. Output release, tool execution, resume: DF |
| **Fencing**: transitions are persisted transactionally with a version used as a fencing token, rechecked before dispatch **and again before any external effect** | DF |
| **Identity**: the task key is bound by the server; child and resumed tasks inherit the parent's restrictions; a new client identifier is not evidence of a clean task. (The paper notes that RouteForge's prototype envelope `header` mode *can* be reset by changing an id. That is why authority must not live there.) | DF |
| **Release**: authenticated, scoped to one task, action and anchor version, expiring, recorded. **A timeout is never an approval.** | DF (`domain/approvals.py`) |
| **First version**: automatic transitions only restrict. Restoring authority needs an explicit release. **Returning to S never removes an inherited placement restriction.** | DF |

The paper's Table `tab:smart` gives a proposed per-state policy. The specific limits are
deployment choices, not SMARt requirements:

| State | Behaviour | Permission boundary |
|---|---|---|
| S | Select an eligible model, run the configured checks | Approved actions and authoritative output proceed |
| M | Bounded internal diagnosis, optionally on a designated local recovery model | Contested action and authoritative answer held; internal drafts marked as drafts |
| A | Request approved evidence, tools or expert help | Only separately authorised recovery operations; the contested action does not resume on its own |
| R | Stop contested autonomous execution; create a human-review item | No automatic model or tool call for the contested action |

### 3.2 Calibrated suitability instead of length (RF: B80 shipped, B99 open)

- **Three questions are kept separate**: how big the request is (context and cost), which model
  will likely succeed (selection), and what consequence is permitted (governance). An uncertainty
  estimate may inform **only selection**.
- **Selection**: e* = argmin over E_dispatch of Ĉ_total(x, e), subject to P̂(success | x, e) ≥
  τ_workload. Ĉ_total counts probes, verifiers, failed attempts and retries. Risk floor,
  escalation floor and model pin stay hard constraints. If nothing qualifies, return an explicit
  hold or ask for assistance.
- **Probes are dispatches**: they are drawn from the same E_dispatch, so they stay inside the same
  privacy boundary and task budget.
- **Terminology**: call it *uncertainty-aware*, **not** "entropy-gated", until a specific measure
  is implemented and evaluated. Entropy is not task difficulty or business risk: confident wrong
  answers and validly diverse answers both break it.
- **What DF does**: pass the workload via `x-routeforge-workload`. DF does not choose models.

### 3.3 Replaying changes to invariant anchors (DF, with HarnessLab)

The paper prints this example schema. It is marked as proposed, not shipped. Use it as the
starting point for the DF anchor registry so that the paper and the code agree:

```yaml
anchor_id: procurement.supplier_evidence_freshness
version: 2
scope: {application_id: procurement-assistant}
checker: numeric_upper_bound          # from an allowlist; never a pasted expression
input_feature: supplier_evidence_age_seconds
parameters: {maximum: 14400}          # was 86400 in version 1
missing_evidence: hold
on_failure: {target_state: ASSISTED,
             action: refresh_from_approved_supplier_source}
owner_role: procurement-sme
```

Requirements the paper states:

1. Anchors are **versioned, immutable, stored with their hash**. The checker comes from an
   allowlisted type with structured parameters, and has units and defined missing or invalid
   evidence behaviour.
2. **Records keep the anchor's inputs** (for example, evidence age at decision time, with source
   and validator versions), not only its verdict. Content-free hashes are not enough for semantic
   anchors.
3. **Replay groups records by authenticated task, in order**, initialises state, and runs the
   **production** checker, transition, permission and routing code under the current and the
   proposed bundle.
4. **Run the current arm first**: it must reproduce the recorded transitions, holds and routes
   before the proposed arm is trusted.
5. **Label every replay claim** with one of three kinds (`tab:smartreplay`):
   - **fixed-feature**: exact decisions for the recorded events;
   - **frozen-trajectory**: conditional estimates that assume later events are unchanged;
   - **closed-loop**: measured outcomes of a new experiment.
6. **Budget and capacity are guaranteed by runtime admission, not by replay.** Replay only
   *estimates*. The guarantee comes from atomic reservations covering probes, retries and output
   caps, reconciled against reported usage.
7. **Rollout**: shadow, then canary, with an explicit rule for whether active tasks keep their
   anchor version.

The routing-only forecast of a narrowing policy is RF **B97**.

### 3.4 Properties and experiments the paper commits to

These eight properties are to be checked over generated event sequences. They are DF's
acceptance tests, except 1, which both sides test, and 8, which is shared with RF B82:

1. Every mediated endpoint attempt lies in E_dispatch.
2. A contested action cannot run in R without a separately authenticated release that satisfies
   the current policy.
3. Changing the client's task id does not remove a restriction on inherited context.
4. Recovery terminates or escalates within its declared attempt and time limits.
5. Missing or invalid required evidence never becomes a low-risk estimate.
6. An approval does not authorise changed arguments, a changed anchor version or a different
   action.
7. Replaying identical events and snapshots gives identical checker, transition and selection
   decisions.
8. Atomic reservations preserve budget and resource invariants under concurrent calls.

Experiments (the paper names three):

- **Authority**: the entity-only envelope against the controller. Measure contested actions
  blocked, useful actions allowed, false holds, time to escalation, and bounded recovery. Include
  identity-reset and concurrency cases.
- **Routing**: length rule, cheapest eligible, fixed eligible pair, and a cheap-first verifier
  cascade, against the suitability selection. Measure useful success, calibration, coverage,
  all-in cost, latency and GPU time on held-out tasks, with thresholds fitted on separate data.
- **Anchor replay**: current-arm fidelity first, then predicted vs. observed changes in states,
  routes, rejections, load and review volume for several held-out rule changes.

Reporting rules: a model failure or a human hold **stays in the denominator**; the unit of
analysis is the **task, not the turn**; directly replayable rule changes are reported separately
from counterfactuals.

## 4. What DevFoundry gets from RouteForge (status today)

| RouteForge item | Status | Paper term |
|---|---|---|
| B94: governance interface spec (`docs/architecture/governance-interface.md`) | 🔴 open; needs DF sign-off | none (interface contract) |
| B95: `x-routeforge-governance` raise-only narrowing (`onprem_only`, `max_tier`, `require_verification`, `no_cache`) | 🔴 open | E_gov(σ) |
| B96: gateway-only evidence on every response (verification verdict, failover count, backend error class) | 🔴 open | trigger inputs |
| B97: narrowing forecast in the policy simulator | 🔴 open | fixed-feature replay (routing only) |
| B80: quality-floor selection, cheapest eligible model clearing a calibrated bound | ✅ shipped 2026-11-10 | the suitability-selection equation |
| B99: bounded local uncertainty probes | 🔴 open, optional | probes |
| B82: per-credential spend cap, per-endpoint concurrency admission | 🔴 open | E_adm |

## 5. Points to watch

1. **`max_tier` in recovery.** The paper says lower authority is *not* a smaller model (§2).
   Don't send `max_tier: cheap` as an automatic side effect of entering M. If a recovery model is
   wanted, name it as a designated local recovery endpoint.
2. **Possible double counting (owner decision needed).** The 2026-11-10 handoff §6 said the
   controller, anchors and anchor replay are harness contributions, and that claiming them in both
   papers would break the "no double counting" rule. The RouteForge paper now *describes* them as
   an unevaluated design, and Table `tab:smart` is captioned "A proposed RouteForge policy". If a
   DevFoundry or HarnessLab paper will *evaluate* the controller, agree with Mani and Srini which
   paper claims what. One option: re-caption that table as a gateway-plus-harness policy and cite
   the DF paper for the controller.
3. **Open coauthor questions** from the implementation guide §11 (Q1, Q3, Q5–Q8 DF's; Q2 and Q4
   shared) are still open. The paper does not settle them.
