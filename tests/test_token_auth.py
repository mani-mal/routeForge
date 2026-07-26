"""Fail-closed token auth + end-to-end usage persistence (Phase 1)."""
import asyncio
import os

from fastapi.testclient import TestClient

from app.apps import store
from app.apps.db import Database
from app.main import create_app

_CLEAN = {"messages": [{"role": "user", "content": "What is 2 + 2?"}]}


def test_missing_token_is_rejected(client):
    r = client.post("/v1/chat/completions", json=_CLEAN)
    assert r.status_code == 401


def test_unknown_token_is_rejected(client):
    r = client.post("/v1/chat/completions",
                    headers={"Authorization": "Bearer not-a-real-token"}, json=_CLEAN)
    assert r.status_code == 401


def test_known_seeded_token_is_accepted(client):
    r = client.post("/v1/chat/completions",
                    headers={"Authorization": "Bearer demo-key"}, json=_CLEAN)
    assert r.status_code == 200
    assert r.json()["choices"][0]["message"]["content"]


def test_usage_row_persisted_for_served_request(tmp_path):
    # A temp file DB so this test's own event loop can read what the app wrote
    # in its portal loop (in-memory StaticPool is bound to a single loop).
    db_url = f"sqlite+aiosqlite:///{tmp_path}/rf.db"
    prev = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = db_url
    try:
        with TestClient(create_app()) as c:
            r = c.post("/v1/chat/completions",
                       headers={"Authorization": "Bearer acme-key"}, json=_CLEAN)
            assert r.status_code == 200

        async def read_rows():
            db = Database(url=db_url)
            try:
                async with db.session() as s:
                    return await store.list_usage(s)
            finally:
                await db.dispose()

        rows = asyncio.run(read_rows())
        assert len(rows) >= 1
        row = rows[0]
        assert row.application_id == "acme"
        assert row.total_tokens > 0
        assert row.status == "ok"
    finally:
        if prev is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = prev
