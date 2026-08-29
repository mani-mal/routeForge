"""Feature 3 (DSPy prompt-optimization) Task 2: the optional step-4 LLM
task-mode classifier (`app/routing/task_mode_llm.py::classify_task_mode`),
wired into `app/pipeline.py` behind `routing.prompt_opt.enabled` +
`active_prompts["task_mode_classifier"]`.

Covers all 7 invariants from the task brief:
  1. default OFF -> unchanged (rules path, no classifier DecisionRecord)
  2. enabled happy path -> DecisionRecord w/ chosen model + cost
  3. RESIDENCY: onprem_only + classifier enabled + cloud in the pool
     -> only onprem ever called (spy backend), load-bearing
  4. RESIDENCY exhaustion: onprem_only + no healthy onprem
     -> NoEligibleCandidate -> falls back to rules, cloud never called
  5. fail-safe: backend error / unparseable completion -> rules fallback
  6. sanitized input: the backend receives sanitized text, never raw PII
  7. cost visibility: the classifier's DecisionRecord (and its cost) reach
     the SAME trace the pipeline returns

Tests 2/4/5/6 exercise `classify_task_mode` directly (a Router + a small
scripted fake backend give exact control over the completion/failure and
let us assert the backend was -- or was NOT -- ever called at all, which is
the strongest possible residency-exhaustion assertion). Tests 1/3/7 go
through the full `Pipeline` (mirroring tests/test_traffic_honesty.py's
`RecordingBackend` pattern) so the wiring in app/pipeline.py is exercised
end-to-end, not just the unit.
"""
from __future__ import annotations

import asyncio

import pytest

from app.apps.db import Database
from app.apps.registry import ModelRegistry
from app.compression.ccr import CCRStore
from app.config.loader import load_settings
from app.config.schema import Settings
from app.gateway.gateway import Gateway
from app.models import ChatRequest, ExecutionPolicyContext, GenerationResult, Message
from app.pipeline import Pipeline
from app.policy.detectors import make_detector
from app.policy.gate import Gate
from app.prompts.store import PromptArtifact
from app.routing.router import Router
from app.routing.task_mode import decide
from app.routing.task_mode_llm import classify_task_mode
from tests._litellm_fake import _DeterministicLiteLLM, make_test_registry

ARTIFACT = PromptArtifact(
    id="task_mode_classifier", label="v1", verified=True,
    text="Answer with exactly one word: direct or multi_step.")


# ---------------------------------------------------------------------------
# Shared fixtures / helpers
# ---------------------------------------------------------------------------

class _ScriptedBackend:
    """Minimal `InferenceBackend`-shaped fake with full control over the
    completion text or a forced failure. Records every call's model +
    messages so tests can assert what was actually sent (invariant 6) and
    whether it was ever called at all (invariant 4)."""
    name = "scripted"

    def __init__(self, *, text: str | None = None, raise_exc: Exception | None = None,
                 cost_usd: float = 0.0005) -> None:
        self.text = text
        self.raise_exc = raise_exc
        self.cost_usd = cost_usd
        self.calls: list[dict] = []

    async def generate(self, model, messages, price, tier, hint, tools=None,
                       tool_choice=None):
        self.calls.append({"model": model, "messages": messages, "tier": tier})
        if self.raise_exc is not None:
            raise self.raise_exc
        return GenerationResult(
            model=model, text=self.text or "", tokens_in=8, tokens_out=2,
            latency_ms=5.0, cost_usd=self.cost_usd, confidence=0.9)

    def generate_stream(self, *a, **k):
        raise NotImplementedError

    async def health(self):
        raise NotImplementedError


class RecordingBackend:
    """Wraps `_DeterministicLiteLLM`, recording every model it's asked to
    generate for -- the SAME pattern tests/test_traffic_honesty.py uses so
    residency is asserted on what the backend actually saw, not on the
    DecisionRecord's own account of itself."""
    name = "recording"

    def __init__(self, inner: _DeterministicLiteLLM) -> None:
        self.inner = inner
        self.models_called: list[str] = []

    async def generate(self, model, messages, price, tier, hint, tools=None,
                       tool_choice=None):
        self.models_called.append(model)
        return await self.inner.generate(model, messages, price, tier, hint)

    def generate_stream(self, *a, **k):
        raise NotImplementedError

    async def health(self):
        raise NotImplementedError


def _run(coro):
    return asyncio.run(coro)


def _ctx(providers, *, tenant_id="t", risk="low", onprem=False,
         require_verification=False) -> ExecutionPolicyContext:
    return ExecutionPolicyContext(
        request_id="r", tenant_id=tenant_id, sensitivity="public",
        onprem_only=onprem, allowed_providers=frozenset(providers),
        allowed_models=frozenset(), max_cost_usd=1.0, max_latency_ms=15000,
        risk_class=risk, require_verification=require_verification,
        detected_entities=(), redaction_map_id=None)


async def _empty_registry(settings: Settings) -> ModelRegistry:
    db = Database(url="sqlite+aiosqlite://")
    await db.ensure_ready()
    reg = ModelRegistry(settings)
    await reg.refresh(db)
    return reg


