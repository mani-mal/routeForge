# P3b Minimal Conflicting Core Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** When no endpoint is eligible, name the smallest set of requirements that no
available endpoint meets together. Keep endpoint state separate, and attach remediation hints
that never relax a security requirement. The core appears in the live 422, the trace, the
outcome row and the P3a simulator.

**Architecture:**
- Each `feasible_set` stage check becomes a named predicate, shared by the existing
  first-fail `candidate_rejection` and a new `candidate_violations`. A test proves the two
  agree.
- A pure module `app/policy/core.py` computes the minimum hitting set over the violation
  matrix and renders labels and hints.
- The router computes the core at its two `NoEligibleCandidate` raise sites: policy-empty and
  budget-empty.
- The API, the outcome row, the trace and the simulator surface the core.

**Tech Stack:** Python 3.12, FastAPI, Pydantic v2, SQLAlchemy, Hypothesis, pytest.

**Spec:** `docs/superpowers/specs/2026-09-25-conflicting-core-design.md`

## Global Constraints

- **Requirement ids, fixed order** (the tie-break order, spec §2.1): `residency`,
  `allowed_providers`, `allowed_regions`, `require_no_retention`, `raw_sensitive`,
  `allowed_tiers`, `allowed_models`, `endpoint_app_allowlist`, `requested_model`,
  `max_cost_usd_per_request`, `risk_floor`.
- **Security ids:** `residency`, `allowed_providers`, `allowed_regions`,
  `require_no_retention`, `raw_sensitive`, `risk_floor`.
- **State ids** (never in a core): `no_endpoints`, `disabled`, `circuit_open`,
  `prior_failure`.
- **Core definition:** the minimum-size set of ids that meets every available candidate's
  violation set. Ties go to the lexicographically first set by the fixed order. The search is
  exact, over subsets in increasing size.
- **Hints:** security-id hints only suggest adding or approving an endpoint. They never
  contain relaxation wording (the denylist test in Task 2). Nothing is ever applied
  automatically.
- **Content-free:** `detail` holds policy values only (residency, regions, tiers, model
  slugs, the cap and estimate, rule entity types). It never holds request content or matched
  text.
- **The existing behaviour of `candidate_rejection` and `feasible_set` does not change:**
  same codes, reasons and order.
- **The 422 keeps `code: "no_eligible_model"`** and a B49-valid envelope. The core is
  `error.conflicting_core`.
- **Outcome column:** `routing_outcomes.conflicting_core` is nullable JSON. The deploy ALTER
  is `ALTER TABLE routing_outcomes ADD COLUMN IF NOT EXISTS conflicting_core JSON;`.
- **Serving never imports `app.eval`.**
- **Tests:** run with
  `LITELLM_LOCAL_MODEL_COST_MAP=True /home/manimal/projects/routeForge/.venv/bin/python -m pytest <paths> -p no:warnings`
  from the worktree root. Do not add `-q`: `addopts` already has it, and `-qq` hides the
  summary line. There is no pytest-asyncio.
- **Commits** end with `Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>`.

## File map

| File | Status | Responsibility |
|---|---|---|
| `app/policy/feasible.py` | modify | Named stage predicates; `candidate_violations`; `violation_matrix` |
| `app/policy/core.py` | create | Requirement table, minimum hitting set, `ConflictingCore`, labels/hints |
| `app/errors.py` | modify | `NoEligibleCandidate(..., core=None)` |
| `app/routing/router.py` | modify | Compute the core at both raise sites |
| `app/api/openai_errors.py`, `app/api/routes.py` | modify | `error.conflicting_core` in the 422 |
| `app/apps/models.py`, `app/apps/outcomes.py`, `app/api/routes.py` | modify | Outcome column `conflicting_core` |
| trace export (`app/obs/export.py` or the failure log path) | modify | `sp.conflicting_core` |
| `app/eval/simulate.py` | modify | `ArmResult.core/core_state`, `by_core`, report grouping |
| `tests/policy/test_violations.py`, `tests/policy/test_core.py`, `tests/test_conflicting_core_api.py`, `tests/eval/test_simulate.py` | create/extend | Tests |
| Docs (Task 6) | create/modify | Reference, research §16, outcomes/simulator refs, CLAUDE.md |

Before creating `tests/policy/`, check whether it exists. If there is no `tests/policy/`
package, put the files in `tests/`, named `test_policy_violations.py` and
`test_conflicting_core.py`, following the repo's flat test layout.

