"""Async data-access functions for the application layer.

Every function takes an `AsyncSession`; the caller owns the transaction
(commit/rollback), which keeps these composable and easy to test.
"""
from __future__ import annotations

import datetime as dt
import os
import uuid

from sqlalchemy import and_, delete, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.apps.models import (
    Application, AppPolicy, AppRecognizer, AppSensitivityRule, AppToken,
    EndpointObservation, ModelQuality, OnboardedModel, OutcomeFeedback,
    RequestSnapshot, RoutingOutcome, ServiceCredential, UsageRecord, utcnow,
)
from app.apps.privacy import AppPrivacyProfile, SensitivityRuleView
from app.apps.tokens import hash_token, token_prefix
from app.policy.recognizers import CustomRecognizer
from app.routing.observations import Observation


async def resolve_application(session: AsyncSession, raw_token: str | None,
                              session_id: str | None = None
                              ) -> Application | None:
    """Resolve a bearer token to its application, or None. **Fail closed:**
    returns None for an empty/unknown/revoked/expired token, a token bound to a
    session other than `session_id` (the call's sanitized
    `x-routeforge-session`; a missing header never matches a binding), or a
    disabled application — the caller rejects with 401. Never falls back to a
    default tenant.

    Side effect: stamps the token's `last_used_at` (caller commits)."""
    if not raw_token:
        return None
    token_hash = hash_token(raw_token)
    stmt = (
        select(AppToken)
        .where(AppToken.token_hash == token_hash, AppToken.status == "active")
        # eager-load the application AND its policy so callers can build an
        # AppPolicyView after the session closes (no lazy load in async).
        .options(selectinload(AppToken.application).selectinload(Application.policy))
    )
    token = (await session.execute(stmt)).scalar_one_or_none()
    if token is None or token.application is None:
        return None
    if token_expired(token):
        return None
    if token.session_binding is not None and session_id != token.session_binding:
        return None
    if token.application.status != "active":
        return None
    token.last_used_at = utcnow()
    return token.application


def token_expired(token: AppToken, now: dt.datetime | None = None) -> bool:
    """True once a token with an `expires_at` has reached it."""
    if token.expires_at is None:
        return False
    return (now or utcnow()) >= _to_utc(token.expires_at)


async def resolve_service_credential(session: AsyncSession, raw: str | None
                                     ) -> ServiceCredential | None:
    """Resolve a bearer service credential, or None (fail closed): empty,
    unknown or revoked credential, or one whose application is not active.
    Stamps `last_used_at` (caller commits)."""
    if not raw:
        return None
    stmt = select(ServiceCredential).where(
        ServiceCredential.token_hash == hash_token(raw),
        ServiceCredential.status == "active")
    cred = (await session.execute(stmt)).scalar_one_or_none()
    if cred is None:
        return None
    app = await session.get(Application, cred.application_id)
    if app is None or app.status != "active":
        return None
    cred.last_used_at = utcnow()
    return cred


async def add_service_credential(session: AsyncSession, *, application_id: str,
                                 raw: str, name: str = "default"
                                 ) -> ServiceCredential:
    """Store the hash of a new service credential (caller commits)."""
    cred = ServiceCredential(
        id=uuid.uuid4().hex, application_id=application_id,
        token_hash=hash_token(raw), prefix=token_prefix(raw), name=name,
        status="active")
    session.add(cred)
    return cred


async def list_service_credentials(session: AsyncSession, application_id: str
                                   ) -> list[ServiceCredential]:
    stmt = (select(ServiceCredential)
            .where(ServiceCredential.application_id == application_id)
            .order_by(ServiceCredential.created_at))
    return list((await session.execute(stmt)).scalars().all())


async def revoke_service_credential(session: AsyncSession, cred_id: str
                                    ) -> ServiceCredential | None:
    cred = await session.get(ServiceCredential, cred_id)
    if cred is None:
        return None
    cred.status = "revoked"
    cred.revoked_at = utcnow()
    # Revoking the credential also revokes every token it issued: a leaked
    # credential's tokens must not outlive it.
    issued = await session.execute(
        select(AppToken).where(AppToken.issued_by == cred_id,
                               AppToken.status == "active"))
    for token in issued.scalars():
        token.status = "revoked"
        token.revoked_at = cred.revoked_at
    return cred


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
    cached_prompt_tokens: int = 0,
    cost_usd: float,
    baseline_usd: float,
    latency_ms: float,
    sensitivity: str,
    onprem_only: bool,
    status: str = "ok",
    quality: float = 0.0,
    quality_source: str = "?",
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
        cached_prompt_tokens=cached_prompt_tokens,
        cost_usd=cost_usd,
        baseline_usd=baseline_usd,
        latency_ms=latency_ms,
        sensitivity=sensitivity,
        onprem_only=onprem_only,
        status=status,
        quality=quality,
        quality_source=quality_source,
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


