"""The compiler is a pure function: no DB, no clock, no request."""
from dataclasses import dataclass

import pytest

from app.config.loader import load_settings
from app.apps.policy import AppPolicyView
from app.policy.compile import UNIMPLEMENTED, compile_policy, apply_signals

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
    # policy_mode is in the sweep deliberately: it is the axis the design says
    # silently loosened placement (choosing passthrough disabled the detector,
    # so a detected SSN stopped forcing onprem_only -- counterexample CE-3).
    for axis, values in (("model_input", ("raw", "redact")),
                         ("client_output", ("raw", "redact")),
                         ("policy_mode", ("enforced", "passthrough")),
                         ("content_integrity", ("exact", "transformations_allowed")),
                         ("pii_detection", ("off", "regex", "presidio")),
                         ("capture_io", ("off", "redacted", "full"))):
        for value in values:
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


# --- fail-closed on garbage, and the evidence that survives it ---------------

#: (constructor kwarg, compiled axis name, the SAFE value a garbage string
#: must compile to). One row per axis the compiler resolves.
_GARBAGE_AXES = [
    ("residency", "residency", "onprem_only"),
    ("policy_mode", "policy_mode", "enforced"),
    ("model_input", "model_input", "redact"),
    ("client_output", "client_output", "redact"),
    ("pii_detection", "detection", "presidio"),
    ("capture_io", "telemetry_io", "off"),
    ("content_integrity", "content_integrity", "transformations_allowed"),
    ("stream_mode", "stream_mode", "off"),
]


@pytest.mark.parametrize("kwarg,axis,safe", _GARBAGE_AXES)
def test_every_axis_fed_garbage_compiles_to_the_safe_value(kwarg, axis, safe):
    """A malformed stored value must DENY, never raise. A KeyError here is a
    500 on the request path where a denial was required (CLAUDE.md rule 5)."""
    kw = {"residency": "any"}
    kw[kwarg] = "bogus-not-a-real-value"
    eff = compile_policy(_View(**kw), S)         # must not raise
    got = (eff.placement.residency if axis == "residency"
           else getattr(eff.protection, axis, None))
    if axis == "policy_mode":
        # policy_mode has no compiled field of its own; it shows up as the
        # preset both protection sides fall back to.
        assert eff.protection.model_input == "redact"
        assert eff.protection.client_output == "redact"
    else:
        assert got == safe


@pytest.mark.parametrize("kwarg,axis,safe", _GARBAGE_AXES)
def test_a_rejected_value_is_preserved_for_validation(kwarg, axis, safe):
    """Failing closed must not erase WHAT was rejected: `capture_io="sometimes"`
    and `capture_io="off"` both compile to telemetry_io "off", so without this
    a typo is indistinguishable from a deliberate setting and the admin API
    can only answer 200."""
    kw = {"residency": "any"}
    kw[kwarg] = "bogus-not-a-real-value"
    eff = compile_policy(_View(**kw), S)
    assert (axis, "bogus-not-a-real-value") in eff.rejected


def test_unimplemented_values_are_rejected_by_name():
    """Design §9: a policy naming one of these gets a 422 saying it is not
    implemented. The compiler denies it AND keeps the name so it can."""
    eff = compile_policy(_View(residency="any", model_input="tokenize",
                               client_output="authorized_rehydrate"), S)
    assert ("model_input", "tokenize") in eff.rejected
    assert ("client_output", "authorized_rehydrate") in eff.rejected
    assert {v for _, v in eff.rejected} <= UNIMPLEMENTED


def test_a_legal_value_is_never_rejected():
    eff = compile_policy(
        _View(residency="onprem_only", policy_mode="passthrough",
              model_input="raw", client_output="redact", pii_detection="regex",
              capture_io="full", content_integrity="exact",
              stream_mode="stream_unverified"), S)
    assert eff.rejected == ()


def test_a_ceiling_clamp_of_a_legal_value_is_not_a_rejection():
    """`source == "ceiling"` is ambiguous on its own -- it also means a legal
    `raw` was clamped by allow_app_passthrough. That case must NOT appear in
    `rejected`, or the validator would 422 a policy the operator may keep."""
    locked = S.model_copy(update={"serving": S.serving.model_copy(
        update={"allow_app_passthrough": False})})
    eff = compile_policy(_View(residency="any", model_input="raw"), locked)
    assert eff.protection.model_input == "redact"
    assert eff.protection.sources["model_input"] == "ceiling"
    assert eff.rejected == ()


def test_rule_that_did_not_move_residency_is_not_named_in_placement_sources():
    """The app is already onprem_only, so a restricted rule changes nothing.
    placement.sources must not claim it did; cross_axis_rules_fired records it."""
    eff = compile_policy(_View(residency="onprem_only"), S)
    out = apply_signals(eff, ("US_SSN",),
                        _rule("onprem_only", "high", True, "restricted"))
    assert out.placement.sources == eff.placement.sources
    assert "rule:restricted" in out.cross_axis_rules_fired
    assert out.require_verification is True


def test_rule_fired_with_escalation_off_is_recorded_but_not_in_placement():
    eff = compile_policy(_View(residency="private_cloud_ok",
                               escalate_residency_on_detection=False), S)
    out = apply_signals(eff, ("US_SSN",),
                        _rule("onprem_only", "high", True, "restricted"))
    assert "rule:restricted" in out.cross_axis_rules_fired
    assert "rule:restricted" not in out.placement.sources


def test_apply_signals_never_touches_protection():
    eff = compile_policy(_View(residency="any", policy_mode="passthrough"), S)
    out = apply_signals(eff, ("US_SSN",),
                        _rule("onprem_only", "high", True, "restricted"))
    assert out.protection == eff.protection
