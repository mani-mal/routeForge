"""Task 5: AppPrivacyProfile value object, resolver, fallback, cache.

No pytest-asyncio in this repo (see tests/test_privacy_models.py) — async work
runs through asyncio.run inside ordinary sync `def test_...`, matching
tests/test_apps_store.py's `_fresh_db()` / `_run()` pattern.
"""
import asyncio

from app.apps.db import Database
from app.apps.models import Application
from app.apps import store
from app.apps.privacy import (
    AppPrivacyProfile, SensitivityRuleView, ProfileCache, effective_rules)
from app.config.loader import load_settings


def _fresh_db() -> Database:
    return Database(url="sqlite+aiosqlite://", seed=None)


def _run(coro):
    return asyncio.run(coro)


def test_replace_and_get_roundtrip():
    db = _fresh_db()

    async def go():
        async with db.session() as s:
            s.add(Application(id="t1", name="T1", default_residency="any"))
            await s.commit()
        async with db.session() as s:
            await store.replace_privacy_profile(
                s, "t1",
                recognizers=[{"name": "MRN", "regex": r"\bMRN-\d{7}\b", "score": 0.9}],
                rules=[{"entities": ["MRN"], "sensitivity": "restricted",
                        "risk_class": "high", "residency": "onprem_only",
                        "require_verification": True}])
            await s.commit()
        async with db.session() as s:
            prof = await store.get_privacy_profile(s, "t1")
        assert [r.name for r in prof.recognizers] == ["MRN"]
        assert prof.rules[0].sensitivity == "restricted"
        assert prof.rules[0].entities == frozenset({"MRN"})

    _run(go())


def test_empty_profile_falls_back_to_global_rules():
    prof = AppPrivacyProfile(recognizers=(), rules=())
    settings = load_settings()
    rules = effective_rules(prof, settings)
    # global rules present, and a terminal public catch-all guaranteed
    assert any(r.sensitivity == "restricted" for r in rules)
    assert rules[-1].entities == frozenset() and rules[-1].sensitivity == "public"


def test_cache_put_get_clear():
    c = ProfileCache()
    p = AppPrivacyProfile(recognizers=(), rules=())
    assert c.get("a") is None
    c.put("a", p)
    assert c.get("a") is p
    c.clear("a")
    assert c.get("a") is None
