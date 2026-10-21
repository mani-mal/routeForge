"""B78: a cancelled request gets a `routing_outcomes` row, and a cancelled or
backend-failed request gets a minimal, content-free Langfuse trace."""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest
from sqlalchemy import select

pytest.importorskip("opentelemetry.sdk.trace")
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

import app.obs.export as export_mod
import app.obs.otel as otel
from app.api import routes
from app.apps.models import RoutingOutcome
from app.apps.policy import AppPolicyView
from app.apps.privacy import AppPrivacyProfile
from app.models import ChatRequest, Message
from tests.test_error_metering import (DEMO, HELLO, _client, _disconnected_request,
                                       _failing_backend)


def _mem(monkeypatch):
    mem = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(otel.RedactingSpanExporter(mem)))
    monkeypatch.setattr(otel, "_provider", provider)
    return mem


# ---- the exporter ----------------------------------------------------------
def test_failure_trace_cancelled_is_warning_with_no_content(monkeypatch):
    mem = _mem(monkeypatch)
    export_mod.export_failure_trace(
        request_id="req-1", application_id="app-a", status="cancelled",
        latency_ms=305000.0, attempts=("local-8b", "local-32b"), session_id="sess-9")
    spans = mem.get_finished_spans()
    assert [s.name for s in spans] == ["serving.request"]
    a = dict(spans[0].attributes)
    assert a["sp.request_id"] == "req-1"
    assert a["sp.application_id"] == "app-a"
    assert a["sp.status"] == "cancelled"
    assert a["sp.latency_ms"] == 305000.0
    assert json.loads(a["sp.attempts"]) == ["local-8b", "local-32b"]
    assert a["langfuse.session.id"] == "sess-9"
    assert a["langfuse.observation.level"] == "WARNING"
    # content-free in every mode
    assert "input.value" not in a and "output.value" not in a
    assert "sp.input" not in a and "sp.output" not in a


def test_failure_trace_error_is_error_level_and_span_status(monkeypatch):
    from opentelemetry.trace import StatusCode
    mem = _mem(monkeypatch)
    export_mod.export_failure_trace(
        request_id="req-2", application_id="app-a", status="error",
        latency_ms=600000.0, attempts=("local-8b",))
    span = mem.get_finished_spans()[0]
    assert dict(span.attributes)["langfuse.observation.level"] == "ERROR"
    assert span.status.status_code == StatusCode.ERROR


def test_failure_trace_noop_when_telemetry_off(monkeypatch):
    monkeypatch.setattr(otel, "_provider", None)
    export_mod.export_failure_trace(
        request_id="r", application_id="a", status="error", latency_ms=1.0,
        attempts=())   # must not raise


def test_failure_trace_goes_to_the_apps_own_project(monkeypatch):
    shared = _mem(monkeypatch)
    own = InMemorySpanExporter()
    prov = TracerProvider()
    prov.add_span_processor(SimpleSpanProcessor(own))
    seen = {}

    def fake_app_tracer(host, pk, sk, capture_io_full=False):
        seen["args"] = (host, pk, sk, capture_io_full)
        return prov.get_tracer("t")

    monkeypatch.setattr(export_mod, "get_app_tracer", fake_app_tracer)
    lf = SimpleNamespace(host="h", public_key="pk", secret_key="sk", project_id="p")
    export_mod.export_failure_trace(
        request_id="r", application_id="a", status="error", latency_ms=1.0,
        attempts=(), app_langfuse=lf)
    assert len(own.get_finished_spans()) == 1
    assert shared.get_finished_spans() == ()
    assert seen["args"] == ("h", "pk", "sk", False)


def test_failure_trace_missing_app_project_follows_export_trace(monkeypatch):
    """Per-app tracer build failed: export_trace falls to the shared tracer
    for non-full capture, and so does the failure trace."""
    shared = _mem(monkeypatch)
    monkeypatch.setattr(export_mod, "get_app_tracer", lambda *a, **k: None)
    lf = SimpleNamespace(host="h", public_key="pk", secret_key="sk", project_id="p")
    export_mod.export_failure_trace(
        request_id="r", application_id="a", status="cancelled", latency_ms=1.0,
        attempts=(), app_langfuse=lf)
    assert len(shared.get_finished_spans()) == 1


# ---- the call sites --------------------------------------------------------
def _outcomes(c):
    async def go():
        async with c.app.state.db.session() as session:
            return list((await session.execute(select(RoutingOutcome))).scalars())
    return c.portal.call(go)


def test_cancelled_request_writes_outcome_row_and_failure_trace(monkeypatch):
    traced = []
    monkeypatch.setattr(routes, "export_failure_trace", lambda **kw: traced.append(kw))
    with _client() as c:
        async def go():
            async def work():
                await asyncio.sleep(10)
            task = asyncio.create_task(work())
            await asyncio.sleep(0)
            body = ChatRequest(model="auto", messages=[Message(role="user", content="hi")])
            gen = routes._keepalive_stream(
                _disconnected_request(c), task=task, keepalive_s=0.01,
                request_id="req-b78c", body=body, tenant="demo",
                app_policy=AppPolicyView(), app_langfuse=None,
                profile=AppPrivacyProfile(recognizers=(), rules=()),
                session_id="sess-1", s=c.app.state.settings, started=0.0)
            return [f async for f in gen]
        c.portal.call(go)
        rows = _outcomes(c)
        assert [(r.request_id, r.status) for r in rows] == [("req-b78c", "cancelled")]
        assert rows[0].chosen is None
    assert len(traced) == 1
    kw = traced[0]
    assert kw["status"] == "cancelled" and kw["request_id"] == "req-b78c"
    assert kw["session_id"] == "sess-1"
    assert not {"prompt", "completion", "input", "output"} & set(kw)


def test_backend_failure_calls_failure_trace_without_content(monkeypatch):
    _failing_backend(monkeypatch)
    traced = []
    monkeypatch.setattr(routes, "export_failure_trace", lambda **kw: traced.append(kw))
    with _client() as c:
        r = c.post("/v1/chat/completions", headers=DEMO, json=HELLO)
        assert r.status_code == 502, r.text
    assert len(traced) == 1
    kw = traced[0]
    assert kw["status"] == "error" and kw["application_id"]
    assert kw["attempts"]
    assert not {"prompt", "completion", "input", "output"} & set(kw)


def test_failure_trace_exporter_error_is_swallowed(monkeypatch):
    def boom(**kw):
        raise RuntimeError("langfuse down")
    monkeypatch.setattr(routes, "export_failure_trace", boom)
    _failing_backend(monkeypatch)
    with _client() as c:
        r = c.post("/v1/chat/completions", headers=DEMO, json=HELLO)
        assert r.status_code == 502, r.text


# ---- readers of routing_outcomes.status -------------------------------------
def test_simulator_excludes_cancelled_rows_under_unknown_status():
    from app.eval.simulate import SIMULATED_STATUSES
    assert "cancelled" not in SIMULATED_STATUSES


def test_calibration_excludes_cancelled_rows():
    from app.eval.calibration import EXCLUDED_STATUSES
    assert "cancelled" in EXCLUDED_STATUSES


def test_report_counts_cancelled_in_denominator_not_served():
    from app.eval.outcomes import _summarize_bucket
    rows = [{"status": "ok", "latency_ms": 10.0, "cost_usd": 0.1},
            {"status": "cancelled", "latency_ms": 5.0, "cost_usd": 0.0}]
    assert _summarize_bucket(rows)["served_ok_rate"] == 0.5
