"""The six invariants of design §7, plus the generative form of the privacy
rule (CLAUDE.md rules 3 and 8), asserted with Hypothesis.

**What these properties do and do not prove.** They quantify over *stored
policy*, *deployment configuration* and *a declared signal* — the tuple of
detected entity TYPES. They do not quantify over prompts: whether a given
detector would have found `US_SSN` in a given string is the detectors' own
business, tested in `tests/test_detectors.py`. So "a protection axis never
moves placement" is proven here for the placement function *given the same
observation*, which is the property the design calls noninterference; it is
not a claim that turning the detector off cannot change what is observed. It
can, and design §4 says so deliberately (see `test_counterexamples.py::CE-3`).

Each property is stated so that it CAN fail: every one was checked by breaking
the relevant compiler behaviour in a working copy and confirming the property
went red. The mutations are recorded in the task report.
"""
from __future__ import annotations

from dataclasses import replace

import pytest
from hypothesis import assume, event, example, given
from hypothesis import strategies as st

from app.apps.policy import AppPolicyView
from app.errors import NoEligibleCandidate
from app.policy.compile import (
    PolicyViolation, apply_signals, compile_policy, validate_effective,
)
from app.routing.router import Router
from tests.policy_compiler.strategies import (
    CAPTURE_IOS, CLOUD, CLOUD_PROVIDERS, COMPILED_RANK, DETECTIONS,
    EMPTY_REGISTRY, INVALID_VALUES, ONPREM_REGISTRY, PROTECTION_FIELDS,
    PROTECTION_VALUES, RANK, RESIDENCIES, app_policies, compiled_placement,
    context_for, deployments, entity_types, protection_axes,
    valid_app_policies, _settings,
)

# The golden table IS invariant 6; it is referenced, never restated (design §7).
from tests.test_policy_golden_table import S as GOLDEN_S, _rejects


def _placement_of(eff):
    return eff.placement.residency


def _protection_ranks(eff) -> dict[str, int]:
    """The compiled protection axes as ranks in the tightening order. Axes
    whose compiled value is outside the order (impossible today — the compiler
    denies to a legal value — but not a thing to assume) are left out."""
    out = {}
    for attr, ranks in COMPILED_RANK.items():
        value = getattr(eff.protection, attr)
        if value in ranks:
            out[attr] = ranks[value]
    return out


# --- invariant 1: tightening never enlarges what is permitted ----------------

@st.composite
def _tightenings(draw):
    """A policy and a pointwise-tighter version of it.

    Only axes the app has SET are moved: `None` means "inherit" and has no
    place in the tightening order, so leaving it alone is what keeps the
    comparison meaningful — and keeps the `policy_mode` preset in play, which
    is where tightening one axis is supposed to pull others along with it.
    """
    view = draw(valid_app_policies())
    deltas = {}
    can_tighten = []          # axes with a strictly tighter value available
    for field, ranks in RANK.items():
        current = getattr(view, field)
        if current is None:
            continue
        tighter = sorted((v for v, r in ranks.items() if r >= ranks[current]),
                         key=ranks.get)
        if len(tighter) > 1:
            can_tighten.append(field)
        deltas[field] = draw(st.sampled_from(tighter))

    # Guarantee the pair is a REAL tightening whenever one is possible. Without
    # this every axis may draw its current value, and the property degenerates
    # into "compile_policy is deterministic" -- true, and not what it claims to
    # test. When the drawn deltas happen to change nothing, one axis that can
    # move is bumped one step.
    if not any(RANK[f][deltas[f]] > RANK[f][getattr(view, f)] for f in deltas):
        if can_tighten:
            field = draw(st.sampled_from(sorted(can_tighten)))
            ranks = RANK[field]
            current = ranks[getattr(view, field)]
            deltas[field] = next(v for v, r in sorted(ranks.items(),
                                                      key=lambda kv: kv[1])
                                 if r > current)
            event("tightening: forced one axis")
        else:
            # Every set axis is already at its strictest end; the identity pair
            # is the honest answer and the property still has to hold for it.
            event("tightening: already maximal")
    else:
        event("tightening: drawn")
    return view, replace(view, **deltas)


