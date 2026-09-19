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


def test_onboarded_row_carries_data_handling_declarations():
    async def go():
        db = await _mk()
        async with db.session() as s:
            await store.upsert_onboarded(
                s, "declared-model", display_name="Declared", provider="onprem",
                tier="mid", price_input=0.2, price_output=0.2,
                endpoint="http://x/v1", enabled=True,
                region="EU", prompt_retention="none", accepts_raw_sensitive=True)
            await s.commit()
        async with db.session() as s:
            row = await store.get_onboarded(s, "declared-model")
            # store persists the raw declared value verbatim -- normalization
            # (e.g. lowercasing region) happens at resolution in
            # ModelRegistry.effective_pool(), not at write time.
            assert row.region == "EU"
            assert row.prompt_retention == "none"
            assert row.accepts_raw_sensitive is True
    _run(go())


def test_onboarded_row_data_handling_defaults_to_null():
    async def go():
        db = await _mk()
        async with db.session() as s:
            await store.upsert_onboarded(
                s, "undeclared-model", display_name="Undeclared", provider="onprem",
                tier="mid", price_input=0.2, price_output=0.2,
                endpoint="http://x/v1", enabled=True)
            await s.commit()
        async with db.session() as s:
            row = await store.get_onboarded(s, "undeclared-model")
            assert row.region is None
            assert row.prompt_retention is None
            assert row.accepts_raw_sensitive is None
    _run(go())