def _to_utc(ts: dt.datetime) -> dt.datetime:
    """SQLite may hand back a naive datetime for a `DateTime(timezone=True)`
    column; treat naive as UTC (it was written as UTC by
    `write_observations` below) rather than the local zone."""
    return ts if ts.tzinfo is not None else ts.replace(tzinfo=dt.timezone.utc)


def _to_observation(row: EndpointObservation) -> Observation:
    return Observation(_to_utc(row.ts).timestamp(), row.model, row.outcome,
                       row.latency_ms, row.streamed, row.ttft_ms)


async def write_observations(session: AsyncSession,
                             observations: list[Observation]) -> None:
    """Persist a batch of endpoint observations (caller commits)."""
    for o in observations:
        session.add(EndpointObservation(
            ts=dt.datetime.fromtimestamp(o.ts, dt.timezone.utc),
            model=o.model, outcome=o.outcome, latency_ms=o.latency_ms,
            streamed=o.streamed, ttft_ms=o.ttft_ms))


async def load_seed_observations(session: AsyncSession, *, since_hours: float,
                                 per_model_cap: int) -> list[Observation]:
    """The replay set for `SignalStore.replay` (spec §3.4): per model, the
    most recent `per_model_cap` rows within the window, concatenated and
    sorted ascending by `ts` (tie-break `id`) -- `replay` requires ascending
    order. Two queries per model (distinct models, then a capped desc select
    per model) rather than a window function, to stay portable across
    SQLite/Postgres."""
    cutoff = utcnow() - dt.timedelta(hours=since_hours)
    models = (await session.execute(
        select(EndpointObservation.model).distinct()
        .where(EndpointObservation.ts >= cutoff))).scalars().all()
    rows: list[EndpointObservation] = []
    for model in models:
        stmt = (
            select(EndpointObservation)
            .where(EndpointObservation.model == model,
                  EndpointObservation.ts >= cutoff)
            .order_by(EndpointObservation.ts.desc(), EndpointObservation.id.desc())
            .limit(per_model_cap)
        )
        rows.extend((await session.execute(stmt)).scalars().all())
    rows.sort(key=lambda r: (r.ts, r.id))
    return [_to_observation(r) for r in rows]


async def prune_observations(session: AsyncSession, *,
                             older_than_hours: float) -> int:
    """Delete observation rows older than the window (caller commits).
    Returns the number of rows deleted."""
    cutoff = utcnow() - dt.timedelta(hours=older_than_hours)
    result = await session.execute(
        delete(EndpointObservation).where(EndpointObservation.ts < cutoff))
    return result.rowcount


# --- routing outcomes (P2b-2: routing_outcomes + outcome_feedback) ---------
#
# CONTENT-FREE BY CONTRACT (spec §4.3): `row` and the feedback fields below
# are ids, enums, numbers, entity TYPES and version hashes only -- never
# prompt/completion text, reason strings or upstream error text. Callers
# (later P2b-2 tasks) are responsible for only ever passing content-free
# values; these functions do not re-validate that (RoutingOutcome/
# OutcomeFeedback's own column set is the enforcement point -- see
# tests/test_outcome_store.py's schema test).


async def write_outcome(session: AsyncSession, row: dict) -> None:
    """Persist one routing outcome row (caller commits). `row` is a plain
    dict of RoutingOutcome column values, keyed by column name."""
    session.add(RoutingOutcome(**row))


async def get_outcome(session: AsyncSession, request_id: str, *,
                      application_id: str) -> RoutingOutcome | None:
    """Fetch one outcome by primary key, scoped to `application_id` -- a
    request_id that belongs to another app returns None (fail closed, same
    posture as `resolve_application`)."""
    row = await session.get(RoutingOutcome, request_id)
    if row is None or row.application_id != application_id:
        return None
    return row


