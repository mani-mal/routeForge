"""Task 3: per-app `pii_detection` wiring into `Pipeline`.

The load-bearing invariant: an app whose `pii_detection` is downgraded to
"off" must NOT be able to leak to a cloud provider. Detection mode only ever
changes WHAT gets redacted -- the onprem_only / allowed_providers floor comes
from `app_policy.residency` in `app/policy/context.py` and never consults
`pii_detection` at all. See CLAUDE.md rule 3 ("`onprem_only` is absolute").
"""
from __future__ import annotations

import asyncio

import pytest

from app.apps.policy import AppPolicyView
from app.compression.ccr import CCRStore
from app.config.loader import load_settings
from app.gateway.gateway import Gateway
from app.models import ChatRequest, Message
from app.pipeline import Pipeline
from app.policy.detectors import RegexDetector, make_detector
from app.policy.enums import PII_MODES
from app.policy.gate import Gate
from app.routing.router import Router
from tests._litellm_fake import _DeterministicLiteLLM, make_test_registry


def _settings_with_global_mode(mode: str):
    s = load_settings()
    return s.model_copy(update={
        "serving": s.serving.model_copy(update={"pii_detection": mode})})


def _pipeline(settings) -> Pipeline:
    """Build a real Pipeline (deterministic backend, seeded on-prem models)
    the same way tests/test_two_lever_cost.py does, so `pipeline.run()`
    actually exercises the app/pipeline.py detector-composition code under
    test instead of a re-implementation of it in the test."""
    det = make_detector(settings.serving.pii_detection)
    registry = make_test_registry(settings)
    co = settings.serving.context_optimization
    gate = Gate(settings, det, registry=registry)
    gateway = Gateway(settings, _DeterministicLiteLLM(settings),
                      Router(settings, registry=registry), registry)
    return Pipeline(settings, gate, det, gateway, self_port=8080,
                    co_cfg_provider=lambda: co, ccr=CCRStore(3600),
                    registry=registry)


def _run(pipeline: Pipeline, content: str, app_policy=None) -> dict:
    req = ChatRequest(messages=[Message(role="user", content=content)])
    return asyncio.run(pipeline.run("req-1", "acme", req, app_policy=app_policy))


# --- _detector_for: inherit-global + memoization ----------------------------


def test_detector_for_none_inherits_global_instance():
    """No override (app_policy is None or pii_detection is None) -> the SAME
    global detector object, not a new instance."""
    s = _settings_with_global_mode("regex")
    pipe = _pipeline(s)
    assert pipe._detector_for(None) is pipe._detector


def test_detector_for_mode_matching_global_returns_global_instance():
    s = _settings_with_global_mode("regex")
    pipe = _pipeline(s)
    assert pipe._detector_for("regex") is pipe._detector


def test_detector_for_memoizes_non_global_mode():
    """A non-global mode is built once via make_detector(), then the SAME
    cached object is returned on every later call -- Presidio is expensive
    to construct and must never be rebuilt per request."""
    s = _settings_with_global_mode("off")
    pipe = _pipeline(s)
    assert "regex" not in pipe._detectors  # not built yet
    d1 = pipe._detector_for("regex")
    assert isinstance(d1, RegexDetector)
    assert "regex" in pipe._detectors
    d2 = pipe._detector_for("regex")
    assert d2 is d1  # memoized, not rebuilt
    # global mode itself was pre-seeded into the memo at construction time
    assert pipe._detectors[s.serving.pii_detection] is pipe._detector


def test_detector_for_covers_every_pii_mode():
    """Every mode in the canonical vocabulary resolves to a live detector,
    not just the two exercised elsewhere in this file."""
    s = _settings_with_global_mode("off")
    pipe = _pipeline(s)
    for mode in PII_MODES:
        if mode == "presidio":
            pytest.importorskip("presidio_analyzer")
        det = pipe._detector_for(mode)
        assert det is not None
        assert det.analyze("plain text") == []  # sanity: doesn't explode


# --- invariant: pii_detection="off" cannot leak onprem_only to cloud -------


def test_pii_off_app_still_forces_onprem_only_and_excludes_cloud_but_leaks_nothing_redacted():
    """The brief's load-bearing case, as specified: global detection is
    Presidio (so PERSON would normally be caught), the app downgrades to
    "off", and residency is onprem_only. Detection-off must NOT be able to
    widen allowed_providers to include cloud -- and, as the direct
    consequence of actually running the "off" detector, the PERSON entity
    passes through un-redacted, proving the two concerns are independent."""
    pytest.importorskip("presidio_analyzer")
    s = _settings_with_global_mode("presidio")
    prov = dict(s.serving.providers)
    prov["azure"] = prov["azure"].model_copy(update={"enabled": True})
    prov["bedrock"] = prov["bedrock"].model_copy(update={"enabled": True})
    s = s.model_copy(update={"serving": s.serving.model_copy(update={"providers": prov})})
    pipe = _pipeline(s)
    app_policy = AppPolicyView(residency="onprem_only", pii_detection="off")

    out = _run(pipe, "My name is John Smith, please help me plan a trip.",
              app_policy=app_policy)

    assert out["policy"]["onprem_only"] is True
    assert "azure" not in out["policy"]["allowed_providers"]
    assert "bedrock" not in out["policy"]["allowed_providers"]
    assert "John Smith" in out["sanitized_prompt"]  # detection off -> nothing caught/redacted


