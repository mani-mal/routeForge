"""Feature 5, Task 2 -- persistent memory wired into the pipeline (direct
path): READ after san_messages/before compress(), WRITE after step-9 verify()
passes. Exercises the residency contract in
docs/architecture/specs/persistent-memory-residency-spec.md (R1-R7) end to end
through a real `Pipeline`, mirroring tests/test_pii_detection_pipeline.py's
fixture pattern (deterministic backend, seeded on-prem models) so these tests
actually exercise app/pipeline.py and app/memory/__init__.py under test,
not a re-implementation of them.
"""
from __future__ import annotations

import asyncio

import pytest

from app.apps.policy import AppPolicyView
from app.compression.ccr import CCRStore
from app.config.loader import load_settings
from app.gateway.gateway import Gateway
from app.memory.store import DeterministicMemoryStore
from app.models import ChatRequest, MemoryRecord, Message
from app.pipeline import Pipeline
from app.policy.detectors import make_detector
from app.policy.gate import Gate
from app.routing.router import Router
from tests._litellm_fake import _DeterministicLiteLLM, make_test_registry


def _mem_settings(mode: str = "regex", enabled: bool = True, min_tokens: int = 0,
                  bypass_high_risk: bool = True, bypass_restricted: bool = True):
    """Portable settings (regex detector, no optional presidio extra needed --
    regex still catches US_SSN, which several invariants below drive) with
    `serving.memory` overridden for the test."""
    s = load_settings()
    mem = s.serving.memory.model_copy(update={
        "enabled": enabled, "min_tokens": min_tokens,
        "bypass_on": s.serving.memory.bypass_on.model_copy(update={
            "high_risk": bypass_high_risk,
            "restricted_sensitivity": bypass_restricted})})
    return s.model_copy(update={"serving": s.serving.model_copy(update={
        "pii_detection": mode, "memory": mem})})


class _SpyBackend(_DeterministicLiteLLM):
    """Records the exact messages handed to the backend on each call, so
    tests can assert what content actually reached the model -- not just what
    the trace claims."""

    def __init__(self, settings):
        super().__init__(settings)
        self.calls: list[list[Message]] = []

    async def generate(self, model, messages, price, tier, hint, tools=None,
                       tool_choice=None, params=None):
        self.calls.append(list(messages))
        return await super().generate(model, messages, price, tier, hint, tools=tools)


def _pipeline(settings, memory=None, backend=None) -> Pipeline:
    """Real Pipeline (deterministic backend, seeded on-prem models), mirroring
    tests/test_pii_detection_pipeline.py::_pipeline, with a `memory` store
    threaded in (default None, matching app/main.py's parameter default)."""
    det = make_detector(settings.serving.pii_detection)
    registry = make_test_registry(settings)
    co = settings.serving.context_optimization
    gate = Gate(settings, det, registry=registry)
    be = backend if backend is not None else _DeterministicLiteLLM(settings)
    gateway = Gateway(settings, be, Router(settings, registry=registry), registry)
    return Pipeline(settings, gate, det, gateway, co_cfg_provider=lambda: co, ccr=CCRStore(3600),
                    registry=registry, memory=memory)


def _run(pipeline: Pipeline, content: str, *, tenant: str = "acme",
        app_policy=None, request_id: str = "req-1",
        session_id: str | None = None) -> dict:
    req = ChatRequest(messages=[Message(role="user", content=content)])
    return asyncio.run(pipeline.run(request_id, tenant, req, app_policy=app_policy,
                                    session_id=session_id))


def _mem_trace(out: dict) -> dict | None:
    return next((t for t in out["trace"] if t.get("step") == "memory"), None)


# --- 1. default OFF = unchanged --------------------------------------------


def test_default_off_no_recall_no_write_no_trace_record():
    s = _mem_settings(enabled=False)
    store = DeterministicMemoryStore(ttl_seconds=3600, clock=lambda: 0.0)
    pipe = _pipeline(s, memory=store)

    out = _run(pipe, "hello there, just chatting")

    assert _mem_trace(out) is None
    assert store.size == 0  # nothing written despite a real store being wired


