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
