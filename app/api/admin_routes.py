"""Admin API (Phase 3): applications, tokens, per-app policy, usage.

All routes except /admin/login require an admin session cookie. This surface is
separate from the token-authed serving API (/v1/*) — different auth, different
audience. The dashboard (Phase 4) is the only intended client.
"""
from __future__ import annotations

import datetime as dt

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel

from app.apps import store
from app.apps.admin_auth import SESSION_COOKIE, require_admin
from app.apps.langfuse_provision import LangfuseProvisioner, encrypt_secret
from app.apps.tokens import new_token
from app.policy.enums import RESIDENCY, RISK_CLASS, SENSITIVITY
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
            "compression_enabled": p.compression_enabled}


def _app_dict(app, *, with_children: bool = True) -> dict:
    d = {"id": app.id, "name": app.name, "owner_email": app.owner_email,
         "status": app.status, "default_residency": app.default_residency,
         "langfuse_project_id": app.langfuse_project_id,
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


class PolicyIn(BaseModel):
    allowed_tiers: list[str] | None = None
    allowed_providers: list[str] | None = None
    allowed_models: list[str] | None = None
    residency: str | None = None
    max_spend_usd_per_day: float | None = None
    max_rpm: int | None = None
    compression_enabled: bool | None = None   # None = inherit global Headroom toggle


class TokenIn(BaseModel):
    name: str = "default"


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


@admin_router.put("/applications/{app_id}/policy")
async def set_policy(app_id: str, body: PolicyIn, request: Request,
                     _: str = Depends(require_admin)) -> dict:
    async with request.app.state.db.session() as session:
        if await store.get_application(session, app_id) is None:
            raise HTTPException(404, "application not found")
        await store.upsert_policy(session, app_id, **body.model_dump())
        await session.commit()
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


# --- infra management (Run:ai declared facts + per-model engine/ops metadata) ----
# CLAUDE.md rule 4 (no auto-deploy): this surfaces DB-declared facts and a live
# backend health cross-check only. It never calls Run:ai/Dynamo/K8s and never
# renders or deploys anything. `runai_licensed`/`runai_project` are declarative
# (design decision: routeForge does not probe the cluster) — validated only for
# shape, taken at face value otherwise. Cloud models are never fleet infra:
# /admin/infra/models lists only routing.yaml's onprem pool members.

_ENGINES = {"vllm", "trtllm"}


class InfraSettingsIn(BaseModel):
    runai_licensed: bool | None = None
    runai_project: str | None = None
    default_engine: str | None = None


class InfraModelIn(BaseModel):
    engine: str | None = None
    enabled: bool | None = None
    replicas: int | None = None
    min_replicas: int | None = None
    max_replicas: int | None = None
    tensor_parallel: int | None = None
    gpu_type: str | None = None
    endpoint: str | None = None
    runai_scheduled: bool | None = None
    notes: str | None = None


def _infra_model_dict(pool_cand, row, health) -> dict:
    """One on-prem roster entry: routing.yaml identity + infra_models metadata
    (or defaults if unmanaged) + a live-health cross-check."""
    managed = row is not None
    engine = row.engine if managed else None
    enabled = row.enabled if managed else True  # unmanaged -> enabled (§6)
    present = health.ok and pool_cand.name in health.models
    if not health.ok:
        cross = "unreachable"
    elif present:
        cross = "serving"
    else:
        cross = "absent"
    return {
        "model_name": pool_cand.name,
        "display_name": pool_cand.display_name or pool_cand.name,
        "tier": pool_cand.tier,
        "provider": pool_cand.provider,
        "managed": managed,
        "engine": engine,
        "enabled": enabled,
        "replicas": row.replicas if managed else None,
        "min_replicas": row.min_replicas if managed else None,
        "max_replicas": row.max_replicas if managed else None,
        "tensor_parallel": row.tensor_parallel if managed else None,
        "gpu_type": row.gpu_type if managed else None,
        "endpoint": row.endpoint if managed else None,
        "runai_scheduled": row.runai_scheduled if managed else False,
        "notes": row.notes if managed else None,
        "cross_check": cross,  # serving | absent | unreachable
    }


@admin_router.get("/infra")
async def get_infra(request: Request, _: str = Depends(require_admin)) -> dict:
    s = request.app.state.settings
    async with request.app.state.db.session() as session:
        row = await store.get_infra_settings(session)
    return {
        "inference_backend": s.serving.inference_backend,  # read-only boot fact
        "runai_licensed": bool(row.runai_licensed) if row else False,
        "runai_project": row.runai_project if row else None,
        "default_engine": row.default_engine if row else "vllm",
    }


@admin_router.put("/infra")
async def put_infra(body: InfraSettingsIn, request: Request,
                    _: str = Depends(require_admin)) -> dict:
    if body.default_engine is not None and body.default_engine not in _ENGINES:
        raise HTTPException(422, f"bad default_engine {body.default_engine!r}")
    fields = {k: v for k, v in body.model_dump().items() if v is not None}
    async with request.app.state.db.session() as session:
        await store.upsert_infra_settings(session, **fields)
        await session.commit()
    await request.app.state.infra_roster.refresh(request.app.state.db)
    return {"ok": True}


@admin_router.get("/infra/models")
async def get_infra_models(request: Request, _: str = Depends(require_admin)) -> list:
    s = request.app.state.settings
    onprem = [c for c in s.routing.model_pool if c.provider == "onprem"]
    health = await request.app.state.backend.health()
    async with request.app.state.db.session() as session:
        rows = {r.model_name: r for r in await store.list_infra_models(session)}
    return [_infra_model_dict(c, rows.get(c.name), health) for c in onprem]


@admin_router.put("/infra/models/{slug}")
async def put_infra_model(slug: str, body: InfraModelIn, request: Request,
                          _: str = Depends(require_admin)) -> dict:
    if body.engine is not None and body.engine not in _ENGINES:
        raise HTTPException(422, f"bad engine {body.engine!r}")
    s = request.app.state.settings
    onprem_names = {c.name for c in s.routing.model_pool if c.provider == "onprem"}
    if slug not in onprem_names:
        raise HTTPException(404, "not an on-prem model in the pool")
    fields = {k: v for k, v in body.model_dump().items() if v is not None}
    async with request.app.state.db.session() as session:
        await store.upsert_infra_model(session, slug, **fields)
        await session.commit()
    await request.app.state.infra_roster.refresh(request.app.state.db)
    return {"ok": True}
