#!/usr/bin/env python
"""Idempotent column migration for `app_policies` and `onboarded_models`.

RouteForge has no migration tool: `Base.metadata.create_all` creates missing
TABLES but never ALTERs an existing one (see [[routeforge-schema-no-migrations]]).
So when per-app policy or an onboarded endpoint gains a new nullable column, an
already-deployed database (the shared Langfuse Postgres on a customer node)
keeps the old shape and any admin write touching the new field fails with a raw
DB error instead of a clean 422 — until this runs.

This adds every post-original column that is missing on either table, and is
safe to run repeatedly: a column that already exists is left untouched. Works
against Postgres (deployment) and SQLite (dev/offline). Reads `DATABASE_URL`
exactly like the app (`app/apps/db.py`).

    DATABASE_URL=postgresql+asyncpg://... python scripts/migrate_app_policies.py

Prints one line per column (added / already present) and exits 0 on success.

**Run it BEFORE rolling the image**, never after. The old image never references
a column it does not know about, so applying the ALTERs early is always safe;
the new image reads them on every policy lookup / pool refresh, so on a database
missing one, requests fail with a raw DB error until it exists.

Every column here is nullable with no default. An existing row therefore gets
`NULL`, which is "inherit the deployment default" for an `app_policies` column
and "undeclared / unknown" for an `onboarded_models` one — so this migration on
its own changes no application's behaviour and no endpoint's eligibility.
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
    ("policy_mode", "VARCHAR(16)", "VARCHAR(16)"),
    ("pii_exempt_patterns", "JSON", "JSON"),
    # 2026-09-15 onboarding conformance — per-app per-request cost cap.
    ("max_cost_usd_per_request", "DOUBLE PRECISION", "FLOAT"),
    # 2026-09-18 P1a policy compiler — the four data-handling axes
    # (app/policy/compile.py). No behavior wired to them yet (Task 2 only);
    # added here so the column exists before any later task's image reads them.
    ("model_input", "VARCHAR(16)", "VARCHAR(16)"),
    ("client_output", "VARCHAR(16)", "VARCHAR(16)"),
    ("content_integrity", "VARCHAR(32)", "VARCHAR(32)"),
    ("escalate_residency_on_detection", "BOOLEAN", "BOOLEAN"),
    # 2026-09-19 P1b endpoint data handling — the app-side REQUIREMENTS
    # (app/apps/models.py:AppPolicy, design §3). NULL = no constraint:
    # `allowed_regions` NULL means "no regional constraint" and
    # `require_no_retention` NULL reads as false, so an existing app's
    # candidate set is unchanged until an operator sets one.
    ("allowed_regions", "JSON", "JSON"),
    ("require_no_retention", "BOOLEAN", "BOOLEAN"),
]

# The endpoint-side half of the same feature: what an operator DECLARED about
# an onboarded endpoint (design §2). NULL = undeclared, which resolves to
# "unknown" in `ModelRegistry.effective_pool()`. That is the state every
# already-onboarded endpoint lands in, and it blocks nothing: an `unknown`
# endpoint is only excluded for an app that declared a matching requirement.
MODELS_TABLE = "onboarded_models"

MODEL_COLUMNS: list[tuple[str, str, str]] = [
    ("region", "VARCHAR(64)", "VARCHAR(64)"),
    ("prompt_retention", "VARCHAR(16)", "VARCHAR(16)"),
    ("accepts_raw_sensitive", "BOOLEAN", "BOOLEAN"),
]


async def _run() -> int:
    url = os.environ.get("DATABASE_URL") or DEFAULT_SQLITE_URL
    # Never print credentials; show only the scheme/host shape.
    safe = url.split("@")[-1] if "@" in url else url
    print(f"migrate_app_policies: target …@{safe}")
    engine = create_async_engine(url)
    try:
        async with engine.begin() as conn:
            dialect = engine.dialect.name  # "postgresql" | "sqlite" | ...
            for table, columns in ((TABLE, COLUMNS), (MODELS_TABLE, MODEL_COLUMNS)):
                print(f"  {table}:")
                existing = await conn.run_sync(
                    lambda sync_conn, t=table: {
                        c["name"] for c in inspect(sync_conn).get_columns(t)})
                for name, pg_type, sqlite_type in columns:
                    if name in existing:
                        print(f"    = {name}: already present")
                        continue
                    col_type = sqlite_type if dialect == "sqlite" else pg_type
                    # ADD COLUMN with a plain nullable type is valid DDL on both
                    # Postgres and SQLite; the existence check above makes it
                    # idempotent without relying on `IF NOT EXISTS` (SQLite lacks it).
                    await conn.execute(
                        text(f'ALTER TABLE {table} ADD COLUMN {name} {col_type}'))
                    print(f"    + {name}: added ({col_type})")
    finally:
        await engine.dispose()
    print("migrate_app_policies: done")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(_run()))
