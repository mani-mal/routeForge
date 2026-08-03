"""Async data-access functions for the application layer.

Every function takes an `AsyncSession`; the caller owns the transaction
(commit/rollback), which keeps these composable and easy to test.
"""
from __future__ import annotations

import datetime as dt
import uuid

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.apps.models import (
    Application, AppPolicy, AppRecognizer, AppSensitivityRule, AppToken,
    InfraModel, InfraSettings, ModelQuality, OnboardedModel, UsageRecord, utcnow,
)
from app.apps.privacy import AppPrivacyProfile, SensitivityRuleView
from app.apps.tokens import hash_token, token_prefix
from app.policy.recognizers import CustomRecognizer


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
        # eager-load the application AND its policy so callers can build an
        # AppPolicyView after the session closes (no lazy load in async).
        .options(selectinload(AppToken.application).selectinload(Application.policy))
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
               "langfuse_secret_key_enc"}
    for k, v in fields.items():
        if k in allowed:
            setattr(app, k, v)
    return app


async def upsert_policy(session: AsyncSession, app_id: str, **fields) -> AppPolicy:
    """Create or update an application's policy row (caller commits)."""
    policy = await session.get(AppPolicy, app_id)
    if policy is None:
        policy = AppPolicy(application_id=app_id)
        session.add(policy)
    allowed = {"allowed_tiers", "allowed_providers", "allowed_models",
               "residency", "max_spend_usd_per_day", "max_rpm",
               "compression_enabled"}
    for k, v in fields.items():
        if k in allowed:
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
    SQLite/Postgres) over the selected rows."""
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
    for r in ok:
        tier_dist[r.tier] = tier_dist.get(r.tier, 0) + 1
        model_dist[r.model] = model_dist.get(r.model, 0) + 1
        app_dist[r.application_id] = app_dist.get(r.application_id, 0) + 1
    latencies = sorted(r.latency_ms for r in ok)
    return {
        "count": len(ok),
        "rejected_count": sum(1 for r in rows if r.status == "rejected"),
        "total_usd": sum(r.cost_usd for r in ok),
        "baseline_usd": sum(r.baseline_usd for r in ok),
        "total_tokens": sum(r.total_tokens for r in ok),
        "prompt_tokens": sum(r.prompt_tokens for r in ok),
        "completion_tokens": sum(r.completion_tokens for r in ok),
        "onprem_count": sum(1 for r in ok if r.onprem_only),
        "tier_distribution": tier_dist,
        "model_distribution": model_dist,
        "application_distribution": app_dist,
        "latency_p50_ms": _pctile(latencies, 0.50),
        "latency_p95_ms": _pctile(latencies, 0.95),
    }


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
    source: str,
    eval_run_id: str | None = None,
    notes: str | None = None,
) -> ModelQuality:
    """Insert-or-replace the (model_name, task_class, source) score (caller commits)."""
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
    row.eval_run_id = eval_run_id
    row.notes = notes
    return row


# Provenance precedence for load_quality_overrides: customer wins over calibrated.
_QUALITY_SOURCE_RANK = {"customer": 2, "calibrated": 1}


async def load_quality_overrides(session: AsyncSession) -> dict[str, tuple[float, str]]:
    """One winning (score, source) per model_name across the scalar (task_class
    IS NULL) rows, customer beating calibrated. Feeds the router's override
    snapshot; empty until the Stage 2 harness writes rows."""
    stmt = select(ModelQuality).where(ModelQuality.task_class.is_(None))
    rows = (await session.execute(stmt)).scalars().all()
    best: dict[str, tuple[int, float, str]] = {}
    for r in rows:
        rank = _QUALITY_SOURCE_RANK.get(r.source, 0)
        cur = best.get(r.model_name)
        if cur is None or rank > cur[0]:
            best[r.model_name] = (rank, r.score, r.source)
    return {name: (score, source) for name, (_r, score, source) in best.items()}


# --- infra management (Run:ai + per-model engine; runtime/DB-backed fleet facts) ---

_INFRA_SETTINGS_FIELDS = {"runai_licensed", "runai_project", "default_engine"}
_INFRA_MODEL_FIELDS = {
    "engine", "enabled", "replicas", "min_replicas", "max_replicas",
    "tensor_parallel", "gpu_type", "endpoint", "runai_scheduled", "notes"}


async def get_infra_settings(session: AsyncSession) -> InfraSettings | None:
    return await session.get(InfraSettings, "cluster")


async def upsert_infra_settings(session: AsyncSession, **fields) -> InfraSettings:
    """Create or update the cluster singleton (caller commits)."""
    row = await session.get(InfraSettings, "cluster")
    if row is None:
        row = InfraSettings(id="cluster")
        session.add(row)
    for k, v in fields.items():
        if k in _INFRA_SETTINGS_FIELDS:
            setattr(row, k, v)
    return row


async def list_infra_models(session: AsyncSession) -> list[InfraModel]:
    stmt = select(InfraModel).order_by(InfraModel.model_name)
    return list((await session.execute(stmt)).scalars().all())


async def get_infra_model(session: AsyncSession, model_name: str) -> InfraModel | None:
    return await session.get(InfraModel, model_name)


async def upsert_infra_model(session: AsyncSession, model_name: str, **fields) -> InfraModel:
    """Create or update one on-prem model's serving metadata (caller commits)."""
    row = await session.get(InfraModel, model_name)
    if row is None:
        row = InfraModel(model_name=model_name)
        session.add(row)
    for k, v in fields.items():
        if k in _INFRA_MODEL_FIELDS:
            setattr(row, k, v)
    return row


# --- onboarded models (runtime model registry; replaces infra_models/infra_settings) ---

_ONBOARDED_FIELDS = {
    "display_name", "provider", "tier", "quality", "price_input",
    "price_output", "endpoint", "api_key_env", "context_window", "enabled",
    "allowed_app_ids", "probe_status", "probe_error", "notes"}


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