async def find_outcome(session: AsyncSession, *, application_id: str,
                       request_id: str | None,
                       trace_id: str | None) -> RoutingOutcome | None:
    """App-scoped lookup by request_id when given, else by trace_id. Both
    None returns None rather than an unscoped "any row for this app" query."""
    stmt = select(RoutingOutcome).where(RoutingOutcome.application_id == application_id)
    if request_id is not None:
        stmt = stmt.where(RoutingOutcome.request_id == request_id)
    elif trace_id is not None:
        stmt = stmt.where(RoutingOutcome.trace_id == trace_id)
    else:
        return None
    return (await session.execute(stmt)).scalars().first()


async def write_feedback(session: AsyncSession, *, request_id: str,
                         application_id: str, score_name: str,
                         value_num: float | None, value_cat: str | None,
                         source: str) -> None:
    """Persist one feedback score (caller commits). `request_id` is a
    LOGICAL link to routing_outcomes, not an FK (see OutcomeFeedback
    docstring) -- this never validates the outcome row exists."""
    session.add(OutcomeFeedback(
        request_id=request_id, application_id=application_id,
        score_name=score_name, value_num=value_num, value_cat=value_cat,
        source=source))


async def feedback_for(session: AsyncSession, request_ids: list[str], *,
                       application_id: str | None = None
                       ) -> dict[str, list[OutcomeFeedback]]:
    """All feedback rows for the given request ids, grouped by request_id.
    A request_id with no feedback is simply absent from the result. With
    `application_id`, only that app's rows -- the export joins feedback by
    (application_id, request_id), never by request_id alone."""
    if not request_ids:
        return {}
    stmt = select(OutcomeFeedback).where(OutcomeFeedback.request_id.in_(request_ids))
    if application_id is not None:
        stmt = stmt.where(OutcomeFeedback.application_id == application_id)
    rows = (await session.execute(stmt)).scalars().all()
    out: dict[str, list[OutcomeFeedback]] = {}
    for row in rows:
        out.setdefault(row.request_id, []).append(row)
    return out


async def prune_outcomes(session: AsyncSession, *, older_than_days: float) -> int:
    """Delete outcome AND feedback rows older than the retention window
    (caller commits). Returns the number of routing_outcomes rows deleted."""
    cutoff = utcnow() - dt.timedelta(days=older_than_days)
    await session.execute(delete(OutcomeFeedback).where(OutcomeFeedback.ts < cutoff))
    result = await session.execute(
        delete(RoutingOutcome).where(RoutingOutcome.ts < cutoff))
    return result.rowcount


async def iter_outcomes(session: AsyncSession, *, since: dt.datetime,
                        until: dt.datetime, application_id: str | None = None,
                        after: tuple[dt.datetime, str] | None = None,
                        page: int = 1000) -> list[RoutingOutcome]:
    """One page of outcomes in [since, until], ordered by (ts, request_id).
    `after` is the last (ts, request_id) of the previous page (keyset
    pagination -- stable under concurrent writes, unlike OFFSET)."""
    stmt = select(RoutingOutcome).where(
        RoutingOutcome.ts >= since, RoutingOutcome.ts <= until)
    if application_id is not None:
        stmt = stmt.where(RoutingOutcome.application_id == application_id)
    if after is not None:
        after_ts, after_id = after
        stmt = stmt.where(or_(
            RoutingOutcome.ts > after_ts,
            and_(RoutingOutcome.ts == after_ts, RoutingOutcome.request_id > after_id)))
    stmt = stmt.order_by(RoutingOutcome.ts, RoutingOutcome.request_id).limit(page)
    return list((await session.execute(stmt)).scalars().all())


# --- request snapshots (B3: shared dashboard state across API replicas) ---

# Bounded size of the shared `request_snapshots` cache (console hot set), env-
# overridable. Defined once here (not in routes.py/ui_routes.py) to avoid a
# circular import between those two sibling modules — both already import
# this module as `store` and reference it as `store.SNAPSHOT_CAPACITY`.
SNAPSHOT_CAPACITY = int(os.environ.get("SP_SNAPSHOT_CAPACITY", "500"))