---

### Task 1: Violation predicates and `candidate_violations`

**Files:**
- Modify: `app/policy/feasible.py`
- Test: `tests/policy/test_violations.py` (or `tests/test_policy_violations.py`)

**Interfaces:**
- Produces, in `app/policy/feasible.py`:

```python
STATE_NO_ENDPOINTS = "no_endpoints"
STATE_DISABLED = "disabled"
STATE_CIRCUIT_OPEN = "circuit_open"
STATE_PRIOR_FAILURE = "prior_failure"

def candidate_violations(c: PoolCandidate, *, allowed_providers: frozenset[str] | set[str],
                         onprem_only: bool, scope: "Scope", requested_model: str | None,
                         registry: "ModelRegistry | None", tenant_id: str,
                         data_handling: DataHandling) -> frozenset[str]
    # every REQUIREMENT id (spec §2.1, except the two budget ids) this candidate violates;
    # never a state id

def candidate_state(c: PoolCandidate, *, exclude: frozenset[str],
                    registry: "ModelRegistry | None",
                    open_circuits: frozenset[str]) -> str | None
    # the first state id that applies (disabled, then circuit_open, then prior_failure), or None

@dataclass(frozen=True)
class ViolationMatrix:
    available: dict[str, frozenset[str]]     # name -> violated requirement ids (available candidates only)
    state: dict[str, tuple[str, ...]]        # state id -> sorted model names
    candidates: dict[str, PoolCandidate]     # every pool candidate by name
    onprem_only: bool
    allowed_providers: frozenset[str]

def violation_matrix(settings: Settings, registry: "ModelRegistry | None",
                     ctx: ExecutionPolicyContext, *, tenant_id: str,
                     exclude: frozenset[str] = frozenset(),
                     open_circuits: frozenset[str] = frozenset()) -> ViolationMatrix
```

**Rules:**
- **`Scope`** is a small frozen dataclass: `allowed_tiers: frozenset[str] | None` and
  `allowed_models: frozenset[str] | None`. `None` means no constraint.
  - Build it from the context: read how `_ctx_scope` decides scope today, including
    `ctx.model_scope_active` and whatever fields it reads.
  - Build it from a policy view the same way `_policy_scope` does.
  - Then rewrite `_ctx_scope` / `_policy_scope` to call two predicates, `tier_in_scope(c,
    scope)` and `model_in_scope(c, scope)`, so the existing `in_scope` callable becomes
    `tier_in_scope and model_in_scope`.
  - **Behaviour must be identical.** If `_ctx_scope` has semantics that don't split cleanly
    into tiers vs models (for example a precomputed `ctx.allowed_models` set), keep that
    semantics and document in `Scope` exactly how the split maps. The Hypothesis agreement
    test below is the proof.
- **Stage 1 split:**
  - if `onprem_only` and `c.provider != "onprem"`: `residency`;
  - otherwise, if `c.provider not in allowed_providers`: `allowed_providers`.

  `allowed_providers` must be resolved exactly as `feasible_set` resolves `providers`,
  including the B52 intersection. Extract that resolution into a private helper,
  `_resolve_placement(...) -> tuple[frozenset[str], bool]`, used by `feasible_set` and by
  `violation_matrix`.
- **Stage 2:** `_data_handling_rejection` returns the first rule. Add
  `_data_handling_violations(c, dh) -> frozenset[str]`, which returns every one of
  `allowed_regions`, `require_no_retention` and `raw_sensitive` that fires. The existing
  codes are `region_not_allowed`, `prompt_retention_not_allowed` and
  `raw_sensitive_not_accepted`; map each to its requirement id. Refactor so both functions
  use the same three per-rule predicates.
- **The other stages:**
  - `requested_model is not None and c.name != requested_model`: `requested_model`.
  - `registry is not None and not registry.is_allowed_for_app(c.name, tenant_id)`:
    `endpoint_app_allowlist`.
  - Disabled (onprem and not `registry.is_enabled`), open circuit and prior failure are
    **state**, handled in `candidate_state`. They never appear in `candidate_violations`.
- **`candidate_rejection` keeps its exact order, codes and reasons,** now calling the shared
  predicates.
