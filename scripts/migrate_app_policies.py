#!/usr/bin/env python
"""Idempotent column + index migration for every table in `app/apps/models.py`.

RouteForge has no migration tool: `Base.metadata.create_all` creates missing
TABLES but never ALTERs an existing one (see [[routeforge-schema-no-migrations]]).
So when per-app policy or an onboarded endpoint gains a new nullable column, an
already-deployed database (the shared Langfuse Postgres on a customer node)
keeps the old shape and any admin write touching the new field fails with a raw
DB error instead of a clean 422 — until this runs.

This adds every column added to an existing table after that table's first
release (and the later-added indexes) that is missing, and is
safe to run repeatedly: a column that already exists is left untouched. Works
against Postgres (deployment) and SQLite (dev/offline). Reads `DATABASE_URL`
exactly like the app (`app/apps/db.py`).

    DATABASE_URL=postgresql+asyncpg://... python scripts/migrate_app_policies.py

Prints one line per column (added / already present) and exits 0 on success. A
table that does not exist yet is reported (`table absent, skipped`) and does not
stop the other table's migration — each table runs in its own transaction.

**Run it BEFORE rolling the image**, never after. The old image never references
a column it does not know about, so applying the ALTERs early is always safe;
the new image reads them on every policy lookup / pool refresh, so on a database
missing one, requests fail with a raw DB error until it exists.

`tests/test_migration_completeness.py` fails when a model column is neither in
the frozen original set nor listed here, so this stays complete.

Every column here is nullable with no default, except the four NOT NULL columns
that carry a literal DEFAULT (`usage_records.quality`/`quality_source`,
`routing_outcomes.cache_hit`, `external_snapshots.dated_at_upload`). An existing row therefore gets
`NULL`, which is "inherit the deployment default" for an `app_policies` column
and "undeclared / unknown" for an `onboarded_models` one — so this migration on
its own changes no application's behaviour and no endpoint's eligibility.
"""
from __future__ import annotations

import asyncio
import os
import sys

from sqlalchemy import inspect, text
from sqlalchemy.exc import NoSuchTableError
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
    ("trajectory_mode", "VARCHAR(16)", "VARCHAR(16)"),
    ("model_aliases", "JSON", "JSON"),
    ("quality_floors", "JSON", "JSON"),
    ("quality_floor_mode", "VARCHAR(16)", "VARCHAR(16)"),
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
    # 2026-11-05 model metrics slice 4 — display-only monthly budget. NULL = no
    # budget shown; never read by enforcement or by AppPolicy.version().
    ("monthly_budget_usd", "DOUBLE PRECISION", "FLOAT"),
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
    # 2026-09-19 P2a — the endpoint's declared cached-input rate. NULL =
    # undeclared -> bill cached tokens at price_input, i.e. today's behaviour.
    ("price_cached_input", "DOUBLE PRECISION", "FLOAT"),
    # 2026-09-22 B50 — where context_window came from: "operator" | "probed".
    # NULL on every existing row, which reads as "unknown provenance" and
    # changes no stored context_window.
    ("context_window_source", "VARCHAR(16)", "VARCHAR(16)"),
    # 2026-11-11 B68 -- the endpoint's API key as a Fernet token under
    # ENCRYPTION_KEY. NULL on every existing row = no stored key; the row keeps
    # using its api_key_env (or the provider default) exactly as before.
    ("api_key_enc", "TEXT", "TEXT"),
]

# 2026-09-19 P2a cached-token accounting. Nullable with no default: an existing
# row reads NULL, which every reader coerces to 0 ("no cached tokens recorded"),
# so this migration changes no cost figure already written.
USAGE_TABLE = "usage_records"

USAGE_COLUMNS: list[tuple[str, str, str]] = [
    ("quality", "DOUBLE PRECISION NOT NULL DEFAULT 0.0", "FLOAT NOT NULL DEFAULT 0.0"),
    ("quality_source", "VARCHAR(20) NOT NULL DEFAULT '?'", "VARCHAR(20) NOT NULL DEFAULT '?'"),
    ("cached_prompt_tokens", "INTEGER", "INTEGER"),
    # 2026-11-04 model metrics slice 1: per-lever savings. Nullable, no default;
    # NULL routing_saved_usd marks a pre-change row (baseline_usd there is the
    # old, cache-adjusted figure).
    ("compression_saved_usd", "DOUBLE PRECISION", "FLOAT"),
    ("routing_saved_usd", "DOUBLE PRECISION", "FLOAT"),
    ("prompt_cache_saved_usd", "DOUBLE PRECISION", "FLOAT"),
    ("semantic_cache_saved_usd", "DOUBLE PRECISION", "FLOAT"),
]


OBSERVATIONS_TABLE = "endpoint_observations"

OBSERVATIONS_COLUMNS: list[tuple[str, str, str]] = [
    ("completion_tokens", "INTEGER", "INTEGER"),
]

OUTCOMES_TABLE = "routing_outcomes"

