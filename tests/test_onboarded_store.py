import asyncio

from app.apps.db import Database
from app.apps import store


def _run(coro):
    return asyncio.run(coro)


async def _mk():
    db = Database(url="sqlite+aiosqlite://")
    await db.ensure_ready()
    return db


def test_upsert_get_list_delete_onboarded():
    async def go():
        db = await _mk()
        async with db.session() as s:
            await store.upsert_onboarded(
                s, "nemotron-mid", display_name="Nemotron", provider="onprem",
                tier="mid", price_input=0.2, price_output=0.2,
                endpoint="http://x/v1", api_key_env="NEM_KEY", enabled=True,
                allowed_app_ids=["acme"])
            await s.commit()
        async with db.session() as s:
            row = await store.get_onboarded(s, "nemotron-mid")
            assert row.tier == "mid" and row.enabled is True
            assert row.allowed_app_ids == ["acme"]
            rows = await store.list_onboarded(s)
            assert [r.slug for r in rows] == ["nemotron-mid"]
        async with db.session() as s:
            assert await store.delete_onboarded(s, "nemotron-mid") is True
            await s.commit()
        async with db.session() as s:
            assert await store.get_onboarded(s, "nemotron-mid") is None
    _run(go())
