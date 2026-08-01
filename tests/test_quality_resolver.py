from app.config.loader import load_settings
from app.routing.quality import QualityResolver, QualityScore, QUALITY_SOURCES


def _settings():
    return load_settings()


def test_tier_default_rung_for_model_without_published_quality():
    s = _settings()  # shipped config: no per-model quality set
    r = QualityResolver(s)
    q = r.resolve("local-8b")  # cheap tier
    assert q == QualityScore(0.5, "tier_default")


def test_published_rung_when_pool_entry_has_quality():
    s = _settings()
    s.routing.model_pool[0].quality = 0.61  # local-8b published
    r = QualityResolver(s)
    q = r.resolve("local-8b")
    assert q == QualityScore(0.61, "published")


def test_override_rung_beats_published_and_tier_default():
    s = _settings()
    s.routing.model_pool[0].quality = 0.61
    r = QualityResolver(s, overrides={"local-8b": QualityScore(0.9, "customer")})
    assert r.resolve("local-8b") == QualityScore(0.9, "customer")


def test_calibrated_override_rung():
    s = _settings()
    r = QualityResolver(s, overrides={"local-8b": QualityScore(0.72, "calibrated")})
    assert r.resolve("local-8b") == QualityScore(0.72, "calibrated")


def test_unknown_model_never_raises_returns_tier_default_floor():
    s = _settings()
    r = QualityResolver(s)
    q = r.resolve("some-custom-model-not-in-pool")
    assert q.source == "tier_default"
    assert 0.0 <= q.score <= 1.0


def test_task_class_is_accepted_and_ignored():
    s = _settings()
    r = QualityResolver(s)
    assert r.resolve("local-8b", task_class="coding") == r.resolve("local-8b")


def test_quality_sources_are_the_canonical_four():
    assert QUALITY_SOURCES == ("customer", "calibrated", "published", "tier_default")