- **`violation_matrix`:**
  - `candidates` = every pool candidate.
  - `state[s]` = names whose `candidate_state` is `s`.
  - `available[name]` = `candidate_violations` for every candidate with no state.
  - An empty pool gives `state == {"no_endpoints": ()}` and `available == {}`.
  - It reads `DataHandling.from_context(ctx)`, `ctx.requested_model`, and the scope from the
    context. Use the same inputs `feasible_set` uses for a context caller.

- [ ] **Step 1: Write the failing tests**

```python
"""P3b Task 1: candidate_violations agrees with the first-fail seam (spec §5.4)."""
from __future__ import annotations

from hypothesis import given, settings as hsettings, strategies as st

from app.policy.feasible import (candidate_rejection, candidate_violations,
                                 violation_matrix)

# Build candidates/contexts with the SAME helpers the existing feasible_set tests use --
# read tests that call feasible_set/candidate_rejection (grep "candidate_rejection(" and
# "feasible_set(" under tests/) and reuse their fixture builders rather than inventing new
# ones. The properties below are the requirement; adapt construction to the real builders.

CODE_TO_IDS = {
    "residency": {"residency", "allowed_providers"},
    "region_not_allowed": {"allowed_regions"},
    "prompt_retention_not_allowed": {"require_no_retention"},
    "raw_sensitive_not_accepted": {"raw_sensitive"},
    "app_scope": {"allowed_tiers", "allowed_models"},
    "not_requested": {"requested_model"},
    "model_scope": {"endpoint_app_allowlist"},   # "disabled" model_scope is STATE, see below
}


def test_residency_vs_allowed_providers_split():
    """onprem_only + cloud candidate -> 'residency'; non-onprem_only residency with the
    provider outside allowed_providers -> 'allowed_providers'."""


def test_all_three_data_handling_rules_reported_together():
    """An endpoint failing region AND retention AND raw-sensitive reports all three ids,
    while candidate_rejection still reports only the first code (region_not_allowed)."""


def test_state_is_not_a_violation():
    """A disabled onprem endpoint, an open circuit, a prior failure: candidate_violations
    ignores them; violation_matrix lists them under state and not in available."""


def test_empty_pool_state():
    """Empty effective pool -> state == {'no_endpoints': ()} and available == {}."""


@hsettings(max_examples=200, deadline=None)
@given(...)  # random pool (providers onprem/azure, tiers, declarations), random app policy
             # (residency, allowed_tiers/models/providers, regions, no-retention), random pin
def test_agreement_with_candidate_rejection(...):
    """For every candidate with no state: candidate_rejection is None  <=>  violations empty;
    when not None, CODE_TO_IDS[code] intersects violations."""
```

The four named tests must have real bodies built from the repo's existing fixture helpers.
The docstrings state exactly what each asserts. The Hypothesis strategy must vary:
- residency across `any`, `private_cloud_ok` and `onprem_only`;
- at least two providers;
- region, retention and raw-sensitive declarations, including `unknown`;
- allowed_tiers and allowed_models (null or a subset);
- the pin (null or a pool name).

- [ ] **Step 2: Run the tests to verify they fail** (ImportError on `candidate_violations`).
- [ ] **Step 3: Implement** per the rules above.
- [ ] **Step 4: Run** the new tests, then the whole existing feasible-set / router / gate
  suites. To find them, grep `tests/` for `feasible_set`, `candidate_rejection` and
  `Router(`. Expected: PASS, with no existing test changed.
- [ ] **Step 5: Commit** `refactor(policy): shared stage predicates; candidate_violations and violation_matrix (P3b)`.

---

### Task 2: `app/policy/core.py`, the minimum hitting set, labels and hints

**Files:**
- Create: `app/policy/core.py`
- Test: `tests/policy/test_core.py` (or `tests/test_conflicting_core.py`)

**Interfaces:**
- Consumes: nothing from Task 1 at runtime (pure). It uses the state id strings.
- Produces:

