"""Admin API (Phase 3): applications, tokens, per-app policy, usage.

All routes except /admin/login require an admin session cookie. This surface is
separate from the token-authed serving API (/v1/*) — different auth, different
audience. The dashboard (Phase 4) is the only intended client.
"""
from __future__ import annotations

import datetime as dt
import re

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field, field_validator

from app.apps import store
from app.apps.admin_auth import SESSION_COOKIE, require_admin
from app.apps.langfuse_provision import LangfuseProvisioner, encrypt_secret
from app.apps.probe import probe_endpoint
from app.apps.tokens import new_token
from app.policy.effective import PROTECTED_ENTITIES
from app.policy.enums import PII_MODES, RESIDENCY, RISK_CLASS, SENSITIVITY, STREAM_MODES
from app.policy.recognizers import PRESIDIO_BUILTINS, validate_recognizer

admin_router = APIRouter(prefix="/admin", tags=["admin"])


# --- serialization helpers --------------------------------------------------

def _iso(v: dt.datetime | None) -> str | None:
    return v.isoformat() if v is not None else None


def _token_dict(t) -> dict:
    return {"id": t.id, "prefix": t.prefix, "name": t.name, "status": t.status,
            "created_at": _iso(t.created_at), "last_used_at": _iso(t.last_used_at),
            "revoked_at": _iso(t.revoked_at)}


def _policy_dict(p) -> dict | None:
    if p is None:
        return None
    return {"allowed_tiers": p.allowed_tiers, "allowed_providers": p.allowed_providers,
            "allowed_models": p.allowed_models, "residency": p.residency,
            "max_spend_usd_per_day": p.max_spend_usd_per_day, "max_rpm": p.max_rpm,
            "compression_enabled": p.compression_enabled, "capture_io": p.capture_io,
            "pii_detection": p.pii_detection, "stream_mode": p.stream_mode,
            "pii_entity_denylist": p.pii_entity_denylist,
            "pii_score_threshold": p.pii_score_threshold,
            "pii_exempt_patterns": p.pii_exempt_patterns}


def _onboarded_dict(m) -> dict:
    return {"slug": m.slug, "display_name": m.display_name, "provider": m.provider,
            "tier": m.tier, "quality": m.quality, "price_input": m.price_input,
            "price_output": m.price_output, "endpoint": m.endpoint,
            "api_key_env": m.api_key_env, "context_window": m.context_window,
            "enabled": m.enabled, "allowed_app_ids": m.allowed_app_ids,
            "probe_status": m.probe_status, "probe_error": m.probe_error,
            "notes": m.notes, "updated_at": _iso(m.updated_at)}


def _app_dict(app, *, with_children: bool = True) -> dict:
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
    max_rpm: int | None = None
    compression_enabled: bool | None = None   # None = inherit global Headroom toggle
    capture_io: str | None = None   # None = inherit global; off|redacted|full
    pii_detection: str | None = None   # None = inherit global; off|regex|presidio
    stream_mode: str | None = None   # None = inherit (reject inbound stream); see STREAM_MODES
    # Narrow the gate WITHOUT disabling it: entity types to detect-but-not-redact
    # for this app (false positives that mangle mandatory field values), and a
    # minimum Presidio confidence below which a match is dropped. US_SSN can
    # never be denylisted (rules 3/8). See docs/per-app-config.md.
    pii_entity_denylist: list[str] | None = None
    pii_score_threshold: float | None = None
    # Type-independent complement to pii_entity_denylist: regexes over the
    # detected entity's VALUE (fullmatch). US_SSN can never be exempted
    # (rules 3/8). See docs/per-app-config.md.
    pii_exempt_patterns: list[str] | None = None


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


class TokenIn(BaseModel):
    name: str = "default"


class OnboardIn(BaseModel):
    slug: str
    display_name: str
    tier: str                       # cheap|mid|frontier
    endpoint: str
    api_key_env: str | None = None
    price_input: float
    price_output: float
    quality: float | None = None
    context_window: int | None = None
    allowed_app_ids: list[str] | None = None
    notes: str | None = None
    enabled: bool = True


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
    return {"admin_id": admin_id}


# --- applications -----------------------------------------------------------

@admin_router.get("/applications")
async def list_applications(request: Request, _: str = Depends(require_admin)) -> list:
    async with request.app.state.db.session() as session:
        out = []
        for app in await store.list_applications(session):
            full = await store.get_application_with_policy(session, app.id)
            out.append(_app_dict(full))
        return out


