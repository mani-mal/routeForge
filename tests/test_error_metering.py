"""Signal hardening, spec §4 (B38, meter half): a request that fails on the
BACKEND writes one `usage_records` row with `status="error"`, so an outage is
visible in the console instead of vanishing from every usage figure.

Only a backend failure earns an `error` row. A context-window overflow (the
client's request, 400), `NoEligibleCandidate` (a policy/routing outcome), a
policy rejection and a client disconnect (`cancelled`) do not. And an `error`
row never moves a spend, token, latency or budget figure -- those stay computed
over `ok` rows only (test 5 proves it rather than assuming it).

The route tests drive the real app (`create_app()`) and make the backend fail by
replacing `litellm_backend._acompletion` -- the one seam both `generate` and
`generate_stream` call -- so the model each call was made against is recorded
in order, and the row's `model` can be checked against it.
"""
from __future__ import annotations

import asyncio
import json
from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.api import routes
from app.apps import store
from app.apps.db import Database
from app.apps.policy import AppPolicyView, day_spend_usd
from app.apps.privacy import AppPrivacyProfile
from app.errors import ContextWindowExceeded
from app.gateway.backends import litellm_backend
from app.main import create_app
from app.models import ChatRequest, Message
from tests._litellm_fake import _FakeStreamChunk
from tests.onboarding.harness import disable_seeded_models, onboard

DEMO = {"Authorization": "Bearer demo-key"}
HELLO = {"messages": [{"role": "user", "content": "hello"}]}


@contextmanager
def _client():
    # raise_server_exceptions=False: a real-token stream that dies mid-body
    # re-raises out of the ASGI app (that is today's behaviour, unchanged); the
    # client has already had a 200 and the frames sent so far.
    with TestClient(create_app(), raise_server_exceptions=False) as c:
        yield c


def _rows(c: TestClient, status: str | None = None) -> list:
    async def go():
        async with c.app.state.db.session() as session:
            return await store.list_usage(session, limit=1000)
    rows = c.portal.call(go)
    return [r for r in rows if status is None or r.status == status]


def _fast_keepalive(c: TestClient, seconds: float = 0.05) -> None:
    s = c.app.state.settings
    c.app.state.settings = s.model_copy(update={
        "serving": s.serving.model_copy(update={"stream_keepalive_s": seconds})})


def _failing_backend(monkeypatch, *, delay_s: float = 0.0) -> list[str]:
    """Every backend call fails like a refused connection. Returns the list the
    fake appends each attempted model slug to, in call order."""
    attempted: list[str] = []

    async def _refuse(model, call):
        attempted.append(model)
        if delay_s:
            await asyncio.sleep(delay_s)
        raise RuntimeError("connection refused")

    monkeypatch.setattr(litellm_backend, "_acompletion", _refuse)
    return attempted


def _assert_zero_footprint(row) -> None:
    assert row.prompt_tokens == row.completion_tokens == row.total_tokens == 0
    assert row.cost_usd == 0.0
    assert row.baseline_usd == 0.0
    assert row.latency_ms > 0


# --------------------------------------------------------------------------- #
# 1. non-streamed: every candidate fails -> 502 + one error row
# --------------------------------------------------------------------------- #
def test_non_streamed_backend_failure_writes_one_error_row(monkeypatch):
    attempted = _failing_backend(monkeypatch)
    with _client() as c:
        r = c.post("/v1/chat/completions", headers=DEMO, json=HELLO)
        assert r.status_code == 502, r.text
        errors = _rows(c, "error")
        assert len(errors) == 1, [(x.status, x.model) for x in _rows(c)]
        row = errors[0]
        assert attempted, "the backend was never called"
        assert row.model == attempted[-1]
        assert row.tier != "-" and row.provider != "-"  # resolved from the registry
        _assert_zero_footprint(row)
        assert _rows(c, "ok") == []


