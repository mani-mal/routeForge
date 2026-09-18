# P1a — Policy Compiler and Noninterference Harness — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Split RouteForge's `policy_mode` into independent, separately-validated policy axes compiled by one pure function, so an operator can send a trusted model raw input while the client response and telemetry stay redacted — and prove, by generative test, that no axis silently rewrites another.

**Architecture:** A new pure `compile_policy()` produces an immutable `EffectivePolicy` (placement + protection, each field carrying its source). Every consumer — the gate, the egress verifier, the telemetry exporter, the admin validator — reads that instead of branching on `policy_mode`. The gate computes raw and redacted representations once and each consumer selects its own view, which decouples telemetry redaction from model-input redaction. A Hypothesis harness proves six invariants and carries three counterexamples from real bugs.

**Tech Stack:** Python 3.12, FastAPI, Pydantic v2, SQLAlchemy (async), pytest, Hypothesis (new dev dependency), React (console).

**Spec:** `docs/superpowers/specs/2026-09-18-policy-compiler-p1a-design.md` — read it first; this plan argues from it and the spec wins any disagreement.

## Global Constraints

- **Branch:** `feat/policy-compiler-p1a` (created; the spec is committed there as `2b8f9c5`).
- **Run pytest as** `LITELLM_LOCAL_MODEL_COST_MAP=True uv run pytest <args>`, or it wedges in D-state on a litellm network fetch. Add `timeout 900` in front. Do NOT run the full suite in a task unless the task says to; run the files the task names.
- **Environment gaps, not your bugs:** `presidio_analyzer` and `prometheus_client` are not installed. `tests/test_pii_detection_pipeline.py::test_engineering_labels_survive_gate_for_onprem_only_pii_off_app` fails with `ModuleNotFoundError`. `tests/test_admin_feedback.py` and `tests/test_langfuse_provision.py` are isolation-flaky.
- **Backward compatibility is the contract.** Every currently-legal policy combination must produce today's behavior, with exactly two deliberate exceptions (spec §4): `telemetry_io: redacted` with `model_input: raw`, and `client_output: redact` with `model_input: raw`.
- **`kit-gtm`-shaped apps** (`policy_mode: passthrough`, `pii_detection` null, `capture_io: full`) must stay byte-identical: no detector runs, nothing is redacted anywhere.
- **Never widen placement.** No protection axis may clear `onprem_only`, widen `allowed_providers`, or lower an app's residency floor. `apply_signals` is raise-only.
- **Never label raw content "redacted".** Any fail-closed path with no genuine sanitizer must emit `off`.
- **No migrations exist.** `create_all` never alters a table; four `ALTER TABLE` statements are part of the deploy, documented in the PR body.
- **Nulls stay meaningful.** Every new column is nullable and null means "inherit", exactly like the existing ones.
- **Commit after every task**, message ending with:
  `Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>`
- **Do not touch** `docs/archive/`, `docs/new/`, or other `docs/superpowers/` files.

---

## File Structure

**New**

| Path | Responsibility |
|---|---|
| `app/policy/compile.py` | the pure compiler: types, `compile_policy`, `apply_signals`, `validate_effective` |
| `tests/test_policy_compile.py` | unit tests for the compiler |
| `tests/test_policy_golden_table.py` | the backward-compatibility contract |
| `tests/policy_compiler/test_invariants.py` | the Hypothesis harness |
| `tests/policy_compiler/test_counterexamples.py` | `CE-1`, `CE-2`, `CE-3` |
| `docs/research/policy-compiler-noninterference.md` | the research artifact |

**Modified**

| Path | Change |
|---|---|
| `app/apps/models.py` | 4 nullable columns on `AppPolicy` |
| `app/apps/store.py` | `POLICY_FIELDS` |
| `app/apps/policy.py` | `AppPolicyView` fields + `version()` hash inputs |
| `app/api/admin_routes.py` | `PolicyIn`; clamps replaced by `validate_effective`; save-time invariant check |
| `app/pipeline.py` | `_gate_inputs` compiles instead of branching on `policy_mode`; model view selection |
| `app/policy/gate.py` | returns `Representations` |
| `app/policy/context.py` | escalation honours `escalate_residency_on_detection` |
| `app/output/verify.py` | egress redaction keyed on `client_output` |
| `app/obs/export.py` | telemetry builds its own redacted copy |
| `app/api/routes.py` | drop the `:599` clamp; certificate fields in the response |
| `app/compression/__init__.py` | `content_integrity: exact` bypass |
| `ui/src/api.ts`, `ui/src/console/RequestDrilldown.tsx` | certificate display |
| docs + `.claude/skills/privacy-gate/SKILL.md` | axis vocabulary; correct the detokenize section |

---

# Wave 1 — the seam

### Task 1: The pure compiler

**Files:**
- Create: `app/policy/compile.py`, `tests/test_policy_compile.py`
- Read first (do not edit): `app/policy/enums.py`, `app/apps/policy.py` (`AppPolicyView`), `app/config/schema.py` (`ServingCfg`)