```python
REQUIREMENT_ORDER: tuple[str, ...]      # the 11 ids, spec §2.1 order
SECURITY_IDS: frozenset[str]
STATE_ORDER: tuple[str, ...] = ("no_endpoints", "disabled", "circuit_open", "prior_failure")

@dataclass(frozen=True)
class Requirement:
    id: str
    security: bool
    source: str
    detail: dict            # JSON-safe policy values only
    label: str
    hints: tuple[str, ...]

@dataclass(frozen=True)
class ConflictingCore:
    requirements: tuple[Requirement, ...]
    state: tuple[tuple[str, tuple[str, ...]], ...]   # ((state_id, (models...)), ...) in STATE_ORDER
    candidates_considered: int
    def ids(self) -> tuple[str, ...]
    def state_ids(self) -> tuple[str, ...]
    def to_dict(self) -> dict          # spec §3 object (requirements with label/hints; state as [{"id","models"}])
    def to_record(self) -> dict        # to_dict minus label/hints -- the outcome-row / trace shape
    def summary(self) -> str           # one line, e.g. "no endpoint satisfies all of: residency (onprem_only), requested_model (azure-gpt-mid)"

def minimum_hitting_set(violations: Iterable[frozenset[str]]) -> tuple[str, ...]
    # ids in REQUIREMENT_ORDER; () when there are no violation sets
    # raises ValueError if any set is empty (a candidate violating nothing means the set was not empty)

def build_core(violations: Mapping[str, frozenset[str]], *,
               state: Mapping[str, Sequence[str]],
               details: Mapping[str, dict], sources: Mapping[str, str]) -> ConflictingCore
```

**Rules:**
- **`minimum_hitting_set`:** collect the union U of all ids, ordered by `REQUIREMENT_ORDER`.
  An unknown id raises `ValueError`. For k = 1..len(U), iterate
  `itertools.combinations(U_ordered, k)` and return the first combination that intersects
  every set. Combinations of an ordered list come out in lexicographic order, so the tie-break
  is automatic.
- **`build_core`:**
  - The core ids come from `minimum_hitting_set(violations.values())`.
  - Each `Requirement` takes `detail = details.get(id, {})`, `source = sources.get(id,
    DEFAULT_SOURCE[id])`, and a label and hints from templates.
  - `state` keeps only non-empty entries, plus `no_endpoints` even when empty, in
    `STATE_ORDER`, with models sorted.
  - `candidates_considered = len(violations)`.
- **Default sources:**
  - `residency`: `app_residency`;
  - `allowed_providers`: `app_policy`;
  - `allowed_regions`, `require_no_retention`, `raw_sensitive`, `allowed_tiers`,
    `allowed_models`: `app_policy`;
  - `endpoint_app_allowlist`: `endpoint`;
  - `requested_model`: `request`;
  - `max_cost_usd_per_request`: `app_policy`;
  - `risk_floor`: `risk_class`.
- **Label templates** (`str.format(**detail)`, with safe fallback text when a detail key is
  missing):
  - `residency`: "must run on-premises" plus " (raised by sensitivity rule on {entities})"
    when `source == "sensitivity_rule"`;
  - `allowed_providers`: "provider must be one of {providers}";
  - `allowed_regions`: "endpoint region must be one of {regions}";
  - `require_no_retention`: "endpoint must declare no prompt retention";
  - `raw_sensitive`: "endpoint must accept raw sensitive content (the app sends it unredacted)";
  - `allowed_tiers`: "tier must be one of {tiers}";
  - `allowed_models`: "model must be one of {models}";
  - `endpoint_app_allowlist`: "endpoint must allow this application";
  - `requested_model`: "request pins model {value}";
  - `max_cost_usd_per_request`: "estimated cost must be at most ${cap:.4f} per request";
  - `risk_floor`: "tier must be at least {floor} for risk class {risk_class}".
- **Hint templates.** Security ids only suggest adding or approving an endpoint:
  - `residency`: "Onboard an on-premises endpoint that also satisfies the other requirements listed here."
  - `allowed_providers`: "Onboard or approve an endpoint from an allowed provider that also satisfies the other requirements listed here."
  - `allowed_regions`: "Onboard or approve an endpoint declared in an allowed region."
  - `require_no_retention`: "Onboard or approve an endpoint that declares no prompt retention."
  - `raw_sensitive`: "Onboard or approve an endpoint declared to accept raw sensitive content."
  - `risk_floor`: "Onboard an endpoint at or above the required tier that fits the other requirements."

  Non-security ids may also suggest a policy or request change:
  - `allowed_tiers`: "Onboard an endpoint in an allowed tier, or ask an administrator to review the application's allowed tiers."
  - `allowed_models`: "Onboard or permit an allowed model, or ask an administrator to review the application's allowed models."
  - `endpoint_app_allowlist`: "Ask an administrator to allow this application on a suitable endpoint."
  - `requested_model`: "Send model \"auto\", or pin a model that satisfies the other requirements."
  - `max_cost_usd_per_request`: "Lower max_tokens or shorten the prompt, or ask an administrator to review max_cost_usd_per_request."