# --------------------------------------------------------------------------- #
# 2. verified_then_stream past one keepalive: error frame + one error row
# --------------------------------------------------------------------------- #
def test_keepalive_stream_backend_failure_writes_one_error_row(monkeypatch):
    attempted = _failing_backend(monkeypatch, delay_s=0.15)
    with _client() as c:
        app = onboard(c, "err-keepalive", stream_mode="verified_then_stream")
        _fast_keepalive(c)
        with c.stream("POST", "/v1/chat/completions", headers=app.headers,
                      json={**HELLO, "stream": True}) as r:
            assert r.status_code == 200
            body = "".join(r.iter_text())
        assert body.startswith(": keepalive")  # the keepalive path really ran
        data = [e for e in body.split("\n\n") if e.startswith("data: ")]
        assert data[-1] == "data: [DONE]"
        assert json.loads(data[-2][6:])["error"]["code"] == 502

        errors = _rows(c, "error")
        assert len(errors) == 1
        assert errors[0].application_id == "err-keepalive"
        assert errors[0].model == attempted[-1]
        _assert_zero_footprint(errors[0])


# --------------------------------------------------------------------------- #
# 3. real-token stream (stream_unverified) dies mid-stream -> one error row
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("mode", ["stream_unverified", "incremental_verify"])
def test_real_token_stream_backend_failure_writes_one_error_row(monkeypatch, mode):
    attempted: list[str] = []

    async def _dies_mid_stream(model, call):
        attempted.append(model)

        async def _gen():
            yield _FakeStreamChunk(content="partial ")
            raise RuntimeError("backend connection reset")
        return _gen()

    monkeypatch.setattr(litellm_backend, "_acompletion", _dies_mid_stream)
    chosen: list[str] = []

    with _client() as c:
        pipeline = c.app.state.pipeline
        real_run_stream = pipeline.run_stream

        async def spy_run_stream(*a, **kw):
            deltas, get_meta = await real_run_stream(*a, **kw)
            chosen.append(get_meta.chosen_model)
            return deltas, get_meta

        monkeypatch.setattr(pipeline, "run_stream", spy_run_stream)
        app = onboard(c, f"err-{mode}", stream_mode=mode)
        with c.stream("POST", "/v1/chat/completions", headers=app.headers,
                      json={**HELLO, "stream": True}) as r:
            assert r.status_code == 200
            "".join(r.iter_text())

        errors = _rows(c, "error")
        assert len(errors) == 1, [(x.status, x.model) for x in _rows(c)]
        row = errors[0]
        assert chosen and row.model == chosen[0] == attempted[0]
        assert row.application_id == f"err-{mode}"
        _assert_zero_footprint(row)
        # No `ok` row: a failed stream has no final metadata to finalize.
        assert _rows(c, "ok") == []


# --------------------------------------------------------------------------- #
# 4. no error row: overflow, no eligible candidate, policy rejection, disconnect
# --------------------------------------------------------------------------- #
def test_pipeline_error_after_a_completed_backend_stream_writes_no_error_row(
        monkeypatch):
    """The backend stream finished (the gateway already observed it `ok`);
    the pipeline then fails assembling its own final metadata. That is a
    pipeline bug, not a backend failure: it must never be metered `error`."""
    from app.pipeline import Pipeline

    boomed: list[bool] = []

    def _boom(self, *a, **kw):
        boomed.append(True)
        raise RuntimeError("assemble boom")

    async def _streams_fine(model, call):
        async def _gen():
            yield _FakeStreamChunk(content="all ")
            yield _FakeStreamChunk(content="good")
            yield _FakeStreamChunk(empty_choices=True,
                                   usage={"prompt_tokens": 5, "completion_tokens": 2})
        return _gen()

    monkeypatch.setattr(litellm_backend, "_acompletion", _streams_fine)
    monkeypatch.setattr(Pipeline, "_assemble_stream_meta", _boom)
    with _client() as c:
        app = onboard(c, "err-post-stream", stream_mode="stream_unverified")
        with c.stream("POST", "/v1/chat/completions", headers=app.headers,
                      json={**HELLO, "stream": True}) as r:
            assert r.status_code == 200
            "".join(r.iter_text())
        assert boomed, "the backend stream never completed"
        assert _rows(c, "error") == [], [(x.status, x.model) for x in _rows(c)]


