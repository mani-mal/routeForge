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
import logging
import os
import weakref
from contextlib import asynccontextmanager
from typing import Callable

from sqlalchemy import event, inspect as sa_inspect
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase, Session
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


# --- auth-view invalidation (B86) --------------------------------------------
# The request path caches resolved tokens (app/apps/auth_cache.py). Any
# committed write to a table that cached view is built from clears it, in this
# process, through the hooks below -- whatever code path made the write (admin
# API, service credentials, scripts sharing the Database). Registered once,
# globally, on the sync Session class; a session reaches its own Database's
# callbacks through `session.info` (shared, read-only), and the per-session
# "a relevant write was flushed" mark lives in a WeakSet, never in that shared
# dict.
AUTH_VIEW_TABLES = frozenset({"applications", "app_policies", "app_tokens"})
# Execution option for a bulk UPDATE that must NOT invalidate (the request
# path's own `last_used_at` stamp).
AUTH_STAMP_OPTION = "routeforge_auth_stamp"
_AUTH_LISTENERS_KEY = "routeforge_auth_listeners"
_auth_dirty: "weakref.WeakSet[Session]" = weakref.WeakSet()


def _changes_auth_view(obj, *, dirty: bool) -> bool:
    if getattr(obj, "__tablename__", None) not in AUTH_VIEW_TABLES:
        return False
    if not dirty:
        return True
    # A token whose only change is the `last_used_at` stamp: not a view change.
    changed = {a.key for a in sa_inspect(obj).attrs if a.history.has_changes()}
    return bool(changed - {"last_used_at"})


@event.listens_for(Session, "after_flush")
def _note_auth_writes(session, _flush_context) -> None:
    if _AUTH_LISTENERS_KEY not in session.info or session in _auth_dirty:
        return
    if (any(_changes_auth_view(o, dirty=False) for o in session.new)
            or any(_changes_auth_view(o, dirty=False) for o in session.deleted)
            or any(_changes_auth_view(o, dirty=True) for o in session.dirty)):
        _auth_dirty.add(session)


@event.listens_for(Session, "do_orm_execute")
def _note_bulk_auth_writes(state) -> None:
    session = state.session
    if _AUTH_LISTENERS_KEY not in session.info:
        return
    if not (state.is_update or state.is_delete):
        return
    if state.execution_options.get(AUTH_STAMP_OPTION):
        return
    tables = {getattr(m.local_table, "name", None) for m in state.all_mappers}
    if tables & AUTH_VIEW_TABLES:
        _auth_dirty.add(session)


@event.listens_for(Session, "after_commit")
def _fire_auth_listeners(session) -> None:
    if session not in _auth_dirty:
        return
    _auth_dirty.discard(session)
    for cb in session.info.get(_AUTH_LISTENERS_KEY, ()):
        try:
            cb()
        except Exception:  # an invalidation hook must never fail a commit
            logging.getLogger("serving.api").exception("auth invalidation hook failed")


@event.listens_for(Session, "after_rollback")
def _forget_auth_writes(session) -> None:
    _auth_dirty.discard(session)


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
    # pool_pre_ping (B88): test a pooled connection on checkout and replace it
    # if dead, so one connection broken by a cancelled write (or a Postgres
    # restart) cannot fail an unrelated request with a 500.
    return create_async_engine(url, pool_pre_ping=True)


class Database:
    """Owns the engine + sessionmaker and lazily initializes schema/seed once.

    Stored on `app.state.db`. Route handlers use `async with db.session() as s`.
    """

    def __init__(self, url: str | None = None, *, seed=None) -> None:
        self.engine = make_engine(url)
        # B86: callbacks run after any commit that changed an app, its policy
        # or a token (see `_fire_auth_listeners`). Shared by reference into
        # every session's `info`.
        self._auth_listeners: list[Callable[[], None]] = []
        self.sessionmaker = async_sessionmaker(
            self.engine, expire_on_commit=False,
            info={_AUTH_LISTENERS_KEY: self._auth_listeners})
        self._ready = False
        self._lock = asyncio.Lock()
        # Injected to avoid an import cycle (bootstrap imports models -> db).
        self._seed = seed
        # Serializes `session()` bodies on SQLite only. `StaticPool` hands out
        # the SAME underlying DBAPI connection to every concurrent AsyncSession
        # (that's how one in-memory sqlite db survives across requests at all —
        # see the module docstring), but sqlite3 supports only one transaction
        # per connection. Two coroutines' sessions interleaving BEGIN/COMMIT on
        # that shared connection (e.g. two /v1/chat/completions requests in a
        # traffic burst, each opening 2-3 `db.session()` blocks) silently loses
        # writes — no exception, a session's commit just lands on the wrong
        # transaction. This never happens on Postgres (every real deploy, and
        # what B3 is specifically for): each session gets a real pooled
        # connection there, so the lock is a no-op skip on that path.
        self._sqlite_write_lock = (
            asyncio.Lock() if self.engine.url.drivername.startswith("sqlite") else None)

    def on_auth_change(self, callback: Callable[[], None]) -> None:
        """Run `callback` after every commit (through this Database) that
        changed an application, its policy, or a token -- other than the
        `last_used_at` stamp. Used to clear the auth cache (B86)."""
        self._auth_listeners.append(callback)

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
        if self._sqlite_write_lock is None:
            async with self.sessionmaker() as s:
                yield s
            return
        async with self._sqlite_write_lock:
            async with self.sessionmaker() as s:
                yield s

    async def dispose(self) -> None:
        await self.engine.dispose()


def _missing_columns_sync(sync_conn) -> list[str]:
    from sqlalchemy import inspect
    insp = inspect(sync_conn)
    existing_tables = set(insp.get_table_names())
    missing: list[str] = []
    for table in Base.metadata.sorted_tables:
        if table.name not in existing_tables:
            continue
        live = {c["name"] for c in insp.get_columns(table.name)}
        missing.extend(f"{table.name}.{c.name}" for c in table.columns
                       if c.name not in live)
    return sorted(missing)


async def missing_schema_columns(engine) -> list[str]:
    """B62: every `table.column` the ORM declares that the live table lacks
    (tables that do not exist are skipped: create_all makes them). Names only,
    content-free. `create_all` never ALTERs, so a deploy that skipped the
    ALTER step shows up here instead of as swallowed per-request write errors."""
    async with engine.connect() as conn:
        return await conn.run_sync(_missing_columns_sync)
