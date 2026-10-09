"""Per-application policy: the view the router honors, plus quota helpers.

`AppPolicyView` is a plain, immutable snapshot decoupled from the ORM so it can
travel into `build_context` (and be cached / passed around) without a live DB
session. A null field means "no restriction" — the global privacy rules always
still apply on top; per-app policy can only ever *narrow* them.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
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
    max_cost_usd_per_request: float | None = None  # None = inherit budgets.default_max_spend_usd
    max_rpm: int | None = None
    compression_enabled: bool | None = None   # None = inherit live global toggle
    capture_io: str | None = None             # None = inherit global capture_io
    # enforced | passthrough. None = inherit serving.policy.default_policy_mode.
    # Protection posture only — never widens placement (residency is separate).
    policy_mode: str | None = None
    pii_detection: str | None = None          # None = inherit global pii_detection
    stream_mode: str | None = None            # None = inherit (reject inbound stream)
    pii_entity_denylist: frozenset[str] | None = None  # entity types to NOT redact
    pii_score_threshold: float | None = None  # min Presidio confidence; None = no floor
    pii_exempt_patterns: frozenset[str] | None = None  # regexes; VALUE fullmatch => not redacted
    # --- P1a data-handling axes. null = inherit; resolved by
    # app/policy/compile.py::compile_policy, not here.
    model_input: str | None = None            # None = inherit (policy_mode preset)
    client_output: str | None = None          # None = inherit (policy_mode preset)
    content_integrity: str | None = None      # None = inherit (transformations_allowed)
    escalate_residency_on_detection: bool | None = None  # None = inherit (true)
    # --- P1b app-side data-handling requirements (design doc §3). null = no
    # constraint; resolved explicit-or-null by app/policy/compile.py (no
    # preset, no deployment default -- unlike the P1a axes above). Narrows
    # the candidate set only; never widens allowed_providers/onprem_only.
    allowed_regions: frozenset[str] | None = None       # None = no regional constraint
    require_no_retention: bool | None = None            # None = inherit (false)
    # B72: off | header | sticky; None = off. app/policy/trajectory.py.
    trajectory_mode: str | None = None

    def version(self) -> str:
        """First 12 hex of sha256 over this app policy's decision-relevant
        fields. Stamped on every response (and trace) so a consumer — notably a
        client's own conformance probe — can tell whether a cached PASS/FAIL
        result predates a later policy change: the version differs the instant an
        operator edits residency, policy_mode (the protection posture — an app
        flipped to `passthrough` must never be unprotected invisibly),
        pii_detection, the denylist, the score floor, the value-shape exempt
        patterns, a tier/model/provider allowlist, or (when set) one of the
        P1a data-handling axes (model_input, client_output, content_integrity,
        escalate_residency_on_detection), or one of the P1b app-side
        requirements (allowed_regions, require_no_retention) -- all
        mutated out-of-band via the admin API, which is exactly what makes a
        stale probe indistinguishable from a live one without this). Sorted-key
        JSON → stable across boots for equal policy. Mirrors
        `app/config/loader.py::_policy_version` for routing.

        Scope note: this covers the per-app `AppPolicy` surface only. Custom
        recognizers / sensitivity rules live in the separate privacy *profile*
        and are not folded in here."""
        fields = {
            "residency": self.residency,
            "allowed_tiers": sorted(self.allowed_tiers) if self.allowed_tiers else None,
            "allowed_providers":
                sorted(self.allowed_providers) if self.allowed_providers else None,
            "allowed_models": sorted(self.allowed_models) if self.allowed_models else None,
            "max_spend_usd_per_day": self.max_spend_usd_per_day,
            "max_rpm": self.max_rpm,
            "compression_enabled": self.compression_enabled,
            "capture_io": self.capture_io,
            "policy_mode": self.policy_mode,
            "pii_detection": self.pii_detection,
            "stream_mode": self.stream_mode,
            "pii_entity_denylist":
                sorted(self.pii_entity_denylist) if self.pii_entity_denylist else None,
            "pii_score_threshold": self.pii_score_threshold,
            "pii_exempt_patterns":
                sorted(self.pii_exempt_patterns) if self.pii_exempt_patterns else None,
        }
        # Folded in only when set, so an unset cap leaves every existing app's
        # version hash unchanged (matches the field's null = inherit default).
        if self.max_cost_usd_per_request is not None:
            fields["max_cost_usd_per_request"] = self.max_cost_usd_per_request
        # P1a data-handling axes: same "fold in only when set" rule, so an app
        # with all four still-null columns hashes identically to before this
        # task landed them.
        if self.model_input is not None:
            fields["model_input"] = self.model_input
        if self.client_output is not None:
            fields["client_output"] = self.client_output
        if self.content_integrity is not None:
            fields["content_integrity"] = self.content_integrity
        if self.escalate_residency_on_detection is not None:
            fields["escalate_residency_on_detection"] = \
                self.escalate_residency_on_detection
        if self.trajectory_mode is not None:
            fields["trajectory_mode"] = self.trajectory_mode
        # P1b data-handling requirements: same "fold in only when set" rule.
        if self.allowed_regions is not None:
            fields["allowed_regions"] = sorted(self.allowed_regions)
        if self.require_no_retention is not None:
            fields["require_no_retention"] = self.require_no_retention
        blob = json.dumps(fields, sort_keys=True, default=str)
        return hashlib.sha256(blob.encode()).hexdigest()[:12]


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
        max_cost_usd_per_request=p.max_cost_usd_per_request,
        max_rpm=p.max_rpm,
        compression_enabled=p.compression_enabled,
        capture_io=p.capture_io,
        policy_mode=p.policy_mode,
        pii_detection=p.pii_detection,
        stream_mode=p.stream_mode,
        pii_entity_denylist=_fs(p.pii_entity_denylist),
        pii_score_threshold=p.pii_score_threshold,
        pii_exempt_patterns=_fs(p.pii_exempt_patterns),
        model_input=p.model_input,
        client_output=p.client_output,
        content_integrity=p.content_integrity,
        escalate_residency_on_detection=p.escalate_residency_on_detection,
        allowed_regions=_fs(p.allowed_regions),
        require_no_retention=p.require_no_retention,
        trajectory_mode=p.trajectory_mode,
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