- **Relaxation denylist for security hints** (case-insensitive substrings): "remove", "disable",
  "relax", "lower", "loosen", "drop", "allow cloud", "turn off", "waive", "bypass".

- [ ] **Step 1: Write the failing tests**

```python
"""P3b Task 2: minimum hitting set + ConflictingCore (spec §2.4, §3, §5)."""
from __future__ import annotations

import itertools
import random

import pytest
from hypothesis import given, settings as hsettings, strategies as st

from app.policy.core import (REQUIREMENT_ORDER, SECURITY_IDS, build_core,
                             minimum_hitting_set)

IDS = st.sampled_from(REQUIREMENT_ORDER)
VSETS = st.lists(st.frozensets(IDS, min_size=1, max_size=4), min_size=1, max_size=8)


def _covers(core, vsets):
    return all(set(core) & v for v in vsets)


def _brute_min(vsets):
    u = [i for i in REQUIREMENT_ORDER if any(i in v for v in vsets)]
    for k in range(1, len(u) + 1):
        for combo in itertools.combinations(u, k):
            if _covers(combo, vsets):
                return combo


@hsettings(max_examples=300, deadline=None)
@given(VSETS)
def test_covering_minimum_irreducible(vsets):                      # §5.1-5.3
    core = minimum_hitting_set(vsets)
    assert _covers(core, vsets)
    assert len(core) == len(_brute_min(vsets))
    for drop in core:
        rest = [i for i in core if i != drop]
        assert not _covers(rest, vsets)


@hsettings(max_examples=100, deadline=None)
@given(VSETS, st.randoms())
def test_order_independent(vsets, rnd):                             # §5.5
    shuffled = list(vsets)
    rnd.shuffle(shuffled)
    assert minimum_hitting_set(vsets) == minimum_hitting_set(shuffled)


def test_empty_violation_set_raises():
    with pytest.raises(ValueError):
        minimum_hitting_set([frozenset({"residency"}), frozenset()])


def test_worked_example_onprem_pin():                                # §5.8
    vsets = [frozenset({"requested_model"}), frozenset({"residency"})]
    core = build_core({"local-8b": vsets[0], "azure-gpt-mid": vsets[1]},
                      state={}, details={"requested_model": {"value": "azure-gpt-mid"},
                                         "residency": {"value": "onprem_only"}},
                      sources={})
    assert core.ids() == ("residency", "requested_model")
    assert [r.security for r in core.requirements] == [True, False]
    assert "azure-gpt-mid" in core.summary()


def test_state_only_core_is_empty():                                 # §5.6
    core = build_core({}, state={"disabled": ["b", "a"], "circuit_open": []},
                      details={}, sources={})
    assert core.ids() == () and core.state == (("disabled", ("a", "b")),)


def test_sensitivity_rule_source_in_label():
    core = build_core({"x": frozenset({"residency"})}, state={},
                      details={"residency": {"value": "onprem_only", "entities": ["US_SSN"]}},
                      sources={"residency": "sensitivity_rule"})
    assert "US_SSN" in core.requirements[0].label


def test_security_hints_never_relax():                               # §5.7
    deny = ("remove", "disable", "relax", "lower", "loosen", "drop", "allow cloud",
            "turn off", "waive", "bypass")
    for rid in SECURITY_IDS:
        core = build_core({"x": frozenset({rid})}, state={}, details={}, sources={})
        for hint in core.requirements[0].hints:
            assert not any(d in hint.lower() for d in deny), (rid, hint)


def test_to_record_drops_label_and_hints():
    core = build_core({"x": frozenset({"residency"})}, state={}, details={}, sources={})
    rec = core.to_record()
    assert "label" not in rec["requirements"][0] and "hints" not in rec["requirements"][0]
```

- [ ] **Step 2: Run to verify failure.**
- [ ] **Step 3: Implement** `app/policy/core.py`. Its module docstring states the model
  (minimum hitting set over the violation matrix), that state is kept separate, and the
  security-hint rule.
- [ ] **Step 4: Run.** Expected: PASS.
- [ ] **Step 5: Commit** `feat(policy): minimal conflicting core (minimum hitting set, labels, hints) (P3b)`.

---

### Task 3: The router computes the core at both raise sites

**Files:**
- Modify: `app/errors.py`, `app/routing/router.py`
- Test: `tests/policy/test_router_core.py` (or `tests/test_router_conflicting_core.py`)

