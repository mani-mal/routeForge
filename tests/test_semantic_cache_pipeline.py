"""Feature #6 (semantic cache), Task 2: wiring the CHECK/STORE into the
direct pipeline path. Reuses the compression/pii pipeline test fixtures
(`_pipeline`) and the `RecordingBackend` spy from test_traffic_honesty.py so
these are real end-to-end `Pipeline.run()` calls, not a re-implementation of
the wiring under test.

See docs/architecture/specs/semantic-cache-residency-spec.md (C1-C8) and
.superpowers/sdd/semantic-cache/task-2-brief.md for the contract each test
below is named after.
"""
from __future__ import annotations

import asyncio

import pytest

from app.apps.policy import AppPolicyView
from app.apps.privacy import AppPrivacyProfile, SensitivityRuleView
from app.cache.store import DeterministicSemanticCache
from app.compression.ccr import CCRStore
from app.config.loader import load_settings
from app.gateway.gateway import Gateway
from app.models import ChatRequest, Message
from app.pipeline import Pipeline, PipelineRejected
from app.policy.detectors import NullDetector, make_detector
from app.policy.gate import Gate
from app.routing.router import Router
from tests._litellm_fake import _DeterministicLiteLLM, make_test_registry
from tests.test_traffic_honesty import RecordingBackend, _provider_of, _with_bedrock_enabled


class _FixedTextBackend(_DeterministicLiteLLM):
    """Like `_DeterministicLiteLLM`, but the completion TEXT is pinned to a
    caller-supplied string regardless of model/prompt -- used to put
    deliberately PII-bearing / "UNVERIFIABLE"-bearing text under test control
    for the C4 re-verify tests below."""

    def __init__(self, settings, text: str) -> None:
        super().__init__(settings)
        self._text = text

    async def generate(self, model, messages, price, tier, hint, tools=None,
                       tool_choice=None, params=None):
        result = await super().generate(model, messages, price, tier, hint, tools)
        return result.model_copy(update={"text": self._text})


def _base_settings(**cache_overrides):
    """Global pii_detection pinned to "regex" (portable -- no optional
    presidio extra needed) and semantic_cache.enabled forced True, mirroring
    `_settings_with_global_mode` in test_pii_detection_pipeline.py."""
    s = load_settings()
    s = s.model_copy(update={
        "serving": s.serving.model_copy(update={"pii_detection": "regex"})})
    sc = s.serving.semantic_cache.model_copy(update={"enabled": True, **cache_overrides})
    return s.model_copy(update={"serving": s.serving.model_copy(update={"semantic_cache": sc})})


def _pipeline(rec, settings, registry=None, cache=None):
    """Build a real Pipeline (mirrors tests/test_traffic_honesty.py's
    `_pipeline`), additionally wiring a `DeterministicSemanticCache` the way
    app/main.py does. Returns (pipeline, cache) so tests can introspect the
    cache's counters directly."""
    det = make_detector(settings.serving.pii_detection)
    registry = registry or make_test_registry(settings)
    co = settings.serving.context_optimization
    gate = Gate(settings, det, registry=registry)
    gateway = Gateway(settings, rec, Router(settings, registry=registry), registry)
    cache = cache if cache is not None else DeterministicSemanticCache(ttl_seconds=3600)
    pipe = Pipeline(settings, gate, det, gateway, co_cfg_provider=lambda: co, ccr=CCRStore(3600),
                    registry=registry, cache=cache)
    return pipe, cache


def _cache_trace_records(resp: dict) -> list[dict]:
    return [r for r in resp["trace"] if r.get("step") == "cache"]


# --- 1. Default OFF = unchanged --------------------------------------------


