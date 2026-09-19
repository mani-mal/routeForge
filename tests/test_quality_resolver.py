from app.config.loader import load_settings
from app.routing.quality import QualityResolver, QualityScore, QUALITY_SOURCES
from tests._litellm_fake import make_test_registry


def _settings():
    return load_settings()


def _registry(s):
    return make_test_registry(s)


def _with_published_quality(s, slug, quality):
    """Give a static pool entry a published `quality`, through the SETTINGS the
    registry reads.

    `ModelRegistry.effective_pool()` returns `model_copy` results (it resolves
    the P1b data-handling declarations onto each candidate), so the older
    `reg.effective_pool()[0].quality = 0.61` mutated a throwaway copy and the
    resolver never saw it. Setting it on `routing.model_pool` -- the thing the
    pool is BUILT from -- expresses the same intent and survives the copy.
    """
    pool = [c.model_copy(update={"quality": quality}) if c.name == slug else c
            for c in s.routing.model_pool]
    return s.model_copy(update={"routing": s.routing.model_copy(
        update={"model_pool": pool})})


def _with_azure_enabled(s):
    # B10: azure is disabled by default in serving.yaml, so effective_pool()
    # now omits azure-gpt-mid entirely unless the provider is enabled -- these
    # tests are about the QualityResolver's published/override rungs, not
    # about provider gating, so enable azure to keep azure-gpt-mid a real
    # pool member.
    prov = dict(s.serving.providers)
    prov["azure"] = prov["azure"].model_copy(update={"enabled": True})
    return s.model_copy(update={"serving": s.serving.model_copy(update={"providers": prov})})


def test_tier_default_rung_for_model_without_published_quality():
    s = _settings()  # shipped config: no per-model quality set
    r = QualityResolver(s, registry=_registry(s))
    q = r.resolve("local-8b")  # cheap tier
    assert q == QualityScore(0.5, "tier_default")


def test_published_rung_when_pool_entry_has_quality():
    s = _with_published_quality(
        _with_azure_enabled(_settings()), "azure-gpt-mid", 0.61)
    reg = _registry(s)
    r = QualityResolver(s, registry=reg)
    q = r.resolve("azure-gpt-mid")
    assert q == QualityScore(0.61, "published")


def test_override_rung_beats_published_and_tier_default():
    s = _with_published_quality(
        _with_azure_enabled(_settings()), "azure-gpt-mid", 0.61)
    reg = _registry(s)
    r = QualityResolver(s, overrides={"azure-gpt-mid": QualityScore(0.9, "customer")},
                        registry=reg)
    assert r.resolve("azure-gpt-mid") == QualityScore(0.9, "customer")


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
    reg = _registry(s)
    r = QualityResolver(s, registry=reg)
    assert r.resolve("local-8b", task_class="coding") == r.resolve("local-8b")


def test_quality_sources_are_the_canonical_four():
    assert QUALITY_SOURCES == ("customer", "calibrated", "published", "tier_default")


def test_registry_aware_resolver_covers_onboarded_onprem_tier():
    """Regression guard for the model-registry-onboarding purge: an onboarded
    on-prem model never appears in `settings.routing.model_pool`, so a
    registry-blind resolver would silently fall through to the flat
    tier_default FLOOR (0.5) for every on-prem model regardless of its real
    tier -- e.g. local-32b (mid tier) would incorrectly score as if cheap.
    A registry-aware resolver must use the onboarded row's actual tier."""
    s = _settings()
    reg = _registry(s)
    # sanity: registry has the seeded mid-tier local-32b
    assert any(c.name == "local-32b" and c.tier == "mid" for c in reg.effective_pool())
    r = QualityResolver(s, registry=reg)
    assert r.resolve("local-32b") == QualityScore(s.routing.tier_quality["mid"], "tier_default")