def test_context_window_overflow_writes_no_error_row(monkeypatch):
    async def _overflow(model, call):
        raise ContextWindowExceeded(f"{model}: maximum context length exceeded")

    monkeypatch.setattr(litellm_backend, "_acompletion", _overflow)
    with _client() as c:
        r = c.post("/v1/chat/completions", headers=DEMO, json=HELLO)
        assert r.status_code == 400, r.text
        assert _rows(c, "error") == []


def test_real_token_stream_overflow_writes_no_error_row(monkeypatch):
    async def _overflow(model, call):
        raise ContextWindowExceeded(f"{model}: maximum context length exceeded")

    monkeypatch.setattr(litellm_backend, "_acompletion", _overflow)
    with _client() as c:
        app = onboard(c, "err-unverified-overflow", stream_mode="stream_unverified")
        with c.stream("POST", "/v1/chat/completions", headers=app.headers,
                      json={**HELLO, "stream": True}) as r:
            "".join(r.iter_text())
        assert _rows(c, "error") == []


def test_no_eligible_candidate_writes_no_error_row(monkeypatch):
    attempted = _failing_backend(monkeypatch)
    with _client() as c:
        disable_seeded_models(c)  # no healthy on-prem model left
        app = onboard(c, "err-no-candidate", residency="onprem_only")
        r = app.chat(HELLO)
        assert r.status_code == 422, r.text
        assert "no model is available" in r.json()["detail"]
        assert attempted == []
        assert _rows(c, "error") == []


def test_policy_rejection_writes_no_error_row(monkeypatch):
    attempted = _failing_backend(monkeypatch)
    with _client() as c:
        r = c.post("/v1/chat/completions", headers=DEMO,
                   json={**HELLO, "model": "no-such-model"})
        assert r.status_code == 422, r.text
        assert attempted == []
        assert _rows(c, "error") == []


def _disconnected_request(c: TestClient):
    async def is_disconnected() -> bool:
        return True
    return SimpleNamespace(app=c.app, is_disconnected=is_disconnected)


def test_keepalive_client_disconnect_is_cancelled_not_error():
    """The client leaves while the backend is still working: `cancelled`, and
    never an `error` row -- even though the cancelled backend task ends in an
    exception. Driven directly (see test_client_disconnect_cancel.py: this
    TestClient cannot deliver a real mid-body disconnect)."""
    with _client() as c:
        async def go():
            async def work():
                await asyncio.sleep(10)
            task = asyncio.create_task(work())
            await asyncio.sleep(0)
            body = ChatRequest(model="auto", messages=[Message(role="user", content="hi")])
            gen = routes._keepalive_stream(
                _disconnected_request(c), task=task, keepalive_s=0.01,
                request_id="req-disc", body=body, tenant="demo",
                app_policy=AppPolicyView(), app_langfuse=None,
                profile=AppPrivacyProfile(recognizers=(), rules=()),
                session_id=None, s=c.app.state.settings, started=0.0)
            return [frame async for frame in gen]

        frames = c.portal.call(go)
        assert all(f.startswith(": keepalive") for f in frames)
        assert len(_rows(c, "cancelled")) == 1
        assert _rows(c, "error") == []


def test_real_token_stream_client_disconnect_writes_no_error_row(monkeypatch):
    """A client closing a real-token stream is GeneratorExit in the generator
    (a BaseException) -- it keeps today's finalize/drain path and must never be
    mistaken for a backend failure."""
    failed: list[str] = []

    async def spy_record_failed(request, **kw):
        failed.append(kw["model"])

    monkeypatch.setattr(routes, "_record_failed", spy_record_failed)

    async def deltas():
        yield "one "
        yield "two"

    def get_meta():
        raise RuntimeError("never reached")
    get_meta.chosen_model = "local-8b"
    get_meta.ctx = None

    with _client() as c:
        async def go():
            resp = routes._stream_unverified_response(
                _disconnected_request(c), request_id="req-sdisc", deltas=deltas(),
                get_meta=get_meta,
                body=ChatRequest(model="auto",
                                 messages=[Message(role="user", content="hi")]),
                tenant="demo", app_policy=AppPolicyView(), app_langfuse=None,
                profile=AppPrivacyProfile(recognizers=(), rules=()),
                session_id=None, s=c.app.state.settings, started=0.0)
            gen = resp.body_iterator
            await gen.__anext__()   # role frame
            await gen.aclose()      # the client went away

        c.portal.call(go)
    assert failed == []


