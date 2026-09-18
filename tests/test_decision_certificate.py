"""Task 9: the decision certificate (design P1a §6).

The certificate reaches three existing channels: the response `policy`
block, the exported trace attributes, and the console drill-down (which
reads the stored request snapshot -- a UI concern, not exercised here). This
file pins down the two channels this repo tests in Python:

1. Every response shape's `policy` dict carries the full per-axis
   certificate -- placement (+ sources), the four protection modes, the
   declared cross-axis rules that fired, and `sp.checks`' two per-request
   proofs -- built once by `Pipeline._certificate`/`_eligibility_checks` and
   never re-derived per call site.
2. `app/obs/export.py` stamps the matching `sp.*` attributes off that SAME
   dict, so the response and the trace can never disagree.
3. NO RAW VALUE ever appears in the certificate -- entity TYPES and counts
   only. A planted SSN must be absent from the whole serialized `policy`
   block and from the whole exported span attribute set.
"""
from __future__ import annotations

import asyncio
import json

import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

import app.obs.export as export_mod
import app.obs.otel as otel
from app.apps.policy import AppPolicyView
from app.compression.ccr import CCRStore
from app.config.loader import load_settings
from app.gateway.gateway import Gateway
from app.models import ChatRequest, DecisionRecord, ExecutionPolicyContext, Message
from app.pipeline import Pipeline
from app.policy.detectors import make_detector
from app.policy.gate import Gate
from app.routing.router import Router
from tests._litellm_fake import _DeterministicLiteLLM, make_test_registry

_SSN = "482-73-1946"
_PROMPT = f"My name is John Smith and my ssn is {_SSN}, please help."
_BENIGN = "What is the capital of France?"

_CERT_KEYS = (
    "placement", "placement_sources", "model_input_mode", "client_output_mode",
    "telemetry_io_mode", "detection_mode", "stream_mode",
    "cross_axis_rules_fired", "detected_entity_types",
)


def _settings():
    return load_settings()


def _pipeline(settings) -> Pipeline:
    """A real Pipeline (deterministic backend, seeded on-prem models) so
    `pipeline.run()` exercises the actual composition code, not a
    reimplementation of it -- mirrors tests/test_policy_mode_pipeline.py."""
    det = make_detector(settings.serving.pii_detection)
    registry = make_test_registry(settings)
    co = settings.serving.context_optimization
    gate = Gate(settings, det, registry=registry)
    gateway = Gateway(settings, _DeterministicLiteLLM(settings),
                      Router(settings, registry=registry), registry)
    return Pipeline(settings, gate, det, gateway, co_cfg_provider=lambda: co,
                    ccr=CCRStore(3600), registry=registry)


def _run(pipeline: Pipeline, content: str, app_policy=None) -> dict:
    req = ChatRequest(messages=[Message(role="user", content=content)])
    return asyncio.run(pipeline.run("req-1", "acme", req, app_policy=app_policy))


# --- 1. every certificate field is present on the response ------------------

def test_certificate_fields_present_on_response():
    pipe = _pipeline(_settings())
    out = _run(pipe, _BENIGN)
    p = out["policy"]
    for key in _CERT_KEYS:
        assert key in p, f"{key!r} missing from response policy block"
    assert "checks" in p
    assert set(p["checks"]) == {"chosen_in_eligible_set", "fallback_in_eligible_set"}
    # A single-hop request: the served model was drawn from the eligible set,
    # and there was no fallback to check.
    assert p["checks"]["chosen_in_eligible_set"] is True
    assert p["checks"]["fallback_in_eligible_set"] is None
    # Sane default-posture values (enforced, no app policy).
    assert p["model_input_mode"] == "redact"
    assert p["client_output_mode"] == "redact"
    assert p["placement_sources"]  # non-empty: at least one source rung
    assert p["cross_axis_rules_fired"] == []


# --- 2. cross_axis_rules_fired: named when it fires, empty when it doesn't --

def test_cross_axis_rule_named_when_ssn_escalates_residency():
    """The one declared cross-axis effect (design §5): an SSN raises this
    app's residency, and the certificate must NAME the rule that did it, not
    just show the after-the-fact placement value."""
    pipe = _pipeline(_settings())
    out = _run(pipe, _PROMPT)  # tenant "acme" defaults to private_cloud_ok
    p = out["policy"]
    assert p["cross_axis_rules_fired"] == ["rule:restricted"]
    assert p["placement"] == "onprem_only"
    assert "rule:restricted" in p["placement_sources"]
    assert p["detected_entity_types"] == ["US_SSN"]


def test_cross_axis_rules_fired_empty_when_nothing_fires():
    pipe = _pipeline(_settings())
    out = _run(pipe, _BENIGN)
    assert out["policy"]["cross_axis_rules_fired"] == []
    assert out["policy"]["detected_entity_types"] == []


