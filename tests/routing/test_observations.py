"""Signal hardening: the observation log (spec §3). The load-bearing test is
replay identity (§3.5): replaying the log reproduces the live state up to a
constant clock shift."""
from __future__ import annotations

import asyncio

import pytest
from hypothesis import given, settings, strategies as st

from app.config.loader import load_settings
from app.config.schema import SignalsCfg
from app.gateway.backends.base import StreamChunk
from app.gateway.gateway import Gateway
from app.models import ExecutionPolicyContext, GenerationResult, Message
from app.routing.observations import Observation, ObservationSink
from app.routing.router import Router
from app.routing.signals import SignalStore
from tests._litellm_fake import make_test_registry

CFG = SignalsCfg(min_samples=3, half_life_s=100.0, staleness_s=1000.0)


def _live(sink=None, offset=1_000_000.0):
    return SignalStore(CFG, sink=sink, wall_offset=offset)


# -- sink ---------------------------------------------------------------

def test_sink_drains_in_order_and_empties():
    s = ObservationSink(10)
    a = Observation(1.0, "m", "ok", 5.0, False, None)
    b = Observation(2.0, "m", "error", None, False, None)
    s.put(a); s.put(b)
    assert s.drain() == [a, b]
    assert s.drain() == []

def test_sink_overflow_drops_oldest_and_counts():
    s = ObservationSink(2)
    obs = [Observation(float(i), "m", "ok", 1.0, False, None) for i in range(5)]
    for o in obs:
        s.put(o)
    assert s.drain() == obs[-2:]
    assert s.take_dropped() == 3
    assert s.take_dropped() == 0


# -- log equals live ----------------------------------------------------

def test_each_applied_observation_is_logged_once_with_wall_time():
    sink = ObservationSink(100)
    s = _live(sink, offset=500.0)
    s.observe_success("m", 120.0, now=10.0)
    s.observe_error("m", now=11.0)
    s.observe_error("m", streamed=True, now=12.0)
    s.observe_stream_success("m", 33.0, now=13.0)
    assert sink.drain() == [
        Observation(510.0, "m", "ok", 120.0, False, None),
        Observation(511.0, "m", "error", None, False, None),
        Observation(512.0, "m", "error", None, True, None),
        Observation(513.0, "m", "ok", None, True, 33.0),
    ]

def test_replay_logs_nothing():
    sink = ObservationSink(100)
    s = _live(sink)
    s.replay([Observation(1_000_010.0, "m", "ok", 50.0, False, None)])
    assert sink.drain() == []


def test_replay_skips_malformed_rows():
    """An unknown outcome, or a non-streamed `ok` with no latency, is skipped:
    never read as a success (and never as a 0 ms sample that would bias
    routing toward the model)."""
    s = _live()
    s.replay([Observation(1_000_010.0, "m", "timeout", None, False, None),
              Observation(1_000_011.0, "m", "ok", None, False, None)])
    assert s.snapshot() == {}


# -- stream success is availability-only --------------------------------

def test_stream_success_moves_availability_and_leaves_latency_untouched():
    s = _live()
    for t in range(3):
        s.observe_error("m", now=float(t))
    before = s.snapshot()["m"]
    s.observe_stream_success("m", 40.0, now=3.0)
    after = s.snapshot()["m"]
    assert after["latency_ms"] == before["latency_ms"]
    assert after["latency_n"] == before["latency_n"]
    assert after["availability"] > before["availability"]
    assert after["availability_n"] == before["availability_n"] + 1


def test_stream_success_does_not_refresh_latency_freshness():
    """A stream success is availability-only, so it must not make an old
    latency EWMA look fresh again: past staleness_s it is tier_default."""
    s = _live()
    for t in range(CFG.min_samples):
        s.observe_success("m", 90.0, now=float(t))
    s.observe_stream_success("m", 40.0, now=CFG.staleness_s + 10)
    assert s.latency("m", "cheap", now=CFG.staleness_s + 11).source == "tier_default"


# -- replay identity (spec §3.5) ----------------------------------------

_kind = st.sampled_from(["ok", "error", "stream_ok", "stream_error"])
_event = st.tuples(_kind, st.sampled_from(["a", "b"]),
                   st.floats(min_value=0.001, max_value=500.0),   # gap to previous, s
                   st.floats(min_value=1.0, max_value=5000.0))    # latency / ttft ms