**Interfaces:**
- Produces, for every later task:
  ```python
  compile_policy(app_policy: "AppPolicyView | None", deployment: Settings) -> EffectivePolicy
  apply_signals(eff: EffectivePolicy, detected_entity_types: tuple[str, ...], rule) -> EffectivePolicy
  ```
  plus the frozen dataclasses `Placement`, `Protection`, `EffectivePolicy` and the enum tuples `MODEL_INPUTS = ("raw", "redact")`, `CLIENT_OUTPUTS = ("raw", "redact")`, `CONTENT_INTEGRITY = ("exact", "transformations_allowed")`, and `UNIMPLEMENTED = {"tokenize", "authorized_rehydrate", "resolve_tokens", "trusted_raw", "inherit_model"}`.
- Consumes: nothing. This task wires nothing into the request path.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_policy_compile.py`:

```python
"""The compiler is a pure function: no DB, no clock, no request."""
import pytest

from app.config.loader import load_settings
from app.apps.policy import AppPolicyView
from app.policy.compile import compile_policy, apply_signals

S = load_settings()


def test_preset_enforced_maps_to_redact_both_sides():
    eff = compile_policy(AppPolicyView(residency="any", policy_mode="enforced"), S)
    assert eff.protection.model_input == "redact"
    assert eff.protection.client_output == "redact"
    assert eff.protection.sources["model_input"] == "preset:enforced"


def test_preset_passthrough_maps_to_raw_both_sides_and_no_detection():
    eff = compile_policy(AppPolicyView(residency="any", policy_mode="passthrough"), S)
    assert eff.protection.model_input == "raw"
    assert eff.protection.client_output == "raw"
    # pii_detection unset under passthrough resolves to off -- today's behavior
    assert eff.protection.detection == "off"


def test_explicit_axis_beats_preset():
    eff = compile_policy(
        AppPolicyView(residency="any", policy_mode="passthrough",
                      model_input="raw", client_output="redact"), S)
    assert eff.protection.client_output == "redact"
    assert eff.protection.sources["client_output"] == "app_policy"


def test_ceiling_beats_explicit_axis():
    locked = S.model_copy(update={"serving": S.serving.model_copy(
        update={"allow_app_passthrough": False})})
    eff = compile_policy(
        AppPolicyView(residency="any", model_input="raw", client_output="raw"), locked)
    assert eff.protection.model_input == "redact"
    assert eff.protection.client_output == "redact"
    assert eff.protection.sources["model_input"] == "ceiling"


def test_placement_never_changed_by_a_protection_axis():
    base = compile_policy(AppPolicyView(residency="onprem_only"), S)
    for axis in ("model_input", "client_output"):
        for value in ("raw", "redact"):
            eff = compile_policy(
                AppPolicyView(residency="onprem_only", **{axis: value}), S)
            assert eff.placement == base.placement, (axis, value)


def test_apply_signals_is_raise_only():
    eff = compile_policy(AppPolicyView(residency="onprem_only"), S)
    # a rule that would *loosen* placement must be ignored
    loosening = type("R", (), {"residency": "any", "risk_class": "low",
                               "require_verification": False, "sensitivity": "public"})()
    assert apply_signals(eff, ("PERSON",), loosening).placement.residency == "onprem_only"


def test_apply_signals_records_the_rule_that_fired():
    eff = compile_policy(AppPolicyView(residency="any"), S)
    tightening = type("R", (), {"residency": "onprem_only", "risk_class": "high",
                                "require_verification": True,
                                "sensitivity": "restricted"})()
    out = apply_signals(eff, ("US_SSN",), tightening)
    assert out.placement.residency == "onprem_only"
    assert "rule:restricted" in out.placement.sources
    assert out.require_verification is True


def test_escalation_switch_off_blocks_tightening_but_keeps_the_floor():
    eff = compile_policy(
        AppPolicyView(residency="private_cloud_ok",
                      escalate_residency_on_detection=False), S)
    tightening = type("R", (), {"residency": "onprem_only", "risk_class": "high",
                                "require_verification": True,
                                "sensitivity": "restricted"})()
    out = apply_signals(eff, ("US_SSN",), tightening)
    assert out.placement.residency == "private_cloud_ok"   # not raised
    assert out.require_verification is True                 # verification still applies


def test_compiler_is_deterministic_and_frozen():
    a = compile_policy(AppPolicyView(residency="any"), S)
    b = compile_policy(AppPolicyView(residency="any"), S)
    assert a == b
    with pytest.raises(Exception):
        a.protection.model_input = "raw"   # frozen dataclass
