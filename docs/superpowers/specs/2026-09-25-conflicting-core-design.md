# P3b: the minimal conflicting core

**Date:** 2026-09-25 · **Status:** approved design, not built
**Priority:** 3b (handoff doc §29 and §37 "Empty feasible sets include a minimal conflicting
core when possible"; "Security constraints are never automatically relaxed").
**Follows:** P1b `FeasibleSet` (`app/policy/feasible.py`), the B49 error envelope
(`app/api/openai_errors.py`), P2b-2 outcome rows, and the P3a simulator.

## 1. Purpose

Today an empty feasible set gives a 422 whose message lists each candidate's *first* failed
stage. An operator or app developer then has to work out for themselves which requirements
actually conflict. P3b names the **smallest set of requirements that no available endpoint
satisfies together**. It keeps endpoint *state* (disabled, circuit open, prior failure)
separate from policy, and attaches remediation hints. The hints never relax a security
requirement, and nothing is applied automatically.

Example (handoff §29): the app is `onprem_only`, the request is pinned to a model, and the
only on-prem endpoint is not that model. The core is `{residency: onprem_only,
requested_model}`. Relaxing either requirement alone would let some endpoint serve the
request.

## 2. Definitions

### 2.1 Requirements (the core's alphabet)

A **requirement** is one operator-policy or request constraint. It has a stable id, a
`security` flag, and a `source` naming where it came from. The ids are fixed:

| id | fires on a candidate when | security | source |
|---|---|---|---|
| `residency` | the resolved residency is `onprem_only` and the provider is not on-prem | yes | `app_residency`, or `sensitivity_rule` (the rule's entity **types**, never matched text) |
| `allowed_providers` | the provider is not in the placement set for a non-`onprem_only` residency (the app's `allowed_providers`, the `private_cloud_ok` provider set, enabled providers) | yes | `app_policy` / `deployment` |
| `allowed_regions` | stage-2 region rule | yes | `app_policy` |
| `require_no_retention` | stage-2 retention rule | yes | `app_policy` |
| `raw_sensitive` | stage-2 raw-sensitive rule (the compiled `model_input: raw` plus detected sensitive content, against an endpoint that does not accept it) | yes | `app_policy` |
| `allowed_tiers` | the app's `allowed_tiers` excludes the candidate's tier | no | `app_policy` |
| `allowed_models` | the app's `allowed_models` excludes the candidate | no | `app_policy` |
| `endpoint_app_allowlist` | the onboarded endpoint's `allowed_app_ids` excludes this app | no | `endpoint` |
| `requested_model` | the request pins a different model | no | `request` |
| `max_cost_usd_per_request` | the router's pre-call estimate for this candidate exceeds the cap (§2.3) | no | `app_policy` / `deployment` |
| `risk_floor` | the candidate's tier is below the effective risk floor (§2.3) | yes | `risk_class` (with the rule name when a sensitivity rule set it) |

Each predicate is exactly the check `feasible_set` / `Router` already applies. §4 requires
that they share code so the two cannot drift.

### 2.2 State (never in the core)

These are facts about endpoints or this request's history, not policy, and are reported
**separately** as `state`:

| id | meaning |
|---|---|
| `no_endpoints` | the effective pool is empty |
| `disabled` | the onboarded endpoint is disabled by the admin |
| `circuit_open` | the step-7 breaker holds the circuit open |
| `prior_failure` | the endpoint already failed on this request (lateral failover) |

Only **available** candidates enter the core computation: candidates in the effective pool
that are not in any §2.2 state. If no candidate is available, the core is empty (`[]`) and
the state list explains the rejection.

### 2.3 The budget case

The router raises a budget rejection only after the policy-eligible set is non-empty. The
candidates the router's budget stage actually considers are those in tiers from the
**effective floor** up to the tier it picked. The effective floor is
`min(max(risk floor, escalation min_tier), best allowed tier)`, which is the same clamp the
router applies. For a budget rejection:

- The candidates entering the matrix are the policy-eligible, available candidates whose
  tier is ≤ the picked tier. This is the router's actual search space, plus the lower tiers
  it refused.
- A candidate in that set violates `max_cost_usd_per_request` when its estimate exceeds the
  cap. It violates `risk_floor` when its tier is below the effective floor.

Candidates in tiers above the picked tier are not part of the router's budget search and are
not in the matrix. The spec records this deliberately, so the core describes what the router
really did.

### 2.4 The core

Let `V(c)` be the set of requirement ids that candidate `c` violates. The candidates are the
available ones: for a policy rejection, the whole available pool; for a budget rejection, the
§2.3 set.

The feasible set is empty exactly when every `V(c)` is non-empty. A **conflicting core** is a
set `K` of requirement ids that meets every `V(c)`. The **minimal conflicting core** is such
a `K` of smallest size.

- It is found by exact search over subsets of the ≤ 11 ids, in increasing size. That is at
  most 2048 checks and runs in microseconds.
- Ties are broken by a fixed id order (the §2.1 table order), giving the lexicographically
  first minimum set. The result is deterministic and does not depend on pool order.
- A minimum hitting set is automatically **irreducible**: removing any id leaves some
  candidate violating nothing in the rest.

## 3. The result object

```json
{
  "requirements": [
    {"id": "residency", "security": true, "source": "sensitivity_rule",
     "detail": {"value": "onprem_only", "entities": ["US_SSN"]},
     "label": "must run on-premises (raised by sensitivity rule on US_SSN)",
     "hints": ["Onboard an on-premises endpoint that also satisfies the other requirements listed here."]},
    {"id": "requested_model", "security": false, "source": "request",
     "detail": {"value": "azure-gpt-mid"},
     "label": "request pins model azure-gpt-mid",
     "hints": ["Send model \"auto\", or pin a model that satisfies the other requirements."]}
  ],
  "state": [{"id": "disabled", "models": ["local-8b"]}],
  "candidates_considered": 3
}
```

- **`detail`** holds policy values only: residency, region tokens, tier names, model slugs,
  the budget cap and estimate, and rule entity types. It never holds request content or
  matched text.
- **`hints`** come from a fixed template per requirement id:
  - **Security requirements** (`security: true`) have templates that only suggest adding or
    approving an endpoint that meets the requirement, for example "Onboard or approve an
    endpoint in an allowed region". They never suggest relaxing it.
  - **Other requirements** may also suggest changing the app policy or the request.
- Nothing is ever applied automatically. A test enforces this on the hint templates.
- **`state`** groups model slugs by state id. Its models are the pool's own slugs.

## 4. Components

| Unit | File | Role |
|---|---|---|
| Violation predicates | `app/policy/feasible.py` | Refactor each stage's check into a named predicate. `candidate_rejection` (first failing stage, unchanged behaviour and codes) and the new `candidate_violations(c, …) -> frozenset[str]` (every violated requirement id) both call the same predicates. |
| Core computation | `app/policy/core.py` (new) | Pure: `conflicting_core(candidates, violations, *, context) -> ConflictingCore`. Holds the §2.1 id table, the minimum-hitting-set search, labels and hint templates. No I/O. |
| Router | `app/routing/router.py` | At each `raise NoEligibleCandidate`, compute the core: the policy case over the available pool, the budget case over the §2.3 set. Attach it as `NoEligibleCandidate(..., core=...)`, a new keyword-only argument that defaults to `None`. |
| 422 body | `app/api/routes.py` + `app/api/openai_errors.py` | For `no_eligible_model`, `error.conflicting_core` = the §3 object. The existing `message` is unchanged apart from a one-line core summary appended to it. `error_payload` gains an optional `extra: dict`, merged into the inner `error` object. |
| Trace | where the router's failure DecisionRecord / `sp.*` attributes are emitted | Emit `sp.conflicting_core` as the requirement ids plus state ids, whatever `capture_io` says, since it is content-free. |
| Outcome row | `app/apps/models.py`, `app/apps/outcomes.py` | New nullable JSON column `routing_outcomes.conflicting_core` (the §3 object minus `label`/`hints`), written by the failure builder for `no_eligible` rows. **Needs an ALTER** (§7). |
| Simulator | `app/eval/simulate.py` | `ArmResult` gains `core: tuple[str, ...]` (requirement ids) and `core_state: tuple[str, ...]`. The summary's `new_rejections` gains `by_core: {"residency+requested_model": n, …}`, and `report.md`'s "New rejections" section groups by core. |

**Out of scope:**
- **The gate's pinned-model 422 (`model_not_found`).** It already returns a single named
  reason through `pinned_model_reason`.
- **Context-window 400s.** The gateway enforces these, not eligibility, the same limitation
  as P3a.
- **Capabilities the registry does not model yet,** such as vision.

## 5. Invariants and tests

1. **Covering.** Every available candidate violates at least one core requirement. This is a
   Hypothesis property over random pools and policies.
2. **Minimum.** No smaller set covers. The property test uses a brute-force oracle.
3. **Irreducible.** Removing any single core id leaves a candidate that violates nothing in
   the rest (Hypothesis).
4. **Agreement with the live seam.** For every candidate, `candidate_rejection(c)` is `None`
   exactly when `candidate_violations(c)` is empty, ignoring state. When not `None`, its code
   maps to one of the violated ids. This is a Hypothesis property over random pools,
   policies and contexts.
5. **Order independence.** Shuffling the pool gives an identical core.
6. **State is never in the core.** When disabled, circuit-open or prior-failure candidates
   make up the whole pool, the core is `[]` and the state lists them. An empty pool gives
   `state: [no_endpoints]`.
7. **Security hints never relax.** Every hint template for a `security: true` id is checked
   against a denylist: "remove", "disable", "relax", "lower", "allow cloud" and similar.
8. **Worked examples.**
   - `onprem_only` + a pinned cloud model gives `{residency, requested_model}`.
   - `allowed_regions: [eu]` + `require_no_retention` against endpoints that each satisfy
     only one of them gives `{allowed_regions, require_no_retention}`.
   - A budget case, where every in-floor candidate is over the cap and a cheaper candidate
     sits below the floor, gives `{max_cost_usd_per_request, risk_floor}`.
   - A sensitivity-rule escalation names the rule's entity types in `source`/`detail`.
9. **Live 422.** Through real HTTP, an `onprem_only` app with no on-prem model gives
   `error.conflicting_core.requirements[0].id == "residency"` and a B49-valid envelope.
   `code` stays `no_eligible_model`.
10. **Outcome and trace.** A `no_eligible` outcome row carries `conflicting_core`, and the
    export includes it.
11. **Simulator.** A proposal that removes the only model satisfying a pin gives
    `by_core == {"requested_model": n}` (or the real computed core). The report groups by
    core.
12. **Isolation and content-freeness.** No `detail` field ever holds request content: a
    test seeds a prompt with a unique marker and asserts it appears nowhere in the 422 core,
    the trace attribute or the outcome row.

## 6. Documentation

- `docs/reference/conflicting-core.md` (new): the requirement and state tables, the result
  object, the 422 example, the security rule on hints, and the budget semantics.
- `docs/reference/routing-outcomes.md`: the new column, and the ALTER in the Upgrade note.
- `docs/reference/policy-simulator.md`: `by_core` and the report grouping.
- `docs/research/feasible-route-bench.md` §16: the core as a minimum hitting set over the
  violation matrix, the invariants with their test names, and a worked Spark example.
- `CLAUDE.md`: one sentence in the routing-observability paragraph.

## 7. Deployment

The only schema change is the new nullable JSON column, and the ALTER must run **before** the
image rolls:

```sql
ALTER TABLE routing_outcomes ADD COLUMN IF NOT EXISTS conflicting_core JSON;
```

Everything else is code: the standard Spark deploy (backup, ALTER, fast-forward, rebuild).
After the deploy, trigger one real `onprem_only` + pinned-cloud 422 on a throwaway app,
confirm the body, the trace attribute and the outcome row, then delete the app.
