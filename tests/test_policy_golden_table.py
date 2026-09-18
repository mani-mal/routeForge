"""The compatibility contract for the policy compiler (design §4).

`_rejects` below is today's rule set, stated once. Every combination that is
legal today must stay legal and compile to the same behavior; every
combination rejected today must stay rejected. **Any change to `_rejects` is a
change to the product's contract**, not an implementation detail, and must be
called out in the PR.

The only intended differences from the pre-compiler behavior are the two
unlocks of design §4, each with its own named test below.
"""
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
                         list(itertools.product(MODES, PII, CAPTURE, RESIDENCY, STREAM)))
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


# --- the deployment ceilings -------------------------------------------------
# The golden table above cannot reach these: it runs on the repo's own config,
# where both ceilings are open. They get their own tests against a locked-down
# copy of the settings, because the ceiling rules carry the asymmetry that is
# easiest to re-break.

def _locked(**serving):
    return S.model_copy(update={"serving": S.serving.model_copy(update=serving)})


def test_pii_downgrade_ceiling_refuses_the_apps_own_override():
    locked = _locked(allow_app_pii_downgrade=False)
    eff = compile_policy(AppPolicyView(residency="any", pii_detection="regex"), locked)
    with pytest.raises(PolicyViolation) as e:
        validate_effective(eff, locked, has_own_langfuse_project=True)
    assert e.value.code == "PII_DOWNGRADE_FORBIDDEN"


def test_pii_downgrade_ceiling_ignores_an_app_that_overrode_nothing():
    """The false positive this rule is written to avoid.

    `allow_app_pii_downgrade: false` with a deployment `pii_detection` of
    `regex` is this repo's own test configuration. Keying the refusal on the
    EFFECTIVE detection value would 422 every single write on such a
    deployment, including for apps that never named the field — `pii_detection`
    unset means "inherit the live global detector", which is not a downgrade.
    Only an app's own override is (see `EffectivePolicy.clamped`).
    """
    locked = _locked(allow_app_pii_downgrade=False, pii_detection="regex")
    eff = compile_policy(AppPolicyView(residency="any"), locked)
    assert eff.protection.detection == "regex"
    validate_effective(eff, locked, has_own_langfuse_project=True)


def test_passthrough_ceiling_refuses_an_explicit_passthrough():
    locked = _locked(allow_app_passthrough=False)
    eff = compile_policy(AppPolicyView(residency="any", policy_mode="passthrough"),
                         locked)
    with pytest.raises(PolicyViolation) as e:
        validate_effective(eff, locked, has_own_langfuse_project=True)
    assert e.value.code == "PASSTHROUGH_FORBIDDEN"


def test_passthrough_ceiling_refuses_the_deployment_default_too():
    """The other half of the asymmetry: an app is exactly as unprotected
    whether `passthrough` came from its own row or from `default_policy_mode`,
    so unlike `pii_detection` this one DOES key on the effective value."""
    locked = _locked(allow_app_passthrough=False, default_policy_mode="passthrough")
    eff = compile_policy(AppPolicyView(residency="any"), locked)
    with pytest.raises(PolicyViolation) as e:
        validate_effective(eff, locked, has_own_langfuse_project=True)
    assert e.value.code == "PASSTHROUGH_FORBIDDEN"


def test_passthrough_ceiling_refuses_a_raw_axis_named_directly():
    """`raw` on either axis IS the no-protection posture; the ceiling gates the
    two independently, so neither may be used to walk around `policy_mode`."""
    locked = _locked(allow_app_passthrough=False)
    for axis in ("model_input", "client_output"):
        eff = compile_policy(AppPolicyView(residency="any", **{axis: "raw"}), locked)
        with pytest.raises(PolicyViolation) as e:
            validate_effective(eff, locked, has_own_langfuse_project=True)
        assert e.value.code == "PASSTHROUGH_FORBIDDEN", axis


# --- stream_unverified vs. an egress guarantee (design §2.5) -----------------

def test_stream_unverified_refused_with_an_explicit_redacted_client_output():
    eff = compile_policy(AppPolicyView(residency="any", client_output="redact",
                                       stream_mode="stream_unverified"), S)
    with pytest.raises(PolicyViolation) as e:
        validate_effective(eff, S, has_own_langfuse_project=True)
    assert e.value.code == "STREAM_UNVERIFIED_FORBIDDEN_REDACTED_OUTPUT"


def test_stream_unverified_still_legal_under_the_enforced_preset():
    """The compatibility half of the rule above. `enforced` resolves
    client_output to `redact` for every app that never named the axis, and
    `enforced` + `stream_unverified` is legal today — widening the new rule to
    the effective value would refuse that whole population, which would be a
    contract change, not a new rule."""
    eff = compile_policy(AppPolicyView(residency="any", policy_mode="enforced",
                                       stream_mode="stream_unverified"), S)
    assert eff.protection.client_output == "redact"
    validate_effective(eff, S, has_own_langfuse_project=True)


# --- the own-project lookup is lazy ------------------------------------------

def test_own_project_callable_is_only_invoked_for_full_capture():
    """`has_own_langfuse_project` may be a callable, and it is asked only when
    `telemetry_io` is `full`. The admin API's answer comes from decrypting
    stored credentials, which can raise on ciphertext left by a key rotation —
    so an unrelated policy write must never trigger it."""
    calls = []

    def _lookup():
        calls.append(1)
        return True

    for capture in ("off", "redacted"):
        eff = compile_policy(AppPolicyView(residency="any", capture_io=capture,
                                           pii_detection="presidio"), S)
        validate_effective(eff, S, has_own_langfuse_project=_lookup)
    assert calls == []

    eff = compile_policy(AppPolicyView(residency="any", capture_io="full"), S)
    validate_effective(eff, S, has_own_langfuse_project=_lookup)
    assert calls == [1]


def test_own_project_callable_still_refuses_full_without_a_project():
    eff = compile_policy(AppPolicyView(residency="any", capture_io="full"), S)
    with pytest.raises(PolicyViolation) as e:
        validate_effective(eff, S, has_own_langfuse_project=lambda: False)
    assert e.value.code == "CAPTURE_FULL_NEEDS_OWN_PROJECT"
