"""SQLAlchemy ORM models for the application layer.

All five tables are defined now (schema-complete) even though Phase 1 only
exercises applications / tokens / usage — there is no migration tool yet, so
`Base.metadata.create_all` is the single source of the schema. Later phases fill
in policy, admin auth, and per-app Langfuse columns without a migration.

Never stores prompt/answer text — usage rows keep the same redaction guarantee
as the rest of the system (types only, no PII, no CCR contents).
"""
from __future__ import annotations

import datetime as dt

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    LargeBinary,
    String,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.apps.db import Base


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


class Application(Base):
    """A client application (a.k.a. tenant). `id` is a stable slug used as the
    `application_id` on traces and usage rows and as the key into
    `policy.yaml` tenants."""

    __tablename__ = "applications"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    owner_email: Mapped[str | None] = mapped_column(String(320), nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="active")  # active|disabled
    default_residency: Mapped[str] = mapped_column(String(32), default="any")

    # Per-app Langfuse project (Phase 5). secret key is encrypted at rest.
    langfuse_project_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    langfuse_public_key: Mapped[str | None] = mapped_column(String(128), nullable=True)
    langfuse_secret_key_enc: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)

    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow)

    tokens: Mapped[list["AppToken"]] = relationship(
        back_populates="application", cascade="all, delete-orphan")
    policy: Mapped["AppPolicy | None"] = relationship(
        back_populates="application", uselist=False, cascade="all, delete-orphan")


class AppToken(Base):
    """An API token for an application. Only the sha256 hash is stored; the
    plaintext is shown once at creation."""

    __tablename__ = "app_tokens"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    application_id: Mapped[str] = mapped_column(
        ForeignKey("applications.id", ondelete="CASCADE"), index=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    prefix: Mapped[str] = mapped_column(String(32))  # display only
    name: Mapped[str] = mapped_column(String(100), default="default")
    status: Mapped[str] = mapped_column(String(16), default="active")  # active|revoked
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow)
    last_used_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True)

    application: Mapped[Application] = relationship(back_populates="tokens")


class AppPolicy(Base):
    """Per-application routing/quota policy (Phase 2). null lists/values mean
    'no restriction'; the global privacy rules always still apply on top."""

    __tablename__ = "app_policies"

    application_id: Mapped[str] = mapped_column(
        ForeignKey("applications.id", ondelete="CASCADE"), primary_key=True)
    allowed_tiers: Mapped[list | None] = mapped_column(JSON, nullable=True)
    allowed_providers: Mapped[list | None] = mapped_column(JSON, nullable=True)
    allowed_models: Mapped[list | None] = mapped_column(JSON, nullable=True)
    residency: Mapped[str | None] = mapped_column(String(32), nullable=True)
    max_spend_usd_per_day: Mapped[float | None] = mapped_column(Float, nullable=True)
    max_rpm: Mapped[int | None] = mapped_column(Integer, nullable=True)

    application: Mapped[Application] = relationship(back_populates="policy")


class UsageRecord(Base):
    """One row per served (or rejected) request. Durable replacement for the
    in-memory HistoryStore; feeds per-app dashboards and budget enforcement."""

    __tablename__ = "usage_records"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    application_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("applications.id", ondelete="CASCADE"), index=True)
    request_id: Mapped[str] = mapped_column(String(64), index=True)
    ts: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, index=True)

    model: Mapped[str] = mapped_column(String(100), default="?")
    provider: Mapped[str] = mapped_column(String(50), default="?")
    tier: Mapped[str] = mapped_column(String(20), default="?")

    prompt_tokens: Mapped[int] = mapped_column(Integer, default=0)
    completion_tokens: Mapped[int] = mapped_column(Integer, default=0)
    total_tokens: Mapped[int] = mapped_column(Integer, default=0)

    cost_usd: Mapped[float] = mapped_column(Float, default=0.0)
    baseline_usd: Mapped[float] = mapped_column(Float, default=0.0)
    latency_ms: Mapped[float] = mapped_column(Float, default=0.0)

    sensitivity: Mapped[str] = mapped_column(String(20), default="?")
    onprem_only: Mapped[bool] = mapped_column(Boolean, default=False)
    status: Mapped[str] = mapped_column(String(16), default="ok")  # ok|rejected|error


class AdminUser(Base):
    """A dashboard admin (Phase 3). Password hashed with argon2."""

    __tablename__ = "admin_users"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    email: Mapped[str] = mapped_column(String(320), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow)
