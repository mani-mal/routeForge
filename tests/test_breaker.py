"""Step-7 circuit breaker state machine (spec §3). Pure and clock-injected --
every call passes `now`, so nothing sleeps."""
from app.config.schema import BreakerCfg
from app.gateway.breaker import CircuitBreaker
from app.obs import metrics

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


def test_expired_lease_regrant_emits_no_duplicate_half_open_metric(monkeypatch):
    """Finding 4: re-granting an expired trial lease within the same open
    period is not a new open->half_open transition, so it must not re-emit
    the metric/log."""
    calls: list[str] = []
    monkeypatch.setattr(metrics, "observe_breaker_transition",
                        lambda to_state: calls.append(to_state))
    b = CircuitBreaker(CFG)
    _trip(b)                                    # opens at t=2, until 32 -> "open"
    b.open_set(now=33.0)                        # first grant -> "half_open"
    b.open_set(now=94.0)                        # lease expired (33+60=93) -> re-grant only
    assert calls.count("half_open") == 1
