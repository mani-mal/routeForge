import os
import re
from pathlib import Path

import yaml

from app.config.schema import (
    LangfuseCfg, ObservabilityCfg, PolicyCfg, PricingCfg, RoutingCfg,
    ServingCfg, Settings,
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
            public_key=os.environ.get("LANGFUSE_PUBLIC_KEY"),
            secret_key=os.environ.get("LANGFUSE_SECRET_KEY"),
        )
    )

    serving = ServingCfg(
        inference_backend=os.environ.get(
            "SP_INFERENCE_BACKEND", serving_raw["inference_backend"]),
        allow_backend_fallback=serving_raw["allow_backend_fallback"],
        providers=serving_raw["providers"],
        pii_detection=os.environ.get(
            "SP_PII_DETECTION", serving_raw["policy"]["pii_detection"]),
        reject_stream_requests=serving_raw["server"]["reject_stream_requests"],
        context_optimization_enabled=serving_raw["context_optimization"]["enabled"],
        observability=observability,
    )
    routing = RoutingCfg(**routing_raw)
    policy = PolicyCfg(**policy_raw)
    pricing = PricingCfg(**pricing_raw)
    return Settings(serving=serving, routing=routing, policy=policy, pricing=pricing)
