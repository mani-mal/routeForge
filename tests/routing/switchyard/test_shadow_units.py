"""B104/B105 unit contracts: the tag vocabulary and trace attributes in the
exporter, the outcome builders' switchyard columns, the DecisionRecord
`advisor` field's off-mode invisibility, and fallback version stamping.
No upstream Switchyard needed."""
from __future__ import annotations

import pytest

from app.apps.models import RoutingOutcome
from app.apps.outcomes import SWITCHYARD_COLUMNS, build_failure_outcome, build_ok_outcome
from app.config.schema import SwitchyardCfg
from app.models import DecisionRecord
from app.obs.export import (
    LANGFUSE_TRACE_TAGS, SWITCHYARD_TAGS, SWITCHYARD_TRACE_KEYS, _switchyard_tags,
)
from app.routing.switchyard import AdvisorResult


# --- tags ---------------------------------------------------------------------

def test_tag_attribute_matches_the_pinned_langfuse_sdk():
    attrs = pytest.importorskip("langfuse._client.attributes")
    assert LANGFUSE_TRACE_TAGS == attrs.LangfuseOtelSpanAttributes.TRACE_TAGS


def test_vocabulary_is_fixed():
    assert SWITCHYARD_TAGS == {
        "switchyard", "switchyard:shadow", "switchyard:enforce",
        "switchyard:agreed", "switchyard:overridden", "switchyard:fallback"}


@pytest.mark.parametrize("advice, expected", [
    ({"mode": "shadow", "agreed": True}, "switchyard:agreed"),
    ({"mode": "shadow", "agreed": False}, "switchyard:overridden"),
    ({"mode": "enforce", "agreed": None, "fallback_cause": "error"}, "switchyard:fallback"),
])
def test_tags(advice, expected):
    tags = _switchyard_tags(advice)
    assert tags == ["switchyard", f"switchyard:{advice['mode']}", expected]


def test_unknown_outcome_omits_the_outcome_tag():
    assert _switchyard_tags({"mode": "shadow", "agreed": None}) == [
        "switchyard", "switchyard:shadow"]


@pytest.mark.parametrize("mode", ["off", "free text", "shadow;drop"])
def test_a_tag_outside_the_vocabulary_is_refused(mode):
    with pytest.raises(ValueError):
        _switchyard_tags({"mode": mode, "agreed": True})


def test_trace_keys():
    assert SWITCHYARD_TRACE_KEYS == ("mode", "role", "tier", "confidence", "agreed",
                                     "fallback_cause", "latency_ms", "version",
                                     "algorithm")


def test_export_trace_sets_attributes_regardless_of_capture_io(monkeypatch):
    pytest.importorskip("opentelemetry.sdk.trace")
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    import app.obs.export as export_mod
    import app.obs.otel as otel
    mem = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(otel.RedactingSpanExporter(mem)))
    monkeypatch.setattr(otel, "_provider", provider)
    advice = {"mode": "shadow", "role": "capable", "tier": "frontier",
              "confidence": 0.7, "agreed": False, "fallback_cause": None,
              "latency_ms": 1, "version": "0.4.0a1", "algorithm": "stage_router"}
    export_mod.export_trace(
        request_id="r", tenant_id="t", model="m", cost={}, policy={}, trace=[],
        capture_io="off", switchyard=advice)
    root = next(s for s in mem.get_finished_spans() if s.name == "serving.request")
    a = root.attributes
    assert a["sp.capture_io"] == "off"
    assert a["sp.switchyard.mode"] == "shadow"
    assert a["sp.switchyard.role"] == "capable"
    assert a["sp.switchyard.tier"] == "frontier"
    assert a["sp.switchyard.confidence"] == 0.7
    assert a["sp.switchyard.agreed"] is False
    assert a["sp.switchyard.latency_ms"] == 1.0
    assert "sp.switchyard.fallback_cause" not in a     # None is skipped
    assert list(a[LANGFUSE_TRACE_TAGS]) == ["switchyard", "switchyard:shadow",
                                            "switchyard:overridden"]


