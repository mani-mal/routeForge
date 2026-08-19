"""Per-application policy: the view the router honors, plus quota helpers.

`AppPolicyView` is a plain, immutable snapshot decoupled from the ORM so it can
travel into `build_context` (and be cached / passed around) without a live DB
session. A null field means "no restriction" — the global privacy rules always
still apply on top; per-app policy can only ever *narrow* them.
"""
from __future__ import annotations

import datetime as dt
import time
from collections import defaultdict, deque
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.apps.models import Application, UsageRecord


@dataclass(frozen=True)
class AppPolicyView:
    """What the router is allowed to do for one application."""

    residency: str = "any"                     # any | private_cloud_ok | onprem_only
    allowed_tiers: frozenset[str] | None = None
    allowed_providers: frozenset[str] | None = None
    allowed_models: frozenset[str] | None = None
    max_spend_usd_per_day: float | None = None
    max_rpm: int | None = None
    compression_enabled: bool | None = None   # None = inherit live global toggle
    capture_io: str | None = None             # None = inherit global capture_io


def _fs(v) -> frozenset[str] | None:
    return frozenset(v) if v else None


def policy_view_from(app: Application) -> AppPolicyView:
    """Build the view from a loaded Application (its `policy` must be eagerly
    loaded). Residency falls back to the application's default when no policy
    row overrides it."""
    p = app.policy
    if p is None:
        return AppPolicyView(residency=app.default_residency)
    return AppPolicyView(
        residency=p.residency or app.default_residency,
        allowed_tiers=_fs(p.allowed_tiers),
        allowed_providers=_fs(p.allowed_providers),
        allowed_models=_fs(p.allowed_models),
        max_spend_usd_per_day=p.max_spend_usd_per_day,
        max_rpm=p.max_rpm,
        compression_enabled=p.compression_enabled,
        capture_io=p.capture_io,
    )


async def load_policy_view(session: AsyncSession, app_id: str) -> AppPolicyView | None:
    """Load an application's policy view by id (admin/side uses). None if the
    application does not exist."""
    stmt = (select(Application)
            .where(Application.id == app_id)
            .options(selectinload(Application.policy)))
    app = (await session.execute(stmt)).scalar_one_or_none()
    return policy_view_from(app) if app is not None else None


async def day_spend_usd(session: AsyncSession, app_id: str) -> float:
    """Sum of today's (UTC) successful spend for an application."""
    start = dt.datetime.now(dt.timezone.utc).replace(
        hour=0, minute=0, second=0, microsecond=0)
    stmt = select(func.coalesce(func.sum(UsageRecord.cost_usd), 0.0)).where(
        UsageRecord.application_id == app_id,
        UsageRecord.ts >= start,
        UsageRecord.status == "ok",
    )
    return float((await session.execute(stmt)).scalar_one())


class RateLimiter:
    """In-memory per-application sliding-window limiter (requests per minute).

    Process-local — fine for the internal/single-node scale this product
    targets. A multi-node deployment would move this to Redis (out of scope)."""

    def __init__(self, window_s: float = 60.0) -> None:
        self._window = window_s
        self._hits: dict[str, deque[float]] = defaultdict(deque)

    def allow(self, app_id: str, max_rpm: int | None) -> bool:
        if max_rpm is None:
            return True
        now = time.monotonic()
        q = self._hits[app_id]
        cutoff = now - self._window
        while q and q[0] < cutoff:
            q.popleft()
        if len(q) >= max_rpm:
            return False
        q.append(now)
        return True