# --- 3. no raw value ever appears in the certificate -------------------------

def test_no_raw_value_in_certificate():
    pipe = _pipeline(_settings())
    out = _run(pipe, _PROMPT)
    blob = json.dumps(out["policy"])
    assert _SSN not in blob
    # The TYPE is exactly what should be there instead.
    assert "US_SSN" in blob


# --- 4. `detection_mode` reports what RAN, not what was configured ----------
# "Not checked" must never read as "checked and clean" -- the same honesty gap
# Task 9 closed for verify()'s reason string under `client_output: raw`.

_NOBODY_LOOKED = AppPolicyView(
    # No consumer needs a redacted view: the model sees raw, the client gets
    # raw, telemetry stores nothing, and the declared cross-axis rule may not
    # move placement. `_gate_inputs` therefore builds the NullDetector...
    model_input="raw", client_output="raw", capture_io="off",
    escalate_residency_on_detection=False,
    # ...even though the app names the most expensive detector there is.
    pii_detection="presidio",
)


def test_certificate_detection_mode_is_off_when_nothing_scanned():
    """The configuration is legal and reachable, and the certificate used to
    stamp `detection_mode: presidio` with an empty `detected_entity_types` --
    indistinguishable from "presidio scanned this and found nothing". It must
    say `off`: nothing scanned."""
    settings = _settings()
    pipe = _pipeline(settings)
    out = _run(pipe, _PROMPT, app_policy=_NOBODY_LOOKED)   # a real SSN, unseen
    p = out["policy"]
    assert p["detection_mode"] == "off"
    assert p["detected_entity_types"] == []
    # The COMPILED axis still says presidio -- this is the certificate telling
    # the truth about the request, not the compiler changing its answer.
    from app.policy.compile import compile_policy
    assert compile_policy(_NOBODY_LOOKED, settings).protection.detection == "presidio"


def test_certificate_detection_mode_names_the_detector_that_did_run():
    """The other half, so the fix can't be "always say off": in the default
    posture a detector really does run, and the certificate names it."""
    settings = _settings()
    pipe = _pipeline(settings)
    out = _run(pipe, _PROMPT)
    assert out["policy"]["detection_mode"] == settings.serving.pii_detection
    assert out["policy"]["detected_entity_types"] == ["US_SSN"]


# --- eligibility checks: direct unit coverage of the fallback semantics -----

def _ctx(**overrides) -> ExecutionPolicyContext:
    base = dict(
        request_id="r", tenant_id="t", sensitivity="public", onprem_only=False,
        allowed_providers=frozenset({"onprem", "azure"}),
        allowed_models=frozenset({"local-8b"}), model_scope_active=False,
        max_cost_usd=0.05, max_latency_ms=15000, risk_class="low",
        require_verification=False, detected_entities=(), redaction_map_id=None)
    base.update(overrides)
    return ExecutionPolicyContext(**base)


def _rec(chosen, provider, **kw) -> DecisionRecord:
    return DecisionRecord(step="router", chosen=chosen, chosen_provider=provider,
                          reason="test", **kw)


def test_eligibility_checks_no_chosen_hop_is_not_applicable():
    checks = Pipeline._eligibility_checks(_ctx(), [])
    assert checks == {"chosen_in_eligible_set": None, "fallback_in_eligible_set": None}


def test_eligibility_checks_single_eligible_hop():
    records = [_rec("local-8b", "onprem")]
    checks = Pipeline._eligibility_checks(_ctx(), records)
    assert checks["chosen_in_eligible_set"] is True
    assert checks["fallback_in_eligible_set"] is None  # nothing to check -- no fallback


def test_eligibility_checks_flags_ineligible_served_model():
    """provider not in ctx.allowed_providers -- would be a real policy
    violation if it ever happened; the certificate must be able to say so."""
    records = [_rec("cloud-x", "bedrock")]
    checks = Pipeline._eligibility_checks(_ctx(), records)
    assert checks["chosen_in_eligible_set"] is False


def test_eligibility_checks_fallback_all_eligible():
    # A failed-over hop (step="gateway", chosen=None) is not counted as a
    # "chosen" hop; only the two router hops with a chosen candidate are.
    records = [
        DecisionRecord(step="gateway", chosen=None, reason="failed; retrying"),
        _rec("local-8b", "onprem"),
        _rec("local-32b", "onprem"),
    ]
    checks = Pipeline._eligibility_checks(_ctx(), records)
    assert checks["chosen_in_eligible_set"] is True   # the SERVED (last) hop
    assert checks["fallback_in_eligible_set"] is True  # the earlier hop too


