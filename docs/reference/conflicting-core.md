# The minimal conflicting core (P3b)

When the router's eligible set is empty, the 422 used to list each rejected candidate's
*first* failed stage — enough to say "no", not enough to say what would have to change. P3b
names the **minimal conflicting core**: the smallest set of policy requirements no
available endpoint satisfies together. Endpoint *state* (disabled, circuit open, a prior
failure on this request) is kept separate from policy, and every requirement carries a
remediation hint. A security requirement's hint never suggests relaxing it, and nothing is
ever applied automatically — an admin or operator still has to act.

Design: `docs/superpowers/specs/2026-09-25-conflicting-core-design.md`. Code:
`app/policy/core.py` (pure computation), `app/policy/feasible.py`
(`candidate_violations`/`violation_matrix`, shared with the first-fail seam),
`app/routing/router.py` (the two raise sites). Prior slices: P1b's `FeasibleSet`
(`app/policy/feasible.py`), the B49 error envelope (`app/api/openai_errors.py`), P2b-2
outcome rows (`docs/reference/routing-outcomes.md`), and the P3a simulator
(`docs/reference/policy-simulator.md`).

## 1. When it appears

Every `NoEligibleCandidate` the router raises carries a core (`app/errors.py`). There are
exactly two raise sites, both in `Router._select` (`app/routing/router.py`):

- **Policy-empty.** `feasible_set` left nothing eligible. The core is computed
  (`Router._policy_core`) over the whole **available** pool — every pool candidate that is
  not in a §3 state — via `app/policy/feasible.py::violation_matrix`.
- **Budget-empty.** The policy-eligible set was non-empty, but every candidate the budget
  stage could use is over the cap. The core is computed (`Router._budget_core`) over the
  §6 search space.

Computing the core can never change which exception is raised or let a request through:
`Router._safe_core` wraps both call sites in a `try`/`except`, logs at `ERROR` and falls
back to `core=None` on any failure (`tests/test_router_conflicting_core.py::test_core_failure_is_fail_safe`).
The core explains a rejection; it never decides one.

**Where it reaches the caller** — three surfaces, all content-free:

1. The 422 body: `error.conflicting_core` (`ConflictingCore.to_dict()`), and the same object
   on the keepalive stream's SSE error frame. Built by `app/api/routes.py::_error_extra`.
2. `routing_outcomes.conflicting_core` (`ConflictingCore.to_record()` — no `label`, no
   `hints`), on every `no_eligible` row. See `docs/reference/routing-outcomes.md`.
3. A structured `serving.api` log line,
   `{"event": "no_eligible_model", "request_id": "...", "conflicting_core": {...}}`
   (`app/api/routes.py::_log_no_eligible`). This is **not** a Langfuse trace attribute: a
   request that fails with no eligible candidate never reaches `_finalize`, so
   `export_trace` never runs and no trace exists for it to sit on. The log line, keyed on
   `request_id`, is the only place the core reaches anything trace-adjacent today.
   Since B57 (2026-09-26) the line actually reaches the process's stderr in deployment:
   `create_app()` calls `app/obs/log_config.py::configure_logging`, which attaches one
   stderr handler to the `serving` logger at `SP_LOG_LEVEL` (default `INFO`). Before that,
   nothing configured logging and every `serving.*` INFO record — this line included — was
   dropped. The line is printed as `<time> INFO serving.api {"event": "no_eligible_model", …}`.

## 2. The requirement table

A **requirement** is one operator-policy or request constraint, with a stable `id`, a
`security` flag, and a `source` naming where it came from. `app/policy/core.py::REQUIREMENT_ORDER`
fixes both the alphabet and the tie-break order for the minimum-hitting-set search.

