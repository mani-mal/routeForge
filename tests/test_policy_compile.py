"""The compiler is a pure function: no DB, no clock, no request."""
from dataclasses import dataclass

import pytest

from app.config.loader import load_settings
from app.apps.policy import AppPolicyView
from app.policy.compile import compile_policy, apply_signals

S = load_settings()


@dataclass(frozen=True)
class _View(AppPolicyView):
    """`AppPolicyView` plus the four axis columns Task 2 adds to it.

    The compiler reads the new axes with `getattr(..., None)` precisely so it
    can ship before the schema does; this stand-in lets the tests exercise
    those paths now. Delete it (and use `AppPolicyView` directly) once Task 2
    has landed the columns.
    """

    model_input: str | None = None
    client_output: str | None = None
    content_integrity: str | None = None
    escalate_residency_on_detection: bool | None = None


def _rule(residency: str, risk_class: str, require_verification: bool,
          sensitivity: str):
    return type("R", (), {"residency": residency, "risk_class": risk_class,
                          "require_verification": require_verification,
                          "sensitivity": sensitivity})()


def test_preset_enforced_maps_to_redact_both_sides():
    eff = compile_policy(_View(residency="any", policy_mode="enforced"), S)
    assert eff.protection.model_input == "redact"
    assert eff.protection.client_output == "redact"
    assert eff.protection.sources["model_input"] == "preset:enforced"


def test_preset_passthrough_maps_to_raw_both_sides_and_no_detection():
    eff = compile_policy(_View(residency="any", policy_mode="passthrough"), S)
    assert eff.protection.model_input == "raw"
    assert eff.protection.client_output == "raw"
    # pii_detection unset under passthrough resolves to off -- today's behavior
    assert eff.protection.detection == "off"


def test_explicit_axis_beats_preset():
    eff = compile_policy(
        _View(residency="any", policy_mode="passthrough",
              model_input="raw", client_output="redact"), S)
    assert eff.protection.client_output == "redact"
    assert eff.protection.sources["client_output"] == "app_policy"


def test_ceiling_beats_explicit_axis():
    locked = S.model_copy(update={"serving": S.serving.model_copy(
        update={"allow_app_passthrough": False})})
    eff = compile_policy(
        _View(residency="any", model_input="raw", client_output="raw"), locked)
    assert eff.protection.model_input == "redact"
    assert eff.protection.client_output == "redact"
    assert eff.protection.sources["model_input"] == "ceiling"


def test_placement_never_changed_by_a_protection_axis():
    base = compile_policy(_View(residency="onprem_only"), S)
    for axis in ("model_input", "client_output"):
        for value in ("raw", "redact"):
            eff = compile_policy(
                _View(residency="onprem_only", **{axis: value}), S)
            assert eff.placement == base.placement, (axis, value)


def test_apply_signals_is_raise_only():
    eff = compile_policy(_View(residency="onprem_only"), S)
    # a rule that would *loosen* placement must be ignored
    loosening = _rule("any", "low", False, "public")
    assert apply_signals(eff, ("PERSON",), loosening).placement.residency == "onprem_only"


def test_apply_signals_records_the_rule_that_fired():
    eff = compile_policy(_View(residency="any"), S)
    tightening = _rule("onprem_only", "high", True, "restricted")
    out = apply_signals(eff, ("US_SSN",), tightening)
    assert out.placement.residency == "onprem_only"
    assert "rule:restricted" in out.placement.sources
    assert out.require_verification is True


def test_escalation_switch_off_blocks_tightening_but_keeps_the_floor():
    eff = compile_policy(
        _View(residency="private_cloud_ok",
              escalate_residency_on_detection=False), S)
    tightening = _rule("onprem_only", "high", True, "restricted")
    out = apply_signals(eff, ("US_SSN",), tightening)
    assert out.placement.residency == "private_cloud_ok"   # not raised
    assert out.require_verification is True                 # verification still applies


def test_compiler_is_deterministic_and_frozen():
    a = compile_policy(_View(residency="any"), S)
    b = compile_policy(_View(residency="any"), S)
    assert a == b
    with pytest.raises(Exception):
        a.protection.model_input = "raw"   # frozen dataclass


# --- requirements from the brief that its snippets did not pin down ----------

def test_no_app_policy_compiles_to_the_deployment_default():
    eff = compile_policy(None, S)
    assert eff.placement.residency == "any"
    assert eff.protection.model_input == "redact"      # default_policy_mode: enforced
    assert eff.escalate_residency_on_detection is True


def test_pii_downgrade_ceiling_restores_the_deployment_default():
    locked = S.model_copy(update={"serving": S.serving.model_copy(
        update={"allow_app_pii_downgrade": False})})
    eff = compile_policy(_View(residency="any", pii_detection="off"), locked)
    assert eff.protection.detection == locked.serving.pii_detection
    assert eff.protection.sources["detection"] == "ceiling"


def test_content_integrity_defaults_to_transformations_allowed():
    assert compile_policy(_View(residency="any"), S) \
        .protection.content_integrity == "transformations_allowed"
    assert compile_policy(_View(residency="any", content_integrity="exact"), S) \
        .protection.content_integrity == "exact"


def test_unimplemented_axis_value_fails_closed():
    eff = compile_policy(_View(residency="any", model_input="tokenize",
                               client_output="authorized_rehydrate"), S)
    assert eff.protection.model_input == "redact"
    assert eff.protection.client_output == "redact"
    assert eff.protection.sources["model_input"] == "ceiling"


def test_apply_signals_with_no_detections_is_the_identity():
    eff = compile_policy(_View(residency="any"), S)
    assert apply_signals(eff, (), _rule("onprem_only", "high", True, "restricted")) is eff


def test_apply_signals_never_touches_protection():
    eff = compile_policy(_View(residency="any", policy_mode="passthrough"), S)
    out = apply_signals(eff, ("US_SSN",),
                        _rule("onprem_only", "high", True, "restricted"))
    assert out.protection == eff.protection
