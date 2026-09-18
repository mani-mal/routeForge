"""The three counterexample regressions (design §7, `CE-1`..`CE-3`).

Each test names the bug, the `file:line` it lived at before this branch (line
numbers are from `a7db4f5`, the merge base of `feat/policy-compiler-p1a`), and
asserts **the invariant that catches it** rather than the single fixed
behaviour. The distinction matters: "today's code returns the right answer for
this one input" is satisfied by a lucky special case, while "no placement
setting may rewrite a protection value, for any app, on any deployment" is a
statement the old code could not have satisfied however it was patched.

**What these tests do and do not demonstrate - read this before citing them.**
Design §13 says "the harness finds CE-1, CE-2 and CE-3 from a clean checkout of
the pre-fix code". That is **not** what is shown here, and the difference is
not pedantic:

* `CE-1` and `CE-2` are pinned **at the compiler seam**. Both bugs lived in
  code this branch deleted -- a clamp in `app/api/routes.py:596-598` and a
  missing check in `app/api/admin_routes.py:424-431` -- and they are asserted
  here through `compile_policy` / `validate_effective` /
  `obs.export.resolve_capture_io`, none of which existed in the pre-fix tree.
  The harness therefore cannot be run against a clean pre-fix checkout at all,
  and reinstating either bug **at its original site** would not redden these
  tests. What is demonstrated is narrower, and still worth having: neither bug
  can be reintroduced *at the seam every consumer now reads*, which is where
  this design moved the decision. The exporter half of `CE-1` is the exception
  -- it is asserted over live code (`resolve_capture_io`), and a mutation of
  that function is caught.
* `CE-3` is the closest to end-to-end: its mechanism half calls the real
  `Pipeline._gate_inputs`, the same function at the same site the bug lived at,
  and its compiler half is a mutation of live code.

End-to-end coverage of `CE-1` at the span level lives in
`tests/test_capture_io.py` / `tests/test_obs_export.py`, and of `CE-2` at the
HTTP boundary in `tests/test_admin_api.py`. Those are separate suites; this
file constrains the compiler, not every site an old bug could live at.
"""
from __future__ import annotations

import asyncio
from dataclasses import replace

import pytest
from hypothesis import given
from hypothesis import strategies as st

from app.apps.policy import AppPolicyView
from app.compression.ccr import CCRStore
from app.gateway.gateway import Gateway
from app.obs.export import resolve_capture_io
from app.policy.compile import (
    PolicyViolation, apply_signals, compile_policy, validate_effective,
)
from app.policy.context import match_sensitivity_rule
from app.policy.detectors import NullDetector, make_detector
from app.policy.gate import Gate
from app.routing.router import Router
from app.pipeline import Pipeline
from tests._litellm_fake import _DeterministicLiteLLM, make_test_registry
from tests.policy_compiler.strategies import (
    CLOUD, CLOUD_PROVIDERS, INVALID_VALUES, ONPREM_REGISTRY,
    UNIMPLEMENTED_VALUES, app_policies, context_for, deployments,
    valid_app_policies, _settings,
)

#: A deployment whose ceilings are both open — the only place the postures
#: these counterexamples are about (raw model input, raw client output, weak
#: detection) can be reached at all.
OPEN = _settings(True, True, "enforced", "regex", "off", True)


# =============================================================================
# CE-1
# =============================================================================

def test_ce1_placement_must_not_rewrite_a_telemetry_protection_value():
    """**CE-1.** An `onprem_only` + `passthrough` app configured with
    `capture_io: full` exported content labelled `redacted` that was raw apart
    from a regex pass.

    Where it lived (`a7db4f5`):

    * `app/api/routes.py:596-598` — ``if capture_io == "full" and
      (app_policy.residency == "onprem_only" or app_langfuse is None):
      capture_io = "redacted"``. A PLACEMENT setting silently rewrote a
      TELEMETRY protection value, for any posture.
    * `app/obs/export.py:70-75` — the `redacted` branch then exported
      ``sanitized_prompt or redact(prompt, ...)``, i.e. whatever view the GATE
      had handed the MODEL. Under `passthrough` that view is the raw prompt, so
      the "redaction" was the egress regex pass and nothing else.

    Why the harness catches it, and where that stops. The rewrite is a
    violation of invariant 3, "placement never changes protection" — the
    generative form of which is
    `test_invariants.py::test_placement_never_changes_protection`. This test is
    that invariant evaluated at CE-1's own point.

    **Scope, stated precisely.** `compile_policy` did not exist when this bug
    shipped, so this test could not have been run against the pre-fix tree, and
    re-adding the clamp at `routes.py:596-598` today would not redden it —
    `routes.py` no longer decides capture at all. What is pinned is that the
    clamp cannot be written **at the compiler seam**: the mutation proving it
    (M3, task report §3) inserts the same clamp inside `compile_policy`, which
    is where such a rule would have to live now. The end-to-end claim — that no
    span can carry raw content under a `redacted` label — is
    `tests/test_capture_io.py` / `tests/test_obs_export.py`, not this file.
    """
    base = AppPolicyView(policy_mode="passthrough", capture_io="full")
    onprem = replace(base, residency="onprem_only")
    elsewhere = replace(base, residency="any")

    assert (compile_policy(onprem, OPEN).protection
            == compile_policy(elsewhere, OPEN).protection)
    # ...and specifically: `full` stays `full`. The old clamp turned exactly
    # this into "redacted", which is the label the content did not deserve.
    assert compile_policy(onprem, OPEN).protection.telemetry_io == "full"


