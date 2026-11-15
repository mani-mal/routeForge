"""B119 step 1: per-request detector queue-wait / run-time instrumentation.

Measurement only. Every detector hop (`run_detector_for`) is timed against a
per-request accumulator in a ContextVar: executor jobs as queue wait + run
time (+ the jobs in flight at submission), inline regex/off work as inline
time with no queue. The totals reach the request's trace as `sp.detector_*`
(regardless of capture_io; no content) and a `detector_timing` log line.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time

import pytest

from app.policy import detector_pool
from app.policy.detector_pool import (
    current_timing, run_detector, run_detector_for, start_request_timing,
)
from app.policy.detectors import RegexDetector

_SLOW_S = 0.3
_TOL_MS = 120
_SENTINEL_SSN = "482-73-1946"


class _ExpensiveDetector(RegexDetector):
    """A RegexDetector SUBCLASS: the pool treats it as expensive (only the
    exact cheap types run inline), so work for it goes to the executor."""


async def _request(label: str, sleep_s: float):
    """One "request": its own accumulator, one slow executor job."""
    timing = start_request_timing()
    await run_detector_for(_ExpensiveDetector(), time.sleep, sleep_s, hop_label=label)
    return timing


def test_second_request_queue_wait_is_first_requests_run_time():
    async def main():
        first = asyncio.create_task(_request("gate", _SLOW_S))
        await asyncio.sleep(0)            # let the first submit before the second
        second = asyncio.create_task(_request("gate", _SLOW_S))
        return await first, await second

    first, second = asyncio.run(main())
    slow_ms = _SLOW_S * 1000
    assert first.jobs == second.jobs == 1
    assert abs(first.run_ms - slow_ms) < _TOL_MS
    assert abs(second.run_ms - slow_ms) < _TOL_MS
    assert first.queue_ms < _TOL_MS
    # The second waited behind the first's whole pass (single FIFO worker).
    assert abs(second.queue_ms - first.run_ms) < _TOL_MS
    assert first.queue_depth_max == 0
    assert second.queue_depth_max == 1
    assert first.inline_calls == second.inline_calls == 0


def test_labels_attributed_and_inline_work_has_no_queue():
    async def main():
        timing = start_request_timing()
        await run_detector_for(_ExpensiveDetector(), lambda: None, hop_label="gate")
        await run_detector_for(_ExpensiveDetector(), lambda: None, hop_label="verify")
        await run_detector_for(_ExpensiveDetector(), lambda: None, hop_label="verify")
        # Exact RegexDetector: inline, never queued.
        await run_detector_for(RegexDetector(), time.sleep, 0.05, hop_label="egress_redact")
        # No detector at all: no detector work, nothing recorded.
        await run_detector_for(None, lambda: None, hop_label="export")
        return timing

    timing = asyncio.run(main())
    assert timing.jobs == 3
    assert timing.inline_calls == 1
    assert timing.inline_ms >= 40
    by_hop = json.loads(timing.by_hop_json())
    assert set(by_hop) == {"gate", "verify", "egress_redact"}
    assert by_hop["gate"][0] == 1 and by_hop["verify"][0] == 2
    jobs, queue_ms, run_ms, inline_ms = by_hop["egress_redact"]
    assert (jobs, queue_ms, run_ms) == (0, 0, 0) and inline_ms >= 40
    attrs = timing.attributes()
    assert set(attrs) == {"sp.detector_queue_ms", "sp.detector_run_ms",
                          "sp.detector_inline_ms", "sp.detector_jobs",
                          "sp.detector_queue_depth_max", "sp.detector_by_hop"}
    assert all(isinstance(v, int) for k, v in attrs.items() if k != "sp.detector_by_hop")


def test_no_accumulator_records_nothing_and_still_runs():
    async def main():
        assert current_timing() is None
        out = await run_detector_for(_ExpensiveDetector(), lambda: 7, hop_label="warmup")
        inline = await run_detector_for(RegexDetector(), lambda: 8)
        return out, inline, current_timing()

    assert asyncio.run(main()) == (7, 8, None)


def test_exception_propagates_unchanged_and_job_is_timed():
    class Boom(Exception):
        pass

    def explode():
        raise Boom("x")

    async def main():
        timing = start_request_timing()
        with pytest.raises(Boom):
            await run_detector(explode, hop_label="gate")
        return timing

    timing = asyncio.run(main())
    assert timing.jobs == 1


def test_hop_label_is_not_passed_to_fn():
    seen = {}

    def fn(*args, **kwargs):
        seen.update(kwargs)
        return args

    async def main():
        start_request_timing()
        return await run_detector_for(_ExpensiveDetector(), fn, 1, x=2, hop_label="gate")

    assert asyncio.run(main()) == (1,)
    assert seen == {"x": 2}


def test_in_flight_counter_returns_to_zero_after_cancelled_queued_job():
    async def main():
        start_request_timing()
        blocker = asyncio.create_task(
            run_detector_for(_ExpensiveDetector(), time.sleep, 0.2, hop_label="gate"))
        await asyncio.sleep(0.02)
        queued = asyncio.create_task(
            run_detector_for(_ExpensiveDetector(), time.sleep, 0.2, hop_label="gate"))
        await asyncio.sleep(0.02)
        queued.cancel()
        with pytest.raises(asyncio.CancelledError):
            await queued
        await blocker

    asyncio.run(main())
    detector_pool._EXECUTOR.submit(lambda: None).result()   # drain
    assert detector_pool._in_flight == 0


# --- pipeline-level: every hop gets its label ---------------------------------


def test_pipeline_hops_are_labelled():
    from tests.test_gate_off_event_loop import (
        _RecordingDetector, _ToolCallBackend, _pipeline, _tool_request,
    )
    pipeline = _pipeline(_RecordingDetector(), backend_cls=_ToolCallBackend)

    async def main():
        timing = start_request_timing()
        await pipeline.run("req-b119", "acme", _tool_request())
        return timing

    timing = asyncio.run(main())
    by_hop = json.loads(timing.by_hop_json())
    assert {"gate", "tool_view", "verify", "egress_redact"} <= set(by_hop)
    assert all(by_hop[h][0] >= 1 for h in ("gate", "tool_view", "verify", "egress_redact"))
    assert timing.inline_calls == 0


# --- HTTP: the attributes reach the exported trace -----------------------------


@pytest.fixture
def spans(monkeypatch):
    pytest.importorskip("opentelemetry.sdk.trace")
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
        InMemorySpanExporter,
    )

    import app.obs.otel as otel
    mem = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(otel.RedactingSpanExporter(mem)))
    monkeypatch.setattr(otel, "_provider", provider)
    # Route ALL detector work (regex here) through the executor, as Presidio's
    # would be, so the request has real executor jobs to time.
    monkeypatch.setattr(detector_pool, "needs_detector_executor",
                        lambda d: d is not None)
    return provider, mem


def _root_attrs(provider, mem) -> dict:
    provider.force_flush()
    roots = [s for s in mem.get_finished_spans() if s.name == "serving.request"]
    assert roots, "no serving.request span exported"
    return dict(roots[-1].attributes)


def _timing_attrs(attrs: dict) -> dict:
    return {k: v for k, v in attrs.items() if k.startswith("sp.detector_")}


def _timing_log_lines(caplog) -> list[dict]:
    out = []
    for rec in caplog.records:
        msg = rec.getMessage()
        if '"event": "detector_timing"' in msg:
            out.append(json.loads(msg))
    return out


def test_non_stream_trace_carries_detector_timing(client, spans, caplog):
    caplog.set_level(logging.INFO, logger="serving.api")
    provider, mem = spans
    r = client.post("/v1/chat/completions",
                    headers={"authorization": "Bearer hospital-key"},
                    json={"model": "auto", "messages": [
                        {"role": "user", "content": f"my SSN is {_SENTINEL_SSN}"}]})
    assert r.status_code == 200, r.text
    timing = _timing_attrs(_root_attrs(provider, mem))
    assert timing["sp.detector_jobs"] >= 2           # gate + verify at least
    by_hop = json.loads(timing["sp.detector_by_hop"])
    assert {"gate", "verify"} <= set(by_hop)
    # The export hop runs after the attributes are set: not on its own trace.
    assert "export" not in by_hop
    blob = json.dumps(timing)
    assert _SENTINEL_SSN not in blob and "SSN" not in blob
    # One final log line per request (capture is off here, so the export
    # does no detector work and is not a hop; see the next test).
    lines = [ln for ln in _timing_log_lines(caplog) if ln["request_id"] == r.json()["id"]]
    assert len(lines) == 1
    assert lines[0]["detector_jobs"] >= timing["sp.detector_jobs"]
    assert _SENTINEL_SSN not in json.dumps(lines[0])


def test_export_hop_reaches_the_log_line_when_capture_is_on(client, spans, caplog, monkeypatch):
    monkeypatch.setenv("SP_CAPTURE_IO", "redacted")
    from app.main import create_app
    from fastapi.testclient import TestClient
    c = TestClient(create_app())
    caplog.set_level(logging.INFO, logger="serving.api")
    provider, mem = spans
    r = c.post("/v1/chat/completions",
               headers={"authorization": "Bearer demo-key"},
               json={"model": "auto", "messages": [
                   {"role": "user", "content": f"my SSN is {_SENTINEL_SSN}"}]})
    assert r.status_code == 200, r.text
    on_trace = json.loads(_timing_attrs(_root_attrs(provider, mem))["sp.detector_by_hop"])
    assert "export" not in on_trace
    line = [ln for ln in _timing_log_lines(caplog) if ln["request_id"] == r.json()["id"]][0]
    assert "export" in line["detector_by_hop"]
    assert line["detector_by_hop"]["export"][0] == 1


def test_stream_trace_carries_feed_and_flush_jobs(client, spans, monkeypatch):
    from app.gateway.backends import litellm_backend
    from tests._litellm_fake import make_streaming_acompletion
    from tests.test_stream_unverified import _make_app

    provider, mem = spans
    deltas = ["Sure thing! ", "The record is ", "123-45-", "6789", " and more text."]
    monkeypatch.setattr(litellm_backend, "acompletion", make_streaming_acompletion(deltas))
    token = _make_app(client, "b119-iv", stream_mode="incremental_verify")
    r = client.post("/v1/chat/completions",
                    headers={"Authorization": f"Bearer {token}"},
                    json={"model": "auto", "stream": True,
                          "messages": [{"role": "user", "content": "hi there"}]})
    assert r.status_code == 200, r.text
    assert "123-45-6789" not in r.text
    timing = _timing_attrs(_root_attrs(provider, mem))
    by_hop = json.loads(timing["sp.detector_by_hop"])
    assert {"gate", "stream_feed", "stream_flush"} <= set(by_hop)
    assert by_hop["stream_feed"][0] == len(deltas)
    assert "123-45" not in json.dumps(timing) and "6789" not in json.dumps(timing)
