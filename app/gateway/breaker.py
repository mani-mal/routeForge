"""Step-7 per-endpoint circuit breaker (B45).

B45 measured, simulated and live, that the availability scoring term cannot
keep traffic off a dead endpoint: it moves a score by at most its weight, and
a dead endpoint that is 4x cheaper keeps winning. Failover then pays a wasted
attempt on every request, and a streamed request -- which has no failover --
simply fails. Taking a dead endpoint OUT of rotation is this module's job,
not a scoring weight's.

States, per model name:

    closed    --failure_threshold consecutive unhealth failures--> open
    open      --cooldown elapsed---------------------------------> half_open
    half_open --first reported success----------------------------> closed  (count + cooldown reset)
    half_open --first reported failure-----------------------------> open    (cooldown doubles, capped)

Half-open is EVIDENCE-based, with NO per-caller lease: once the cooldown has
elapsed (`now >= open_until`) the model is simply admitted to EVERY caller --
`open_set()` grants nothing and holds nothing -- its only write is the one-shot
`half_open` log/metric dedup flag described below. The FIRST
result reported for that model after the cooldown elapses -- by whichever
caller reports it first -- decides the transition: a success closes, a
failure reopens (cooldown doubled, capped). Before the cooldown elapses, a
result changes nothing: no close, no reopen, no count or cooldown mutation.

The accepted cost: while a circuit is half-open, EVERY concurrent in-flight
request may pick the recovering model and spend one attempt on it if it is
still dead -- there is no single-trial gate any more. Failover
(app/gateway/gateway.py) serves those requests off the peer/escalation path
exactly like any other backend failure; a streamed request (no failover)
simply fails that one call.

This replaces an earlier design (Task 2) that granted a single-caller trial
*lease* from inside `open_set()`: the first caller to merely READ open_set()
burned it, even one that could never have used the model at all (narrower
app scope, wrong tier, over budget, or simply because a cheaper eligible peer
outscored it) -- so a genuinely recovered endpoint could sit excluded for a
full `trial_timeout_s` per such ineligible read, with no failed request ever
having reached it. Removing the lease removes that starvation failure mode
entirely; `open_set()` cannot be "burned" by a caller who was never going to
attempt the model.

`half_open` is still emitted as a transition (metric + one log line) exactly
once per open period -- the first time ANY of `open_set()`, `record_success()`
or `record_failure()` observes `now >= open_until` for that model, tracked by
a one-shot flag on the entry (not a lease: it gates nothing, it only dedupes
the log/metric).

Only failures the gateway classifies with `counts_as_endpoint_unhealth` are
recorded -- a 4xx is the request's fault and never trips a circuit (B36).

What the breaker does NOT do: decide eligibility, or the target tier.
`open_set()` is handed to `Router.select`, which picks the tier from the
POLICY-eligible set first and only then drops open names among that tier's
final candidates (labelled by `feasible_set`'s LAST stage, `circuit_open`) --
so it can only remove a candidate policy already admitted, cannot widen
placement and cannot lower a risk floor or an escalation (rule 8). When
every candidate in the target tier is open, the router bypasses the breaker
within that tier (`circuit_bypass`).

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
    # One-shot dedup flag: has the half_open transition (metric + log)
    # already been emitted for THIS open period? Reset to False every time
    # `_open()` starts a fresh period. Gates nothing about admission -- see
    # module docstring -- it only stops the transition being re-logged by
    # every subsequent caller that observes the same half-open window.
    half_open_logged: bool = False


class CircuitBreaker:
    def __init__(self, cfg: BreakerCfg) -> None:
        self._cfg = cfg
        self._m: dict[str, _Entry] = {}

    @staticmethod
    def _now(now: float | None) -> float:
        return time.monotonic() if now is None else now

    def _transition(self, model: str, from_state: str, to_state: str,
                    cooldown: float | None) -> None:
        """One metric increment and ONE structured log line per transition
        (spec §6): model, from-state, to-state and the cooldown just set
        (`-` when the transition sets none)."""
        metrics.observe_breaker_transition(to_state)
        log.info("circuit transition model=%s from=%s to=%s cooldown_s=%s",
                 model, from_state, to_state,
                 f"{cooldown:.0f}" if cooldown else "-")

    def _open(self, model: str, e: _Entry, t: float, cooldown: float,
              from_state: str) -> _Entry:
        self._transition(model, from_state, "open", cooldown)
        return replace(e, open_until=t + cooldown, cooldown_s=cooldown,
                       half_open_logged=False)

    def _note_half_open(self, model: str, e: _Entry, t: float) -> _Entry:
        """If the cooldown has elapsed and no caller has observed it yet this
        open period, log+meter the half_open transition exactly once. Purely
        a log/metric dedup -- it does not gate admission (see module
        docstring); callers must persist the returned entry themselves."""
        if e.open_until is not None and t >= e.open_until and not e.half_open_logged:
            self._transition(model, "open", "half_open", None)
            return replace(e, half_open_logged=True)
        return e

    def record_failure(self, model: str, *, now: float | None = None) -> None:
        if not self._cfg.enabled:
            return
        t = self._now(now)
        e = self._m.get(model, _Entry())
        if e.open_until is not None:
            if t < e.open_until:
                # Still mid-cooldown: not evidence yet. Changes nothing.
                return
            # Cooldown has elapsed: this is the half-open trial's result.
            # Reopen with cooldown doubled (capped), regardless of who
            # reported it.
            e = self._note_half_open(model, e, t)
            self._m[model] = self._open(
                model, e, t, min(e.cooldown_s * 2, self._cfg.max_cooldown_s),
                "half_open")
            return
        # Closed: accumulate toward the threshold.
        e = replace(e, failures=e.failures + 1)
        if e.failures >= self._cfg.failure_threshold:
            e = self._open(model, e, t, self._cfg.cooldown_s, "closed")
        self._m[model] = e

    def record_success(self, model: str, *, now: float | None = None) -> None:
        e = self._m.get(model)
        if e is None:
            return
        if e.open_until is not None:
            t = self._now(now)
            if t < e.open_until:
                # Still mid-cooldown: not evidence yet. Changes nothing.
                return
            # Cooldown has elapsed: this is the half-open trial's result.
            self._note_half_open(model, e, t)
            self._transition(model, "half_open", "closed", None)
        self._m[model] = _Entry()

    def open_set(self, *, now: float | None = None) -> frozenset[str]:
        """Models to exclude from THIS selection. It grants nothing and holds
        no lease (see module docstring for why the earlier lease design was
        removed); its only write is the one-shot half_open log-dedup flag. A model whose cooldown has elapsed is
        half-open and simply left OUT of the returned set for every caller --
        no admission bookkeeping is made or consumed here. The only mutation
        is the one-shot `half_open_logged` dedup flag, which affects nothing
        about eligibility."""
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
            new_e = self._note_half_open(model, e, t)
            if new_e is not e:
                self._m[model] = new_e
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