| id | fires on a candidate when | security | source |
|---|---|---|---|
| `residency` | resolved residency is `onprem_only` and the provider is not on-prem | yes | `app_residency`, or `sensitivity_rule` (the rule's entity **types** only) |
| `allowed_providers` | the provider is outside the resolved placement set (app `allowed_providers`, enabled providers, on-prem availability) for a non-`onprem_only` residency | yes | `app_policy`, or `deployment` when the resolved set is empty AND the deployment's own placement capacity — ignoring the app's own `allowed_providers` entirely — is already empty (the `providers.*.enabled` kill switch or no healthy on-prem endpoint) |
| `allowed_regions` | stage-2 region rule: the app set `allowed_regions` and the endpoint's declared region is not in it (`unknown` excludes) | yes | `app_policy` |
| `require_no_retention` | stage-2 retention rule: the app requires no retention and the endpoint declares other than `none` (`unknown` excludes) | yes | `app_policy` |
| `raw_sensitive` | stage-2 raw-sensitive rule: the request sends unredacted sensitive content and the endpoint declares `accepts_raw_sensitive: false` (only a declared `false` fires this — `unknown` does **not** exclude, unlike the two rows above) | yes | `app_policy` |
| `allowed_tiers` | the app's `allowed_tiers` excludes the candidate's tier | no | `app_policy` |
| `allowed_models` | the app's `allowed_models` excludes the candidate | no | `app_policy` |
| `endpoint_app_allowlist` | the onboarded endpoint's `allowed_app_ids` excludes this app | no | `endpoint` |
| `requested_model` | the request pins a different model | no | `request` |
| `max_cost_usd_per_request` | the router's pre-call estimate for this candidate exceeds the cap (§6) | no | `app_policy` |
| `risk_floor` | the candidate's tier is below the effective risk floor (§6) | yes | `risk_class`, or `escalation` when a confidence-escalation `min_tier` — not the request's own risk class — is what set the effective floor (§6); the detail then also carries `min_tier` alongside `floor` |

`SECURITY_IDS` (`app/policy/core.py`) is exactly the six ids marked `security: true` above:
`residency`, `allowed_providers`, `allowed_regions`, `require_no_retention`, `raw_sensitive`,
`risk_floor`.

Every predicate is the same check `app/policy/feasible.py::candidate_rejection` (the
first-fail seam) already applies — `candidate_violations` walks the identical stages and
reports *every* one a candidate fails, not just the first, so the two can never drift
(`tests/test_policy_violations.py::test_agreement_with_candidate_rejection`, a Hypothesis
property). `max_cost_usd_per_request` and `risk_floor` belong to the router's own budget
stage, not this seam, and `app/policy/feasible.py` never produces them.

## 3. The state table

**State** is a fact about an endpoint or this request's history — never policy — and is
reported separately, never inside the core:

| id | meaning |
|---|---|
| `no_endpoints` | the effective pool is empty |
| `disabled` | the onboarded endpoint is disabled by the admin |
| `circuit_open` | the step-7 breaker currently holds the circuit open |
| `prior_failure` | the endpoint already failed on this request (lateral failover) |

Only candidates that are **not** in any of these states enter the violation matrix as
"available"; if no candidate is available the core is `[]` and `state` explains why
(`app/policy/core.py::build_core`, `tests/test_conflicting_core.py::test_state_only_core_is_empty`).
An empty pool with no disabled row reads `state: [{"id": "no_endpoints", "models": []}]`
— `no_endpoints` is the one state id that is still listed with an empty `models` list, so
its presence is visible even though it names nothing (`app/policy/core.py::build_core`).

A **disabled onboarded row never enters the pool at all** (`ModelRegistry.effective_pool()`
drops it) — it is reported for explanation only, via `ModelRegistry.disabled_onboarded()`,
filtered to the rows this app is allowed to use
(`app/policy/feasible.py::violation_matrix`). A disabled row scoped to a different app is
never named. `no_endpoints` fires only when the pool is empty **and** no such reportable
disabled row exists
(`tests/test_router_conflicting_core.py::test_a_disabled_row_restricted_to_another_app_is_not_reported`).

## 4. The result object

```json
{
  "requirements": [
    {"id": "requirement-id", "security": true, "source": "...",
     "detail": {"...": "..."},
     "label": "operator-facing sentence",
     "hints": ["what an operator or app developer could do"]}
  ],
  "state": [{"id": "disabled", "models": ["onboarded-slug", "..."]}],
  "candidates_considered": 3
}
```

- **`detail`** holds policy values only — residency, region tokens, tier names, model
  slugs, the budget cap/estimate, entity **types** — never request content or matched text
  (`tests/test_conflicting_core_api.py::test_conflicting_core_is_content_free`).
- **`hints`** come from a fixed template per id (§5).
- **An empty list/set detail renders as `(none)`**, in both the requirement's `label` (e.g.
  `"provider must be one of (none)"`) and `summary()`'s parenthetical (e.g. `"no endpoint
  satisfies all of: allowed_providers (none)"`) — never as an empty string, so an operator
  reading either never sees a dangling "must be one of " with nothing after it
  (`app/policy/core.py::_joined`).
