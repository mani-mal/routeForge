from app.config.loader import load_settings
from app.config.lint import enforce, lint_settings


def test_enabled_provider_without_litellm_prefix_is_a_violation():
    s = load_settings()
    providers = dict(s.serving.providers)
    providers["azure"] = providers["azure"].model_copy(
        update={"enabled": True, "litellm_prefix": None})
    s = s.model_copy(update={"serving": s.serving.model_copy(update={"providers": providers})})
    errs = lint_settings(s)
    assert any("PROVIDER_NO_LITELLM_MAP" in e and "azure" in e for e in errs)


def test_disabled_provider_without_prefix_is_ok():
    s = load_settings()  # azure/bedrock disabled by default, bedrock has prefix anyway
    assert not any("PROVIDER_NO_LITELLM_MAP" in e for e in lint_settings(s))


def test_shipped_config_lints_clean():
    enforce(load_settings())  # must not raise
