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
from app.apps.admin_auth import SESSION_COOKIE
from app.apps.langfuse_provision import LangfuseProvisioner, encrypt_secret
from app.apps.tokens import new_token

admin_router = APIRouter(prefix="/admin", tags=["admin"])


# --- auth dependency --------------------------------------------------------

async def require_admin(request: Request) -> str:
    sid = request.cookies.get(SESSION_COOKIE)
    admin_id = request.app.state.admin_sessions.get(sid)
    if admin_id is None:
        raise HTTPException(401, "admin login required")
    return admin_id


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
            "max_spend_usd_per_day": p.max_spend_usd_per_day, "max_rpm": p.max_rpm}


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


class TokenIn(BaseModel):
    name: str = "default"


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
