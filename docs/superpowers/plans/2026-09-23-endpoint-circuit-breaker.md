# Endpoint Circuit Breaker Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A per-endpoint circuit breaker in step 7 that takes an endpoint which keeps failing
out of routing for a cooldown, through the one eligibility seam. This closes backlog B45.

**Architecture:**
- A pure, clock-injectable `CircuitBreaker` (`app/gateway/breaker.py`) is fed by the
  gateway's existing `counts_as_endpoint_unhealth` verdicts.
- Its `open_set()` is passed into every `Router.select`, which threads it into
  `feasible_set` as a new last stage, `circuit_open`. That stage can only remove candidates
  that are already policy-eligible.
- If that stage alone empties the set, `Router.select` retries without it and marks the
  decision `circuit_bypass`.
- FeasibleRouteBench's online arm gets the same breaker, so the effect can be measured.

**Tech Stack:** Python 3.12, Pydantic v2, FastAPI, pytest, prometheus_client (optional extra).

**Spec:** `docs/superpowers/specs/2026-09-23-endpoint-circuit-breaker-design.md`

## Global Constraints

- Run tests with `LITELLM_LOCAL_MODEL_COST_MAP=True` set, or pytest hangs on a network fetch.
  In a git worktree use the main checkout's venv: `../routeForge/.venv/bin/python -m pytest`.
  The worktree's own `.venv` lacks the Presidio extra.
- Read the summary with `-p no:cacheprovider -W ignore` and **without** `-qq`. `-qq` hides
  the pass/fail count.
- **Only failures where `counts_as_endpoint_unhealth(exc)` is true may trip the breaker.**
  A 4xx other than 429, including `ContextWindowExceeded`, never trips it.
- The breaker may only **remove** candidates that are already policy-eligible. It must never
  widen placement. An `onprem_only` request must never reach cloud through any breaker path.
- `breaker.enabled: false` must reproduce today's decisions exactly.
- Breaker state is per process and starts closed at boot. No persistence, no replay.
- Config values (routing.yaml): `enabled: true`, `failure_threshold: 3`, `cooldown_s: 30`,
  `max_cooldown_s: 300`, `trial_timeout_s: 60`.
- Stage code is `circuit_open`; its reason is `"excluded · circuit open"`.
- Metric family: `routeforge_breaker_transitions_total{to_state}`, with `to_state` ∈
  `open|half_open|closed`. No model label.
- Every `DecisionRecord` field added must be optional with a default, because the wire must
  not break.
- Match the surrounding code's docstring and comment density. This code is read on a
  projector.
- Commit after each task. End commit messages with
  `Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>`.

**Deviation from the spec, decided while planning:** the spec §4 put `circuit_open`
"right after `prior_failure`". This plan makes it the **last** stage (stage 7, after
`model_scope`) instead. That way every policy reason wins over it, and `circuit_open` only
ever labels a candidate that is otherwise fully eligible. That makes "the bypass restores
exactly the policy-eligible set" true by construction. Task 6 updates the spec text.

---

### Task 1: `BreakerCfg` config, routing.yaml block, lint

**Files:**
- Modify: `app/config/schema.py` (add `BreakerCfg` next to `SignalsCfg`, and a `breaker`
  field on `RoutingCfg` right after `signals`)
- Modify: `config/routing.yaml` (add the `breaker:` block after `signals:`)
- Modify: `app/config/lint.py` (add checks next to the `signals` checks, ~line 74-100)
- Test: `tests/test_config_lint.py`

**Interfaces:**
- Produces: `app.config.schema.BreakerCfg` with fields `enabled: bool`,
  `failure_threshold: int`, `cooldown_s: float`, `max_cooldown_s: float`,
  `trial_timeout_s: float`; `Settings.routing.breaker: BreakerCfg`.

- [ ] **Step 1: Write the failing tests** (append to `tests/test_config_lint.py`)

```python
from app.config.schema import BreakerCfg


def test_breaker_config_loads_with_shipped_values():
    b = load_settings().routing.breaker
    assert b == BreakerCfg(enabled=True, failure_threshold=3, cooldown_s=30.0,
                           max_cooldown_s=300.0, trial_timeout_s=60.0)


@pytest.mark.parametrize("field,value,code", [
    ("failure_threshold", 0, "BREAKER_THRESHOLD_NOT_POSITIVE"),
    ("cooldown_s", 0, "BREAKER_COOLDOWN_NOT_POSITIVE"),
    ("trial_timeout_s", -1, "BREAKER_TRIAL_TIMEOUT_NOT_POSITIVE"),
    ("max_cooldown_s", 10, "BREAKER_MAX_COOLDOWN_BELOW_COOLDOWN"),
])
def test_bad_breaker_values_are_caught(field, value, code):
    s = copy.deepcopy(load_settings())
    setattr(s.routing.breaker, field, value)
    assert any(code in e for e in lint_settings(s))
```