```

- [ ] **Step 2: Run them and watch them fail**

```bash
LITELLM_LOCAL_MODEL_COST_MAP=True timeout 900 uv run pytest tests/test_policy_compile.py -v
```

Expected: collection error — `app.policy.compile` does not exist.

- [ ] **Step 3: Write the compiler**

Create `app/policy/compile.py` with the types from spec §2.1 and this resolution order (spec §2.2): ceiling → explicit per-app axis → preset → deployment default. Requirements the tests above pin down, plus:

- `AppPolicyView` may not carry the new attributes yet (Task 2 adds them) — read them with `getattr(app_policy, name, None)` so this task stands alone.
- `detection` resolves as: explicit `pii_detection`; else `off` when the effective `policy_mode` is `passthrough`; else the deployment's `serving.pii_detection`. Apply `allow_app_pii_downgrade`: if false and the resolved value is `off`/`regex`, it becomes the deployment default with source `ceiling`.
- `content_integrity` defaults to `transformations_allowed`.
- `escalate_residency_on_detection` defaults to `True` when null.
- `apply_signals(eff, detected, rule)` returns a new `EffectivePolicy`: it may only tighten residency (order `any < private_cloud_ok < onprem_only`), may only set `require_verification` true, appends `f"rule:{rule.sensitivity}"` to `placement.sources`, and returns `eff` unchanged when `detected` is empty. When `escalate_residency_on_detection` is false it must still apply `require_verification` and sensitivity, but must not raise residency.
- Add a module docstring stating the rule the whole design rests on: **placement is computed only from residency and declared cross-axis rules; no protection axis may ever appear in a placement computation.**

- [ ] **Step 4: Run the tests to verify they pass**

```bash
LITELLM_LOCAL_MODEL_COST_MAP=True timeout 900 uv run pytest tests/test_policy_compile.py -v
```

Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add app/policy/compile.py tests/test_policy_compile.py
git commit -m "feat(policy): pure effective-policy compiler

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 2: The four columns

**Files:**
- Modify: `app/apps/models.py` (`AppPolicy`), `app/apps/store.py` (`POLICY_FIELDS`), `app/apps/policy.py` (`AppPolicyView` + `version()` + `policy_view_from`), `app/api/admin_routes.py` (`PolicyIn` only — validation is Task 3)
- Test: `tests/test_apps_store.py`, `tests/test_app_policy.py`

**Interfaces:**
- Consumes: Task 1's value tuples for the allowed literals.
- Produces: `AppPolicyView.model_input / .client_output / .content_integrity / .escalate_residency_on_detection`, all `| None`. `version()` folds each in **only when non-null**, so every existing app's hash is unchanged.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_app_policy.py`:

```python
def test_new_axes_default_to_none_and_do_not_change_the_version_hash():
    before = AppPolicyView(residency="any", policy_mode="enforced").version()
    after = AppPolicyView(residency="any", policy_mode="enforced",
                          model_input=None, client_output=None,
                          content_integrity=None,
                          escalate_residency_on_detection=None).version()
    assert before == after


def test_setting_an_axis_changes_the_version_hash():
    a = AppPolicyView(residency="any", policy_mode="enforced").version()
    b = AppPolicyView(residency="any", policy_mode="enforced",
                      client_output="raw").version()
    assert a != b
```

Add to `tests/test_apps_store.py` a round-trip that writes all four fields through `upsert_policy`, reads them back, and then resets them to null via the `reset` path.

- [ ] **Step 2: Run them and watch them fail**

```bash
LITELLM_LOCAL_MODEL_COST_MAP=True timeout 900 uv run pytest tests/test_app_policy.py tests/test_apps_store.py -v
```

Expected: `TypeError`/`AttributeError` on the unknown fields.

- [ ] **Step 3: Add the columns**

In `app/apps/models.py`, after `pii_exempt_patterns`, following the commenting style of the fields around them:

```python
    # --- P1a data-handling axes. null = inherit (resolved by
    # app/policy/compile.py). `policy_mode` remains a PRESET over
    # model_input/client_output; an explicit value here beats the preset, and
    # the allow_app_passthrough ceiling beats both.
    model_input: Mapped[str | None] = mapped_column(String(16), nullable=True)
    client_output: Mapped[str | None] = mapped_column(String(16), nullable=True)
    content_integrity: Mapped[str | None] = mapped_column(String(32), nullable=True)
    # Declared cross-axis rule switch: may a detected entity RAISE this app's
    # residency? null = true (today's behavior). false only ever prevents
    # tightening -- it can never lower the app's own residency floor.
    escalate_residency_on_detection: Mapped[bool | None] = mapped_column(
        Boolean, nullable=True)
```

Add the same four names to `store.POLICY_FIELDS`, to `PolicyIn` in `admin_routes.py` (typed `str | None` / `bool | None`, no validation yet), to `AppPolicyView`, to `policy_view_from`, and to `version()` using the existing "fold in only when set" pattern that `max_cost_usd_per_request` uses.

- [ ] **Step 4: Run the tests to verify they pass**

```bash
LITELLM_LOCAL_MODEL_COST_MAP=True timeout 900 uv run pytest tests/test_app_policy.py tests/test_apps_store.py tests/test_admin_api.py -v
```

- [ ] **Step 5: Write the deploy note**

Create `docs/guides/migrations/2026-09-18-p1a-columns.md` containing the four statements, and state they run **before** the new image rolls:

```sql
ALTER TABLE app_policies ADD COLUMN model_input VARCHAR(16);
ALTER TABLE app_policies ADD COLUMN client_output VARCHAR(16);
ALTER TABLE app_policies ADD COLUMN content_integrity VARCHAR(32);
ALTER TABLE app_policies ADD COLUMN escalate_residency_on_detection BOOLEAN;
```

If `docs/guides/migrations/` does not exist, check where the previous manual ALTER was documented (`git log --all --diff-filter=A --name-only | grep -i alter`) and follow that location instead.

- [ ] **Step 6: Commit**

