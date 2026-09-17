"""Task 2: per-app `policy_mode` (enforced | passthrough) wiring into `Pipeline`.

`residency` used to conflate two independent things: WHERE compute may run
(placement) and how much the gate does to the data (protection). `policy_mode`
splits protection off. The load-bearing guarantee this file pins down is that
splitting them did NOT weaken placement: an `onprem_only` + `passthrough` app
gets its data through the gate completely untouched, while `ctx.onprem_only`
stays true and cloud providers stay out of `allowed_providers`. CLAUDE.md rule
3 ("`onprem_only` is absolute") is about placement, and passthrough never
touches it.

The second guarantee: `passthrough` is stronger than `pii_detection: "off"`.
"off" still layers the app's custom recognizers on top of the null detector, so
a leftover recognizer keeps silently redacting. "Passthrough" means untouched,
so the profile's recognizers are not layered on at all -- see `_gate_inputs`.
"""
from __future__ import annotations

import asyncio

import pytest

from app.apps.policy import AppPolicyView
from app.apps.privacy import AppPrivacyProfile
from app.compression.ccr import CCRStore
from app.config.loader import load_settings
from app.gateway.gateway import Gateway
from app.models import ChatRequest, Message
from app.pipeline import Pipeline
from app.policy.detectors import NullDetector, make_detector
from app.policy.recognizers import CustomRecognizer
from app.policy.gate import Gate
from app.routing.router import Router
from tests._litellm_fake import _DeterministicLiteLLM, make_test_registry


def _settings(global_pii: str = "regex", *, default_policy_mode: str = "enforced",
              allow_app_passthrough: bool = True):
    """Settings with the process-wide detection mode AND the two new
    deployment-level protection switches under our control (mirrors
    tests/test_pii_detection_pipeline.py::_settings_with_global_mode)."""
    s = load_settings()
    return s.model_copy(update={
        "serving": s.serving.model_copy(update={
            "pii_detection": global_pii,
            "default_policy_mode": default_policy_mode,
            "allow_app_passthrough": allow_app_passthrough,
        })})


def _pipeline(settings) -> Pipeline:
    """A real Pipeline (deterministic backend, seeded on-prem models) so
    `pipeline.run()` exercises the actual app/pipeline.py composition code
    rather than a re-implementation of it here."""
    det = make_detector(settings.serving.pii_detection)
    registry = make_test_registry(settings)
    co = settings.serving.context_optimization
    gate = Gate(settings, det, registry=registry)
    gateway = Gateway(settings, _DeterministicLiteLLM(settings),
                      Router(settings, registry=registry), registry)
    return Pipeline(settings, gate, det, gateway, co_cfg_provider=lambda: co, ccr=CCRStore(3600),
                    registry=registry)


def _run(pipeline: Pipeline, content: str, app_policy=None, profile=None) -> dict:
    req = ChatRequest(messages=[Message(role="user", content=content)])
    return asyncio.run(pipeline.run("req-1", "acme", req,
                                    app_policy=app_policy, profile=profile))


_SSN = "482-73-1946"
_PROMPT = f"My name is John Smith and my ssn is {_SSN}, please help."

# A per-app privacy profile with one custom recognizer, the kind an operator
# adds for a site-specific identifier the built-in detectors cannot see.
_MRN = "MRN-1234567"


def _profile() -> AppPrivacyProfile:
    return AppPrivacyProfile(
        recognizers=(CustomRecognizer("MRN", r"\bMRN-\d{7}\b", 0.9),), rules=())


# --- _policy_mode_for: effective-mode resolution ----------------------------


def test_policy_mode_defaults_to_enforced_with_no_app_policy():
    pipe = _pipeline(_settings())
    assert pipe._policy_mode_for(None) == "enforced"


def test_policy_mode_null_inherits_deployment_default():
    """`AppPolicy.policy_mode` null means inherit, exactly like
    `pii_detection` and `capture_io` already do."""
    pipe = _pipeline(_settings(default_policy_mode="passthrough"))
    assert pipe._policy_mode_for(AppPolicyView()) == "passthrough"
    # an explicit per-app value still wins over the deployment default
    assert pipe._policy_mode_for(AppPolicyView(policy_mode="enforced")) == "enforced"


