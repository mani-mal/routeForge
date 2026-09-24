"""Task 2: model_quality DB table + store accessors.

No pytest-asyncio in this repo (see tests/test_apps_store.py) — async work
runs through a small asyncio.run helper inside ordinary sync `def test_...`.
"""
import asyncio

from app.apps.db import Database
from app.apps import store


def _run(coro):
    return asyncio.run(coro)


def test_upsert_and_load_quality_overrides():
    db = Database(url="sqlite+aiosqlite:///:memory:")

    async def go():
        async with db.session() as s:
            await store.write_calibration_set(
                s, set_id="run-1",
                units=[{"model": "local-8b", "workload": None, "score": 0.62}])
            await store.upsert_model_quality(
                s, model_name="local-8b", score=0.80, source="customer",
                notes="seed")
            await s.commit()

        async with db.session() as s:
            overrides = await store.load_quality_overrides(s)
        # both candidates are kept per key (precedence is the resolver's job);
        # customer rows always carry a None set_id (P2b-3).
        assert overrides[("local-8b", None)] == {
            "customer": (0.80, None), "calibrated": (0.62, "run-1")}

    _run(go())


def test_load_quality_overrides_empty_when_no_rows():
    db = Database(url="sqlite+aiosqlite:///:memory:")

    async def go():
        async with db.session() as s:
            overrides = await store.load_quality_overrides(s)
        assert overrides == {}

    _run(go())