- **`state`** groups the pool's own model slugs by state id, sorted.
- **`candidates_considered`** is the number of available candidates whose violation set
  fed the minimum-hitting-set search (§1's policy case, or the §6 budget search space). It
  never includes a state-only candidate (disabled, circuit-open, prior-failure, or an empty
  pool). For every context the real gate builds this is ordinarily every available
  candidate — each was rejected, so each violates something. The one exception is a
  hand-built context that refuses a candidate through `ctx.allowed_models` while carrying
  no `app_allowed_tiers`/`app_allowed_models` to name why; that candidate violates nothing
  the matrix can express, so `Router._policy_core` drops it from the count (logging a
  warning) rather than crash the 422 over it
  (`tests/test_router_conflicting_core.py::test_hand_built_context_with_an_unexplained_rejection_does_not_crash`).
- `to_record()` (used by the outcome row and the log line) is `to_dict()` with `label` and
  `hints` dropped from every requirement — content-free and hint-free, since an outcome row
  and a log line are not the place to render operator prose.

### A real 422

Captured from a live run through the real HTTP boundary (`TestClient` + `create_app()`,
same shape as `tests/test_conflicting_core_api.py`): an `onprem_only` app whose only
on-prem endpoint is disabled, with the cloud provider `azure` enabled so there is a
residency-violating candidate to explain (model slug renamed to avoid the docs vocabulary
gate's purged demo-model names — the shape and every other value are exactly as returned):

```json
{
  "error": {
    "conflicting_core": {
      "requirements": [
        {
          "id": "residency",
          "security": true,
          "source": "app_residency",
          "detail": {"value": "onprem_only"},
          "label": "must run on-premises",
          "hints": [
            "Onboard an on-premises endpoint that also satisfies the other requirements listed here."
          ]
        }
      ],
      "state": [
        {"id": "disabled", "models": ["onprem-mid-1"]}
      ],
      "candidates_considered": 1
    },
    "message": "no model is available for this request's policy: no candidates after residency filter: 1 excluded · residency (request or application is onprem_only: onboard and enable an on-prem model) [no endpoint satisfies all of: residency (onprem_only)]",
    "type": "invalid_request_error",
    "param": null,
    "code": "no_eligible_model"
  }
}
```

`candidates_considered: 1` is the one static cloud candidate (`azure-gpt-mid`) that
violates `residency`; the disabled on-prem row never enters that count (§3). The
`message` field is unchanged apart from the trailing `[...]` — `core.summary()` appended
once, after the existing first-fail explanation
(`tests/test_router_conflicting_core.py::test_message_keeps_the_old_text_and_appends_the_summary`).
`code` stays `no_eligible_model` — the core adds an explanation, never a new error kind.

## 5. The hint rule

Hints come from one fixed template dict keyed by requirement id
(`app/policy/core.py::_HINT_TEMPLATES`), never generated per request. The split is
absolute:

- **Security requirements** (`security: true`) — `residency`, `allowed_providers`,
  `allowed_regions`, `require_no_retention`, `raw_sensitive`, `risk_floor` — only ever get a
  hint that suggests **onboarding or approving an endpoint** that satisfies the
  requirement, e.g. *"Onboard an on-premises endpoint that also satisfies the other
  requirements listed here."* They never suggest relaxing, removing, disabling or
  otherwise loosening the requirement itself.
- **Every other requirement** (`allowed_tiers`, `allowed_models`,
  `endpoint_app_allowlist`, `requested_model`, `max_cost_usd_per_request`) may also suggest
  changing the app policy or the request, e.g. *"Send model \"auto\", or pin a model that
  satisfies the other requirements."* or *"Lower max_tokens or shorten the prompt, or ask
  an administrator to review max_cost_usd_per_request."*

`tests/test_conflicting_core.py::test_security_hints_never_relax` enforces this on every
security id's hint templates against a denylist ("remove", "disable", "relax", "lower",
"loosen", "drop", "allow cloud", "turn off", "waive", "bypass").

**Nothing here is ever applied automatically.** The core is read-only explanation; no code
path writes a policy, onboards a model, or changes a request in response to it. An admin or
operator always acts.

## 6. Budget semantics (§2.3)

The router raises a budget rejection only after the policy-eligible set is non-empty
(§1's policy case already succeeded). The candidates the budget core is computed over are
**not** the whole policy-eligible set — they are bounded by the tier the router actually
picked:

- The **effective floor** is `min(max(risk floor, escalation min_tier), best allowed
  tier)` — the same clamp the router's own tier targeting applies
  (`Router._budget_core`).
- The matrix is built from the policy-eligible, available candidates whose tier is **at or
  below the picked tier** — the router's real budget search space, plus the lower tiers it
  already passed over.
- **Candidates in tiers above the picked tier are excluded from the matrix entirely** —
  the router's budget stage never considered them, and the core describes what the router
  actually did, not a hypothetical wider search
  (`tests/test_router_conflicting_core.py::test_budget_floor_over_cap_and_cheap_below_floor`
  pins this: a `frontier`-tier candidate the router never looked at does not appear).
- Within that set, a candidate violates `max_cost_usd_per_request` when its estimate
  exceeds the cap, and `risk_floor` when its tier is below the effective floor. A candidate
  that violates neither is impossible by construction — if one existed, the router's own
  budget fallback would have picked it — and `_budget_core` raises an internal
  `AssertionError` rather than silently building a wrong core if that invariant is ever
  broken (`tests/test_router_conflicting_core.py::test_budget_core_asserts_on_a_candidate_violating_neither`).
- `max_cost_usd_per_request`'s `detail.cheapest_estimate` is the cheapest estimate **among
  the candidates that violate the cost cap** — never a below-floor candidate that happens
  to fit the cap, which would misleadingly read as "under the cap" next to "violates cost".
- Open circuits never enter a budget core: the within-tier breaker bypass
  (`app/routing/router.py::select`) handles them, so state is always `{}` for this raise
  site.

## 7. Known limits — what the shipped build does not (yet) do exactly

- **`max_cost_usd_per_request`'s `source` always reads `app_policy`**, even on a deployment
  installed at the deployment ceiling rather than set per-app. `app/policy/core.py::DEFAULT_SOURCE`
  has no `"deployment"` value for this id, unlike `allowed_providers` (§2), which does
  distinguish the deployment-emptied case.
- **`cheapest_estimate` is the cheapest among the candidates that violate the cap** (§6),
  not the cheapest candidate overall.

## 8. What is not covered

- **The gate's pinned-model 422** (`model_not_found`). A client pin the app's placement or
  scope forbids is refused by the pipeline's own pinned-model check, before the router
  runs — that 422 already carries a single named reason
  (`app/policy/context.py::pinned_model_reason`), not a `NoEligibleCandidate`, so it has no
  `conflicting_core` at all
  (`tests/test_conflicting_core_api.py::test_pinned_model_rejection_carries_no_core`).
- **Context-window 400s.** The gateway enforces the model's context window as a backend
  bad-request, not an eligibility filter — the same limitation P3a's simulator has. No core
  is attached to a `context_length_exceeded` response.
- **Capabilities the endpoint registry does not model**, such as vision or tool-calling
  support. There is no such requirement id; a candidate that cannot actually serve a
  multimodal request is not distinguished from one that can by anything in this core.
