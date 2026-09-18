"""Admin API (Phase 3): applications, tokens, per-app policy, usage.

All routes except /admin/login require an admin session cookie. This surface is
separate from the token-authed serving API (/v1/*) — different auth, different
audience. The dashboard (Phase 4) is the only intended client.
"""
from __future__ import annotations

import datetime as dt
import logging
import re

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field, field_validator

from app.apps import store
from app.apps.admin_auth import SESSION_COOKIE, require_admin
from app.apps.langfuse_provision import LangfuseProvisioner, encrypt_secret
from app.apps.policy_lint import price_warnings
from app.apps.probe import probe_endpoint
from app.apps.policy import AppPolicyView
from app.apps.tokens import new_token
from app.policy.compile import PolicyViolation, compile_policy, validate_effective
from app.policy.effective import PROTECTED_ENTITIES
from app.policy.enums import (
    PII_MODES,
    POLICY_MODES,
    RESIDENCY,
    RISK_CLASS,
    SENSITIVITY,
    STREAM_MODES,
)
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
            "max_spend_usd_per_day": p.max_spend_usd_per_day,
            "max_cost_usd_per_request": p.max_cost_usd_per_request, "max_rpm": p.max_rpm,
            "compression_enabled": p.compression_enabled, "capture_io": p.capture_io,
            "pii_detection": p.pii_detection, "stream_mode": p.stream_mode,
            "policy_mode": p.policy_mode,
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
        )
        # An app's own provisioned Langfuse project is the one condition the
        # compiler cannot see: it is DB state, not policy. Raw capture requires
        # it and nothing ever relaxes that (CLAUDE.md rule 2), so it is passed
        # in rather than inferred.
        from app.apps.langfuse_provision import load_app_langfuse
        try:
            validate_effective(
                compile_policy(post_write, settings), settings,
                has_own_langfuse_project=load_app_langfuse(app) is not None)
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
        app_dict = _app_dict(full)
        from app.apps.policy import policy_view_from
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
    async with request.app.state.db.session() as session:
        return [_onboarded_dict(m) for m in await store.list_onboarded(session)]
