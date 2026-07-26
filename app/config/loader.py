import os
import re
from pathlib import Path

import yaml

from app.config.schema import (
    BypassCfg, CCRCfg, ContextOptimizationCfg, DynamoCfg, LangfuseCfg,
    ObservabilityCfg, PolicyCfg, PricingCfg, RoutingCfg, ServingCfg, Settings,
    VllmCfg,
)

_ENV_PAT = re.compile(r"\$\{([A-Z_][A-Z0-9_]*)(?::-([^}]*))?\}")


def _expand_env(value: str) -> str:
    def repl(m: re.Match) -> str:
        return os.environ.get(m.group(1), m.group(2) or "")
    return _ENV_PAT.sub(repl, value)


def _read(config_dir: str, name: str) -> dict:
    return yaml.safe_load(Path(config_dir, name).read_text()) or {}


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
    # pattern as SP_INFERENCE_BACKEND/SP_PII_DETECTION above. Used by
    # scripts/smoke-compression.sh to toggle Headroom on/off across two boots
    # without editing config/serving.yaml.
    _co_opt_env = os.environ.get("SP_CONTEXT_OPT")
    co_enabled = (
        co_raw["enabled"] if _co_opt_env is None
        else _co_opt_env.strip().lower() in ("on", "true", "1", "yes")
    )
    serving = ServingCfg(
        inference_backend=os.environ.get(
            "SP_INFERENCE_BACKEND", serving_raw["inference_backend"]),
        allow_backend_fallback=serving_raw["allow_backend_fallback"],
        providers=serving_raw["providers"],
        pii_detection=os.environ.get(
            "SP_PII_DETECTION", serving_raw["policy"]["pii_detection"]),
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
        vllm=VllmCfg(**serving_raw["vllm"]),
        dynamo=DynamoCfg(**serving_raw["dynamo"]),
    )
    routing = RoutingCfg(**routing_raw)
    policy = PolicyCfg(**policy_raw)
    pricing = PricingCfg(**pricing_raw)
    return Settings(serving=serving, routing=routing, policy=policy, pricing=pricing)