def _apply(store, events, t0):
    t = t0
    for kind, model, gap, ms in events:
        t += gap
        if kind == "ok":
            store.observe_success(model, ms, now=t)
        elif kind == "error":
            store.observe_error(model, now=t)
        elif kind == "stream_ok":
            store.observe_stream_success(model, ms, now=t)
        else:
            store.observe_error(model, streamed=True, now=t)
    return t

@settings(max_examples=200, deadline=None)
@given(events=st.lists(_event, min_size=1, max_size=60),
       later=st.lists(st.floats(min_value=0.0, max_value=5000.0), min_size=1, max_size=4))
def test_replay_reproduces_the_live_state_up_to_a_clock_shift(events, later):
    sink = ObservationSink(10_000)
    live = SignalStore(CFG, sink=sink, wall_offset=1_000_000.0)
    t_end = _apply(live, events, t0=50.0)

    # A different process: different monotonic origin, different offset.
    booted = SignalStore(CFG, wall_offset=2_000_000.0)
    booted.replay(sink.drain())
    shift = (1_000_000.0 - 2_000_000.0)      # booted_mono = live_mono + shift

    for dt in later:
        for m in ("a", "b"):
            for tier in ("cheap", "frontier"):
                assert booted.latency(m, tier, now=t_end + dt + shift) == \
                       live.latency(m, tier, now=t_end + dt)
            assert _same_av(booted.availability(m, now=t_end + dt + shift),
                            live.availability(m, now=t_end + dt))

def _same_av(a, b):
    return (a.source, a.samples) == (b.source, b.samples) and abs(a.value - b.value) <= 1e-9


def test_replay_honours_age_so_an_old_latency_is_stale():
    """The stale-as-fresh defect (spec §1): an observation older than
    staleness_s must seed as tier_default, not measured."""
    sink = ObservationSink(100)
    live = SignalStore(CFG, sink=sink, wall_offset=0.0)
    for i in range(5):
        live.observe_success("m", 90.0, now=float(i))
    booted = SignalStore(CFG, wall_offset=0.0)
    booted.replay(sink.drain())
    assert booted.latency("m", "cheap", now=4.0 + CFG.staleness_s + 1).source == "tier_default"
    assert booted.latency("m", "cheap", now=5.0).source == "measured"


# -- cap bound (spec §3.5) ----------------------------------------------

def test_replaying_only_the_last_K_is_within_the_EWMA_bound():
    K = 200
    cfg = SignalsCfg(min_samples=3, half_life_s=1e12, staleness_s=1e12)
    obs = [Observation(float(i), "m", "ok", float(100 + (i * 37) % 900), False, None)
           for i in range(1000)]
    full = SignalStore(cfg, wall_offset=0.0); full.replay(obs)
    tail = SignalStore(cfg, wall_offset=0.0); tail.replay(obs[-K:])
    lo = min(o.latency_ms for o in obs); hi = max(o.latency_ms for o in obs)
    bound = (1 - cfg.alpha_floor) ** (K - 9) * (hi - lo)
    a = full.latency("m", "cheap", now=1000.0).value
    b = tail.latency("m", "cheap", now=1000.0).value
    assert abs(a - b) <= bound


# -- Task 6: Gateway.run_stream observes availability -----------------------
#
# A minimal InferenceBackend stand-in whose generate_stream is fully
# scripted: how many deltas to yield, whether/when to raise, and whether to
# emit the terminal chunk at all. Exercises Gateway.run_stream directly
# (not through the Pipeline), matching the brief's "drive Gateway.run_stream
# directly" instruction.

GS = load_settings()
GREG = make_test_registry(GS)


class _Status(Exception):
    """A status-coded failure, matching the pattern in
    tests/routing/test_signal_wiring.py -- counts_as_endpoint_unhealth reads
    `.status_code`."""

    def __init__(self, code):
        super().__init__(f"http {code}")
        self.status_code = code