def test_eligibility_checks_fallback_flags_an_ineligible_earlier_hop():
    records = [_rec("cloud-x", "bedrock"), _rec("local-8b", "onprem")]
    checks = Pipeline._eligibility_checks(_ctx(), records)
    assert checks["chosen_in_eligible_set"] is True    # served hop is fine
    assert checks["fallback_in_eligible_set"] is False  # the one before it wasn't


def test_eligibility_checks_respects_model_scope():
    ctx = _ctx(model_scope_active=True, allowed_models=frozenset({"local-8b"}))
    ok = Pipeline._eligibility_checks(ctx, [_rec("local-8b", "onprem")])
    assert ok["chosen_in_eligible_set"] is True
    out_of_scope = Pipeline._eligibility_checks(ctx, [_rec("local-32b", "onprem")])
    assert out_of_scope["chosen_in_eligible_set"] is False


# --- trace attributes: export_trace stamps the same certificate ------------

@pytest.fixture
def mem_provider(monkeypatch):
    mem = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(otel.RedactingSpanExporter(mem)))
    monkeypatch.setattr(otel, "_provider", provider)
    return mem


def _cert_sample():
    """A policy dict shaped exactly like `Pipeline._certificate`'s output,
    embedded in an otherwise-ordinary export_trace call -- mirrors
    tests/test_obs_export.py's `_sample()`, extended with Task 9's fields.
    The planted SSN lives in `trace[0]["reason"]` (an existing egress
    surface `export_trace` already redacts), never in a certificate field --
    proving the certificate itself never carries one is the point of
    `test_export_stamps_the_certificate_and_leaks_no_raw_value` below."""
    return dict(
        request_id="req-1", tenant_id="hospital", model="local-32b",
        cost={"total_usd": 0.0004, "baseline_usd": 0.012, "routing_saved_usd": 0.0116},
        policy={
            "sensitivity": "restricted", "onprem_only": True,
            "allowed_providers": ["onprem"], "detected_entities": ["US_SSN"],
            "app_policy_version": "abc123",
            "placement": "onprem_only",
            "placement_sources": ["app_policy", "rule:restricted"],
            "model_input_mode": "redact", "client_output_mode": "redact",
            "telemetry_io_mode": "redacted", "detection_mode": "presidio",
            "stream_mode": "off",
            "cross_axis_rules_fired": ["rule:restricted"],
            "detected_entity_types": ["US_SSN"],
            "checks": {"chosen_in_eligible_set": True, "fallback_in_eligible_set": None},
        },
        trace=[{"step": "route", "chosen": "local-32b",
                "reason": f"leaked SSN {_SSN} in reason text", "hop_index": 0,
                "actual_cost_usd": 0.0004, "latency_ms": 12.0}],
    )


def test_export_stamps_the_certificate_and_leaks_no_raw_value(mem_provider):
    export_mod.export_trace(**_cert_sample())
    otel._provider.force_flush()
    spans = mem_provider.get_finished_spans()
    root = next(s for s in spans if s.name == "serving.request")

    assert root.attributes["sp.placement"] == "onprem_only"
    assert root.attributes["sp.placement_sources"] == "app_policy,rule:restricted"
    assert root.attributes["sp.model_input_mode"] == "redact"
    assert root.attributes["sp.client_output_mode"] == "redact"
    assert root.attributes["sp.telemetry_io_mode"] == "redacted"
    assert root.attributes["sp.detection_mode"] == "presidio"
    assert root.attributes["sp.stream_mode"] == "off"
    assert root.attributes["sp.cross_axis_rules_fired"] == "rule:restricted"
    assert root.attributes["sp.detected_entity_types"] == "US_SSN"
    checks = json.loads(root.attributes["sp.checks"])
    assert checks == {"chosen_in_eligible_set": True, "fallback_in_eligible_set": None}

    # No raw value anywhere on ANY span this export produced -- the planted
    # SSN lived only in the (unrelated, already-covered) trace reason text.
    blob = "".join(str(dict(s.attributes)) + s.name for s in spans)
    assert _SSN not in blob


def test_export_omits_checks_when_absent(mem_provider):
    """A caller that never built a certificate (e.g. a pre-Task-9 snapshot
    replayed through export_trace) must not get a fabricated `sp.checks`."""
    sample = _cert_sample()
    sample["policy"] = {"sensitivity": "public", "onprem_only": False,
                        "allowed_providers": [], "detected_entities": []}
    export_mod.export_trace(**sample)
    otel._provider.force_flush()
    root = next(s for s in mem_provider.get_finished_spans() if s.name == "serving.request")
    assert "sp.checks" not in root.attributes
    assert "sp.placement" not in root.attributes
