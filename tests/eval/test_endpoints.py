"""Tests for `app.eval.endpoints` (Task 2, FeasibleRouteBench).

Covers the §4.2 endpoint-file loader, `harness_settings`, and the scratch
`build_registry`. See docs/superpowers/specs/2026-09-21-feasible-route-bench-design.md
§3/§4.2.
"""
from __future__ import annotations

import asyncio
import copy

import pytest
import yaml

from app.apps.policy import AppPolicyView
from app.config.loader import load_settings
from app.eval.endpoints import (
    EndpointFileError,
    build_registry,
    harness_settings,
    load_endpoints,
)
from app.policy.feasible import feasible_set

BASE_SETTINGS = load_settings()
TIERS = BASE_SETTINGS.routing.tiers


def _write(tmp_path, doc: dict) -> str:
    path = tmp_path / "endpoints.yaml"
    path.write_text(yaml.safe_dump(doc))
    return str(path)


def _sim_entry(**kw) -> dict:
    base = {
        "slug": "local-llama",
        "provider": "onprem",
        "tier": "cheap",
        "price": {"input": 0.08, "output": 0.08},
        "simulated": {
            "quality": {"default": 0.9},
            "latency_ms": {"mean": 900, "jitter": 100},
        },
    }
    base.update(kw)
    return base


def _live_entry(**kw) -> dict:
    base = {
        "slug": "qwen3.8-27b",
        "provider": "onprem",
        "tier": "cheap",
        "price": {"input": 0, "output": 0},
        "live": {"endpoint": "http://spark:8000/v1", "api_key_env": "SP_ONPREM_API_KEY"},
    }
    base.update(kw)
    return base


# --------------------------------------------------------------------------
# load_endpoints -- valid loads
# --------------------------------------------------------------------------

class TestLoadEndpointsValid:
    def test_loads_simulated_entries_and_returns_stable_hash(self, tmp_path):
        path = _write(tmp_path, {"endpoints": [_sim_entry()]})
        specs, digest = load_endpoints(path, live=False, tiers=TIERS)
        assert [s.slug for s in specs] == ["local-llama"]
        assert isinstance(digest, str) and len(digest) == 64

    def test_hash_is_sha256_of_raw_bytes(self, tmp_path):
        import hashlib
        path = _write(tmp_path, {"endpoints": [_sim_entry()]})
        _, digest = load_endpoints(path, live=False, tiers=TIERS)
        with open(path, "rb") as f:
            expected = hashlib.sha256(f.read()).hexdigest()
        assert digest == expected

    def test_loads_live_entries_when_live_true(self, tmp_path):
        path = _write(tmp_path, {"endpoints": [_live_entry()]})
        specs, _ = load_endpoints(path, live=True, tiers=TIERS)
        assert specs[0].live.endpoint == "http://spark:8000/v1"

    def test_price_cached_input_defaults_none(self, tmp_path):
        path = _write(tmp_path, {"endpoints": [_sim_entry()]})
        specs, _ = load_endpoints(path, live=False, tiers=TIERS)
        assert specs[0].price.cached_input is None


# --------------------------------------------------------------------------
# load_endpoints -- errors
# --------------------------------------------------------------------------

