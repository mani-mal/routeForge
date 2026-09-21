# Signal Hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the routing signal's boot seed a faithful reconstruction of what the live signal observed (an observation log + replay), meter failed requests as `error` rows, let plain-chat streams feed availability, and add a clock seam to `rank`.

**Architecture:** `SignalStore` gets an optional `ObservationSink`; every applied observation is also enqueued and flushed in batches to a new `endpoint_observations` table by a lifespan background task. At boot the store replays each model's most recent observations at their true age through its own `observe_*` methods. The chat route writes one `usage_records` row with `status="error"` for a request that fails on the backend. `Gateway.run_stream` observes stream outcomes (availability only) and logs time-to-first-token.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy 2 async (SQLite in tests, Postgres in prod), Pydantic v2, pytest + Hypothesis, React/TS (one small UI change).

**Spec:** `docs/superpowers/specs/2026-09-21-signal-hardening-design.md` — the binding authority. Read it before starting any task.

## Global Constraints

- **No PII, tenant, request id or content in `endpoint_observations`.** Columns are exactly: `id, ts, model, outcome, latency_ms, streamed, ttft_ms`.
- **Nothing on the serving path may raise or block because of the log.** `ObservationSink.put` does no I/O and never raises; flush/prune/seed failures are logged and swallowed.
- **A signal can only reorder; it never changes eligibility.** No task touches `app/policy/feasible.py`.
- **Replay never re-logs.** Replay runs with the sink detached.
- **Stream timing never enters the latency EWMA.** Streams update availability only.
- **Every spend, token, latency-percentile and budget figure stays computed over `status == "ok"` rows.** `error` rows must not change any of them.
- **No `error` row** for `ContextWindowExceeded`, `NoEligibleCandidate`, `PipelineRejected`, policy rejections, or client disconnects.
- **Protocol changes land with every implementation and every test fake in the same commit** (a mismatch has previously been swallowed as `NoEligibleCandidate`).
- Tests: run with `LITELLM_LOCAL_MODEL_COST_MAP=True uv run pytest ...` (without the env var pytest hangs on a network fetch). No pytest-asyncio: async tests use `asyncio.run(...)`, matching `tests/routing/test_signal_wiring.py`.
- Shell quirk in this environment: use `/usr/bin/grep`, and `rtk proxy git ...` for git commands whose output you need verbatim.
- Commit messages end with `Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>`.

---

## File Structure

| File | Change | Responsibility |
|---|---|---|
| `app/config/schema.py` | modify | three new `SignalsCfg` fields |
| `app/config/lint.py` | modify | three positivity rules |
| `app/routing/observations.py` | **create** | `Observation` dataclass, `ObservationSink` (bounded buffer) |
| `app/routing/signals.py` | modify | sink + `wall_offset`, `observe_stream_success`, `observe_error(streamed=)`, `replay()`; remove `seed`/`SeedRow` |
| `app/routing/strategy.py` | modify | `now` param on `rank` (Protocol + default) |
| `app/apps/models.py` | modify | `EndpointObservation` table |
| `app/apps/store.py` | modify | `write_observations`, `load_seed_observations`, `prune_observations`, `error_count`; remove `aggregate_model_signals` |
| `app/main.py` | modify | sink wiring, replay seed, flusher task, shutdown flush |
| `app/errors.py` | modify | `BackendUnavailable.attempted_models` |
| `app/gateway/gateway.py` | modify | `_exhausted` passes attempted models; `run_stream` observations |
| `app/pipeline.py` | modify | expose `get_meta.chosen_model` eagerly |
| `app/api/routes.py` | modify | `_record_failed`; error rows on 502 and streamed backend failure |
| `ui/src/api.ts`, `ui/src/admin/CrossAppReport.tsx` | modify | show `error_count` |
| `tests/routing/test_observations.py` | **create** | sink, log-equals-live, replay identity, cap bound, stream observations |
| `tests/routing/test_signals.py` | modify | drop seed tests, keep the rest |
| `tests/routing/test_signal_wiring.py` | modify | drop aggregate tests; add DB log, lifespan, flusher tests |
| `tests/test_routing_strategy.py` | modify | clock-seam tests |
| `tests/test_error_metering.py` | **create** | error rows + `error_count` + totals unchanged |
| docs (Task 8) | modify | research notes, backlog, reference, CLAUDE.md |

---

### Task 1: Config fields and lint rules