- [ ] **Step 2: Run it and see it fail**

Run: `LITELLM_LOCAL_MODEL_COST_MAP=True ../routeForge/.venv/bin/python -m pytest tests/test_config_lint.py -p no:cacheprovider -W ignore`
Expected: FAIL with `ImportError: cannot import name 'BreakerCfg'`.

- [ ] **Step 3: Implement**

In `app/config/schema.py`, after `class SignalsCfg`:

```python
class BreakerCfg(BaseModel):
    """Per-endpoint circuit breaker (step 7; app/gateway/breaker.py). Owned by
    routing.yaml only. `enabled: false` makes the breaker's open set always
    empty -- exactly the pre-breaker routing, and the control arm."""
    enabled: bool = True
    # Consecutive endpoint-unhealth failures (counts_as_endpoint_unhealth)
    # that open the circuit. Any success resets the count.
    failure_threshold: int = 3
    # First open period; doubles on every failed half-open trial, capped below.
    cooldown_s: float = 30.0
    max_cooldown_s: float = 300.0
    # A half-open trial lease expires after this, so a trial whose caller
    # vanished (client disconnect) cannot keep the circuit half-open forever.
    trial_timeout_s: float = 60.0
```

On `RoutingCfg`, right after `signals: SignalsCfg = SignalsCfg()`:

```python
    # Step-7 endpoint circuit breaker. Owned here (routing input), never restated.
    breaker: BreakerCfg = BreakerCfg()
```

In `config/routing.yaml`, after the `signals:` block:

```yaml
# Step-7 endpoint circuit breaker (app/gateway/breaker.py). An endpoint that
# fails `failure_threshold` times in a row (endpoint-side failures only -- a
# 4xx never counts) is excluded from routing for `cooldown_s`, then admitted
# for ONE trial request; a failed trial doubles the cooldown up to
# `max_cooldown_s`. It can only remove candidates policy already admitted, and
# if every eligible candidate is open the router tries one anyway
# (circuit_bypass). Per-process state. `enabled: false` = no breaker.
breaker:
  enabled: true
  failure_threshold: 3
  cooldown_s: 30
  max_cooldown_s: 300
  trial_timeout_s: 60
```

In `app/config/lint.py`, after the signals checks:

```python
    # Step-7 circuit breaker (routing.yaml `breaker`). Zero/negative values
    # would open a circuit on no failures or never let it recover.
    brk = s.routing.breaker
    if brk.failure_threshold < 1:
        errs.append(f"BREAKER_THRESHOLD_NOT_POSITIVE {brk.failure_threshold}")
    if brk.cooldown_s <= 0:
        errs.append(f"BREAKER_COOLDOWN_NOT_POSITIVE {brk.cooldown_s}")
    if brk.trial_timeout_s <= 0:
        errs.append(f"BREAKER_TRIAL_TIMEOUT_NOT_POSITIVE {brk.trial_timeout_s}")
    if brk.max_cooldown_s < brk.cooldown_s:
        errs.append(f"BREAKER_MAX_COOLDOWN_BELOW_COOLDOWN "
                    f"{brk.max_cooldown_s} < {brk.cooldown_s}")
```

Also check whether the lint has a single-owner / duplicated-concern check that lists
routing-owned keys (search `lint.py` for `signals` in any ownership table). If it does,
add `breaker` there too.

- [ ] **Step 4: Run it and see it pass.** Run the same command. Expected: all pass,
  including `test_current_config_is_clean`.

- [ ] **Step 5: Check that the routing_policy_version hash now covers the breaker.**
  Run `grep -n "routing_policy_version" app/config/loader.py` and confirm the hash is
  computed over the parsed routing config. If it covers the whole `routing` model, nothing
  needs doing. If it covers an explicit key list, add `breaker`, then add a test asserting
  that changing `breaker.cooldown_s` changes `routing_policy_version`.

- [ ] **Step 6: Commit** `feat(routing): breaker config block and lint (B45)`

---

### Task 2: `CircuitBreaker` state machine and metric

**Files:**
- Create: `app/gateway/breaker.py`
- Modify: `app/obs/metrics.py` (one new Counter family and one helper)
- Test: `tests/test_breaker.py` (new)

**Interfaces:**
- Consumes: `BreakerCfg` (Task 1).
- Produces:
  - `CircuitBreaker(cfg: BreakerCfg)`
  - `.record_failure(model: str, *, now: float | None = None) -> None`
  - `.record_success(model: str, *, now: float | None = None) -> None`
  - `.open_set(*, now: float | None = None) -> frozenset[str]`. This grants a half-open
    trial lease to the caller; see the docstring.
  - `.state(model: str, *, now: float | None = None) -> CircuitState`
  - `.snapshot(*, now: float | None = None) -> dict[str, dict]`
  - `CircuitState` is a frozen dataclass: `state: str` ("closed"|"open"|"half_open"),
    `consecutive_failures: int`, `open_until: float | None`.
  - `metrics.observe_breaker_transition(to_state: str) -> None`.

