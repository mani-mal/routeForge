from app.config.loader import load_settings


def test_loads_real_config():
    s = load_settings()
    assert s.routing.tiers == ["cheap", "mid", "frontier"]
    assert s.policy.default_sensitivity == "public"
    assert s.pricing.baseline_model == "bedrock-frontier"
    # No built-in on-prem pool member any more — on-prem models are onboarded
    # at runtime (ModelRegistry), never a static YAML entry.
    assert {c.provider for c in s.routing.model_pool} == {"azure", "bedrock"}
    assert not hasattr(s.serving, "inference_backend")


def test_routing_config_exposes_quality_fields():
    from app.config.loader import load_settings
    s = load_settings()
    # tier_quality covers every canonical tier, promoted from the old hardcoded _Q
    assert set(s.routing.tier_quality) == set(s.routing.tiers)
    assert s.routing.tier_quality["cheap"] == 0.5
    assert s.routing.tier_quality["frontier"] == 1.0
    # per-model published quality is optional; shipped config leaves it unset
    # (tier-default regression — populating it is an onboarding step)
    assert all(c.quality is None for c in s.routing.model_pool)


# --- policy_mode: deployment default + ceiling -------------------------------

def test_policy_mode_defaults_are_enforced_and_ceiling_open():
    # Shipped config/serving.yaml: privacy-first stays the default posture, and
    # the ceiling is open so an operator can opt individual apps out.
    s = load_settings()
    assert s.serving.default_policy_mode == "enforced"
    assert s.serving.allow_app_passthrough is True


def test_sp_default_policy_mode_env_override(monkeypatch):
    # Same env-override pattern as SP_PII_DETECTION: an install where no
    # customer wants PII checks flips the posture per-deploy, not per-YAML-edit.
    monkeypatch.delenv("SP_DEFAULT_POLICY_MODE", raising=False)
    assert load_settings().serving.default_policy_mode == "enforced"
    monkeypatch.setenv("SP_DEFAULT_POLICY_MODE", "passthrough")
    assert load_settings().serving.default_policy_mode == "passthrough"


def test_sp_allow_app_passthrough_env_override(monkeypatch):
    # Boolean parsed exactly like SP_ALLOW_APP_PII_DOWNGRADE — anything that
    # isn't an affirmative spelling closes the ceiling (fail-closed reading).
    monkeypatch.delenv("SP_ALLOW_APP_PASSTHROUGH", raising=False)
    assert load_settings().serving.allow_app_passthrough is True
    for v in ("on", "On", "TRUE", "1", "yes"):
        monkeypatch.setenv("SP_ALLOW_APP_PASSTHROUGH", v)
        assert load_settings().serving.allow_app_passthrough is True, v
    for v in ("off", "false", "0", "no", "garbage", ""):
        monkeypatch.setenv("SP_ALLOW_APP_PASSTHROUGH", v)
        assert load_settings().serving.allow_app_passthrough is False, v


def test_boot_survives_serving_yaml_without_policy_mode_keys(tmp_path, monkeypatch):
    # Upgrade safety, same case as the semantic_cache block: a mounted
    # serving.yaml that predates this feature (DGX ConfigMap, Helm/HPE
    # serving-config) carries neither key. Both must be .get()-with-default, or
    # every existing deployment KeyErrors at boot.
    import pathlib

    import yaml

    monkeypatch.delenv("SP_DEFAULT_POLICY_MODE", raising=False)
    monkeypatch.delenv("SP_ALLOW_APP_PASSTHROUGH", raising=False)
    raw = yaml.safe_load(pathlib.Path("config/serving.yaml").read_text())
    for k in ("default_policy_mode", "allow_app_passthrough"):
        raw["policy"].pop(k, None)
    (tmp_path / "serving.yaml").write_text(yaml.safe_dump(raw))
    for f in ("policy.yaml", "routing.yaml", "pricing.yaml"):
        (tmp_path / f).write_text(pathlib.Path("config", f).read_text())
    s = load_settings(config_dir=str(tmp_path))  # must not raise
    assert s.serving.default_policy_mode == "enforced"
    assert s.serving.allow_app_passthrough is True


# --- P1b: endpoint data-handling declarations (design §2/§7) -----------------

def test_shipped_config_declares_no_data_handling_keys():
    # The shipped YAMLs carry only COMMENTED examples (design §7) -- no
    # provider default and no model_pool entry declares region /
    # prompt_retention / accepts_raw_sensitive. Every field resolves to
    # None on the raw (unresolved) PoolCandidate/ProviderCfg; the upgrade
    # -safety property this pins is that a config with none of the new keys
    # boots and behaves exactly as it did before P1b.
    s = load_settings()
    for pname, pcfg in s.serving.providers.items():
        assert pcfg.region is None, pname
        assert pcfg.prompt_retention is None, pname
        assert pcfg.accepts_raw_sensitive is None, pname
    for c in s.routing.model_pool:
        assert c.region is None, c.name
        assert c.prompt_retention is None, c.name
        assert c.accepts_raw_sensitive is None, c.name
