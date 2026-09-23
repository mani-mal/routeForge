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
    Text,
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
    status: Mapped[str] = mapped_column(String(16), default="active")  # active|archived
    default_residency: Mapped[str] = mapped_column(String(32), default="any")

    # Per-app Langfuse project (Phase 5). secret key is encrypted at rest.
    langfuse_project_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    langfuse_public_key: Mapped[str | None] = mapped_column(String(128), nullable=True)
    langfuse_secret_key_enc: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    langfuse_host: Mapped[str | None] = mapped_column(String(255), nullable=True)

    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow)

    tokens: Mapped[list["AppToken"]] = relationship(
        back_populates="application", cascade="all, delete-orphan")
    policy: Mapped["AppPolicy | None"] = relationship(
        back_populates="application", uselist=False, cascade="all, delete-orphan")
    recognizers: Mapped[list["AppRecognizer"]] = relationship(
        cascade="all, delete-orphan")
    sensitivity_rules: Mapped[list["AppSensitivityRule"]] = relationship(
        cascade="all, delete-orphan")


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
    # Per-request cost cap (USD) for this app. null = inherit
    # policy.yaml budgets.default_max_spend_usd. Agent clients send 25K-token
    # turns, which a deployment-wide $0.05 cap rejects on any priced model.
    max_cost_usd_per_request: Mapped[float | None] = mapped_column(Float, nullable=True)
    max_rpm: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Per-app Headroom (step 5) override. null = inherit the live global toggle
    # (app.state.co_cfg.enabled); True/False = force on/off for this app. Only
    # ever gates whether compression is *attempted* — the residency/high-risk/
    # restricted bypass rules (config/serving.yaml) still fire on top, so an app
    # can never force compression onto content the safety rules exclude.
    compression_enabled: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    # Per-app eval-capture override (F1 Phase B). null = inherit the live global
    # switch (s.serving.observability.langfuse.capture_io). "off"|"redacted"|"full".
    # "full" is gated: only an app that is NOT onprem_only AND has its own
    # provisioned Langfuse project may resolve to it (write-time lint in
    # admin_routes.set_policy + runtime clamp in api/routes.py).
    capture_io: Mapped[str | None] = mapped_column(String(16), nullable=True)
    # Per-app protection posture. null = inherit the deployment-wide default
    # (serving.yaml policy.default_policy_mode). One of
    # app.policy.enums.POLICY_MODES (enforced|passthrough). "passthrough" turns
    # this app into router + meter + tracer only: no PII detection, no input or
    # egress redaction, no custom recognizers. It is a PROTECTION switch, never a
    # placement one — residency/onprem_only and allowed_providers are untouched
    # (CLAUDE.md rule 3 holds), and the deployment ceiling
    # serving.policy.allow_app_passthrough can forbid it install-wide.
    policy_mode: Mapped[str | None] = mapped_column(String(16), nullable=True)
    # Per-app PII-detection override. null = inherit live global
    # (s.serving.pii_detection). One of app.policy.enums.PII_MODES (off|regex|presidio).
    pii_detection: Mapped[str | None] = mapped_column(String(16), nullable=True)
    # Per-app streaming-mode override. null = inherit (reject inbound
    # stream:true per server.reject_stream_requests). One of
    # app.policy.enums.STREAM_MODES.
    stream_mode: Mapped[str | None] = mapped_column(String(24), nullable=True)
    # Per-app PII entity denylist: entity TYPES the gate detects but must NOT
    # redact for this app (false positives that mangle mandatory field values,
    # e.g. US_DRIVER_LICENSE on a shop-order number). null/[] = redact everything
    # detected. US_SSN can never be suppressed (FilteredDetector.PROTECTED_ENTITIES).
    # Narrows detection only; residency/onprem_only is unaffected (context.py).
    pii_entity_denylist: Mapped[list | None] = mapped_column(JSON, nullable=True)
    # Per-app minimum Presidio confidence [0,1]: detected entities scoring below
    # this are treated as non-matches (culls low-confidence recognizers). null =
    # no threshold. Never drops US_SSN (protected) or unscored regex/custom hits.
    pii_score_threshold: Mapped[float | None] = mapped_column(Float, nullable=True)
    # Per-app value-shape exemption: regexes describing the app's OWN identifier
    # formats (drawing/order numbers, etc.). Any detected entity whose VALUE
    # fullmatches a pattern is dropped BEFORE redaction/classification, regardless
    # of the entity TYPE Presidio assigned or surrounding whitespace -- the
    # type-independent complement to pii_entity_denylist. US_SSN can never be
    # exempted (FilteredDetector.PROTECTED_ENTITIES). null/[] = no exemption.
    pii_exempt_patterns: Mapped[list | None] = mapped_column(JSON, nullable=True)
    # --- P1a data-handling axes. null = inherit (resolved by
    # app/policy/compile.py). `policy_mode` remains a PRESET over
    # model_input/client_output; an explicit value here beats the preset, and
    # the allow_app_passthrough ceiling beats both.
    model_input: Mapped[str | None] = mapped_column(String(16), nullable=True)
    client_output: Mapped[str | None] = mapped_column(String(16), nullable=True)
    content_integrity: Mapped[str | None] = mapped_column(String(32), nullable=True)
    # Declared cross-axis rule switch: may a detected entity RAISE this app's
    # residency? null = true (today's behavior). false only ever prevents
    # tightening -- it can never lower the app's own residency floor.
    escalate_residency_on_detection: Mapped[bool | None] = mapped_column(
        Boolean, nullable=True)
    # --- P1b app-side data-handling REQUIREMENTS (design doc §3). null = no
    # constraint. Resolved explicit-or-null in app/policy/compile.py -- unlike
    # the P1a axes above, neither has a `policy_mode` preset or a deployment
    # default to fall back to. Placement-ADJACENT, not a protection axis: they
    # narrow WHERE a request may run (region) and WHAT retention promise an
    # endpoint must make, never what the gate does to the data. May only ever
    # NARROW the candidate set -- no value of either may widen
    # allowed_providers, clear onprem_only, or add a model (P1a noninterference
    # must survive). No enforcement lives here yet: Tasks 3-5 apply the three
    # exclusion rules against ModelRegistry.effective_pool()'s resolved
    # region/prompt_retention/accepts_raw_sensitive (Task 1). This task only
    # stores, resolves and validates the shape.
    allowed_regions: Mapped[list | None] = mapped_column(JSON, nullable=True)
    require_no_retention: Mapped[bool | None] = mapped_column(Boolean, nullable=True)

    application: Mapped[Application] = relationship(back_populates="policy")


