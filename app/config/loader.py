import hashlib
import json
import os
import re
from pathlib import Path

import yaml

from app.config.schema import (
    BypassCfg, CCRCfg, ContextOptimizationCfg, LangfuseCfg,
    ObservabilityCfg, PolicyCfg, PricingCfg, RoutingCfg, ServingCfg, Settings,
)

_ENV_PAT = re.compile(r"\$\{([A-Z_][A-Z0-9_]*)(?::-([^}]*))?\}")


def _expand_env(value: str) -> str:
    def repl(m: re.Match) -> str:
        return os.environ.get(m.group(1), m.group(2) or "")
    return _ENV_PAT.sub(repl, value)


def _read(config_dir: str, name: str) -> dict:
    return yaml.safe_load(Path(config_dir, name).read_text()) or {}


def _policy_version(routing_raw: dict, pricing_raw: dict) -> str:
    """First 12 hex of sha256 over the canonicalized routing + pricing config.
    Sorted-key JSON makes it stable across boots for identical config and
    change only when a scoring/pool/price value changes."""
    blob = json.dumps({"routing": routing_raw, "pricing": pricing_raw},
                      sort_keys=True, default=str)
    return hashlib.sha256(blob.encode()).hexdigest()[:12]


def load_settings(config_dir: str = "config") -> Settings:
    serving_raw = _read(config_dir, "serving.yaml")
    routing_raw = _read(config_dir, "routing.yaml")
    policy_raw = _read(config_dir, "policy.yaml")
    pricing_raw = _read(config_dir, "pricing.yaml")

    lf_raw = serving_raw["observability"]["langfuse"]
    observability = ObservabilityCfg(
        langfuse=LangfuseCfg(
            enabled=lf_raw["enabled"],
            host=_expand_env(str(lf_raw["host"])),
            redact_on_export=lf_raw["redact_on_export"],
            # SP_CAPTURE_IO overrides observability.langfuse.capture_io — same
            # env-override pattern as SP_PII_DETECTION below, so eval capture
            # can be flipped per-deploy without editing the YAML.
            capture_io=os.environ.get(
                "SP_CAPTURE_IO",
                _expand_env(str(lf_raw.get("capture_io", "off")))).strip().lower(),
            project_id=os.environ.get(
                "LANGFUSE_PROJECT_ID",
                _expand_env(str(lf_raw.get("project_id", "demo-project")))),
            public_host=(os.environ.get("LANGFUSE_PUBLIC_URL")
                         or (_expand_env(str(lf_raw["public_host"]))
                             if lf_raw.get("public_host") else None) or None),
            public_key=os.environ.get("LANGFUSE_PUBLIC_KEY"),
            secret_key=os.environ.get("LANGFUSE_SECRET_KEY"),
        )
    )

    co_raw = serving_raw["context_optimization"]
    # SP_CONTEXT_OPT=on/off overrides context_optimization.enabled — same
    # env-override pattern as SP_PII_DETECTION/SP_CAPTURE_IO above. Used by
    # scripts/smoke-compression.sh to toggle Headroom on/off across two boots
    # without editing config/serving.yaml.
    _co_opt_env = os.environ.get("SP_CONTEXT_OPT")
    co_enabled = (
        co_raw["enabled"] if _co_opt_env is None
        else _co_opt_env.strip().lower() in ("on", "true", "1", "yes")
    )
    # SP_ALLOW_APP_PII_DOWNGRADE overrides policy.allow_app_pii_downgrade — same
    # env-override pattern as SP_PII_DETECTION/SP_CONTEXT_OPT above. Defaults to
    # true (matches config/serving.yaml) if absent from both env and YAML.
    _allow_pii_dg_env = os.environ.get("SP_ALLOW_APP_PII_DOWNGRADE")
    allow_app_pii_downgrade = (
        bool(serving_raw["policy"].get("allow_app_pii_downgrade", True))
        if _allow_pii_dg_env is None
        else _allow_pii_dg_env.strip().lower() in ("on", "true", "1", "yes")
    )
    serving = ServingCfg(
        providers=serving_raw["providers"],
        pii_detection=os.environ.get(
            "SP_PII_DETECTION", serving_raw["policy"]["pii_detection"]),
        allow_app_pii_downgrade=allow_app_pii_downgrade,
        reject_stream_requests=serving_raw["server"]["reject_stream_requests"],
        context_optimization=ContextOptimizationCfg(
            enabled=co_enabled,
            mode=co_raw["mode"],
            min_tokens=co_raw["min_tokens"],
            bypass_on=BypassCfg(**co_raw["bypass_on"]),
            ccr=CCRCfg(**co_raw["ccr"]),
            cache_alignment=co_raw["cache_alignment"],
        ),
        observability=observability,
    )
    routing = RoutingCfg(**routing_raw)
    policy = PolicyCfg(**policy_raw)
    pricing = PricingCfg(**pricing_raw)
    return Settings(serving=serving, routing=routing, policy=policy, pricing=pricing,
                    routing_policy_version=_policy_version(routing_raw, pricing_raw))