```bash
git add -A
git commit -m "feat(policy): add the four data-handling columns

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 3: Validation through the compiler

Replaces the hand-written clamps in `set_policy` with one function both the admin API and the tests can call, fixes `CE-2`, and lands the two deliberate unlocks.

**Files:**
- Modify: `app/policy/compile.py` (add `validate_effective`), `app/api/admin_routes.py:394-723` (`set_policy`)
- Create: `tests/test_policy_golden_table.py`
- Test: `tests/test_policy_save_validation.py`, `tests/test_admin_api.py`

**Interfaces:**
- Produces:
  ```python
  class PolicyViolation(Exception):
      code: str      # e.g. "CAPTURE_FULL_NEEDS_OWN_PROJECT"
      message: str   # operator-facing, names the axis and the alternative

  validate_effective(eff: EffectivePolicy, deployment: Settings, *,
                     has_own_langfuse_project: bool) -> None   # raises PolicyViolation
  ```
- `set_policy` catches `PolicyViolation` and raises `HTTPException(422, exc.message)`, so every existing 422 message stays recognizable.

- [ ] **Step 1: Write the golden table first**

Create `tests/test_policy_golden_table.py`. It enumerates the product of `policy_mode × pii_detection × capture_io × residency × stream_mode` and asserts accept/reject plus the compiled axis values. Start from this table — it is the contract, and the two rows marked UNLOCK are the only intended changes from today:

```python
import itertools
import pytest

from app.config.loader import load_settings
from app.apps.policy import AppPolicyView
from app.policy.compile import compile_policy, validate_effective, PolicyViolation

S = load_settings()

MODES = ("enforced", "passthrough", None)
PII = ("off", "regex", "presidio", None)
CAPTURE = ("off", "redacted", "full", None)
RESIDENCY = ("any", "private_cloud_ok", "onprem_only")
STREAM = ("off", "verified_then_stream", "stream_unverified", "incremental_verify", None)


def _rejects(mode, pii, capture, residency, stream, own_project):
    """Today's rules, stated once. Any change to this function is a change to
    the product's contract and must be called out in the PR."""
    eff_pii = pii if pii is not None else ("off" if mode == "passthrough"
                                           else S.serving.pii_detection)
    if capture == "full":
        if not own_project:
            return "CAPTURE_FULL_NEEDS_OWN_PROJECT"
        # onprem_only + full requires the declared no-protection posture
        if residency == "onprem_only" and mode != "passthrough":
            return "CAPTURE_FULL_ONPREM_NEEDS_NO_PROTECTION"
    if capture == "redacted" and eff_pii == "off":
        return "CAPTURE_REDACTED_NEEDS_A_DETECTOR"
    if stream == "stream_unverified" and residency == "onprem_only":
        return "STREAM_UNVERIFIED_FORBIDDEN_ONPREM"
    return None


@pytest.mark.parametrize("mode,pii,capture,residency,stream",
                         itertools.product(MODES, PII, CAPTURE, RESIDENCY, STREAM))
@pytest.mark.parametrize("own_project", (True, False))
def test_golden_table(mode, pii, capture, residency, stream, own_project):
    view = AppPolicyView(residency=residency, policy_mode=mode, pii_detection=pii,
                         capture_io=capture, stream_mode=stream)
    eff = compile_policy(view, S)
    expected = _rejects(mode, pii, capture, residency, stream, own_project)
    if expected is None:
        validate_effective(eff, S, has_own_langfuse_project=own_project)
    else:
        with pytest.raises(PolicyViolation) as e:
            validate_effective(eff, S, has_own_langfuse_project=own_project)
        assert e.value.code == expected


def test_unlock_redacted_telemetry_with_raw_model_input():
    """UNLOCK 1 (spec §4): rejected today at admin_routes.py:631 because the
    exporter reused the gate's model-facing view. Telemetry now sanitizes
    independently, so this is legal WHEN a detector is configured."""
    eff = compile_policy(AppPolicyView(residency="any", policy_mode="passthrough",
                                       pii_detection="presidio",
                                       capture_io="redacted"), S)
    validate_effective(eff, S, has_own_langfuse_project=False)


def test_unlock_redacted_client_output_with_raw_model_input():
    """UNLOCK 2 (spec §4): the missing enterprise scenario."""
    eff = compile_policy(AppPolicyView(residency="onprem_only", model_input="raw",
                                       client_output="redact",
                                       pii_detection="presidio",
                                       capture_io="redacted"), S)
    validate_effective(eff, S, has_own_langfuse_project=False)
    assert eff.protection.model_input == "raw"
    assert eff.protection.client_output == "redact"


def test_still_rejected_redacted_telemetry_without_a_detector():
    eff = compile_policy(AppPolicyView(residency="any", policy_mode="passthrough",
                                       pii_detection="off", capture_io="redacted"), S)
    with pytest.raises(PolicyViolation):
        validate_effective(eff, S, has_own_langfuse_project=False)


@pytest.mark.parametrize("bad", ("sometimes", "FULL", "", "raw"))
def test_capture_io_enum_is_validated(bad):
    """CE-2: set_policy accepted any string for capture_io."""
    eff = compile_policy(AppPolicyView(residency="any", capture_io=bad), S)
    with pytest.raises(PolicyViolation) as e:
        validate_effective(eff, S, has_own_langfuse_project=True)
    assert e.value.code == "CAPTURE_IO_UNKNOWN_VALUE"


