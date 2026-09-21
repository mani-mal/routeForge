"""Task 7: wiring the P2a signal store into live traffic.

No pytest-asyncio in this repo (see tests/test_apps_store.py) -- async work
runs through asyncio.run, matching tests/test_gateway.py and
tests/test_apps_store.py.
"""
from __future__ import annotations

import asyncio
import time

import pytest

from app.apps.bootstrap import seed_demo_apps
from app.apps.db import Database
from app.apps import store
from app.apps.policy import AppPolicyView
from app.compression.ccr import CCRStore
from app.config.loader import load_settings
from app.errors import ContextWindowExceeded
from app.gateway.gateway import Gateway, counts_as_endpoint_unhealth
from app.models import ChatRequest, ExecutionPolicyContext, Message
from app.pipeline import Pipeline, PipelineRejected
from app.policy.detectors import make_detector
from app.policy.gate import Gate
from app.routing.observations import Observation, ObservationSink
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


@pytest.mark.parametrize("code", [400, 401, 403, 404, 422, 428, 430, 499])
def test_client_errors_do_not_count_against_the_endpoint(code):
    """428, 430 and 499 close the upper-boundary gap of `not (400 <= code <
    500)` that the pre-existing 400/401/403/404/422 cases don't reach (B46).
    499 (nginx's "client closed request") is the practically important one:
    it is not rare behind a proxy, and misclassifying it as endpoint unhealth
    would push traffic off a healthy endpoint on every client disconnect --
    precisely the failure mode ruling 2 exists to prevent."""
    assert counts_as_endpoint_unhealth(_Status(code)) is False


@pytest.mark.parametrize("code", [500, 502, 503, 504])
def test_server_errors_count(code):
    assert counts_as_endpoint_unhealth(_Status(code)) is True


@pytest.mark.parametrize("code", [399, 429])
def test_rate_limiting_and_the_sub_400_boundary_count_as_unhealth(code):
    """429 is the closest thing to admission pressure an OpenAI-compatible
    endpoint can express -- the one real capacity signal available. 399
    closes the LOWER boundary of `not (400 <= code < 500)` (B46): not a real
    HTTP status, but exactly where an off-by-one (`400 < code`, ...) would
    first show up -- it falls outside the 4xx range entirely, so the
    predicate correctly counts it as endpoint unhealth like any other
    non-4xx code."""
    assert counts_as_endpoint_unhealth(_Status(code)) is True


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
    """Rejection happens before the gateway is ever called -- here, a pinned
    `model` the app's policy does not permit is refused by
    `Pipeline.run`'s eligibility check (app/pipeline.py), which raises
    `PipelineRejected` well before `Router.select`/`Gateway.run` run. If a
    rejection ever did reach the gateway, this SignalStore (shared with the
    Router and Gateway the way `test_a_served_request_records_a_latency_
    observation` wires it) would pick up an observation -- so the assertion
    that its snapshot stays empty is a real, falsifiable check of the claim
    in this test's name, not just documentation."""
    store_ = SignalStore(S.routing.signals)
    det = make_detector(S.serving.pii_detection)
    gate = Gate(S, det, registry=REG)
    gateway = Gateway(S, _DeterministicLiteLLM(S),
                      Router(S, registry=REG, signals=store_), REG, signals=store_)
    pipe = Pipeline(S, gate, det, gateway,
                    co_cfg_provider=lambda: S.serving.context_optimization,
                    ccr=CCRStore(3600), registry=REG)
    req = ChatRequest(model="local-8b",
                      messages=[Message(role="user", content="hello")])

    with pytest.raises(PipelineRejected):
        asyncio.run(pipe.run(
            "r1", "acme", req,
            app_policy=AppPolicyView(allowed_models=frozenset({"local-32b"}))))

    assert store_.snapshot() == {}