def test_default_off_no_check_no_store_no_trace_record_backend_called_normally():
    settings = load_settings()
    assert settings.serving.semantic_cache.enabled is False
    registry = make_test_registry(settings)
    rec = RecordingBackend(_DeterministicLiteLLM(settings))
    pipe, cache = _pipeline(rec, settings, registry)
    req = ChatRequest(messages=[Message(role="user", content="hello there")])

    out1 = asyncio.run(pipe.run("r1", "acme", req))
    out2 = asyncio.run(pipe.run("r2", "acme", req))

    assert "cache_saved_usd" not in out1["cost"]
    assert "cache_saved_usd" not in out2["cost"]
    assert not _cache_trace_records(out1)
    assert not _cache_trace_records(out2)
    assert len(rec.models_called) == 2  # backend called BOTH times -- no short-circuit
    assert cache.size == 0              # cache instance was never touched


# --- 2. Hit skips the model = real saving -----------------------------------


def test_hit_skips_the_model_and_reports_real_saving():
    settings = _base_settings()
    registry = make_test_registry(settings)
    rec = RecordingBackend(_DeterministicLiteLLM(settings))
    pipe, cache = _pipeline(rec, settings, registry)
    req = ChatRequest(messages=[Message(role="user", content="What is the capital of France?")])

    out1 = asyncio.run(pipe.run("r1", "acme", req))
    out2 = asyncio.run(pipe.run("r2", "acme", req))

    assert len(rec.models_called) == 1  # only the first (miss) call reached the backend
    assert out2["cost"]["total_usd"] == 0.0
    assert out2["cost"]["cache_saved_usd"] > 0.0
    # No provider call => nothing reported: NULL in usage_records (final-review
    # I2), never 0, so a hit cannot make a non-reporting endpoint look "reported".
    assert out2["usage"]["cached_prompt_tokens"] is None
    assert out2["choices"][0]["message"]["content"] == out1["choices"][0]["message"]["content"]
    hit_recs = _cache_trace_records(out2)
    assert hit_recs and hit_recs[0]["hit"] is True
    miss_recs = _cache_trace_records(out1)
    assert miss_recs and miss_recs[0]["hit"] is False


# --- 3. RESIDENCY end-to-end (LOAD-BEARING) ---------------------------------


def test_onprem_only_request_never_served_a_cloud_produced_cached_answer():
    settings = _with_bedrock_enabled(_base_settings())
    registry = make_test_registry(settings)
    rec = RecordingBackend(_DeterministicLiteLLM(settings))
    pipe, cache = _pipeline(rec, settings, registry)

    # Same recipe as test_two_lever_cost.py's escalation test: an
    # EMAIL_ADDRESS entity floors risk_class to "medium" (-> mid tier), and
    # mock_confidence=0.1 forces the ONE allowed escalation (mid -> frontier),
    # landing squarely on bedrock-frontier (the only frontier candidate).
    query = "Email john.doe@example.com re: Q3 planning"
    cloud_req = ChatRequest(messages=[Message(role="user", content=query)],
                            mock_confidence=0.1)
    out1 = asyncio.run(pipe.run(
        "r1", "acme", cloud_req, app_policy=AppPolicyView(residency="private_cloud_ok")))
    assert out1["policy"]["onprem_only"] is False
    assert out1["model"] == "bedrock-frontier"
    assert _provider_of(out1["model"], settings, registry) != "onprem"
    calls_after_req1 = len(rec.models_called)  # confidence escalation may call >1 model for req1

    onprem_req = ChatRequest(messages=[Message(role="user", content=query)])
    out2 = asyncio.run(pipe.run(
        "r2", "acme", onprem_req, app_policy=AppPolicyView(residency="onprem_only")))

    assert out2["policy"]["onprem_only"] is True
    assert out2["model"] != "bedrock-frontier"
    assert _provider_of(out2["model"], settings, registry) == "onprem"
    assert not any(r.get("hit") is True for r in _cache_trace_records(out2))
    req2_calls = rec.models_called[calls_after_req1:]
    assert req2_calls, "req2 must have genuinely called a REAL model, not been served from cache"
    assert all(_provider_of(m, settings, registry) == "onprem" for m in req2_calls), req2_calls
    # Structural partitioning: an onprem_only lookup landed in a DIFFERENT
    # partition than the cloud entry, so the residency refusal never had to
    # fire at all -- the honesty counter proves no breach happened.
    assert cache.blocked_cloud_entries == 0
    assert cache.size == 2  # two independent partitions, each with one entry


