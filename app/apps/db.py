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


def _patch_aiosqlite_wakeup() -> None:
    """Work around a runtime-specific aiosqlite wakeup failure.

    In this Python 3.12 runtime the aiosqlite worker completes the connection
    future but does not wake the event loop waiting on it. Polling the already
    thread-safe Future at scheduler checkpoints preserves aiosqlite's worker
    thread and avoids blocking the loop. The patch is intentionally limited to
    the connection handshake; normal query execution remains aiosqlite's code.
    """
    try:
        import aiosqlite.core
    except ImportError:
        return
    connection_type = aiosqlite.core.Connection
    if getattr(connection_type, "_routeforge_wakeup_patch", False):
        return

    async def connect_without_broken_wakeup(self):
        if self._connection is None:
            future = asyncio.get_running_loop().create_future()
            self._tx.put_nowait((future, self._connector))
            while not future.done():
                await asyncio.sleep(0)
            self._connection = future.result()
        return self

    async def execute_without_broken_wakeup(self, fn, *args, **kwargs):
        if not self._running or not self._connection:
            raise ValueError("Connection closed")
        future = asyncio.get_running_loop().create_future()
        from functools import partial
        self._tx.put_nowait((future, partial(fn, *args, **kwargs)))
        while not future.done():
            await asyncio.sleep(0)
        return future.result()

    connection_type._connect = connect_without_broken_wakeup
    connection_type._execute = execute_without_broken_wakeup
    connection_type._routeforge_wakeup_patch = True


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
        _patch_aiosqlite_wakeup()
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
        # B18: the in-memory sqlite engine uses StaticPool — ONE shared aiosqlite
        # connection for every concurrently checked-out AsyncSession. Concurrent
        # transactions interleave on that single connection and lose committed
        # writes. Serialize whole sessions in that case only. Real pools
        # (file-sqlite, Postgres) give each session its own connection and are
        # left untouched. Safe (non-reentrant): db.session() is never nested.
        self._serialize_sessions = _is_memory_sqlite(_resolve_url(url))
        self._session_lock = asyncio.Lock()

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
        if self._serialize_sessions:
            async with self._session_lock:
                async with self.sessionmaker() as s:
                    yield s
        else:
            async with self.sessionmaker() as s:
                yield s

    async def dispose(self) -> None:
        await self.engine.dispose()