async def record_snapshot(session: AsyncSession, *, request_id: str,
                          application_id: str, summary_json: dict,
                          full_json: dict) -> RequestSnapshot:
    """Upsert-by-write one request snapshot (caller commits). Newer writes for
    the same request_id replace the row (merge on PK)."""
    row = RequestSnapshot(
        request_id=request_id, application_id=application_id,
        summary_json=summary_json, full_json=full_json)
    await session.merge(row)
    return row


async def get_snapshot_full(session: AsyncSession, request_id: str) -> dict | None:
    """Retrieve the full_json for a single snapshot, or None."""
    row = await session.get(RequestSnapshot, request_id)
    return row.full_json if row is not None else None


async def list_snapshot_summaries(session: AsyncSession, *,
                                  application_id: str | None = None,
                                  limit: int = 100) -> list[dict]:
    """Newest-first (ts desc) summary dicts, optionally scoped to one app."""
    if limit <= 0:
        return []
    stmt = (select(RequestSnapshot.summary_json, RequestSnapshot.ts)
            .order_by(RequestSnapshot.ts.desc(), RequestSnapshot.request_id.desc())
            .limit(limit))
    if application_id is not None:
        stmt = stmt.where(RequestSnapshot.application_id == application_id)
    rows = (await session.execute(stmt)).all()
    return [{**summary_json, "ts": ts.isoformat() if ts is not None else None}
            for summary_json, ts in rows]


async def all_snapshot_summaries(session: AsyncSession, *,
                                 limit: int = 500) -> list[dict]:
    """The current bounded snapshot set as summary dicts, for stats/fleet."""
    return await list_snapshot_summaries(session, limit=limit)


async def prune_snapshots(session: AsyncSession, *, capacity: int = 500) -> None:
    """Keep only the newest `capacity` snapshots (bounded UI cache). Ordering by
    (ts desc, request_id desc) matches the read path; under identical timestamps
    the tie-break keeps prune and list consistent."""
    keep = (select(RequestSnapshot.request_id)
            .order_by(RequestSnapshot.ts.desc(), RequestSnapshot.request_id.desc())
            .limit(capacity)).scalar_subquery()
    await session.execute(
        delete(RequestSnapshot).where(RequestSnapshot.request_id.not_in(keep)))


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
                    name: str = "default", expires_at: dt.datetime | None = None,
                    session_binding: str | None = None,
                    issued_by: str | None = None) -> AppToken:
    """Store the hash of `raw_token` for an application (caller commits).
    The plaintext is the caller's to hand to the app owner — never persisted.
    `expires_at` / `session_binding` are optional limits (see
    `resolve_application`); `issued_by` names the creating service credential."""
    token = AppToken(
        id=uuid.uuid4().hex,
        application_id=application_id,
        token_hash=hash_token(raw_token),
        prefix=token_prefix(raw_token),
        name=name,
        status="active",
        expires_at=expires_at,
        session_binding=session_binding,
        issued_by=issued_by,
    )
    session.add(token)
    return token


async def get_application_with_policy(session: AsyncSession, app_id: str
                                      ) -> Application | None:
    stmt = (select(Application).where(Application.id == app_id)
            .options(selectinload(Application.policy),
                     selectinload(Application.tokens)))
    return (await session.execute(stmt)).scalar_one_or_none()


async def update_application(session: AsyncSession, app_id: str, **fields
                             ) -> Application | None:
    """Patch mutable Application columns (name, owner_email, status,
    default_residency, langfuse_*). Unknown keys are ignored."""
    app = await session.get(Application, app_id)
    if app is None:
        return None
    allowed = {"name", "owner_email", "status", "default_residency",
               "langfuse_project_id", "langfuse_public_key",
               "langfuse_secret_key_enc", "langfuse_host"}
    for k, v in fields.items():
        if k in allowed:
            setattr(app, k, v)
    return app


# The writable per-app policy columns — the single authority for "is this a
# legal policy field name?". `upsert_policy` filters writes through it, and the
# admin API validates `PolicyIn.reset` names against it (a name outside this set
# is a 422, never a silent no-op). Anything added here must also get a field on
# `PolicyIn`; tests/test_admin_api.py asserts the two stay in step.
POLICY_FIELDS: frozenset[str] = frozenset({
    "allowed_tiers", "allowed_providers", "allowed_models",
    "residency", "max_spend_usd_per_day", "max_cost_usd_per_request", "max_rpm",
    "compression_enabled", "capture_io",
    "policy_mode", "pii_detection", "stream_mode",
    "pii_entity_denylist", "pii_score_threshold", "pii_exempt_patterns",
    "model_input", "client_output", "content_integrity",
    "escalate_residency_on_detection",
    "allowed_regions", "require_no_retention", "trajectory_mode"})


