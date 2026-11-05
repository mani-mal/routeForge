"""B62: boot-time ORM-vs-live-DB column check (log + /health, never blocks boot)."""
import logging
import sqlite3

from fastapi.testclient import TestClient
from sqlalchemy import create_engine

from app.apps.models import Base
from app.main import create_app


def _db_file(tmp_path, monkeypatch, drop: str | None = None) -> None:
    path = tmp_path / "rf.db"
    eng = create_engine(f"sqlite:///{path}")
    Base.metadata.create_all(eng)
    eng.dispose()
    if drop:
        table, col = drop.split(".")
        con = sqlite3.connect(path)
        con.execute(f"ALTER TABLE {table} DROP COLUMN {col}")
        con.commit()
        con.close()
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{path}")


def test_missing_column_is_logged_and_exposed(tmp_path, monkeypatch, caplog):
    _db_file(tmp_path, monkeypatch, drop="routing_outcomes.routing_tokens")
    caplog.set_level(logging.ERROR, logger="serving.schema")
    with TestClient(create_app()) as c:
        body = c.get("/health").json()
    assert body["status"] == "ok"
    assert body["schema_missing"] == ["routing_outcomes.routing_tokens"]
    recs = [r for r in caplog.records if r.name == "serving.schema"]
    assert len(recs) == 1 and recs[0].levelno == logging.ERROR
    msg = recs[0].getMessage()
    assert "routing_outcomes.routing_tokens" in msg
    assert "scripts/migrate_app_policies.py" in msg


def test_complete_schema_is_empty(tmp_path, monkeypatch, caplog):
    _db_file(tmp_path, monkeypatch)
    caplog.set_level(logging.WARNING, logger="serving.schema")
    with TestClient(create_app()) as c:
        assert c.get("/health").json()["schema_missing"] == []
    assert not [r for r in caplog.records if r.name == "serving.schema"]


def test_check_error_warns_and_does_not_block_boot(monkeypatch, caplog):
    async def boom(engine):
        raise RuntimeError("nope")
    monkeypatch.setattr("app.main.missing_schema_columns", boom)
    caplog.set_level(logging.WARNING, logger="serving.schema")
    with TestClient(create_app()) as c:
        assert c.get("/health").json()["schema_missing"] == []
    assert any(r.levelno == logging.WARNING for r in caplog.records
               if r.name == "serving.schema")