@given(view=valid_app_policies(), deployment=deployments(),
       residency=st.sampled_from(("any", "private_cloud_ok", "onprem_only")))
def test_ce1_no_residency_ever_moves_telemetry_io(view, deployment, residency):
    """CE-1, quantified: for every policy and every deployment, moving the
    residency axis leaves `telemetry_io` — and every other protection value —
    untouched. A clamp of the old shape, written at this seam, fails this for
    the whole `capture_io: full` population and not just the passthrough one.

    Seam-level, like the test above: it constrains `compile_policy`, which the
    pre-fix tree did not have."""
    moved = compile_policy(replace(view, residency=residency), deployment)
    assert moved.protection == compile_policy(view, deployment).protection


@given(capture=st.sampled_from(("off", "redacted", "full", "sometimes", "")),
       own_project=st.booleans(), has_detector=st.booleans())
def test_ce1_a_redacted_label_always_has_a_redactor_behind_it(
        capture, own_project, has_detector):
    """The other half of CE-1, at the exporter (design §2.6): the `redacted`
    LABEL is a promise about the bytes, so it may only be used when a detector
    actually ran. With none, the honest answer is `off` — never raw content
    under a redacted label, which is what shipped.

    `resolve_capture_io` is the single place that decision is made now, so the
    property is stated over it directly.

    **This is the one CE-1 assertion over live code**: `resolve_capture_io` is
    on the request path today, and mutation CE-1b (deleting its
    no-detector-means-off rule) reddens this property. It still is not the
    pre-fix code — that function was written in Task 7 — but it is the real
    decision point rather than a seam that stands in for one."""
    detector = object() if has_detector else None
    resolved = resolve_capture_io(capture, has_own_project=own_project,
                                  detector=detector)
    assert resolved in ("off", "redacted", "full")
    if resolved == "redacted":
        assert detector is not None, "a 'redacted' label with nothing to redact with"
    if resolved == "full":
        assert own_project, "raw I/O resolved for an app with no own project"
    if capture not in ("off", "redacted", "full"):
        assert resolved == "off", "an unparseable capture mode exported content"


def test_ce1_onprem_raw_capture_is_refused_at_write_time_not_relabelled():
    """What replaced the clamp. `onprem_only` + raw capture without a declared
    no-protection posture is now a named 422 at save time, so the operator is
    told; it is no longer accepted and quietly downgraded to a label the
    export could not honour (CLAUDE.md rule 2, amended 2026-09-05)."""
    eff = compile_policy(
        AppPolicyView(residency="onprem_only", policy_mode="enforced",
                      capture_io="full"), OPEN)
    with pytest.raises(PolicyViolation) as exc:
        validate_effective(eff, OPEN, has_own_langfuse_project=True)
    assert exc.value.code == "CAPTURE_FULL_ONPREM_NEEDS_NO_PROTECTION"


# =============================================================================
# CE-2
# =============================================================================

#: Every axis, the per-app field it is written through, and the value the
#: compiler must fail closed to when the stored one cannot be parsed.
_AXIS_DENY = (
    ("residency", "placement.residency", "onprem_only"),
    ("model_input", "protection.model_input", "redact"),
    ("client_output", "protection.client_output", "redact"),
    ("capture_io", "protection.telemetry_io", "off"),
    ("pii_detection", "protection.detection", "presidio"),
    ("stream_mode", "protection.stream_mode", "off"),
    # A data control, not a privacy control: its safe end is the one that
    # promises the LEAST about the bytes, which is `transformations_allowed`.
    ("content_integrity", "protection.content_integrity",
     "transformations_allowed"),
)


