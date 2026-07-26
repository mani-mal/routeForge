"""Admin authentication: argon2 password hashing + an in-memory session store.

Scope is deliberately small (single-node, admin-only dashboard — see the spec):
sessions live in process memory, so a restart just means admins log in again.
A multi-node deployment would move sessions to Redis and add SSO (out of scope).
"""
from __future__ import annotations

import os
import secrets
import time
import uuid

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError
from fastapi import HTTPException, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.apps.models import AdminUser

_ph = PasswordHasher()

SESSION_COOKIE = "rf_admin_session"
_SESSION_TTL_S = 12 * 3600


async def require_admin(request: Request) -> str:
    """FastAPI dependency: resolve the admin session cookie to an admin id or
    raise 401. Shared by both the /admin/* surface and the /api/* console read
    surface — the console exposes cross-tenant metadata (spend, PII counts,
    entity types) so it is admin-gated, not public."""
    sid = request.cookies.get(SESSION_COOKIE)
    admin_id = request.app.state.admin_sessions.get(sid)
    if admin_id is None:
        raise HTTPException(401, "admin login required")
    return admin_id


def hash_password(password: str) -> str:
    return _ph.hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    try:
        return _ph.verify(password_hash, password)
    except VerifyMismatchError:
        return False
    except Exception:
        return False


async def get_admin_by_email(session: AsyncSession, email: str) -> AdminUser | None:
    stmt = select(AdminUser).where(AdminUser.email == email)
    return (await session.execute(stmt)).scalar_one_or_none()


async def create_admin(session: AsyncSession, *, email: str, password: str) -> AdminUser:
    admin = AdminUser(id=uuid.uuid4().hex, email=email,
                      password_hash=hash_password(password))
    session.add(admin)
    return admin


async def authenticate(session: AsyncSession, email: str, password: str
                       ) -> AdminUser | None:
    admin = await get_admin_by_email(session, email)
    if admin is None:
        # Hash a dummy password anyway to keep timing roughly constant.
        _ph.hash(password)
        return None
    if not verify_password(admin.password_hash, password):
        return None
    return admin


async def seed_admin(session: AsyncSession) -> None:
    """Seed one admin from ADMIN_EMAIL / ADMIN_PASSWORD (idempotent).

    Defaults to admin@routeforge.local / changeme for a first local run — these
    MUST be overridden via env for any real deployment."""
    email = os.environ.get("ADMIN_EMAIL", "admin@routeforge.local")
    password = os.environ.get("ADMIN_PASSWORD", "changeme")
    if await get_admin_by_email(session, email) is None:
        await create_admin(session, email=email, password=password)


class SessionStore:
    """In-memory admin sessions: opaque id -> (admin_id, expiry)."""

    def __init__(self, ttl_s: int = _SESSION_TTL_S) -> None:
        self._ttl = ttl_s
        self._sessions: dict[str, tuple[str, float]] = {}

    def create(self, admin_id: str) -> str:
        sid = secrets.token_urlsafe(32)
        self._sessions[sid] = (admin_id, time.monotonic() + self._ttl)
        return sid

    def get(self, sid: str | None) -> str | None:
        if not sid:
            return None
        entry = self._sessions.get(sid)
        if entry is None:
            return None
        admin_id, expiry = entry
        if time.monotonic() > expiry:
            self._sessions.pop(sid, None)
            return None
        return admin_id

    def delete(self, sid: str | None) -> None:
        if sid:
            self._sessions.pop(sid, None)
