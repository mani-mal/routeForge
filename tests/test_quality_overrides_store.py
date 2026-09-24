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


def test_customer_beats_calibrated_for_same_model():
    async def go():
        db = await _db()
        async with db.session() as s:
            await store.upsert_model_quality(
                s, model_name="local-8b", score=0.62, source="calibrated",
                eval_run_id="run-1")
            await store.upsert_model_quality(
                s, model_name="local-8b", score=0.80, source="customer")
            await s.commit()

        async with db.session() as s:
            overrides = await store.load_quality_overrides(s)
        assert overrides[("local-8b", None)] == (0.80, "customer", None)

    _run(go())


def test_per_workload_rows_are_keyed_separately():
    async def go():
        db = await _db()
        async with db.session() as s:
            await store.upsert_model_quality(
                s, model_name="local-8b", task_class=None, score=0.70,
                source="calibrated", eval_run_id="run-1")
            await store.upsert_model_quality(
                s, model_name="local-8b", task_class="claims", score=0.55,
                source="calibrated", eval_run_id="run-1")
            await s.commit()

        async with db.session() as s:
            overrides = await store.load_quality_overrides(s)
        assert overrides[("local-8b", None)] == (0.70, "calibrated", "run-1")
        assert overrides[("local-8b", "claims")] == (0.55, "calibrated", "run-1")

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
        assert overrides[("local-8b", None)] == (0.75, "calibrated", "set-new")

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
        assert overrides[("local-8b", None)] == (0.9, "customer", None)
        # the surviving calibrated row for local-8b/claims is gone
        assert ("local-8b", "claims") not in overrides
        # the other set is untouched
        assert overrides[("local-14b", None)] == (0.7, "calibrated", "set-keep")
        assert [row["set_id"] for row in sets] == ["set-keep"]

    _run(go())


def test_delete_calibration_set_unknown_id_returns_zero():
    async def go():
        db = await _db()
        async with db.session() as s:
            deleted = await store.delete_calibration_set(s, "nope")
        assert deleted == 0

    _run(go())