@given(pair=_tightenings(), deployment=deployments())
def test_tightening_never_enlarges_what_is_permitted(pair, deployment):
    """Design §7.1. Two halves, because "what is permitted" has two meanings:

    (a) every compiled axis lands at least as protective as before, and
    (b) the eligible candidate set — the providers and models a request may
        actually reach, derived through `build_context` exactly as the request
        path derives it — only ever shrinks.

    Half (b) is where a protection axis leaking into placement would surface as
    a *larger* set, which is why it is asserted over the real context builder
    rather than over the dataclass.
    """
    loose, tight = pair
    loose_eff = compile_policy(loose, deployment)
    tight_eff = compile_policy(tight, deployment)

    assert (RANK["residency"][_placement_of(tight_eff)]
            >= RANK["residency"][_placement_of(loose_eff)]), (
        f"tightening residency compiled looser: {loose} -> {tight}")

    loose_ranks, tight_ranks = _protection_ranks(loose_eff), _protection_ranks(tight_eff)
    for attr in loose_ranks.keys() & tight_ranks.keys():
        assert tight_ranks[attr] >= loose_ranks[attr], (
            f"tightening the policy made {attr} less protective: "
            f"{getattr(loose_eff.protection, attr)!r} -> "
            f"{getattr(tight_eff.protection, attr)!r} ({loose} -> {tight})")

    loose_ctx = context_for(loose, deployment, ())
    tight_ctx = context_for(tight, deployment, ())
    assert tight_ctx.allowed_providers <= loose_ctx.allowed_providers
    assert tight_ctx.allowed_models <= loose_ctx.allowed_models


# --- invariant 2: protection never changes placement -------------------------

@given(view=valid_app_policies(), deployment=deployments(),
       axis=protection_axes(), detected=entity_types())
def test_protection_never_changes_placement(view, deployment, axis, detected):
    """Design §7.2. Changing a protection axis never changes placement —
    unless a declared cross-axis rule fired, and then the rule is named in the
    certificate.

    Asserted three ways over the same perturbation: the compiled placement
    (residency AND its `sources`, so a placement that moved and then moved back
    would still be caught by its provenance), the placement after the declared
    rule has been applied, and the eligible candidate set the request path
    would actually build.
    """
    field, value = axis
    other = replace(view, **{field: value})
    assume(getattr(view, field) != value)

    base, variant = compile_policy(view, deployment), compile_policy(other, deployment)
    assert base.placement == variant.placement, (
        f"setting {field}={value!r} moved placement "
        f"{base.placement!r} -> {variant.placement!r}")

    after_base = compiled_placement(view, deployment, detected)
    after_variant = compiled_placement(other, deployment, detected)
    assert after_base.placement == after_variant.placement, (
        f"with {detected!r} detected, setting {field}={value!r} moved placement "
        f"{after_base.placement!r} -> {after_variant.placement!r}")

    # Any placement that DID move (relative to the compile-time placement) moved
    # only by a declared rule, and that rule is named in both places design §5
    # requires it to be named.
    if after_base.placement.residency != base.placement.residency:
        fired = after_base.cross_axis_rules_fired
        assert fired, "placement moved with no cross-axis rule recorded"
        assert any(name in after_base.placement.sources for name in fired), (
            "placement moved without the firing rule in placement.sources: "
            f"{after_base.placement.sources!r} vs {fired!r}")

    assert (context_for(view, deployment, detected).allowed_providers
            == context_for(other, deployment, detected).allowed_providers), (
        f"setting {field}={value!r} changed the eligible provider set")


# --- invariant 3: placement never changes protection -------------------------

@given(view=valid_app_policies(), deployment=deployments(),
       residency=st.sampled_from(("any", "private_cloud_ok", "onprem_only")),
       detected=entity_types())
def test_placement_never_changes_protection(view, deployment, residency, detected):
    """Design §7.3. Moving residency — the placement axis — leaves every
    protection value, and its recorded source, exactly as it was. Applying the
    declared cross-axis rule (which may move placement) likewise touches no
    protection axis.

    This is the invariant CE-1 violates: a placement setting (`onprem_only`)
    rewrote a telemetry protection value (`full` -> `redacted`).
    """
    moved = replace(view, residency=residency)
    base, variant = compile_policy(view, deployment), compile_policy(moved, deployment)
    assert base.protection == variant.protection, (
        f"residency={residency!r} changed protection "
        f"{base.protection!r} -> {variant.protection!r}")
    assert (base.escalate_residency_on_detection
            == variant.escalate_residency_on_detection)

    after = compiled_placement(moved, deployment, detected)
    assert after.protection == variant.protection, (
        f"applying the cross-axis rule for {detected!r} changed protection")