async def upsert_policy(session: AsyncSession, app_id: str, **fields) -> AppPolicy:
    """Create or update an application's policy row (caller commits).

    An explicit `None` value here DOES write NULL — it is how a field is put
    back to "inherit the deployment default". The merge/omit semantics live one
    level up in the admin API (`exclude_none=True` on the request body), so a
    caller that wants a NULL has to ask for it by name (`PolicyIn.reset`).
    """
    policy = await session.get(AppPolicy, app_id)
    if policy is None:
        policy = AppPolicy(application_id=app_id)
        session.add(policy)
    for k, v in fields.items():
        if k in POLICY_FIELDS:
            setattr(policy, k, v)
    return policy


async def list_tokens(session: AsyncSession, app_id: str) -> list[AppToken]:
    stmt = (select(AppToken).where(AppToken.application_id == app_id)
            .order_by(AppToken.created_at.asc()))
    return list((await session.execute(stmt)).scalars().all())


async def revoke_token(session: AsyncSession, token_id: str) -> AppToken | None:
    token = await session.get(AppToken, token_id)
    if token is None:
        return None
    token.status = "revoked"
    token.revoked_at = utcnow()
    return token


def _pctile(sorted_vals: list[float], pct: float) -> float:
    if not sorted_vals:
        return 0.0
    idx = min(len(sorted_vals) - 1, int(round(pct * (len(sorted_vals) - 1))))
    return sorted_vals[idx]


async def usage_summary(session: AsyncSession, *, application_id: str | None = None,
                        since: dt.datetime | None = None) -> dict:
    """Aggregate usage for the admin dashboard, optionally scoped to one
    application and/or a start time. Computed in Python (portable across
    SQLite/Postgres) over the selected rows.

    Every spend, token, latency and distribution figure is over `ok` rows
    only. `rejected_count` and `error_count` count their statuses beside them:
    `error` rows are requests that failed on the backend (spec §4, B38) --
    shown so an outage is visible, never counted as spend or served work."""
    stmt = select(UsageRecord)
    if application_id is not None:
        stmt = stmt.where(UsageRecord.application_id == application_id)
    if since is not None:
        stmt = stmt.where(UsageRecord.ts >= since)
    rows = list((await session.execute(stmt)).scalars().all())

    ok = [r for r in rows if r.status == "ok"]
    tier_dist: dict[str, int] = {}
    model_dist: dict[str, int] = {}
    app_dist: dict[str, int] = {}
    # Provenance of the `quality` numerator below — a tier proxy by default
    # (see app/routing/quality.py's customer > calibrated > published >
    # tier_default ladder), never presented as measured accuracy.
    quality_source_dist: dict[str, int] = {}
    for r in ok:
        tier_dist[r.tier] = tier_dist.get(r.tier, 0) + 1
        model_dist[r.model] = model_dist.get(r.model, 0) + 1
        app_dist[r.application_id] = app_dist.get(r.application_id, 0) + 1
        if r.quality_source:
            quality_source_dist[r.quality_source] = (
                quality_source_dist.get(r.quality_source, 0) + 1)
    latencies = sorted(r.latency_ms for r in ok)
    total_tokens = sum(r.total_tokens for r in ok)
    total_usd = sum(r.cost_usd for r in ok)
    # Same numerator/formula as app/api/history.py:145-146 (stats_over):
    # quality summed across calls, over tokens (x1000) or over USD spend.
    quality_sum = sum(r.quality for r in ok)
    return {
        "count": len(ok),
        "rejected_count": sum(1 for r in rows if r.status == "rejected"),
        "error_count": sum(1 for r in rows if r.status == "error"),
        "total_usd": total_usd,
        "baseline_usd": sum(r.baseline_usd for r in ok),
        "total_tokens": total_tokens,
        "prompt_tokens": sum(r.prompt_tokens for r in ok),
        "completion_tokens": sum(r.completion_tokens for r in ok),
        "onprem_count": sum(1 for r in ok if r.onprem_only),
        "tier_distribution": tier_dist,
        "model_distribution": model_dist,
        "application_distribution": app_dist,
        "latency_p50_ms": _pctile(latencies, 0.50),
        "latency_p95_ms": _pctile(latencies, 0.95),
        "token_intelligence_density": (
            quality_sum / total_tokens * 1000.0) if total_tokens > 0 else 0.0,
        "cost_intelligence_density": (
            quality_sum / total_usd) if total_usd > 0 else 0.0,
        "quality_source_distribution": quality_source_dist,
    }