@pytest.mark.parametrize("axis,value", [("model_input", "tokenize"),
                                        ("client_output", "authorized_rehydrate")])
def test_unimplemented_axis_values_are_rejected_not_faked(axis, value):
    eff = compile_policy(AppPolicyView(residency="any", **{axis: value}), S)
    with pytest.raises(PolicyViolation) as e:
        validate_effective(eff, S, has_own_langfuse_project=True)
    assert e.value.code == "AXIS_VALUE_NOT_IMPLEMENTED"
    assert value in e.value.message
```

- [ ] **Step 2: Run it and watch it fail**

```bash
LITELLM_LOCAL_MODEL_COST_MAP=True timeout 900 uv run pytest tests/test_policy_golden_table.py -x -q
```

Expected: `ImportError` on `validate_effective` / `PolicyViolation`.

- [ ] **Step 3: Write `validate_effective`**

Add it to `app/policy/compile.py` with one named code per rule. Port the rules from `admin_routes.py:551-700` — read that block first; its comments explain *why* each clamp exists and those reasons must survive into the new code's comments. Rules:

| code | condition |
|---|---|
| `AXIS_VALUE_NOT_IMPLEMENTED` | any axis value in `UNIMPLEMENTED` |
| `CAPTURE_IO_UNKNOWN_VALUE` | `telemetry_io` not in `("off","redacted","full")` |
| `MODEL_INPUT_UNKNOWN_VALUE` / `CLIENT_OUTPUT_UNKNOWN_VALUE` / `CONTENT_INTEGRITY_UNKNOWN_VALUE` | likewise |
| `PASSTHROUGH_FORBIDDEN` | raw on either axis while `allow_app_passthrough` is false |
| `PII_DOWNGRADE_FORBIDDEN` | `detection` in `("off","regex")` while `allow_app_pii_downgrade` is false |
| `CAPTURE_REDACTED_NEEDS_A_DETECTOR` | `telemetry_io == "redacted"` and `detection == "off"` |
| `CAPTURE_FULL_NEEDS_OWN_PROJECT` | `telemetry_io == "full"` without the app's own project |
| `CAPTURE_FULL_ONPREM_NEEDS_NO_PROTECTION` | `telemetry_io == "full"`, `onprem_only`, and not (`model_input == "raw"` and `client_output == "raw"`) |
| `STREAM_UNVERIFIED_FORBIDDEN_ONPREM` | `stream_unverified` with `onprem_only` |
| `STREAM_UNVERIFIED_FORBIDDEN_REDACTED_OUTPUT` | `stream_unverified` with `client_output == "redact"` (new; spec §2.5) |

Each message must name the axis, why it is refused, and the legal alternatives — match the tone of the existing 422 strings, which operators already see.

- [ ] **Step 4: Rewire `set_policy`**

In `app/api/admin_routes.py`, replace the clamp block (from the `allow_app_passthrough` ceiling check through the `capture_io == "full"` clamp) with: build the post-write `AppPolicyView` from the merged fields, `compile_policy`, then `validate_effective(...)` inside `try/except PolicyViolation` → `HTTPException(422, exc.message)`. Keep every surrounding behavior exactly: the `reset` semantics, `exclude_none=True` merge, atomicity (nothing is committed when validation fails), and `policy_lint.policy_warnings` afterwards.

- [ ] **Step 5: Run the affected suites**

```bash
LITELLM_LOCAL_MODEL_COST_MAP=True timeout 900 uv run pytest \
  tests/test_policy_golden_table.py tests/test_policy_save_validation.py \
  tests/test_admin_api.py tests/test_app_policy.py tests/onboarding -q
```

Expected: all pass. Existing tests asserting specific 422 *messages* may need the message text matched to the new strings — update the test only when the meaning is unchanged; if a test's meaning changes, stop and report it as a contract change.

- [ ] **Step 6: Commit**

```bash
git add -A
git commit -m "feat(policy): validate effective policy through the compiler

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 4: Save-time invariant check

**Files:**
- Modify: `app/policy/compile.py` (add `check_invariants`), `app/api/admin_routes.py` (call it in `set_policy`)
- Test: `tests/test_policy_save_validation.py`

**Interfaces:**
- Produces: `check_invariants(view: AppPolicyView, deployment: Settings) -> None`, raising `PolicyViolation(code="INVARIANT_<NAME>")`. It compiles the given app policy over a fixed grid — the two residency neighbours, both values of each protection axis, and a fixed set of detected-entity tuples (`()`, `("PERSON",)`, `("US_SSN",)`) — and asserts monotonicity and noninterference for that app. Bounded work: the grid is a compile-time constant of ~50 combinations, no I/O.

- [ ] **Step 1: Write the failing test**

```python
def test_save_time_check_rejects_a_policy_that_breaks_an_invariant(monkeypatch):
    """A cross-axis rule that LOWERS placement must never be savable."""
    import app.policy.compile as c

    def broken(eff, detected, rule):
        return eff  # stand-in for a compiler bug: ignores the tightening rule
    monkeypatch.setattr(c, "apply_signals", broken)
    with pytest.raises(c.PolicyViolation) as e:
        c.check_invariants(AppPolicyView(residency="any"), S)
    assert e.value.code.startswith("INVARIANT_")


def test_save_time_check_passes_for_every_shipped_recipe():
    for view in (AppPolicyView(residency="any", policy_mode="enforced"),
                 AppPolicyView(residency="onprem_only", policy_mode="passthrough"),
                 AppPolicyView(residency="onprem_only", model_input="raw",
                               client_output="redact", pii_detection="presidio")):
        c.check_invariants(view, S)
```

