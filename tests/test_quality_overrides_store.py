"""P2b-3 Task 1: keyed quality overrides + calibration-set CRUD in the store.

No pytest-asyncio in this repo (see tests/test_apps_store.py) -- async work
runs through a small asyncio.run helper inside ordinary sync `def test_...`.
"""
from __future__ import annotations

import asyncio

import pytest

from app.apps.db import Database
from app.apps import store


def _run(coro):
    return asyncio.run(coro)


async def _db() -> Database:
    db = Database(url="sqlite+aiosqlite://")
    await db.ensure_ready()
    return db


def test_no_rows_gives_empty_dict():
    async def go():
        db = await _db()
        async with db.session() as s:
            overrides = await store.load_quality_overrides(s)
        assert overrides == {}

    _run(go())


def test_customer_and_calibrated_for_same_key_are_both_kept():
    """Precedence runs ACROSS keys at resolve time, so the store keeps both
    candidates per key (final-review ruling 1)."""
    async def go():
        db = await _db()
        async with db.session() as s:
            await store.write_calibration_set(
                s, set_id="run-1",
                units=[{"model": "local-8b", "workload": None, "score": 0.62}])
            await store.upsert_model_quality(
                s, model_name="local-8b", score=0.80, source="customer")
            await s.commit()

        async with db.session() as s:
            overrides = await store.load_quality_overrides(s)
        assert overrides[("local-8b", None)] == {
            "customer": (0.80, None), "calibrated": (0.62, "run-1")}

    _run(go())


def test_per_workload_rows_are_keyed_separately():
    async def go():
        db = await _db()
        async with db.session() as s:
            await store.write_calibration_set(
                s, set_id="run-1",
                units=[{"model": "local-8b", "workload": None, "score": 0.70},
                       {"model": "local-8b", "workload": "claims", "score": 0.55}])
            await s.commit()

        async with db.session() as s:
            overrides = await store.load_quality_overrides(s)
        assert overrides[("local-8b", None)] == {"calibrated": (0.70, "run-1")}
        assert overrides[("local-8b", "claims")] == {"calibrated": (0.55, "run-1")}

    _run(go())


def test_newer_calibrated_at_wins_among_calibrated_rows():
    async def go():
        db = await _db()
        async with db.session() as s:
            await store.write_calibration_set(
                s, set_id="set-old",
                units=[{"model": "local-8b", "workload": None, "score": 0.60}])
            await s.commit()
        async with db.session() as s:
            await store.write_calibration_set(
                s, set_id="set-new",
                units=[{"model": "local-8b", "workload": None, "score": 0.75}])
            await s.commit()

        async with db.session() as s:
            overrides = await store.load_quality_overrides(s)
        assert overrides[("local-8b", None)] == {"calibrated": (0.75, "set-new")}

    _run(go())


def test_write_calibration_set_returns_count():
    async def go():
        db = await _db()
        async with db.session() as s:
            count = await store.write_calibration_set(
                s, set_id="set-1",
                units=[
                    {"model": "local-8b", "workload": None, "score": 0.6},
                    {"model": "local-8b", "workload": "claims", "score": 0.5},
                    {"model": "local-14b", "workload": None, "score": 0.8},
                ])
            await s.commit()
        assert count == 3

    _run(go())


def test_write_calibration_set_twice_raises_value_error():
    async def go():
        db = await _db()
        async with db.session() as s:
            await store.write_calibration_set(
                s, set_id="set-dup",
                units=[{"model": "local-8b", "workload": None, "score": 0.6}])
            await s.commit()

        with pytest.raises(ValueError):
            async with db.session() as s:
                await store.write_calibration_set(
                    s, set_id="set-dup",
                    units=[{"model": "local-8b", "workload": None, "score": 0.7}])
                await s.commit()

    _run(go())


