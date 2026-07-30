"""Task 11: demo app rename + seeded per-app privacy profiles.

No pytest-asyncio in this repo (see tests/test_privacy_models.py) — async work
runs through asyncio.run inside ordinary sync `def test_...`, matching
tests/test_privacy_profile.py's `_fresh_db()` / `_run()` pattern.
"""
import asyncio

from app.apps.db import Database
from app.apps import store
from app.apps.bootstrap import seed_all


def _fresh_db() -> Database:
    return Database(seed=None)


def _run(coro):
    return asyncio.run(coro)


def test_seed_demotes_location_and_adds_mrn():
    db = _fresh_db()

    async def go():
        async with db.session() as s:
            await seed_all(s)
            await s.commit()
        async with db.session() as s:
            acme = await store.get_application(s, "acme")
            assert acme.name == "RAG Agent"
            prof_acme = await store.get_privacy_profile(s, "acme")
            loc = [r for r in prof_acme.rules if "LOCATION" in r.entities][0]
            assert loc.sensitivity == "internal" and loc.risk_class == "low"
            prof_h = await store.get_privacy_profile(s, "hospital")
            assert any(r.name == "MRN" for r in prof_h.recognizers)

    _run(go())


def test_seed_is_idempotent():
    db = _fresh_db()

    async def go():
        async with db.session() as s:
            await seed_all(s)
            await seed_all(s)
            await s.commit()
        async with db.session() as s:
            prof = await store.get_privacy_profile(s, "hospital")
            assert sum(1 for r in prof.recognizers if r.name == "MRN") == 1

    _run(go())