# --- invariant 4: ceilings win, and a malformed ceiling closes ---------------

@given(view=valid_app_policies(),
       bad_passthrough=st.sampled_from((None, "true", "false", "yes", 1, 0, "")),
       bad_downgrade=st.sampled_from((None, "true", "false", "yes", 1, 0, "")),
       default_mode=st.sampled_from(("enforced", "passthrough")),
       global_pii=st.sampled_from(("off", "regex", "presidio")))
def test_malformed_ceiling_closes_the_permission(view, bad_passthrough,
                                                 bad_downgrade, default_mode,
                                                 global_pii):
    """Design §2.2: "a ceiling that cannot be parsed denies; it never opens a
    permission."

    Note `1` and `"true"` are in the generated set on purpose. They are the
    shapes a ceiling takes when it arrives from an env override or a
    stringified YAML value, and both are truthy — a `if ceiling:` test would
    pass them and open the permission. The property is stated as an equivalence
    with the *closed* ceiling, which pins the whole compiled result rather than
    just the two axes anyone remembers to check.
    """
    malformed = _settings(bad_passthrough, bad_downgrade, default_mode,
                          global_pii, "off", True)
    closed = _settings(False, False, default_mode, global_pii, "off", True)
    assert compile_policy(view, malformed) == compile_policy(view, closed), (
        f"a malformed ceiling ({bad_passthrough!r}/{bad_downgrade!r}) did not "
        "compile identically to a closed one")


@given(view=app_policies(), deployment=deployments(malformed_ceilings=True))
def test_ceilings_win(view, deployment):
    """Design §2.2 rung 1: no per-app value, and no preset, can get past a
    closed ceiling.

    * `allow_app_passthrough` closed => neither data-handling axis is `raw`,
      whether the app asked for it directly or reached it through the
      `passthrough` preset (including a deployment whose *default* posture is
      passthrough).
    * `allow_app_pii_downgrade` closed => detection never lands weaker than the
      deployment's own default. Stated as a rank comparison rather than an
      equality because an app may still ask for something STRONGER.
    """
    eff = compile_policy(view, deployment)
    if deployment.serving.allow_app_passthrough is not True:
        assert eff.protection.model_input == "redact"
        assert eff.protection.client_output == "redact"
    if deployment.serving.allow_app_pii_downgrade is not True:
        default = deployment.serving.pii_detection
        if default in RANK["pii_detection"]:
            floor = RANK["pii_detection"][default]
            assert RANK["pii_detection"][eff.protection.detection] >= floor, (
                f"detection {eff.protection.detection!r} is weaker than the "
                f"deployment default {default!r} with the downgrade ceiling "
                "closed")
        else:
            # A malformed default cannot BE a floor, so the clamp has nothing
            # legal to fall back to and the axis denies outright.
            event("ceiling: closed over a malformed deployment default")
            assert eff.protection.detection == "presidio"


# --- invariant 5: an empty eligible set rejects ------------------------------

@given(view=valid_app_policies(), deployment=deployments(),
       detected=entity_types(), complexity=st.floats(0.0, 1.0))
def test_empty_eligible_set_rejects(view, deployment, detected, complexity):
    """Design §7.5 / IP-9's derived reject. With no healthy on-prem model
    onboarded, an `onprem_only` application has NO eligible candidate — and the
    router must reject rather than relax into the cloud pool that is sitting
    right there, enabled, in the same deployment.

    The registry here is empty (`has_healthy_onprem()` false) while azure and
    bedrock are switched on, so "relax" is a live option at every step.
    """
    onprem = replace(view, residency="onprem_only")
    ctx = context_for(onprem, deployment, detected, registry=EMPTY_REGISTRY)
    assert ctx.onprem_only is True
    assert not ctx.allowed_providers, (
        f"an onprem_only app with no healthy on-prem model kept "
        f"{set(ctx.allowed_providers)!r} eligible")

    with pytest.raises(NoEligibleCandidate):
        Router(deployment, registry=EMPTY_REGISTRY).select(
            ctx, tokens_after=200, complexity=complexity)


# --- invariant 6: preset fidelity -------------------------------------------

@given(mode=st.sampled_from(("enforced", "passthrough", None)),
       pii=st.sampled_from(("off", "regex", "presidio", None)),
       capture=st.sampled_from(("off", "redacted", "full", None)),
       residency=st.sampled_from(("any", "private_cloud_ok", "onprem_only")),
       stream=st.sampled_from(("off", "verified_then_stream", "stream_unverified",
                               "incremental_verify", None)),
       own_project=st.booleans())