@admin_router.post("/applications")
async def create_application(body: CreateAppIn, request: Request,
                             _: str = Depends(require_admin)) -> dict:
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
        return {"application": _app_dict(full), "token": raw}


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
        return _app_dict(full)


@admin_router.patch("/applications/{app_id}")
async def update_application(app_id: str, body: UpdateAppIn, request: Request,
                            _: str = Depends(require_admin)) -> dict:
    async with request.app.state.db.session() as session:
        app = await store.update_application(
            session, app_id, **body.model_dump(exclude_none=True))
        if app is None:
            raise HTTPException(404, "application not found")
        await session.commit()
        full = await store.get_application_with_policy(session, app_id)
        return _app_dict(full)


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
        return _app_dict(full)


@admin_router.put("/applications/{app_id}/policy")
async def set_policy(app_id: str, body: PolicyIn, request: Request,
                     _: str = Depends(require_admin)) -> dict:
    async with request.app.state.db.session() as session:
        app = await store.get_application_with_policy(session, app_id)
        if app is None:
            raise HTTPException(404, "application not found")
        if body.residency is not None and body.residency not in RESIDENCY:
            raise HTTPException(422, f"bad residency {body.residency!r}")
        if body.pii_detection is not None and body.pii_detection not in PII_MODES:
            raise HTTPException(422, f"bad pii_detection {body.pii_detection!r}")
        if body.stream_mode is not None and body.stream_mode not in STREAM_MODES:
            raise HTTPException(422, f"bad stream_mode {body.stream_mode!r}")
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

        # Resolve the EFFECTIVE post-write residency/capture_io/pii_detection
        # once. The PUT is a merge (see below), so "effective value = body
        # value if provided, else the stored value, else the global default"
        # is what will actually land — every clamp below must key on these,
        # not on the stored row or on `body.*` alone, or a partial PUT can be
        # judged on a value that's about to be overwritten (the Critical this
        # fixed) or on a value that's already in effect but never mentioned
        # in this particular write (fix round 1: the redacted/pii-downgrade
        # clamp used to key on `body.pii_detection` alone, so a stored
        # pii=off app could be pushed to capture_io='redacted' — or flipped
        # to onprem_only — via a PUT that never mentioned pii_detection at
        # all, silently skipping the whole downgrade branch).
        settings = request.app.state.settings
        stored = app.policy
        eff_residency = (body.residency or (stored.residency if stored else None)) \
            or app.default_residency
        eff_capture = body.capture_io if body.capture_io is not None else (
            (stored.capture_io if stored else None)
            or settings.serving.observability.langfuse.capture_io)
        # NOTE: unlike eff_residency/eff_capture, this deliberately does NOT
        # fall through to the global default (`settings.serving.pii_detection`).
        # Per `docs/per-app-config.md`, unset/None for this field means
        # "inherit the deployment's live global detector" and is NOT itself a
        # downgrade — only an actual per-app override (this write's body, or
        # a previously-stored one) is. Falling back to the global default
        # here would make this clamp fire for every app whenever the
        # deployment's own global default happens to be non-presidio (e.g.
        # this test suite's SP_PII_DETECTION=regex), including apps that
        # never had a per-app override at all — a false positive that broke
        # `test_policy_full_capture_rejected_for_ineligible_app`'s "redacted
        # is always allowed" invariant. A non-presidio *global* default
        # combined with capture is a deployment-wide config-lint concern
        # (CLAUDE.md's boot-time cross-file lint), not a per-app write clamp.
        eff_pii = body.pii_detection if body.pii_detection is not None else (
            stored.pii_detection if stored else None)

        # --- PII downgrade clamp: fires whenever the EFFECTIVE post-write
        # pii_detection is off/regex — whether this write is what sets it or
        # it was already in effect from a prior write. Re-validates the
        # invariants (operator-flag-on, no unsanitized telemetry egress) on
        # every write while the app is in a pii-downgraded state, the same
        # fail-closed shape as the capture_io=='full' clamp below.
        #
        # Residency is deliberately NOT a condition here. It used to be: an
        # `onprem_only` app was refused a PII downgrade outright. That clamp
        # was over-broad and actively harmful, because detection and residency
        # are independent axes (`app/policy/context.py` computes the
        # onprem_only/allowed_providers floor from `app_policy.residency` and
        # never consults `pii_detection` — asserted by
        # tests/test_pii_detection_pipeline.py). Refusing the downgrade for
        # onprem_only apps therefore protected nothing, while pushing every
        # operator who needed unredacted prompts toward the documented
        # workaround of setting `residency: "any"` + an `allowed_providers`
        # pin — which is STRICTLY LESS SAFE: it trades a hard residency floor
        # that cloud cannot survive by construction (rule 3) for a mutable
        # provider allowlist, and it drops `ctx.onprem_only`, which is what
        # the step-9 streaming floor and the capture_io=='full' clamp key on.
        # An on-prem-only deployment is precisely the case where turning the
        # gate off is safest: the model is inside the customer's boundary and
        # nothing external ever sees the prompt.
        #
        # What still holds the line, unchanged: the deployment ceiling below,
        # the `redacted` check below, and the capture_io=='full' clamp further
        # down (which rejects `full` for onprem_only apps). Together those mean
        # an onprem_only app can reach pii_detection=off ONLY with
        # capture_io='off' — no telemetry I/O at all, so there is no egress
        # path for the unsanitized prompt. `_set_io` returns early on 'off' and
        # the runtime downgrade in app/obs/export.py only ever steps 'full'
        # down to 'redacted', never 'off' up, so that envelope cannot be
        # widened at request time. ---
        if eff_pii in ("off", "regex"):
            if not settings.serving.allow_app_pii_downgrade:
                raise HTTPException(
                    422, "per-app PII downgrade disabled on this deployment")
            if eff_capture == "redacted":
                alternatives = ("capture_io='off'." if eff_residency == "onprem_only"
                                else "capture_io='full' with the app's own Langfuse "
                                     "project, or capture_io='off'.")
                raise HTTPException(
                    422,
                    "capture_io='redacted' cannot be combined with pii_detection "
                    "off/regex: redacted telemetry reuses the gate's sanitized "
                    "prompt, which is raw when PII detection is off. Use "
                    + alternatives)
            # eff_capture == 'full' -> permitted here; the capture_io=='full'
            #   clamp below enforces non-onprem + own provisioned Langfuse on
            #   the EFFECTIVE value, so onprem_only + off + full is still 422.
            # eff_capture == 'off'  -> always safe, at any residency.

        # --- stream_unverified clamp: key on the EFFECTIVE stream_mode, not
        # body.stream_mode alone, so a stored stream_unverified app can't
        # reach onprem_only via a PUT that omits stream_mode entirely. No
        # global fallback needed (unlike eff_pii's doc-defined "inherit
        # global" semantics) -- stream_mode has no global default; None just
        # means "no streaming opt-in for this app." ---
        eff_stream_mode = body.stream_mode if body.stream_mode is not None else (
            stored.stream_mode if stored else None)
        if eff_stream_mode == "stream_unverified" and eff_residency == "onprem_only":
            raise HTTPException(
                422, "stream_unverified not allowed for onprem_only apps")

        # --- capture_io == 'full' clamp: key on EFFECTIVE capture, not
        # body.capture_io, so a stale stored 'full' is re-validated on every
        # write (e.g. a partial PUT that only touches pii_detection). ---
        if eff_capture == "full":
            from app.apps.langfuse_provision import load_app_langfuse
            if eff_residency == "onprem_only" or load_app_langfuse(app) is None:
                raise HTTPException(
                    422, "capture_io='full' stores raw telemetry and is allowed only "
                    "for a non-onprem application with its own provisioned Langfuse "
                    "project (residency beats eval; CLAUDE.md rules 2/8). Use 'redacted'.")

        # exclude_none=True makes this a MERGE, not a full replace: an
        # omitted field keeps its stored value instead of being wiped to
        # None (the Critical this fixed — see Part A in the task-9 brief).
        # Trade-off: clearing a field back to "inherit default" now requires
        # sending an explicit value, not omission.
        await store.upsert_policy(session, app_id, **body.model_dump(exclude_none=True))
        await session.commit()
        # `app.policy` was eager-loaded (possibly as None) above for the
        # eligibility check; expire it so upsert_policy's write is actually
        # re-read below instead of returning the stale cached relationship
        # (expire_on_commit=False on this sessionmaker).
        session.expire(app)
        full = await store.get_application_with_policy(session, app_id)
        return _app_dict(full)


