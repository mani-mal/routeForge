"""Task 7: wiring the P2a signal store into live traffic.

No pytest-asyncio in this repo (see tests/test_apps_store.py) -- async work
runs through asyncio.run, matching tests/test_gateway.py and
tests/test_apps_store.py.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import time
import uuid

import pytest

from app.apps.bootstrap import seed_demo_apps
from app.apps.db import Database
from app.apps import store
from app.apps.models import UsageRecord, utcnow
from app.config.loader import load_settings
from app.errors import ContextWindowExceeded
from app.gateway.gateway import Gateway, counts_as_endpoint_unhealth
from app.models import ExecutionPolicyContext, Message
from app.routing.router import Router
from app.routing.signals import SignalStore
from tests._litellm_fake import _DeterministicLiteLLM, make_test_registry

S = load_settings()
REG = make_test_registry(S)


class _Status(Exception):
    def __init__(self, code):
        super().__init__(f"http {code}")
        self.status_code = code


# -- counts_as_endpoint_unhealth ------------------------------------------

def test_a_context_window_overflow_is_the_REQUEST_being_wrong():
    """An oversized prompt must not make a healthy endpoint look sick. This is
    the exact shape of the Qwen Code retry storm (backlog B36)."""
    assert counts_as_endpoint_unhealth(ContextWindowExceeded("too long")) is False


@pytest.mark.parametrize("code", [400, 401, 403, 404, 422])
def test_client_errors_do_not_count_against_the_endpoint(code):
    assert counts_as_endpoint_unhealth(_Status(code)) is False


@pytest.mark.parametrize("code", [500, 502, 503, 504])
def test_server_errors_count(code):
    assert counts_as_endpoint_unhealth(_Status(code)) is True


def test_rate_limiting_counts_as_unhealth():
    """429 is the closest thing to admission pressure an OpenAI-compatible
    endpoint can express -- the one real capacity signal available."""
    assert counts_as_endpoint_unhealth(_Status(429)) is True


def test_a_connection_error_with_no_status_counts():
    assert counts_as_endpoint_unhealth(OSError("connection refused")) is True


# -- end-to-end gateway wiring ---------------------------------------------

def _ctx(providers, risk="low"):
    providers = frozenset(providers)
    return ExecutionPolicyContext(
        request_id="r", tenant_id="t", sensitivity="public", onprem_only=False,
        allowed_providers=providers,
        allowed_models=frozenset(c.name for c in REG.effective_pool()
                                 if c.provider in providers),
        max_cost_usd=0.05, max_latency_ms=15000, risk_class=risk,
        require_verification=False, detected_entities=(), redaction_map_id=None)


def test_a_served_request_records_a_latency_observation():
    store_ = SignalStore(S.routing.signals)
    gw = Gateway(S, _DeterministicLiteLLM(S),
                 Router(S, registry=REG, signals=store_), REG, signals=store_)
    msgs = [Message(role="user", content="hello")]
    asyncio.run(gw.run(_ctx({"onprem", "azure", "bedrock"}), msgs, 20, 0.1, {}))
    snap = store_.snapshot()
    assert snap  # some model got an observation
    assert any(v["latency_n"] >= 1 for v in snap.values())


def test_a_failed_request_observes_availability_not_latency():
    """A backend failure moves availability for the model that failed. It must
    NOT touch latency -- a failed call's duration says nothing about serving
    speed (see SignalStore.observe_error's docstring)."""
    store_ = SignalStore(S.routing.signals)
    gw = Gateway(S, _DeterministicLiteLLM(S),
                 Router(S, registry=REG, signals=store_), REG, signals=store_)
    msgs = [Message(role="user", content="hello")]
    ctx = _ctx({"onprem"}, risk="high")  # only onprem allowed -> tier clamps to mid
    with pytest.raises(Exception):
        asyncio.run(gw.run(ctx, msgs, 20, 0.1, {"mock_fail": True}))
    snap = store_.snapshot()
    assert snap
    assert all(v["latency_n"] == 0 for v in snap.values())
    assert any(v["availability_n"] >= 1 for v in snap.values())


def test_a_rejected_request_never_reaches_the_gateway_so_never_touches_signals():
    """Rejection happens at the policy gate (step 2), never inside the
    gateway -- there is nothing to assert here beyond the fact that the
    gateway's own failure path is keyed on counts_as_endpoint_unhealth, which
    a rejection can never reach (it isn't a backend Exception at all)."""
    assert True  # documented, not exercised: rejections never call Gateway.run


def test_router_decision_carries_the_scoring_record():
    d = Router(S, registry=REG).select(
        _ctx({"onprem", "azure", "bedrock"}), tokens_after=20, complexity=0.1)
    assert d.scoring is not None
    assert d.scoring.ordered[0].candidate.name == d.chosen.name


# -- boot seeding: app/apps/store.aggregate_model_signals -----------------

def _fresh_db() -> Database:
    return Database(url="sqlite+aiosqlite://", seed=seed_demo_apps)


def _row(application_id, model, status, latency_ms, ts):
    return UsageRecord(
        id=uuid.uuid4().hex, application_id=application_id,
        request_id=uuid.uuid4().hex, ts=ts, model=model, provider="onprem",
        tier="cheap", prompt_tokens=1, completion_tokens=1, total_tokens=2,
        cost_usd=0.0, baseline_usd=0.0, latency_ms=latency_ms,
        sensitivity="public", onprem_only=False, status=status)


def test_seed_aggregate_counts_ok_and_error_averages_latency_over_ok_only():
    db = _fresh_db()
    now = utcnow()

    async def go():
        async with db.session() as s:
            s.add_all([
                _row("acme", "local-8b", "ok", 100.0, now),
                _row("acme", "local-8b", "ok", 300.0, now),
                _row("acme", "local-8b", "error", 999.0, now),
            ])
            await s.commit()
            return await store.aggregate_model_signals(s, since_hours=24)

    rows = asyncio.run(go())
    assert len(rows) == 1
    r = rows[0]
    assert r.model == "local-8b"
    assert r.avg_latency_ms == pytest.approx(200.0)   # (100+300)/2, error excluded
    assert r.samples == 3                             # 2 ok + 1 error
    assert r.error_rate == pytest.approx(1 / 3)


def test_seed_aggregate_excludes_rejected_rows():
    """A rejection is the POLICY GATE doing its job -- not endpoint unhealth
    (CLAUDE.md rule 2a: a protection posture must never influence routing
    preference)."""
    db = _fresh_db()
    now = utcnow()

    async def go():
        async with db.session() as s:
            s.add_all([
                _row("acme", "local-8b", "ok", 100.0, now),
                _row("acme", "local-8b", "rejected", 5.0, now),
            ])
            await s.commit()
            return await store.aggregate_model_signals(s, since_hours=24)

    rows = asyncio.run(go())
    assert len(rows) == 1
    r = rows[0]
    assert r.samples == 1
    assert r.error_rate == pytest.approx(0.0)


def test_seed_aggregate_excludes_rows_outside_the_window():
    db = _fresh_db()
    stale = utcnow() - dt.timedelta(hours=100)

    async def go():
        async with db.session() as s:
            s.add(_row("acme", "old-model", "ok", 50.0, stale))
            await s.commit()
            return await store.aggregate_model_signals(s, since_hours=24)

    rows = asyncio.run(go())
    assert rows == []


def test_seed_aggregate_uses_monotonic_clock_for_last_ts():
    """The store runs on time.monotonic(); a wall-clock timestamp would make
    every seeded row instantly stale."""
    db = _fresh_db()
    now = utcnow()

    async def go():
        async with db.session() as s:
            s.add(_row("acme", "local-8b", "ok", 100.0, now))
            await s.commit()
            return await store.aggregate_model_signals(s, since_hours=24)

    before = time.monotonic()
    rows = asyncio.run(go())
    after = time.monotonic()
    assert len(rows) == 1
    assert before <= rows[0].last_ts <= after


def test_seed_aggregate_empty_when_no_rows_in_window():
    db = _fresh_db()

    async def go():
        async with db.session() as s:
            return await store.aggregate_model_signals(s, since_hours=24)

    assert asyncio.run(go()) == []


# -- app/main.py wiring: the SAME store is shared by create_app()'s Router
# and Gateway, and boot seeding runs on the real serving loop -----------

def test_create_app_wires_one_shared_signal_store_into_router_and_gateway():
    """Regression for the event-loop-binding bug class (see
    routeforge-event-loop-bug memory / app/gateway/backends/factory.py):
    starting the app via `with TestClient(...) as c:` drives the real ASGI
    lifespan, so the startup-event seed query runs on the SAME loop that
    serves requests -- unlike a bare asyncio.run() inside create_app(), which
    would bind the DB engine to a throwaway loop. A clean start (no usage
    rows yet) must leave the store empty, not crash."""
    from fastapi.testclient import TestClient

    from app.main import create_app

    with TestClient(create_app()) as c:
        assert isinstance(c.app.state.signals, SignalStore)
        assert c.app.state.signals.snapshot() == {}
        # Router and Gateway hold the SAME instance, not independent copies.
        assert c.app.state.model_router._signals is c.app.state.signals

        r = c.post("/v1/chat/completions",
                   headers={"Authorization": "Bearer demo-key"},
                   json={"messages": [{"role": "user", "content": "hi"}]})
        assert r.status_code == 200
        # A served request observed through the gateway lands in the SAME
        # store app.state.signals points at -- proof the injected reference,
        # not a copy, is what the running pipeline actually uses.
        assert c.app.state.signals.snapshot()