- [ ] **Step 2: Run it, watch it fail, implement, run again**

```bash
LITELLM_LOCAL_MODEL_COST_MAP=True timeout 900 uv run pytest tests/test_policy_save_validation.py -v
```

Implement `check_invariants`, then call it from `set_policy` immediately after `validate_effective`. Re-run until green.

- [ ] **Step 3: Assert the write path stays fast**

Add a test that a policy PUT completes without the check dominating it — assert the grid size is bounded (`len(c._INVARIANT_GRID) <= 64`) rather than timing it, since wall-clock assertions are flaky in CI.

- [ ] **Step 4: Commit**

```bash
git add -A
git commit -m "feat(policy): reject policies that break an invariant at save time

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

# Wave 2 — consumers and evidence

### Task 5: Gate and pipeline read the compiler

**Files:**
- Modify: `app/pipeline.py` (`_gate_inputs`, `_policy_mode_for`, both call sites in `run`/`run_stream`), `app/policy/gate.py` (return `Representations`), `app/policy/context.py` (escalation honours the switch)
- Test: `tests/test_policy_mode_pipeline.py`, `tests/test_pii_detection_pipeline.py`, `tests/test_openai_compat.py`, `tests/test_tool_call_redaction.py` (run `ls tests | grep -i gate` first and include any gate-level file you find)

**Interfaces:**
- Consumes: `compile_policy`, `apply_signals` (Tasks 1, 3).
- Produces:
  ```python
  @dataclass(frozen=True)
  class Representations:
      raw: list[Message]
      redacted: list[Message] | None      # None only when no detector ran
      detected_entity_types: tuple[str, ...]
  ```
  `Gate.evaluate(...)` returns `(ctx, reps, eff)`. `_gate_inputs(app_policy, profile)` returns `(detector, rules, eff)` — **`policy_mode` is no longer returned**; every caller uses `eff.protection`.

- [ ] **Step 1: Write the failing tests**

```python
def test_detector_runs_when_only_the_client_needs_redaction():
    """model_input raw + client_output redact: the model sees the SSN, so the
    detector must still run for egress."""
    det, rules, eff = pipe._gate_inputs(
        AppPolicyView(residency="onprem_only", model_input="raw",
                      client_output="redact", pii_detection="regex"), None)
    assert not isinstance(det, NullDetector)
    assert eff.protection.model_input == "raw"


def test_no_detector_for_a_router_only_app():
    det, rules, eff = pipe._gate_inputs(
        AppPolicyView(residency="any", policy_mode="passthrough"), None)
    assert isinstance(det, NullDetector)


def test_model_receives_raw_text_under_model_input_raw(...):
    """End to end through /v1/chat/completions with a seeded backend: the text
    handed to the backend contains the SSN; the response does not."""


def test_model_receives_redacted_text_under_model_input_redact(...):
    """The inverse: the backend sees <US_SSN_1>."""
```

Write the last two as HTTP-level tests using the existing seeded-backend fixture (see `tests/test_openai_compat.py` for the pattern) — this behavior must be asserted at the boundary, not on an internal call.

- [ ] **Step 2: Run them, watch them fail**

- [ ] **Step 3: Implement**

- `_gate_inputs` builds the detector when **any** consumer needs it (spec §2.3), choosing its kind from `eff.protection.detection`, and keeps today's layering: `EffectiveDetector` for profile recognizers, `FilteredDetector` for the denylist/threshold/exempt patterns. Preserve the existing rule that `passthrough` runs *no* profile recognizers — under the compiler that becomes: no recognizers when `detection == "off"` **and** the app is in the raw/raw posture, which is the same set of apps.
- `Gate.evaluate` returns `Representations`; keep the redaction map inside the gate exactly as now (the map is still discarded — tokenization is out of scope).
- `run`/`run_stream` select the model-facing messages by `eff.protection.model_input`.
- `build_context` in `context.py` takes the escalation switch and applies `apply_signals` semantics; placement must still be computed from residency alone plus declared rules.
- Delete `_policy_mode_for` once nothing calls it.

- [ ] **Step 4: Run the suites**

```bash
LITELLM_LOCAL_MODEL_COST_MAP=True timeout 900 uv run pytest \
  tests/test_policy_mode_pipeline.py tests/test_pii_detection_pipeline.py \
  tests/test_openai_compat.py tests/test_tool_call_redaction.py \
  tests/test_derived_onprem_reject.py -q
