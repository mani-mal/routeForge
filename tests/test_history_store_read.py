import asyncio

import pytest

from app.apps.db import Database
from app.apps import history_store as hs
from tests.test_history_store_write import _resp   # reuse the builder


def test_list_newest_first_and_app_filter_and_limit():
    async def _inner():
        db = Database(url="sqlite+aiosqlite://")
        async with db.session() as s:
            await hs.record_history(s, _resp("a1"), tenant_id="acme", tier="cheap", provider="onprem", retention_rows=5000)
            await hs.record_history(s, _resp("h1"), tenant_id="hospital", tier="mid", provider="onprem", retention_rows=5000)
            await hs.record_history(s, _resp("a2"), tenant_id="acme", tier="cheap", provider="onprem", retention_rows=5000)
            await s.commit()
        async with db.session() as s:
            assert [x.id for x in await hs.list_history(s)] == ["a2", "h1", "a1"]  # newest first
            assert [x.id for x in await hs.list_history(s, app="acme")] == ["a2", "a1"]
            assert await hs.list_history(s, limit=0) == []
            assert (await hs.list_history(s))[0].created_seq >= 1
        await db.dispose()

    asyncio.run(_inner())


def test_stats_empty_and_aggregate():
    async def _inner():
        db = Database(url="sqlite+aiosqlite://")
        async with db.session() as s:
            assert (await hs.stats(s))["count"] == 0            # empty-safe
            await hs.record_history(s, _resp("a", total=0.001, base=0.002, route_saved=0.001,
                                             onprem=True), tenant_id="acme", tier="cheap",
                                    provider="onprem", retention_rows=5000)
            await hs.record_history(s, _resp("b", total=0.003, base=0.004, route_saved=0.001,
                                             comp_saved=0.002), tenant_id="acme", tier="frontier",
                                    provider="onprem", retention_rows=5000)
            await s.commit()
        async with db.session() as s:
            st = await hs.stats(s)
            assert st["count"] == 2
            assert st["onprem_count"] == 1
            assert st["tier_distribution"] == {"cheap": 1, "frontier": 1}
            assert st["compression_saved_usd"] == pytest.approx(0.002)
            assert st["latency_p50_ms"] > 0
        await db.dispose()

    asyncio.run(_inner())


def test_fleet_groups_and_flags_onprem_violation():
    async def _inner():
        db = Database(url="sqlite+aiosqlite://")
        async with db.session() as s:
            # onprem_only request served OFF-prem (azure) -> a genuine breach.
            await hs.record_history(s, _resp("v", onprem=True), tenant_id="hospital",
                                    tier="frontier", provider="azure", retention_rows=5000)
            await hs.record_history(s, _resp("ok", onprem=True), tenant_id="hospital",
                                    tier="cheap", provider="onprem", retention_rows=5000)
            await s.commit()
        async with db.session() as s:
            rows = await hs.fleet(s)
            row = next(r for r in rows if r["app"] == "hospital")
            assert row["count"] == 2
            assert row["onprem_violations"] == 1        # azure-served onprem_only
        await db.dispose()

    asyncio.run(_inner())
