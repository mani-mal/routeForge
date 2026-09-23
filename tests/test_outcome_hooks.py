"""P2b-2 Task 4: every routed request writes one content-free
`routing_outcomes` row, driven through the real HTTP boundary.

Consumer-contract style (see .claude/skills/consumer-contract-tests): a real
app from `create_app()`, a real `TestClient`, and a deterministic backend
(the litellm fakes). The row is checked against what the client and the
server actually did -- the chosen model, the attempts, the versions -- never
against mocks of the route internals.

What earns a row: `ok`, `backend_failed`, `verify_failed`, `no_eligible`,
`context_overflow`. What never does: a cancelled request, a 401, a 429 (rate
limit or budget), a policy-gate error, and a pinned-model rejection. A
failing outcome write never changes the response, and the rows never carry
request content.
"""
from __future__ import annotations

import asyncio
import json
from contextlib import contextmanager

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.api import routes
from app.apps import store
from app.apps.models import RoutingOutcome
from app.errors import ContextWindowExceeded
from app.gateway.backends import litellm_backend
from app.main import create_app
from app.models import VerificationRecord
from tests._litellm_fake import CALLS, _FakeStreamChunk, make_recording_acompletion, make_response
from tests.conftest import v1_error
from tests.onboarding.harness import (
    disable_seeded_models, install_dual_fake, onboard,
)

DEMO = {"Authorization": "Bearer demo-key"}
HELLO = {"messages": [{"role": "user", "content": "hello"}]}
SSN = "123-45-6789"
EMAIL = "jane@example.com"


@contextmanager
def _client():
    with TestClient(create_app(), raise_server_exceptions=False) as c:
        yield c


def _outcomes(c: TestClient) -> list[RoutingOutcome]:
    async def go():
        async with c.app.state.db.session() as session:
            return list((await session.execute(select(RoutingOutcome))).scalars())
    return c.portal.call(go)


def _usage(c: TestClient) -> list:
    async def go():
        async with c.app.state.db.session() as session:
            return await store.list_usage(session, limit=1000)
    return c.portal.call(go)


def _columns(row: RoutingOutcome) -> dict:
    return {col.name: getattr(row, col.name) for col in RoutingOutcome.__table__.columns}


def _only(c: TestClient) -> RoutingOutcome:
    rows = _outcomes(c)
    assert len(rows) == 1, [(r.status, r.request_id) for r in rows]
    return rows[0]


def _set_outcomes(c: TestClient, *, enabled: bool) -> None:
    s = c.app.state.settings
    outcomes = s.serving.outcomes.model_copy(update={"enabled": enabled})
    c.app.state.settings = s.model_copy(update={
        "serving": s.serving.model_copy(update={"outcomes": outcomes})})


def _fast_keepalive(c: TestClient, seconds: float = 0.05) -> None:
    s = c.app.state.settings
    c.app.state.settings = s.model_copy(update={
        "serving": s.serving.model_copy(update={"stream_keepalive_s": seconds})})


def _failing_backend(monkeypatch, *, delay_s: float = 0.0) -> list[str]:
    attempted: list[str] = []

    async def _refuse(model, call):
        attempted.append(model)
        if delay_s:
            await asyncio.sleep(delay_s)
        raise RuntimeError("connection refused")

    monkeypatch.setattr(litellm_backend, "_acompletion", _refuse)
    return attempted


# --------------------------------------------------------------------------- #
# 1. a plain request -> one `ok` row that matches what was served
# --------------------------------------------------------------------------- #
def test_plain_request_writes_one_ok_row():
    with _client() as c:
        r = c.post("/v1/chat/completions", headers=DEMO, json=HELLO)
        assert r.status_code == 200, r.text
        body = r.json()
        row = _only(c)
        assert row.status == "ok"
        assert row.request_id == body["id"]
        assert row.application_id == "default"
        assert row.chosen == body["model"]
        assert row.attempts == [body["model"]]
        assert body["model"] in row.eligible
        assert row.routing_policy_version == c.app.state.settings.routing_policy_version
        assert row.endpoint_registry_version == c.app.state.registry.registry_version()
        assert row.app_policy_version == body["policy"].get("app_policy_version")
        assert row.prompt_tokens_bucket == "<1k"
        assert row.verify_mode in ("not_required", "verified")
        assert row.tier_target in ("cheap", "mid", "frontier")
        assert row.complexity is not None
        assert row.workload is None and row.workload_source == "none"
        # The outcome sits beside, never instead of, the usage row.
        assert [u.status for u in _usage(c)] == ["ok"]


