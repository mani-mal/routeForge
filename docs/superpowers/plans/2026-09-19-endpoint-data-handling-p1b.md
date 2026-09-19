# P1b — Endpoint Data-Handling Metadata — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let an operator declare where an endpoint runs, whether it retains prompts, and whether it may see raw sensitive data — and let an application require those properties, so an endpoint is excluded for a reason a compliance officer would recognise, named in the decision certificate.

**Architecture:** Three declarations ride on every endpoint (per-model value → provider default → `unknown`) through `ModelRegistry.effective_pool()`. Eligibility moves behind one seam — a `FeasibleSet` computed once from the pool, the policy context and the registry, carrying the eligible candidates *and* every rejection with its reason. The router and the gateway's failover consume that set instead of re-deriving it, which is what makes "a fallback cannot escape the eligible set" structural rather than aspirational.

**Tech Stack:** Python 3.12, FastAPI, Pydantic v2, SQLAlchemy (async), pytest, Hypothesis, React (console).

**Spec:** `docs/superpowers/specs/2026-09-19-endpoint-data-handling-p1b-design.md` — read it first; it wins any disagreement with this plan.

## Global Constraints

- **Branch:** `feat/endpoint-data-handling-p1b` (created; the spec is committed there as `45d2b5e`).
- **Run pytest as** `LITELLM_LOCAL_MODEL_COST_MAP=True timeout 900 uv run pytest <args>`. `pyproject` sets `addopts="-q"`, so add `-o addopts=""` when you want the count line. Do NOT run the full suite in a task unless the task says to.
- **Environment gaps, not your bugs:** `presidio_analyzer` is not installed (`tests/test_pii_detection_pipeline.py` has one known failure). An `rtk` shell hook mangles `grep` and `npx … 2>&1`; use `/usr/bin/grep` and `/usr/bin/npx`.
- **Nothing breaks on upgrade.** Every new column is nullable; `unknown` never satisfies a requirement and never blocks a request that imposes none. An app that sets neither new column, and an endpoint that declares nothing, must route byte-identically to today.
- **Declarations, not proofs.** RouteForge enforces what an operator declared about an endpoint; it cannot verify a provider's claim. No code comment, doc or UI string may imply verification.
- **Placement stays placement.** The new constraints may only NARROW the candidate set. No value of `allowed_regions` or `require_no_retention` may widen `allowed_providers`, clear `onprem_only`, or touch any P1a protection axis.
- **One authority.** After Task 3 there is exactly one place that decides whether a candidate is eligible. Adding a second is the defect this plan exists to prevent — the branch has already fixed three of them (residency, `content_integrity`, the console's rule copies).
- **Commit after every task**, message ending with:
  `Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>`
- **Do not touch** `docs/archive/`, `docs/new/`, or other `docs/superpowers/` files.

---

## A finding that shapes this plan

The spec (§4) says the exclusion rules apply "where the candidate pool is already filtered", i.e. `app/policy/context.py`. That is true but insufficient: `app/routing/router.py::select` **re-derives** eligibility from the pool on every call, and it only consults `ctx.allowed_models` when `ctx.model_scope_active` is true — which is false for any app that sets no `allowed_tiers`/`allowed_models`. Exclusions written only into `ctx.allowed_models` would therefore be silently ignored for exactly the apps most likely to rely on a regional requirement alone.

So Task 3 introduces the `FeasibleSet` seam **before** any new rule is added, as a pure refactor with no behavior change. Tasks 4 and 5 then add rules and fallback enforcement in one place. This is a larger change than the spec's wording implies, and it is the same "one authority" principle the branch has already applied three times.

---

## File Structure

**New**

| Path | Responsibility |
|---|---|
| `app/policy/feasible.py` | the `FeasibleSet` seam: eligible candidates + rejections with reasons |
| `tests/test_feasible_set.py` | the seam's unit tests |
| `tests/test_endpoint_data_handling.py` | the three rules, and the healthcare scenario |

**Modified**

| Path | Change |
|---|---|
| `app/apps/models.py` | 3 columns on `OnboardedModel` |
| `app/apps/registry.py` | `_OnboardedRow` + `effective_pool()` carry the declarations and their provenance |
| `app/config/schema.py` | `PoolCandidate` + `ProviderCfg` gain the three keys |
| `app/apps/policy.py`, `app/apps/store.py`, `app/api/admin_routes.py` | 2 columns on `AppPolicy`, + `PolicyIn`, `POLICY_FIELDS`, `version()` |
| `app/policy/compile.py` | resolve + validate the two app requirements |
| `app/policy/context.py` | `build_context` / `requestable_models` consume the seam |
| `app/routing/router.py` | `select` consumes the seam instead of re-deriving |
| `app/gateway/gateway.py` | failover and escalation draw from the seam |
| `app/obs/export.py` | `sp.endpoint_registry_version`, new reasons, provenance |
| `app/config/lint.py` | two new checks |
| `ui/src/admin/*`, `ui/src/console/RequestDrilldown.tsx` | onboarding fields, exclusion reasons |

---

### Task 1: Endpoints carry their declarations

**Files:**
- Modify: `app/apps/models.py` (`OnboardedModel`), `app/apps/registry.py` (`_OnboardedRow`, `refresh`, `effective_pool`), `app/config/schema.py` (`PoolCandidate`, `ProviderCfg`)
- Test: `tests/test_onboarded_store.py`, `tests/test_registry.py` (or the file your `ls tests | /usr/bin/grep -i registry` finds)

**Interfaces:**
- Produces, for every later task:
  ```python
  # on PoolCandidate, resolved (never None):
  region: str                     # lowercase token, or "unknown"
  prompt_retention: str           # "none" | "retained" | "unknown"
  accepts_raw_sensitive: str      # "true" | "false" | "unknown"
  data_handling_source: Mapping[str, str]   # field -> "model" | "provider_default" | "unknown"
  ```
  Resolution order per field: the per-model value, else the provider default from `serving.yaml`, else `unknown`. `effective_pool()` resolves it once so no consumer re-derives it.
- Consumes: nothing.

- [ ] **Step 1: Write the failing tests**

```python
def test_per_model_value_wins_over_provider_default():
    """An onboarded row that declares eu beats a provider default of us."""

def test_provider_default_applies_when_the_model_is_silent():

def test_everything_unset_resolves_to_unknown_with_unknown_provenance():
    """This is every endpoint that exists today — it must not become an error."""

def test_region_is_normalised_to_lowercase():
    """`US` and `us` are the same region; matching is exact after normalisation."""

def test_provenance_names_where_each_value_came_from():
```

- [ ] **Step 2: Run them and watch them fail**

```bash
LITELLM_LOCAL_MODEL_COST_MAP=True timeout 900 uv run pytest tests/test_onboarded_store.py -o addopts="" -q
```

- [ ] **Step 3: Implement**

Three nullable columns on `OnboardedModel` (`region` `String(64)`, `prompt_retention` `String(16)`, `accepts_raw_sensitive` `Boolean`), mirrored on `_OnboardedRow` and read in `refresh`. Three optional fields on `PoolCandidate` and on `ProviderCfg`. Resolve in `effective_pool()` — it is already the one place the YAML pool and the onboarded rows merge.

Comment the declaration/verification distinction where an implementer will read it: this records what the operator declared.

- [ ] **Step 4: Run the tests, then commit**

---

### Task 2: Applications carry their requirements

**Files:**
- Modify: `app/apps/models.py` (`AppPolicy`), `app/apps/store.py` (`POLICY_FIELDS`), `app/apps/policy.py` (`AppPolicyView`, `policy_view_from`, `version()`), `app/api/admin_routes.py` (`PolicyIn`, `_effective_dict`), `app/policy/compile.py` (resolution + validation)
- Test: `tests/test_app_policy.py`, `tests/test_policy_save_validation.py`, `tests/test_policy_golden_table.py`

**Interfaces:**
- Produces: `AppPolicyView.allowed_regions: list | None`, `.require_no_retention: bool | None`; compiled onto the effective policy as placement-adjacent constraints; both folded into `version()` **only when set**, so existing hashes are unchanged.
- Consumes: Task 1's resolved fields (for the lint warning only).

- [ ] **Step 1: Write the failing tests**

```python
def test_new_requirement_columns_default_to_none_and_do_not_change_the_version_hash():

def test_setting_a_requirement_changes_the_version_hash():

def test_allowed_regions_is_normalised_and_rejected_when_not_a_list_of_tokens():
    """A 422 naming the field, like every other axis (P1a error-code style)."""

def test_requirements_cannot_widen_anything():
    """No value of either column may add a provider or model that residency,
    the app allowlists or the registry had excluded."""
```

- [ ] **Step 2-4: Run, implement, re-run**

Follow P1a's patterns exactly: nullable columns, `PolicyIn` types, the "fold in only when set" hash, a `validate_effective` code for a malformed value, and the `effective` block so the console never re-derives. The golden table must still pass unchanged — these columns add rules, they do not alter any existing accept/reject.

- [ ] **Step 5: Commit**

---

### Task 3: The FeasibleSet seam (refactor, no behavior change)

This task adds no rule. It moves the eligibility decision into one place so Task 4 can add rules once.

**Files:**
- Create: `app/policy/feasible.py`, `tests/test_feasible_set.py`
- Modify: `app/policy/context.py` (`build_context`, `requestable_models`, `_in_app_scope`, `registry_permits`, `pinned_model_reason`), `app/routing/router.py` (`select`)
- Test: `tests/test_router.py`, `tests/test_derived_onprem_reject.py`, `tests/test_policy_mode_pipeline.py`, `tests/onboarding`

**Interfaces:**
- Produces:
  ```python
  @dataclass(frozen=True)
  class Rejection:
      name: str; provider: str; tier: str
      display_name: str | None
      reason: str            # the operator-facing string used today
      code: str              # stable: "residency" | "app_scope" | "not_requested" | "model_scope" | "prior_failure"

  @dataclass(frozen=True)
  class FeasibleSet:
      eligible: tuple[PoolCandidate, ...]
      rejected: tuple[Rejection, ...]

  def feasible_set(settings, registry, ctx_or_policy, *, tenant_id,
                   exclude: frozenset[str] = frozenset()) -> FeasibleSet: ...
  ```
- Consumes: Task 1's resolved candidates.

- [ ] **Step 1: Characterise today's behavior first**

Before refactoring, write `tests/test_feasible_set.py` asserting the CURRENT rejection reasons and eligible sets for: an `onprem_only` app with cloud enabled; an app with `allowed_tiers`; a pinned `requested_model`; a model scoped to another app via `allowed_app_ids`; a disabled on-prem row; and `exclude` after a prior failure. Take the expected strings from the current `router.py::select` and `context.py` — these are what must not change.

- [ ] **Step 2: Run them against the unrefactored code — they must PASS**

That is the point: they are the safety net, not a red-green cycle. If one fails, you have mis-read the current behavior; fix the test, not the code.

- [ ] **Step 3: Implement the seam and route both callers through it**

`feasible_set()` applies, in this order: residency/provider (`ctx.allowed_providers`), `exclude`, app scope (tiers/models), requested-model pin, and the registry gates (`is_allowed_for_app`, `is_enabled`). Reasons and their ordering must match what `router.py::select` produces today, because the console renders those strings.

Then: `build_context` derives `allowed_models` from `feasible_set(...).eligible`; `requestable_models` returns it; `router.select` consumes it instead of its own loop. **`model_scope_active` stops gating whether the eligible set is honoured** — that flag exists to tell the UI whether the app narrowed anything, not to decide enforcement.

- [ ] **Step 4: The characterisation tests must still pass, unchanged**

```bash
LITELLM_LOCAL_MODEL_COST_MAP=True timeout 900 uv run pytest \
  tests/test_feasible_set.py tests/test_router.py tests/test_derived_onprem_reject.py \
  tests/test_policy_mode_pipeline.py tests/onboarding -o addopts="" -q
```

If a test needs editing to pass, stop and report it — that is a behavior change this task is not allowed to make.

- [ ] **Step 5: Commit**

---

### Task 4: The three exclusion rules

**Files:**
- Modify: `app/policy/feasible.py`, `app/policy/context.py` (`pinned_model_reason`)
- Create: `tests/test_endpoint_data_handling.py`
- Test: also `tests/test_policy_compile.py`, `tests/test_derived_onprem_reject.py`

**Interfaces:**
- Produces three new `Rejection.code` values: `region_not_allowed`, `prompt_retention_not_allowed`, `raw_sensitive_not_accepted`, each with an operator-facing `reason` string naming the requirement and the declaration.
- Consumes: Tasks 1-3.

- [ ] **Step 1: Write the failing tests**

```python
def test_region_requirement_excludes_a_mismatched_endpoint():

def test_region_requirement_excludes_an_endpoint_that_declares_nothing():
    """unknown never satisfies a requirement."""

def test_an_app_with_no_requirements_is_unaffected_by_unknown_declarations():
    """Every endpoint in every deployment today. This is the upgrade-safety test."""

def test_retention_requirement_excludes_a_retaining_endpoint():

def test_raw_sensitive_rule_fires_only_for_raw_model_input_and_a_sensitive_request():
    """model_input: redact ⇒ the endpoint is eligible again (eligible after redaction)."""

def test_empty_feasible_set_is_a_reject_with_reasons_not_a_relaxation():

def test_pinned_model_reason_names_the_data_handling_rule_that_refused_it():
```

- [ ] **Step 2-4: Run, implement, re-run**

The raw-sensitive rule needs the compiled `model_input` and the request's sensitivity, so `feasible_set` takes them from the context it already receives. Keep the rule's condition in ONE expression — a second copy in the router or the gateway is precisely what Task 3 exists to prevent.

- [ ] **Step 5: Commit**

---

### Task 5: Fallback and escalation stay inside the set

**Files:**
- Modify: `app/gateway/gateway.py`
- Test: `tests/test_router.py`, `tests/test_lateral_switch.py` (or whatever `/usr/bin/grep -rln "exclude=" tests/` finds), `tests/test_endpoint_data_handling.py`

**Interfaces:**
- Consumes: `FeasibleSet`. Produces no new types.

- [ ] **Step 1: Write the failing test**

```python
def test_a_data_handling_excluded_endpoint_is_never_tried_on_failover():
    """The first-choice endpoint fails; the excluded one must not be the retry,
    even though it is healthy, cheaper and in the pool."""

def test_escalation_stays_inside_the_feasible_set():
```

- [ ] **Step 2-4: Run, implement, re-run.** The failover path re-selects with `exclude` — it must re-select from the same `FeasibleSet`, not from the pool.
- [ ] **Step 5: Commit**

---

### Task 6: Registry version and certificate evidence

**Files:**
- Modify: `app/apps/registry.py` (the hash), `app/obs/export.py`, `app/pipeline.py` (certificate fields)
- Test: `tests/test_decision_certificate.py`, `tests/test_candidate_export.py`

**Interfaces:**
- Produces `ModelRegistry.registry_version() -> str` (sha256[:12] over each candidate's identity + the three declarations, sorted), emitted as `sp.endpoint_registry_version` and in the response `policy` block.

- [ ] **Step 1: Write the failing tests** — the version changes when a declaration changes and is stable across boots for equal input; the new reason codes appear in `sp.candidates`; the certificate names each declaration's provenance; no raw value anywhere.
- [ ] **Step 2-4: Run, implement, re-run**
- [ ] **Step 5: Commit**

---

### Task 7: Config keys and lint

**Files:**
- Modify: `app/config/schema.py`, `app/config/lint.py`, `config/serving.yaml`, `config/routing.yaml`
- Test: `tests/test_config_lint.py`, `tests/test_config_loader.py`

- [ ] **Step 1: Write the failing tests** — a provider default with a value outside its enum fails the boot with a named violation; an app requiring a region no endpoint declares produces a WARNING, not a failure; a config with no new keys boots unchanged.
- [ ] **Step 2-4: Run, implement, re-run.** Add commented examples to both YAMLs showing the shape without changing shipped behavior.
- [ ] **Step 5: Commit**

---

### Task 8: Console

**Files:**
- Modify: the onboarding form (`/usr/bin/grep -rln "api_key_env" ui/src` finds it), `ui/src/console/RequestDrilldown.tsx`, `ui/src/api.ts`
- Gate: `cd ui && /usr/bin/npm run build`

- [ ] **Step 1: Add the three declarations to the onboarding form**, defaulting to `unknown`, with one line stating RouteForge enforces but cannot verify them.
- [ ] **Step 2: Render the new exclusion reasons** in the drill-down beside the existing ones, and show `endpoint_registry_version` where `app_policy_version` is already shown.
- [ ] **Step 3: No client-side rules.** Values come from the server. If you find yourself writing a condition that mirrors `feasible.py`, stop and use a server-supplied value instead.
- [ ] **Step 4: Build, then commit**

---

### Task 9: Migration, docs, the healthcare scenario, and the gate

**Files:**
- Modify: `scripts/migrate_app_policies.py`, `docs/reference/per-app-config.md`, `docs/guides/onboarding-models.md`, `docs/reference/routing-observability.md`, `CLAUDE.md` (the onboarding bullet), `.claude/skills/routing-and-gateway/SKILL.md`, `.claude/skills/inference-backends/SKILL.md`
- Test: `tests/test_endpoint_data_handling.py` (the scenario), `tests/test_docs_links.py`

- [ ] **Step 1: The five ALTER statements** — three on `onboarded_models`, two on `app_policies` — into the migration script and the "Manual DB migration" section, stating again that they run BEFORE the image rolls.
- [ ] **Step 2: Write the healthcare scenario end to end.** Four endpoints — a local model (raw permitted), a US cloud model (no retention, redaction required), an EU model, a retaining US model — an app requiring `us` + no-retention, and a request carrying an SSN. Assert: two survivors, and the two exclusions named as `region_not_allowed` and `prompt_retention_not_allowed` in the certificate. This is the spec's definition of done; it must assert at the HTTP boundary.
- [ ] **Step 3: Document the axes** — what each declaration means, the resolution order, that `unknown` never satisfies and never blocks, and the declarations-not-proofs statement in the operator's own words.
- [ ] **Step 4: The full gate** — paste real output:

```bash
LITELLM_LOCAL_MODEL_COST_MAP=True timeout 2400 uv run pytest tests -o addopts="" -q
LITELLM_LOCAL_MODEL_COST_MAP=True timeout 900 uv run pytest tests/onboarding -o addopts="" -q
cd ui && /usr/bin/npm run build
helm template deploy/helm/serving-plane >/dev/null && helm template deploy/hpe/routeforge >/dev/null
```

Expected failure: ONLY `tests/test_pii_detection_pipeline.py::test_engineering_labels_survive_gate_for_onprem_only_pii_off_app` (presidio_analyzer missing). Report anything else.

- [ ] **Step 5: Commit**

---

## Manual verification (after Task 9)

1. Apply the five `ALTER`s to a scratch database and boot; `/health` is 200.
2. Onboard an endpoint declaring `region: us`, `prompt_retention: none`; onboard a second declaring `region: eu`. Configure an app with `allowed_regions: ["us"]`.
3. Send a request: it routes to the US endpoint, and the trace names the EU one under `region_not_allowed`.
4. Change the US endpoint's declaration to `retained`, set `require_no_retention` on the app, and confirm the request is now **rejected with reasons** rather than routed anywhere.
5. Confirm an app with neither requirement still routes to both.

## Out of scope

Certifications, training-on-data and subprocessor metadata; a strict mode excluding incompletely-described endpoints; capability matching beyond today's router; Priority 3's minimal conflicting core; P2's evaluation plane.