def test_policy_mode_app_value_wins_over_enforced_default():
    pipe = _pipeline(_settings())
    assert pipe._policy_mode_for(AppPolicyView(policy_mode="passthrough")) == "passthrough"


def test_deployment_ceiling_forces_enforced():
    """`allow_app_passthrough: false` forbids passthrough install-wide, for
    every app, regardless of the per-app value OR the deployment default. Fail
    closed (CLAUDE.md rule 5): a policy row written before the ceiling was
    lowered must not keep running unprotected."""
    s = _settings(default_policy_mode="passthrough", allow_app_passthrough=False)
    pipe = _pipeline(s)
    assert pipe._policy_mode_for(AppPolicyView(policy_mode="passthrough")) == "enforced"
    assert pipe._policy_mode_for(AppPolicyView()) == "enforced"  # via the default too


# --- the headline guarantee: placement kept, protection off -----------------


def test_onprem_only_passthrough_keeps_placement_and_redacts_nothing():
    """THE case this feature exists for. An `onprem_only` + `passthrough` app:
    every value passes through VERBATIM (no detection, no redaction), while
    `onprem_only` is still true and the only allowed provider is on-prem.

    `residency` = placement, `policy_mode` = protection. Asserting both in one
    test is the point: the split must not have bought "untouched data" at the
    cost of the residency floor."""
    s = _settings("regex")
    prov = dict(s.serving.providers)
    prov["azure"] = prov["azure"].model_copy(update={"enabled": True})
    prov["bedrock"] = prov["bedrock"].model_copy(update={"enabled": True})
    s = s.model_copy(update={"serving": s.serving.model_copy(update={"providers": prov})})
    pipe = _pipeline(s)
    app_policy = AppPolicyView(residency="onprem_only", policy_mode="passthrough")

    out = _run(pipe, _PROMPT, app_policy=app_policy)

    # protection OFF: nothing detected, nothing redacted
    assert out["sanitized_prompt"] == _PROMPT
    assert _SSN in out["sanitized_prompt"]
    assert out["policy"]["detected_entities"] == []
    # placement UNCHANGED (CLAUDE.md rule 3)
    assert out["policy"]["onprem_only"] is True
    assert out["policy"]["allowed_providers"] == ["onprem"]


def test_passthrough_does_not_widen_allowed_providers_for_non_onprem_app():
    """The mirror image: passthrough is not a placement lever in EITHER
    direction. An app pinned to a provider subset keeps that subset."""
    s = _settings("regex")
    prov = dict(s.serving.providers)
    prov["azure"] = prov["azure"].model_copy(update={"enabled": True})
    s = s.model_copy(update={"serving": s.serving.model_copy(update={"providers": prov})})
    pipe = _pipeline(s)
    app_policy = AppPolicyView(residency="any", policy_mode="passthrough",
                               allowed_providers=frozenset({"onprem"}))

    out = _run(pipe, _PROMPT, app_policy=app_policy)

    assert out["policy"]["allowed_providers"] == ["onprem"]
    assert _SSN in out["sanitized_prompt"]


def test_passthrough_uses_the_null_detector_whatever_pii_detection_says():
    """Composition-level: passthrough overrides the app's own `pii_detection`
    (here "presidio", the strongest mode) down to the null detector. Nothing
    is wrapped around it -- no EffectiveDetector, no FilteredDetector."""
    pipe = _pipeline(_settings("regex"))
    ap = AppPolicyView(policy_mode="passthrough", pii_detection="presidio",
                       pii_entity_denylist=frozenset({"PERSON"}),
                       pii_score_threshold=0.9)
    det, rules, mode = pipe._gate_inputs(ap, _profile())
    assert mode == "passthrough"
    assert isinstance(det, NullDetector)
    assert det.analyze(_PROMPT) == []


def test_passthrough_leaves_the_completion_untouched_on_egress():
    """Egress (step 9) is handed the same null detector, so verify() finds
    nothing and the completion is returned exactly as the backend produced
    it -- `raw_completion` (pre-verify) equals the served content."""
    pipe = _pipeline(_settings("regex"))
    ap = AppPolicyView(residency="onprem_only", policy_mode="passthrough")

    out = _run(pipe, _PROMPT, app_policy=ap)

    served = out["choices"][0]["message"]["content"]
    assert served == out["raw_completion"]
    assert "<US_SSN" not in served