# --------------------------------------------------------------------------- #
# 2. workload: header > metadata; invalid still serves; metadata not forwarded
# --------------------------------------------------------------------------- #
def test_workload_header_is_stored():
    with _client() as c:
        r = c.post("/v1/chat/completions",
                   headers={**DEMO, "x-routeforge-workload": "Claims.Summary"},
                   json={**HELLO, "metadata": {"workload": "other.tag"}})
        assert r.status_code == 200, r.text
        row = _only(c)
        assert row.workload == "claims.summary"
        assert row.workload_source == "client"


def test_invalid_workload_header_still_serves():
    with _client() as c:
        r = c.post("/v1/chat/completions",
                   headers={**DEMO, "x-routeforge-workload": "<script>"}, json=HELLO)
        assert r.status_code == 200, r.text
        row = _only(c)
        assert row.workload is None
        assert row.workload_source == "invalid"


def test_metadata_workload_is_stored_and_not_forwarded_upstream(monkeypatch):
    CALLS.clear()
    monkeypatch.setattr(litellm_backend, "acompletion",
                        make_recording_acompletion(make_response(content="fine")))
    with _client() as c:
        r = c.post("/v1/chat/completions", headers=DEMO,
                   json={**HELLO, "metadata": {"workload": "claims.summary"}})
        assert r.status_code == 200, r.text
        row = _only(c)
        assert row.workload == "claims.summary"
        assert row.workload_source == "client"
    assert CALLS, "backend was never called"
    for call in CALLS:
        assert "metadata" not in call, call.keys()
        assert "claims.summary" not in json.dumps(call, default=str)


# --------------------------------------------------------------------------- #
# 3. backend always fails -> 502 + backend_failed with the attempts
# --------------------------------------------------------------------------- #
def test_backend_failure_writes_backend_failed_row(monkeypatch):
    attempted = _failing_backend(monkeypatch)
    with _client() as c:
        r = c.post("/v1/chat/completions", headers=DEMO, json=HELLO)
        assert r.status_code == 502, r.text
        row = _only(c)
        assert row.status == "backend_failed"
        assert row.attempts and row.attempts[-1] == attempted[-1]
        assert set(row.attempts) <= set(attempted)
        assert row.chosen is None
        assert row.endpoint_registry_version == c.app.state.registry.registry_version()
        assert row.sensitivity is not None  # the gate ran; its features are kept
        # Metering unchanged: still exactly one `error` usage row.
        assert [u.status for u in _usage(c)] == ["error"]


def test_keepalive_stream_backend_failure_writes_backend_failed_row(monkeypatch):
    attempted = _failing_backend(monkeypatch, delay_s=0.15)
    with _client() as c:
        app = onboard(c, "oc-keepalive", stream_mode="verified_then_stream")
        _fast_keepalive(c)
        with c.stream("POST", "/v1/chat/completions", headers=app.headers,
                      json={**HELLO, "stream": True}) as r:
            assert r.status_code == 200
            body = "".join(r.iter_text())
        assert body.startswith(": keepalive")
        row = _only(c)
        assert row.status == "backend_failed"
        assert row.application_id == "oc-keepalive"
        assert row.attempts and row.attempts[-1] == attempted[-1]


@pytest.mark.parametrize("mode", ["stream_unverified", "incremental_verify"])
def test_real_token_stream_backend_failure_writes_backend_failed_row(monkeypatch, mode):
    attempted: list[str] = []

    async def _dies_mid_stream(model, call):
        attempted.append(model)

        async def _gen():
            yield _FakeStreamChunk(content="partial ")
            raise RuntimeError("backend connection reset")
        return _gen()

    monkeypatch.setattr(litellm_backend, "_acompletion", _dies_mid_stream)
    with _client() as c:
        app = onboard(c, f"oc-{mode}", stream_mode=mode)
        with c.stream("POST", "/v1/chat/completions", headers=app.headers,
                      json={**HELLO, "stream": True}) as r:
            assert r.status_code == 200
            "".join(r.iter_text())
        row = _only(c)
        assert row.status == "backend_failed"
        assert row.attempts == [attempted[0]]
        assert row.sensitivity is not None


# --------------------------------------------------------------------------- #
# 4. onprem_only with no on-prem model -> 422 + no_eligible (residency)
# --------------------------------------------------------------------------- #
def _with_azure_enabled(monkeypatch) -> None:
    """Boot the app with the Azure provider on, so the static cloud pool has a
    candidate for the residency filter to reject under `onprem_only`."""
    import app.main as main_mod
    from app.config.loader import load_settings

    def _load():
        s = load_settings()
        providers = dict(s.serving.providers)
        providers["azure"] = providers["azure"].model_copy(update={"enabled": True})
        return s.model_copy(update={
            "serving": s.serving.model_copy(update={"providers": providers})})

    monkeypatch.setattr(main_mod, "load_settings", _load)