def test_preset_fidelity_matches_the_golden_table(mode, pii, capture, residency,
                                                  stream, own_project):
    """Design §7.6. The contract is `tests/test_policy_golden_table.py`; this
    test REFERENCES it — it imports that module's `_rejects` and its settings
    rather than restating either — and re-checks the same agreement from the
    generative side.

    Why keep it when the golden table is already exhaustive over these five
    axes: the table's expectation lives in `_rejects`, and `_rejects` is the
    product's contract. Any change to it has to break something here too, so a
    future refactor cannot quietly relax the contract by editing the table and
    its expectation in the same commit and calling the suite green.

    The four P1a axes are deliberately left unset: `_rejects` does not model
    them, and a test that asserted an expectation its oracle does not hold
    would be asserting the implementation against itself.
    """
    view = AppPolicyView(residency=residency, policy_mode=mode, pii_detection=pii,
                         capture_io=capture, stream_mode=stream)
    eff = compile_policy(view, GOLDEN_S)
    expected = _rejects(mode, pii, capture, residency, stream, own_project)
    if expected is None:
        validate_effective(eff, GOLDEN_S, has_own_langfuse_project=own_project)
    else:
        with pytest.raises(PolicyViolation) as exc:
            validate_effective(eff, GOLDEN_S, has_own_langfuse_project=own_project)
        assert exc.value.code == expected


# --- invariant 7: onprem_only never yields a cloud candidate -----------------

@given(view=app_policies(), deployment=deployments(), detected=entity_types(),
       complexity=st.floats(0.0, 1.0))
# The rule-8 half below is guarded by `risk_class == "high"`. On the current
# strategies roughly half the generated examples reach it, but nothing in a
# generator GUARANTEES that, and a property whose most important assertion is
# silently never evaluated is worse than no property. This explicit example
# pins it: `US_SSN` matches config/policy.yaml's restricted rule, so the
# high-risk branch runs at least once however the strategies later change.
@example(view=AppPolicyView(), deployment=CLOUD, detected=("US_SSN",),
         complexity=0.1)
def test_onprem_only_never_yields_a_cloud_candidate(view, deployment, detected,
                                                    complexity):
    """CLAUDE.md rules 3 and 8, asserted generatively over EVERY combination of
    protection axes — including the invalid and unimplemented values an
    operator should never have been able to store.

    Rule 3: cloud is unreachable by construction. The assertion is on the
    candidate set, not on the chosen model, because "filtered out before the
    router scores anything" is the actual requirement — a post-hoc check on the
    winner would pass even if a cloud model had been scored and merely lost.

    Rule 8: residency beats the risk floor. The on-prem pool here tops out at
    `mid` (local-32b) while `risk_floors.high` is `frontier` and a
    bedrock-frontier candidate is enabled and sitting in the pool, so a
    high-risk request is exactly the case where a floor-satisfying
    implementation would reach for the cloud. It must floor to the best ALLOWED
    tier instead.
    """
    onprem = replace(view, residency="onprem_only")
    ctx = context_for(onprem, deployment, detected)

    assert ctx.onprem_only is True
    assert not (ctx.allowed_providers & CLOUD_PROVIDERS), (
        f"onprem_only app reached cloud providers "
        f"{set(ctx.allowed_providers & CLOUD_PROVIDERS)!r}")
    assert all(not name.startswith(("azure", "bedrock"))
               for name in ctx.allowed_models)

    decision = Router(deployment, registry=ONPREM_REGISTRY).select(
        ctx, tokens_after=200, complexity=complexity)
    assert decision.chosen.provider == "onprem", (
        f"onprem_only request routed to {decision.chosen.provider}")
    if ctx.risk_class == "high":
        # `frontier` exists in the pool but not on-prem: the floor is clamped
        # to the best allowed tier, never satisfied by leaving the boundary.
        event("rule 8: high-risk request floored inside the boundary")
        assert decision.chosen.tier == "mid"
        assert ctx.require_verification is True
    for rejected in decision.rejected:
        if rejected.provider in CLOUD_PROVIDERS:
            assert "residency" in rejected.reason, (
                f"cloud candidate {rejected.name} was rejected for "
                f"{rejected.reason!r}, not residency")