# --- tokens -----------------------------------------------------------------

@admin_router.post("/applications/{app_id}/tokens")
async def create_token(app_id: str, body: TokenIn, request: Request,
                       _: str = Depends(require_admin)) -> dict:
    async with request.app.state.db.session() as session:
        if await store.get_application(session, app_id) is None:
            raise HTTPException(404, "application not found")
        raw = new_token()
        token = await store.add_token(session, application_id=app_id,
                                      raw_token=raw, name=body.name)
        await session.commit()
        # Plaintext returned ONCE — never retrievable again.
        return {"token": raw, "id": token.id, "prefix": token.prefix}


@admin_router.post("/tokens/{token_id}/revoke")
async def revoke_token(token_id: str, request: Request,
                       _: str = Depends(require_admin)) -> dict:
    async with request.app.state.db.session() as session:
        token = await store.revoke_token(session, token_id)
        if token is None:
            raise HTTPException(404, "token not found")
        await session.commit()
        return {"ok": True, "id": token_id}


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

def _static_validate_onboard(body: OnboardIn, s) -> None:
    tiers = set(s.routing.tiers)
    if body.tier not in tiers:
        raise HTTPException(422, f"bad tier {body.tier!r}")
    if not re.match(r"^https?://", body.endpoint):
        raise HTTPException(422, "endpoint must be an http(s) URL")
    if body.price_input < 0 or body.price_output < 0:
        raise HTTPException(422, "price must be >= 0")