# --- 4. RE-VERIFY keystone (C4, LOAD-BEARING) -------------------------------


def test_reverify_redacts_with_the_current_apps_detector_never_serves_raw_cached_text():
    settings = _base_settings()
    registry = make_test_registry(settings)
    fixed_text = "Your records show SSN 482-73-1946 on file."
    rec = RecordingBackend(_FixedTextBackend(settings, fixed_text))
    pipe, cache = _pipeline(rec, settings, registry)
    query = "Please check my account status"

    out1 = asyncio.run(pipe.run(
        "r1", "acme", ChatRequest(messages=[Message(role="user", content=query)]),
        app_policy=AppPolicyView(pii_detection="off")))
    # detection off at store time -> nothing caught/redacted -> stored as-is
    assert "482-73-1946" in out1["choices"][0]["message"]["content"]

    out2 = asyncio.run(pipe.run(
        "r2", "acme", ChatRequest(messages=[Message(role="user", content=query)]),
        app_policy=AppPolicyView(pii_detection="regex")))

    served = out2["choices"][0]["message"]["content"]
    assert "482-73-1946" not in served      # RE-VERIFIED with the CURRENT (regex) detector
    assert "<US_SSN_1>" in served
    assert any(r.get("hit") is True for r in _cache_trace_records(out2))
    assert len(rec.models_called) == 1      # served from cache -- no second model call


def test_reverify_leaves_a_raw_client_output_apps_cached_text_byte_identical():
    """The `client_output: raw` direction of the same C4 re-verify (design P1a
    §2.5). A `passthrough` app that NAMES a detector really gets one at intake,
    so the cache-hit path has everything it needs to over-redact -- and must
    not: its cached answer comes back byte-for-byte. A `_serve_from_cache` that
    verified with the intake detector unconditionally would serve `<US_SSN_1>`
    and mangle the customer's own load-bearing data, with the rest of this file
    still green."""
    settings = _base_settings()
    registry = make_test_registry(settings)
    fixed_text = "Your records show SSN 482-73-1946 on file."
    rec = RecordingBackend(_FixedTextBackend(settings, fixed_text))
    pipe, cache = _pipeline(rec, settings, registry)
    ap = AppPolicyView(policy_mode="passthrough", pii_detection="regex")
    query = "Please check my account status"

    # The over-redaction risk is real, not hypothetical: a live detector, and an
    # egress axis that says do not use it.
    det, _rules, eff = pipe._gate_inputs(ap, None)
    assert not isinstance(det, NullDetector)
    assert eff.protection.client_output == "raw"

    out1 = asyncio.run(pipe.run(
        "r1", "acme", ChatRequest(messages=[Message(role="user", content=query)]),
        app_policy=ap))
    out2 = asyncio.run(pipe.run(
        "r2", "acme", ChatRequest(messages=[Message(role="user", content=query)]),
        app_policy=ap))

    assert any(r.get("hit") is True for r in _cache_trace_records(out2))
    assert len(rec.models_called) == 1      # genuinely served from the cache
    assert out2["choices"][0]["message"]["content"] == fixed_text
    assert out2["choices"][0]["message"]["content"] == \
        out1["choices"][0]["message"]["content"]


