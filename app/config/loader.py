import os
from pathlib import Path

import yaml

from app.config.schema import (
    PolicyCfg, PricingCfg, RoutingCfg, ServingCfg, Settings,
)


def _read(config_dir: str, name: str) -> dict:
    return yaml.safe_load(Path(config_dir, name).read_text()) or {}


def load_settings(config_dir: str = "config") -> Settings:
    serving_raw = _read(config_dir, "serving.yaml")
    routing_raw = _read(config_dir, "routing.yaml")
    policy_raw = _read(config_dir, "policy.yaml")
    pricing_raw = _read(config_dir, "pricing.yaml")

    serving = ServingCfg(
        inference_backend=os.environ.get(
            "SP_INFERENCE_BACKEND", serving_raw["inference_backend"]),
        allow_backend_fallback=serving_raw["allow_backend_fallback"],
        providers=serving_raw["providers"],
        pii_detection=os.environ.get(
            "SP_PII_DETECTION", serving_raw["policy"]["pii_detection"]),
        reject_stream_requests=serving_raw["server"]["reject_stream_requests"],
        context_optimization_enabled=serving_raw["context_optimization"]["enabled"],
    )
    routing = RoutingCfg(**routing_raw)
    policy = PolicyCfg(**policy_raw)
    pricing = PricingCfg(**pricing_raw)
    return Settings(serving=serving, routing=routing, policy=policy, pricing=pricing)
