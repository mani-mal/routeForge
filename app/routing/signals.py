"""Measured per-model routing signals (P2a; signal hardening).

Two signals, both in-process and both PREFERENCES:

* **latency** -- an EWMA of observed wall-clock latency, used in place of the
  pre-P2a per-tier constant.
* **availability** -- an EWMA of per-call success, replacing a `capacity` term
  that added the same constant to every candidate and therefore never changed
  a routing decision at all.

Neither can make a candidate eligible or ineligible. `app/policy/feasible.py`
is the one authority on eligibility; this store is injected into the step-6
*strategy*, which by protocol can only reorder the set it is handed.

The store is per-process. Each API replica learns from its own traffic, so two
replicas can hold slightly different estimates for the same model. That is
acceptable for a ranking preference -- it cannot affect eligibility, cost caps
or residency -- and boot seeding, which now REPLAYS the observation log
(`app/routing/observations.py`, spec §3.4) rather than aggregating
`usage_records`, bounds the divergence.

Every method takes an injectable `now` so decay is testable without sleeping.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Iterable

from app.config.schema import SignalsCfg
from app.routing.observations import Observation

if TYPE_CHECKING:
    from app.routing.observations import ObservationSink


@dataclass(frozen=True)
class SignalValue:
    value: float      # milliseconds for latency; [0,1] for availability
    source: str       # "measured" | "tier_default" | "neutral" | "disabled"
    samples: int      # observations behind `value`; reported even when unused


@dataclass(frozen=True)
class _ModelState:
    latency_ms: float = 0.0
    latency_n: int = 0
    latency_ts: float = 0.0
    availability: float = 1.0
    availability_n: int = 0
    availability_ts: float = 0.0


class SignalStore:
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

    # -- internals --------------------------------------------------------
    def _now(self, now: float | None) -> float:
        return time.monotonic() if now is None else now

    def _log(self, t: float, model: str, outcome: str, *, latency_ms=None,
             streamed=False, ttft_ms=None) -> None:
        if self._sink is not None:
            self._sink.put(Observation(t + self._wall_offset, model, outcome,
                                       latency_ms, streamed, ttft_ms))

    def _alpha(self, n: int) -> float:
        """Running mean while samples are few, fixed-width EWMA thereafter."""
        return max(1.0 / (n + 1), self._cfg.alpha_floor)

    def _decayed_availability(self, st: _ModelState, now: float) -> float:
        """Pull availability back toward 1.0 on WALL-CLOCK time.

        This is what closes the starvation trap: down-weighting a failing model
        plus failover means it receives less traffic, so a purely sample-driven
        EWMA would never see the successes it needs to recover. Time decay
        returns it to neutral and earns it a retry regardless of traffic.
        """
        dt = max(0.0, now - st.availability_ts)
        if dt == 0.0 or self._cfg.half_life_s <= 0:
            return st.availability
        return 1.0 - (1.0 - st.availability) * (0.5 ** (dt / self._cfg.half_life_s))

    # -- observation ------------------------------------------------------
    def _apply_available(self, st: _ModelState, t: float) -> _ModelState:
        """The availability-success update, shared by `observe_success` and
        `observe_stream_success` so the rule exists exactly once."""
        base = self._decayed_availability(st, t)
        a_av = self._alpha(st.availability_n)
        return replace(
            st,
            availability=min(1.0, base + a_av * (1.0 - base)),
            availability_n=st.availability_n + 1, availability_ts=t)

    def observe_success(self, model: str, latency_ms: float, *,
                        now: float | None = None) -> None:
        t = self._now(now)
        st = self._state.get(model, _ModelState())
        a_lat = self._alpha(st.latency_n)
        new_lat = (latency_ms if st.latency_n == 0
                   else st.latency_ms + a_lat * (latency_ms - st.latency_ms))
        st = self._apply_available(st, t)
        self._state[model] = replace(
            st, latency_ms=new_lat, latency_n=st.latency_n + 1, latency_ts=t)
        self._log(t, model, "ok", latency_ms=latency_ms)

    def observe_stream_success(self, model: str, ttft_ms: float | None, *,
                               now: float | None = None) -> None:
        """A streamed call finished successfully. Availability-only: stream
        timing (queueing, client backpressure, chunk cadence) is not
        comparable to the non-streamed backend latency the EWMA tracks, so
        TTFT is logged for observability but never enters `latency_ms`
        (spec §5)."""
        t = self._now(now)
        st = self._state.get(model, _ModelState())
        self._state[model] = self._apply_available(st, t)
        self._log(t, model, "ok", streamed=True, ttft_ms=ttft_ms)

    def observe_error(self, model: str, *, streamed: bool = False,
                      now: float | None = None) -> None:
        """An endpoint-side failure. Latency is NOT updated -- the duration of a
        failed call says nothing about how fast the endpoint serves."""
        t = self._now(now)
        st = self._state.get(model, _ModelState())
        base = self._decayed_availability(st, t)
        a_av = self._alpha(st.availability_n)
        self._state[model] = replace(
            st,
            availability=max(0.0, base + a_av * (0.0 - base)),
            availability_n=st.availability_n + 1, availability_ts=t)
        self._log(t, model, "error", streamed=streamed)

    # -- reads ------------------------------------------------------------
    def _tier_default(self, tier: str) -> float:
        d = self._cfg.tier_latency_ms
        return d.get(tier, self._cfg.latency_scale_ms)

    def latency(self, model: str, tier: str, *, now: float | None = None) -> SignalValue:
        default = self._tier_default(tier)
        if not self._cfg.enabled:
            return SignalValue(default, "disabled", 0)
        st = self._state.get(model)
        if st is None or st.latency_n < self._cfg.min_samples:
            return SignalValue(default, "tier_default", 0 if st is None else st.latency_n)
        if self._now(now) - st.latency_ts > self._cfg.staleness_s:
            # A measurement from two days ago is not a measurement. Fall back
            # OUTRIGHT rather than drifting toward the default -- a drifted
            # value is neither measured nor default and cannot be labelled
            # honestly.
            return SignalValue(default, "tier_default", st.latency_n)
        return SignalValue(st.latency_ms, "measured", st.latency_n)

    def availability(self, model: str, *, now: float | None = None) -> SignalValue:
        if not self._cfg.enabled:
            return SignalValue(1.0, "disabled", 0)
        st = self._state.get(model)
        if st is None or st.availability_n < self._cfg.min_samples:
            return SignalValue(1.0, "neutral", 0 if st is None else st.availability_n)
        v = self._decayed_availability(st, self._now(now))
        return SignalValue(min(1.0, max(0.0, v)), "measured", st.availability_n)

    # -- lifecycle --------------------------------------------------------
    def replay(self, observations: Iterable[Observation]) -> None:
        """Rebuild state from the observation log (spec §3.4). Each row is
        applied through the SAME observe_* method that produced it, at its
        TRUE age (mono = wall - this process's offset), with the sink
        detached so replay never re-logs what it reads. Rows must be in
        ascending `ts` order. A malformed row -- an unknown outcome, or a
        non-streamed `ok` with no latency -- is skipped, never guessed at (a
        guessed 0 ms sample would bias routing toward that model)."""
        sink, self._sink = self._sink, None
        try:
            for o in observations:
                if o.outcome not in ("ok", "error"):
                    continue
                if o.outcome == "ok" and not o.streamed and o.latency_ms is None:
                    continue
                t = o.ts - self._wall_offset
                if o.outcome == "error":
                    self.observe_error(o.model, streamed=o.streamed, now=t)
                elif o.streamed:
                    self.observe_stream_success(o.model, o.ttft_ms, now=t)
                else:
                    self.observe_success(o.model, float(o.latency_ms), now=t)
        finally:
            self._sink = sink

    def snapshot(self) -> dict[str, dict]:
        return {m: {"latency_ms": st.latency_ms, "latency_n": st.latency_n,
                    "availability": st.availability,
                    "availability_n": st.availability_n}
                for m, st in self._state.items()}