```

- [ ] **Step 5: Commit**

---

### Task 6: Egress redaction independent of ingress

**Files:**
- Modify: `app/output/verify.py`, `app/pipeline.py` (the `verify()` call sites, including the cache re-verify), `app/api/routes.py` (streaming mode resolution)
- Test: `tests/test_egress_custom_redaction.py`, `tests/test_streaming.py`, `tests/test_stream_unverified.py`, `tests/test_incremental_verify_route.py`

**Interfaces:**
- Consumes: `eff` from Task 5.
- Produces: `verify(det, ctx, text, *, client_output: str)`; when `client_output == "raw"` it performs no redaction but still runs the `UNVERIFIABLE` fail-closed check.

- [ ] **Step 1: Write the failing tests**

```python
def test_client_output_redact_removes_the_ssn_the_model_saw(...):
    """model_input raw + client_output redact, end to end: the backend is handed
    123-45-6789 and the HTTP body contains a placeholder, not the number."""


def test_client_output_raw_leaves_the_completion_untouched(...):


def test_stream_unverified_is_refused_when_client_output_is_redact():
    """Spec §2.5 / the new STREAM_UNVERIFIED_FORBIDDEN_REDACTED_OUTPUT rule:
    the write is refused; and if such a policy somehow exists, the runtime
    floors it to verified_then_stream and records the fallback in the trace."""
```

- [ ] **Step 2-4: Run, implement, re-run**

The runtime floor lives in exactly one place (`Pipeline.run_stream`'s `StreamFallback`), as it does today — extend its condition, do not add a second floor in the route.

- [ ] **Step 5: Commit**

---

### Task 7: Telemetry projects independently (fixes CE-1)

**Files:**
- Modify: `app/obs/export.py` (`_set_io`), `app/api/routes.py` (delete the clamp at ~`:599`), `app/obs/otel.py` if its skip condition keys on the old values
- Test: `tests/test_capture_io.py`, `tests/test_raw_completion_capture.py`, `tests/test_trace_full_io.py`, `tests/test_obs_redact.py`, `tests/test_request_display.py`

**Interfaces:**
- Consumes: `Representations` (Task 5), `eff` (Task 1).
- Produces: `_set_io` builds its own redacted copy from the raw values with the app's detector. Fail-closed: a genuine redacted copy exists → `redacted`; otherwise → `off`. Never raw under a redacted label.

- [ ] **Step 1: Write the failing test — this is CE-1**

```python
def test_onprem_passthrough_full_does_not_export_raw_under_a_redacted_label():
    """CE-1. Before P1a, routes.py:599 clamped full -> redacted for ANY
    onprem_only app regardless of policy_mode, and the 'redacted' exporter then
    reused the gate's sanitized prompt -- which under passthrough IS the raw
    text. The app was accepted at write time as onprem_only + passthrough +
    full, so the operator believed raw capture was going to their own project;
    what shipped was raw content labelled redacted, minus a regex pass."""
```

Assert on the exported span payload that the SSN is absent from every I/O field when the effective capture is `redacted`, and that a `full` app with its own project still gets raw.

- [ ] **Step 2-4: Run, implement, re-run**

Also assert the display payload (`resp["display"]`) obeys the same rule — it is a second I/O surface and `routes.py:626-636` currently feeds it `sanitized_prompt`.

- [ ] **Step 5: Commit**

---

### Task 8: `content_integrity: exact`

**Files:**
- Modify: `app/compression/__init__.py` (`_bypass`), `app/policy/context.py` (carry the mode), `app/obs/export.py` (trace attribute)
- Test: `tests/test_compress_orchestrator.py`, `tests/test_compression_and_mode.py`

**Interfaces:**
- Produces: bypass reason `content_integrity_exact`, and `sp.content_integrity_mode` on the trace.

- [ ] **Step 1: Write the failing test** — an app with `content_integrity: exact` gets `applied=False`, `bypass_reason="content_integrity_exact"`, and byte-identical messages, even with compression enabled and a payload well over `min_tokens`.
- [ ] **Step 2-4: Run, implement, re-run.** `exact` is a data control: it must not touch placement, detection or telemetry.
- [ ] **Step 5: Commit**

---

### Task 9: The certificate

**Files:**
- Modify: `app/obs/export.py` (the `sp.*` attributes), `app/api/routes.py` (response `policy` block), `ui/src/api.ts`, `ui/src/console/RequestDrilldown.tsx`
- Test: `tests/test_candidate_export.py`, `tests/test_request_display.py`, new `tests/test_decision_certificate.py`

**Interfaces:**
- Produces the fields listed in spec §6. `sp.checks` contains `chosen_in_eligible_set` and `fallback_in_eligible_set`.

- [ ] **Step 1: Write the failing tests** — every field present on the trace and in the response; no raw value anywhere in the certificate (assert the SSN string is absent from the serialized certificate); `sp.cross_axis_rules_fired` names the rule when an SSN raises residency and is empty when nothing fired.
- [ ] **Step 2-4: Run, implement, re-run.** The console shows placement, the four protection modes, the rules that fired, and the checks, in the existing drill-down. Keep the UI a renderer: no rule may be re-implemented client-side (that is the recurring defect class in this repo's console).
- [ ] **Step 5: `cd ui && npm run build`, then commit**

---

### Task 10: The Hypothesis harness

**Files:**
- Create: `tests/policy_compiler/__init__.py`, `tests/policy_compiler/strategies.py`, `tests/policy_compiler/test_invariants.py`, `tests/policy_compiler/test_counterexamples.py`
- Modify: `pyproject.toml` (add `hypothesis` to the dev/test dependency group — check how the group is declared before editing)

**Interfaces:**
- Consumes everything above. Produces no application code.

- [ ] **Step 1: Write the strategies**

`strategies.py` builds `AppPolicyView`s across every axis, deployment settings across both ceilings, and detected-entity tuples drawn from the types named in `config/policy.yaml`'s `sensitivity_rules`.

- [ ] **Step 2: Write the six invariants** (spec §7), one test each:

```python
@given(view=app_policies(), deployment=deployments())
def test_tightening_never_enlarges_the_eligible_set(view, deployment): ...