# --- outcome rows ---------------------------------------------------------------

def test_switchyard_columns_exist_on_the_table():
    cols = {c.name for c in RoutingOutcome.__table__.columns}
    assert set(SWITCHYARD_COLUMNS) <= cols
    assert all(RoutingOutcome.__table__.columns[c].nullable for c in SWITCHYARD_COLUMNS)


def test_migration_script_lists_the_columns():
    import importlib.util
    import pathlib
    path = pathlib.Path(__file__).resolve().parents[3] / "scripts" / "migrate_app_policies.py"
    spec = importlib.util.spec_from_file_location("_migrate", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert set(SWITCHYARD_COLUMNS) <= {c[0] for c in mod.OUTCOMES_COLUMNS}


_ADVICE = {"mode": "shadow", "role": "efficient", "tier": "cheap", "confidence": 0.9,
           "agreed": True, "fallback_cause": None}


def test_builders_copy_the_record_and_default_to_null():
    kw = dict(request_id="r", application_id="a", routing_policy_version="v",
              workload=None, workload_source="none")
    ok = build_ok_outcome({"trace": []}, outcome_ctx={"switchyard": _ADVICE}, **kw)
    assert {c: ok[c] for c in SWITCHYARD_COLUMNS} == {
        "switchyard_mode": "shadow", "switchyard_role": "efficient",
        "switchyard_tier": "cheap", "switchyard_confidence": 0.9,
        "switchyard_agreed": True, "switchyard_fallback_cause": None}
    off = build_ok_outcome({"trace": []}, outcome_ctx={}, **kw)
    assert all(off[c] is None for c in SWITCHYARD_COLUMNS)
    failed = build_failure_outcome(
        status="backend_failed", endpoint_registry_version=None,
        outcome_ctx={"switchyard": {**_ADVICE, "agreed": None}}, **kw)
    assert failed["switchyard_mode"] == "shadow" and failed["switchyard_agreed"] is None
    # Every key is a real column, so `RoutingOutcome(**row)` accepts it.
    RoutingOutcome(**ok)


# --- DecisionRecord -------------------------------------------------------------

def test_advisor_field_is_invisible_when_absent():
    rec = DecisionRecord(step="router", chosen="m", reason="x")
    assert "advisor" not in rec.model_dump()
    assert "advisor" not in rec.model_dump_json()
    assert "advisor" not in rec.model_dump(exclude={"reason": True})
    rec.advisor = {"mode": "shadow", "tier": "cheap", "agreed": True}
    assert rec.model_dump()["advisor"]["tier"] == "cheap"
    assert rec.model_dump(exclude={"reason": True})["advisor"]["agreed"] is True


# --- fallback version stamping (B104) -------------------------------------------

def test_fallback_record_carries_version_and_algorithm():
    rec = AdvisorResult.fallback("timeout", 1.0, version="0.4.0a1",
                                 algorithm="stage_router").to_record()
    assert rec["version"] == "0.4.0a1" and rec["algorithm"] == "stage_router"
    assert AdvisorResult.fallback("timeout", 1.0).to_record()["version"] is None


def test_real_advisor_stamps_its_fallbacks(monkeypatch):
    pytest.importorskip("switchyard")
    import asyncio

    from app.models import Message
    from app.routing.switchyard import SwitchyardAdvisor
    from app.routing.switchyard import advisor as advisor_mod

    def _unconvertible(messages):
        raise advisor_mod._Unconvertible("x")
    monkeypatch.setattr(advisor_mod, "to_switchyard_request", _unconvertible)
    adv = SwitchyardAdvisor(SwitchyardCfg(mode="shadow"))
    rec = asyncio.run(adv.advise([Message(role="user", content="hi")])).to_record()
    assert rec["fallback_cause"] == "unconvertible"
    assert rec["version"] == "0.4.0a1" and rec["algorithm"] == "stage_router"