class AppRecognizer(Base):
    """A customer-defined entity recognizer: a named regex whose matches are
    tagged with `name` (a custom entity type) and fed into classification."""
    __tablename__ = "app_recognizers"
    application_id: Mapped[str] = mapped_column(
        ForeignKey("applications.id", ondelete="CASCADE"), primary_key=True)
    name: Mapped[str] = mapped_column(String(32), primary_key=True)
    regex: Mapped[str] = mapped_column(Text)
    score: Mapped[float] = mapped_column(Float, default=0.85)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)


class AppSensitivityRule(Base):
    """Per-app classification rule. `position` orders rules most-severe-first,
    mirroring config/policy.yaml. Overrides the global sensitivity_rules for
    this application when any row exists."""
    __tablename__ = "app_sensitivity_rules"
    application_id: Mapped[str] = mapped_column(
        ForeignKey("applications.id", ondelete="CASCADE"), primary_key=True)
    position: Mapped[int] = mapped_column(Integer, primary_key=True)
    entities: Mapped[list] = mapped_column(JSON)
    sensitivity: Mapped[str] = mapped_column(String(16))
    risk_class: Mapped[str] = mapped_column(String(8))
    residency: Mapped[str] = mapped_column(String(24))
    require_verification: Mapped[bool] = mapped_column(Boolean, default=False)


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
    # Nullable with no default: this column is added to deployed databases by
    # scripts/migrate_app_policies.py, whose stated invariant is that every
    # column it adds is nullable with no default. Readers coerce NULL to 0.
    cached_prompt_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    quality: Mapped[float] = mapped_column(Float, default=0.0)
    quality_source: Mapped[str] = mapped_column(String(20), default="?")

    cost_usd: Mapped[float] = mapped_column(Float, default=0.0)
    baseline_usd: Mapped[float] = mapped_column(Float, default=0.0)
    latency_ms: Mapped[float] = mapped_column(Float, default=0.0)

    sensitivity: Mapped[str] = mapped_column(String(20), default="?")
    onprem_only: Mapped[bool] = mapped_column(Boolean, default=False)
    status: Mapped[str] = mapped_column(String(16), default="ok")  # ok|rejected|error


