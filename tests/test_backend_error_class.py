"""B117: a failed backend call leaves a content-free error class -- the
exception class name, the HTTP status it carried and a closed `kind` enum --
on the failed hop's DecisionRecord, the `routing_outcomes.backend_errors`
column and the failure trace. Never the message, a URL or a host."""
from __future__ import annotations

import asyncio
import json

import httpx
import litellm
import openai
import pytest

pytest.importorskip("opentelemetry.sdk.trace")
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

import app.obs.otel as otel
from app.errors import ContextWindowExceeded
from app.gateway import gateway as gw
from app.gateway.backends import litellm_backend
from app.gateway.gateway import BACKEND_ERROR_KINDS, classify_backend_error
from tests._litellm_fake import _FakeStreamChunk
from tests.onboarding.harness import onboard
from tests.test_outcome_hooks import DEMO, HELLO, _client, _only

SENTINEL = "SECRET-PROMPT-TEXT http://10.0.0.1:8000/v1"
_REQ = httpx.Request("POST", "http://10.0.0.1:8000/v1/chat/completions")


def _resp(code: int) -> httpx.Response:
    return httpx.Response(code, request=_REQ)


def _litellm(cls, code: int | None = None):
    kw = {"message": SENTINEL, "model": "m", "llm_provider": "openai"}
    if code is not None:
        kw["response"] = _resp(code)
    return cls(**kw)


def _cases():
    return [
        (_litellm(litellm.Timeout), "timeout"),
        (_litellm(litellm.APIConnectionError), "connect"),
        (_litellm(litellm.InternalServerError, 500), "http_5xx"),
        (_litellm(litellm.ServiceUnavailableError, 503), "http_5xx"),
        (_litellm(litellm.RateLimitError, 429), "http_429"),
        (_litellm(litellm.BadRequestError, 400), "http_4xx"),
        (openai.APITimeoutError(request=_REQ), "timeout"),
        (openai.APIConnectionError(message=SENTINEL, request=_REQ), "connect"),
        (openai.InternalServerError(SENTINEL, response=_resp(502), body=None), "http_5xx"),
        (httpx.ConnectError(SENTINEL), "connect"),
        (httpx.RemoteProtocolError(SENTINEL), "connect"),
        (httpx.ReadTimeout(SENTINEL), "timeout"),
        (httpx.HTTPStatusError(SENTINEL, request=_REQ, response=_resp(503)), "http_5xx"),
        (httpx.HTTPStatusError(SENTINEL, request=_REQ, response=_resp(404)), "http_4xx"),
        (asyncio.TimeoutError(SENTINEL), "timeout"),
        (ConnectionRefusedError(SENTINEL), "connect"),
        (ContextWindowExceeded(SENTINEL), "context_overflow"),
        (TypeError(SENTINEL), "local_error"),
        (KeyError(SENTINEL), "local_error"),
        (RuntimeError(SENTINEL), "other"),
    ]


@pytest.mark.parametrize("exc,kind", _cases(), ids=lambda v: getattr(v, "__class__", v).__name__
                         if not isinstance(v, str) else v)
def test_classifier_kind_and_content_free(exc, kind):
    out = classify_backend_error(exc)
    assert set(out) == {"type", "http_status", "kind"}
    assert out["kind"] == kind and kind in BACKEND_ERROR_KINDS
    assert out["type"] == type(exc).__name__
    dumped = json.dumps(out)
    assert "SECRET-PROMPT-TEXT" not in dumped and "10.0.0.1" not in dumped


def test_classifier_reads_status():
    assert classify_backend_error(_litellm(litellm.RateLimitError, 429))["http_status"] == 429
    assert classify_backend_error(
        httpx.HTTPStatusError("x", request=_REQ, response=_resp(503)))["http_status"] == 503
    assert classify_backend_error(httpx.ConnectError("x"))["http_status"] is None


def test_classifier_never_raises_on_weird_objects():
    class Weird(Exception):
        @property
        def status_code(self):
            raise RuntimeError("boom")

        def __str__(self):
            raise RuntimeError("no str")

    for obj in (Weird(), object(), None, 42, "text"):
        out = classify_backend_error(obj)
        assert out["kind"] in BACKEND_ERROR_KINDS
        assert set(out) == {"type", "http_status", "kind"}

    class BadStatus(Exception):
        status_code = "not-a-number"
    assert classify_backend_error(BadStatus())["http_status"] is None


def _mem(monkeypatch):
    mem = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(otel.RedactingSpanExporter(mem)))
    monkeypatch.setattr(otel, "_provider", provider)
    return mem


def _assert_clean(*things):
    blob = json.dumps(things, default=str)
    assert "SECRET-PROMPT-TEXT" not in blob and "10.0.0.1" not in blob


def _connect_error():
    return _litellm(litellm.APIConnectionError)