class TestLoadEndpointsErrors:
    def test_both_simulated_and_live_raises(self, tmp_path):
        entry = _sim_entry(live={"endpoint": "http://x/v1"})
        path = _write(tmp_path, {"endpoints": [entry]})
        with pytest.raises(EndpointFileError):
            load_endpoints(path, live=True, tiers=TIERS)

    def test_neither_simulated_nor_live_raises(self, tmp_path):
        entry = {"slug": "bare", "provider": "onprem", "tier": "cheap",
                  "price": {"input": 0.1, "output": 0.1}}
        path = _write(tmp_path, {"endpoints": [entry]})
        with pytest.raises(EndpointFileError):
            load_endpoints(path, live=False, tiers=TIERS)

    def test_live_entry_without_live_flag_raises(self, tmp_path):
        path = _write(tmp_path, {"endpoints": [_live_entry()]})
        with pytest.raises(EndpointFileError):
            load_endpoints(path, live=False, tiers=TIERS)

    def test_simulated_entry_in_live_run_raises(self, tmp_path):
        path = _write(tmp_path, {"endpoints": [_sim_entry()]})
        with pytest.raises(EndpointFileError):
            load_endpoints(path, live=True, tiers=TIERS)

    def test_tier_not_canonical_raises(self, tmp_path):
        entry = _sim_entry(tier="strong")
        path = _write(tmp_path, {"endpoints": [entry]})
        with pytest.raises(EndpointFileError):
            load_endpoints(path, live=False, tiers=TIERS)

    def test_duplicate_slug_raises(self, tmp_path):
        path = _write(tmp_path, {"endpoints": [_sim_entry(), _sim_entry()]})
        with pytest.raises(EndpointFileError):
            load_endpoints(path, live=False, tiers=TIERS)

    def test_missing_endpoints_key_raises(self, tmp_path):
        path = _write(tmp_path, {"not_endpoints": []})
        with pytest.raises(EndpointFileError):
            load_endpoints(path, live=False, tiers=TIERS)

    def test_non_mapping_entry_raises(self, tmp_path):
        path = _write(tmp_path, {"endpoints": ["not-a-mapping"]})
        with pytest.raises(EndpointFileError):
            load_endpoints(path, live=False, tiers=TIERS)

    def test_missing_required_field_raises(self, tmp_path):
        entry = _sim_entry()
        del entry["price"]
        path = _write(tmp_path, {"endpoints": [entry]})
        with pytest.raises(EndpointFileError):
            load_endpoints(path, live=False, tiers=TIERS)

    def test_sim_quality_missing_default_raises(self, tmp_path):
        entry = _sim_entry(simulated={
            "quality": {"faq": 0.9},
            "latency_ms": {"mean": 900},
        })
        path = _write(tmp_path, {"endpoints": [entry]})
        with pytest.raises(EndpointFileError):
            load_endpoints(path, live=False, tiers=TIERS)

    def test_sim_quality_out_of_range_raises(self, tmp_path):
        entry = _sim_entry(simulated={
            "quality": {"default": 1.5},
            "latency_ms": {"mean": 900},
        })
        path = _write(tmp_path, {"endpoints": [entry]})
        with pytest.raises(EndpointFileError):
            load_endpoints(path, live=False, tiers=TIERS)

    @pytest.mark.parametrize("bad_env", [
        "sk-abcdef1234567890",                     # contains sk-
        "sp onprem api key",                        # whitespace
        "abcdefghijklmnopqrstuvwxyz012345",          # >=32 chars, lowercase
    ])
    def test_key_looking_api_key_env_raises(self, tmp_path, bad_env):
        entry = _live_entry(live={"endpoint": "http://spark:8000/v1", "api_key_env": bad_env})
        path = _write(tmp_path, {"endpoints": [entry]})
        with pytest.raises(EndpointFileError):
            load_endpoints(path, live=True, tiers=TIERS)

    def test_normal_env_name_is_accepted(self, tmp_path):
        path = _write(tmp_path, {"endpoints": [_live_entry()]})
        specs, _ = load_endpoints(path, live=True, tiers=TIERS)
        assert specs[0].live.api_key_env == "SP_ONPREM_API_KEY"


# --------------------------------------------------------------------------
# harness_settings
# --------------------------------------------------------------------------

