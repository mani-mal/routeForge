import asyncio

import pytest

from app.config.loader import load_settings
from app.apps.db import Database
from app.apps.registry import ModelRegistry
from app.models import ExecutionPolicyContext
from app.policy.context import build_context
from app.routing.router import Router

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


def test_router_rejects_onprem_only_with_no_onboarded_candidates():
    # Now that Qwen is gone from the static YAML pool, an onprem_only context
    # with an empty registry (no onboarded on-prem rows) leaves the router with
    # zero candidates after the residency filter -- it must raise, never fall
    # through to a cloud tier (CLAUDE.md hard rule 3: onprem_only is absolute).
    reg = asyncio.run(_empty_registry())
    ctx = ExecutionPolicyContext(
        request_id="r", tenant_id="hospital", sensitivity="restricted",
        onprem_only=True, allowed_providers=frozenset({"onprem"}),
        allowed_models=frozenset(), max_cost_usd=1.0, max_latency_ms=10000,
        risk_class="high", require_verification=True, detected_entities=(),
        redaction_map_id=None)
    with pytest.raises(ValueError, match="no candidates after residency filter"):
        Router(S, registry=reg).select(ctx, tokens_after=20, complexity=0.1)