- [ ] **Step 1: Write the failing tests** in `tests/test_breaker.py`:

```python
"""Step-7 circuit breaker state machine (spec §3). Pure and clock-injected --
every call passes `now`, so nothing sleeps."""
from app.config.schema import BreakerCfg
from app.gateway.breaker import CircuitBreaker

CFG = BreakerCfg(enabled=True, failure_threshold=3, cooldown_s=30.0,
                 max_cooldown_s=100.0, trial_timeout_s=60.0)


def _trip(b, model="m", start=0.0):
    for k in range(CFG.failure_threshold):
        b.record_failure(model, now=start + k)


def test_opens_after_threshold_consecutive_failures():
    b = CircuitBreaker(CFG)
    b.record_failure("m", now=0.0)
    b.record_failure("m", now=1.0)
    assert b.open_set(now=1.5) == frozenset()
    b.record_failure("m", now=2.0)
    assert b.open_set(now=2.5) == frozenset({"m"})
    assert b.state("m", now=2.5).state == "open"


def test_success_resets_the_count():
    b = CircuitBreaker(CFG)
    b.record_failure("m", now=0.0)
    b.record_failure("m", now=1.0)
    b.record_success("m", now=2.0)
    b.record_failure("m", now=3.0)
    assert b.open_set(now=3.5) == frozenset()


def test_half_open_admits_exactly_one_trial():
    b = CircuitBreaker(CFG)
    _trip(b)                                    # opens at t=2, until 32
    assert "m" in b.open_set(now=31.0)
    assert "m" not in b.open_set(now=33.0)      # this caller holds the trial
    assert "m" in b.open_set(now=33.1)          # everyone else still excluded
    assert b.state("m", now=33.1).state == "half_open"


def test_trial_success_closes():
    b = CircuitBreaker(CFG)
    _trip(b)
    b.open_set(now=33.0)
    b.record_success("m", now=34.0)
    assert b.state("m", now=34.0).state == "closed"
    assert b.open_set(now=34.1) == frozenset()


def test_trial_failure_reopens_with_doubled_cooldown_capped():
    b = CircuitBreaker(CFG)
    _trip(b)                                    # cooldown 30
    b.open_set(now=33.0)
    b.record_failure("m", now=34.0)             # cooldown 60 -> open until 94
    assert "m" in b.open_set(now=93.0)
    assert "m" not in b.open_set(now=95.0)      # trial granted
    b.record_failure("m", now=96.0)             # cooldown 120 -> capped 100 -> until 196
    assert "m" in b.open_set(now=195.0)
    assert "m" not in b.open_set(now=197.0)


def test_trial_lease_expires():
    b = CircuitBreaker(CFG)
    _trip(b)
    assert "m" not in b.open_set(now=33.0)      # trial leased at 33
    assert "m" in b.open_set(now=90.0)          # lease still held (< 33+60)
    assert "m" not in b.open_set(now=94.0)      # lease expired -> new trial


def test_disabled_never_opens():
    b = CircuitBreaker(CFG.model_copy(update={"enabled": False}))
    _trip(b)
    assert b.open_set(now=3.0) == frozenset()


def test_models_are_independent():
    b = CircuitBreaker(CFG)
    _trip(b, "a")
    b.record_failure("b", now=0.0)
    assert b.open_set(now=3.0) == frozenset({"a"})


def test_snapshot_reports_state():
    b = CircuitBreaker(CFG)
    _trip(b, "a")
    snap = b.snapshot(now=3.0)
    assert snap["a"] == {"state": "open", "consecutive_failures": 3,
                         "open_until": 32.0}
```

- [ ] **Step 2: Run it and see it fail.** Run:
  `LITELLM_LOCAL_MODEL_COST_MAP=True ../routeForge/.venv/bin/python -m pytest tests/test_breaker.py -p no:cacheprovider -W ignore`.
  Expected: `ModuleNotFoundError: app.gateway.breaker`.

- [ ] **Step 3: Implement** `app/gateway/breaker.py`:

```python
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
        if t < e.open_until or e.trial_until is None:
            return CircuitState("open" if t < e.open_until else "half_open",
                                e.failures, e.open_until)
        return CircuitState("half_open", e.failures, e.open_until)

    def snapshot(self, *, now: float | None = None) -> dict[str, dict]:
        return {m: {"state": s.state, "consecutive_failures": s.consecutive_failures,
                    "open_until": s.open_until}
                for m in self._m
                for s in (self.state(m, now=now),)}
```