# --- totality: the compiler never emits a value that is not on its axis -------

@given(view=app_policies(), deployment=deployments())
def test_every_compiled_value_is_legal_and_malformed_defaults_deny(view,
                                                                   deployment):
    """`compile_policy` is **total**: whatever is stored and whatever is
    mounted, every value it returns is a legal member of its axis.

    This is the property that covers the compiler's malformed-**deployment
    default** guards — the `not in PII_MODES` / `not in TELEMETRY_IOS`
    re-checks that run after `_resolve` has fallen through to
    `serving.pii_detection` or `langfuse.capture_io`. Those guards had no
    property coverage at all until `deployments()` started drawing malformed
    defaults: a mutation removing one read as "equivalent" only because
    nothing generated an input that could reach it. Absence of coverage is not
    evidence of fail-closed behaviour, and the paper claims fail-closed
    behaviour.
    """
    eff = compile_policy(view, deployment)
    assert eff.placement.residency in RESIDENCIES
    for field, attr in PROTECTION_FIELDS.items():
        value = getattr(eff.protection, attr)
        assert value in PROTECTION_VALUES[field], (
            f"compiled {attr}={value!r}, which is not a member of the "
            f"{field} axis")

    # Telemetry has no preset, so a malformed deployment default always reaches
    # the guard when the app names nothing: it must deny AND leave evidence.
    if (view.capture_io is None
            and deployment.serving.observability.langfuse.capture_io
            not in CAPTURE_IOS):
        event("malformed deployment default: capture_io")
        assert eff.protection.telemetry_io == "off"
        assert any(axis == "telemetry_io" for axis, _ in eff.rejected)

    # Detection does have a preset (`passthrough` -> "off"), so either the
    # preset supplied a legal value or the junk default was denied and recorded.
    if view.pii_detection is None and deployment.serving.pii_detection not in DETECTIONS:
        event("malformed deployment default: pii_detection")
        assert (eff.protection.detection == "off"
                or (eff.protection.detection == "presidio"
                    and any(axis == "detection" for axis, _ in eff.rejected)))


# --- the fail-open the harness found, now closed ------------------------------

@given(bad=st.sampled_from(INVALID_VALUES), escalate=st.booleans(),
       detected=entity_types(), deployment=deployments())
def test_unparseable_residency_fails_closed_in_the_context_path(
        bad, escalate, detected, deployment):
    """A residency the compiler denies must be denied by the placement path
    too - the two may not disagree about where one app's compute may run.

    **The defect this records (found by this harness, fixed in this branch).**
    `compile_policy` denied an unparseable `residency` to `onprem_only`
    (`_DENY`, CLAUDE.md rule 5) while `app/policy/context.py` did not: with
    `escalate_residency_on_detection` false it skipped `_stricter` entirely,
    and `_placement_providers`'s ``if residency == "onprem_only" ... else:
    allowed = enabled`` treated anything it did not recognise as the LOOSEST
    placement. Minimal repro:

        view = AppPolicyView(residency="ONPREM_ONLY",
                             escalate_residency_on_detection=False)
        compile_policy(view, CLOUD).placement.residency
        # -> 'onprem_only'                              (denied, correct)
        build_context(...).allowed_providers
        # -> frozenset({'azure', 'bedrock', 'onprem'})  <-- routed to CLOUD
        build_context(...).onprem_only
        # -> False

    With escalation ON the same value instead raised `KeyError` out of
    `_stricter` - a 500, which at least did not route. Reachable without a
    direct DB write: `CreateAppIn.default_residency` and
    `UpdateAppIn.default_residency` were plain `str` with no enum check, and a
    policy row with a null residency inherits `app.default_residency`.

    Closed at both ends: `app/policy/compile.py::resolve_residency` is now the
    single authority the compiler AND the placement path call, and both admin
    endpoints validate `default_residency` against the enum (regression tests
    in `tests/test_admin_api.py`).
    """
    view = AppPolicyView(residency=bad, escalate_residency_on_detection=escalate)
    eff = compile_policy(view, deployment)
    assert eff.placement.residency == "onprem_only"
    assert ("residency", bad) in eff.rejected

    ctx = context_for(view, deployment, detected)
    assert ctx.onprem_only is True, (
        f"residency={bad!r} routed as {set(ctx.allowed_providers)!r}")
    assert not (ctx.allowed_providers & CLOUD_PROVIDERS)