def test_failover_then_ok_row_records_the_failed_hop(monkeypatch):
    real = litellm_backend._acompletion
    failed: list[str] = []

    async def _first_fails(model, call):
        if not failed:
            failed.append(model)
            raise _connect_error()
        return await real(model, call)

    monkeypatch.setattr(litellm_backend, "_acompletion", _first_fails)
    with _client() as c:
        r = c.post("/v1/chat/completions", headers=DEMO, json=HELLO)
        assert r.status_code == 200, r.text
        row = _only(c)
    assert row.status == "ok"
    assert row.backend_errors == [{"model": failed[0], "type": "APIConnectionError",
                                   "http_status": 500, "kind": "connect"}]
    assert row.attempts[0] == failed[0]
    _assert_clean(row.backend_errors, r.json().get("trace"))


def test_clean_ok_row_has_no_backend_errors():
    with _client() as c:
        r = c.post("/v1/chat/completions", headers=DEMO, json=HELLO)
        assert r.status_code == 200, r.text
        assert _only(c).backend_errors is None


def test_only_endpoint_failing_is_a_backend_failed_row_with_kind(monkeypatch):
    mem = _mem(monkeypatch)
    attempted: list[str] = []

    async def _refuse(model, call):
        attempted.append(model)
        raise _connect_error()

    monkeypatch.setattr(litellm_backend, "_acompletion", _refuse)
    with _client() as c:
        app = onboard(c, "b117-nofail", allowed_tiers=["cheap"])
        r = app.chat(HELLO)
        assert r.status_code == 502, r.text
        row = _only(c)
    assert row.status == "backend_failed" and row.route_reason == "no_failover"
    assert row.backend_errors == [{"model": attempted[0], "type": "APIConnectionError",
                                   "http_status": 500, "kind": "connect"}]
    spans = [s for s in mem.get_finished_spans() if s.name == "serving.request"]
    attrs = dict(spans[-1].attributes)
    assert json.loads(attrs["sp.backend_error_kinds"]) == ["connect"]
    _assert_clean(row.backend_errors, attrs)


def test_every_attempt_failing_lists_each_in_attempt_order(monkeypatch):
    async def _refuse(model, call):
        raise _litellm(litellm.InternalServerError, 503)

    monkeypatch.setattr(litellm_backend, "_acompletion", _refuse)
    with _client() as c:
        r = c.post("/v1/chat/completions", headers=DEMO, json=HELLO)
        assert r.status_code == 502, r.text
        row = _only(c)
    assert [e["model"] for e in row.backend_errors] == row.attempts
    assert {e["kind"] for e in row.backend_errors} == {"http_5xx"}
    assert {e["http_status"] for e in row.backend_errors} == {500}   # litellm hardcodes 500 on InternalServerError
    _assert_clean(row.backend_errors)


@pytest.mark.parametrize("mode", ["stream_unverified", "incremental_verify"])
def test_streamed_failure_row_carries_the_class(monkeypatch, mode):
    mem = _mem(monkeypatch)

    async def _dies(model, call):
        async def _gen():
            yield _FakeStreamChunk(content="partial ")
            raise httpx.ReadTimeout(SENTINEL)
        return _gen()

    monkeypatch.setattr(litellm_backend, "_acompletion", _dies)
    with _client() as c:
        app = onboard(c, f"b117-s-{mode}", stream_mode=mode)
        with c.stream("POST", "/v1/chat/completions", headers=app.headers,
                      json={**HELLO, "stream": True}) as r:
            assert r.status_code == 200
            body = "".join(r.iter_text())
        row = _only(c)
    assert row.status == "backend_failed"
    assert row.backend_errors == [{"model": row.attempts[0], "type": "ReadTimeout",
                                   "http_status": None, "kind": "timeout"}]
    spans = [s for s in mem.get_finished_spans() if s.name == "serving.request"]
    attrs = dict(spans[-1].attributes)
    assert json.loads(attrs["sp.backend_error_kinds"]) == ["timeout"]
    _assert_clean(row.backend_errors, attrs)
    assert "SECRET-PROMPT-TEXT" not in body


def test_context_overflow_row_records_the_kind(monkeypatch):
    async def _overflow(model, call):
        raise ContextWindowExceeded(SENTINEL)

    monkeypatch.setattr(litellm_backend, "_acompletion", _overflow)
    with _client() as c:
        r = c.post("/v1/chat/completions", headers=DEMO, json=HELLO)
        assert r.status_code == 400, r.text
        row = _only(c)
    assert row.status == "context_overflow"
    assert row.backend_errors and {e["kind"] for e in row.backend_errors} == {"context_overflow"}
    _assert_clean(row.backend_errors)


def test_decision_record_omits_backend_error_when_absent():
    from app.models import DecisionRecord
    rec = DecisionRecord(step="router", chosen="m", reason="r")
    assert "backend_error" not in rec.model_dump()
    assert gw is not None