**Interfaces:**
- Consumes: `violation_matrix`, `ViolationMatrix` (Task 1); `build_core`, `ConflictingCore`
  (Task 2).
- Produces: `NoEligibleCandidate(*args, rejected: tuple = (), core: "ConflictingCore | None" = None)`
  with the `.core` attribute. Every `NoEligibleCandidate` the Router raises carries a core.

**Rules:**
- **`app/errors.py`:** add the keyword-only `core=None`, stored as `self.core`. Existing
  constructions are unchanged.
- **Policy-empty raise** (`if not allowed:` in `Router._select`):
  - Compute `m = violation_matrix(self._s, self._reg, ctx, tenant_id=ctx.tenant_id,
    exclude=exclude, open_circuits=frozenset())`. The router's first `feasible_set` passes no
    open circuits: the breaker never makes the policy set empty, so circuits are not state
    here.
  - Then call `build_core(m.available, state=m.state, details=..., sources=...)`.
  - The empty-pool branch uses the same call; the matrix yields `no_endpoints`.
- **Details and sources** (content-free):
  - `residency`: `{"value": "onprem_only"}`. The source is `"sensitivity_rule"` when the app's
    own compiled residency is not `onprem_only` but `ctx.onprem_only` is true (escalated). In
    that case add `"entities": sorted(ctx.detected_entities)`, which is the entity **types**
    (confirm that `ctx.detected_entities` holds types only; `outcome_context` treats it that
    way). Otherwise the source is `"app_residency"`.
    - To learn the app's own residency, find what `ctx` carries: `ctx.residency`, the policy
      version, or a compiled placement. If the context has no direct field, add a
      content-free `ctx` field **only if** it is set in `build_context`, is immutable, and
      carries only the residency token. Record that in the report as a decision.
  - `allowed_providers`: `{"providers": sorted(m.allowed_providers)}`.
  - `allowed_regions`: `{"regions": sorted(ctx's compiled allowed_regions)}`.
  - `allowed_tiers` / `allowed_models`: the scope's sets, sorted.
  - `requested_model`: `{"value": ctx.requested_model}`.
- **Budget-empty raise** (spec §2.3):
  - Candidates = `allowed` (the policy-eligible, available set the router already has) whose
    tier order is ≤ that of the picked tier, i.e. the `tier_pick` before the fallback loop
    changed it. Keep the original pick in a local variable.
  - Violations per candidate:
    - `max_cost_usd_per_request` when `self._est_cost(c, tokens_after, out_tokens) >
      ctx.max_cost_usd`;
    - `risk_floor` when `_ORDER[c.tier] < _ORDER[effective_floor]`, where `effective_floor`
      is the lower (by `_ORDER`) of the floor tier the fallback already computes and the best
      allowed tier.
  - Details:
    - `max_cost_usd_per_request`: `{"cap": ctx.max_cost_usd, "cheapest_estimate":
      <min est over the matrix candidates>}`;
    - `risk_floor`: `{"floor": effective_floor, "risk_class": ctx.risk_class}`.
  - State: empty. (Open circuits never block budget; the within-tier bypass handles them.)
  - A candidate that violates neither cannot exist when the budget raise fires. Assert this
    with a clear `AssertionError` message, never silently.
- **The message text is unchanged,** except for `f" [{core.summary()}]"` appended to both
  messages.

- [ ] **Step 1: Write the failing tests.** Build a Router the way existing router tests do
  (grep `Router(` in `tests/`). Cover:
  1. `onprem_only` app, only a cloud model plus an onprem model not matching the pin:
     `exc.core.ids() == ("residency", "requested_model")`.
  2. `allowed_regions=["eu"]` + `require_no_retention=True`, with endpoint A in `eu` that
     retains and endpoint B that doesn't retain but is in `us`:
     `("allowed_regions", "require_no_retention")`.
  3. Budget: a risk-class-high request, where the floor-tier candidates are all over the cap
     and a cheap below-floor candidate fits: `("max_cost_usd_per_request", "risk_floor")`.
  4. Budget with no below-floor candidates: `("max_cost_usd_per_request",)`.
  5. Empty pool: `ids() == ()`, `state_ids() == ("no_endpoints",)`.
  6. Only a disabled onprem model: `ids() == ()`, state `disabled`.
  7. A sensitivity-rule escalation (the app residency is `any`, a `US_SSN` rule raises it to
     `onprem_only`, only cloud models): the `residency` source is `sensitivity_rule` and
     `detail["entities"] == ["US_SSN"]`.
  8. The message still starts with the old text; `core.summary()` is appended.
