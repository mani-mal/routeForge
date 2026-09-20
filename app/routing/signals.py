"""Measured per-model routing signals (P2a).

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
or residency -- and boot seeding from `usage_records` bounds the divergence.

Every method takes an injectable `now` so decay is testable without sleeping.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, replace

from app.config.schema import SignalsCfg


@dataclass(frozen=True)
class SignalValue:
    value: float      # milliseconds for latency; [0,1] for availability
    source: str       # "measured" | "tier_default" | "neutral" | "disabled"
    samples: int      # observations behind `value`; reported even when unused


@dataclass(frozen=True)
class SeedRow:
    model: str
    avg_latency_ms: float
    error_rate: float
    samples: int
    last_ts: float


@dataclass(frozen=True)
class _ModelState:
    latency_ms: float = 0.0
    latency_n: int = 0
    latency_ts: float = 0.0
    availability: float = 1.0
    availability_n: int = 0
    availability_ts: float = 0.0


class SignalStore:
    def __init__(self, cfg: SignalsCfg) -> None:
        self._cfg = cfg
        self._state: dict[str, _ModelState] = {}

    # -- internals --------------------------------------------------------
    def _now(self, now: float | None) -> float:
        return time.monotonic() if now is None else now

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
    def observe_success(self, model: str, latency_ms: float, *,
                        now: float | None = None) -> None:
        t = self._now(now)
        st = self._state.get(model, _ModelState())
        a_lat = self._alpha(st.latency_n)
        new_lat = (latency_ms if st.latency_n == 0
                   else st.latency_ms + a_lat * (latency_ms - st.latency_ms))
        base = self._decayed_availability(st, t)
        a_av = self._alpha(st.availability_n)
        self._state[model] = replace(
            st,
            latency_ms=new_lat, latency_n=st.latency_n + 1, latency_ts=t,
            availability=min(1.0, base + a_av * (1.0 - base)),
            availability_n=st.availability_n + 1, availability_ts=t)

    def observe_error(self, model: str, *, now: float | None = None) -> None:
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
    def seed(self, rows) -> None:
        """Populate from aggregated `usage_records` at boot. Replaces state for
        each named model; models absent from `rows` are left untouched."""
        for r in rows:
            self._state[r.model] = _ModelState(
                latency_ms=r.avg_latency_ms, latency_n=r.samples, latency_ts=r.last_ts,
                availability=min(1.0, max(0.0, 1.0 - r.error_rate)),
                availability_n=r.samples, availability_ts=r.last_ts)

    def snapshot(self) -> dict[str, dict]:
        return {m: {"latency_ms": st.latency_ms, "latency_n": st.latency_n,
                    "availability": st.availability,
                    "availability_n": st.availability_n}
                for m, st in self._state.items()}
