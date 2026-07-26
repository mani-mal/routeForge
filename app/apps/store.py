"""Async data-access functions for the application layer.

Every function takes an `AsyncSession`; the caller owns the transaction
(commit/rollback), which keeps these composable and easy to test.
"""
from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.apps.models import Application, AppToken, UsageRecord, utcnow
from app.apps.tokens import hash_token, token_prefix


async def resolve_application(session: AsyncSession, raw_token: str | None
                              ) -> Application | None:
    """Resolve a bearer token to its application, or None. **Fail closed:**
    returns None for an empty/unknown/revoked token or a disabled application —
    the caller rejects with 401. Never falls back to a default tenant.

    Side effect: stamps the token's `last_used_at` (caller commits)."""
    if not raw_token:
        return None
    token_hash = hash_token(raw_token)
    stmt = (
        select(AppToken)
        .where(AppToken.token_hash == token_hash, AppToken.status == "active")
        .options(selectinload(AppToken.application))
    )
    token = (await session.execute(stmt)).scalar_one_or_none()
    if token is None or token.application is None:
        return None
    if token.application.status != "active":
        return None
    token.last_used_at = utcnow()
    return token.application


async def record_usage(
    session: AsyncSession,
    *,
    application_id: str,
    request_id: str,
    model: str,
    provider: str,
    tier: str,
    prompt_tokens: int,
    completion_tokens: int,
    total_tokens: int,
    cost_usd: float,
    baseline_usd: float,
    latency_ms: float,
    sensitivity: str,
    onprem_only: bool,
    status: str = "ok",
) -> UsageRecord:
    """Persist one usage row (caller commits)."""
    row = UsageRecord(
        id=uuid.uuid4().hex,
        application_id=application_id,
        request_id=request_id,
        model=model,
        provider=provider,
        tier=tier,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        total_tokens=total_tokens,
        cost_usd=cost_usd,
        baseline_usd=baseline_usd,
        latency_ms=latency_ms,
        sensitivity=sensitivity,
        onprem_only=onprem_only,
        status=status,
    )
    session.add(row)
    return row


async def list_usage(session: AsyncSession, *, application_id: str | None = None,
                     limit: int = 100) -> list[UsageRecord]:
    """Newest-first usage rows, optionally scoped to one application."""
    stmt = select(UsageRecord).order_by(UsageRecord.ts.desc()).limit(limit)
    if application_id is not None:
        stmt = stmt.where(UsageRecord.application_id == application_id)
    return list((await session.execute(stmt)).scalars().all())


# --- application / token management (used by seeding now; admin API in Phase 3) ---

async def get_application(session: AsyncSession, app_id: str) -> Application | None:
    return await session.get(Application, app_id)


async def list_applications(session: AsyncSession) -> list[Application]:
    stmt = select(Application).order_by(Application.created_at.asc())
    return list((await session.execute(stmt)).scalars().all())


async def create_application(
    session: AsyncSession,
    *,
    app_id: str,
    name: str,
    owner_email: str | None = None,
    default_residency: str = "any",
    status: str = "active",
) -> Application:
    app = Application(
        id=app_id,
        name=name,
        owner_email=owner_email,
        default_residency=default_residency,
        status=status,
    )
    session.add(app)
    return app


async def add_token(session: AsyncSession, *, application_id: str, raw_token: str,
                    name: str = "default") -> AppToken:
    """Store the hash of `raw_token` for an application (caller commits).
    The plaintext is the caller's to hand to the app owner — never persisted."""
    token = AppToken(
        id=uuid.uuid4().hex,
        application_id=application_id,
        token_hash=hash_token(raw_token),
        prefix=token_prefix(raw_token),
        name=name,
        status="active",
    )
    session.add(token)
    return token