# --- passthrough vs pii_detection: "off" (the real behaviour difference) ----


def test_custom_recognizer_does_not_fire_under_passthrough():
    """`passthrough` skips the app's custom recognizers entirely. A leftover
    recognizer silently redacting an app the operator declared untouched is
    exactly the bug this mode exists to kill."""
    pipe = _pipeline(_settings("regex"))
    ap = AppPolicyView(residency="onprem_only", policy_mode="passthrough")

    out = _run(pipe, f"patient {_MRN} needs a refill", app_policy=ap, profile=_profile())

    assert _MRN in out["sanitized_prompt"]
    assert "MRN" not in out["policy"]["detected_entities"]
    assert out["policy"]["onprem_only"] is True


def test_custom_recognizer_still_fires_under_pii_detection_off():
    """The contrast case, asserted so the difference is not accidental:
    `pii_detection: "off"` still layers the profile's recognizers on top of the
    null base detector, so MRN IS redacted. That is the pre-existing behaviour
    and this task does not change it."""
    pipe = _pipeline(_settings("regex"))
    ap = AppPolicyView(residency="onprem_only", pii_detection="off")  # policy_mode inherits

    out = _run(pipe, f"patient {_MRN} needs a refill", app_policy=ap, profile=_profile())

    assert _MRN not in out["sanitized_prompt"]
    assert "<MRN_1>" in out["sanitized_prompt"]
    assert "MRN" in out["policy"]["detected_entities"]


# --- effective-mode inheritance, end to end ---------------------------------


def test_null_policy_mode_on_passthrough_deployment_behaves_as_passthrough():
    """An app that never sets `policy_mode` on a deployment whose
    `default_policy_mode` is `passthrough` -- the "install where no customer
    wants PII checks sets it once" case."""
    pipe = _pipeline(_settings("regex", default_policy_mode="passthrough"))
    ap = AppPolicyView(residency="onprem_only")  # policy_mode is None

    out = _run(pipe, _PROMPT, app_policy=ap)

    assert out["sanitized_prompt"] == _PROMPT
    assert out["policy"]["policy_mode"] == "passthrough"
    assert out["policy"]["onprem_only"] is True


def test_null_policy_mode_on_default_deployment_still_redacts():
    """No regression: with the shipped default (`enforced`), an app that sets
    no `policy_mode` redacts exactly as it did before this feature existed."""
    pipe = _pipeline(_settings("regex"))
    ap = AppPolicyView(residency="onprem_only")  # policy_mode is None

    out = _run(pipe, _PROMPT, app_policy=ap)

    assert _SSN not in out["sanitized_prompt"]
    assert "<US_SSN_1>" in out["sanitized_prompt"]
    assert "US_SSN" in out["policy"]["detected_entities"]
    assert out["policy"]["policy_mode"] == "enforced"


def test_enforced_is_byte_identical_to_no_policy_mode_at_all():
    """Regression guard: an explicit `policy_mode="enforced"` produces the same
    response as an app with the field unset, field for field (bar the app
    policy version, which folds `policy_mode` in BY DESIGN so flipping the mode
    always moves the stamp -- the stale-probe lesson)."""
    pipe = _pipeline(_settings("regex"))
    base = _run(pipe, _PROMPT, app_policy=AppPolicyView(residency="onprem_only"))
    explicit = _run(pipe, _PROMPT,
                    app_policy=AppPolicyView(residency="onprem_only",
                                             policy_mode="enforced"))

    assert explicit["sanitized_prompt"] == base["sanitized_prompt"]
    assert explicit["choices"] == base["choices"]
    assert explicit["cost"] == base["cost"]
    for k, v in base["policy"].items():
        if k == "app_policy_version":
            continue
        assert explicit["policy"][k] == v, k
    assert explicit["policy"]["policy_mode"] == "enforced"


# --- never silent: the stamp -------------------------------------------------


