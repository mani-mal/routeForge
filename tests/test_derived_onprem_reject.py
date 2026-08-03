import asyncio

from app.config.loader import load_settings
from app.apps.db import Database
from app.apps.registry import ModelRegistry
from app.policy.context import build_context

S = load_settings()


async def _empty_registry() -> ModelRegistry:
    # Zero onboarded rows -> no healthy on-prem model.
    db = Database(url="sqlite+aiosqlite://")
    await db.ensure_ready()
    reg = ModelRegistry(S)
    await reg.refresh(db)
    return reg


def test_no_healthy_onprem_drops_onprem_for_onprem_only():
    reg = asyncio.run(_empty_registry())
    # The `hospital` tenant is default_residency: onprem_only in policy.yaml, so
    # with no entities the request still resolves onprem_only via the tenant.
    ctx = build_context(S, request_id="r", tenant_id="hospital",
                        entities=[], redaction_map_id=None, registry=reg)
    assert ctx.onprem_only is True
    # Derived on-prem availability: no healthy onboarded on-prem model ->
    # "onprem" is dropped from allowed_providers (fail-closed; replaces the old
    # inference_backend == "none" drop). An onprem_only request thus resolves to
    # an empty provider set and the router rejects it downstream.
    assert "onprem" not in ctx.allowed_providers