Check `test_half_open_admits_exactly_one_trial` against `state()`. After the trial is
leased at t=33, `open_until=32` and `trial_until=93`, so `state` must say `half_open`. The
code above handles that: `t >= open_until` and `trial_until` is set. An open model whose
cooldown has elapsed but which no caller has asked about yet (no lease) reports `half_open`
as well, because `t >= open_until` and `trial_until is None` falls to the
`"open" if t < open_until else "half_open"` branch. **Make the tests the judge.** If a
state assertion disagrees with this sketch, fix the sketch, not the test.

In `app/obs/metrics.py`, inside `if AVAILABLE:`:

```python
    _breaker = Counter(
        "routeforge_breaker_transitions_total",
        "Circuit-breaker state transitions, by the state entered.",
        ["to_state"])
```

and the helper:

```python
def observe_breaker_transition(to_state: str) -> None:
    if AVAILABLE:
        _breaker.labels(to_state=to_state).inc()
```

- [ ] **Step 4: Run it and see it pass.** Run the same command. Also run
  `tests/test_metrics.py`. If it asserts on the exact set of families, add the new one
  there too.

- [ ] **Step 5: Commit** `feat(gateway): circuit breaker state machine (B45)`

---

### Task 3: The `circuit_open` stage, `Router.select(open_circuits=…)`, and the all-open bypass

**Files:**
- Modify: `app/policy/feasible.py`:
  - constants `CIRCUIT_OPEN = "circuit_open"` and
    `REASON_CIRCUIT_OPEN = "excluded · circuit open"` next to `PRIOR_FAILURE` (~line 97-114);
  - a stage-7 row in the module docstring table;
  - an `open_circuits` param on `candidate_rejection` and `feasible_set`.
- Modify: `app/routing/router.py`:
  - `RoutingDecision.circuit_bypass: bool = False`;
  - `select(..., open_circuits: frozenset[str] = frozenset())`;
  - the bypass.
- Modify: `app/models.py`: `DecisionRecord.circuit_bypass: bool = False`, with a comment.
- Test: `tests/test_feasible_set.py`, and `tests/test_router_breaker.py` (new).

**Interfaces:**
- Consumes: nothing from Tasks 1–2 (pure names).
- Produces:
  - `feasible_set(..., open_circuits: frozenset[str] = frozenset())`
  - `Router.select(ctx, tokens_after, complexity, min_tier=None, exclude=frozenset(), output_tokens=None, open_circuits=frozenset()) -> RoutingDecision`
  - `RoutingDecision.circuit_bypass`
  - `DecisionRecord.circuit_bypass`
  - `app.policy.feasible.CIRCUIT_OPEN`

- [ ] **Step 1: Write the failing tests.**
  - In `tests/test_feasible_set.py`, read the file's existing helpers first and reuse its
    settings/registry/ctx builders. Add three tests:
    1. A candidate named in `open_circuits` lands in `rejected` with `code == "circuit_open"`
       and is absent from `eligible`.
    2. A candidate that is **both** placement-excluded (a cloud model under `onprem_only`)
       and in `open_circuits` is rejected with code `residency`, not `circuit_open`, because
       policy reasons win.
    3. With `open_circuits=frozenset()` the result equals the call without the parameter.
  - In `tests/test_router_breaker.py`, reuse the helpers from
    `tests/test_gateway_lateral_failover.py`: copy `_onprem`, `_build_reg`/`_reg` and `_ctx`
    into the new file. Do not import from another test module.

```python
def test_open_circuit_is_skipped_for_a_peer():
    reg = _reg({"a": _onprem("a"), "b": _onprem("b")})
    d = Router(S, registry=reg).select(_ctx(reg, {"onprem"}, onprem=True), 20, 0.1,
                                        open_circuits=frozenset({"a"}))
    assert d.chosen.name == "b"
    assert d.circuit_bypass is False
    assert any(r.name == "a" and r.code == "circuit_open" for r in d.rejected)


def test_all_open_bypasses_and_marks_it():
    reg = _reg({"a": _onprem("a"), "b": _onprem("b")})
    d = Router(S, registry=reg).select(_ctx(reg, {"onprem"}, onprem=True), 20, 0.1,
                                        open_circuits=frozenset({"a", "b"}))
    assert d.chosen.name in {"a", "b"}
    assert d.circuit_bypass is True
    assert "circuit bypass" in d.reason


def test_bypass_never_widens_placement():
    # every on-prem model open, cloud enabled in the pool, onprem_only ctx
    reg = _reg({"a": _onprem("a"), "b": _onprem("b")})
    d = Router(S, registry=reg).select(_ctx(reg, {"onprem"}, onprem=True), 20, 0.1,
                                        open_circuits=frozenset({"a", "b"}))
    assert d.chosen.provider == "onprem"


def test_no_eligible_for_policy_reasons_still_raises():
    # nothing on-prem onboarded, onprem_only: the empty set is policy, not breaker
    reg = _reg({})
    with pytest.raises(NoEligibleCandidate):
        Router(S, registry=reg).select(_ctx(reg, {"onprem"}, onprem=True), 20, 0.1,
                                        open_circuits=frozenset({"x"}))


def test_prior_failure_plus_open_circuits_bypass_keeps_prior_failure():
    # a failed on this request, b open: the bypass restores b, never a
    reg = _reg({"a": _onprem("a"), "b": _onprem("b")})
    d = Router(S, registry=reg).select(_ctx(reg, {"onprem"}, onprem=True), 20, 0.1,
                                        exclude=frozenset({"a"}),
                                        open_circuits=frozenset({"b"}))
    assert d.chosen.name == "b" and d.circuit_bypass is True
```