def test_router_decision_carries_the_scoring_record():
    d = Router(S, registry=REG).select(
        _ctx({"onprem", "azure", "bedrock"}), tokens_after=20, complexity=0.1)
    assert d.scoring is not None
    assert d.scoring.ordered[0].candidate.name == d.chosen.name


# -- the observation log: app/apps/store.write/load_seed/prune_observations

def _fresh_db() -> Database:
    return Database(url="sqlite+aiosqlite://", seed=seed_demo_apps)


def _obs(ts, model="m", outcome="ok", latency=100.0, streamed=False, ttft=None):
    return Observation(ts, model, outcome,
                       latency if outcome == "ok" and not streamed else None,
                       streamed, ttft)


def test_observations_round_trip_through_the_table():
    db = _fresh_db()
    now = time.time()
    written = [_obs(now - 30), _obs(now - 20, outcome="error"),
               _obs(now - 10, streamed=True, ttft=42.0)]

    async def go():
        async with db.session() as s:
            await store.write_observations(s, written)
            await s.commit()
            return await store.load_seed_observations(s, since_hours=1, per_model_cap=100)

    got = asyncio.run(go())
    assert [(o.model, o.outcome, o.latency_ms, o.streamed, o.ttft_ms) for o in got] == \
           [(o.model, o.outcome, o.latency_ms, o.streamed, o.ttft_ms) for o in written]
    assert [round(o.ts, 3) for o in got] == [round(o.ts, 3) for o in written]


def test_seed_load_caps_per_model_keeping_the_most_recent_and_orders_ascending():
    db = _fresh_db()
    now = time.time()
    rows = [_obs(now - 100 + i, model="a") for i in range(10)] + \
           [_obs(now - 50 + i, model="b") for i in range(3)]

    async def go():
        async with db.session() as s:
            await store.write_observations(s, rows)
            await s.commit()
            return await store.load_seed_observations(s, since_hours=1, per_model_cap=4)

    got = asyncio.run(go())
    assert [o.model for o in got].count("a") == 4
    assert [o.model for o in got].count("b") == 3
    a_ts = [o.ts for o in got if o.model == "a"]
    assert min(a_ts) == pytest.approx(now - 100 + 6, abs=1e-3)   # the 4 newest
    assert [o.ts for o in got] == sorted(o.ts for o in got)


def test_seed_load_breaks_identical_timestamp_ties_by_insertion_order():
    """`SignalStore.replay` requires ascending `ts`, but is silent on ties --
    two observations can share a `ts` (same wall-clock second). The per-model
    query orders `(ts desc, id desc)` to grab the newest N; without an `id`
    ascending tie-break in the final sort, a stable sort on `ts` alone would
    leave same-`ts` rows in that id-DESCENDING order, i.e. reversed relative
    to how they were written."""
    db = _fresh_db()
    now = time.time()
    same_ts = now - 10
    first = _obs(same_ts, model="a", latency=111.0)
    interleaved = _obs(same_ts, model="b", latency=222.0)   # a different model, same ts
    second = _obs(same_ts, model="a", latency=333.0)

    async def go():
        async with db.session() as s:
            await store.write_observations(s, [first, interleaved, second])
            await s.commit()
            return await store.load_seed_observations(s, since_hours=1, per_model_cap=10)

    got = asyncio.run(go())
    a_latencies = [o.latency_ms for o in got if o.model == "a"]
    assert a_latencies == [111.0, 333.0]   # insertion (id) order, not id-desc


def test_seed_load_excludes_rows_outside_the_window():
    db = _fresh_db()
    now = time.time()

    async def go():
        async with db.session() as s:
            await store.write_observations(s, [_obs(now - 7200), _obs(now - 60)])
            await s.commit()
            return await store.load_seed_observations(s, since_hours=1, per_model_cap=10)

    assert len(asyncio.run(go())) == 1