class _ScriptedStreamBackend:
    """generate_stream yields `deltas`, optionally raising `exc` at delta
    index `raise_at` (raising before any delta at all when `raise_at == 0`
    and `deltas` is empty), then emits the terminal chunk unless
    `emit_final=False`."""

    name = "scripted"

    def __init__(self, *, deltas: tuple[str, ...] = ("hi",), raise_at=None,
                exc: Exception | None = None, emit_final: bool = True) -> None:
        self._deltas = deltas
        self._raise_at = raise_at
        self._exc = exc if exc is not None else RuntimeError("boom")
        self._emit_final = emit_final

    async def generate(self, *a, **kw):
        raise NotImplementedError

    async def generate_stream(self, model, messages, price, tier, hint,
                              tools=None, tool_choice=None, params=None):
        for i, d in enumerate(self._deltas):
            if self._raise_at is not None and i == self._raise_at:
                raise self._exc
            yield StreamChunk(delta=d)
        if self._raise_at is not None and self._raise_at >= len(self._deltas):
            raise self._exc
        if self._emit_final:
            yield StreamChunk(final=GenerationResult(
                model=model, text="".join(self._deltas), tokens_in=1,
                tokens_out=1, latency_ms=1.0, cost_usd=0.0001,
                confidence=None, tool_calls=[]))

    async def health(self):
        raise NotImplementedError


def _stream_ctx():
    providers = frozenset({"onprem", "azure", "bedrock"})
    return ExecutionPolicyContext(
        request_id="r", tenant_id="t", sensitivity="public", onprem_only=False,
        allowed_providers=providers,
        allowed_models=frozenset(c.name for c in GREG.effective_pool()
                                 if c.provider in providers),
        max_cost_usd=0.05, max_latency_ms=15000, risk_class="low",
        require_verification=False, detected_entities=(), redaction_map_id=None)


def _stream_gateway(backend, sink):
    store_ = SignalStore(CFG, sink=sink)
    gw = Gateway(GS, backend, Router(GS, registry=GREG), GREG, signals=store_)
    return gw, store_


def _run_stream(gw):
    async def go():
        stream, _records = await gw.run_stream(
            _stream_ctx(), [Message(role="user", content="hi")], 20, 0.1, {})
        async for _ in stream:
            pass
    asyncio.run(go())


def test_stream_terminal_chunk_observes_availability_with_ttft_latency_untouched():
    sink = ObservationSink(100)
    gw, store_ = _stream_gateway(
        _ScriptedStreamBackend(deltas=("Hi", " there")), sink)

    _run_stream(gw)

    obs = sink.drain()
    assert len(obs) == 1
    o = obs[0]
    assert o.outcome == "ok"
    assert o.streamed is True
    assert o.ttft_ms is not None and o.ttft_ms >= 0
    assert o.latency_ms is None
    model = next(iter(store_.snapshot()))
    assert store_.snapshot()[model]["latency_n"] == 0  # never non-streamed


def test_stream_error_before_first_chunk_observes_once_and_propagates():
    sink = ObservationSink(100)
    gw, store_ = _stream_gateway(
        _ScriptedStreamBackend(deltas=(), raise_at=0, exc=_Status(503)), sink)

    with pytest.raises(_Status):
        _run_stream(gw)

    obs = sink.drain()
    assert len(obs) == 1
    assert obs[0].outcome == "error"
    assert obs[0].streamed is True


def test_stream_error_after_two_deltas_observes_once():
    sink = ObservationSink(100)
    gw, store_ = _stream_gateway(
        _ScriptedStreamBackend(deltas=("a", "b", "c"), raise_at=2,
                               exc=_Status(503)), sink)

    with pytest.raises(_Status):
        _run_stream(gw)

    obs = sink.drain()
    assert len(obs) == 1
    assert obs[0].outcome == "error"
    assert obs[0].streamed is True


def test_stream_error_excluded_by_counts_as_endpoint_unhealth_is_not_observed():
    sink = ObservationSink(100)
    gw, store_ = _stream_gateway(
        _ScriptedStreamBackend(deltas=(), raise_at=0, exc=_Status(400)), sink)

    with pytest.raises(_Status):
        _run_stream(gw)

    assert sink.drain() == []


def test_stream_consumer_disconnect_is_not_observed():
    sink = ObservationSink(100)
    gw, store_ = _stream_gateway(
        _ScriptedStreamBackend(deltas=("a", "b", "c")), sink)

    async def go():
        stream, _records = await gw.run_stream(
            _stream_ctx(), [Message(role="user", content="hi")], 20, 0.1, {})
        first = await stream.__anext__()
        assert first.delta == "a"
        await stream.aclose()

    asyncio.run(go())
    assert sink.drain() == []


def test_stream_without_terminal_chunk_is_not_observed():
    sink = ObservationSink(100)
    gw, store_ = _stream_gateway(
        _ScriptedStreamBackend(deltas=("a", "b"), emit_final=False), sink)

    _run_stream(gw)

    assert sink.drain() == []