@given(view=app_policies(), deployment=deployments(), axis=protection_axes())
def test_protection_never_changes_placement(view, deployment, axis):
    """Unless a declared cross-axis rule fired — and then it must be named in
    the certificate."""

@given(...)
def test_placement_never_changes_protection(...): ...

@given(...)
def test_malformed_ceiling_closes_the_permission(...): ...

@given(...)
def test_empty_eligible_set_rejects(...): ...

@given(view=app_policies(), deployment=deployments())
def test_onprem_only_never_yields_a_cloud_candidate(view, deployment):
    """Spec §8 / CLAUDE.md rules 3 and 8, asserted generatively: for EVERY
    combination of protection axes, an onprem_only app's allowed_providers
    contains no cloud provider, and a high-risk request is floored to the best
    allowed on-prem tier rather than lifted to cloud."""
```

Invariant 6 (preset fidelity) is already the golden table from Task 3; reference it rather than duplicating it.

- [ ] **Step 3: Write the three counterexample regressions**

`test_counterexamples.py`, one test per `CE-`, each with a docstring stating the bug, the file:line it lived at before P1a, and why the harness would have caught it. `CE-1` and `CE-2` belong to Tasks 7 and 3 — here they are asserted through the compiler, so a future refactor cannot reintroduce them without a red test.

- [ ] **Step 4: Run the harness**

```bash
LITELLM_LOCAL_MODEL_COST_MAP=True timeout 900 uv run pytest tests/policy_compiler -q
```

If Hypothesis finds a genuine counterexample in the new code, **do not weaken the invariant to make it pass** — fix the compiler, and record the counterexample in your report; it belongs in the research doc.

- [ ] **Step 5: Commit**

---

### Task 11: Documentation and the research artifact

**Files:**
- Modify: `docs/reference/per-app-config.md` (authoritative), `docs/guides/choosing-app-settings.md`, `faq.md`, `getting-started.md`, `creating-a-sample-application.md`, `CLAUDE.md` (rule 2a gains the axis vocabulary), `.claude/skills/privacy-gate/SKILL.md`
- Create: `docs/research/policy-compiler-noninterference.md`

- [ ] **Step 1: Make `per-app-config.md` authoritative** — document each axis, its values, the precedence order (ceiling → explicit → preset → default), the presets, the golden-table rules, and the two unlocks. Add the recipe table from the handoff §12, restricted to what P1a implements.
- [ ] **Step 2: Reconcile the four guides against it**, resolving the contradictions the handoff §14 lists: the full-capture rule, what `regex` detects and redacts, what `pii_detection: off` disables (and that custom recognizers still run under it), and default-versus-ceiling language. Every guide must state the same rule.
- [ ] **Step 3: Correct the privacy-gate skill** — the redaction map is discarded today and nothing detokenizes; tokenization is not implemented. Do not describe an unbuilt mode as a supported behavior.
- [ ] **Step 4: Write the research doc** — the formal model (axes, ordering, the meet), the six invariants, each counterexample with its commit SHA and the failing assertion, the harness setup and how to reproduce it, a related-work section listing what must be verified (the arXiv IDs in `docs/roadmap/ip-and-research-opportunities.md` are flagged unverified — do not cite them as established), and the file-before-publish note covering the IP-1 / IP-4 / IP-9 overlap.
- [ ] **Step 5: Full gate**

```bash
LITELLM_LOCAL_MODEL_COST_MAP=True timeout 2400 uv run pytest tests -q
LITELLM_LOCAL_MODEL_COST_MAP=True timeout 900 uv run pytest tests/onboarding -q
cd ui && npm run build && cd ..
helm template deploy/helm/serving-plane >/dev/null && helm template deploy/hpe/routeforge >/dev/null
```

Expected: the only failure is the known `presidio_analyzer` one. Paste real output.

- [ ] **Step 6: Commit**

---

## Manual verification (after Task 11)

1. Apply the four `ALTER` statements to a scratch database, boot, and confirm `/health`.
2. Configure an app `onprem_only` + `model_input: raw` + `client_output: redact` + `capture_io: redacted`, send "Summarize the ticket for Jane Smith. Her SSN is 123-45-6789", and confirm: the backend received the real SSN, the HTTP body did not, the trace's I/O fields did not, and the certificate names the axes.
3. Configure a router-only app (`passthrough`, `pii_detection: off`, `capture_io: off`) and confirm the request and response are byte-identical to the input and that no entity types are reported.
4. Attempt `model_input: tokenize` and confirm the 422 says it is not implemented.

## Out of scope

Tokenization and the broker, authorized rehydration, per-tool axes, endpoint data-handling metadata (P1b), and the compression-bypass gap recorded separately (single-turn requests never compress under shipped defaults).