def test_no_eligible_writes_no_eligible_row_with_rejections(monkeypatch):
    _with_azure_enabled(monkeypatch)
    with _client() as c:
        disable_seeded_models(c)
        app = onboard(c, "oc-no-candidate", residency="onprem_only")
        r = app.chat(HELLO)
        assert r.status_code == 422, r.text
        row = _only(c)
        assert row.status == "no_eligible"
        assert row.onprem_only is True
        codes = {x["code"] for x in row.rejected}
        assert "residency" in codes, row.rejected
        assert all(set(x) == {"name", "code"} for x in row.rejected)


# --------------------------------------------------------------------------- #
# 5. context overflow -> 400 + context_overflow
# --------------------------------------------------------------------------- #
def test_context_overflow_writes_context_overflow_row(monkeypatch):
    async def _overflow(model, call):
        raise ContextWindowExceeded(f"{model}: maximum context length exceeded")

    monkeypatch.setattr(litellm_backend, "_acompletion", _overflow)
    with _client() as c:
        r = c.post("/v1/chat/completions", headers=DEMO, json=HELLO)
        assert r.status_code == 400, r.text
        row = _only(c)
        assert row.status == "context_overflow"


# --------------------------------------------------------------------------- #
# 6. step-9 failure -> 422 + verify_failed; a pinned-model 422 writes nothing
# --------------------------------------------------------------------------- #
def _failing_verify(monkeypatch) -> None:
    import app.pipeline as pipeline_mod

    def _verify(det, ctx, text, **kw):
        return text, VerificationRecord(required=True, performed=True, passed=False,
                                        reason="leak detected; failed closed")

    monkeypatch.setattr(pipeline_mod, "verify", _verify)


def test_verify_failure_writes_verify_failed_row(monkeypatch):
    _failing_verify(monkeypatch)
    with _client() as c:
        r = c.post("/v1/chat/completions", headers=DEMO, json=HELLO)
        assert r.status_code == 422, r.text
        assert v1_error(r)["code"] == "response_failed_verification"
        row = _only(c)
        assert row.status == "verify_failed"
        # No free text from the verify record reaches the row.
        assert "leak detected" not in json.dumps(_columns(row), default=str)


def test_keepalive_stream_verify_failure_writes_verify_failed_row(monkeypatch):
    _failing_verify(monkeypatch)
    real = litellm_backend.acompletion

    async def _slow(**kw):
        await asyncio.sleep(0.15)
        return await real(**kw)

    monkeypatch.setattr(litellm_backend, "acompletion", _slow)
    with _client() as c:
        app = onboard(c, "oc-keepalive-verify", stream_mode="verified_then_stream")
        _fast_keepalive(c)
        with c.stream("POST", "/v1/chat/completions", headers=app.headers,
                      json={**HELLO, "stream": True}) as r:
            body = "".join(r.iter_text())
        assert body.startswith(": keepalive")
        assert "response_failed_verification" in body
        assert _only(c).status == "verify_failed"


def test_pinned_model_rejection_writes_no_row():
    with _client() as c:
        r = c.post("/v1/chat/completions", headers=DEMO,
                   json={**HELLO, "model": "no-such-model"})
        assert r.status_code == 422, r.text
        assert v1_error(r)["code"] == "model_not_found"
        assert _outcomes(c) == []


# --------------------------------------------------------------------------- #
# 7. cancelled, 401 and 429 write no row
# --------------------------------------------------------------------------- #
def test_cancelled_request_writes_no_row(monkeypatch):
    async def _gone(task, is_disconnected, poll_s):
        task.cancel()
        raise routes.ClientDisconnected()

    monkeypatch.setattr(routes, "_await_unless_disconnected", _gone)
    with _client() as c:
        r = c.post("/v1/chat/completions", headers=DEMO, json=HELLO)
        assert r.status_code == 499
        assert [u.status for u in _usage(c)] == ["cancelled"]
        assert _outcomes(c) == []


def test_unauthorized_writes_no_row():
    with _client() as c:
        r = c.post("/v1/chat/completions",
                   headers={"Authorization": "Bearer nope"}, json=HELLO)
        assert r.status_code == 401
        assert _outcomes(c) == []


def test_rate_limited_writes_no_row():
    with _client() as c:
        app = onboard(c, "oc-rpm", max_rpm=1)
        assert app.chat(HELLO).status_code == 200
        r = app.chat(HELLO)
        assert r.status_code == 429, r.text
        assert [o.status for o in _outcomes(c)] == ["ok"]


def test_budget_exceeded_writes_no_row():
    with _client() as c:
        app = onboard(c, "oc-budget", max_spend_usd_per_day=0.0)
        r = app.chat(HELLO)
        assert r.status_code == 429, r.text
        assert _outcomes(c) == []


