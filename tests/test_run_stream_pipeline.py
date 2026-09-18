"""Task 5: real backend token-streaming substrate.

Exercises `Pipeline.run_stream` end to end against the REAL `LiteLLMBackend`
(so the streaming path -- `generate_stream` -> `gateway.run_stream` ->
`pipeline.run_stream` -- actually runs), with `litellm.acompletion` replaced by
a streaming fake (`tests._litellm_fake.make_streaming_acompletion`).

The load-bearing guarantees under test:
  * benign request -> ordered token deltas, step-9 verify NEVER called, final
    metadata carries a non-zero cost and a "waived" verification marker;
  * the runtime floor (Global Constraint 1) raises `StreamFallback` for an
    onprem_only OR require_verification request.
"""
from __future__ import annotations

import asyncio

import pytest

from app.apps.policy import AppPolicyView
from app.compression.ccr import CCRStore
from app.config.loader import load_settings
from app.gateway.backends.litellm_backend import LiteLLMBackend
from app.gateway.gateway import Gateway
from app.models import ChatRequest, Message
from app.pipeline import Pipeline, StreamFallback
from app.policy.detectors import make_detector
from app.policy.gate import Gate
from app.routing.router import Router
from tests import _litellm_fake
from tests._litellm_fake import make_streaming_acompletion, make_test_registry


def _settings(mode: str = "regex"):
    """Portable settings: force the global detector to `regex` so `make_detector`
    never needs the optional presidio extra (consistent with the baseline's 9
    optional-extra skips). regex still catches US_SSN, which the floor tests need."""
    s = load_settings()
    return s.model_copy(update={
        "serving": s.serving.model_copy(update={"pii_detection": mode})})


def _pipeline(settings) -> Pipeline:
    """Real Pipeline wired to the REAL LiteLLMBackend (not the deterministic
    fake), so `generate_stream` runs and consumes the monkeypatched streaming
    `acompletion`. Mirrors tests/test_pii_detection_pipeline.py otherwise."""
    det = make_detector(settings.serving.pii_detection)
    registry = make_test_registry(settings)
    co = settings.serving.context_optimization
    gate = Gate(settings, det, registry=registry)
    gateway = Gateway(settings, LiteLLMBackend(settings, registry=registry),
                      Router(settings, registry=registry), registry)
    return Pipeline(settings, gate, det, gateway, co_cfg_provider=lambda: co, ccr=CCRStore(3600),
                    registry=registry)


async def _drain(pipeline: Pipeline, content: str, app_policy=None):
    req = ChatRequest(messages=[Message(role="user", content=content)])
    deltas, get_meta = await pipeline.run_stream(
        "req-1", "demo", req, app_policy=app_policy)
    collected: list[str] = []
    async for d in deltas:
        collected.append(d)
    return collected, get_meta


# --- benign request: real deltas, no verify, waived marker, non-zero cost ---


def test_run_stream_yields_deltas_in_order_no_verify_waived(monkeypatch):
    from app.gateway.backends import litellm_backend

    monkeypatch.setattr(
        litellm_backend, "acompletion",
        make_streaming_acompletion(["Hello", " ", "world", "!"],
                                   prompt_tokens=12, completion_tokens=7))
    _litellm_fake.CALLS.clear()

    # Spy: step-9 verify must NEVER run on the streaming (waived) path.
    import app.pipeline as pipeline_mod
    called = {"verify": False}

    def _boom_verify(*a, **k):
        called["verify"] = True
        raise AssertionError("verify() must not run on the streaming path")

    monkeypatch.setattr(pipeline_mod, "verify", _boom_verify)

    s = _settings()
    pipe = _pipeline(s)

    deltas, meta = asyncio.run(_drain_capture(
        pipe, "Write a short greeting.", AppPolicyView(residency="any")))

    assert deltas == ["Hello", " ", "world", "!"]           # ordered real deltas
    assert called["verify"] is False                        # step 9 skipped
    assert meta["verification"] == "waived"                 # waived marker
    assert meta["cost"]["total_usd"] > 0.0                  # real pricing.yaml cost
    assert meta["raw_completion"] == "Hello world!"         # accumulated text
    assert meta["usage"]["completion_tokens"] == 7          # usage from final chunk
    # the streaming call really asked litellm to stream
    assert _litellm_fake.CALLS and _litellm_fake.CALLS[-1]["stream"] is True
    assert _litellm_fake.CALLS[-1]["stream_options"] == {"include_usage": True}
    assert "logprobs" not in _litellm_fake.CALLS[-1]        # omitted while streaming
    # Regression: `_assemble_stream_meta` must carry the decision certificate
    # (b8b8b2a threaded `det` into `_certificate` at the run()/run() call sites
    # but left this streaming call site referencing an undefined `det`, which
    # raised NameError on every streaming request). The default posture here
    # is `enforced` -> `model_input: redact`, so a detector is actually built
    # and used -- the certificate must name it, not stamp "off".
    assert meta["policy"]["detection_mode"] == s.serving.pii_detection
    assert meta["policy"]["detected_entity_types"] == []


