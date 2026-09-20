import pytest

from app.config.schema import SignalsCfg
from app.routing.signals import SeedRow, SignalStore


def _store(**kw) -> SignalStore:
    cfg = SignalsCfg(min_samples=3, half_life_s=100.0, staleness_s=1000.0, **kw)
    return SignalStore(cfg)


def test_an_unmeasured_model_reports_the_tier_default_not_a_guess():
    s = _store()
    v = s.latency("m", "cheap", now=0.0)
    assert (v.value, v.source, v.samples) == (200.0, "tier_default", 0)


def test_an_unmeasured_model_reports_neutral_availability():
    """1.0 is today's hardcoded constant -- the identity property depends on it."""
    s = _store()
    v = s.availability("m", now=0.0)
    assert (v.value, v.source, v.samples) == (1.0, "neutral", 0)


def test_below_min_samples_still_reports_the_true_sample_count():
    s = _store()
    s.observe_success("m", 500.0, now=0.0)
    v = s.latency("m", "cheap", now=1.0)
    assert v.source == "tier_default"
    assert v.samples == 1        # honest about how little it knows


def test_latency_becomes_measured_at_min_samples():
    s = _store()
    for i in range(3):
        s.observe_success("m", 400.0, now=float(i))
    v = s.latency("m", "cheap", now=3.0)
    assert v.source == "measured"
    assert v.value == pytest.approx(400.0)
    assert v.samples == 3


def test_a_stale_latency_falls_back_to_the_tier_default():
    """A measurement from two days ago is not a measurement."""
    s = _store()
    for i in range(3):
        s.observe_success("m", 400.0, now=float(i))
    v = s.latency("m", "cheap", now=10_000.0)
    assert v.source == "tier_default"
    assert v.value == 200.0


def test_availability_drops_on_errors():
    s = _store()
    for i in range(6):
        s.observe_error("m", now=float(i))
    v = s.availability("m", now=6.0)
    assert v.source == "measured"
    assert v.value < 0.5


def test_availability_decays_back_toward_neutral_on_WALL_CLOCK_with_no_traffic():
    """The starvation trap: down-weighting plus failover starves a model of the
    traffic it would need to prove it recovered. Time decay, not sample decay,
    is what lets it earn a retry."""
    s = _store()
    for i in range(6):
        s.observe_error("m", now=float(i))
    sick = s.availability("m", now=6.0).value
    later = s.availability("m", now=6.0 + 300.0).value   # 3 half-lives
    assert later > sick
    assert later == pytest.approx(1.0 - (1.0 - sick) * 0.125, rel=1e-6)


def test_availability_never_exceeds_neutral():
    s = _store()
    for i in range(10):
        s.observe_success("m", 100.0, now=float(i))
    assert s.availability("m", now=10_000.0).value <= 1.0


def test_disabled_reports_defaults_and_says_so():
    s = SignalStore(SignalsCfg(enabled=False, min_samples=1))
    for i in range(5):
        s.observe_success("m", 999.0, now=float(i))
    v = s.latency("m", "frontier", now=5.0)
    assert v.value == pytest.approx(1000.0), "tier default"
    assert v.source == "disabled"
    assert s.availability("m", now=5.0).source == "disabled"
    assert s.availability("m", now=5.0).value == 1.0


def test_seeding_makes_a_model_measured_without_live_traffic():
    s = _store()
    s.seed([SeedRow(model="m", avg_latency_ms=350.0, error_rate=0.1, samples=42,
                    last_ts=0.0)])
    lat = s.latency("m", "cheap", now=1.0)
    # now=0.0 (== SeedRow.last_ts): "without live traffic" means zero elapsed
    # wall-clock time, so availability's continuous decay-toward-neutral (see
    # `_decayed_availability`; unlike latency, it has no staleness cliff -- it
    # drifts from the very first tick) does not nudge the value off 0.9.
    avail = s.availability("m", now=0.0)
    assert (lat.source, lat.value, lat.samples) == ("measured", 350.0, 42)
    assert avail.source == "measured"
    assert avail.value == pytest.approx(0.9)


def test_seeded_last_ts_is_actually_threaded_not_defaulted():
    """Closes a gap the Task 4 review flagged: the seeding test above uses
    last_ts=0.0, which is indistinguishable from _ModelState's own default of
    0.0. Seed at a real, non-zero monotonic time and prove staleness is
    measured from THAT time, not from zero (which this store's staleness_s of
    1000.0 would already have blown past at now=5000.0 if last_ts silently
    stayed at the struct default)."""
    s = _store()
    s.seed([SeedRow(model="m", avg_latency_ms=350.0, error_rate=0.0, samples=10,
                    last_ts=5000.0)])
    fresh = s.latency("m", "cheap", now=5000.0)      # no elapsed time since seed
    assert fresh.source == "measured"
    stale = s.latency("m", "cheap", now=5000.0 + 1000.0 + 1.0)   # past staleness_s
    assert stale.source == "tier_default"


def test_an_unknown_tier_falls_back_without_raising():
    s = _store()
    assert s.latency("m", "no-such-tier", now=0.0).source == "tier_default"


def test_snapshot_exposes_per_model_state():
    s = _store()
    s.observe_success("m", 100.0, now=0.0)
    snap = s.snapshot()
    assert "m" in snap and snap["m"]["latency_n"] == 1