def test_pii_off_app_forces_onprem_only_and_leaves_ssn_unredacted():
    """Same invariant, but portable to environments without the optional
    presidio extra: with pii_detection="off" an entity the global mode
    (regex) would normally catch (US_SSN) also passes straight through,
    while residency is still forced to onprem_only/no-cloud purely from
    app_policy.residency."""
    s = _settings_with_global_mode("regex")
    pipe = _pipeline(s)
    app_policy = AppPolicyView(residency="onprem_only", pii_detection="off")

    out = _run(pipe, "my ssn is 482-73-1946, please help", app_policy=app_policy)

    assert out["policy"]["onprem_only"] is True
    assert out["policy"]["allowed_providers"] == ["onprem"]
    assert "482-73-1946" in out["sanitized_prompt"]  # detection off -> SSN not redacted


def test_pii_regex_app_catches_ssn_but_not_person_or_datetime():
    """pii_detection="regex" on the app, overriding a global mode of "off":
    the app-selected regex detector still catches US_SSN (a pattern it knows)
    but has no PERSON/DATE_TIME recognizer, so both pass through untouched."""
    s = _settings_with_global_mode("off")
    pipe = _pipeline(s)
    app_policy = AppPolicyView(pii_detection="regex")

    out = _run(
        pipe,
        "My name is John Smith, born 1990-01-01, SSN 482-73-1946.",
        app_policy=app_policy)

    sanitized = out["sanitized_prompt"]
    assert "John Smith" in sanitized       # PERSON: regex has no such recognizer
    assert "1990-01-01" in sanitized       # DATE_TIME: regex has no such recognizer
    assert "482-73-1946" not in sanitized  # US_SSN: regex catches this
    assert "<US_SSN_1>" in sanitized


def test_pii_none_inherits_global_detector_end_to_end():
    """app_policy.pii_detection is None (no explicit AppPolicyView.pii_detection
    override) -> the global detector runs, exactly as if no app_policy detector
    override existed at all."""
    s = _settings_with_global_mode("regex")
    pipe = _pipeline(s)
    app_policy = AppPolicyView()  # pii_detection defaults to None (inherit)

    out = _run(pipe, "my ssn is 482-73-1946, ok", app_policy=app_policy)

    assert "482-73-1946" not in out["sanitized_prompt"]
    assert "<US_SSN_1>" in out["sanitized_prompt"]


# --- regression: engineering labels survive an onprem_only + pii=off app ----

# The exact NC-table prompt from the field report (2026-09-04): CAD drawing
# attribute tags on roll drawings, each naming a machining dimension. Under
# Presidio, spaCy's statistical NER tags `HARD2` and `SOFT1` as PERSON (0.85)
# and the value `42363` as DATE_TIME (0.85) -- purely context-dependent, so the
# same labels in a short prose sentence come back clean. Nothing errors: the
# model answers confidently about a dimension whose identity has been replaced
# by `<PERSON_1>` while its value survives. Silent data corruption, and
# undetectable from inside the calling application.
_NC_TABLE = (
    'List these keys exactly, one per line:\n'
    '{"HARD1": "71.631°", "HARD2": "1.5105", "HARD3": "1.662", '
    '"HARD4": ".094", "HARD5": "6.940", "HARD6": "9.883", "HARD7": "6.500", '
    '"HARD8": ".2490", "HARD9": "42363", "SOFT1": "71.631", "SOFT2": "1.5055", '
    '"SOFT3": "1.657", "SOFT4": ".094", "SOFT5": "9.913", "SOFT6": ".068", '
    '"SOFT7": "3.250", "SOFT8": ".000", "SOFT9": ".2500", "SOFT10": ""}'
)
_NC_LABELS = [f"HARD{i}" for i in range(1, 10)] + [f"SOFT{i}" for i in range(1, 11)]


def test_engineering_labels_survive_gate_for_onprem_only_pii_off_app():
    """An `onprem_only` app with `pii_detection="off"` -- the configuration the
    admin clamp used to forbid -- passes every NC-table label and value through
    verbatim, while residency stays pinned on-prem.

    This is the shape a document-extraction app needs: no third party to
    protect (the model is inside the customer's boundary) and every field on
    the drawing is load-bearing output it is contractually required to
    reproduce. Asserted on the "off" path rather than on spaCy's tagging so the
    test states the guarantee, not the model version's false positives."""
    s = _settings_with_global_mode("presidio")
    pipe = _pipeline(s)
    app_policy = AppPolicyView(residency="onprem_only", pii_detection="off")

    out = _run(pipe, _NC_TABLE, app_policy=app_policy)

    for label in _NC_LABELS:
        assert f'"{label}"' in out["sanitized_prompt"], f"{label} did not survive the gate"
    assert "42363" in out["sanitized_prompt"]      # HARD9's value, tagged DATE_TIME
    assert "PERSON" not in out["sanitized_prompt"]  # no placeholder spliced in
    # ...and turning the gate off did not buy that at the cost of residency.
    assert out["policy"]["onprem_only"] is True
    assert out["policy"]["allowed_providers"] == ["onprem"]