def test_list_calibration_sets_groups_by_eval_run_id():
    async def go():
        db = await _db()
        async with db.session() as s:
            await store.write_calibration_set(
                s, set_id="set-a",
                units=[
                    {"model": "local-8b", "workload": None, "score": 0.6},
                    {"model": "local-8b", "workload": "claims", "score": 0.5},
                ])
            await s.commit()
        async with db.session() as s:
            await store.write_calibration_set(
                s, set_id="set-b",
                units=[{"model": "local-14b", "workload": None, "score": 0.8}])
            await s.commit()

        async with db.session() as s:
            sets = await store.list_calibration_sets(s)
        by_id = {row["set_id"]: row for row in sets}
        assert by_id["set-a"]["units"] == 2
        assert by_id["set-b"]["units"] == 1
        assert "applied_at" in by_id["set-a"]
        # newest first
        assert [row["set_id"] for row in sets] == ["set-b", "set-a"]

    _run(go())


def test_delete_calibration_set_removes_only_that_set():
    async def go():
        db = await _db()
        async with db.session() as s:
            await store.upsert_model_quality(
                s, model_name="local-8b", score=0.9, source="customer")
            await store.write_calibration_set(
                s, set_id="set-keep",
                units=[{"model": "local-14b", "workload": None, "score": 0.7}])
            await store.write_calibration_set(
                s, set_id="set-gone",
                units=[
                    {"model": "local-8b", "workload": None, "score": 0.6},
                    {"model": "local-8b", "workload": "claims", "score": 0.5},
                ])
            await s.commit()

        async with db.session() as s:
            deleted = await store.delete_calibration_set(s, "set-gone")
            await s.commit()
        assert deleted == 2

        async with db.session() as s:
            overrides = await store.load_quality_overrides(s)
            sets = await store.list_calibration_sets(s)
        # customer row untouched
        assert overrides[("local-8b", None)] == {"customer": (0.9, None)}
        # the surviving calibrated row for local-8b/claims is gone
        assert ("local-8b", "claims") not in overrides
        # the other set is untouched
        assert overrides[("local-14b", None)] == {"calibrated": (0.7, "set-keep")}
        assert [row["set_id"] for row in sets] == ["set-keep"]

    _run(go())


def test_delete_calibration_set_unknown_id_returns_zero():
    async def go():
        db = await _db()
        async with db.session() as s:
            deleted = await store.delete_calibration_set(s, "nope")
        assert deleted == 0

    _run(go())


def test_load_quality_bounds_newest_set_wins_per_key():
    import datetime as dt
    from app.apps.models import ModelQualityBound

    async def go():
        db = await _db()
        async with db.session() as s:
            await store.write_calibration_set(
                s, set_id="old", units=[],
                bounds=[{"model": "local-8b", "workload": None, "lower": 0.5, "ci_level": 0.9},
                        {"model": "local-8b", "workload": "claims", "lower": 0.4, "ci_level": 0.9}])
            await s.commit()
        async with db.session() as s:
            await store.write_calibration_set(
                s, set_id="new", units=[],
                bounds=[{"model": "local-8b", "workload": None, "lower": 0.7, "ci_level": 0.9}])
            await s.commit()
        async with db.session() as s:
            from sqlalchemy import update
            await s.execute(update(ModelQualityBound).where(
                ModelQualityBound.set_id == "old").values(
                calibrated_at=dt.datetime(2020, 1, 1, tzinfo=dt.timezone.utc)))
            await s.commit()
        async with db.session() as s:
            got = await store.load_quality_bounds(s)
        assert got == {("local-8b", None): (0.7, "new"),
                       ("local-8b", "claims"): (0.4, "old")}

    _run(go())


def test_write_calibration_set_409_covers_bounds_table():
    async def go():
        db = await _db()
        bounds = [{"model": "local-8b", "workload": None, "lower": 0.5, "ci_level": 0.9}]
        async with db.session() as s:
            await store.write_calibration_set(s, set_id="b-only", units=[], bounds=bounds)
            await s.commit()
        with pytest.raises(ValueError):
            async with db.session() as s:
                await store.write_calibration_set(s, set_id="b-only", units=[], bounds=bounds)

    _run(go())