- [ ] **Step 2: Run them and see them fail**, with `TypeError: unexpected keyword argument 'open_circuits'`.

- [ ] **Step 3: Implement.**
  - **`feasible.py`.** Add the constants. Add the table row
    `| 7 | an open circuit (step-7 breaker)       | `circuit_open`  | "excluded · circuit open" |`
    and a docstring sentence under the table: the stage is last so that every policy reason
    wins, and so that `circuit_open` only ever labels an otherwise-eligible candidate. Also
    change the table's "six stages" wording to "seven". Add
    `open_circuits: frozenset[str] = frozenset()` as a keyword parameter on both
    `candidate_rejection` and `feasible_set`. In `feasible_set`, pass it through. In
    `candidate_rejection`, at the very end, before `return None`, add:

```python
    # Stage 7: a circuit the step-7 breaker holds open (app/gateway/breaker.py).
    # LAST, after every policy stage: it only ever labels a candidate policy
    # fully admitted, so dropping it can only shrink the set, and the router's
    # all-open bypass restores exactly the policy-eligible set.
    if c.name in open_circuits:
        return _rejection(c, REASON_CIRCUIT_OPEN, CIRCUIT_OPEN)
```

  - **`router.py`.** Rename the current `select` body to
    `_select(self, ctx, tokens_after, complexity, min_tier, exclude, output_tokens, open_circuits)`,
    which passes `open_circuits=open_circuits` to `feasible_set`. The public `select`
    becomes:

```python
    def select(self, ctx: ExecutionPolicyContext, tokens_after: int,
               complexity: float, min_tier: str | None = None,
               exclude: frozenset[str] = frozenset(),
               output_tokens: int | None = None,
               open_circuits: frozenset[str] = frozenset()) -> RoutingDecision:
        """... (keep existing doc) `open_circuits` are models the step-7 breaker
        holds open. They are removed LAST (feasible_set stage 7). If that
        leaves no candidate -- every candidate policy admits is open -- select
        again without them and mark the decision `circuit_bypass`: trying an
        endpoint that may have recovered is never worse than today's
        no-breaker behaviour, and the retry can only restore candidates
        policy already admitted."""
        try:
            return self._select(ctx, tokens_after, complexity, min_tier, exclude,
                                output_tokens, open_circuits)
        except NoEligibleCandidate:
            if not open_circuits:
                raise
        d = self._select(ctx, tokens_after, complexity, min_tier, exclude,
                         output_tokens, frozenset())
        d.circuit_bypass = True
        d.reason += "; circuit bypass: every eligible candidate is open"
        return d
```

  The retry raises its own `NoEligibleCandidate` when the empty set was policy's doing.
  That is correct, and `test_no_eligible_for_policy_reasons_still_raises` pins it.
  - **`models.py`.** On `DecisionRecord`, after `scoring`:

```python
    # Step-7 breaker: True when every candidate policy admitted was held open
    # and the router tried one anyway (app/routing/router.py::select).
    circuit_bypass: bool = False
```

- [ ] **Step 4: Run the new tests and the existing suites that call `select`/`feasible_set`**:
  `tests/test_feasible_set.py tests/test_router_breaker.py tests/test_router.py tests/test_routing_strategy.py tests/test_gateway_lateral_failover.py`.
  Expected: all pass.
- [ ] **Step 5: Commit** `feat(routing): circuit_open eligibility stage and all-open bypass (B45)`

---

### Task 4: Gateway wiring, app wiring, admin view

**Files:**
- Modify: `app/gateway/gateway.py` (`Gateway.__init__`, `run`, `run_stream`)
- Modify: `app/main.py` (~line 239-245: build one `CircuitBreaker`, store it on
  `app.state.breaker`, pass it to `Gateway`)
