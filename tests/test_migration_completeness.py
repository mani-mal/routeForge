"""Guard: scripts/migrate_app_policies.py covers every post-release column.

`create_all` never ALTERs an existing table, so a model column added after its
table's first release must be added by the migration script or a deployed DB
fails with a raw error. ORIGINAL_COLUMNS is frozen from git history: for each
table, the columns of its class at the commit that introduced it (listed above
each entry). Any other column must appear in the script's column lists.
"""
from __future__ import annotations

import importlib.util
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from app.apps.models import Base

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "migrate_app_policies.py"

ORIGINAL_COLUMNS: dict[str, frozenset[str]] = {
    # 36c45e3
    "applications": frozenset({"id", "name", "owner_email", "status", "default_residency", "langfuse_project_id", "langfuse_public_key", "langfuse_secret_key_enc", "created_at"}),
    # 36c45e3
    "app_tokens": frozenset({"id", "application_id", "token_hash", "prefix", "name", "status", "created_at", "last_used_at", "revoked_at"}),
    # d6ca082
    "service_credentials": frozenset({"id", "application_id", "token_hash", "prefix", "name", "status", "created_at", "last_used_at", "revoked_at"}),
    # 36c45e3
    "app_policies": frozenset({"application_id", "allowed_tiers", "allowed_providers", "allowed_models", "residency", "max_spend_usd_per_day", "max_rpm"}),
    # ab969c9
    "app_recognizers": frozenset({"application_id", "name", "regex", "score", "enabled"}),
    # ab969c9
    "app_sensitivity_rules": frozenset({"application_id", "position", "entities", "sensitivity", "risk_class", "residency", "require_verification"}),
    # 36c45e3
    "usage_records": frozenset({"id", "application_id", "request_id", "ts", "model", "provider", "tier", "prompt_tokens", "completion_tokens", "total_tokens", "cost_usd", "baseline_usd", "latency_ms", "sensitivity", "onprem_only", "status"}),
    # 3286adc
    "endpoint_observations": frozenset({"id", "ts", "model", "outcome", "latency_ms", "streamed", "ttft_ms"}),
    # e24eb2c
    "routing_outcomes": frozenset({"request_id", "application_id", "ts", "trace_id", "status", "app_policy_version", "endpoint_registry_version", "routing_policy_version", "workload", "workload_source", "tier_target", "complexity", "prompt_tokens_bucket", "sensitivity", "risk_class", "onprem_only", "detected_entity_types", "eligible", "rejected", "attempts", "chosen", "scoring", "circuit_bypass", "cost_usd", "latency_ms", "prompt_tokens", "completion_tokens", "verify_required", "verify_performed", "verify_passed", "verify_mode"}),
    # e24eb2c
    "outcome_feedback": frozenset({"id", "request_id", "application_id", "ts", "score_name", "value_num", "value_cat", "source"}),
    # b07c7d9
    "trajectory_envelopes": frozenset({"id", "application_id", "trajectory_key", "entity_types", "updated_at", "expires_at"}),
    # 1eb1e94
    "request_snapshots": frozenset({"request_id", "application_id", "ts", "summary_json", "full_json"}),
    # 36c45e3
    "admin_users": frozenset({"id", "email", "password_hash", "created_at"}),
    # 1cb9858
    "model_quality": frozenset({"id", "model_name", "task_class", "score", "source", "calibrated_at", "eval_run_id", "notes"}),
    "model_quality_bounds": frozenset({"id", "model_name", "task_class", "lower", "ci_level", "set_id", "calibrated_at"}),
    # adec76b
    "onboarded_models": frozenset({"slug", "display_name", "provider", "tier", "quality", "price_input", "price_output", "endpoint", "api_key_env", "context_window", "enabled", "allowed_app_ids", "probe_status", "probe_error", "notes", "updated_at"}),
    # d329a75
    "external_snapshots": frozenset({"id", "source", "origin", "retrieved_at", "index_version", "api_tier", "row_count"}),
    # d329a75
    "external_model_metrics": frozenset({"id", "snapshot_id", "ext_model_slug", "ext_model_name", "ext_creator", "intelligence_index", "coding_index", "math_index", "price_in", "price_out", "price_blended", "output_tps", "ttft_s", "ttfat_s", "prompt_length", "measurement_window"}),
    # d329a75
    "external_model_map": frozenset({"model", "ext_model_slug", "mapped_by", "mapped_at"}),
}