def test_reverify_unverifiable_cached_text_is_treated_as_a_miss_never_served():
    settings = _base_settings()
    registry = make_test_registry(settings)
    fixed_text = "Status: UNVERIFIABLE due to incomplete data."
    rec = RecordingBackend(_FixedTextBackend(settings, fixed_text))
    pipe, cache = _pipeline(rec, settings, registry)
    query = "Email john.doe@example.com re: verification status"

    # Store-time: the global rule for EMAIL_ADDRESS has require_verification
    # =False -- the "UNVERIFIABLE"-bearing text is stored as-is (nothing to
    # content-check yet).
    out1 = asyncio.run(pipe.run(
        "r1", "acme", ChatRequest(messages=[Message(role="user", content=query)])))
    assert out1["choices"][0]["message"]["content"] == fixed_text

    # Hit-time: SAME query, SAME sensitivity/risk_class/residency (-> SAME
    # cache partition) but this app's EFFECTIVE policy now requires
    # verification for that entity (e.g. policy tightened between calls).
    # verify() sees the literal "UNVERIFIABLE" marker and fails closed.
    strict_profile = AppPrivacyProfile(recognizers=(), rules=(
        SensitivityRuleView(entities=frozenset({"EMAIL_ADDRESS"}), sensitivity="confidential",
                            risk_class="medium", residency="private_cloud_ok",
                            require_verification=True),
    ))
    with pytest.raises(PipelineRejected):
        asyncio.run(pipe.run(
            "r2", "acme", ChatRequest(messages=[Message(role="user", content=query)]),
            profile=strict_profile))

    # Never served un-re-verified: the request fell all the way through to a
    # REAL second generation (which itself failed the SAME content check and
    # was correctly rejected) rather than returning the stale cached text.
    assert len(rec.models_called) == 2


# --- 5. Sanitized-only STORE (C1) -------------------------------------------


def test_store_caches_the_sanitized_query_key_and_verified_redacted_value_only():
    settings = _base_settings()
    registry = make_test_registry(settings)
    fixed_text = "Your SSN on file is 482-73-1946."
    rec = RecordingBackend(_FixedTextBackend(settings, fixed_text))
    pipe, cache = _pipeline(rec, settings, registry)
    raw_query = "My email is john@example.com, please summarize."

    out = asyncio.run(pipe.run(
        "r1", "acme", ChatRequest(messages=[Message(role="user", content=raw_query)])))

    sanitized = out["sanitized_prompt"]
    assert "john@example.com" not in sanitized  # KEY is the SANITIZED query, not raw
    assert "<EMAIL_ADDRESS_1>" in sanitized

    ctx_providers = frozenset(out["policy"]["allowed_providers"])
    onprem_only = out["policy"]["onprem_only"]
    # Looking up by the SANITIZED text succeeds (that is the real key)...
    entry = cache.lookup(tenant_id="acme", onprem_only=onprem_only,
                         allowed_providers=ctx_providers, query=sanitized)
    assert entry is not None
    # ...the VALUE is the verified/redacted response text, never raw:
    assert entry.response_text == out["choices"][0]["message"]["content"]
    assert "482-73-1946" not in entry.response_text
    assert "<US_SSN_1>" in entry.response_text
    # ...while looking up by the RAW (unsanitized) text is a structural miss.
    raw_lookup = cache.lookup(tenant_id="acme", onprem_only=onprem_only,
                              allowed_providers=ctx_providers, query=raw_query)
    assert raw_lookup is None


# --- 6. requested_model pin bypasses the cache ------------------------------


def test_requested_model_pin_bypasses_cache():
    settings = _base_settings()
    registry = make_test_registry(settings)
    rec = RecordingBackend(_DeterministicLiteLLM(settings))
    pipe, cache = _pipeline(rec, settings, registry)
    req = ChatRequest(model="local-8b", messages=[Message(role="user", content="ping")])

    out1 = asyncio.run(pipe.run("r1", "acme", req))
    out2 = asyncio.run(pipe.run("r2", "acme", req))

    assert len(rec.models_called) == 2  # never served from cache, even on request #2
    assert cache.size == 0              # bypass means STORE never ran either
    for out in (out1, out2):
        recs = _cache_trace_records(out)
        assert recs and recs[0]["bypass_reason"] == "requested_model"
        assert recs[0]["hit"] is False


# --- 7. Tenant isolation e2e -------------------------------------------------


def test_tenant_isolation_e2e_same_query_different_tenants_both_miss():
    settings = _base_settings()
    registry = make_test_registry(settings)
    rec = RecordingBackend(_DeterministicLiteLLM(settings))
    pipe, cache = _pipeline(rec, settings, registry)
    req = ChatRequest(messages=[Message(role="user", content="Summarize the quarterly report")])

    asyncio.run(pipe.run("r1", "acme", req))
    asyncio.run(pipe.run("r2", "beta", req))

    assert len(rec.models_called) == 2  # tenant B never sees tenant A's cached entry
    assert cache.size == 2