async def usage_by_application(session: AsyncSession, *,
                               since: dt.datetime | None = None) -> list[dict]:
    """App-wise rollup over UsageRecord (durable store; portable Python
    aggregation across SQLite/Postgres — mirrors `usage_summary`'s style).
    Sorted by total_usd descending. As there, everything but `rejected_count`
    and `error_count` (backend failures, spec §4) is over `ok` rows only."""
    stmt = select(UsageRecord)
    if since is not None:
        stmt = stmt.where(UsageRecord.ts >= since)
    rows = (await session.execute(stmt)).scalars().all()
    agg: dict[str, dict] = {}
    for r in rows:
        a = agg.setdefault(r.application_id, {
            "application_id": r.application_id, "count": 0, "rejected_count": 0,
            "error_count": 0, "total_usd": 0.0, "baseline_usd": 0.0, "total_tokens": 0, "onprem_count": 0,
            "tier_distribution": {}, "model_distribution": {}, "sensitivity_distribution": {},
            "quality_source_distribution": {},
        })
        if r.status == "rejected":
            a["rejected_count"] += 1
        if r.status == "error":
            a["error_count"] += 1
        if r.status != "ok":
            continue
        a["count"] += 1
        a["total_usd"] += float(r.cost_usd or 0.0)
        a["baseline_usd"] += float(r.baseline_usd or 0.0)
        a["total_tokens"] += int(r.total_tokens or 0)
        # Internal-only running sum, same numerator history.py uses; popped
        # below before the rollup is returned (aggregate-only, no leakage).
        a["_quality_sum"] = a.get("_quality_sum", 0.0) + float(r.quality or 0.0)
        if r.onprem_only:
            a["onprem_count"] += 1
        for key, val in (("tier_distribution", r.tier), ("model_distribution", r.model),
                         ("sensitivity_distribution", r.sensitivity),
                         ("quality_source_distribution", r.quality_source)):
            if val:
                a[key][val] = a[key].get(val, 0) + 1

    # Same formula as usage_summary/app/api/history.py:145-146, computed once
    # the per-app sums are final. `quality` here is a tier-quality proxy (or
    # calibrated/customer when set) — not measured accuracy.
    for a in agg.values():
        quality_sum = a.pop("_quality_sum", 0.0)
        tok_sum = a["total_tokens"]
        usd_sum = a["total_usd"]
        a["token_intelligence_density"] = (
            quality_sum / tok_sum * 1000.0) if tok_sum > 0 else 0.0
        a["cost_intelligence_density"] = (
            quality_sum / usd_sum) if usd_sum > 0 else 0.0

    return sorted(agg.values(), key=lambda d: d["total_usd"], reverse=True)


# --- per-app privacy profile (recognizers + sensitivity rules) ---

async def get_privacy_profile(session: AsyncSession, app_id: str) -> AppPrivacyProfile:
    """Load an application's enabled recognizers and ordered sensitivity rules.
    Empty `rules` (no rows) signals "fall back to global" — see effective_rules."""
    recs = (await session.execute(
        select(AppRecognizer).where(
            AppRecognizer.application_id == app_id,
            AppRecognizer.enabled.is_(True)))).scalars().all()
    rules = (await session.execute(
        select(AppSensitivityRule).where(
            AppSensitivityRule.application_id == app_id)
        .order_by(AppSensitivityRule.position))).scalars().all()
    return AppPrivacyProfile(
        recognizers=tuple(CustomRecognizer(r.name, r.regex, r.score) for r in recs),
        rules=tuple(SensitivityRuleView(
            frozenset(r.entities), r.sensitivity, r.risk_class,
            r.residency, r.require_verification) for r in rules))