- Modify: `app/api/admin_routes.py` (`list_onboarded_models` ~line 1116: add `circuit`)
- Modify: `ui/src/...`, the Infrastructure models table. Find it with
  `grep -rn "probe_status" ui/src`. Add a small state badge when `circuit.state !== "closed"`.
- Test: `tests/test_gateway_breaker.py` (new), and `tests/test_onboard_api.py` (one
  assertion).

**Interfaces:**
- Consumes: `CircuitBreaker` (Task 2), and `Router.select(open_circuits=…)` plus
  `RoutingDecision.circuit_bypass` (Task 3).
- Produces: `Gateway(settings, backend, router, registry=None, signals=None, breaker=None)`;
  `app.state.breaker`; and a `circuit` key on each `/admin/infra/models` row, shaped
  `{"state": str, "consecutive_failures": int, "open_until_s": float | None}`, where
  `open_until_s` is seconds from now, or None.

- [ ] **Step 1: Write the failing tests** in `tests/test_gateway_breaker.py`. Copy the
  `_FailForModels`, `_onprem`, `_reg` and `_ctx` helpers from
  `tests/test_gateway_lateral_failover.py`.

```python
def _gw(backend, reg, breaker):
    return Gateway(S, backend, Router(S, registry=reg), reg, breaker=breaker)


def _call(gw, ctx):
    return asyncio.run(gw.run(ctx, [Message(role="user", content="hi")], 20, 0.1, {}))


def test_open_circuit_saves_the_wasted_attempt():
    reg = _reg({"a": _onprem("a"), "b": _onprem("b")})
    backend = _FailForModels(S, {"a"})
    br = CircuitBreaker(S.routing.breaker)
    gw = _gw(backend, reg, br)
    ctx = _ctx(reg, {"onprem"}, onprem=True)
    # Force "a" to be tried first by making it the only candidate until it trips.
    for _ in range(S.routing.breaker.failure_threshold):
        br.record_failure("a")
    backend.calls.clear()
    _result, records = _call(gw, ctx)
    assert backend.calls == ["b"]                   # "a" never attempted
    router = next(r for r in records if r.step == "router")
    assert any(x.name == "a" and x.code == "circuit_open" for x in router.rejected)


def test_gateway_failures_trip_the_breaker_and_4xx_does_not():
    reg = _reg({"a": _onprem("a"), "b": _onprem("b")})
    br = CircuitBreaker(S.routing.breaker)
    gw = _gw(_FailForModels(S, {"a"}), reg, br)
    ctx = _ctx(reg, {"onprem"}, onprem=True)
    for _ in range(S.routing.breaker.failure_threshold):
        _call(gw, ctx)          # whenever "a" is picked first it fails over to "b"
    # "a" failed at least once per call it was chosen; assert via state, not count:
    st = br.state("a")
    assert st.consecutive_failures >= 1


def test_success_records_on_the_breaker():
    reg = _reg({"a": _onprem("a")})
    br = CircuitBreaker(S.routing.breaker)
    br.record_failure("a"); br.record_failure("a")
    _call(_gw(_FailForModels(S, set()), reg, br), _ctx(reg, {"onprem"}, onprem=True))
    assert br.state("a").consecutive_failures == 0


def test_all_open_bypass_is_on_the_record():
    reg = _reg({"a": _onprem("a")})
    br = CircuitBreaker(S.routing.breaker)
    for _ in range(S.routing.breaker.failure_threshold):
        br.record_failure("a")
    _result, records = _call(_gw(_FailForModels(S, set()), reg, br),
                             _ctx(reg, {"onprem"}, onprem=True))
    router = next(r for r in records if r.step == "router")
    assert router.circuit_bypass is True


def test_no_breaker_is_todays_behaviour():
    reg = _reg({"a": _onprem("a"), "b": _onprem("b")})
    backend = _FailForModels(S, {"a"})
    _result, records = _call(_gw(backend, reg, None), _ctx(reg, {"onprem"}, onprem=True))
    assert all(not getattr(r, "circuit_bypass", False) for r in records)
```

Add a 4xx test. Create an exception class with `status_code = 400` and a
`_FailWith(exc_factory)` backend that raises it for model "a". After
`failure_threshold + 1` calls, assert `br.state("a").consecutive_failures == 0` and
`br.open_set() == frozenset()`.

Add a streaming test. Trip "a", call `gw.run_stream(...)`, consume the iterator, and assert
that the router record's `chosen` is not "a" and that "a" is rejected with `circuit_open`.
Read `run_stream`'s docstring for the consumption contract.

- [ ] **Step 2: Run them and see them fail**, with
  `TypeError: Gateway.__init__() got an unexpected keyword argument 'breaker'`.