# --- 2. round-trip continuity ------------------------------------------------


def test_round_trip_continuity_same_tenant_recalls_prior_write():
    s = _mem_settings()
    store = DeterministicMemoryStore(ttl_seconds=3600, clock=lambda: 0.0)
    spy = _SpyBackend(s)
    pipe = _pipeline(s, memory=store, backend=spy)

    _run(pipe, "My favorite color is blue.", tenant="acme")
    assert store.size == 1

    out2 = _run(pipe, "What did I just tell you?", tenant="acme")

    mem_trace = _mem_trace(out2)
    assert mem_trace is not None
    assert mem_trace["recalled_count"] >= 1
    assert mem_trace["tokens_recalled"] > 0
    joined = "\n".join(m.content for m in spy.calls[-1])
    assert "blue" in joined  # recalled context actually reached the backend


# --- 3. tenant isolation end-to-end (LOAD-BEARING) --------------------------


def test_tenant_isolation_end_to_end_no_cross_tenant_recall():
    s = _mem_settings()
    store = DeterministicMemoryStore(ttl_seconds=3600, clock=lambda: 0.0)
    spy = _SpyBackend(s)
    pipe = _pipeline(s, memory=store, backend=spy)

    _run(pipe, "Tenant A secret: the launch code is Falcon-9.", tenant="acme")
    out_b = _run(pipe, "What do you know about me?", tenant="beta")  # unrelated tenant

    mem_trace = _mem_trace(out_b)
    assert mem_trace is not None
    assert mem_trace["recalled_count"] == 0
    joined = "\n".join(m.content for m in spy.calls[-1])
    assert "Falcon-9" not in joined
    assert "launch code" not in joined


# --- 4. onprem_only stays local (R3) ----------------------------------------


def test_onprem_only_uses_memory_locally_retrievals_to_cloud_zero():
    """Isolates the R3 branch: app_policy forces onprem_only directly (not
    via a detected entity), so sensitivity stays "public" and risk_class
    stays "low" -- neither safety bypass fires, proving onprem_only by itself
    is NOT a memory bypass condition, and that recall/write use the store
    normally while never crossing the residency boundary."""
    s = _mem_settings()
    store = DeterministicMemoryStore(ttl_seconds=3600, clock=lambda: 0.0)
    pipe = _pipeline(s, memory=store)
    app_policy = AppPolicyView(residency="onprem_only")

    out1 = _run(pipe, "Remember that my project name is Nova.",
               tenant="acme", app_policy=app_policy)
    assert out1["policy"]["onprem_only"] is True
    assert store.size == 1  # write was NOT bypassed for onprem_only

    out2 = _run(pipe, "What's my project name?", tenant="acme", app_policy=app_policy)

    mem_trace = _mem_trace(out2)
    assert mem_trace is not None
    assert mem_trace["recalled_count"] >= 1
    assert mem_trace["retrievals_to_cloud"] == 0
    assert mem_trace["recall_stayed_local"] is True
    # Structural (R3): the store has no cloud path at all -- the caller-side
    # discipline (never to_cloud=True) is corroborated by the store's own
    # honesty counter staying pinned at 0 across both calls above.
    assert store.retrievals_to_cloud == 0


# --- 5. sanitized-only WRITE (R1) -------------------------------------------


def test_write_uses_sanitized_content_only_not_raw_pii():
    """Bypass safety flags turned OFF deliberately, so the write actually
    happens on this high-risk/restricted request -- isolating the CONTENT
    guarantee (R1) from the bypass mechanism (invariant 7)."""
    s = _mem_settings(bypass_high_risk=False, bypass_restricted=False)
    store = DeterministicMemoryStore(ttl_seconds=3600, clock=lambda: 0.0)
    pipe = _pipeline(s, memory=store)

    out = _run(pipe, "my ssn is 482-73-1946, please help", tenant="acme")
    assert "<US_SSN_1>" in out["sanitized_prompt"]
    assert "482-73-1946" not in out["sanitized_prompt"]

    stored = store.recall(tenant_id="acme", application_id="acme", session_id="",
                          max_items=5)
    assert len(stored) == 1
    assert "482-73-1946" not in stored[0]  # raw SSN never written
    assert "<US_SSN_1>" in stored[0]       # the gate's redaction placeholder IS
    assert "482-73-1946" not in out["raw_completion"]  # sanity: mock backend never echoed it


