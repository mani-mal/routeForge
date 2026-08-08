import asyncio

from sqlalchemy import func, select

from app.apps.db import Database
from app.apps.models import RequestHistory
from app.apps import history_store as hs
from tests.test_history_store_write import _resp


def test_concurrent_writes_do_not_lose_rows_on_memory_sqlite():
    async def _inner():
        db = Database(url="sqlite+aiosqlite://")

        async def write(i):
            async with db.session() as s:
                await hs.record_history(s, _resp(f"r{i}"), tenant_id="acme",
                                        tier="cheap", provider="onprem",
                                        retention_rows=5000)
                await s.commit()

        # Many concurrent writers on the shared single connection.
        await asyncio.gather(*(write(i) for i in range(25)))

        async with db.session() as s:
            n = (await s.execute(
                select(func.count()).select_from(RequestHistory))).scalar_one()
        await db.dispose()
        return n

    assert asyncio.run(_inner()) == 25   # no writes lost


def test_serialize_sessions_false_for_file_url():
    assert Database(url="sqlite+aiosqlite:///./x.db")._serialize_sessions is False