# Tables created whole by create_all on a deployed DB (no ALTER needed): they
# gained no column after their introducing commit, so nothing to migrate.
CREATED_WHOLE = frozenset({
    "service_credentials", "app_recognizers", "app_sensitivity_rules", "outcome_feedback", "trajectory_envelopes", "request_snapshots", "admin_users", "model_quality", "model_quality_bounds", "external_model_metrics", "external_model_map",
})

# Indexes added to an existing table after its first release.
LATER_INDEXES = {
    "ix_routing_outcomes_session", "ix_routing_outcomes_trajectory_key",
    "ix_outcome_feedback_app_request",
}


def _load():
    spec = importlib.util.spec_from_file_location("migrate_app_policies", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _script_columns(mod) -> dict[str, dict[str, str]]:
    return {t: {c[0]: c[2] for c in cols} for t, cols in mod.ALL_TABLES}


def test_every_table_is_classified() -> None:
    assert set(Base.metadata.tables) == set(ORIGINAL_COLUMNS)


def test_every_post_release_column_is_in_the_script() -> None:
    mod = _load()
    listed = _script_columns(mod)
    problems = []
    for name, table in Base.metadata.tables.items():
        for col in table.columns:
            if col.name in ORIGINAL_COLUMNS[name]:
                continue
            if name in CREATED_WHOLE:
                problems.append(f"{name}.{col.name} added after creation of a CREATED_WHOLE table")
            elif col.name not in listed.get(name, {}):
                problems.append(f"{name}.{col.name} missing from scripts/migrate_app_policies.py")
    assert not problems, "\n".join(problems)


def test_script_types_match_models() -> None:
    mod = _load()
    for table, cols in mod.ALL_TABLES:
        model = Base.metadata.tables[table]
        for name, pg, sqlite_t in cols:
            col = model.columns[name]
            if not col.nullable:
                assert "NOT NULL DEFAULT" in pg, f"{table}.{name} NOT NULL needs a DEFAULT"
            ours = col.type.compile(dialect=_pg()).split("(")[0].split()[0]
            theirs = pg.split("(")[0].split()[0]
            alias = {"FLOAT": "DOUBLE"}
            assert alias.get(ours, ours) == theirs, f"{table}.{name}: {ours} vs {pg}"


def _pg():
    from sqlalchemy.dialects import postgresql
    return postgresql.dialect()


def test_later_indexes_are_in_the_script_and_the_models() -> None:
    mod = _load()
    assert {i[0] for i in mod.INDEXES} == LATER_INDEXES
    model_idx = {i.name for t in Base.metadata.tables.values() for i in t.indexes}
    assert LATER_INDEXES <= model_idx


def _old_schema_db(path: Path) -> None:
    con = sqlite3.connect(path)
    for name, cols in ORIGINAL_COLUMNS.items():
        defs = ", ".join(f'"{c}"' for c in sorted(cols))
        con.execute(f'CREATE TABLE "{name}" ({defs})')
    con.commit()
    con.close()


@pytest.mark.parametrize("run", [1, 2])
def test_script_upgrades_old_schema_and_is_idempotent(tmp_path, run) -> None:
    db = tmp_path / "old.db"
    _old_schema_db(db)
    env = {"DATABASE_URL": f"sqlite+aiosqlite:///{db}", "PATH": "/usr/bin:/bin"}
    for _ in range(run):
        r = subprocess.run([sys.executable, str(SCRIPT)], env=env,
                           capture_output=True, text=True, timeout=120)
        assert r.returncode == 0, r.stdout + r.stderr
    con = sqlite3.connect(db)
    for name, table in Base.metadata.tables.items():
        have = {r[1] for r in con.execute(f'PRAGMA table_info("{name}")')}
        assert {c.name for c in table.columns} <= have, (name, set(table.columns.keys()) - have)
    idx = {r[1] for r in con.execute("SELECT type, name FROM sqlite_master WHERE type='index'")}
    assert LATER_INDEXES <= idx
    con.close()