def test_response_policy_block_carries_the_effective_mode():
    """Stamped on every response, and it is the EFFECTIVE value -- the app's
    column here is null, so the deployment default is what must appear."""
    pipe = _pipeline(_settings("regex", default_policy_mode="passthrough"))
    out = _run(pipe, "hello there", app_policy=AppPolicyView())
    assert out["policy"]["policy_mode"] == "passthrough"

    pipe2 = _pipeline(_settings("regex"))
    out2 = _run(pipe2, "hello there", app_policy=AppPolicyView())
    assert out2["policy"]["policy_mode"] == "enforced"


def test_stamp_reports_the_clamped_mode_not_the_requested_one():
    """Under the deployment ceiling the response must say "enforced" -- what
    actually ran -- never the app's (refused) "passthrough". A stamp that
    reported intent instead of behaviour would be worse than none."""
    pipe = _pipeline(_settings("regex", allow_app_passthrough=False))
    out = _run(pipe, _PROMPT,
               app_policy=AppPolicyView(policy_mode="passthrough"))

    assert out["policy"]["policy_mode"] == "enforced"
    assert _SSN not in out["sanitized_prompt"]  # the gate really did run


def test_policy_mode_moves_the_app_policy_version():
    """Flipping the mode must move the version stamp so a customer's cached
    conformance-probe result can be told apart from a live one."""
    a = AppPolicyView(residency="onprem_only", policy_mode="enforced")
    b = AppPolicyView(residency="onprem_only", policy_mode="passthrough")
    assert a.version() != b.version()


# --- the tool-calling path (structured, per-message redaction) --------------


def test_passthrough_on_the_tool_calling_path_redacts_nothing():
    """The tool-calling path redacts each message individually rather than
    flattening; it must honour passthrough too, or a client driving its own
    agent loop would get silently-mangled tool arguments."""
    pipe = _pipeline(_settings("regex"))
    req = ChatRequest(
        messages=[Message(role="user", content=_PROMPT)],
        tools=[{"type": "function",
                "function": {"name": "lookup", "description": "look up",
                             "parameters": {"type": "object", "properties": {}}}}])
    out = asyncio.run(pipe.run(
        "req-tools", "acme", req,
        app_policy=AppPolicyView(residency="onprem_only", policy_mode="passthrough")))

    assert _SSN in out["sanitized_prompt"]
    assert out["policy"]["policy_mode"] == "passthrough"
    assert out["policy"]["onprem_only"] is True


def test_enforced_tool_calling_path_still_redacts():
    """Same request, enforced -- the per-message redaction is unchanged."""
    pipe = _pipeline(_settings("regex"))
    req = ChatRequest(
        messages=[Message(role="user", content=_PROMPT)],
        tools=[{"type": "function",
                "function": {"name": "lookup", "description": "look up",
                             "parameters": {"type": "object", "properties": {}}}}])
    out = asyncio.run(pipe.run(
        "req-tools", "acme", req,
        app_policy=AppPolicyView(residency="onprem_only")))

    assert _SSN not in out["sanitized_prompt"]


# --- the streaming path composes the same detector --------------------------


def test_stream_path_composes_the_same_passthrough_detector():
    """`run_stream` builds its detector through the SAME `_gate_inputs`, so the
    two paths cannot drift. Asserted at the composition seam because an
    `onprem_only` app can never reach the token-streaming path anyway (the
    step-9 runtime floor raises StreamFallback first)."""
    pipe = _pipeline(_settings("regex"))
    ap = AppPolicyView(residency="any", policy_mode="passthrough")
    det, _rules, mode = pipe._gate_inputs(ap, _profile())
    assert mode == "passthrough"
    assert isinstance(det, NullDetector)
    assert det.analyze(f"{_PROMPT} {_MRN}") == []


def test_presidio_global_passthrough_leaves_everything_verbatim():
    """The strongest global posture (Presidio, which tags PERSON/DATE_TIME with
    statistical NER) still touches nothing for a passthrough app."""
    pytest.importorskip("presidio_analyzer")
    pipe = _pipeline(_settings("presidio"))
    ap = AppPolicyView(residency="onprem_only", policy_mode="passthrough")

    out = _run(pipe, _PROMPT, app_policy=ap)

    assert out["sanitized_prompt"] == _PROMPT
    assert "John Smith" in out["sanitized_prompt"]
    assert out["policy"]["onprem_only"] is True
    assert out["policy"]["allowed_providers"] == ["onprem"]