- [ ] **Step 2: Run to verify failure.**
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** the new tests plus every router, gateway and pipeline test file. Expected:
  PASS.
- [ ] **Step 5: Commit** `feat(routing): attach the minimal conflicting core to NoEligibleCandidate (P3b)`.

---

### Task 4: Surfaces: the 422 body, the outcome row and the trace

**Files:**
- Modify: `app/api/openai_errors.py`, `app/api/routes.py`, `app/apps/models.py`,
  `app/apps/outcomes.py`, and the trace/export path (see the Rules)
- Test: `tests/test_conflicting_core_api.py`

**Interfaces:**
- Consumes: `NoEligibleCandidate.core` (Task 3); `ConflictingCore.to_dict()` /
  `to_record()` (Task 2).
- Produces:
  - `error_payload(status, message, *, code=None, type=None, extra: dict | None = None)`,
    which merges `extra` into the inner `error` object;
  - the column `RoutingOutcome.conflicting_core` (JSON, nullable);
  - the `build_failure_outcome(..., conflicting_core: dict | None = None)` parameter.

**Rules:**
- **422:** where `routes.py` maps `NoEligibleCandidate` to `(422, msg, CODE_NO_ELIGIBLE_MODEL)`
  (~line 255), carry the core so the envelope body gets `error.conflicting_core =
  exc.core.to_dict()` when `exc.core` is not None. Follow how the status/message/code tuple
  flows into `error_payload` (read the call sites) and thread an optional `extra` through the
  smallest change. The streaming paths that return an error envelope must include it too;
  grep `CODE_NO_ELIGIBLE_MODEL` and every `error_payload(` call.
- **Outcome row:**
  - Add `conflicting_core: Mapped[dict | None] = mapped_column(JSON, nullable=True)` to
    `RoutingOutcome`.
  - `build_failure_outcome` sets it from the parameter.
  - `_record_failure_outcome` in `routes.py` (~line 487) passes
    `conflicting_core=exc.core.to_record() if getattr(exc, "core", None) else None`.
  - `build_ok_outcome` sets `None`.
  - Existing tests that assert exact row keys must be updated to include the new key; grep
    `tests/` for them.
- **Trace:** find whether a `no_eligible` request exports a trace or DecisionRecord today.
  Grep `app/obs/export.py` and `app/pipeline.py` for how a failed run is traced, and check
  `routes.py` for a failure trace export.
  - **If it does,** add `sp.conflicting_core` = `json.dumps(core.to_record(),
    sort_keys=True)` on that span, regardless of `capture_io`, like `sp.candidates`.
  - **If no trace is exported for this failure,** add the core to the structured JSON log
    line that `routes.py` emits for the failed request, as `conflicting_core` with the same
    `to_record()` shape. Record that as a decision in the report.

  Either way the value is content-free.
- **Content-free test (spec §5.12):** send a request whose prompt contains the marker
  `ZZMARKER-7f3a` through an app that will 422. Assert the marker is absent from the 422
  body's `conflicting_core`, from the outcome row's `conflicting_core` column, and from the
  trace attribute or log record captured in the test.

- [ ] **Step 1: Write the failing tests** in `tests/test_conflicting_core_api.py`, using the
  real `TestClient(create_app())` and the admin helpers other API tests use (grep
  `_make_app`, `_login` in `tests/`). Cover:
  1. An `onprem_only` app with every onprem model disabled and a cloud model available: 422,
     `code == "no_eligible_model"`, `error.conflicting_core.state` lists `disabled`, and the
     requirements include `residency`. Alternatively, construct a case whose core is
     non-empty: an `onprem_only` app pinned to a cloud model.
  2. The envelope still has `message`, `type`, `param: None` and `code`.
  3. The outcome row (read like `tests/test_outcome_hooks.py` does) has
     `status == "no_eligible"` and a `conflicting_core` dict with matching ids.
  4. The `/admin/outcomes/export` line includes `conflicting_core`.
  5. The marker test above.
- [ ] **Step 2: Run to verify failure.**
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** the new file plus `tests/test_outcome_*.py`, `tests/test_openai_compat.py`,
  `tests/test_v1_errors*.py`, and whatever file holds the B49 envelope tests (grep
  `v1_error(`). Then run the FULL `tests/` once. Expected: PASS.