def _with_classifier_enabled(settings: Settings) -> Settings:
    """Enables the classifier + pins the shipped artifact, WITHOUT touching
    on-disk config -- mirrors test_traffic_honesty.py's `_with_bedrock_enabled`
    recipe."""
    prompt_opt = settings.routing.prompt_opt.model_copy(
        update={"enabled": True, "active": {"task_mode_classifier": "v1"}})
    routing = settings.routing.model_copy(update={"prompt_opt": prompt_opt})
    return settings.model_copy(update={
        "routing": routing,
        "active_prompts": {"task_mode_classifier": ARTIFACT}})


def _with_bedrock_enabled(settings: Settings) -> Settings:
    prov = dict(settings.serving.providers)
    prov["bedrock"] = prov["bedrock"].model_copy(update={"enabled": True})
    return settings.model_copy(
        update={"serving": settings.serving.model_copy(update={"providers": prov})})


def _pipeline(backend, settings: Settings, registry: ModelRegistry) -> Pipeline:
    det = make_detector("regex")
    co = settings.serving.context_optimization
    gateway = Gateway(settings, backend, Router(settings, registry=registry), registry)
    return Pipeline(settings, Gate(settings, det, registry=registry), det, gateway,
                    self_port=8080, co_cfg_provider=lambda: co, ccr=CCRStore(3600),
                    registry=registry)


def _provider_of(model: str, settings: Settings, registry: ModelRegistry) -> str:
    pool = registry.effective_pool()
    return {c.name: c.provider for c in pool}.get(model, "?")


# ---------------------------------------------------------------------------
# 1. Default OFF -> unchanged
# ---------------------------------------------------------------------------

def test_default_off_no_classifier_record_and_rules_path_runs():
    settings = load_settings()
    # The shipped config: master switch off, nothing pinned.
    assert settings.routing.prompt_opt.enabled is False
    assert "task_mode_classifier" not in settings.active_prompts

    registry = make_test_registry(settings)
    rec = RecordingBackend(_DeterministicLiteLLM(settings))
    pipe = _pipeline(rec, settings, registry)
    req = ChatRequest(messages=[Message(role="user", content="What is 2+2?")])
    out = _run(pipe.run("r1", "acme", req))

    assert all(r["step"] != "task_mode_classifier" for r in out["trace"])


# ---------------------------------------------------------------------------
# 2. Enabled happy path
# ---------------------------------------------------------------------------

def test_enabled_happy_path_multistep_verdict_with_cost():
    settings = load_settings()
    reg = make_test_registry(settings)
    router = Router(settings, registry=reg)
    backend = _ScriptedBackend(text="multi_step", cost_usd=0.0007)
    ctx = _ctx({"onprem", "azure", "bedrock"}, tenant_id="acme", risk="low")
    request = ChatRequest(messages=[Message(role="user", content="look something up then act on it")])

    mode, crec = _run(classify_task_mode(
        ctx, "look something up then act on it", ARTIFACT, request,
        router, backend, settings, reg))

    assert mode == "multi_step"
    assert crec.step == "task_mode_classifier"
    assert crec.chosen is not None
    assert crec.actual_cost_usd == pytest.approx(0.0007)
    assert "llm classifier" in crec.reason
    assert "multi_step" in crec.reason


# ---------------------------------------------------------------------------
# 3. RESIDENCY (load-bearing): onprem_only, cloud in the pool, healthy
#    onprem candidate -> classifier's routed call selects onprem; cloud
#    backend is NEVER invoked, for classification OR for serving.
# ---------------------------------------------------------------------------

def test_residency_onprem_only_classifier_never_reaches_cloud():
    settings = _with_classifier_enabled(_with_bedrock_enabled(load_settings()))
    registry = make_test_registry(settings)  # local-8b/local-32b onprem, healthy
    rec = RecordingBackend(_DeterministicLiteLLM(settings))
    pipe = _pipeline(rec, settings, registry)

    req = ChatRequest(messages=[
        Message(role="user", content="My SSN is 482-73-1946, please summarize this.")])
    out = _run(pipe.run("r1", "hospital", req))

    assert out["policy"]["onprem_only"] is True
    classifier_records = [r for r in out["trace"] if r["step"] == "task_mode_classifier"]
    assert len(classifier_records) == 1, out["trace"]
    # The classifier's own DecisionRecord never names a cloud provider...
    assert classifier_records[0]["chosen_provider"] in (None, "onprem")
    # ...and, the stronger assertion: the backend itself was never asked to
    # generate for a cloud model, for the classification call OR the served
    # response -- not just what the DecisionRecord claims about itself.
    assert rec.models_called, "expected at least one model to have been called"
    assert all(_provider_of(m, settings, registry) == "onprem" for m in rec.models_called), (
        rec.models_called)


# ---------------------------------------------------------------------------
# 4. RESIDENCY exhaustion: onprem_only + NO healthy onprem candidate ->
#    Router.select raises NoEligibleCandidate -> falls back to rules
#    decide(), and the backend is NEVER called at all (not even attempted).
# ---------------------------------------------------------------------------