class EndpointObservation(Base):
    """One backend attempt's outcome (signal hardening, spec §3.1). The
    persisted half of the live SignalStore's input; boot seeding replays it.
    Endpoint health only -- no tenant, request id or content, by design."""

    __tablename__ = "endpoint_observations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), index=True)
    model: Mapped[str] = mapped_column(String(100), index=True)
    outcome: Mapped[str] = mapped_column(String(8))          # ok | error
    latency_ms: Mapped[float | None] = mapped_column(Float, nullable=True)
    streamed: Mapped[bool] = mapped_column(Boolean, default=False)
    ttft_ms: Mapped[float | None] = mapped_column(Float, nullable=True)


class RoutingOutcome(Base):
    """One routed request's outcome (P2b-2): what was eligible, what was tried,
    what was served, what it cost, and how it verified -- the substrate P2b-3
    learns from. CONTENT-FREE BY CONTRACT: ids, enums, numbers, entity TYPES
    and version hashes only; never prompt/completion text, reason strings or
    upstream error text (spec §4.3). A NEW table -> create_all, no migration."""

    __tablename__ = "routing_outcomes"

    request_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    application_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("applications.id", ondelete="CASCADE"), index=True)
    ts: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    trace_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    status: Mapped[str] = mapped_column(String(20))
    app_policy_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    endpoint_registry_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    routing_policy_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    workload: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    workload_source: Mapped[str] = mapped_column(String(8), default="none")
    tier_target: Mapped[str | None] = mapped_column(String(16), nullable=True)
    complexity: Mapped[float | None] = mapped_column(Float, nullable=True)
    prompt_tokens_bucket: Mapped[str | None] = mapped_column(String(8), nullable=True)
    sensitivity: Mapped[str | None] = mapped_column(String(20), nullable=True)
    risk_class: Mapped[str | None] = mapped_column(String(8), nullable=True)
    onprem_only: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    detected_entity_types: Mapped[list] = mapped_column(JSON, default=list)
    eligible: Mapped[list] = mapped_column(JSON, default=list)
    rejected: Mapped[list] = mapped_column(JSON, default=list)
    attempts: Mapped[list] = mapped_column(JSON, default=list)
    chosen: Mapped[str | None] = mapped_column(String(100), nullable=True)
    scoring: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    circuit_bypass: Mapped[bool] = mapped_column(Boolean, default=False)
    # True when a semantic-cache entry served the request: no router ran, so
    # `chosen`/`attempts` stay empty.
    cache_hit: Mapped[bool] = mapped_column(Boolean, default=False)
    cost_usd: Mapped[float] = mapped_column(Float, default=0.0)
    latency_ms: Mapped[float] = mapped_column(Float, default=0.0)
    prompt_tokens: Mapped[int] = mapped_column(Integer, default=0)
    completion_tokens: Mapped[int] = mapped_column(Integer, default=0)
    verify_required: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    verify_performed: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    verify_passed: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    verify_mode: Mapped[str | None] = mapped_column(String(16), nullable=True)


class OutcomeFeedback(Base):
    """A score attached to one routed request (P2b-2 §4.2). `request_id` is a
    LOGICAL link to routing_outcomes, not an FK, so pruning order never
    orphans or blocks. `application_id` is always the AUTHENTICATED caller's.
    `comment`/`corrected_output` text is never stored here -- it goes to the
    app's own Langfuse project exactly as before."""

    __tablename__ = "outcome_feedback"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    request_id: Mapped[str] = mapped_column(String(64), index=True)
    application_id: Mapped[str] = mapped_column(String(64), index=True)
    ts: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    score_name: Mapped[str] = mapped_column(String(32))
    value_num: Mapped[float | None] = mapped_column(Float, nullable=True)
    value_cat: Mapped[str | None] = mapped_column(String(100), nullable=True)
    source: Mapped[str] = mapped_column(String(8))   # app | admin


