import asyncio
import datetime as dt

from app.apps.bootstrap import seed_demo_apps
from app.apps.db import Database
from app.metrics import external as ext
from app.metrics.external import AAModel


def _db() -> Database:
    return Database(url="sqlite+aiosqlite://", seed=seed_demo_apps)


def _m(slug, **kw) -> AAModel:
    base = dict(slug=slug, name=slug.upper(), creator=None, intelligence_index=None,
                coding_index=None, math_index=None, price_in=None, price_out=None,
                price_blended=None, output_tps=None, ttft_s=None, ttfat_s=None)
    base.update(kw)
    return AAModel(**base)


T0 = dt.datetime(2026, 11, 1, tzinfo=dt.timezone.utc)


def test_store_and_read_latest():
    db = _db()

    async def go():
        async with db.session() as s:
            assert await ext.latest_snapshot(s) is None
            sid = await ext.store_snapshot(
                s, [_m("a", intelligence_index=50.0), _m("b")], origin="import",
                retrieved_at=T0, index_version="v4", api_tier=None)
            snap = await ext.latest_snapshot(s)
            assert snap.id == sid and snap.row_count == 2 and snap.index_version == "v4"
            models = await ext.snapshot_models(s, sid)
            assert set(models) == {"a", "b"}
            assert models["a"].intelligence_index == 50.0
            assert models["b"].price_in is None

    asyncio.run(go())


def test_list_snapshots_newest_first():
    db = _db()

    async def go():
        async with db.session() as s:
            old = await ext.store_snapshot(s, [_m("a")], origin="import", retrieved_at=T0,
                                           index_version=None, api_tier=None)
            new = await ext.store_snapshot(s, [_m("a")], origin="fetch",
                                           retrieved_at=T0 + dt.timedelta(days=1),
                                           index_version=None, api_tier=None)
            assert [x.id for x in await ext.list_snapshots(s)] == [new, old]
            assert len(await ext.list_snapshots(s, limit=1)) == 1

    asyncio.run(go())


def test_set_and_clear_map():
    db = _db()

    async def go():
        async with db.session() as s:
            assert await ext.get_map(s) == {}
            await ext.set_map(s, "m1", "slug-a", by="admin")
            await ext.set_map(s, "m1", "slug-b", by="admin")
            assert await ext.get_map(s) == {"m1": "slug-b"}
            await ext.set_map(s, "m1", None, by="admin")
            assert await ext.get_map(s) == {}

    asyncio.run(go())
