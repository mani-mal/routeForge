"""Admin API (Phase 3): applications, tokens, per-app policy, usage.

All routes except /admin/login require an admin session cookie. This surface is
separate from the token-authed serving API (/v1/*) — different auth, different
audience. The dashboard (Phase 4) is the only intended client.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import re
import time

from fastapi import APIRouter, Body, Depends, HTTPException, Request, Response
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field, field_validator

from app.apps import store
from app.apps.admin_auth import SESSION_COOKIE, require_admin
from app.apps.calibration_sets import CalibrationError, validate_recommendation
from app.apps.langfuse_provision import LangfuseProvisioner, encrypt_secret
from app.apps.models import AppToken, OutcomeFeedback, RoutingOutcome
from app.apps.policy_lint import price_warnings
from app.apps.probe import probe_endpoint
from app.apps.policy import AppPolicyView, policy_view_from
from app.apps.snapshot import build_config_snapshot
from app.apps.tokens import new_service_credential, new_token
from app.api.routes import _sanitize_session_id
from app.policy.compile import (PolicyViolation, check_invariants, compile_policy,
                                validate_effective)
from app.policy.effective import PROTECTED_ENTITIES
from app.policy.enums import (
    PII_MODES,
    POLICY_MODES,
    PROMPT_RETENTION_VALUES,
    RESIDENCY,
    RISK_CLASS,
    SENSITIVITY,
    STREAM_MODES,
)
from app.policy.recognizers import PRESIDIO_BUILTINS, validate_recognizer
from app.policy.trajectory import TRAJECTORY_MODES

admin_router = APIRouter(prefix="/admin", tags=["admin"])


# --- serialization helpers --------------------------------------------------

def _iso(v: dt.datetime | None) -> str | None:
    return v.isoformat() if v is not None else None


def _token_dict(t) -> dict:
    # An active token past its `expires_at` is reported as "expired": it is
    # refused exactly like a revoked one, so it must not count as active.
    status = "expired" if t.status == "active" and store.token_expired(t) else t.status
    return {"id": t.id, "prefix": t.prefix, "name": t.name, "status": status,
            "created_at": _iso(t.created_at), "last_used_at": _iso(t.last_used_at),
            "revoked_at": _iso(t.revoked_at), "expires_at": _iso(t.expires_at and store._to_utc(t.expires_at)),
            "session": t.session_binding, "issued_by": t.issued_by}


def _service_credential_dict(c) -> dict:
    return {"id": c.id, "application_id": c.application_id, "prefix": c.prefix,
            "name": c.name, "status": c.status, "created_at": _iso(c.created_at),
            "last_used_at": _iso(c.last_used_at), "revoked_at": _iso(c.revoked_at)}


def _policy_dict(p) -> dict | None:
    if p is None:
        return None
    return {"allowed_tiers": p.allowed_tiers, "allowed_providers": p.allowed_providers,
            "allowed_models": p.allowed_models, "residency": p.residency,
            "max_spend_usd_per_day": p.max_spend_usd_per_day,
            "max_cost_usd_per_request": p.max_cost_usd_per_request, "max_rpm": p.max_rpm,
            "compression_enabled": p.compression_enabled, "capture_io": p.capture_io,
            "pii_detection": p.pii_detection, "stream_mode": p.stream_mode,
            "trajectory_mode": p.trajectory_mode,
            "policy_mode": p.policy_mode,
            "pii_entity_denylist": p.pii_entity_denylist,
            "pii_score_threshold": p.pii_score_threshold,
            "pii_exempt_patterns": p.pii_exempt_patterns}


def _onboarded_dict(m) -> dict:
    return {"slug": m.slug, "display_name": m.display_name, "provider": m.provider,
            "tier": m.tier, "quality": m.quality, "price_input": m.price_input,
            "price_output": m.price_output,
            "price_cached_input": m.price_cached_input, "endpoint": m.endpoint,
            "api_key_env": m.api_key_env, "context_window": m.context_window,
            "context_window_source": m.context_window_source,
            "enabled": m.enabled, "allowed_app_ids": m.allowed_app_ids,
            "probe_status": m.probe_status, "probe_error": m.probe_error,
            "notes": m.notes, "updated_at": _iso(m.updated_at),
            # P1b declarations (design §2/§8) -- the STORED, raw values (not
            # resolved: no provider-default fallback, no "unknown" backfill).
            # `ModelRegistry.effective_pool()` is what resolves these for
            # routing; this is what the operator declared for THIS row, so
            # the form can render exactly what was saved.
            "region": m.region, "prompt_retention": m.prompt_retention,
            "accepts_raw_sensitive": m.accepts_raw_sensitive}


def _circuit_dict(st, now: float) -> dict:
    """Step-7 breaker state (B45) for one onboarded model row. `now` is a
    single `time.monotonic()` snapshot taken once per list call so every
    row's `open_until_s` is relative to the same instant. Per-process, like
    the breaker itself (app/gateway/breaker.py) -- THIS REPLICA's view only;
    another API replica may disagree.

    `open_until_s` is only meaningful while still counting DOWN to a retry --
    i.e. `state == "open"`. `half_open` keeps the same `open_until` timestamp
    internally (it is how the breaker recognizes the cooldown has elapsed),
    but that is now a moment in the past: surfacing it as "seconds from now"
    would render a negative number ("retry in -12s"), which reads as a bug
    rather than "already admitted". Clamp to None for every other state."""
    open_until_s = (st.open_until - now) if st.state == "open" else None
    return {"state": st.state, "consecutive_failures": st.consecutive_failures,
            "open_until_s": open_until_s}


def _effective_dict(app, settings) -> dict:
    """The compiled `EffectivePolicy`'s axis VALUES only -- no sources, no
    cross-axis rules, no version; the certificate (Task 9) already carries
    those on a served REQUEST. This is the fix for Task 9 review round 1's
    Important finding: `ui/src/admin/AppSettings.tsx` used to re-derive this
    same precedence client-side (`effDetectionFor`) and it drifted the moment
    a rung was added server-side -- `compile_policy`'s `allow_pii_downgrade`
    ceiling (app/policy/compile.py ~line 309) reassigns `detection` back to
    the deployment default whenever the ceiling is false and the resolved
    value is off/regex, INCLUDING the `passthrough` preset case, not just an
    explicit per-app override. A client-side mirror had no way to learn that
    without literally re-implementing `compile_policy`, and it fell behind.

    The fix is server-driven, not a better mirror: hand the client the
    already-compiled values and let it read them. `compile_policy` is pure
    and cheap (no DB, no I/O -- see its own docstring), so recomputing it on
    every app read is free; this is the one place it runs for the admin
    surface, and every `_app_dict` caller gets it, so GET and the PUT
    response can never disagree about what the app's policy actually
    resolves to."""
    eff = compile_policy(policy_view_from(app), settings)
    return {
        "model_input": eff.protection.model_input,
        "client_output": eff.protection.client_output,
        "telemetry_io": eff.protection.telemetry_io,
        "detection": eff.protection.detection,
        "content_integrity": eff.protection.content_integrity,
        # P1b app-side data-handling requirements (design doc §3). Server-
        # resolved so the console never re-derives normalisation itself.
        "allowed_regions":
            sorted(eff.allowed_regions) if eff.allowed_regions else None,
        "require_no_retention": eff.require_no_retention,
    }


def _app_dict(app, *, with_children: bool = True, settings=None) -> dict:
    d = {"id": app.id, "name": app.name, "owner_email": app.owner_email,
         "status": app.status, "default_residency": app.default_residency,
         "langfuse_project_id": app.langfuse_project_id,
         "langfuse_host": app.langfuse_host,
         "has_langfuse_keys": bool(app.langfuse_public_key and app.langfuse_secret_key_enc),
         "created_at": _iso(app.created_at)}
    if with_children:
        d["policy"] = _policy_dict(app.policy)
        d["tokens"] = [_token_dict(t) for t in sorted(
            app.tokens, key=lambda t: t.created_at or dt.datetime.min)]
        # `settings` is optional only so a caller with no deployment context
        # (there is none in this codebase today) degrades to omitting the
        # block rather than crashing -- every real call site below passes it.
        if settings is not None:
            d["effective"] = _effective_dict(app, settings)
    return d


# --- request models ---------------------------------------------------------

class LoginIn(BaseModel):
    email: str
    password: str


class CreateAppIn(BaseModel):
    id: str
    name: str
    owner_email: str | None = None
    default_residency: str = "any"


class UpdateAppIn(BaseModel):
    name: str | None = None
    owner_email: str | None = None
    status: str | None = None
    default_residency: str | None = None


# pii_exempt_patterns caps (ReDoS/DoS guard on operator-supplied regex).
MAX_EXEMPT_PATTERNS = 25
MAX_EXEMPT_PATTERN_LEN = 200


class PolicyIn(BaseModel):
    allowed_tiers: list[str] | None = None
    allowed_providers: list[str] | None = None
    allowed_models: list[str] | None = None
    residency: str | None = None
    max_spend_usd_per_day: float | None = None
    max_cost_usd_per_request: float | None = None  # None = inherit budgets.default_max_spend_usd
    max_rpm: int | None = None
    compression_enabled: bool | None = None   # None = inherit global Headroom toggle
    capture_io: str | None = None   # None = inherit global; off|redacted|full
    pii_detection: str | None = None   # None = inherit global; off|regex|presidio
    stream_mode: str | None = None   # None = inherit (reject inbound stream); see STREAM_MODES
    # Protection posture, independent of `residency` (which is placement only).
    # None = inherit serving.policy.default_policy_mode; enforced|passthrough.
    policy_mode: str | None = None
    # Narrow the gate WITHOUT disabling it: entity types to detect-but-not-redact
    # for this app (false positives that mangle mandatory field values), and a
    # minimum Presidio confidence below which a match is dropped. US_SSN can
    # never be denylisted (rules 3/8). See docs/reference/per-app-config.md.
    pii_entity_denylist: list[str] | None = None
    pii_score_threshold: float | None = None
    # Type-independent complement to pii_entity_denylist: regexes over the
    # detected entity's VALUE (fullmatch). US_SSN can never be exempted
    # (rules 3/8). See docs/reference/per-app-config.md.
    pii_exempt_patterns: list[str] | None = None
    # P1a data-handling axes (types only here -- validation is Task 3).
    # None = inherit; see app/policy/compile.py for how these resolve.
    model_input: str | None = None
    client_output: str | None = None
    content_integrity: str | None = None
    escalate_residency_on_detection: bool | None = None
    # B72: off | header | sticky (app/policy/trajectory.py). None = inherit (off).
    trajectory_mode: str | None = None
    # P1b app-side data-handling requirements (design doc §3; types only here
    # -- shape/normalisation validation is app/policy/compile.py::
    # validate_effective, like every other axis). None = no constraint. No
    # enforcement yet (Tasks 3-5 own narrowing the candidate set).
    allowed_regions: list[str] | None = None
    require_no_retention: bool | None = None
    # --- the one escape from the merge -------------------------------------
    # This PUT is a MERGE: `null` means "leave the stored value alone", which
    # plenty of callers depend on. That left no way to put a field BACK to NULL
    # ("inherit the deployment default") once it had been set — an operator who
    # set pii_score_threshold=0.85 or policy_mode=passthrough was stuck with it.
    # `reset` names the fields to NULL out, applied after the merge. It is
    # additive and explicit: merge semantics are unchanged, and a field is only
    # cleared when it is named here. Names are validated against
    # `store.POLICY_FIELDS` (422 on an unknown name — never silently ignored),
    # and naming a field here AND giving it a non-null value in the same request
    # is a 422 rather than a silent precedence rule. Every clamp below keys on
    # the post-RESET effective value, so a reset can never sneak an app into a
    # posture the clamps forbid.
    reset: list[str] | None = None


class LangfuseCredsIn(BaseModel):
    # Every field is load-bearing for the capture_io='full' eligibility check
    # (`load_app_langfuse(app) is not None`, `has_langfuse_keys`). A blank or
    # whitespace-only value would still encrypt to a non-empty Fernet token and
    # silently unlock 'full' capture with no valid sink — reject fail-closed.
    host: str = Field(min_length=1)
    public_key: str = Field(min_length=1)
    secret_key: str = Field(min_length=1)
    project_id: str = Field(min_length=1)

    @field_validator("host", "public_key", "secret_key", "project_id")
    @classmethod
    def _not_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("must not be blank")
        return v

    @field_validator("host")
    @classmethod
    def _host_is_url(cls, v: str) -> str:
        # Mirror onboard_model's endpoint check (~admin_routes.py:431).
        if not re.match(r"^https?://", v):
            raise ValueError("host must be an http(s) URL")
        return v


# Longest lifetime a caller may ask for with `expires_in_s` (30 days). An
# attempt-scoped token lives for one attempt; anything longer-lived is an
# administrator's ordinary, non-expiring token.
MAX_TOKEN_EXPIRES_IN_S = 30 * 24 * 3600


class TokenIn(BaseModel):
    name: str = "default"
    # Optional: refused (like a revoked token) this many seconds after creation.
    expires_in_s: int | None = Field(default=None, ge=1, le=MAX_TOKEN_EXPIRES_IN_S)
    # Optional: refused unless the call's `x-routeforge-session` equals this.
    session: str | None = None

    @field_validator("session")
    @classmethod
    def _session_is_a_valid_header_value(cls, v: str | None) -> str | None:
        # Must be a value `x-routeforge-session` can actually carry, or the
        # token could never authenticate.
        if v is not None and _sanitize_session_id(v) != v:
            raise ValueError("session must be 1-128 chars of [A-Za-z0-9._:-]")
        return v


class ServiceCredentialIn(BaseModel):
    name: str = "default"


class OnboardIn(BaseModel):
    slug: str
    display_name: str
    tier: str                       # cheap|mid|frontier
    endpoint: str
    api_key_env: str | None = None
    price_input: float
    price_output: float
    # USD/1M for prompt tokens this endpoint served from its own prompt
    # cache. None = undeclared -> price_for bills cached tokens at the full
    # price_input rate (no invented discount). Like accepts_raw_sensitive
    # below (not like region/prompt_retention), this field does NOT
    # full-replace on omission: an omitted field PRESERVES the stored value
    # and only an explicit null CLEARS it -- see `_resolved_price_cached_input`.
    # A price silently reverting to "no discount" on an unrelated edit would
    # overstate spend with no operator action.
    price_cached_input: float | None = None
    quality: float | None = None
    context_window: int | None = None
    allowed_app_ids: list[str] | None = None
    notes: str | None = None
    enabled: bool = True
    # P1b endpoint data-handling declarations (design §2/§8; Task 1's DB
    # columns and store.upsert_onboarded already accept these -- this is the
    # admin-HTTP wiring that was missing). None = undeclared ("unknown" at
    # resolution time -- app/policy/feasible.py::_declared reads None the
    # same way it reads an explicit "unknown"). `accepts_raw_sensitive` is a
    # JSON boolean here, matching the DB column -- the YAML pool's string
    # token form ("true"/"false"/"unknown") is a different surface (Task 1's
    # concern note) and not this one.
    # Free customer vocabulary (design §2) -- deliberately unvalidated (no
    # RouteForge-owned geography enum), so the literal string "unknown"
    # typed into the form is a legal, storable value here and reads back
    # indistinguishable from an omitted/null region: both resolve to
    # "unknown" at app/policy/feasible.py::_declared. Not worth guarding
    # against -- same resolved behaviour either way -- just a trap for a
    # reader who expects the stored string to prove which one happened.
    region: str | None = None
    prompt_retention: str | None = None       # none|retained|unknown, or None (undeclared)
    # JSON boolean; None (undeclared). UNLIKE every other optional field on
    # this route (including region/prompt_retention above), an OMITTED
    # accepts_raw_sensitive on a PUT/re-POST does NOT full-replace to None --
    # see `_resolved_accepts_raw_sensitive` below for why, and do not "fix"
    # this back into consistency with the other fields.
    accepts_raw_sensitive: bool | None = None


class RecognizerIn(BaseModel):
    name: str
    regex: str
    score: float = 0.85
    enabled: bool = True


class RuleIn(BaseModel):
    entities: list[str]
    sensitivity: str
    risk_class: str
    residency: str
    require_verification: bool = False


class PrivacyProfileIn(BaseModel):
    recognizers: list[RecognizerIn] = []
    rules: list[RuleIn] = []


def _validate_profile(body: PrivacyProfileIn) -> None:
    """Whole-profile validation, run BEFORE any persistence. Raises ValueError
    (caught by the caller -> 422) on the first violation found."""
    if len(body.recognizers) > 32:
        raise ValueError("too many recognizers (max 32)")
    if len(body.rules) > 64:
        raise ValueError("too many rules (max 64)")
    names: set[str] = set()
    for r in body.recognizers:
        if not (0.0 <= r.score <= 1.0):
            raise ValueError(f"recognizer {r.name!r} score {r.score!r} out of range [0.0, 1.0]")
        validate_recognizer(r.name, r.regex)  # raises ValueError
        if r.name in names:
            raise ValueError(f"duplicate recognizer {r.name!r}")
        names.add(r.name)
    known = PRESIDIO_BUILTINS | names
    for rule in body.rules:
        if rule.sensitivity not in SENSITIVITY:
            raise ValueError(f"bad sensitivity {rule.sensitivity!r}")
        if rule.risk_class not in RISK_CLASS:
            raise ValueError(f"bad risk_class {rule.risk_class!r}")
        if rule.residency not in RESIDENCY:
            raise ValueError(f"bad residency {rule.residency!r}")
        for e in rule.entities:
            if e not in known:
                raise ValueError(f"unknown entity {e!r}")


# --- auth endpoints ---------------------------------------------------------

@admin_router.post("/login")
async def login(body: LoginIn, request: Request, response: Response) -> dict:
    from app.apps.admin_auth import authenticate
    async with request.app.state.db.session() as session:
        admin = await authenticate(session, body.email, body.password)
        if admin is not None:
            await session.commit()  # nothing to persist, but keep the pattern
    if admin is None:
        raise HTTPException(401, "invalid credentials")
    sid = request.app.state.admin_sessions.create(admin.id)
    response.set_cookie(SESSION_COOKIE, sid, httponly=True, samesite="lax")
    return {"email": admin.email}


@admin_router.post("/logout")
async def logout(request: Request, response: Response) -> dict:
    request.app.state.admin_sessions.delete(request.cookies.get(SESSION_COOKIE))
    response.delete_cookie(SESSION_COOKIE)
    return {"ok": True}


@admin_router.get("/me")
async def me(request: Request, admin_id: str = Depends(require_admin)) -> dict:
    """Session identity plus the deployment's read-only policy envelope.

    The console needs the second half to be honest about two things it
    otherwise has to guess at. (1) A per-app field left null inherits a
    deployment default, and a control that renders that as a bare
    "(global default)" tells the operator nothing — it can now name the
    value being inherited. (2) The deployment ceilings decide whether a
    posture is reachable AT ALL; without them the UI can only offer
    `passthrough` and let the write fail with a 422.

    Deliberately read-only. These live in `serving.yaml` (the operator's
    ConfigMap) and constrain what an admin may do — an admin-editable
    ceiling would not be a ceiling. Nothing here is per-tenant or secret:
    it is the same envelope every app in this deployment sits inside.
    """
    s = request.app.state.settings
    return {
        "admin_id": admin_id,
        "deployment": {
            "default_policy_mode": s.serving.default_policy_mode,
            "default_pii_detection": s.serving.pii_detection,
            "default_capture_io": s.serving.observability.langfuse.capture_io,
            "reject_stream_requests": s.serving.reject_stream_requests,
            "allow_app_passthrough": s.serving.allow_app_passthrough,
            "allow_app_pii_downgrade": s.serving.allow_app_pii_downgrade,
        },
    }


# --- applications -----------------------------------------------------------

@admin_router.get("/applications")
async def list_applications(request: Request, _: str = Depends(require_admin)) -> list:
    async with request.app.state.db.session() as session:
        out = []
        for app in await store.list_applications(session):
            full = await store.get_application_with_policy(session, app.id)
            out.append(_app_dict(full, settings=request.app.state.settings))
        return out


@admin_router.post("/applications")
async def create_application(body: CreateAppIn, request: Request,
                             _: str = Depends(require_admin)) -> dict:
    # Validated against the enum like every other placement/posture field. It
    # is not cosmetic: a policy row with a null `residency` inherits
    # `app.default_residency` (see `policy_view_from`), so a typo here becomes
    # that app's placement. Unvalidated, it was the way an unparseable
    # residency reached the request path at all.
    if body.default_residency not in RESIDENCY:
        raise HTTPException(422, f"bad default_residency {body.default_residency!r}")
    async with request.app.state.db.session() as session:
        if await store.get_application(session, body.id) is not None:
            raise HTTPException(409, f"application {body.id!r} already exists")
        await store.create_application(
            session, app_id=body.id, name=body.name, owner_email=body.owner_email,
            default_residency=body.default_residency)
        # Issue one initial token and return the plaintext ONCE.
        raw = new_token()
        await store.add_token(session, application_id=body.id, raw_token=raw,
                              name="default")
        # Best-effort: provision a dedicated Langfuse project so the owner sees
        # only their own traces. No-op (and never fails app creation) when the
        # org API isn't configured — traces then fall back to the shared project.
        provisioned = await _maybe_provision_langfuse(body.id, body.name)
        if provisioned is not None:
            await store.update_application(
                session, body.id,
                langfuse_project_id=provisioned.project_id,
                langfuse_public_key=provisioned.public_key,
                langfuse_secret_key_enc=encrypt_secret(provisioned.secret_key))
        await session.commit()
        full = await store.get_application_with_policy(session, body.id)
        return {"application": _app_dict(full, settings=request.app.state.settings),
                "token": raw}


async def _maybe_provision_langfuse(app_id: str, name: str):
    try:
        return await LangfuseProvisioner().provision(app_id, name)
    except Exception:  # provisioning must never break app creation
        return None


@admin_router.get("/applications/{app_id}")
async def get_application(app_id: str, request: Request,
                          _: str = Depends(require_admin)) -> dict:
    async with request.app.state.db.session() as session:
        full = await store.get_application_with_policy(session, app_id)
        if full is None:
            raise HTTPException(404, "application not found")
        return _app_dict(full, settings=request.app.state.settings)


@admin_router.patch("/applications/{app_id}")
async def update_application(app_id: str, body: UpdateAppIn, request: Request,
                            _: str = Depends(require_admin)) -> dict:
    # Same enum check as on create: this PATCH is the other door to
    # `app.default_residency`, and `store.update_application` whitelists the
    # column without validating it.
    if (body.default_residency is not None
            and body.default_residency not in RESIDENCY):
        raise HTTPException(422, f"bad default_residency {body.default_residency!r}")
    async with request.app.state.db.session() as session:
        app = await store.update_application(
            session, app_id, **body.model_dump(exclude_none=True))
        if app is None:
            raise HTTPException(404, "application not found")
        await session.commit()
        full = await store.get_application_with_policy(session, app_id)
        return _app_dict(full, settings=request.app.state.settings)


@admin_router.put("/applications/{app_id}/langfuse")
async def set_langfuse_creds(app_id: str, body: LangfuseCredsIn, request: Request,
                             _: str = Depends(require_admin)) -> dict:
    """Manual per-app Langfuse creds for an air-gapped operator who can't reach
    the org-scoped provisioning API (B15). Stored the same way auto-provisioning
    stores them (secret encrypted at rest via `encrypt_secret`), so once set,
    `load_app_langfuse(app)` returns non-None and the app becomes
    capture_io='full'-eligible under the existing Phase-B lint in `set_policy`.
    """
    async with request.app.state.db.session() as session:
        app = await store.update_application(
            session, app_id,
            langfuse_host=body.host, langfuse_public_key=body.public_key,
            langfuse_project_id=body.project_id,
            langfuse_secret_key_enc=encrypt_secret(body.secret_key))
        if app is None:
            raise HTTPException(404, "application not found")
        await session.commit()
        full = await store.get_application_with_policy(session, app_id)
        return _app_dict(full, settings=request.app.state.settings)


@admin_router.put("/applications/{app_id}/policy")
async def set_policy(app_id: str, body: PolicyIn, request: Request,
                     _: str = Depends(require_admin)) -> dict:
    async with request.app.state.db.session() as session:
        app = await store.get_application_with_policy(session, app_id)
        if app is None:
            raise HTTPException(404, "application not found")

        # --- `reset`: validate the names before anything else looks at them.
        # store.POLICY_FIELDS is the authority on what a policy column is; an
        # unknown name is a typo that would otherwise silently leave the field
        # set, so it is refused rather than dropped.
        reset_fields = set(body.reset or ())
        unknown = sorted(reset_fields - store.POLICY_FIELDS)
        if unknown:
            raise HTTPException(
                422, f"reset names unknown policy field(s) {unknown}; legal fields: "
                f"{sorted(store.POLICY_FIELDS)}")
        # Naming a field in `reset` AND sending a value for it in the same
        # request is a contradiction ("clear it" + "set it to X"). Refuse it
        # instead of inventing a precedence rule: either order would be a silent
        # guess at intent on the exact fields (policy_mode, capture_io) where
        # guessing wrong changes the app's protection posture, and a UI that
        # emits both is buggy in a way the operator must be told about.
        conflicting = sorted(f for f in reset_fields if getattr(body, f, None) is not None)
        if conflicting:
            raise HTTPException(
                422, f"reset names field(s) {conflicting} that also carry a value in "
                "this request. Send either the value (to set it) or the name in "
                "`reset` (to clear it back to the deployment default), not both.")

        if body.residency is not None and body.residency not in RESIDENCY:
            raise HTTPException(422, f"bad residency {body.residency!r}")
        if body.pii_detection is not None and body.pii_detection not in PII_MODES:
            raise HTTPException(422, f"bad pii_detection {body.pii_detection!r}")
        if body.stream_mode is not None and body.stream_mode not in STREAM_MODES:
            raise HTTPException(422, f"bad stream_mode {body.stream_mode!r}")
        if body.trajectory_mode is not None and body.trajectory_mode not in TRAJECTORY_MODES:
            raise HTTPException(422, f"bad trajectory_mode {body.trajectory_mode!r}")
        if body.policy_mode is not None and body.policy_mode not in POLICY_MODES:
            raise HTTPException(422, f"bad policy_mode {body.policy_mode!r}")
        if body.pii_entity_denylist is not None:
            for ent in body.pii_entity_denylist:
                if ent in PROTECTED_ENTITIES:
                    raise HTTPException(
                        422, f"{ent!r} cannot be denylisted: it carries an absolute "
                        "redaction guarantee (CLAUDE.md rules 3/8)")
                if ent not in PRESIDIO_BUILTINS:
                    raise HTTPException(
                        422, f"unknown pii entity {ent!r} (not a Presidio built-in)")
        if body.pii_score_threshold is not None and not (
                0.0 <= body.pii_score_threshold <= 1.0):
            raise HTTPException(
                422, f"pii_score_threshold {body.pii_score_threshold!r} out of range [0.0, 1.0]")
        if body.max_cost_usd_per_request is not None and not body.max_cost_usd_per_request > 0:
            raise HTTPException(
                422, "max_cost_usd_per_request must be > 0. Omit it, or name it in "
                "`reset`, to inherit the deployment's budgets.default_max_spend_usd.")
        if body.pii_exempt_patterns is not None:
            if len(body.pii_exempt_patterns) > MAX_EXEMPT_PATTERNS:
                raise HTTPException(
                    422, f"pii_exempt_patterns: at most {MAX_EXEMPT_PATTERNS} patterns")
            for pat in body.pii_exempt_patterns:
                if len(pat) > MAX_EXEMPT_PATTERN_LEN:
                    raise HTTPException(
                        422, f"pii_exempt_patterns regex too long "
                        f"(>{MAX_EXEMPT_PATTERN_LEN} chars): {pat!r}")
                try:
                    re.compile(pat)
                except re.error as ex:
                    raise HTTPException(
                        422, f"invalid pii_exempt_patterns regex {pat!r}: {ex}")

        settings = request.app.state.settings
        stored = app.policy
        await _validate_policy_values(body, settings, session, stored)

        def _stored(field: str):
            """The stored value this write will leave behind, BEFORE the body's
            own value is layered on: NULL for anything named in `reset`.

            Every field of the post-write view below goes through this, and
            that is the whole safety argument for `reset`. A reset changes the
            effective post-write value, so validating `stored.<field>` directly
            would judge a state that is not the one being written — e.g.
            resetting `policy_mode` on an onprem_only + passthrough +
            capture_io=full app reverts the posture to the deployment default
            (normally `enforced`) and leaves an ENFORCED onprem_only app storing
            raw I/O, exactly what `CAPTURE_FULL_ONPREM_NEEDS_NO_PROTECTION`
            exists to forbid (CLAUDE.md rules 2/8). Routed through here, the
            compiled policy resolves to `enforced` — hence `model_input:
            redact` — and that write is refused, whole, like any other.
            """
            if field in reset_fields:
                return None
            return getattr(stored, field) if stored else None

        # --- The effective post-write policy, judged by the compiler.
        #
        # The PUT is a MERGE (see `exclude_none=True` below), so what must be
        # validated is "body value if provided, else the stored value, else the
        # deployment default" — the state that will actually land. Every clamp
        # this replaced keyed on such an `eff_*` value by hand, and the two
        # Criticals that shaped them are both merge bugs: a partial PUT judged
        # on a value it was about to overwrite, and a partial PUT skipping a
        # clamp entirely because the field it keys on was never mentioned (a
        # stored `pii_detection: off` app pushed to `capture_io: redacted`, or a
        # stored `stream_unverified` app flipped to `onprem_only`). Building one
        # post-write `AppPolicyView` and compiling it makes that structural:
        # there is no path by which a rule can read the body instead.
        #
        # `_stored` is what makes `reset` safe here — see its docstring. A reset
        # changes the effective post-write value, so the view below is built
        # from post-reset values and the write is judged on the state it
        # actually produces.
        def _merged(field: str):
            """Body value if this write carries one, else what the row will
            still hold afterwards (NULL for anything named in `reset`)."""
            value = getattr(body, field, None)
            return value if value is not None else _stored(field)

        # `residency` alone falls back to the APPLICATION's default, which is
        # not a policy column: a null policy residency inherits
        # `app.default_residency`, exactly as `policy_view_from` resolves it.
        post_write = AppPolicyView(
            residency=_merged("residency") or app.default_residency,
            capture_io=_merged("capture_io"),
            policy_mode=_merged("policy_mode"),
            pii_detection=_merged("pii_detection"),
            stream_mode=_merged("stream_mode"),
            model_input=_merged("model_input"),
            client_output=_merged("client_output"),
            content_integrity=_merged("content_integrity"),
            escalate_residency_on_detection=_merged(
                "escalate_residency_on_detection"),
            allowed_regions=_merged("allowed_regions"),
            require_no_retention=_merged("require_no_retention"),
            trajectory_mode=_merged("trajectory_mode"),
        )
        # An app's own provisioned Langfuse project is the one condition the
        # compiler cannot see: it is DB state, not policy. Raw capture requires
        # it and nothing ever relaxes that (CLAUDE.md rule 2), so it is passed
        # in rather than inferred.
        #
        # Passed as a CALLABLE, and `validate_effective` only invokes it when
        # the effective capture is 'full' — matching the old clamp, which was
        # reached only inside `if eff_capture == "full"`. `load_app_langfuse`
        # decrypts the stored secret, and ciphertext left behind by a key
        # rotation can raise (decrypt_secret swallows Fernet errors but not a
        # UnicodeDecodeError on garbage plaintext). Resolved eagerly, that would
        # 500 an unrelated PUT — editing `max_rpm` on such an app — instead of
        # only the writes that actually depend on the answer.
        from app.apps.langfuse_provision import load_app_langfuse
        try:
            validate_effective(
                compile_policy(post_write, settings), settings,
                has_own_langfuse_project=lambda: load_app_langfuse(app) is not None)
            # A policy can pass every rule in `validate_effective` and still
            # make the COMPILER misbehave for this app -- monotonicity,
            # noninterference, or "placement never weakens protection"
            # (design §7, invariants 1-3). `check_invariants` compiles this
            # one app's post-write policy over a small fixed grid to catch
            # that before it is persisted, not after.
            check_invariants(post_write, settings)
        except PolicyViolation as exc:
            # Every refusal reaches the operator as the 422 it always did; the
            # rules, their reasons and their wording now live beside the
            # compiler that resolves the axes, in `app/policy/compile.py`. Read
            # them there before changing anything here — each one encodes a
            # privacy invariant, and two of them encode deliberate asymmetries
            # (which axes fall through to a deployment default, and which do
            # not) that have been re-broken before.
            raise HTTPException(422, exc.message)

        # exclude_none=True makes this a MERGE, not a full replace: an
        # omitted field keeps its stored value instead of being wiped to
        # None (the Critical this fixed — see Part A in the task-9 brief).
        # `reset` is the one explicit escape from that (see PolicyIn): the
        # named fields are written as NULL after the merge, so "inherit the
        # deployment default" is reachable without weakening the merge for
        # every other caller. It is applied last, and only reaches here once
        # every clamp above has judged the post-reset effective state — a
        # refused write applies nothing at all (no commit has happened yet).
        fields = body.model_dump(exclude_none=True)
        fields.pop("reset", None)   # a request knob, not a policy column
        fields.update({f: None for f in reset_fields})
        await store.upsert_policy(session, app_id, **fields)
        await session.commit()
        # `app.policy` was eager-loaded (possibly as None) above for the
        # eligibility check; expire it so upsert_policy's write is actually
        # re-read below instead of returning the stale cached relationship
        # (expire_on_commit=False on this sessionmaker).
        session.expire(app)
        full = await store.get_application_with_policy(session, app_id)
        app_dict = _app_dict(full, settings=settings)
        view = policy_view_from(full)
    # Outside the session block: registry.ensure_loaded() opens its own
    # db.session(), which would deadlock on the non-reentrant SQLite
    # write-lock (app/apps/db.py) if called while the outer session above is
    # still open.
    # The policy is already committed: a failure computing advisory warnings
    # must never turn a successful save into a 500.
    try:
        registry = request.app.state.registry
        await registry.ensure_loaded(request.app.state.db)
        from app.apps import policy_lint
        warnings = policy_lint.policy_warnings(settings, registry, app_id, view)
    except Exception:
        logging.getLogger("serving.admin").exception(
            "policy saved for %s but computing warnings failed", app_id)
        warnings = []
    return {**app_dict, "warnings": warnings}


# --- tokens -----------------------------------------------------------------

class _Caller(BaseModel):
    """Who called a token route: an administrator, or a service credential
    scoped to one application."""
    admin_id: str | None = None
    service_id: str | None = None
    service_app_id: str | None = None


async def require_admin_or_service(request: Request) -> _Caller:
    """Admin session cookie, else a `Bearer rf_svc_…` service credential.
    Used ONLY by the token create/revoke routes; every other /admin/* route
    keeps `require_admin`, so a service credential reaches nothing else."""
    try:
        return _Caller(admin_id=await require_admin(request))
    except HTTPException:
        pass
    raw = request.headers.get("authorization", "").removeprefix("Bearer ").strip()
    async with request.app.state.db.session() as session:
        cred = await store.resolve_service_credential(session, raw)
        if cred is None:
            raise HTTPException(401, "admin login or service credential required")
        caller = _Caller(service_id=cred.id, service_app_id=cred.application_id)
        await session.commit()  # persist last_used_at
        return caller


@admin_router.post("/applications/{app_id}/tokens")
async def create_token(app_id: str, body: TokenIn, request: Request,
                       caller: _Caller = Depends(require_admin_or_service)) -> dict:
    if caller.service_id is not None and caller.service_app_id != app_id:
        raise HTTPException(403, "service credential is scoped to another application")
    expires_at = None
    if body.expires_in_s is not None:
        expires_at = dt.datetime.now(dt.timezone.utc) + dt.timedelta(
            seconds=body.expires_in_s)
    async with request.app.state.db.session() as session:
        if await store.get_application(session, app_id) is None:
            raise HTTPException(404, "application not found")
        raw = new_token()
        token = await store.add_token(session, application_id=app_id,
                                      raw_token=raw, name=body.name,
                                      expires_at=expires_at,
                                      session_binding=body.session,
                                      issued_by=caller.service_id)
        await session.commit()
        # Plaintext returned ONCE — never retrievable again.
        return {"token": raw, "id": token.id, "prefix": token.prefix,
                "expires_at": _iso(expires_at), "session": body.session}


@admin_router.post("/tokens/{token_id}/revoke")
async def revoke_token(token_id: str, request: Request,
                       caller: _Caller = Depends(require_admin_or_service)) -> dict:
    async with request.app.state.db.session() as session:
        if caller.service_id is not None:
            # A service credential revokes only tokens it created; any other
            # token is answered exactly like one that does not exist.
            existing = await session.get(AppToken, token_id)
            if existing is None or existing.issued_by != caller.service_id:
                raise HTTPException(404, "token not found")
        token = await store.revoke_token(session, token_id)
        if token is None:
            raise HTTPException(404, "token not found")
        await session.commit()
        return {"ok": True, "id": token_id}


# --- service credentials (admin-only) ---------------------------------------

@admin_router.post("/applications/{app_id}/service-credentials")
async def create_service_credential(app_id: str, body: ServiceCredentialIn,
                                    request: Request,
                                    _: str = Depends(require_admin)) -> dict:
    async with request.app.state.db.session() as session:
        if await store.get_application(session, app_id) is None:
            raise HTTPException(404, "application not found")
        raw = new_service_credential()
        cred = await store.add_service_credential(
            session, application_id=app_id, raw=raw, name=body.name)
        await session.commit()
        # Plaintext returned ONCE — never retrievable again.
        return {"credential": raw, "id": cred.id, "prefix": cred.prefix,
                "application_id": app_id}


@admin_router.get("/applications/{app_id}/service-credentials")
async def list_service_credentials(app_id: str, request: Request,
                                   _: str = Depends(require_admin)) -> list[dict]:
    async with request.app.state.db.session() as session:
        if await store.get_application(session, app_id) is None:
            raise HTTPException(404, "application not found")
        creds = await store.list_service_credentials(session, app_id)
        return [_service_credential_dict(c) for c in creds]


@admin_router.post("/service-credentials/{cred_id}/revoke")
async def revoke_service_credential(cred_id: str, request: Request,
                                    _: str = Depends(require_admin)) -> dict:
    async with request.app.state.db.session() as session:
        cred = await store.revoke_service_credential(session, cred_id)
        if cred is None:
            raise HTTPException(404, "service credential not found")
        await session.commit()
        return {"ok": True, "id": cred_id}


# --- usage ------------------------------------------------------------------

@admin_router.get("/usage")
async def usage(request: Request, application_id: str | None = None,
                hours: int | None = None, _: str = Depends(require_admin)) -> dict:
    since = None
    if hours is not None:
        since = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=hours)
    async with request.app.state.db.session() as session:
        return await store.usage_summary(
            session, application_id=application_id, since=since)


@admin_router.get("/usage/by-application")
async def usage_by_application(request: Request, hours: int | None = None,
                               _: str = Depends(require_admin)) -> dict:
    since = None
    if hours is not None:
        since = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=hours)
    async with request.app.state.db.session() as session:
        apps = await store.usage_by_application(session, since=since)
    return {"apps": apps, "window_hours": hours}


# --- per-app privacy profile (recognizers + sensitivity rules) --------------

@admin_router.get("/applications/{app_id}/privacy")
async def get_privacy(app_id: str, request: Request,
                      _: str = Depends(require_admin)) -> dict:
    async with request.app.state.db.session() as session:
        if await store.get_application(session, app_id) is None:
            raise HTTPException(404, "application not found")
        prof = await store.get_privacy_profile(session, app_id)
    return {
        "recognizers": [{"name": r.name, "regex": r.pattern, "score": r.score}
                        for r in prof.recognizers],
        "rules": [{"entities": sorted(r.entities), "sensitivity": r.sensitivity,
                   "risk_class": r.risk_class, "residency": r.residency,
                   "require_verification": r.require_verification}
                  for r in prof.rules],
    }


@admin_router.put("/applications/{app_id}/privacy")
async def put_privacy(app_id: str, body: PrivacyProfileIn, request: Request,
                      _: str = Depends(require_admin)) -> dict:
    try:
        _validate_profile(body)
    except ValueError as e:
        raise HTTPException(422, str(e))
    async with request.app.state.db.session() as session:
        if await store.get_application(session, app_id) is None:
            raise HTTPException(404, "application not found")
        await store.replace_privacy_profile(
            session, app_id,
            recognizers=[r.model_dump() for r in body.recognizers],
            rules=[r.model_dump() for r in body.rules])
        await session.commit()
    request.app.state.profile_cache.clear(app_id)
    return {"ok": True}


# --- on-prem model onboarding (runtime registry; probe-gated activation) ----
#
# routeForge deploys nothing — these endpoints register an externally-served
# OpenAI-compatible endpoint as a routable on-prem model. A model is saved
# `enabled=True` only if the admin requested enabled AND `probe_endpoint`
# reached it and confirmed it serves the given slug (enabled = requested_enabled
# AND probe_ok); an unreachable model is always saved disabled regardless of
# admin intent. Downstream, ModelRegistry.has_healthy_onprem() keys on
# `enabled`, so a probe-failed model correctly never satisfies onprem_only
# routing. `provider` is always forced to "onprem" — never taken from the
# request body.

async def _validate_policy_values(body: "PolicyIn", settings, session,
                                  stored=None) -> None:
    """Refuse values that can never route. Empty allowlists are refused because
    the store reads [] as 'no restriction', the opposite of what an operator
    who clears a list means.

    Only values this write CHANGES are checked. The console always sends the
    whole policy, so a value stored before these checks existed (max_rpm=0, a
    since-retired model slug) must not block an unrelated edit; changing it,
    or setting a new illegal value, is still refused."""
    def changed(field: str) -> bool:
        v = getattr(body, field)
        return v is not None and v != (getattr(stored, field) if stored else None)

    for field in ("allowed_tiers", "allowed_providers", "allowed_models"):
        v = getattr(body, field)
        if changed(field) and len(v) == 0:
            raise HTTPException(
                422, f"{field} is an empty list, which RouteForge reads as 'no "
                "restriction'. Omit the field to leave it unchanged, or name it in "
                "`reset` to clear it.")
    if changed("allowed_tiers"):
        bad = sorted(set(body.allowed_tiers) - set(settings.routing.tiers))
        if bad:
            raise HTTPException(
                422, f"unknown tier(s) {bad}; legal tiers: {list(settings.routing.tiers)}")
    if changed("allowed_providers"):
        bad = sorted(set(body.allowed_providers) - set(settings.serving.providers))
        if bad:
            raise HTTPException(
                422, f"unknown provider(s) {bad}; legal providers: "
                f"{sorted(settings.serving.providers)}")
    if changed("allowed_models"):
        known = ({c.name for c in settings.routing.model_pool}
                 | {m.slug for m in await store.list_onboarded(session)})
        bad = sorted(set(body.allowed_models) - known)
        if bad:
            raise HTTPException(
                422, f"unknown model(s) {bad}: not in the YAML pool and not onboarded")
    if changed("max_rpm") and body.max_rpm < 1:
        raise HTTPException(
            422, "max_rpm must be >= 1 (0 rejects every request). Omit it, or reset it, "
            "for no rate limit.")
    if changed("max_spend_usd_per_day") and not body.max_spend_usd_per_day > 0:
        raise HTTPException(
            422, "max_spend_usd_per_day must be > 0 (0 rejects every request). Omit it, "
            "or reset it, for no daily cap.")


def _static_validate_onboard(body: OnboardIn, s) -> None:
    tiers = set(s.routing.tiers)
    if body.tier not in tiers:
        raise HTTPException(422, f"bad tier {body.tier!r}")
    if not re.match(r"^https?://", body.endpoint):
        raise HTTPException(422, "endpoint must be an http(s) URL")
    if body.price_input < 0 or body.price_output < 0:
        raise HTTPException(422, "price must be >= 0")
    if body.price_cached_input is not None and body.price_cached_input < 0:
        raise HTTPException(422, "price_cached_input must be >= 0")
    # B50: zero (or negative) is never a valid context window -- refuse it
    # here rather than store it verbatim and label it "operator" (design
    # §4.2). `/v1/models` would omit a falsy value anyway, but junk should
    # not reach the database in the first place.
    if body.context_window is not None and body.context_window < 1:
        raise HTTPException(422, "context_window must be >= 1, or omit/null for unknown")
    # P1b: prompt_retention is a fixed enum (app/policy/enums.py, shared with
    # the YAML lint so an admin-accepted value is a value the resolver will
    # actually read as a real declaration) -- None (undeclared) is always
    # legal and is not in the set. `region` is deliberately NOT validated
    # here: it is customer vocabulary with no RouteForge-owned list (design
    # §2), matched exactly after normalisation, not against an enum.
    if (body.prompt_retention is not None
            and body.prompt_retention not in PROMPT_RETENTION_VALUES):
        raise HTTPException(
            422, f"bad prompt_retention {body.prompt_retention!r}; legal values: "
            f"{sorted(PROMPT_RETENTION_VALUES)}, or omit/null for undeclared")


def _normalized_region(region: str | None) -> str | None:
    """Store `region` pre-normalized (stripped + lowercased) so a value the
    admin API accepted reads back exactly as `ModelRegistry._resolve_data_handling`
    would resolve it -- the same `.strip().lower()` that function already
    applies at read time (app/apps/registry.py), done once here so the GET
    response and the form show the same token the router will actually
    match, rather than the raw un-normalized input. A blank/whitespace-only
    string is undeclared, same as omitting the field."""
    if region is None:
        return None
    stripped = region.strip().lower()
    return stripped or None


def _resolved_accepts_raw_sensitive(body: OnboardIn, existing) -> bool | None:
    """`accepts_raw_sensitive` is the one field on this route that does NOT
    full-replace on omission, unlike every other optional OnboardIn field
    (region/prompt_retention included) -- deliberate, found in Task 8's
    console review, not an oversight.

    Why it's asymmetric: `region`/`prompt_retention` clearing to "unknown"
    makes app/policy/feasible.py's stage-2 rule MORE restrictive (an app
    that requires a region or no-retention excludes an "unknown" endpoint),
    so full-replace-on-omission fails CLOSED for those two -- safe.
    `accepts_raw_sensitive` is the opposite: the raw-sensitive rule
    (`_data_handling_rejection`) fires ONLY on a declared `false` -- an
    "unknown" endpoint is KEPT, not excluded. So clearing a declared
    `false` to unknown/None on omission would SILENTLY REOPEN an endpoint
    an operator had explicitly closed to raw sensitive traffic the moment
    any caller resubmits a body that doesn't happen to name this field
    (e.g. a scripted price update written against an older body shape) --
    fail-OPEN, with no error or warning. CLAUDE.md rule 5 (fail closed) and
    rule 2a (a no-protection posture is never silent) both rule that out.

    So: the field being ABSENT from the request (`"accepts_raw_sensitive"
    not in body.model_fields_set`) keeps whatever is already stored (`None`
    on a brand-new row -- nothing to preserve). The field being PRESENT --
    including an explicit JSON `null` -- is honoured as real intent: an
    operator who explicitly clears the declaration must still be able to,
    on this same route; only silent, accidental clearing via omission is
    blocked.
    """
    if "accepts_raw_sensitive" not in body.model_fields_set:
        return existing.accepts_raw_sensitive if existing is not None else None
    return body.accepts_raw_sensitive


def _resolved_price_cached_input(body: OnboardIn, existing) -> float | None:
    """Same rule as `_resolved_accepts_raw_sensitive`, applied to the P2a
    cached-input rate: omission preserves, explicit null clears. A price
    quietly reverting to "no discount" on an unrelated edit (e.g. a scripted
    display-name update written against an older body shape) would overstate
    spend with no operator action -- fail-OPEN on cost accuracy, which
    CLAUDE.md rule 2a's "a no-protection posture is never silent" spirit
    rules out here too, even though this isn't a protection axis."""
    if "price_cached_input" not in body.model_fields_set:
        return existing.price_cached_input if existing is not None else None
    return body.price_cached_input


def _resolved_context_window(
        body: OnboardIn, probed: int | None, existing) -> tuple[int | None, str | None]:
    """`context_window` and its source (B50, design §4.2, amended after Task 5
    review round 1).

    Provenance is decided by the **value**, not by whether the field was
    populated. The console pre-fills its edit form from the stored row and
    resubmits the whole body on every save, so a naive "populated -> operator"
    rule relabels a probed number as human-entered the moment anyone next
    saves the model -- destroying the exact honesty this column exists to
    record.

    So: a submitted value that equals what RouteForge would have stored by
    itself -- the freshly probed `max_model_len`, or (when this probe learned
    nothing) the existing row's own value whose source was already `"probed"`
    -- stays `"probed"`. Any other submitted value is `"operator"`: a human
    changed it to something RouteForge would not itself have produced. Typing
    the exact number the endpoint reports is recorded as `"probed"`, not
    `"operator"` -- the column records where the number came from, not who
    pressed save. No submitted value falls back to the probe; neither present
    leaves both NULL.
    """
    if body.context_window is None:
        if probed is not None:
            return probed, "probed"
        return None, None
    if body.context_window == probed:
        return body.context_window, "probed"
    if (probed is None and existing is not None
            and existing.context_window_source == "probed"
            and body.context_window == existing.context_window):
        return body.context_window, "probed"
    return body.context_window, "operator"


@admin_router.post("/infra/models")
async def onboard_model(body: OnboardIn, request: Request,
                        _: str = Depends(require_admin)) -> dict:
    s = request.app.state.settings
    reg = request.app.state.registry
    yaml_names = {c.name for c in s.routing.model_pool}
    _static_validate_onboard(body, s)
    async with request.app.state.db.session() as session:
        # yaml cloud-pool names are never re-onboardable. An existing onboarded
        # row 409s only when it is ENABLED (edit a working model with PUT); a
        # DISABLED row — e.g. one saved unreachable on a first attempt with a
        # stray-typo endpoint — is re-probed and upserted below, so a bad
        # onboarding self-heals via a clean re-POST instead of needing a manual
        # DB delete (B19).
        existing = await store.get_onboarded(session, body.slug)
        if body.slug in yaml_names or (existing is not None and existing.enabled):
            raise HTTPException(409, f"slug {body.slug!r} already exists")
        if body.allowed_app_ids:
            for aid in body.allowed_app_ids:
                if await store.get_application(session, aid) is None:
                    raise HTTPException(422, f"unknown application {aid!r}")
        _probe = await probe_endpoint(body.endpoint, body.slug, body.api_key_env)
        ok, detail = _probe.ok, _probe.detail
        # `enabled` is explicit admin intent, ANDed with probe result: an
        # unreachable model can NEVER end up enabled (registry.has_healthy_onprem()
        # and onprem_only fail-closed depend on that), but a reachable model can
        # still be turned off on request.
        final_enabled = body.enabled and ok
        context_window, context_window_source = _resolved_context_window(
            body, _probe.max_model_len, existing)
        await store.upsert_onboarded(
            session, body.slug, display_name=body.display_name, provider="onprem",
            tier=body.tier, endpoint=body.endpoint, api_key_env=body.api_key_env,
            price_input=body.price_input, price_output=body.price_output,
            price_cached_input=_resolved_price_cached_input(body, existing),
            quality=body.quality, context_window=context_window,
            context_window_source=context_window_source,
            allowed_app_ids=body.allowed_app_ids, notes=body.notes,
            enabled=final_enabled, probe_status=("healthy" if ok else "unreachable"),
            probe_error=(None if ok else detail),
            # P1b declarations. `region`/`prompt_retention` are full-replace,
            # same as every other optional field on this route (api_key_env,
            # quality, notes, …): the body IS the new stored state, so an
            # omitted/null field clears back to "undeclared" rather than
            # being preserved. The console's edit form always resubmits the
            # full row (see ui/src/admin/InfrastructurePanel.tsx::
            # fieldsOf/toBody), so this matches what an operator editing in
            # place actually intends. `accepts_raw_sensitive` is the
            # deliberate exception -- see `_resolved_accepts_raw_sensitive`.
            # Applied here too, not just on the PUT below: a re-POST of an
            # existing (disabled) row is the same self-heal upsert path
            # (B19) and carries the identical silent-reopen risk once the
            # row re-enables.
            region=_normalized_region(body.region),
            prompt_retention=body.prompt_retention,
            accepts_raw_sensitive=_resolved_accepts_raw_sensitive(body, existing))
        await session.commit()
    await reg.refresh(request.app.state.db)
    return {"slug": body.slug, "enabled": final_enabled,
            "probe_status": "healthy" if ok else "unreachable",
            "probe_error": None if ok else detail,
            "warnings": price_warnings(s, body.price_input, body.price_output)}


# {slug:path} (not {slug}) so real OpenAI-compatible ids that contain a slash
# — Qwen/Qwen2.5-1.5B-Instruct, meta-llama/Llama-3.1-8B-Instruct — are captured.
# A bare {slug} matches [^/]+, so a slashed slug 404s and the row becomes
# un-editable/un-deletable (B19). The UI already sends encodeURIComponent(slug);
# uvicorn decodes %2F back to '/' before routing, which {slug:path} then matches.
@admin_router.put("/infra/models/{slug:path}")
async def edit_onboarded_model(slug: str, body: OnboardIn, request: Request,
                               _: str = Depends(require_admin)) -> dict:
    s = request.app.state.settings
    reg = request.app.state.registry
    _static_validate_onboard(body, s)
    async with request.app.state.db.session() as session:
        existing = await store.get_onboarded(session, slug)
        if existing is None:
            raise HTTPException(404, f"onboarded model {slug!r} not found")
        if body.allowed_app_ids:
            for aid in body.allowed_app_ids:
                if await store.get_application(session, aid) is None:
                    raise HTTPException(422, f"unknown application {aid!r}")
        _probe = await probe_endpoint(body.endpoint, slug, body.api_key_env)
        ok, detail = _probe.ok, _probe.detail
        # Same intent-AND-probe rule as onboard_model — see comment there.
        final_enabled = body.enabled and ok
        context_window, context_window_source = _resolved_context_window(
            body, _probe.max_model_len, existing)
        await store.upsert_onboarded(
            session, slug, display_name=body.display_name, provider="onprem",
            tier=body.tier, endpoint=body.endpoint, api_key_env=body.api_key_env,
            price_input=body.price_input, price_output=body.price_output,
            price_cached_input=_resolved_price_cached_input(body, existing),
            quality=body.quality, context_window=context_window,
            context_window_source=context_window_source,
            allowed_app_ids=body.allowed_app_ids, notes=body.notes,
            enabled=final_enabled, probe_status=("healthy" if ok else "unreachable"),
            probe_error=(None if ok else detail),
            # region/prompt_retention: same full-replace semantics as
            # onboard_model above. accepts_raw_sensitive: the deliberate
            # exception -- see _resolved_accepts_raw_sensitive.
            region=_normalized_region(body.region),
            prompt_retention=body.prompt_retention,
            accepts_raw_sensitive=_resolved_accepts_raw_sensitive(body, existing))
        await session.commit()
    await reg.refresh(request.app.state.db)
    return {"slug": slug, "enabled": final_enabled,
            "probe_status": "healthy" if ok else "unreachable",
            "probe_error": None if ok else detail,
            "warnings": price_warnings(s, body.price_input, body.price_output)}


@admin_router.delete("/infra/models/{slug:path}")  # {slug:path} for slashed ids — see PUT above (B19)
async def delete_onboarded_model(slug: str, request: Request,
                                 _: str = Depends(require_admin)) -> dict:
    reg = request.app.state.registry
    async with request.app.state.db.session() as session:
        deleted = await store.delete_onboarded(session, slug)
        if not deleted:
            raise HTTPException(404, f"onboarded model {slug!r} not found")
        await session.commit()
    await reg.refresh(request.app.state.db)
    return {"ok": True, "slug": slug}


@admin_router.get("/infra/models")
async def list_onboarded_models(request: Request,
                                _: str = Depends(require_admin)) -> list:
    br = getattr(request.app.state, "breaker", None)
    now = time.monotonic()
    async with request.app.state.db.session() as session:
        rows = []
        onboarded = await store.list_onboarded(session)
        cache = await store.cache_reporting_status(session, [m.slug for m in onboarded])
        for m in onboarded:
            d = _onboarded_dict(m)
            d["cache_reporting"] = cache.get(m.slug, "unknown")
            d["circuit"] = (_circuit_dict(br.state(m.slug, now=now), now) if br is not None
                            else {"state": "closed", "consecutive_failures": 0,
                                  "open_until_s": None})
            rows.append(d)
        return rows


# --- routing outcomes export (P2b-2 Task 6) ---------------------------------
#
# GET /admin/outcomes/export streams the routing_outcomes table (+ its joined
# outcome_feedback) as NDJSON for offline analysis -- the eval/RLHF substrate
# CLAUDE.md's RLHF-readiness note has in mind. Admin-only, like every other
# route in this file; the data is content-free by construction (see
# app/apps/store.py's P2b-2 section docstring), so this adds no new exposure
# beyond "an admin can see routing metadata across every tenant", which the
# rest of /admin/* already grants.

_EXPORT_DEFAULT_LIMIT = 100_000
_EXPORT_MAX_LIMIT = 100_000
# Module-level so tests can monkeypatch it to exercise a page boundary
# without needing 1000+ rows; referenced by bare name below, not captured as
# a default arg, so a patched value takes effect on the next page fetch.
_EXPORT_PAGE_SIZE = 1000


def _export_iso(ts: dt.datetime) -> str:
    """ISO-8601 UTC. SQLite (the test DB) hands `ts` back naive for a
    `DateTime(timezone=True)` column; Postgres hands it back aware. Treat a
    naive value as already UTC -- same convention as app/apps/store.py's
    `_to_utc` -- so the exported timestamp always carries an explicit offset
    on either backend, instead of Postgres rows getting `+00:00` and SQLite
    rows silently getting none."""
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=dt.timezone.utc)
    return ts.isoformat()


def _export_outcome_dict(row: RoutingOutcome) -> dict:
    d = {c.name: getattr(row, c.name) for c in RoutingOutcome.__table__.columns}
    d["ts"] = _export_iso(row.ts)
    return d


def _export_feedback_list(rows: list[OutcomeFeedback]) -> list[dict]:
    return [
        {"score_name": r.score_name,
         "value": r.value_num if r.value_num is not None else r.value_cat,
         "source": r.source,
         "ts": _export_iso(r.ts)}
        for r in rows
    ]


def _parse_export_dt(raw: str | None) -> dt.datetime | None:
    """`datetime.fromisoformat`, 422 on a bad value. A naive result (no
    offset in `raw`) is treated as UTC -- see `_export_iso`'s docstring for
    why that convention is safe against both backends."""
    if raw is None:
        return None
    try:
        parsed = dt.datetime.fromisoformat(raw)
    except ValueError:
        raise HTTPException(422, f"bad datetime {raw!r}; expected ISO-8601")
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed


async def _export_lines(db, *, since: dt.datetime, until: dt.datetime,
                        application_id: str | None, limit: int):
    """NDJSON line generator: keyset-paged through `iter_outcomes`, a fresh
    session per page (never one session -- or the whole export -- held open
    or in memory at once), joining `feedback_for` per page and stopping once
    `limit` lines have been emitted, even mid-page."""
    after: tuple[dt.datetime, str] | None = None
    emitted = 0
    while emitted < limit:
        async with db.session() as s:
            rows = await store.iter_outcomes(
                s, since=since, until=until, application_id=application_id,
                after=after, page=_EXPORT_PAGE_SIZE)
            if not rows:
                return
            # Joined by (application_id, request_id): one scoped lookup per
            # app on the page, so feedback can never attach across apps.
            by_app: dict[str, list[str]] = {}
            for r in rows:
                by_app.setdefault(r.application_id, []).append(r.request_id)
            feedback = {
                app_id: await store.feedback_for(s, ids, application_id=app_id)
                for app_id, ids in by_app.items()}
        for row in rows:
            if emitted >= limit:
                return
            obj = {
                "schema": "routeforge.outcome/v1",
                "outcome": _export_outcome_dict(row),
                "feedback": _export_feedback_list(
                    feedback[row.application_id].get(row.request_id, [])),
            }
            yield json.dumps(obj, separators=(",", ":"), default=str) + "\n"
            emitted += 1
        after = (rows[-1].ts, rows[-1].request_id)
        if len(rows) < _EXPORT_PAGE_SIZE:
            return   # short page -- no more rows to fetch


@admin_router.get("/outcomes/export")
async def export_outcomes(
        request: Request, since: str | None = None, until: str | None = None,
        application_id: str | None = None, limit: int = _EXPORT_DEFAULT_LIMIT,
        _: str = Depends(require_admin)) -> StreamingResponse:
    if limit < 1:
        raise HTTPException(422, "limit must be >= 1")
    limit = min(limit, _EXPORT_MAX_LIMIT)
    until_dt = _parse_export_dt(until) or dt.datetime.now(dt.timezone.utc)
    since_dt = _parse_export_dt(since) or (until_dt - dt.timedelta(days=7))
    return StreamingResponse(
        _export_lines(request.app.state.db, since=since_dt, until=until_dt,
                      application_id=application_id, limit=limit),
        media_type="application/x-ndjson")


# --- policy simulator snapshot (P3a) -----------------------------------------

@admin_router.get("/config-snapshot")
async def config_snapshot(request: Request, _: str = Depends(require_admin)) -> dict:
    """Content-free, secret-free snapshot of everything a routing decision
    reads (P3a spec §4), for the offline policy simulator
    (`scripts/policy_simulate.py`). Read-only: it changes nothing."""
    state = request.app.state
    # Outside the session block below (same reason as the other admin routes
    # that call this): registry.ensure_loaded() opens its own db.session(),
    # which would deadlock on the non-reentrant SQLite write-lock if nested
    # inside the one below. Without this, a registry that's never taken a
    # live request yet (registry.py: "lazily loaded on the first request")
    # snapshots as an empty effective pool/registry_version even though
    # `onboarded` (read straight from the DB, not the registry) is accurate
    # -- exactly the discrepancy the P3a round-trip test (spec §9.10) exists
    # to catch.
    await state.registry.ensure_loaded(state.db)
    async with state.db.session() as session:
        return await build_config_snapshot(
            session, settings=state.settings, registry=state.registry,
            quality=state.quality, signals=state.signals,
            now=dt.datetime.now(dt.timezone.utc))


# --- quality calibration (P2b-3 Task 5) -------------------------------------

def _audit_calibration(fields: dict) -> None:
    """ONE structured audit line per apply/revert: a compact JSON object of
    ids, counts and flags (content-free), also attached as `extra` for a
    structured handler."""
    logging.getLogger("serving.admin").info(
        "%s", json.dumps(fields, sort_keys=True, separators=(",", ":")),
        extra={"audit": fields})


def _shadowed_by(model: str, workload: str | None, customer: dict,
                 calibrated: dict) -> list[dict]:
    """Existing override rows MORE SPECIFIC than a proposed `(model, None)`
    unit, which will keep winning for their workload after apply: a
    `customer` or `calibrated` row keyed `(model, wl)`. A per-workload unit
    has nothing more specific, so its list is always empty. (A model-wide
    customer row is not "shadowing" -- it is `customer_wins`.)"""
    if workload is not None:
        return []
    out = []
    for source, rows in (("customer", customer), ("calibrated", calibrated)):
        for (m, wl), q in rows.items():
            if m == model and wl is not None:
                out.append({"workload": wl, "source": source, "set_id": q.set_id})
    return sorted(out, key=lambda x: (x["workload"], x["source"]))


@admin_router.post("/quality/calibrations")
async def apply_calibration(
        request: Request, rec: dict = Body(...),
        dry_run: bool = False, force_registry_mismatch: bool = False,
        admin_id: str = Depends(require_admin)) -> dict:
    """Validate a `recommendation.json` against the live registry, then
    either preview it (`dry_run=true`, writes nothing) or apply it (one
    `write_calibration_set` transaction + `refresh_quality`). See spec §6."""
    registry = request.app.state.registry
    await registry.ensure_loaded(request.app.state.db)
    current_registry_version = registry.registry_version()
    try:
        recommended = validate_recommendation(
            rec, registry=registry, current_registry_version=current_registry_version,
            force_registry_mismatch=force_registry_mismatch)
    except CalibrationError as exc:
        raise HTTPException(422, str(exc)) from exc

    set_id = rec.get("set_id")
    version_match = rec.get("registry_version") == current_registry_version
    forced = force_registry_mismatch and not version_match

    if dry_run:
        quality = request.app.state.quality
        customer, calibrated = quality.override_maps()
        units = []
        for u in recommended:
            model, workload = u["model"], u.get("workload")
            current = quality.resolve(model, workload)
            units.append({
                "model": model,
                "workload": workload,
                "current": {"score": current.score, "source": current.source,
                            "set_id": current.set_id},
                "proposed": u["mean"],
                # Same order `resolve` applies: customer (m, wl) -> customer
                # (m, None) beat ANY calibrated row, however specific.
                "customer_wins": quality.customer_override(model, workload) is not None,
                "shadowed_by": _shadowed_by(model, workload, customer, calibrated),
            })
        return {
            "set_id": set_id,
            "registry_version_match": version_match,
            "units": units,
        }

    async with request.app.state.db.session() as session:
        try:
            applied = await store.write_calibration_set(
                session, set_id=set_id,
                units=[{"model": u["model"], "workload": u.get("workload"),
                        "score": u["mean"]} for u in recommended])
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        await session.commit()

    from app.main import refresh_quality  # local import: avoids admin_routes<->main cycle
    refreshed = await refresh_quality(request.app)
    _audit_calibration({
        "event": "quality_calibration_applied", "set_id": set_id,
        "units": applied, "forced": forced,
        "registry_version_match": version_match, "admin_id": admin_id})
    return {"set_id": set_id, "applied": applied, "refreshed": refreshed,
            "registry_version_match": version_match, "forced": forced}


@admin_router.get("/quality/calibrations")
async def list_calibrations(request: Request, _: str = Depends(require_admin)) -> list:
    async with request.app.state.db.session() as session:
        sets = await store.list_calibration_sets(session)
    return [{"set_id": s["set_id"], "applied_at": _iso(s["applied_at"]),
             "units": s["units"]} for s in sets]


@admin_router.delete("/quality/calibrations/{set_id}")
async def revert_calibration(set_id: str, request: Request,
                             admin_id: str = Depends(require_admin)) -> dict:
    async with request.app.state.db.session() as session:
        deleted = await store.delete_calibration_set(session, set_id)
        if deleted == 0:
            raise HTTPException(404, f"calibration set not found: {set_id}")
        await session.commit()

    from app.main import refresh_quality  # local import: avoids admin_routes<->main cycle
    await refresh_quality(request.app)
    _audit_calibration({
        "event": "quality_calibration_reverted", "set_id": set_id,
        "units": deleted, "forced": False, "admin_id": admin_id})
    return {"set_id": set_id, "deleted": deleted}