# --- 8. CacheRecord counts-only + default-miss hit=False --------------------


def test_cache_record_is_counts_only_and_a_fresh_miss_emits_hit_false():
    settings = _base_settings()
    registry = make_test_registry(settings)
    rec = RecordingBackend(_DeterministicLiteLLM(settings))
    pipe, cache = _pipeline(rec, settings, registry)
    req = ChatRequest(messages=[Message(role="user", content="a never-before-seen query")])

    out = asyncio.run(pipe.run("r1", "acme", req))

    recs = _cache_trace_records(out)
    assert len(recs) == 1
    rec0 = recs[0]
    assert rec0["hit"] is False
    assert rec0["enabled"] is True
    allowed_keys = {"step", "enabled", "hit", "bypass_reason", "tokens_saved",
                    "cache_saved_usd", "entries_size", "blocked_cloud_entries",
                    "latency_ms"}
    assert set(rec0.keys()) <= allowed_keys
    for forbidden in ("query", "response", "response_text", "text", "content"):
        assert forbidden not in rec0


# --- 10. Cache keys are opaque (P1a Task 5, fix round 1) --------------------


def test_cache_keys_hold_no_plaintext_under_model_input_raw():
    """Under `model_input: raw` the model-facing text IS the raw prompt, and
    the cache is keyed on the model-facing text (keying on the redacted view
    would collapse two different values -- two prompts differing only in an
    email address both redact to `<EMAIL_ADDRESS_1>`, and the second caller
    would be served the first's answer). Keys are therefore HMAC'd after
    normalization: the equivalence class is unchanged, but no prompt text
    survives as a dict key."""
    settings = _base_settings()
    pipe, cache = _pipeline(_DeterministicLiteLLM(settings), settings)
    # An email rather than an SSN: US_SSN classifies restricted/high-risk,
    # which the cache bypasses outright (`_cache_bypass_reason`), so nothing
    # would be stored and this test would assert nothing.
    secret = "who owns hazelnut-quicksilver@example.com?"
    ap = AppPolicyView(residency="onprem_only", model_input="raw",
                       client_output="redact", pii_detection="regex")

    asyncio.run(pipe.run("req-key-1", "acme",
                         ChatRequest(messages=[Message(role="user", content=secret)]),
                         app_policy=ap))

    keys = [k for bucket in cache._data.values() for k in bucket]
    assert keys, "nothing was stored, so this asserts nothing"
    for k in keys:
        for fragment in ("hazelnut", "quicksilver", "example.com", "owns"):
            assert fragment not in k
        assert len(k) == 64 and all(c in "0123456789abcdef" for c in k)

    # ...and the cache still WORKS: the same query hits.
    out = asyncio.run(pipe.run("req-key-2", "acme",
                               ChatRequest(messages=[Message(role="user", content=secret)]),
                               app_policy=ap))
    assert cache.hits == 1
    assert [r for r in out["trace"] if r.get("step") == "cache"][0]["hit"] is True


def test_two_prompts_differing_only_in_a_redacted_value_do_not_collide():
    """The correctness half of the same decision: keyed on the model-facing
    text, these are two different cache entries. Keyed on the redacted view
    they would be one, and the second caller would get the first's answer."""
    settings = _base_settings()
    pipe, cache = _pipeline(_DeterministicLiteLLM(settings), settings)
    ap = AppPolicyView(residency="onprem_only", model_input="raw",
                       client_output="redact", pii_detection="regex")
    for i, addr in enumerate(("alice@example.com", "bob@example.com")):
        asyncio.run(pipe.run(f"req-collide-{i}", "acme",
                             ChatRequest(messages=[Message(
                                 role="user", content=f"who owns {addr}?")]),
                             app_policy=ap))
    assert cache.size == 2
    assert cache.hits == 0