def test_prune_deletes_only_rows_past_the_window():
    db = _fresh_db()
    now = time.time()

    async def go():
        async with db.session() as s:
            await store.write_observations(s, [_obs(now - 7200), _obs(now - 60)])
            await s.commit()
            n = await store.prune_observations(s, older_than_hours=1)
            await s.commit()
            left = await store.load_seed_observations(s, since_hours=24, per_model_cap=10)
            return n, left

    n, left = asyncio.run(go())
    assert n == 1 and len(left) == 1


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


# -- Task 5: lifespan wiring -- sink, replay seed, flusher, shutdown flush --

def _prewrite_observations(url: str, model: str, n: int, *, now: float | None = None
                           ) -> None:
    """Write `n` recent `ok` observations for `model` straight to the table,
    via a SEPARATE `Database` on the same file URL -- never `app.state.db`,
    and never inside `create_app`'s throwaway loop. Creates the schema on
    first use (`Database.session()` calls `ensure_ready()`)."""
    now = time.time() if now is None else now
    db = Database(url=url, seed=None)
    obs = [Observation(now - (n - i), model, "ok", 100.0 + i, False, None)
          for i in range(n)]

    async def go():
        async with db.session() as s:
            await store.write_observations(s, obs)
            await s.commit()

    asyncio.run(go())


def test_boot_replays_the_log_so_a_model_is_measured_without_live_traffic(
        tmp_path, monkeypatch):
    """End to end through the real lifespan: rows in endpoint_observations
    before boot -> the store reports `measured` after startup."""
    from fastapi.testclient import TestClient

    from app.main import create_app

    url = f"sqlite+aiosqlite:///{tmp_path / 'rf.db'}"
    monkeypatch.setenv("DATABASE_URL", url)
    _prewrite_observations(url, "local-8b", 6)

    with TestClient(create_app()) as c:
        sig = c.app.state.signals.latency("local-8b", "cheap")
        assert sig.source == "measured"
        assert sig.samples == 6


def test_a_served_request_reaches_the_table_after_a_flush(tmp_path, monkeypatch):
    """Exit the `with` block (shutdown flush) and read the table back with a
    fresh `Database` on the same file URL -- also exercises the final flush."""
    from fastapi.testclient import TestClient

    from app.main import create_app

    url = f"sqlite+aiosqlite:///{tmp_path / 'rf.db'}"
    monkeypatch.setenv("DATABASE_URL", url)

    with TestClient(create_app()) as c:
        r = c.post("/v1/chat/completions",
                   headers={"Authorization": "Bearer demo-key"},
                   json={"messages": [{"role": "user", "content": "hi"}]})
        assert r.status_code == 200

    async def _read():
        db = Database(url=url, seed=None)
        async with db.session() as s:
            return await store.load_seed_observations(
                s, since_hours=24, per_model_cap=1000)

    rows = asyncio.run(_read())
    assert any(o.outcome == "ok" for o in rows)


def test_flush_swallows_a_db_failure():
    from app.main import flush_observations

    sink = ObservationSink(10)
    sink.put(Observation(time.time(), "m", "ok", 1.0, False, None))

    class _BrokenDB:
        def session(self):
            raise RuntimeError("db down")

    assert asyncio.run(flush_observations(_BrokenDB(), sink)) == 0


def test_seed_failure_leaves_an_empty_store_and_the_app_serving(monkeypatch):
    """Monkeypatch store.load_seed_observations to raise; the app starts,
    the store is empty, and a request is served 200."""
    from fastapi.testclient import TestClient

    from app.main import create_app

    async def _boom(*a, **kw):
        raise RuntimeError("seed boom")

    monkeypatch.setattr(store, "load_seed_observations", _boom)

    with TestClient(create_app()) as c:
        assert c.app.state.signals.snapshot() == {}
        r = c.post("/v1/chat/completions",
                   headers={"Authorization": "Bearer demo-key"},
                   json={"messages": [{"role": "user", "content": "hi"}]})
        assert r.status_code == 200