# --------------------------------------------------------------------------- #
# 5. aggregates: error_count, and every other figure is untouched by errors
# --------------------------------------------------------------------------- #
def _row(app_id: str, request_id: str, status: str, *, cost: float, tokens: int,
         latency: float) -> dict:
    return dict(application_id=app_id, request_id=request_id, model="m",
                provider="onprem", tier="cheap", prompt_tokens=tokens,
                completion_tokens=tokens, total_tokens=2 * tokens, cost_usd=cost,
                baseline_usd=2 * cost, latency_ms=latency, sensitivity="public",
                onprem_only=True, status=status)


_BASE = [
    _row("alpha", "a-ok-1", "ok", cost=0.01, tokens=10, latency=100.0),
    _row("alpha", "a-ok-2", "ok", cost=0.03, tokens=30, latency=300.0),
    _row("alpha", "a-rej", "rejected", cost=0.0, tokens=0, latency=0.0),
    _row("alpha", "a-can", "cancelled", cost=0.0, tokens=0, latency=50.0),
    _row("beta", "b-ok-1", "ok", cost=0.02, tokens=20, latency=200.0),
]
# Error rows carry a deliberately NONZERO footprint: the route writes zeros, but
# the aggregates must exclude them by status, not by luck.
_ERRORS = [
    _row("alpha", "a-err-1", "error", cost=9.0, tokens=900, latency=9000.0),
    _row("alpha", "a-err-2", "error", cost=9.0, tokens=900, latency=9000.0),
    _row("beta", "b-err-1", "error", cost=9.0, tokens=900, latency=9000.0),
]


def _aggregate(rows: list[dict]) -> tuple[dict, dict, list, dict]:
    async def go():
        db = Database(url="sqlite+aiosqlite://")
        await db.ensure_ready()
        async with db.session() as s:
            for r in rows:
                await store.record_usage(s, **r)
            await s.commit()
        async with db.session() as s:
            total = await store.usage_summary(s)
            alpha = await store.usage_summary(s, application_id="alpha")
            by_app = await store.usage_by_application(s)
            spend = {a: await day_spend_usd(s, a) for a in ("alpha", "beta")}
        return total, alpha, by_app, spend
    return asyncio.run(go())


def _without_error_count(d: dict) -> dict:
    return {k: v for k, v in d.items() if k != "error_count"}


def test_error_count_counts_only_error_rows_and_moves_nothing_else():
    total, alpha, by_app, spend = _aggregate(_BASE + _ERRORS)
    total0, alpha0, by_app0, spend0 = _aggregate(_BASE)

    assert total["error_count"] == 3
    assert alpha["error_count"] == 2
    assert {a["application_id"]: a["error_count"] for a in by_app} == {"alpha": 2, "beta": 1}
    assert total0["error_count"] == 0
    assert all(a["error_count"] == 0 for a in by_app0)

    for key in ("count", "total_usd", "baseline_usd", "total_tokens", "prompt_tokens",
                "completion_tokens", "latency_p50_ms", "latency_p95_ms"):
        assert total[key] == total0[key], key
        assert alpha[key] == alpha0[key], key
    # ...and, more strongly, EVERY other field is identical.
    assert _without_error_count(total) == _without_error_count(total0)
    assert _without_error_count(alpha) == _without_error_count(alpha0)
    assert ([_without_error_count(a) for a in by_app]
            == [_without_error_count(a) for a in by_app0])
    # The daily budget figure ignores error rows too.
    assert spend == spend0
    assert spend["alpha"] == pytest.approx(0.04)