**Files:**
- Modify: `app/config/schema.py` (class `SignalsCfg`, ~line 53)
- Modify: `app/config/lint.py` (beside `LATENCY_SCALE_NOT_POSITIVE`, ~line 88)
- Test: `tests/test_config_lint.py` (add; if the repo's lint tests live elsewhere, find them with `/usr/bin/grep -rln LATENCY_SCALE_NOT_POSITIVE tests` and add there)

**Interfaces:**
- Produces: `SignalsCfg.flush_interval_s: float = 5.0`, `SignalsCfg.max_buffer: int = 10000`, `SignalsCfg.max_seed_obs_per_model: int = 200`; lint codes `SIGNALS_FLUSH_INTERVAL_NOT_POSITIVE`, `SIGNALS_MAX_BUFFER_NOT_POSITIVE`, `SIGNALS_MAX_SEED_OBS_NOT_POSITIVE`.

- [ ] **Step 1: Write the failing tests.** Find how the existing `LATENCY_SCALE_NOT_POSITIVE` test builds a settings object with an overridden signals field and returns lint errors, and copy that shape. Tests:

```python
@pytest.mark.parametrize("field,value,code", [
    ("flush_interval_s", 0.0, "SIGNALS_FLUSH_INTERVAL_NOT_POSITIVE"),
    ("flush_interval_s", -1.0, "SIGNALS_FLUSH_INTERVAL_NOT_POSITIVE"),
    ("max_buffer", 0, "SIGNALS_MAX_BUFFER_NOT_POSITIVE"),
    ("max_seed_obs_per_model", 0, "SIGNALS_MAX_SEED_OBS_NOT_POSITIVE"),
])
def test_signals_writer_knobs_must_be_positive(field, value, code):
    errs = _lint_with_signals(**{field: value})      # helper: mirror the existing test's construction
    assert any(e.startswith(code) for e in errs)

def test_signals_writer_knob_defaults_pass_lint():
    errs = _lint_with_signals()
    assert not [e for e in errs if e.startswith("SIGNALS_")]

def test_signals_writer_knob_defaults():
    c = SignalsCfg()
    assert (c.flush_interval_s, c.max_buffer, c.max_seed_obs_per_model) == (5.0, 10000, 200)
```

- [ ] **Step 2: Run, expect FAIL** — `LITELLM_LOCAL_MODEL_COST_MAP=True uv run pytest tests/test_config_lint.py -q` → attribute/assert failures.

- [ ] **Step 3: Implement.** In `SignalsCfg` add, with a comment each:

```python
    # Observation-log writer (signal hardening, spec §3.3/§7). The sink buffers
    # observations in memory; a lifespan task flushes them every
    # `flush_interval_s`. `max_buffer` bounds memory if the DB is unreachable
    # (oldest dropped). `max_seed_obs_per_model` caps boot replay: with
    # alpha_floor 0.1 the 200th-oldest observation weighs ~0.9**191, i.e.
    # nothing (spec §3.5).
    flush_interval_s: float = 5.0
    max_buffer: int = 10000
    max_seed_obs_per_model: int = 200
```

In `lint.py`, after the `LATENCY_SCALE_NOT_POSITIVE` block:

```python
    sig = s.routing.signals
    if sig.flush_interval_s <= 0:
        errs.append(f"SIGNALS_FLUSH_INTERVAL_NOT_POSITIVE {sig.flush_interval_s}")
    if sig.max_buffer < 1:
        errs.append(f"SIGNALS_MAX_BUFFER_NOT_POSITIVE {sig.max_buffer}")
    if sig.max_seed_obs_per_model < 1:
        errs.append(f"SIGNALS_MAX_SEED_OBS_NOT_POSITIVE {sig.max_seed_obs_per_model}")
```

- [ ] **Step 4: Run, expect PASS.** Also run `LITELLM_LOCAL_MODEL_COST_MAP=True uv run pytest tests -q -k "lint or config"`.
- [ ] **Step 5: Commit** — `feat(signals): observation-writer config knobs + positivity lint`.

---

### Task 2: `ObservationSink`, store sink/wall clock, stream-success, replay

**Files:**
- Create: `app/routing/observations.py`
- Modify: `app/routing/signals.py`
- Create: `tests/routing/test_observations.py`
- Modify: `tests/routing/test_signals.py` (delete `test_seeding_makes_a_model_measured_without_live_traffic` and `test_seeded_last_ts_is_actually_threaded_not_defaulted`, and the `SeedRow` import)

**Interfaces:**
- Consumes: `SignalsCfg` (Task 1).
- Produces:
  - `app/routing/observations.py`: `@dataclass(frozen=True) class Observation: ts: float; model: str; outcome: str; latency_ms: float | None; streamed: bool; ttft_ms: float | None` (`ts` = wall-clock epoch seconds, `outcome` ∈ `"ok"|"error"`); `class ObservationSink: __init__(max_buffer: int); put(obs) -> None; drain() -> list[Observation]; dropped: int` (read-and-reset via `take_dropped() -> int`).
  - `SignalStore.__init__(cfg, *, sink: ObservationSink | None = None, wall_offset: float | None = None)`; `SignalStore.wall_offset: float` (property); `observe_success(model, latency_ms, *, now=None)`; `observe_stream_success(model, ttft_ms: float | None, *, now=None)`; `observe_error(model, *, streamed: bool = False, now=None)`; `replay(observations: Iterable[Observation]) -> None`.
  - Removed: `SignalStore.seed`, `SeedRow`.

- [ ] **Step 1: Write `app/routing/observations.py`.**

```python
"""The observation log's in-process half (signal hardening, spec §3.2).

`SignalStore` hands every observation it APPLIES to an `ObservationSink`, so
the persisted log can never disagree with the live signal. The sink is on the
serving path: `put` does no I/O, never blocks and never raises. A lifespan
task (`app/main.py`) drains it into `endpoint_observations`.

An observation carries no tenant, request id or content -- endpoint health
only, the same bounded-label stance as GET /metrics.
"""
from __future__ import annotations

import threading
from collections import deque
from dataclasses import dataclass


@dataclass(frozen=True)
class Observation:
    ts: float                  # wall-clock epoch seconds
    model: str
    outcome: str               # "ok" | "error"
    latency_ms: float | None   # non-streamed ok only
    streamed: bool
    ttft_ms: float | None      # streamed ok only; logged, never scored


class ObservationSink:
    def __init__(self, max_buffer: int) -> None:
        self._buf: deque[Observation] = deque(maxlen=max_buffer)
        self._lock = threading.Lock()
        self._dropped = 0

    def put(self, obs: Observation) -> None:
        with self._lock:
            if len(self._buf) == self._buf.maxlen:
                self._dropped += 1          # deque(maxlen) evicts the oldest
            self._buf.append(obs)

    def drain(self) -> list[Observation]:
        with self._lock:
            out = list(self._buf)
            self._buf.clear()
            return out

    def take_dropped(self) -> int:
        with self._lock:
            n, self._dropped = self._dropped, 0
            return n
```

- [ ] **Step 2: Write the failing tests in `tests/routing/test_observations.py`.**

```python
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
```

Latency is compared with `==`. Availability is compared within `1e-9`: the decay evaluates `0.5 ** (dt/half_life)` on a `dt` that is mathematically equal in both stores but computed from shifted floats, so it can differ in the last ulp. **If the latency `==` also proves ulp-fragile** (the staleness comparison `now - ts > staleness_s` at a shifted origin), keep `source`/`samples` exact and compare `value` within `1e-9` — say so in your report; do not loosen `source` or `samples`.

```python
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
```

Add the mutation checks to your report (Step 5), not to the test file.

- [ ] **Step 3: Run, expect FAIL** — `LITELLM_LOCAL_MODEL_COST_MAP=True uv run pytest tests/routing/test_observations.py -q`.

- [ ] **Step 4: Implement in `app/routing/signals.py`.**
  - Update the module docstring: seeding now **replays the observation log** (spec §3.4), not `usage_records`.
  - Delete `SeedRow` and `seed()`.
  - Constructor:

```python
    def __init__(self, cfg: SignalsCfg, *, sink: "ObservationSink | None" = None,
                 wall_offset: float | None = None) -> None:
        self._cfg = cfg
        self._state: dict[str, _ModelState] = {}
        self._sink = sink
        # Maps this process's monotonic clock to wall time (wall = mono +
        # offset). The log stores wall time; replay maps it back with the
        # BOOTING process's offset, which preserves every age exactly.
        self._wall_offset = (time.time() - time.monotonic()
                             if wall_offset is None else wall_offset)

    @property
    def wall_offset(self) -> float:
        return self._wall_offset

    def _log(self, t: float, model: str, outcome: str, *, latency_ms=None,
             streamed=False, ttft_ms=None) -> None:
        if self._sink is not None:
            self._sink.put(Observation(t + self._wall_offset, model, outcome,
                                       latency_ms, streamed, ttft_ms))
```

  - Refactor the availability-success update into a private `_apply_available(st, t) -> _ModelState` used by both `observe_success` and `observe_stream_success`, so the rule exists once.
  - `observe_success`: unchanged math, then `self._log(t, model, "ok", latency_ms=latency_ms)`.
  - New `observe_stream_success(model, ttft_ms, *, now=None)`: availability-only (`self._state[model] = self._apply_available(st, t)`; latency fields untouched), then `self._log(t, model, "ok", streamed=True, ttft_ms=ttft_ms)`. Docstring: why stream latency never enters the EWMA (spec §5).
  - `observe_error(model, *, streamed=False, now=None)`: unchanged math, then `self._log(t, model, "error", streamed=streamed)`.
  - New `replay(observations)`:

```python
    def replay(self, observations) -> None:
        """Rebuild state from the observation log (spec §3.4). Each row is
        applied through the SAME observe_* method that produced it, at its
        TRUE age (mono = wall - this process's offset), with the sink
        detached so replay never re-logs what it reads. Rows must be in
        ascending `ts` order."""
        sink, self._sink = self._sink, None
        try:
            for o in observations:
                t = o.ts - self._wall_offset
                if o.outcome == "error":
                    self.observe_error(o.model, streamed=o.streamed, now=t)
                elif o.streamed:
                    self.observe_stream_success(o.model, o.ttft_ms, now=t)
                else:
                    self.observe_success(o.model, float(o.latency_ms or 0.0), now=t)
        finally:
            self._sink = sink
```

  (An `ok` non-streamed row always has `latency_ms`; the `or 0.0` only guards a hand-edited row.)
  - Import `Observation` from `app.routing.observations` (the sink type under `TYPE_CHECKING` is fine).

- [ ] **Step 5: Run, expect PASS**, then run `tests/routing -q`. **Falsify and report** (revert each after): (a) in `replay`, use `t = time.monotonic()` instead of the offset mapping → the identity and staleness tests must fail; (b) leave the sink attached during replay → `test_replay_logs_nothing` must fail; (c) make `observe_stream_success` also update latency → the stream test must fail. Record each result in your report.
- [ ] **Step 6: Commit** — `feat(signals): observation sink, wall-clock mapping, stream success, replay`.

---

### Task 3: Clock seam on `rank` (B40)

**Files:**
- Modify: `app/routing/strategy.py` (Protocol ~line 100, `DefaultRoutingStrategy.rank` ~line 140)
- Modify: every other implementation or fake of `rank` — find them with `/usr/bin/grep -rn "def rank" app tests`
- Test: `tests/test_routing_strategy.py`

**Interfaces:**
- Produces: `rank(..., cached_pricing=None, now: float | None = None)`; forwards `now=now` to `signals.latency(...)` and `signals.availability(...)`.

- [ ] **Step 1: Failing tests** in `tests/test_routing_strategy.py`. Reuse that file's existing helpers for building `eligible`, `ctx`, `weights`, and quality resolver (read the file first). Two tests:
  1. `test_staleness_cliff_is_reachable_through_rank`: a store with `min_samples` observations at `now=0..4` for one candidate; `rank(..., now=5.0)` reports that candidate's latency term `src == "measured"`; `rank(..., now=4.0 + staleness_s + 1)` reports `"tier_default"`.
  2. `test_availability_decay_is_reachable_through_rank`: errors at `now=0..4`; availability term at `now=5` is lower than at `now=5 + 10*half_life_s`, and the latter is within `1e-3` of `1.0`.
  Read the term values off the returned `RankResult`'s scored candidates (use whatever accessor the existing tests use for `latency` / `availability` terms).
- [ ] **Step 2: Run, expect FAIL** (`TypeError: unexpected keyword argument 'now'`).
- [ ] **Step 3: Implement.** Add `now: float | None = None` to the Protocol and to every implementation/fake found in Step 0's grep, **in this one commit**. In `DefaultRoutingStrategy.rank`, pass `now=now` to both store calls. `Router.select` passes nothing.
- [ ] **Step 4: Run** `tests/test_routing_strategy.py tests/routing -q` and the full routing/gateway set: `LITELLM_LOCAL_MODEL_COST_MAP=True uv run pytest tests -q -k "rout or gateway or strategy or signal"`. Expect PASS.
- [ ] **Step 5: Commit** — `feat(routing): clock seam on RoutingStrategy.rank (B40)`.

---

### Task 4: `endpoint_observations` table and store functions

**Files:**
- Modify: `app/apps/models.py` (add `EndpointObservation` after `UsageRecord`)
- Modify: `app/apps/store.py` (add three functions; delete `aggregate_model_signals` and its `SeedRow` import)
- Modify: `tests/routing/test_signal_wiring.py` (delete the five `test_seed_aggregate_*` tests and the `_row` helper if unused after; add the tests below)

**Interfaces:**
- Consumes: `Observation` (Task 2).
- Produces:
  - `async def write_observations(session, observations: list[Observation]) -> None` (caller commits)
  - `async def load_seed_observations(session, *, since_hours: float, per_model_cap: int) -> list[Observation]` — ascending `ts`
  - `async def prune_observations(session, *, older_than_hours: float) -> int` (caller commits; returns rows deleted)

- [ ] **Step 1: Failing tests** (append to `tests/routing/test_signal_wiring.py`; `_fresh_db()` already exists there):

```python
from app.routing.observations import Observation

def _obs(ts, model="m", outcome="ok", latency=100.0, streamed=False, ttft=None):
    return Observation(ts, model, outcome, latency if outcome == "ok" and not streamed else None,
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
            await store.write_observations(s, rows); await s.commit()
            return await store.load_seed_observations(s, since_hours=1, per_model_cap=4)
    got = asyncio.run(go())
    assert [o.model for o in got].count("a") == 4
    assert [o.model for o in got].count("b") == 3
    a_ts = [o.ts for o in got if o.model == "a"]
    assert min(a_ts) == pytest.approx(now - 100 + 6, abs=1e-3)   # the 4 newest
    assert [o.ts for o in got] == sorted(o.ts for o in got)

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
```

- [ ] **Step 2: Run, expect FAIL.**
- [ ] **Step 3: Implement.** In `models.py`:

```python
class EndpointObservation(Base):
    """One backend attempt's outcome (signal hardening, spec §3.1). The
    persisted half of the live SignalStore's input; boot seeding replays it.
    Endpoint health only -- no tenant, request id or content, by design."""

    __tablename__ = "endpoint_observations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), index=True)
    model: Mapped[str] = mapped_column(String(100), index=True)
    outcome: Mapped[str] = mapped_column(String(8))          # ok | error
    latency_ms: Mapped[float | None] = mapped_column(Float, nullable=True)
    streamed: Mapped[bool] = mapped_column(Boolean, default=False)
    ttft_ms: Mapped[float | None] = mapped_column(Float, nullable=True)
```

In `store.py`: convert `Observation.ts` (epoch seconds) ↔ timezone-aware UTC `datetime` (`dt.datetime.fromtimestamp(ts, dt.timezone.utc)` / `.timestamp()`; SQLite may return a naive datetime — treat naive as UTC with `.replace(tzinfo=dt.timezone.utc)`). `load_seed_observations`: select distinct models with `ts >= cutoff`; for each model select its newest `per_model_cap` rows (`order_by(ts.desc()).limit(cap)`); concatenate and sort ascending by `ts` (tie-break by `id`). Portable across SQLite/Postgres — no window functions. `prune_observations`: `delete(EndpointObservation).where(ts < cutoff)`, return `result.rowcount`. Remove `aggregate_model_signals` and any now-unused imports (`case`, `SeedRow`).

- [ ] **Step 4: Run, expect PASS** — `tests/routing -q`. `app/main.py` still calls `aggregate_model_signals` and `signals.seed` until Task 5: **make Task 4's commit keep the app importable** by leaving `app/main.py` untouched and letting its startup seed fail soft. Check: `test_create_app_wires_one_shared_signal_store_into_router_and_gateway` must still pass (the startup seed catches the `AttributeError` and logs a warning). If it does not, fold the minimal `main.py` seed replacement from Task 5 Step 3 (the startup seed only) into this commit and say so in your report.
- [ ] **Step 5: Commit** — `feat(signals): endpoint_observations table; load/prune/write; drop usage_records seeding`.

---

### Task 5: Lifespan wiring — sink, replay seed, flusher, shutdown flush

**Files:**
- Modify: `app/main.py` (signal construction ~line 116; startup seed ~line 176; shutdown ~line 172)
- Test: `tests/routing/test_signal_wiring.py`

**Interfaces:**
- Consumes: Tasks 1, 2, 4.
- Produces: `app.state.observation_sink: ObservationSink`; module-level helpers in `app/main.py` (or a new small `app/routing/observation_writer.py` if `main.py` would otherwise grow a class — implementer's call, record it): `async def flush_observations(db, sink) -> int` (writes one batch; returns count; swallows+logs DB errors), and the background loop.

- [ ] **Step 1: Failing tests.**

```python
def test_boot_replays_the_log_so_a_model_is_measured_without_live_traffic(tmp_path, monkeypatch):
    """End to end through the real lifespan: rows in endpoint_observations
    before boot -> the store reports `measured` after startup."""
```
  Build a file-backed SQLite URL in `tmp_path`, pre-write 6 recent `ok` observations for a model in the test registry via `store.write_observations`, point the app at that DB (find how `create_app`/`Database` pick their URL — env var or settings — and set it with `monkeypatch`), start `TestClient(create_app())`, and assert `c.app.state.signals.latency(model, tier).source == "measured"` with `samples == 6`.

```python
def test_a_served_request_reaches_the_table_after_a_flush(): ...
```
  Start the app, serve one request (as the existing wiring test does), call `await flush_observations(app.state.db, app.state.observation_sink)` via the client's portal or `asyncio.run` on the app's loop (follow whatever the repo already does for calling async code against a live `TestClient` app; if none, exit the `with` block — shutdown flush — and read the table afterwards), and assert one `ok` row for the served model.

```python
def test_flush_swallows_a_db_failure():
    sink = ObservationSink(10); sink.put(Observation(time.time(), "m", "ok", 1.0, False, None))
    class _BrokenDB:
        def session(self): raise RuntimeError("db down")
    assert asyncio.run(flush_observations(_BrokenDB(), sink)) == 0     # no raise

def test_seed_failure_leaves_an_empty_store_and_the_app_serving():
    """Monkeypatch store.load_seed_observations to raise; the app starts,
    the store is empty, and a request is served 200."""
```

- [ ] **Step 2: Run, expect FAIL.**
- [ ] **Step 3: Implement.**
  - Construction: `sink = ObservationSink(settings.routing.signals.max_buffer)`; `signals = SignalStore(settings.routing.signals, sink=sink)`; `app.state.observation_sink = sink`. Update the comment above it (seeded from the observation log, not `usage_records`).
  - Startup seed (`_seed_signals`): replace the body's query with `obs = await apps_store.load_seed_observations(session, since_hours=cfg.seed_window_hours, per_model_cap=cfg.max_seed_obs_per_model)` then `app.state.signals.replay(obs)`; log `observations=%d models=%d`. Keep the fail-soft `except` and its docstring rationale; update the docstring to describe replay.
  - Flusher: in the same startup event (after seeding), `app.state.observation_task = asyncio.create_task(_observation_loop(app))`. The loop: `while True: await asyncio.sleep(cfg.flush_interval_s); await flush_observations(app.state.db, sink)`, and once every 3600 s of loop time run `prune_observations(older_than_hours=cfg.seed_window_hours)` in its own session, swallowing errors. `flush_observations` also logs `take_dropped()` when non-zero.
  - Shutdown: a new `@app.on_event("shutdown")` handler cancels `observation_task` (await it, suppress `CancelledError`) and then performs one final `flush_observations`. It must not raise.
  - Everything here runs on the real serving loop (lifespan), never in `create_app`'s throwaway loop — keep the existing comment's reasoning intact.
- [ ] **Step 4: Run** `tests/routing -q` and `LITELLM_LOCAL_MODEL_COST_MAP=True uv run pytest tests -q -k "main or wiring or health or chat"`. Expect PASS.
- [ ] **Step 5: Commit** — `feat(signals): replay-seed at boot, background flusher, shutdown flush`.

---

### Task 6: Gateway — stream observations, attempted models

**Files:**
- Modify: `app/errors.py` (`BackendUnavailable`)
- Modify: `app/gateway/gateway.py` (`_exhausted` ~line 72, the `run()` loop's failure branch, `run_stream` ~line 228)
- Modify: `app/pipeline.py` (`run_stream`, where `get_meta.detector` / `get_meta.ctx` are attached, ~line 1247)
- Test: `tests/routing/test_observations.py` (gateway section) and `tests/routing/test_signal_wiring.py`

**Interfaces:**
- Produces: `BackendUnavailable(msg, *, attempted_models: tuple[str, ...] = ())` with attribute `.attempted_models`; `get_meta.chosen_model: str` (set eagerly, before any delta).

- [ ] **Step 1: Failing tests.** Use the fake streaming backend in `tests/_litellm_fake.py` / the patterns in `tests/test_run_stream_pipeline.py` (read both first) to drive `Gateway.run_stream` directly with a `SignalStore(cfg, sink=ObservationSink(100))`:
  1. A stream that yields deltas then a terminal `final` → exactly one `ok` observation, `streamed=True`, `ttft_ms` not `None` and `>= 0`, `latency_ms is None`; the store's latency state for that model unchanged (`snapshot()["m"]["latency_n"] == 0` if never observed non-streamed).
  2. A backend that raises a 503-status exception **before** the first chunk → one `error` observation, `streamed=True`; the exception still propagates.
  3. Same but raises **after** two deltas → one `error` observation.
  4. A backend raising a 400-status exception (excluded by `counts_as_endpoint_unhealth`) → no observation.
  5. The consumer stops iterating and closes the generator (`await agen.aclose()`) mid-stream → no observation.
  6. A stream that ends with no terminal chunk → no observation.
  7. `Gateway.run` where every candidate fails → the raised `BackendUnavailable` has `attempted_models` equal to the tried models, in order.
  8. Pipeline: after `Pipeline.run_stream(...)` returns (before consuming deltas), `get_meta.chosen_model` equals the router's chosen model.
- [ ] **Step 2: Run, expect FAIL.**
- [ ] **Step 3: Implement.**
  - `app/errors.py`:

```python
class BackendUnavailable(RuntimeError):
    """...existing docstring... `attempted_models` lists the candidates the
    gateway tried, in order (signal hardening, spec §4) -- the route meters
    the last one on its `error` usage row."""

    def __init__(self, *args, attempted_models: tuple[str, ...] = ()) -> None:
        super().__init__(*args)
        self.attempted_models = tuple(attempted_models)
```

  - `gateway.py`: keep an ordered `attempted: list[str]` in `run()` (append `cand.name` before each `generate`); `_exhausted(..., attempted)` passes `attempted_models=tuple(attempted)` to `BackendUnavailable`. `ContextWindowExceeded` is unchanged.
  - `run_stream`'s `_agen`:

```python
        async def _agen() -> AsyncIterator[StreamChunk]:
            t0 = time.perf_counter()
            ttft_ms: float | None = None
            try:
                async for chunk in self._backend.generate_stream(...):   # args unchanged
                    if ttft_ms is None and chunk.delta is not None:
                        ttft_ms = (time.perf_counter() - t0) * 1000.0
                    if chunk.final is not None:
                        record.actual_cost_usd = chunk.final.cost_usd
                        record.latency_ms = chunk.final.latency_ms
                        if self._signals is not None:
                            # Availability only -- a stream's timing is not the
                            # quantity the latency EWMA holds (spec §5).
                            self._signals.observe_stream_success(cand.name, ttft_ms)
                    yield chunk
            except Exception as exc:
                if self._signals is not None and counts_as_endpoint_unhealth(exc):
                    self._signals.observe_error(cand.name, streamed=True)
                error = _format_upstream_error(cand.name, exc)
                log.warning(...)   # unchanged
                raise
```

   `GeneratorExit` / `CancelledError` are `BaseException`, so a disconnect is correctly not observed. Update the `run_stream` docstring: streams now observe availability (spec §5), replacing any sentence claiming they observe nothing. Import `time` if absent.
  - `pipeline.py`: next to `get_meta.ctx = ctx`, add `get_meta.chosen_model = records[0].chosen  # type: ignore[attr-defined]` with a one-line comment (the route meters it on a streamed failure, spec §4).
- [ ] **Step 4: Run** `tests/routing tests/test_run_stream_pipeline.py tests/test_gateway.py -q` (and anything matching `-k stream`). Expect PASS.
- [ ] **Step 5: Commit** — `feat(signals): streams observe availability + TTFT; BackendUnavailable carries attempted models (B41)`.

---

### Task 7: Error metering in the route, `error_count`, console

**Files:**
- Modify: `app/api/routes.py`
- Modify: `app/apps/store.py` (`usage_summary`, `usage_by_application`)
- Modify: `ui/src/api.ts`, `ui/src/admin/CrossAppReport.tsx`
- Create: `tests/test_error_metering.py`
- Modify: `tests/test_admin_usage_by_app.py` (extend, don't weaken)

**Interfaces:**
- Consumes: `BackendUnavailable.attempted_models`, `get_meta.chosen_model`, `get_meta.ctx` (Task 6).
- Produces: `async def _record_failed(request, *, tenant, request_id, started, model, ctx=None) -> None` in `routes.py`; `error_count: int` in both usage aggregates and in `UsageSummary` / `UsageByApp` TS types.

- [ ] **Step 1: Failing tests** in `tests/test_error_metering.py`. Drive the real app with `TestClient(create_app())` and the demo app token (`"Bearer demo-key"`, as `test_signal_wiring.py` does). Make the backend fail by the mechanism the existing gateway tests use (the `mock_fail` hint / `_DeterministicLiteLLM` in `tests/_litellm_fake.py` — read how `test_signal_wiring.py::test_a_failed_request_observes_availability_not_latency` forces a failure and reuse it). Then read `usage_records` via `store.list_usage`.
  1. Non-streamed request, every candidate fails → HTTP 502 and exactly one row with `status == "error"`, `model` == the last attempted model, `prompt_tokens == completion_tokens == total_tokens == 0`, `cost_usd == baseline_usd == 0.0`, `latency_ms > 0`.
  2. The same through `stream: true` on an app with `stream_mode: verified_then_stream` (the keepalive/`_keepalive_stream` path) → error frame streamed, one `error` row.
  3. `stream_mode: stream_unverified` plain-chat request whose backend raises mid-stream → one `error` row whose `model` is `get_meta.chosen_model`.
  4. Context-window overflow (400) → **no** `error` row. `NoEligibleCandidate` / a policy rejection → **no** `error` row. Client disconnect → a `cancelled` row, **no** `error` row.
  5. `usage_summary` and `usage_by_application` over a mix of `ok`, `rejected`, `cancelled`, `error` rows: `error_count` counts only `error`; `count`, `total_usd`, `baseline_usd`, `total_tokens`, `prompt_tokens`, `completion_tokens`, `latency_p50_ms`, `latency_p95_ms` and `store`/`policy.day_spend_usd` are **identical** to the same fixture without the `error` rows.

  If setting an app's `stream_mode` in a test needs the admin API, follow the existing streaming route tests (`/usr/bin/grep -rln "stream_unverified" tests`).
- [ ] **Step 2: Run, expect FAIL.**
- [ ] **Step 3: Implement.**
  - `_record_failed` beside `_record_cancelled`, same shape: `metrics` untouched (the existing `observe_request` calls stay where they are); resolve `tier`/`provider` with `next((c for c in request.app.state.registry.effective_pool() if c.name == model), None)` (fall back to `"-"`); `sensitivity`/`onprem_only` from `ctx` when given (`ctx.sensitivity`, `ctx.onprem_only`), else `"-"`/`False`; zero tokens and cost; `latency_ms=(time.monotonic() - started) * 1000`; `status="error"`; wrapped in `try/except Exception` that logs `"error-usage record failed (swallowed)"`. Docstring: why exactly these cases get a row (spec §4).
  - Main handler's `except Exception as e:` (~line 453) and `_keepalive_stream`'s `except Exception as e:` (~line 896): when `isinstance(e, BackendUnavailable)`, `await _record_failed(..., model=(e.attempted_models[-1] if e.attempted_models else "-"))` before returning/yielding the error. In `_keepalive_stream` the write happens in a generator; shield it the same way `_finalize` is shielded there (`anyio.CancelScope(shield=True)`). `_keepalive_stream` needs `started`, which it already receives.
  - Real-token streams (`_stream_unverified_response._gen`, `_incremental_verify_response`'s generator): wrap the delta loop so an `Exception` that is **not** `ContextWindowExceeded` or `PipelineRejected` sets `stream_failed = True` and re-raises. In the `finally`, when `stream_failed`, call `_record_failed(..., model=getattr(get_meta, "chosen_model", "-"), ctx=getattr(get_meta, "ctx", None))` **instead of** `_finalize_stream` (a failed stream has no final metadata to finalize — today that path only logs "drain-on-disconnect failed"). Shield the write. Both generators need `started`: pass `started=started` from the route (it is in scope at ~line 298) and add the parameter.
  - `store.py`: in `usage_summary` add `"error_count": sum(1 for r in rows if r.status == "error")`; in `usage_by_application` add `"error_count": 0` to the per-app default dict and increment it when `r.status == "error"` (before the `!= "ok"` `continue`). Update both docstrings.
  - UI: add `error_count: number;` to `UsageSummary` and `UsageByApp` in `ui/src/api.ts`. In `CrossAppReport.tsx`, carry `error_count` through `joinRows` (`u?.error_count ?? 0`) and render it inside the existing requests cell, after the count, only when non-zero: `<span className="ml-1 text-xs text-red-400" title="requests that failed on the backend (not counted in spend)">· {n} failed</span>`. No new grid column.
- [ ] **Step 4: Run** `LITELLM_LOCAL_MODEL_COST_MAP=True uv run pytest tests/test_error_metering.py tests/test_admin_usage_by_app.py -q`, then the full suite `LITELLM_LOCAL_MODEL_COST_MAP=True uv run pytest tests -q -p no:cacheprovider -W ignore` (known WSL teardown flakes: `test_langfuse_provision`, `test_traffic_*` — rerun those in isolation before calling anything a failure). UI: `cd ui && ./node_modules/.bin/tsc -b --noEmit`.
- [ ] **Step 5: Commit** — `feat(metering): error usage rows for backend failures + error_count (B38)`.

---

### Task 8: Documentation

**Files:**
- Modify: `docs/research/routing-signal-provenance.md`
- Modify: `docs/backlog/bugs.md`
- Modify: `docs/reference/routing-observability.md`
- Modify: `CLAUDE.md` (routing-observability paragraph, one sentence)
- Modify: `deploy/spark/README.md` (one paragraph under §7 Operate)

- [ ] **Step 1: Research notes** (`routing-signal-provenance.md`) — add a section "Seeding by replay" covering, with commit SHAs from `git log --oneline` on this branch:
  - the **stale-as-fresh** counterexample: before this slice, `aggregate_model_signals` stamped every seed row `time.monotonic()`, so a 23-hour-old latency average seeded as fresh and seeded availability had zero decay; the commit that closes it;
  - the **replay-identity** property (spec §3.5) as a statement, why it holds (all updates depend on time only through differences), the Hypothesis test that pins it, and the three falsifications from Task 2 Step 5 with their results;
  - the **cap bound** `0.9^(K-9) × range` and its test;
  - the **cross-replica consequence**: the seed is the replay of the union of replicas' logs, a state no single replica held live;
  - **B39/B41 resolved by construction**: cache hits never call a backend so never appear; stream timing never enters the latency EWMA;
  - add to "what is NOT demonstrated": TTFT is logged, not validated as a routing signal; the flusher can lose up to `flush_interval_s` of observations on a crash.
- [ ] **Step 2: Backlog** — mark B38, B39, B40, B41 `✅ RESOLVED (2026-09-21, <PR>)` with the resolving commit and a short "how". In **B41**, add a visible dated correction: *"Correction (2026-09-21): the claim that Qwen Code / kit-gtm contribute nothing was wrong — every Qwen Code request carries `tools`, which raises `StreamFallback` before `run_stream`, so it is served by `Gateway.run()` and was always observed (live: `availability n=98 measured`). Only plain-chat streaming was blind."* Mark the B38 "latent latency/samples inversion" note resolved (seed no longer aggregates; each signal's count comes from its own observations).
- [ ] **Step 3: Reference** (`routing-observability.md`) — a subsection on `endpoint_observations`: columns, what it never holds, the writer (sink → flusher), pruning, replay seeding, and the first-boot behaviour (empty log → `tier_default`/`neutral` until `min_samples`). Also that `usage_records` now has `status="error"` rows and `error_count`.
- [ ] **Step 4: CLAUDE.md** — in the "Routing observability & signals" paragraph add: "Boot seeding replays the per-attempt `endpoint_observations` log (endpoint health only — no tenant or content), so a seeded signal is a reconstruction of what the live store saw, at its true age."
- [ ] **Step 5: Spark README** — under §7, note: first boot after this deploy seeds nothing (new empty table); every model reports `tier_default`/`neutral` for its first five observations; this is expected, not a regression. No `ALTER` needed.
- [ ] **Step 6: Commit** — `docs(signals): replay seeding research notes; resolve B38-B41; reference + deploy notes`.