OUTCOMES_COLUMNS: list[tuple[str, str, str]] = [
    ("route_reason", "VARCHAR(16)", "VARCHAR(16)"),
    ("session", "VARCHAR(128)", "VARCHAR(128)"),
    ("cache_hit", "BOOLEAN NOT NULL DEFAULT FALSE", "BOOLEAN NOT NULL DEFAULT 0"),
    ("requested_model", "VARCHAR(100)", "VARCHAR(100)"),
    ("requested_alias", "VARCHAR(64)", "VARCHAR(64)"),
    ("max_tokens", "INTEGER", "INTEGER"),
    ("conflicting_core", "JSON", "JSON"),
    ("routing_tokens", "INTEGER", "INTEGER"),
    ("trajectory_key", "VARCHAR(32)", "VARCHAR(32)"),
    ("inherited_entity_types", "JSON", "JSON"),
    ("finish_reason", "VARCHAR(16)", "VARCHAR(16)"),
    ("reply_has_text", "BOOLEAN", "BOOLEAN"),
    ("reply_tool_calls", "INTEGER", "INTEGER"),
    ("reasoning_tokens", "INTEGER", "INTEGER"),
]

APPLICATIONS_TABLE = "applications"

APPLICATIONS_COLUMNS: list[tuple[str, str, str]] = [
    ("langfuse_host", "VARCHAR(255)", "VARCHAR(255)"),
]

TOKENS_TABLE = "app_tokens"

TOKENS_COLUMNS: list[tuple[str, str, str]] = [
    ("expires_at", "TIMESTAMP WITH TIME ZONE", "DATETIME"),
    ("session_binding", "VARCHAR(128)", "VARCHAR(128)"),
    ("issued_by", "VARCHAR(64)", "VARCHAR(64)"),
]

EXTERNAL_SNAPSHOTS_TABLE = "external_snapshots"

EXTERNAL_SNAPSHOTS_COLUMNS: list[tuple[str, str, str]] = [
    ("dated_at_upload", "BOOLEAN NOT NULL DEFAULT FALSE", "BOOLEAN NOT NULL DEFAULT 0"),
]

# (index name, table, columns) -- indexes added to a table after its first
# release. Created with IF NOT EXISTS (CONCURRENTLY on Postgres).
INDEXES: list[tuple[str, str, str]] = [
    ("ix_routing_outcomes_session", "routing_outcomes", "session"),
    ("ix_routing_outcomes_trajectory_key", "routing_outcomes", "trajectory_key"),
    ("ix_outcome_feedback_app_request", "outcome_feedback",
     "application_id, request_id"),
]

ALL_TABLES: list[tuple[str, list[tuple[str, str, str]]]] = [
    (APPLICATIONS_TABLE, APPLICATIONS_COLUMNS),
    (TOKENS_TABLE, TOKENS_COLUMNS),
    (TABLE, COLUMNS),
    (MODELS_TABLE, MODEL_COLUMNS),
    (USAGE_TABLE, USAGE_COLUMNS),
    (OBSERVATIONS_TABLE, OBSERVATIONS_COLUMNS),
    (OUTCOMES_TABLE, OUTCOMES_COLUMNS),
    (EXTERNAL_SNAPSHOTS_TABLE, EXTERNAL_SNAPSHOTS_COLUMNS),
]


async def _create_indexes(engine, dialect: str) -> None:
    print("  indexes:")
    for name, table, cols in INDEXES:
        # CONCURRENTLY cannot run inside a transaction block: use AUTOCOMMIT.
        conc = "CONCURRENTLY " if dialect == "postgresql" else ""
        conn_engine = engine.execution_options(isolation_level="AUTOCOMMIT")
        async with conn_engine.connect() as conn:
            try:
                present = await conn.run_sync(
                    lambda sync_conn, t=table: {
                        i["name"] for i in inspect(sync_conn).get_indexes(t)})
            except NoSuchTableError:
                print(f"    ~ {name}: table {table} absent, skipped")
                continue
            if name in present:
                print(f"    = {name}: already present")
                continue
            await conn.execute(text(
                f"CREATE INDEX {conc}IF NOT EXISTS {name} ON {table} ({cols})"))
            print(f"    + {name}: created")


async def _run() -> int:
    url = os.environ.get("DATABASE_URL") or DEFAULT_SQLITE_URL
    # Never print credentials; show only the scheme/host shape.
    safe = url.split("@")[-1] if "@" in url else url
    print(f"migrate_app_policies: target …@{safe}")
    engine = create_async_engine(url)
    dialect = engine.dialect.name  # "postgresql" | "sqlite" | ...
    try:
        # ONE TRANSACTION PER TABLE, and the missing-table case handled inside
        # it. A database can legitimately have one of these tables and not the
        # other (`onboarded_models` is created on first boot of a version that
        # has it), and `inspect(...).get_columns` raises `NoSuchTableError` for
        # the absent one. Under a single shared `engine.begin()` that exception
        # unwound the whole block, so on Postgres — where DDL is transactional,
        # and Postgres is what the deployed nodes run — the `app_policies`
        # ALTERs that had already succeeded ROLLED BACK too, and the operator
        # got a traceback instead of the per-column log. Per-table scoping means
        # a table that is absent is reported and skipped, and the other table's
        # migration still commits.
        for table, columns in ALL_TABLES:
            print(f"  {table}:")
            async with engine.begin() as conn:
                try:
                    existing = await conn.run_sync(
                        lambda sync_conn, t=table: {
                            c["name"] for c in inspect(sync_conn).get_columns(t)})
                except NoSuchTableError:
                    # Not an error: nothing to migrate, and `create_all` will
                    # build it with every column already in place.
                    print("    ~ table absent, skipped")
                    continue
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
        await _create_indexes(engine, dialect)
    finally:
        await engine.dispose()
    print("migrate_app_policies: done")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(_run()))