def test_ce2_capture_io_sometimes():
    """**CE-2.** `set_policy` accepted any string at all as `capture_io`.

    Where it lived (`a7db4f5`): `app/api/admin_routes.py:424-431` validated
    `residency`, `pii_detection`, `stream_mode` and `policy_mode` — four
    hand-written `if ... not in ...` checks — and stopped. `capture_io` had no
    such check, so `capture_io: "sometimes"` was answered `200`, stored, and
    then behaved as `off`: the operator believed telemetry was being captured
    and it was not.

    Why the harness catches it: the property is not "capture_io is validated",
    it is "**every** axis value that cannot be parsed fails closed AND is
    refused at write time" — quantified over the axes below and over arbitrary
    strings in the generative test that follows. A fifth hand-written check
    would satisfy the first phrasing and fail the second the next time an axis
    is added.

    **Scope, stated precisely.** This is a seam-level pin. It calls
    `validate_effective`, which `set_policy` now delegates to; it does not call
    the endpoint. Reverting `set_policy` to its own hand-written checks and
    dropping `capture_io` again would not redden this test — it would redden
    `tests/test_admin_api.py`, which exercises the HTTP boundary. What is
    demonstrated here is that the authority `set_policy` delegates to cannot
    have the hole, not that `set_policy` still delegates to it.
    """
    eff = compile_policy(AppPolicyView(capture_io="sometimes"), OPEN)
    assert eff.protection.telemetry_io == "off"           # fails closed
    assert ("telemetry_io", "sometimes") in eff.rejected  # and keeps the evidence
    with pytest.raises(PolicyViolation) as exc:
        validate_effective(eff, OPEN, has_own_langfuse_project=True)
    assert exc.value.code == "CAPTURE_IO_UNKNOWN_VALUE"
    assert "sometimes" in exc.value.message


@given(field_deny=st.sampled_from(_AXIS_DENY),
       bad=st.sampled_from(INVALID_VALUES),
       view=valid_app_policies(), deployment=deployments())
def test_ce2_every_axis_fails_closed_and_is_refused(field_deny, bad, view,
                                                    deployment):
    """CE-2, quantified over every axis and every unparseable value: the
    compiled value is the protective end of that axis, the offending value is
    preserved in `rejected` (so the 422 can name it rather than reporting a
    generic failure), and the write is refused."""
    field, path, deny = field_deny
    eff = compile_policy(replace(view, **{field: bad}), deployment)

    node = eff
    for part in path.split("."):
        node = getattr(node, part)
    assert node == deny, (
        f"{field}={bad!r} compiled to {node!r}, not the fail-closed {deny!r}")
    assert any(value == bad for _axis, value in eff.rejected), (
        f"{field}={bad!r} was denied but not recorded in rejected")

    with pytest.raises(PolicyViolation) as exc:
        validate_effective(eff, deployment, has_own_langfuse_project=True)
    assert exc.value.code.endswith("_UNKNOWN_VALUE")


@given(field_deny=st.sampled_from(_AXIS_DENY),
       bad=st.sampled_from(UNIMPLEMENTED_VALUES),
       deployment=deployments(malformed_defaults=False))
def test_ce2_unimplemented_axis_values_are_rejected_not_faked(field_deny, bad,
                                                              deployment):
    """CE-2's companion, design §9: the axis values the design names but does
    not build must be refused with their OWN code, not silently treated as the
    nearest thing that exists. Accepting `model_input: tokenize` and running
    plain redaction would promise a protection that does not run.

    Legal deployment defaults only. `validate_effective` reports the FIRST
    entry in `rejected`, and a malformed deployment default is recorded ahead
    of the app's own value — so a junk default would change the reported code
    without changing the guarantee. The guarantee under a junk default is
    covered by `test_invariants.py::
    test_every_compiled_value_is_legal_and_malformed_defaults_deny`."""
    field, _path, _deny = field_deny
    eff = compile_policy(AppPolicyView(**{field: bad}), deployment)
    with pytest.raises(PolicyViolation) as exc:
        validate_effective(eff, deployment, has_own_langfuse_project=True)
    assert exc.value.code == "AXIS_VALUE_NOT_IMPLEMENTED"


# =============================================================================
# CE-3
# =============================================================================

def _pipeline(settings) -> Pipeline:
    """A real `Pipeline`, so the detector-selection assertion below exercises
    `app/pipeline.py::_gate_inputs` itself rather than a restatement of it."""
    detector = make_detector(settings.serving.pii_detection)
    registry = make_test_registry(settings)
    co = settings.serving.context_optimization
    gateway = Gateway(settings, _DeterministicLiteLLM(settings),
                      Router(settings, registry=registry), registry)
    return Pipeline(settings, Gate(settings, detector, registry=registry),
                    detector, gateway, co_cfg_provider=lambda: co,
                    ccr=CCRStore(3600), registry=registry)


@given(view=app_policies(), deployment=deployments(),
       residency=st.sampled_from(("any", "private_cloud_ok", "onprem_only")))
