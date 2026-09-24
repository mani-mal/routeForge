"""P3a: the content-free config snapshot `GET /admin/config-snapshot` serves
(spec §4) -- everything a routing decision reads, and nothing else.

Read-only. Built from the live settings, registry rows, app policies, quality
overrides and signals so the offline simulator (`app/eval/scenario.py`) can
rebuild the SAME eligibility and routing inputs without touching the
deployment. Content-free and secret-free BY CONSTRUCTION: every field is
listed explicitly below -- nothing is dumped wholesale except the routing,
pricing and policy config sections, which hold no secrets. Excluded: endpoint
URLs, `api_key_env`, `serving.observability` (Langfuse), `pii_exempt_patterns`
(operator regexes), custom recognizers, and per-app credential columns.
"""
from __future__ import annotations

import dataclasses
import datetime as dt

from sqlalchemy import select

from app.apps import store
from app.apps.models import AppSensitivityRule
from app.apps.policy import AppPolicyView, policy_view_from

SCHEMA = "routeforge.config-snapshot/v1"

_ONBOARDED_FIELDS = ("slug", "display_name", "provider", "tier", "quality",
                     "price_input", "price_output", "price_cached_input",
                     "enabled", "allowed_app_ids", "region", "prompt_retention",
                     "accepts_raw_sensitive", "context_window")
_PROVIDER_FIELDS = ("enabled", "region", "prompt_retention", "accepts_raw_sensitive")
_EXCLUDED_POLICY_FIELDS = frozenset({"pii_exempt_patterns"})


def _jsonable(value):
    if isinstance(value, (set, frozenset)):
        return sorted(value)
    return value


def policy_dict(view: AppPolicyView) -> dict:
    """Every `AppPolicyView` field except the excluded ones; sets become
    sorted lists. Shared with the simulator's tests as the one spelling."""
    return {f.name: _jsonable(getattr(view, f.name))
            for f in dataclasses.fields(view)
            if f.name not in _EXCLUDED_POLICY_FIELDS}


def _quality_entries(overrides: dict) -> list[dict]:
    rows = [{"model": model, "workload": workload, "score": q.score, "set_id": q.set_id}
            for (model, workload), q in overrides.items()]
    return sorted(rows, key=lambda r: (r["model"], r["workload"] or ""))


async def _rules(session, app_id: str) -> list[dict]:
    stmt = (select(AppSensitivityRule)
            .where(AppSensitivityRule.application_id == app_id)
            .order_by(AppSensitivityRule.position))
    return [{"entities": sorted(r.entities), "sensitivity": r.sensitivity,
             "risk_class": r.risk_class, "residency": r.residency,
             "require_verification": bool(r.require_verification)}
            for r in (await session.execute(stmt)).scalars()]


async def build_config_snapshot(session, *, settings, registry, quality, signals,
                                now: dt.datetime) -> dict:
    onboarded = []
    for r in await store.list_onboarded(session):
        row = {k: getattr(r, k) for k in _ONBOARDED_FIELDS}
        row["enabled"] = bool(row["enabled"])
        row["allowed_app_ids"] = (list(row["allowed_app_ids"])
                                  if row["allowed_app_ids"] else None)
        onboarded.append(row)
    apps = []
    for a in await store.list_applications(session):
        full = await store.get_application_with_policy(session, a.id)
        apps.append({"application_id": a.id, "name": a.name,
                     "policy": policy_dict(policy_view_from(full)),
                     "sensitivity_rules": await _rules(session, a.id)})
    customer, calibrated = quality.override_maps()
    providers = {name: {k: getattr(cfg, k) for k in _PROVIDER_FIELDS}
                 for name, cfg in settings.serving.providers.items()}
    return {
        "schema": SCHEMA,
        "generated_at": now.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "routing_policy_version": settings.routing_policy_version,
        "registry_version": registry.registry_version(),
        "settings": {
            "routing": settings.routing.model_dump(mode="json"),
            "pricing": settings.pricing.model_dump(mode="json"),
            "policy": settings.policy.model_dump(mode="json"),
            "providers": providers,
        },
        "onboarded": sorted(onboarded, key=lambda o: o["slug"]),
        "apps": sorted(apps, key=lambda x: x["application_id"]),
        "quality": {"customer": _quality_entries(customer),
                    "calibrated": _quality_entries(calibrated)},
        "signals": signals.snapshot(),
    }