- [ ] **Step 3: Implement.**
  - **Gateway init.** `Gateway.__init__` gains `breaker: "CircuitBreaker | None" = None`,
    stored as `self._breaker`. Add a helper:

```python
    def _open_circuits(self) -> frozenset[str]:
        return self._breaker.open_set() if self._breaker is not None else frozenset()
```

  - **`run`.** Pass `open_circuits=self._open_circuits()` to the `self._router.select(...)`
    call inside the loop, so it is re-read on every hop. In the `except Exception as exc:`
    branch, next to `observe_error`, add
    `if self._breaker is not None and counts_as_endpoint_unhealth(exc): self._breaker.record_failure(cand.name)`.
    After a successful generate, next to `observe_success`, add
    `if self._breaker is not None: self._breaker.record_success(cand.name)`. On the router
    `DecisionRecord(...)`, pass `circuit_bypass=decision.circuit_bypass`.
  - **`run_stream`.** Pass `open_circuits=self._open_circuits()` to `select`. In `_agen`,
    record success on the `final` chunk and failure in the `except` branch, both gated like
    the signals, and pass `circuit_bypass=decision.circuit_bypass` to its record.
  - **`app/main.py`.**
    `breaker = CircuitBreaker(settings.routing.breaker); app.state.breaker = breaker`, then
    `Gateway(..., signals=signals, breaker=breaker)`. The import is
    `from app.gateway.breaker import CircuitBreaker`.
  - **`admin_routes.list_onboarded_models`.** Compute
    `br = getattr(request.app.state, "breaker", None)` and `now = time.monotonic()`. For each
    row, add
    `"circuit": _circuit_dict(br.state(m.slug, now=now), now) if br else {"state": "closed", "consecutive_failures": 0, "open_until_s": None}`,
    where `_circuit_dict` converts `open_until` into seconds from now, or None. Document in a
    comment that the value is **this replica's** view.
  - **`tests/test_onboard_api.py`.** Add one assertion that listed rows carry
    `circuit.state == "closed"`.
  - **UI.** In the models table, render a small badge `circuit open` (amber) or `trial`
    (for `half_open`) next to the probe status when the state is not `closed`. Type-check
    with `./node_modules/.bin/tsc -b --noEmit` in `ui/`, and do not use `npm run`, which rtk
    mangles.

- [ ] **Step 4: Run the tests:** `tests/test_gateway_breaker.py tests/test_gateway_lateral_failover.py tests/test_gateway.py tests/test_onboard_api.py tests/test_metrics.py`. Expected: all pass.
- [ ] **Step 5: Run the full suite.** Expected: everything passes except known flakes. If a
  test fails, read it before touching it; see Global Constraints for the command.
- [ ] **Step 6: Commit** `feat(gateway): wire the circuit breaker into failover, streaming and admin (B45)`

---

### Task 5: FeasibleRouteBench: the breaker in the online arm, plus a `breaker_off` control arm

**Files:**
- Modify: `app/eval/strategies.py`:
  - `_router_chain` takes an optional `open_circuits: Callable[[], frozenset[str]] | None`;
  - `OnlineArm` owns a `CircuitBreaker` when `settings.routing.breaker.enabled`.