def test_run_stream_certificate_detection_mode_is_off_when_nothing_scanned(
        monkeypatch):
    """The other half of the regression test above, mirroring
    tests/test_decision_certificate.py::test_certificate_detection_mode_is_off_when_nothing_scanned
    for the streaming path: an app in the raw/raw/no-escalation posture gets
    the NullDetector (`_gate_inputs` builds a detector only when some consumer
    needs it), so the certificate must say `detection_mode: "off"`, not the
    app's named (but never-run) detector."""
    from app.gateway.backends import litellm_backend

    monkeypatch.setattr(
        litellm_backend, "acompletion",
        make_streaming_acompletion(["ok"]))

    s = _settings()
    pipe = _pipeline(s)
    nobody_looked = AppPolicyView(
        residency="any", model_input="raw", client_output="raw",
        capture_io="off", escalate_residency_on_detection=False,
        pii_detection="presidio")

    deltas, meta = asyncio.run(_drain_capture(
        pipe, "Write a short greeting.", nobody_looked))

    assert deltas == ["ok"]
    assert meta["policy"]["detection_mode"] == "off"
    assert meta["policy"]["detected_entity_types"] == []


async def _drain_capture(pipeline: Pipeline, content: str, app_policy):
    collected, get_meta = await _drain(pipeline, content, app_policy)
    return collected, get_meta()


def test_get_meta_before_stream_consumed_raises(monkeypatch):
    from app.gateway.backends import litellm_backend

    monkeypatch.setattr(
        litellm_backend, "acompletion",
        make_streaming_acompletion(["a", "b"]))

    s = _settings()
    pipe = _pipeline(s)

    async def _run():
        req = ChatRequest(messages=[Message(role="user", content="hi there")])
        deltas, get_meta = await pipe.run_stream(
            "req-1", "demo", req, app_policy=AppPolicyView(residency="any"))
        with pytest.raises(RuntimeError):
            get_meta()  # not consumed yet
        async for _ in deltas:
            pass
        assert get_meta()["verification"] == "waived"  # populated after drain

    asyncio.run(_run())


# --- runtime floor: StreamFallback (Global Constraint 1) --------------------


def test_former_trigger_phrase_streams_instead_of_falling_back(monkeypatch):
    """"step by step" used to divert to the internal agent and raise
    StreamFallback. With the agent gone it is an ordinary direct request."""
    from app.gateway.backends import litellm_backend

    monkeypatch.setattr(
        litellm_backend, "acompletion", make_streaming_acompletion(["x"]))
    s = _settings()
    pipe = _pipeline(s)

    async def _run():
        req = ChatRequest(messages=[Message(
            role="user", content="Explain step by step how to bake bread.")])
        deltas, get_meta = await pipe.run_stream(
            "req-1", "demo", req, app_policy=AppPolicyView(residency="any"))
        async for _ in deltas:
            pass
        return get_meta()

    meta = asyncio.run(_run())
    assert meta["verification"] == "waived"


def test_stream_fallback_on_onprem_only(monkeypatch):
    """Default config: US_SSN -> restricted rule -> onprem_only True. The floor
    raises StreamFallback (the onprem_only branch of Global Constraint 1)."""
    from app.gateway.backends import litellm_backend

    monkeypatch.setattr(
        litellm_backend, "acompletion", make_streaming_acompletion(["x"]))
    s = _settings()
    pipe = _pipeline(s)

    async def _run():
        req = ChatRequest(messages=[Message(
            role="user", content="my ssn is 482-73-1946, please help")])
        await pipe.run_stream("req-1", "demo", req,
                              app_policy=AppPolicyView(residency="any"))

    with pytest.raises(StreamFallback):
        asyncio.run(_run())


def _settings_require_verify_without_onprem():
    """A settings variant where the US_SSN rule requires verification but is NOT
    onprem_only -- isolates the `require_verification` branch of the floor."""
    s = _settings()
    rules = []
    for r in s.policy.sensitivity_rules:
        if "US_SSN" in r.entities:
            r = r.model_copy(update={"residency": "private_cloud_ok",
                                     "require_verification": True})
        rules.append(r)
    return s.model_copy(update={
        "policy": s.policy.model_copy(update={"sensitivity_rules": rules})})


def test_stream_fallback_on_require_verification_only(monkeypatch):
    """Isolated require_verification branch: onprem_only is False (rule residency
    downgraded to private_cloud_ok) but require_verification is True -> the floor
    still raises StreamFallback."""
    from app.gateway.backends import litellm_backend

    monkeypatch.setattr(
        litellm_backend, "acompletion", make_streaming_acompletion(["x"]))
    s = _settings_require_verify_without_onprem()
    pipe = _pipeline(s)

    async def _run():
        req = ChatRequest(messages=[Message(
            role="user", content="my ssn is 482-73-1946, please help")])
        # residency any so the rule's private_cloud_ok wins -> onprem_only False,
        # leaving require_verification as the sole reason to fall back.
        deltas, _ = await pipe.run_stream(
            "req-1", "demo", req, app_policy=AppPolicyView(residency="any"))
        async for _d in deltas:
            pass

    with pytest.raises(StreamFallback):
        asyncio.run(_run())