def test_policy_gate_error_writes_no_row(monkeypatch):
    from app.errors import PolicyGateError

    with _client() as c:
        async def _boom(*a, **kw):
            raise PolicyGateError("policy evaluation failed")

        monkeypatch.setattr(c.app.state.pipeline, "run", _boom)
        r = c.post("/v1/chat/completions", headers=DEMO, json=HELLO)
        assert r.status_code == 503
        assert _outcomes(c) == []


# --------------------------------------------------------------------------- #
# 8. outcomes.enabled=False -> no row, same response
# --------------------------------------------------------------------------- #
def _stable(body: dict) -> dict:
    """The response minus the per-request values (id, clock, trace ids)."""
    drop = {"id", "created", "trace_id", "langfuse_url", "trace", "session_id",
            "latency_ms"}
    return {k: v for k, v in body.items() if k not in drop}


def test_outcomes_disabled_writes_no_row_and_same_response():
    # Two fresh apps, one request each: the live latency/availability signals
    # (`policy.scoring[*].n`) then start from the same state in both, so the
    # only differences left are the per-request ids and clock values.
    with _client() as c:
        on = c.post("/v1/chat/completions", headers=DEMO, json=HELLO)
        assert [o.request_id for o in _outcomes(c)] == [on.json()["id"]]
    with _client() as c:
        _set_outcomes(c, enabled=False)
        off = c.post("/v1/chat/completions", headers=DEMO, json=HELLO)
        assert _outcomes(c) == []
    assert on.status_code == off.status_code == 200
    assert set(on.json()) == set(off.json())
    assert _stable(on.json()) == _stable(off.json())


def test_outcomes_disabled_writes_no_failure_row(monkeypatch):
    _failing_backend(monkeypatch)
    with _client() as c:
        _set_outcomes(c, enabled=False)
        r = c.post("/v1/chat/completions", headers=DEMO, json=HELLO)
        assert r.status_code == 502
        assert _outcomes(c) == []


# --------------------------------------------------------------------------- #
# 9. a failing outcome write never fails the request or its metering
# --------------------------------------------------------------------------- #
def test_outcome_write_failure_is_swallowed(monkeypatch, caplog):
    async def _boom(session, row):
        raise RuntimeError("outcome table unavailable")

    monkeypatch.setattr(store, "write_outcome", _boom)
    with _client() as c:
        with caplog.at_level("WARNING", logger="serving.obs"):
            r = c.post("/v1/chat/completions", headers=DEMO, json=HELLO)
        assert r.status_code == 200, r.text
        rid = r.json()["id"]
        assert [(u.status, u.request_id) for u in _usage(c)] == [("ok", rid)]
        assert _outcomes(c) == []
    assert any("outcome record failed" in rec.getMessage() and rid in rec.getMessage()
               for rec in caplog.records)


# --------------------------------------------------------------------------- #
# 10. privacy: no column carries request content
# --------------------------------------------------------------------------- #
def test_outcome_row_carries_no_request_content():
    with _client() as c:
        r = c.post("/v1/chat/completions", headers=DEMO, json={"messages": [
            {"role": "user", "content": f"My SSN is {SSN}, email {EMAIL}"}]})
        assert r.status_code == 200, r.text
        row = _only(c)
        dumped = json.dumps(_columns(row), default=str)
        assert SSN not in dumped
        assert EMAIL not in dumped
        assert "My SSN" not in dumped
        # The entity TYPES are the content-free signal that remains.
        assert row.detected_entity_types, row.detected_entity_types


# --------------------------------------------------------------------------- #
# 11. streaming modes record their verify mode
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("mode, expected", [
    ("stream_unverified", {"waived_stream"}),
    ("incremental_verify", {"incremental"}),
    ("verified_then_stream", {"verified", "not_required"}),
])
def test_stream_modes_record_verify_mode(monkeypatch, mode, expected):
    install_dual_fake(monkeypatch)
    with _client() as c:
        app = onboard(c, f"oc-sm-{mode}", stream_mode=mode)
        with c.stream("POST", "/v1/chat/completions", headers=app.headers,
                      json={**HELLO, "stream": True}) as r:
            assert r.status_code == 200
            "".join(r.iter_text())
        row = _only(c)
        assert row.status == "ok"
        assert row.verify_mode in expected, row.verify_mode
        assert row.chosen


def test_stream_workload_header_is_stored(monkeypatch):
    install_dual_fake(monkeypatch)
    with _client() as c:
        app = onboard(c, "oc-sm-workload", stream_mode="stream_unverified")
        with c.stream("POST", "/v1/chat/completions",
                      headers={**app.headers, "x-routeforge-workload": "chat.support"},
                      json={**HELLO, "stream": True}) as r:
            "".join(r.iter_text())
        row = _only(c)
        assert row.workload == "chat.support"
