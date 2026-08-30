#!/usr/bin/env python
"""Idempotent column migration for `app_policies`.

RouteForge has no migration tool: `Base.metadata.create_all` creates missing
TABLES but never ALTERs an existing one (see [[routeforge-schema-no-migrations]]).
So when per-app policy gains a new nullable column, an already-deployed database
(the shared Langfuse Postgres on a customer node) keeps the old shape and any
admin write touching the new field fails with a raw DB error instead of a clean
422 — until this runs.

This adds every post-original `app_policies` column that is missing, and is safe
to run repeatedly: a column that already exists is left untouched. Works against
Postgres (deployment) and SQLite (dev/offline). Reads `DATABASE_URL` exactly like
the app (`app/apps/db.py`).

    DATABASE_URL=postgresql+asyncpg://... python scripts/migrate_app_policies.py

Prints one line per column (added / already present) and exits 0 on success.
"""
from __future__ import annotations

import asyncio
import os
import sys

from sqlalchemy import inspect, text
from sqlalchemy.ext.asyncio import create_async_engine

# Mirror app/apps/db.py's default so the script stands alone (no `app` import,
# so it runs from any cwd without PYTHONPATH juggling).
DEFAULT_SQLITE_URL = "sqlite+aiosqlite:///./routeforge.db"

TABLE = "app_policies"

# (column, postgres-type, sqlite-type). Ordered oldest→newest; all nullable.
# Older columns are included too so a very old DB is fully caught up — every
# statement is guarded by an existence check, so listing an existing column is
# a no-op.
COLUMNS: list[tuple[str, str, str]] = [
    ("compression_enabled", "BOOLEAN", "BOOLEAN"),
    ("capture_io", "VARCHAR(16)", "VARCHAR(16)"),
    ("pii_detection", "VARCHAR(16)", "VARCHAR(16)"),
    ("stream_mode", "VARCHAR(24)", "VARCHAR(24)"),
    # §4.2b — the narrow PII lever.
    ("pii_entity_denylist", "JSON", "JSON"),
    ("pii_score_threshold", "DOUBLE PRECISION", "FLOAT"),
]


async def _run() -> int:
    url = os.environ.get("DATABASE_URL") or DEFAULT_SQLITE_URL
    # Never print credentials; show only the scheme/host shape.
    safe = url.split("@")[-1] if "@" in url else url
    print(f"migrate_app_policies: target …@{safe}")
    engine = create_async_engine(url)
    try:
        async with engine.begin() as conn:
            existing = await conn.run_sync(
                lambda sync_conn: {c["name"] for c in inspect(sync_conn).get_columns(TABLE)})
            dialect = engine.dialect.name  # "postgresql" | "sqlite" | ...
            for name, pg_type, sqlite_type in COLUMNS:
                if name in existing:
                    print(f"  = {name}: already present")
                    continue
                col_type = sqlite_type if dialect == "sqlite" else pg_type
                # ADD COLUMN with a plain nullable type is valid DDL on both
                # Postgres and SQLite; the existence check above makes it
                # idempotent without relying on `IF NOT EXISTS` (SQLite lacks it).
                await conn.execute(
                    text(f'ALTER TABLE {TABLE} ADD COLUMN {name} {col_type}'))
                print(f"  + {name}: added ({col_type})")
    finally:
        await engine.dispose()
    print("migrate_app_policies: done")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(_run()))