- [ ] **Step 5: Commit** `feat(api): conflicting core in the 422, outcome row and trace (P3b)`.

---

### Task 5: The simulator reports cores

**Files:**
- Modify: `app/eval/simulate.py`
- Test: `tests/eval/test_simulate.py`

**Interfaces:**
- Consumes: `NoEligibleCandidate.core` (Task 3).
- Produces:
  - `ArmResult` gains `core: tuple[str, ...] = ()` and `core_state: tuple[str, ...] = ()`, as
    trailing fields with defaults;
  - `summary["new_rejections"]["by_core"]`, of the form `{"residency+requested_model": n, "(state:disabled)": n, …}`.

**Rules:**
- In `simulate_row`'s `NoEligibleCandidate` branch: `core = exc.core.ids() if exc.core else
  ()` and `core_state = exc.core.state_ids() if exc.core else ()`.
- **The `by_core` key** is `"+".join(core)` when the core is non-empty. Otherwise it is
  `"(state:" + ",".join(core_state) + ")"`, or `"(unknown)"` when both are empty.
- The key is counted over `newly_rejected` rows only, in each totals / by_app / by_workload
  `new_rejections` block wherever `new_rejections` exists today (read `summarize`).
- **In `report.md`,** the "New rejections" section adds a "By conflicting core" table (core
  key, count), sorted by `(-count, key)`.
- **Determinism is unchanged.** Keys are strings and the dict is sorted at dump time.

- [ ] **Step 1: Write the failing tests** in `tests/eval/test_simulate.py`, using the existing
  `sim_fixtures`:
  1. App `app-a` has `allowed_models=["local-mid"]`, and the proposal removes `local-mid`. The
     rows become `newly_rejected`, and `by_core == {"allowed_models": 3}`, or whatever the
     real computed core is. Assert on the value your implementation computes, and state why
     in a comment: removing the model leaves `local-cheap`/`local-big`, which violate
     `allowed_models`.
  2. A pinned row (`requested_model="local-big"`) with a proposal that disables `local-big`:
     `local-big` becomes state `disabled`, and the other models violate `requested_model`.
     Assert the key is `"requested_model"`.
  3. `ArmResult` for a routed row has `core == ()`.
  4. The `report.md` "By conflicting core" table is present when there are new rejections.
- [ ] **Step 2: Run to verify failure.**
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** `tests/eval/`. Expected: PASS.
- [ ] **Step 5: Commit** `feat(eval): simulator groups new rejections by conflicting core (P3b)`.

---

### Task 6: Documentation

**Files:**
- Create: `docs/reference/conflicting-core.md`
- Modify:
  - `docs/reference/routing-outcomes.md`: the column, and the ALTER in the Upgrade note;
  - `docs/reference/policy-simulator.md`: `by_core` and the report table;
  - `docs/research/feasible-route-bench.md`: append §16;
  - `CLAUDE.md`: one sentence after the P3a sentence in "Routing observability & signals".

**Rules:**
- **`conflicting-core.md`**, in the style of `docs/reference/quality-calibration.md`. It
  covers:
  - what the core is and when it appears;
  - the requirement table (id, security, fires when, source) and the state table;
  - the §3 result object, with a real 422 example captured from a test run;
  - the hint rule (security hints never relax; nothing is automatic);
  - the budget semantics (§2.3, including that tiers above the pick are not considered);
  - what is not covered: the gate's pinned-model 422, context-window 400s, and capabilities
    the registry does not model.
- **§16**, in the style of §15:
  - the model (minimum hitting set over the violation matrix);
  - why it is exact at this size;
  - the invariants §5.1–§5.12 with their test names;
  - a "Spark example" subsection with the named slot "Pending the Spark verification after
    deploy" and an empty two-column table (`case | core`), which the controller fills after
    deploy.
- **The CLAUDE.md sentence:** "Since P3b, an empty feasible set's 422 names the minimal
  conflicting core — the smallest set of requirements no available endpoint meets together,
  with endpoint state kept separate and remediation hints that never relax a security
  requirement — see `docs/reference/conflicting-core.md`."
- Verify every documented id, key and example against the code.

- [ ] **Step 1: Write the docs.**
- [ ] **Step 2: Commit** `docs: conflicting core reference, research §16, outcome column (P3b)`.