# --- 6. never exported (R4) -------------------------------------------------


def test_memory_trace_record_is_counts_only():
    s = _mem_settings()
    store = DeterministicMemoryStore(ttl_seconds=3600, clock=lambda: 0.0)
    pipe = _pipeline(s, memory=store)

    out = _run(pipe, "hello, just making conversation", tenant="acme")

    mem_trace = _mem_trace(out)
    assert mem_trace is not None
    assert set(mem_trace.keys()) == set(MemoryRecord.model_fields.keys())
    forbidden = {"text", "content", "summary", "recalled_text", "recalled_items",
                "entries", "memory"}
    assert not (set(mem_trace.keys()) & forbidden)


# --- 7. bypass ---------------------------------------------------------------


def test_restricted_sensitivity_bypasses_memory_recall_and_write():
    s = _mem_settings()  # default bypass_on: high_risk=True, restricted=True
    store = DeterministicMemoryStore(ttl_seconds=3600, clock=lambda: 0.0)
    store.write("prior context mentioning Project Zephyr", tenant_id="acme",
               application_id="acme", session_id="")
    spy = _SpyBackend(s)
    pipe = _pipeline(s, memory=store, backend=spy)

    out = _run(pipe, "my ssn is 482-73-1946, please help", tenant="acme")

    assert _mem_trace(out) is None          # bypassed -- no trace entry at all
    assert store.size == 1                  # unchanged: nothing written
    joined = "\n".join(m.content for m in spy.calls[-1])
    assert "Project Zephyr" not in joined    # recall bypassed, never merged in


def test_below_min_tokens_bypasses_memory():
    s = _mem_settings(min_tokens=100000)
    store = DeterministicMemoryStore(ttl_seconds=3600, clock=lambda: 0.0)
    pipe = _pipeline(s, memory=store)

    out = _run(pipe, "hi", tenant="acme")

    assert _mem_trace(out) is None
    assert store.size == 0


# --- 8. cross-session isolation WITHIN one tenant (fix round 1) -------------
#
# Regression coverage for the Important review finding: session_id was
# hard-coded to "" in both recall_memory(...) and write_memory(...) calls, so
# distinct end-user SESSIONS of the SAME application/tenant shared a single
# memory partition -- user X's sanitized exchange could be recalled into user
# Y's request within the same app. session_id is now threaded from
# Pipeline.run(..., session_id=...) into both partition keys.


def test_cross_session_isolation_same_tenant_distinct_sessions():
    s = _mem_settings()
    store = DeterministicMemoryStore(ttl_seconds=3600, clock=lambda: 0.0)
    spy = _SpyBackend(s)
    pipe = _pipeline(s, memory=store, backend=spy)

    # Session S1 writes a summary.
    _run(pipe, "My favorite color is blue.", tenant="acme", session_id="S1")
    assert store.size == 1

    # A DIFFERENT session (S2) of the SAME tenant recalls NOTHING -- S1's
    # content must not cross into S2's partition.
    out_s2 = _run(pipe, "What did I just tell you?", tenant="acme",
                 session_id="S2")
    mem_trace_s2 = _mem_trace(out_s2)
    assert mem_trace_s2 is not None
    assert mem_trace_s2["recalled_count"] == 0
    joined_s2 = "\n".join(m.content for m in spy.calls[-1])
    assert "blue" not in joined_s2

    # Back on S1: the earlier write IS recalled.
    out_s1 = _run(pipe, "What did I just tell you?", tenant="acme",
                 session_id="S1")
    mem_trace_s1 = _mem_trace(out_s1)
    assert mem_trace_s1 is not None
    assert mem_trace_s1["recalled_count"] >= 1
    joined_s1 = "\n".join(m.content for m in spy.calls[-1])
    assert "blue" in joined_s1