class RequestSnapshot(Base):
    """Durable, redaction-safe snapshot of one served request for the operator
    console (list/detail/stats/fleet + live stream rebuild). Replaces the
    process-local in-memory HistoryStore so the console is coherent across API
    replicas (B3). A bounded UI cache — UsageRecord remains the permanent
    metering record. `full_json` holds ONLY the already-redacted resp dict
    (raw prompt/completion popped before the record path; see CLAUDE.md rule 2)."""

    __tablename__ = "request_snapshots"

    request_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    application_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("applications.id", ondelete="CASCADE"), index=True)
    ts: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, index=True)
    summary_json: Mapped[dict] = mapped_column(JSON)
    full_json: Mapped[dict] = mapped_column(JSON)


class AdminUser(Base):
    """A dashboard admin (Phase 3). Password hashed with argon2."""

    __tablename__ = "admin_users"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    email: Mapped[str] = mapped_column(String(320), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow)


class ModelQuality(Base):
    """Per-model quality score with provenance, for the routing quality ladder.
    Written by the Stage 2 calibration harness (source=calibrated) or a
    customer-declared override (source=customer). A NEW table, so create_all
    handles it — no migration. This is model metadata, not a promotion/registry
    system (spec §5/§10). Not a copy of any request content — no PII."""

    __tablename__ = "model_quality"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    model_name: Mapped[str] = mapped_column(String(100), index=True)
    # Reserved for quality_by_class (Stage 3+); NULL = the scalar quality today.
    task_class: Mapped[str | None] = mapped_column(String(64), nullable=True)
    score: Mapped[float] = mapped_column(Float)
    source: Mapped[str] = mapped_column(String(20))  # customer | calibrated
    calibrated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow)
    eval_run_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    notes: Mapped[str | None] = mapped_column(String(255), nullable=True)


class OnboardedModel(Base):
    """A registered on-prem model routeForge routes to but never deploys.
    Self-sufficient: identity + routing + pricing + endpoint. Replaces the
    infra_models/infra_settings tables. NEW table -> create_all, no migration.
    See 2026-08-03-model-registry-onboarding-design.md."""

    __tablename__ = "onboarded_models"

    slug: Mapped[str] = mapped_column(String(100), primary_key=True)
    display_name: Mapped[str] = mapped_column(String(200))
    provider: Mapped[str] = mapped_column(String(32), default="onprem")
    tier: Mapped[str] = mapped_column(String(16))
    quality: Mapped[float | None] = mapped_column(Float, nullable=True)
    price_input: Mapped[float] = mapped_column(Float)
    price_output: Mapped[float] = mapped_column(Float)
    # USD/1M for prompt tokens this endpoint served from its own prompt cache.
    # NULL = undeclared -> cached tokens bill at `price_input` (no invented
    # discount). On-prem endpoints normally declare this EQUAL to price_input:
    # a prefix-cache hit saves latency, not GPU amortization.
    price_cached_input: Mapped[float | None] = mapped_column(Float, nullable=True)
    endpoint: Mapped[str] = mapped_column(String(255))
    api_key_env: Mapped[str | None] = mapped_column(String(64), nullable=True)
    context_window: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Where `context_window` came from: "operator" (a human typed it) or
    # "probed" (read from the endpoint's own /v1/models max_model_len). NULL
    # when context_window is NULL. Same honesty as the P1b data-handling
    # declarations: a number RouteForge inferred must not look like one the
    # operator asserted. (B50)
    context_window_source: Mapped[str | None] = mapped_column(String(16), nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    allowed_app_ids: Mapped[list | None] = mapped_column(JSON, nullable=True)
    # P1b endpoint data-handling declarations (docs/superpowers/specs/
    # 2026-09-19-endpoint-data-handling-p1b-design.md §2). These are
    # DECLARATIONS an operator records about this endpoint -- RouteForge
    # cannot verify a provider's contractual claim, only record what was
    # declared and enforce policy against that record. null on every field
    # means "not declared here"; ModelRegistry.effective_pool() resolves
    # null to the provider default (serving.yaml) or finally "unknown" --
    # "unknown" is the normal value for every endpoint onboarded before this
    # feature existed, never an error.
    region: Mapped[str | None] = mapped_column(String(64), nullable=True)
    prompt_retention: Mapped[str | None] = mapped_column(String(16), nullable=True)  # none|retained|unknown
    accepts_raw_sensitive: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    probe_status: Mapped[str] = mapped_column(String(16), default="unknown")
    probe_error: Mapped[str | None] = mapped_column(String(500), nullable=True)
    notes: Mapped[str | None] = mapped_column(String(255), nullable=True)
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow)