@admin_router.post("/infra/models")
async def onboard_model(body: OnboardIn, request: Request,
                        _: str = Depends(require_admin)) -> dict:
    s = request.app.state.settings
    reg = request.app.state.registry
    yaml_names = {c.name for c in s.routing.model_pool}
    _static_validate_onboard(body, s)
    async with request.app.state.db.session() as session:
        if body.slug in yaml_names or await store.get_onboarded(session, body.slug):
            raise HTTPException(409, f"slug {body.slug!r} already exists")
        if body.allowed_app_ids:
            for aid in body.allowed_app_ids:
                if await store.get_application(session, aid) is None:
                    raise HTTPException(422, f"unknown application {aid!r}")
        ok, detail = await probe_endpoint(body.endpoint, body.slug, body.api_key_env)
        # `enabled` is explicit admin intent, ANDed with probe result: an
        # unreachable model can NEVER end up enabled (registry.has_healthy_onprem()
        # and onprem_only fail-closed depend on that), but a reachable model can
        # still be turned off on request.
        final_enabled = body.enabled and ok
        await store.upsert_onboarded(
            session, body.slug, display_name=body.display_name, provider="onprem",
            tier=body.tier, endpoint=body.endpoint, api_key_env=body.api_key_env,
            price_input=body.price_input, price_output=body.price_output,
            quality=body.quality, context_window=body.context_window,
            allowed_app_ids=body.allowed_app_ids, notes=body.notes,
            enabled=final_enabled, probe_status=("healthy" if ok else "unreachable"),
            probe_error=(None if ok else detail))
        await session.commit()
    await reg.refresh(request.app.state.db)
    return {"slug": body.slug, "enabled": final_enabled,
            "probe_status": "healthy" if ok else "unreachable",
            "probe_error": None if ok else detail}


@admin_router.put("/infra/models/{slug}")
async def edit_onboarded_model(slug: str, body: OnboardIn, request: Request,
                               _: str = Depends(require_admin)) -> dict:
    s = request.app.state.settings
    reg = request.app.state.registry
    _static_validate_onboard(body, s)
    async with request.app.state.db.session() as session:
        if await store.get_onboarded(session, slug) is None:
            raise HTTPException(404, f"onboarded model {slug!r} not found")
        if body.allowed_app_ids:
            for aid in body.allowed_app_ids:
                if await store.get_application(session, aid) is None:
                    raise HTTPException(422, f"unknown application {aid!r}")
        ok, detail = await probe_endpoint(body.endpoint, slug, body.api_key_env)
        # Same intent-AND-probe rule as onboard_model — see comment there.
        final_enabled = body.enabled and ok
        await store.upsert_onboarded(
            session, slug, display_name=body.display_name, provider="onprem",
            tier=body.tier, endpoint=body.endpoint, api_key_env=body.api_key_env,
            price_input=body.price_input, price_output=body.price_output,
            quality=body.quality, context_window=body.context_window,
            allowed_app_ids=body.allowed_app_ids, notes=body.notes,
            enabled=final_enabled, probe_status=("healthy" if ok else "unreachable"),
            probe_error=(None if ok else detail))
        await session.commit()
    await reg.refresh(request.app.state.db)
    return {"slug": slug, "enabled": final_enabled,
            "probe_status": "healthy" if ok else "unreachable",
            "probe_error": None if ok else detail}


@admin_router.delete("/infra/models/{slug}")
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
    async with request.app.state.db.session() as session:
        return [_onboarded_dict(m) for m in await store.list_onboarded(session)]