- Modify: `app/eval/runner.py` (~line 485-490: add a `router_online:breaker_off` control arm
  for the base (unvaried) config only, when the breaker is enabled; also document it in
  the report's strategy notes near line 106).
- Test: `tests/eval/test_strategies.py`, `tests/eval/test_outage_scenario.py`

**Interfaces:**
- Consumes: `CircuitBreaker` (Task 2) and `Router.select(open_circuits=…)` (Task 3).
- Produces: `OnlineArm(settings, registry, max_attempts, weights=None, *, seconds_per_case, breaker: bool | None = None)`. `None` means follow
  `settings.routing.breaker.enabled`. There is also an arm named `router_online:breaker_off`.

- [ ] **Step 1: Write the failing tests.**
  - In `tests/eval/test_strategies.py`, add a test using the existing
    `_slow_fast_setup`-style builder (`_build`) with two same-price onprem endpoints `a`
    and `b`. Drive an `OnlineArm(..., breaker=True)` through cases 0..5, where "a" fails
    (`counts_unhealth=True`) on every case, observing after each case. Assert that after
    `failure_threshold` observed failures of "a", the next case's chain does **not** start
    with "a". Assert that the same drive with `breaker=False` keeps starting with "a"
    whenever the router prefers it. Make "a" cheaper so the router prefers it; the existing
    tests show how to set prices.
  - In `tests/eval/test_outage_scenario.py`, run the outage scenario (its existing fixture
    or command) and assert:
    1. `router_online:breaker_off` exists in `metrics["strategies"]` (read the existing test
       for the metrics key path);
    2. its `wasted_attempts` is **greater than or equal to** that of `router_online`;
    3. two runs with the same seed give identical `metrics.json`.
  Assert direction, never magnitude.

- [ ] **Step 2: Run them and see them fail.**

- [ ] **Step 3: Implement.**
  - **`OnlineArm.__init__`.**
    `use = settings.routing.breaker.enabled if breaker is None else breaker`, then
    `self._breaker = CircuitBreaker(scored.routing.breaker) if use else None`.
  - **`choose`.** After advancing the read clock, compute
    `t = case_read_time(case_index, self._seconds_per_case)` and pass
    `open_circuits=(lambda: self._breaker.open_set(now=t)) if self._breaker else None` to
    `_router_chain`.
  - **`_router_chain`.** When `open_circuits` is given, call
    `rt.select(..., open_circuits=open_circuits())` on each hop.
  - **`observe`.** In the loop where `observe_success` / `observe_error` are applied, also
    call `self._breaker.record_success(slug, now=t)` / `self._breaker.record_failure(slug, now=t)`
    under the same conditions: failures only when `row.counts_unhealth is True`.
  - **Docstring.** Add to `OnlineArm`'s docstring that the breaker sees only this arm's own
    prior attempts, on the case clock. So the leak guard covers it, because it is fed only
    from `observe`.
  - **Runner.** When `settings.routing.breaker.enabled`, add
    `online["router_online:breaker_off"] = OnlineArm(base_cfg, registry, max_attempts, seconds_per_case=seconds_per_case, breaker=False)`.
    Use whatever the base scored config is called there. Also add a strategy note:
    `"router_online:breaker_off is the control: the same online arm with the step-7 circuit breaker disabled."`

- [ ] **Step 4: Run the tests:** `tests/eval` in full. Expected: all pass. If a golden
  `metrics.json` md5 or a snapshot test fails only because a new arm was added, update the
  golden file and **say so in the commit message**. Never loosen an assertion to make it
  pass.
- [ ] **Step 5: Commit** `feat(eval): breaker in router_online plus a breaker_off control arm (B45)`

---

### Task 6: Measure, and write the docs

**Files:**
- Modify: `docs/research/feasible-route-bench.md` (a new §12)
- Modify: `docs/reference/routing-observability.md` (the breaker section, the
  `circuit_open` code, `circuit_bypass`, the metric family count four→five)
- Modify: `docs/backlog/bugs.md` (B45 → ✅ RESOLVED, with a pointer to §12)
- Modify: `.claude/skills/routing-and-gateway/SKILL.md` (a short step-7 breaker paragraph;
  check the file name with `ls .claude/skills/routing-and-gateway/`)
- Modify: `docs/superpowers/specs/2026-09-23-endpoint-circuit-breaker-design.md` §4 (the
  stage is last, not after `prior_failure`, with the reason)
- Modify: `CLAUDE.md` only if it states the `/metrics` family count. It says "four
  `routeforge_*` families", so change that to five.

- [ ] **Step 1: Measure.** For each seed in 0–9 and each spacing in 1, 30 and 300 s per
  case, run:

```bash
LITELLM_LOCAL_MODEL_COST_MAP=True SP_PII_DETECTION=regex ../routeForge/.venv/bin/python \
  scripts/feasible_bench.py --suite eval/suites/outage.jsonl \
  --endpoints eval/endpoints/outage-sim.yaml --variants eval/variants/b45.yaml \
  --seed $SEED --seconds-per-case $SPC --out /tmp/claude-breaker/s$SEED-$SPC
```

  Then, from each `metrics.json`, tabulate the mean over seeds, per spacing, of these
  values for `router_online:default` (breaker on) and `router_online:breaker_off`:
  `wasted_attempts`, `mean_chosen_cost`, latency regret and pass rate. Also record the
  first-attempt sequence (from `choices.jsonl`) for seed 0 at 30 s. Write the output
  outside the repo.

- [ ] **Step 2: Write §12** in the research doc, following the style of §9/§11:
  - what was built, and the stage-7 placement with its reason;
  - the measurement table: on vs off, per spacing, with mean and range over seeds;
  - the seed-0 sequence;
  - the limits: simulated, a designed 1000 ms failure, per-replica state;
  - the invariants added, each with the test that pins it;
  - how to reproduce it.
  **Report what was measured, and nothing more.** If the breaker did not reduce wasted
  attempts at some spacing, say so and explain why, for example a cooldown shorter than
  the case spacing makes every case a half-open trial.

- [ ] **Step 3: Update the other docs** listed above.
- [ ] **Step 4: Run** `tests/eval` plus the full suite one last time.
- [ ] **Step 5: Commit** `docs: circuit breaker measurement (research §12), reference and B45 resolved`
