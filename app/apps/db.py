"""Async SQLAlchemy engine + a small Database wrapper.

Design notes that matter here:

- **Offline by default.** With no `DATABASE_URL`, dev uses a local SQLite file
  and tests use in-memory SQLite — no DB server needed. Deployment points
  `DATABASE_URL` at the shared Langfuse Postgres (install the `postgres` extra).
- **Lazy, in-loop init.** The engine is created in `create_app()` (synchronous,
  no I/O). Schema creation + demo seeding happen on first DB access, inside
  whatever event loop is serving the request. This deliberately avoids binding
  connections to the throwaway `asyncio.run` loop `create_app` uses for backend
  health checks (see [[routeforge-event-loop-bug]] — a real bug we already hit).
- **Test isolation.** In-memory SQLite with `StaticPool` keeps one connection
  per engine, so each `create_app()` gets its own isolated database.
"""
from __future__ import annotations

import asyncio
import os
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase
from sqlalchemy.pool import StaticPool

DEFAULT_SQLITE_URL = "sqlite+aiosqlite:///./routeforge.db"


class Base(DeclarativeBase):
    """Declarative base for all routeForge application-layer tables."""


def _resolve_url(url: str | None) -> str:
    return url or os.environ.get("DATABASE_URL") or DEFAULT_SQLITE_URL


def _is_memory_sqlite(url: str) -> bool:
    return url.startswith("sqlite") and (":memory:" in url or url.endswith("://"))


def make_engine(url: str | None = None):
    """Create an async engine. SQLite gets `check_same_thread=False` (aiosqlite
    runs on a worker thread); in-memory SQLite additionally uses StaticPool so
    the one connection — and thus the database — survives across requests."""
    url = _resolve_url(url)
    if url.startswith("sqlite"):
        connect_args = {"check_same_thread": False}
        if _is_memory_sqlite(url):
            return create_async_engine(
                url, connect_args=connect_args, poolclass=StaticPool)
        return create_async_engine(url, connect_args=connect_args)
    return create_async_engine(url)


class Database:
    """Owns the engine + sessionmaker and lazily initializes schema/seed once.

    Stored on `app.state.db`. Route handlers use `async with db.session() as s`.
    """

    def __init__(self, url: str | None = None, *, seed=None) -> None:
        self.engine = make_engine(url)
        self.sessionmaker = async_sessionmaker(self.engine, expire_on_commit=False)
        self._ready = False
        self._lock = asyncio.Lock()
        # Injected to avoid an import cycle (bootstrap imports models -> db).
        self._seed = seed

    async def ensure_ready(self) -> None:
        if self._ready:
            return
        async with self._lock:
            if self._ready:  # someone won the race while we waited
                return
            async with self.engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            if self._seed is not None:
                async with self.sessionmaker() as s:
                    await self._seed(s)
                    await s.commit()
            self._ready = True

    @asynccontextmanager
    async def session(self):
        await self.ensure_ready()
        async with self.sessionmaker() as s:
            yield s

    async def dispose(self) -> None:
        await self.engine.dispose()
