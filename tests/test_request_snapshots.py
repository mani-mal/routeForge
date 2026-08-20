"""Request snapshot store tests.

No pytest-asyncio in this repo, so async work runs through asyncio.run.
Each test builds its own in-memory Database for full isolation.
"""
import asyncio

from app.apps.db import Database
from app.apps import store


def _summary(rid: str, app: str = "acme", **over) -> dict:
    d = {"id": rid, "tenant_id": app, "sensitivity": "public", "onprem_only": False,
         "model": "m", "model_display": "m", "tier": "cheap", "provider": "onprem",
         "total_usd": 0.01, "baseline_usd": 0.1, "routing_saved_usd": 0.09,
         "compression_saved_usd": 0.0, "latency_ms": 12.0, "hops": 1,
         "detected_entities": [], "created_seq": 0, "total_tokens": 5,
         "quality_sum": 0.0, "quality": None, "quality_source": None, "session_id": None}
    d.update(over)
    return d


def _run(coro):
    return asyncio.run(coro)


def test_record_get_and_list_newest_first():
    db = Database("sqlite+aiosqlite://")  # in-memory

    async def go():
        async with db.session() as s:
            await store.record_snapshot(s, request_id="r1", application_id="acme",
                                        summary_json=_summary("r1"), full_json={"id": "r1", "trace": []})
            await store.record_snapshot(s, request_id="r2", application_id="beta",
                                        summary_json=_summary("r2", app="beta"), full_json={"id": "r2", "trace": []})
            await s.commit()
        async with db.session() as s:
            rows = await store.list_snapshot_summaries(s, limit=100)
            assert [r["id"] for r in rows] == ["r2", "r1"]              # newest (ts desc) first
            assert all(isinstance(r["ts"], str) and r["ts"] for r in rows)
            acme = await store.list_snapshot_summaries(s, application_id="acme", limit=100)
            assert [r["id"] for r in acme] == ["r1"]
            full = await store.get_snapshot_full(s, "r2")
            assert full == {"id": "r2", "trace": []}
            assert await store.get_snapshot_full(s, "missing") is None

    _run(go())


def test_prune_keeps_newest_capacity():
    db = Database("sqlite+aiosqlite://")

    async def go():
        async with db.session() as s:
            for i in range(7):
                await store.record_snapshot(s, request_id=f"r{i}", application_id="acme",
                                            summary_json=_summary(f"r{i}"), full_json={"id": f"r{i}"})
            await s.commit()
        async with db.session() as s:
            await store.prune_snapshots(s, capacity=3)
            await s.commit()
        async with db.session() as s:
            rows = await store.list_snapshot_summaries(s, limit=100)
            assert len(rows) == 3
            assert [r["id"] for r in rows] == ["r6", "r5", "r4"]

    _run(go())