def test_ce3_a_protection_posture_never_disables_placement_escalation(
        view, deployment, residency):
    """**CE-3.** Selecting `policy_mode: passthrough` silently disabled
    placement escalation for a detected SSN.

    Where it lived (`a7db4f5`): `app/pipeline.py:216-217` —
    ``if mode == "passthrough": return self._detector_for("off"), rules,
    mode``. A PROTECTION posture, and nothing else, selected the NullDetector;
    with no entity ever observed, `config/policy.yaml`'s `US_SSN ->
    onprem_only` rule could not fire, so an app switched to `passthrough` for
    metering reasons quietly lost its placement escalation. Nothing recorded
    that it had.

    Why the harness catches it: placement escalation is now a DECLARED axis
    (`escalate_residency_on_detection`, design §5) that no protection axis may
    touch, so the property is invariant 2 — and it is stated over the whole
    protection space, not over `policy_mode` alone. The old code fails it for
    every app whose posture differed.
    """
    # Residency is drawn from the legal three: CE-3 is about a PROTECTION
    # setting moving placement, and an unparseable residency is a separate
    # (fixed) matter -- see test_invariants.py::
    # test_unparseable_residency_fails_closed_in_the_context_path.
    view = replace(view, residency=residency)
    passthrough = replace(view, policy_mode="passthrough")
    enforced = replace(view, policy_mode="enforced")
    p_eff, e_eff = compile_policy(passthrough, deployment), compile_policy(enforced, deployment)

    # The switch itself is untouched by the posture...
    assert (p_eff.escalate_residency_on_detection
            == e_eff.escalate_residency_on_detection)

    # ...and given the same observation, both postures escalate identically.
    rule = match_sensitivity_rule(deployment, None, {"US_SSN"})
    p_after = apply_signals(p_eff, ("US_SSN",), rule)
    e_after = apply_signals(e_eff, ("US_SSN",), rule)
    assert p_after.placement == e_after.placement
    if view.escalate_residency_on_detection is not False:
        assert p_after.placement.residency == "onprem_only"
        # The rule is always named in the certificate when it fires...
        assert "rule:restricted" in p_after.cross_axis_rules_fired
        # ...and named in placement.sources exactly when it MOVED placement.
        # An app already at onprem_only is not "influenced" by the rule, and
        # sources must not claim it was (design §5).
        if p_eff.placement.residency != "onprem_only":
            assert "rule:restricted" in p_after.placement.sources
        else:
            assert "rule:restricted" not in p_after.placement.sources

    # And at the placement path the request actually takes: a passthrough app
    # that shows an SSN still loses the cloud.
    if view.escalate_residency_on_detection is not False:
        ctx = context_for(passthrough, deployment, ("US_SSN",))
        assert ctx.onprem_only is True
        assert not (ctx.allowed_providers & CLOUD_PROVIDERS)


def test_ce3_passthrough_with_a_named_detector_still_scans():
    """The mechanism half of CE-3. `passthrough` no longer selects the
    detector: `detection` does (design §2.3, §4). An app that names
    `pii_detection` gets the detector it named whatever its posture — so the
    SSN is observed, and the declared rule can fire."""
    pipe = _pipeline(OPEN)
    det, _rules, eff = pipe._gate_inputs(
        AppPolicyView(policy_mode="passthrough", pii_detection="regex"), None)
    assert not isinstance(det, NullDetector)
    assert eff.protection.model_input == "raw"      # still a no-protection posture
    assert eff.protection.client_output == "raw"
    assert eff.escalate_residency_on_detection is True


def test_ce3_residual_passthrough_without_a_named_detector_still_scans_nothing():
    """**The honest residual, recorded rather than papered over.** An app
    stored as `passthrough` with `pii_detection` NULL still resolves detection
    to `off` and still gets the NullDetector, so no entity is observed and no
    rule fires — which is, at the level of outcomes, CE-3 for that population.

    It is kept deliberately (design §4: those rows must stay byte-identical to
    today), and it is no longer *silent*, which is what CE-3 was really about:
    the compiled `detection` axis says `off`, `escalate_residency_on_detection`
    still says true, both are in the certificate (`sp.detection_mode`), and
    both are folded into `app_policy_version`. The fix is a one-field edit the
    operator can see, not a hidden coupling.

    This test exists so the residual cannot be mistaken for a property the
    harness proved absent."""
    pipe = _pipeline(OPEN)
    det, _rules, eff = pipe._gate_inputs(
        AppPolicyView(policy_mode="passthrough"), None)
    assert isinstance(det, NullDetector)
    assert eff.protection.detection == "off"
    assert eff.protection.sources["detection"] == "preset:passthrough"
    assert eff.escalate_residency_on_detection is True