async def replace_privacy_profile(session: AsyncSession, app_id: str,
                                  recognizers: list[dict], rules: list[dict]) -> None:
    """Replace an application's recognizers and sensitivity rules wholesale:
    delete existing rows, insert the new set (caller commits). Caller is
    responsible for validating `recognizers`/`rules` before calling this
    (e.g. via validate_recognizer) — this function trusts its input."""
    await session.execute(
        delete(AppRecognizer).where(AppRecognizer.application_id == app_id))
    await session.execute(
        delete(AppSensitivityRule).where(AppSensitivityRule.application_id == app_id))
    for r in recognizers:
        session.add(AppRecognizer(
            application_id=app_id, name=r["name"], regex=r["regex"],
            score=r.get("score", 0.85), enabled=r.get("enabled", True)))
    for i, r in enumerate(rules):
        session.add(AppSensitivityRule(
            application_id=app_id, position=i, entities=r["entities"],
            sensitivity=r["sensitivity"], risk_class=r["risk_class"],
            residency=r["residency"],
            require_verification=r.get("require_verification", False)))


# --- model quality (routing quality ladder; Stage 2 calibration harness) ---

async def upsert_model_quality(
    session: AsyncSession,
    *,
    model_name: str,
    task_class: str | None = None,
    score: float,
    source: str = "customer",
    notes: str | None = None,
) -> ModelQuality:
    """Insert-or-replace an operator's `customer` quality declaration for
    `(model_name, task_class)` (caller commits).

    Customer rows only: `calibrated` rows are written exclusively by
    `write_calibration_set` (one set per `eval_run_id`, revertible as a unit),
    so a stray upsert can never create a calibrated row no set owns. Any other
    `source` raises `ValueError`."""
    if source != "customer":
        raise ValueError(
            f"upsert_model_quality writes customer rows only, got source={source!r} "
            f"(calibrated rows come from write_calibration_set)")
    stmt = select(ModelQuality).where(
        ModelQuality.model_name == model_name,
        ModelQuality.task_class.is_(task_class) if task_class is None
        else ModelQuality.task_class == task_class,
        ModelQuality.source == source,
    )
    row = (await session.execute(stmt)).scalar_one_or_none()
    if row is None:
        row = ModelQuality(id=uuid.uuid4().hex, model_name=model_name,
                           task_class=task_class, source=source, score=score)
        session.add(row)
    row.score = score
    row.calibrated_at = utcnow()
    row.eval_run_id = None
    row.notes = notes
    return row


# The DB-sourced quality provenances the routing ladder reads (see
# app/routing/quality.py::OVERRIDE_SOURCES). Rows with any other source are
# ignored.
_OVERRIDE_SOURCES = ("customer", "calibrated")


async def load_quality_overrides(
    session: AsyncSession,
) -> dict[tuple[str, str | None], dict[str, tuple[float, str | None]]]:
    """Keyed quality overrides for the routing quality ladder (P2b-3 §7).

    Maps `(model_name, task_class)` to `{source: (score, set_id)}` with at
    most one entry per source in `("customer", "calibrated")`. Customer and
    calibrated candidates are kept SEPARATE per key: precedence runs ACROSS
    keys (customer (m, wl) -> customer (m, None) -> calibrated (m, wl) ->
    calibrated (m, None)), so collapsing to one winner per key here would let
    a per-workload calibrated row beat a model-wide customer declaration.
    `QualityResolver.resolve` applies the order.

    `set_id` is the row's `eval_run_id` for a `calibrated` row and always
    `None` for a `customer` row. Among `calibrated` rows for the same key the
    newest `calibrated_at` wins (a later set supersedes an earlier one). Ids,
    enums and numbers only -- content-free.

    Selects over ALL rows (no `task_class IS NULL` filter) -- the table is
    small, so scanning both the scalar and per-workload rows in one query is
    fine.
    """
    rows = (await session.execute(select(ModelQuality))).scalars().all()
    best: dict[tuple[str, str | None, str], tuple[dt.datetime, float, str | None]] = {}
    for r in rows:
        if r.source not in _OVERRIDE_SOURCES:
            continue
        key = (r.model_name, r.task_class, r.source)
        set_id = r.eval_run_id if r.source == "calibrated" else None
        cur = best.get(key)
        if cur is None or r.calibrated_at > cur[0]:
            best[key] = (r.calibrated_at, r.score, set_id)
    out: dict[tuple[str, str | None], dict[str, tuple[float, str | None]]] = {}
    for (model, task_class, source), (_at, score, set_id) in best.items():
        out.setdefault((model, task_class), {})[source] = (score, set_id)
    return out


