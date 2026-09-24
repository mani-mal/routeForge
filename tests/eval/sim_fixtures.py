"""Shared builders for the P3a simulator tests: a minimal valid config
snapshot (Task 1's shape) and P2b-2 outcome records. Test-only."""
from __future__ import annotations

import copy

from app.config.loader import load_settings

BASE_SETTINGS = load_settings("config")


def onboarded_row(slug: str, tier: str, **over) -> dict:
    row = {"slug": slug, "display_name": slug, "provider": "onprem", "tier": tier,
           "quality": None, "price_input": {"cheap": 0.1, "mid": 0.5, "frontier": 2.0}[tier],
           "price_output": {"cheap": 0.2, "mid": 1.0, "frontier": 6.0}[tier],
           "price_cached_input": None, "enabled": True, "allowed_app_ids": None,
           "region": "onprem", "prompt_retention": "none",
           "accepts_raw_sensitive": True, "context_window": None}
    row.update(over)
    return row


def app_entry(app_id: str, *, sensitivity_rules: list | None = None, **policy) -> dict:
    base = {"residency": "any"}
    base.update(policy)
    return {"application_id": app_id, "name": app_id, "policy": base,
            "sensitivity_rules": sensitivity_rules or []}


def make_snapshot(*, onboarded=None, apps=None, providers=None, quality=None,
                  signals=None) -> dict:
    s = BASE_SETTINGS
    prov = {n: {"enabled": c.enabled, "region": c.region,
                "prompt_retention": c.prompt_retention,
                "accepts_raw_sensitive": c.accepts_raw_sensitive}
            for n, c in s.serving.providers.items()}
    for n, over in (providers or {}).items():
        prov[n] = {**prov[n], **over}
    return copy.deepcopy({
        "schema": "routeforge.config-snapshot/v1",
        "generated_at": "2026-09-24T00:00:00Z",
        "routing_policy_version": s.routing_policy_version,
        "registry_version": "test",
        "settings": {"routing": s.routing.model_dump(mode="json"),
                     "pricing": s.pricing.model_dump(mode="json"),
                     "policy": s.policy.model_dump(mode="json"),
                     "providers": prov},
        "onboarded": onboarded if onboarded is not None else [
            onboarded_row("local-cheap", "cheap"),
            onboarded_row("local-mid", "mid"),
            onboarded_row("local-big", "frontier")],
        "apps": apps if apps is not None else [app_entry("app-a")],
        "quality": quality or {"customer": [], "calibrated": []},
        "signals": signals or {},
    })


def make_row(request_id: str, app_id: str, **over) -> dict:
    row = {"request_id": request_id, "application_id": app_id, "status": "ok",
           "ts": "2026-09-20T12:00:00+00:00", "workload": None,
           "complexity": 0.1, "prompt_tokens": 200, "completion_tokens": 100,
           "sensitivity": "public", "risk_class": "low", "onprem_only": False,
           "detected_entity_types": [], "cache_hit": False,
           "attempts": ["local-cheap"], "chosen": "local-cheap",
           "latency_ms": 120.0, "cost_usd": 0.0, "feedback": []}
    row.update(over)
    return row
