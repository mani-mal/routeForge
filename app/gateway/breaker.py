"""Step-7 per-endpoint circuit breaker (B45).

B45 measured, simulated and live, that the availability scoring term cannot
keep traffic off a dead endpoint: it moves a score by at most its weight, and
a dead endpoint that is 4x cheaper keeps winning. Failover then pays a wasted
attempt on every request, and a streamed request -- which has no failover --
simply fails. Taking a dead endpoint OUT of rotation is this module's job,
not a scoring weight's.

States, per model name:

    closed    --failure_threshold consecutive unhealth failures--> open
    open      --cooldown elapsed; first open_set() caller--------> half_open
    half_open --trial succeeds--> closed  (count + cooldown reset)
    half_open --trial fails-----> open    (cooldown doubles, capped)

Only failures the gateway classifies with `counts_as_endpoint_unhealth` are
recorded -- a 4xx is the request's fault and never trips a circuit (B36).

What the breaker does NOT do: decide eligibility. `open_set()` is handed to
`Router.select`, which removes those names in `feasible_set`'s LAST stage
(`circuit_open`), after every policy stage -- so it can only remove a
candidate policy already admitted, and cannot widen placement. When that
leaves nothing, the router retries without it (`circuit_bypass`).

Per-process and in-memory, like `SignalStore`: each API replica trips on its
own failures, and a restart starts every circuit closed. Every method takes
an injectable monotonic `now` so tests never sleep.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, replace

from app.config.schema import BreakerCfg
from app.obs import metrics

log = logging.getLogger("serving.breaker")


@dataclass(frozen=True)
class CircuitState:
    state: str                    # "closed" | "open" | "half_open"
    consecutive_failures: int = 0
    open_until: float | None = None


@dataclass(frozen=True)
class _Entry:
    failures: int = 0
    open_until: float | None = None   # set while open or half-open
    cooldown_s: float = 0.0           # the cooldown that produced open_until
    trial_until: float | None = None  # a half-open trial lease is held until


class CircuitBreaker:
    def __init__(self, cfg: BreakerCfg) -> None:
        self._cfg = cfg
        self._m: dict[str, _Entry] = {}

    @staticmethod
    def _now(now: float | None) -> float:
        return time.monotonic() if now is None else now

    def _transition(self, model: str, to_state: str, cooldown: float | None) -> None:
        metrics.observe_breaker_transition(to_state)
        log.info("circuit %s -> %s%s", model, to_state,
                 f" (cooldown {cooldown:.0f}s)" if cooldown else "")

    def _open(self, model: str, e: _Entry, t: float, cooldown: float) -> _Entry:
        self._transition(model, "open", cooldown)
        return replace(e, open_until=t + cooldown, cooldown_s=cooldown,
                       trial_until=None)

    def record_failure(self, model: str, *, now: float | None = None) -> None:
        if not self._cfg.enabled:
            return
        t = self._now(now)
        e = self._m.get(model, _Entry())
        e = replace(e, failures=e.failures + 1)
        if e.trial_until is not None:
            # The half-open trial failed: back to open, cooldown doubled.
            e = self._open(model, e, t,
                           min(e.cooldown_s * 2, self._cfg.max_cooldown_s))
        elif e.open_until is None and e.failures >= self._cfg.failure_threshold:
            e = self._open(model, e, t, self._cfg.cooldown_s)
        self._m[model] = e

    def record_success(self, model: str, *, now: float | None = None) -> None:
        e = self._m.get(model)
        if e is None:
            return
        if e.open_until is not None:
            self._transition(model, "closed", None)
        self._m[model] = _Entry()

    def open_set(self, *, now: float | None = None) -> frozenset[str]:
        """Models to exclude from THIS selection. A model whose cooldown has
        elapsed and has no live trial lease is granted one here -- it is
        left OUT of the returned set for this caller only, and stays in it
        for every other caller until the trial reports back or the lease
        expires (`trial_timeout_s`)."""
        if not self._cfg.enabled:
            return frozenset()
        t = self._now(now)
        out: set[str] = set()
        for model, e in list(self._m.items()):
            if e.open_until is None:
                continue
            if t < e.open_until:
                out.add(model)
                continue
            if e.trial_until is not None and t < e.trial_until:
                out.add(model)          # someone else holds the trial
                continue
            self._m[model] = replace(e, trial_until=t + self._cfg.trial_timeout_s)
            self._transition(model, "half_open", None)
        return frozenset(out)

    def state(self, model: str, *, now: float | None = None) -> CircuitState:
        t = self._now(now)
        e = self._m.get(model, _Entry())
        if e.open_until is None:
            return CircuitState("closed", e.failures, None)
        if t < e.open_until:
            return CircuitState("open", e.failures, e.open_until)
        return CircuitState("half_open", e.failures, e.open_until)

    def snapshot(self, *, now: float | None = None) -> dict[str, dict]:
        return {m: {"state": s.state, "consecutive_failures": s.consecutive_failures,
                    "open_until": s.open_until}
                for m in self._m
                for s in (self.state(m, now=now),)}