def test_residency_exhaustion_falls_back_to_rules_backend_never_called():
    settings = load_settings()
    reg = _run(_empty_registry(settings))  # zero onboarded rows
    router = Router(settings, registry=reg)
    backend = _ScriptedBackend(text="multi_step")  # would prove a cloud-style leak if ever called
    ctx = _ctx({"onprem"}, tenant_id="hospital", risk="high", onprem=True,
              require_verification=True)
    request = ChatRequest(messages=[Message(role="user", content="What is 2+2?")])

    mode, crec = _run(classify_task_mode(
        ctx, "What is 2+2?", ARTIFACT, request, router, backend, settings, reg))

    assert mode == decide(ctx, request)  # the safe rules verdict
    assert backend.calls == [], "classifier must not reach a backend when exhausted"
    assert crec.chosen is None
    assert crec.step == "task_mode_classifier"
    assert "no eligible candidate" in crec.reason
    assert "rules fallback" in crec.reason


# ---------------------------------------------------------------------------
# 5. Fail-safe: backend error, and separately an unparseable completion.
# ---------------------------------------------------------------------------

def test_backend_error_falls_back_to_rules_no_crash():
    settings = load_settings()
    reg = make_test_registry(settings)
    router = Router(settings, registry=reg)
    backend = _ScriptedBackend(raise_exc=RuntimeError("provider unreachable"))
    ctx = _ctx({"onprem", "azure", "bedrock"}, tenant_id="acme")
    request = ChatRequest(messages=[Message(role="user", content="hello")])

    mode, crec = _run(classify_task_mode(
        ctx, "hello", ARTIFACT, request, router, backend, settings, reg))

    assert mode == decide(ctx, request)
    assert "backend call failed" in crec.reason
    assert "rules fallback" in crec.reason
    # A candidate WAS selected (residency held), the call just failed.
    assert crec.chosen is not None


def test_unparseable_completion_falls_back_to_rules_no_crash():
    settings = load_settings()
    reg = make_test_registry(settings)
    router = Router(settings, registry=reg)
    backend = _ScriptedBackend(text="I dunno, maybe?", cost_usd=0.0003)
    ctx = _ctx({"onprem", "azure", "bedrock"}, tenant_id="acme")
    request = ChatRequest(messages=[Message(role="user", content="hello")])

    mode, crec = _run(classify_task_mode(
        ctx, "hello", ARTIFACT, request, router, backend, settings, reg))

    assert mode == decide(ctx, request)
    assert "unparseable" in crec.reason
    assert "rules fallback" in crec.reason
    # The real (failed-to-parse) call still cost something -- honesty: don't
    # drop the cost just because the verdict was unusable.
    assert crec.actual_cost_usd == pytest.approx(0.0003)


# ---------------------------------------------------------------------------
# 6. Sanitized input: the classifier receives sanitized text, never raw PII.
# ---------------------------------------------------------------------------

def test_classifier_receives_sanitized_text_never_raw_pii():
    settings = load_settings()
    reg = make_test_registry(settings)
    router = Router(settings, registry=reg)
    backend = _ScriptedBackend(text="direct")
    ctx = _ctx({"onprem", "azure", "bedrock"}, tenant_id="acme")
    raw_content = "My SSN is 482-73-1946, please help."
    sanitized_content = "My SSN is <US_SSN>, please help."
    request = ChatRequest(messages=[Message(role="user", content=raw_content)])

    _run(classify_task_mode(
        ctx, sanitized_content, ARTIFACT, request, router, backend, settings, reg))

    assert len(backend.calls) == 1
    sent = backend.calls[0]["messages"]
    joined = "\n".join(m.content for m in sent)
    assert "482-73-1946" not in joined
    assert sanitized_content in joined
    assert ARTIFACT.text in joined  # the managed instruction went along with it


# ---------------------------------------------------------------------------
# 7. Cost visibility: the classifier's DecisionRecord (and its cost) reach
#    the SAME trace the pipeline returns.
# ---------------------------------------------------------------------------

def test_classifier_cost_reaches_pipeline_trace_and_total():
    settings = _with_classifier_enabled(load_settings())
    registry = make_test_registry(settings)
    rec = RecordingBackend(_DeterministicLiteLLM(settings))
    pipe = _pipeline(rec, settings, registry)
    req = ChatRequest(messages=[Message(role="user", content="What is the capital of France?")])

    out = _run(pipe.run("r1", "acme", req))

    classifier_records = [r for r in out["trace"] if r["step"] == "task_mode_classifier"]
    assert len(classifier_records) == 1, out["trace"]
    crec = classifier_records[0]
    # _DeterministicLiteLLM's canned text never matches "direct"/"multi_step",
    # so this exercises the fallback branch too -- and still must carry cost.
    assert crec["actual_cost_usd"] is not None
    assert crec["actual_cost_usd"] > 0
    assert out["cost"]["total_usd"] >= crec["actual_cost_usd"] - 1e-12
