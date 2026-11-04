"""Per-lever savings persisted on usage_records (model metrics slice 1, Task 3)."""
from __future__ import annotations

import importlib.util
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.apps import store
from app.apps.models import UsageRecord
from app.main import create_app
from tests.onboarding.harness import onboard

HELLO = {"messages": [{"role": "user", "content": "hello"}]}
DEMO = {"Authorization": "Bearer demo-key"}


def _usage(c: TestClient) -> list[UsageRecord]:
    async def go():
        async with c.app.state.db.session() as session:
            return list((await session.execute(select(UsageRecord))).scalars())
    return c.portal.call(go)


def test_served_request_persists_four_levers():
    with TestClient(create_app(), raise_server_exceptions=False) as c:
        r = c.post("/v1/chat/completions", headers=DEMO, json=HELLO)
        assert r.status_code == 200, r.text
        cost = r.json()["cost"]
        rows = [u for u in _usage(c) if u.status == "ok"]
        assert len(rows) == 1
        row = rows[0]
        assert row.compression_saved_usd == cost["compression_saved_usd"]
        assert row.routing_saved_usd == cost["routing_saved_usd"]
        assert row.prompt_cache_saved_usd == cost["prompt_cache_saved_usd"]
        assert row.semantic_cache_saved_usd == cost.get("cache_saved_usd", 0.0)
        assert row.semantic_cache_saved_usd == 0.0
        assert row.baseline_usd == cost["baseline_usd"]


def test_rejected_request_persists_zero_levers():
    with TestClient(create_app(), raise_server_exceptions=False) as c:
        app = onboard(c, "sav-budget", max_spend_usd_per_day=0.0)
        assert app.chat(HELLO).status_code == 429
        rows = [u for u in _usage(c) if u.status == "rejected"]
        assert len(rows) == 1
        row = rows[0]
        for col in ("compression_saved_usd", "routing_saved_usd",
                    "prompt_cache_saved_usd", "semantic_cache_saved_usd"):
            assert getattr(row, col) == 0.0, col


def test_migration_lists_every_new_usage_column():
    path = Path(__file__).resolve().parents[1] / "scripts" / "migrate_app_policies.py"
    spec = importlib.util.spec_from_file_location("migrate_app_policies", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    names = {c[0] for c in mod.USAGE_COLUMNS}
    assert {"compression_saved_usd", "routing_saved_usd",
            "prompt_cache_saved_usd", "semantic_cache_saved_usd"} <= names