class TestHarnessSettings:
    def test_enables_named_provider(self, tmp_path):
        path = _write(tmp_path, {"endpoints": [_sim_entry(provider="azure")]})
        specs, _ = load_endpoints(path, live=False, tiers=TIERS)
        assert BASE_SETTINGS.serving.providers["azure"].enabled is False
        settings = harness_settings(BASE_SETTINGS, specs)
        assert settings.serving.providers["azure"].enabled is True

    def test_does_not_mutate_base(self, tmp_path):
        path = _write(tmp_path, {"endpoints": [_sim_entry(provider="azure")]})
        specs, _ = load_endpoints(path, live=False, tiers=TIERS)
        harness_settings(BASE_SETTINGS, specs)
        assert BASE_SETTINGS.serving.providers["azure"].enabled is False

    def test_disables_semantic_cache_and_memory(self, tmp_path):
        base = copy.deepcopy(BASE_SETTINGS)
        base.serving.semantic_cache.enabled = True
        base.serving.memory.enabled = True
        path = _write(tmp_path, {"endpoints": [_sim_entry()]})
        specs, _ = load_endpoints(path, live=False, tiers=TIERS)
        settings = harness_settings(base, specs)
        assert settings.serving.semantic_cache.enabled is False
        assert settings.serving.memory.enabled is False
        # base itself untouched
        assert base.serving.semantic_cache.enabled is True
        assert base.serving.memory.enabled is True

    def test_static_yaml_pool_is_excluded(self, tmp_path):
        """Enabling `azure` for a file endpoint must not pull the
        deployment's own YAML azure models into the harness pool."""
        assert BASE_SETTINGS.routing.model_pool  # the stock pool is non-empty
        path = _write(tmp_path, {"endpoints": [
            _sim_entry(slug="azure-us", provider="azure", tier="mid")]})
        specs, _ = load_endpoints(path, live=False, tiers=TIERS)
        settings = harness_settings(BASE_SETTINGS, specs)
        assert settings.routing.model_pool == []
        assert BASE_SETTINGS.routing.model_pool  # base untouched

        async def _run():
            registry, db = await build_registry(settings, specs)
            try:
                return sorted(c.name for c in registry.effective_pool())
            finally:
                await db.dispose()

        assert asyncio.run(_run()) == ["azure-us"]

    def test_confidence_escalation_is_off_and_base_untouched(self, tmp_path):
        """The sweep is one generate call per (case, endpoint): with a pin,
        escalation would re-call the same endpoint."""
        assert BASE_SETTINGS.routing.max_escalations > 0  # stock config escalates
        path = _write(tmp_path, {"endpoints": [_sim_entry()]})
        specs, _ = load_endpoints(path, live=False, tiers=TIERS)
        settings = harness_settings(BASE_SETTINGS, specs)
        assert settings.routing.max_escalations == 0
        assert BASE_SETTINGS.routing.max_escalations > 0

    def test_unknown_provider_raises_and_names_known_providers(self, tmp_path):
        path = _write(tmp_path, {"endpoints": [_sim_entry(provider="totally-unknown-cloud")]})
        specs, _ = load_endpoints(path, live=False, tiers=TIERS)
        with pytest.raises(EndpointFileError) as exc_info:
            harness_settings(BASE_SETTINGS, specs)
        message = str(exc_info.value)
        assert "totally-unknown-cloud" in message
        assert "onprem" in message


# --------------------------------------------------------------------------
# build_registry
# --------------------------------------------------------------------------

class TestBuildRegistry:
    def test_effective_pool_and_price_for_match_declared_specs(self, tmp_path):
        doc = {"endpoints": [
            _sim_entry(slug="local-llama", provider="onprem", tier="cheap",
                       region="us", prompt_retention="none",
                       accepts_raw_sensitive=True),
        ]}
        path = _write(tmp_path, doc)
        specs, _ = load_endpoints(path, live=False, tiers=TIERS)
        settings = harness_settings(BASE_SETTINGS, specs)

        async def _run():
            registry, db = await build_registry(settings, specs)
            try:
                pool = {c.name: c for c in registry.effective_pool()}
                assert "local-llama" in pool
                cand = pool["local-llama"]
                assert cand.provider == "onprem"
                assert cand.tier == "cheap"
                assert cand.region == "us"

                price = registry.price_for("local-llama")
                assert price.input == 0.08
                assert price.output == 0.08
            finally:
                await db.dispose()

        asyncio.run(_run())

    def test_azure_endpoint_survives_feasible_set_when_enabled(self, tmp_path):
        doc = {"endpoints": [
            _sim_entry(slug="azure-us", provider="azure", tier="mid"),
        ]}
        path = _write(tmp_path, doc)
        specs, _ = load_endpoints(path, live=False, tiers=TIERS)
        settings = harness_settings(BASE_SETTINGS, specs)

        async def _run():
            registry, db = await build_registry(settings, specs)
            try:
                fs_any = feasible_set(
                    settings, registry, AppPolicyView(), tenant_id="t")
                assert "azure-us" in {c.name for c in fs_any.eligible}

                fs_onprem_only = feasible_set(
                    settings, registry, AppPolicyView(residency="onprem_only"),
                    tenant_id="t")
                assert "azure-us" not in {c.name for c in fs_onprem_only.eligible}
            finally:
                await db.dispose()

        asyncio.run(_run())

    def test_raises_when_registry_refresh_loses_rows(self, tmp_path, monkeypatch):
        """`ModelRegistry.refresh` swallows exceptions and keeps
        last-known-good (registry.py) -- simulate that by making refresh a
        no-op, and assert `build_registry` refuses to hand back a registry
        that silently dropped the rows it just wrote."""
        from app.apps.registry import ModelRegistry

        async def _noop_refresh(self, db):
            return None

        monkeypatch.setattr(ModelRegistry, "refresh", _noop_refresh)

        path = _write(tmp_path, {"endpoints": [_sim_entry()]})
        specs, _ = load_endpoints(path, live=False, tiers=TIERS)
        settings = harness_settings(BASE_SETTINGS, specs)

        async def _run():
            with pytest.raises(EndpointFileError):
                await build_registry(settings, specs)

        asyncio.run(_run())
