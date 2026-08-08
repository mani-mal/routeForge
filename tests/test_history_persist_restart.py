import asyncio

from app.apps.db import Database
from app.apps import history_store as hs
from tests.test_history_store_write import _resp


def test_rows_survive_new_store_handle_same_db(tmp_path):
    async def _inner():
        url = f"sqlite+aiosqlite:///{tmp_path/'h.db'}"
        db1 = Database(url=url)
        async with db1.session() as s:
            await hs.record_history(s, _resp("keep", session_id="wf-9"), tenant_id="acme",
                                    tier="cheap", provider="onprem", retention_rows=5000)
            await s.commit()
        await db1.dispose()
        # Fresh handle on the SAME file -> data persisted (restart durability).
        db2 = Database(url=url)
        async with db2.session() as s:
            listed = await hs.list_history(s)
            assert [x.id for x in listed] == ["keep"]
            assert listed[0].session_id == "wf-9"     # B17 grouping works from DB
            assert (await hs.get_history(s, "keep"))["id"] == "keep"
        await db2.dispose()

    asyncio.run(_inner())
