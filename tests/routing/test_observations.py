"""Signal hardening: the observation log (spec §3). The load-bearing test is
replay identity (§3.5): replaying the log reproduces the live state up to a
constant clock shift."""
from __future__ import annotations

import pytest
from hypothesis import given, settings, strategies as st

from app.config.schema import SignalsCfg
from app.routing.observations import Observation, ObservationSink
from app.routing.signals import SignalStore

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
