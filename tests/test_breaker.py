"""Step-7 circuit breaker state machine (spec §3). Pure and clock-injected --
every call passes `now`, so nothing sleeps."""
from app.config.schema import BreakerCfg
from app.gateway.breaker import CircuitBreaker
from app.obs import metrics

CFG = BreakerCfg(enabled=True, failure_threshold=3, cooldown_s=30.0,
                 max_cooldown_s=100.0)


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


def test_half_open_admits_every_caller_no_lease():
    """Fix round 1, finding 1: `open_set()` is a pure read now -- once the
    cooldown elapses EVERY caller finds the model admitted (not just a single
    lease-holder), repeatedly, for as long as the circuit stays half-open."""
    b = CircuitBreaker(CFG)
    _trip(b)                                    # opens at t=2, until 32
    assert "m" in b.open_set(now=31.0)          # still mid-cooldown: excluded
    assert "m" not in b.open_set(now=33.0)      # cooldown elapsed: admitted
    assert "m" not in b.open_set(now=33.1)      # admitted again -- no lease consumed
    assert "m" not in b.open_set(now=50.0)      # still admitted, no result reported yet
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
    assert "m" not in b.open_set(now=95.0)      # half-open again, admitted
    b.record_failure("m", now=96.0)             # cooldown 120 -> capped 100 -> until 196
    assert "m" in b.open_set(now=195.0)
    assert "m" not in b.open_set(now=197.0)


def test_concurrent_callers_all_admitted_while_still_dead():
    """The accepted cost the module docstring documents: while half-open,
    every concurrent caller may pick the model and attempt it, not just one --
    there is no single-trial gate any more."""
    b = CircuitBreaker(CFG)
    _trip(b)                                    # opens at t=2, until 32
    for t in (33.0, 33.0, 33.0, 40.0, 50.0):
        assert "m" not in b.open_set(now=t)


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


# --- Fix round 1 (review findings 1-4): mid-cooldown results must not act,
# evidence (post-cooldown) results act regardless of a lease, and a
# re-granted expired lease is not a fresh open->half_open transition. ---

def test_success_mid_cooldown_does_not_close():
    """Finding 1 repro: a straggler success reported while still inside the
    cooldown window must NOT close the circuit."""
    b = CircuitBreaker(CFG)
    _trip(b)                                    # opens at t=2, until 32
    b.record_success("m", now=10.0)             # still mid-cooldown
    assert b.state("m", now=10.5).state == "open"
    assert b.open_set(now=10.5) == frozenset({"m"})


def test_failure_mid_cooldown_does_not_change_cooldown_or_count():
    """Finding 3: a failure reported mid-cooldown must not touch open_until
    or consecutive_failures -- that count stays the value that tripped it."""
    b = CircuitBreaker(CFG)
    _trip(b)                                    # opens at t=2, until 32, failures=3
    b.record_failure("m", now=10.0)             # still mid-cooldown
    st = b.state("m", now=10.5)
    assert st.state == "open"
    assert st.open_until == 32.0
    assert st.consecutive_failures == 3


def test_success_after_cooldown_closes_without_lease():
    """Half-open is evidence-based: a success reported once the cooldown has
    elapsed closes the circuit even if the caller never called open_set()
    to take the lease first."""
    b = CircuitBreaker(CFG)
    _trip(b)                                    # opens at t=2, until 32
    b.record_success("m", now=32.5)             # no open_set() call first
    assert b.state("m", now=32.5).state == "closed"
    assert b.open_set(now=32.6) == frozenset()


def test_failure_after_cooldown_reopens_without_lease():
    """Symmetric to the above: a failure reported once the cooldown has
    elapsed reopens the circuit (cooldown doubled) even without a lease."""
    b = CircuitBreaker(CFG)
    _trip(b)                                    # opens at t=2, until 32, cooldown 30
    b.record_failure("m", now=32.5)             # no open_set() call first
    st = b.state("m", now=32.5)
    assert st.state == "open"
    assert st.open_until == 92.5                # 32.5 + 60 (doubled)
    assert "m" in b.open_set(now=92.0)
    assert "m" not in b.open_set(now=93.0)


def test_half_open_metric_emitted_exactly_once_per_open_period(monkeypatch):
    """Finding 4 (rewritten for the no-lease design): repeated reads of
    `open_set()` after the cooldown has elapsed must not re-emit the
    open->half_open transition -- it is logged/metered exactly once per open
    period via the one-shot `half_open_logged` dedup flag, not once per
    caller."""
    calls: list[str] = []
    monkeypatch.setattr(metrics, "observe_breaker_transition",
                        lambda to_state: calls.append(to_state))
    b = CircuitBreaker(CFG)
    _trip(b)                                    # opens at t=2, until 32 -> "open"
    b.open_set(now=33.0)                        # first post-cooldown read -> "half_open"
    b.open_set(now=40.0)                        # every later caller: no re-emit
    b.open_set(now=50.0)
    assert calls.count("half_open") == 1


def test_half_open_metric_also_dedupes_across_record_calls(monkeypatch):
    """The same dedup applies when `record_success`/`record_failure`, not
    `open_set()`, is the first caller to observe the elapsed cooldown."""
    calls: list[str] = []
    monkeypatch.setattr(metrics, "observe_breaker_transition",
                        lambda to_state: calls.append(to_state))
    b = CircuitBreaker(CFG)
    _trip(b)                                    # opens at t=2, until 32 -> "open"
    b.record_success("m", now=33.0)             # first post-cooldown observer -> "half_open" then "closed"
    assert calls.count("half_open") == 1
    assert calls.count("closed") == 1