async def write_calibration_set(
    session: AsyncSession, *, set_id: str, units: list[dict],
) -> int:
    """Write one `model_quality` row per unit for a new calibration set
    (P2b-3 §6). Each unit dict has `model`, `workload` (`None` for the `(model,
    *)` unit) and `score`. Every row is written with `source="calibrated"`,
    `eval_run_id=set_id` and `notes="P2b-3 set"`. Returns the row count.

    Raises `ValueError` if `set_id` already exists (idempotent apply is the
    admin route's job, not this function's -- see spec §6). Caller commits.
    """
    existing = (await session.execute(
        select(ModelQuality.id).where(ModelQuality.eval_run_id == set_id).limit(1)
    )).scalar_one_or_none()
    if existing is not None:
        raise ValueError(f"calibration set already exists: {set_id}")
    count = 0
    for u in units:
        session.add(ModelQuality(
            id=uuid.uuid4().hex,
            model_name=u["model"],
            task_class=u.get("workload"),
            score=u["score"],
            source="calibrated",
            eval_run_id=set_id,
            notes="P2b-3 set",
        ))
        count += 1
    return count


async def list_calibration_sets(session: AsyncSession) -> list[dict]:
    """Applied calibration sets, newest first: `{set_id, applied_at, units}`
    grouped by `eval_run_id` over the `calibrated` rows (P2b-3 §6)."""
    stmt = (
        select(ModelQuality)
        .where(ModelQuality.source == "calibrated", ModelQuality.eval_run_id.is_not(None))
    )
    rows = (await session.execute(stmt)).scalars().all()
    groups: dict[str, dict] = {}
    for r in rows:
        g = groups.get(r.eval_run_id)
        if g is None:
            groups[r.eval_run_id] = {
                "set_id": r.eval_run_id, "applied_at": r.calibrated_at, "units": 1}
        else:
            g["units"] += 1
            if r.calibrated_at > g["applied_at"]:
                g["applied_at"] = r.calibrated_at
    return sorted(groups.values(), key=lambda g: g["applied_at"], reverse=True)


async def delete_calibration_set(session: AsyncSession, set_id: str) -> int:
    """Delete every `calibrated` row belonging to `set_id`. Returns the number
    of rows deleted (0 if the set does not exist). Customer rows and other
    calibration sets are untouched (P2b-3 §6). Caller commits."""
    ids = (await session.execute(
        select(ModelQuality.id).where(
            ModelQuality.eval_run_id == set_id, ModelQuality.source == "calibrated")
    )).scalars().all()
    if not ids:
        return 0
    await session.execute(delete(ModelQuality).where(ModelQuality.id.in_(ids)))
    return len(ids)


# --- onboarded models (runtime model registry; replaces infra_models/infra_settings) ---

_ONBOARDED_FIELDS = {
    "display_name", "provider", "tier", "quality", "price_input",
    "price_output", "price_cached_input", "endpoint", "api_key_env",
    "context_window", "context_window_source", "enabled", "allowed_app_ids",
    "probe_status", "probe_error", "notes",
    "region", "prompt_retention", "accepts_raw_sensitive"}


async def list_onboarded(session: AsyncSession) -> list[OnboardedModel]:
    return list((await session.execute(select(OnboardedModel))).scalars().all())


async def get_onboarded(session: AsyncSession, slug: str) -> OnboardedModel | None:
    return await session.get(OnboardedModel, slug)


async def upsert_onboarded(session: AsyncSession, slug: str, **fields) -> OnboardedModel:
    """Create or update one onboarded model's routing/pricing/endpoint metadata
    (caller commits)."""
    row = await session.get(OnboardedModel, slug)
    clean = {k: v for k, v in fields.items() if k in _ONBOARDED_FIELDS}
    if row is None:
        row = OnboardedModel(slug=slug, **clean)
        session.add(row)
    else:
        for k, v in clean.items():
            setattr(row, k, v)
    return row


async def delete_onboarded(session: AsyncSession, slug: str) -> bool:
    row = await session.get(OnboardedModel, slug)
    if row is None:
        return False
    await session.delete(row)
    return True
