"""P2b-2 Task 1: routing_outcomes + outcome_feedback tables and store.

No pytest-asyncio in this repo (see tests/test_apps_store.py) -- async work
runs through a small asyncio.run helper inside ordinary sync `def test_...`.

Both tables are CONTENT-FREE BY CONTRACT (spec §4.3): ids, enums, numbers,
entity TYPES and version hashes only -- never prompt/completion text, reason
strings, upstream error text, comment or corrected_output. Test 7 below
enforces that at the schema level so a future column addition can't
reintroduce a content field silently.
"""
from __future__ import annotations

import asyncio
import datetime as dt

from sqlalchemy import select

from app.apps.db import Database
from app.apps import store
from app.apps.models import OutcomeFeedback, RoutingOutcome


def _run(coro):
    return asyncio.run(coro)


async def _db() -> Database:
    db = Database(url="sqlite+aiosqlite://")
    await db.ensure_ready()
    return db


def _outcome_row(**overrides) -> dict:
    row = dict(
        request_id="req-1",
        application_id="app-1",
        trace_id="trace-1",
        status="ok",
        app_policy_version="apv-1",
        endpoint_registry_version="erv-1",
        routing_policy_version="rpv-1",
        workload="coding",
        workload_source="header",
        tier_target="cheap",
        complexity=0.42,
        prompt_tokens_bucket="1k-4k",
        sensitivity="internal",
        risk_class="low",
        onprem_only=True,
        detected_entity_types=["US_SSN", "EMAIL_ADDRESS"],
        eligible=["onprem/qwen-7b", "onprem/qwen-14b"],
        rejected=[{"model": "azure/gpt-4", "reason_code": "residency"}],
        attempts=[{"model": "onprem/qwen-7b", "outcome": "ok"}],
        chosen="onprem/qwen-7b",
        scoring={"onprem/qwen-7b": {"cost": 0.1}},
        circuit_bypass=False,
        cost_usd=0.0012,
        latency_ms=234.5,
        prompt_tokens=120,
        completion_tokens=45,
        verify_required=True,
        verify_performed=True,
        verify_passed=True,
        verify_mode="verified_then_stream",
    )
    row.update(overrides)
    return row


def test_write_then_get_round_trips_every_column():
    async def go():
        db = await _db()
        async with db.session() as s:
            await store.write_outcome(s, _outcome_row())
            await s.commit()

        async with db.session() as s:
            got = await store.get_outcome(s, "req-1", application_id="app-1")

        assert got is not None
        expected = _outcome_row()
        for key, value in expected.items():
            assert getattr(got, key) == value, key

    _run(go())


def test_round_trips_nullable_columns_as_none():
    async def go():
        db = await _db()
        nullable_none = dict(
            trace_id=None, app_policy_version=None, endpoint_registry_version=None,
            routing_policy_version=None, workload=None, tier_target=None,
            complexity=None, prompt_tokens_bucket=None, sensitivity=None,
            risk_class=None, onprem_only=None, chosen=None, scoring=None,
            verify_required=None, verify_performed=None, verify_passed=None,
            verify_mode=None,
        )
        async with db.session() as s:
            await store.write_outcome(s, _outcome_row(**nullable_none))
            await s.commit()

        async with db.session() as s:
            got = await store.get_outcome(s, "req-1", application_id="app-1")

        assert got is not None
        for key in nullable_none:
            assert getattr(got, key) is None, key
        # JSON list columns keep their default([]) shape when not overridden
        assert got.detected_entity_types == ["US_SSN", "EMAIL_ADDRESS"]

    _run(go())


def test_get_outcome_is_app_scoped():
    async def go():
        db = await _db()
        async with db.session() as s:
            await store.write_outcome(s, _outcome_row())
            await s.commit()

        async with db.session() as s:
            got = await store.get_outcome(s, "req-1", application_id="other-app")

        assert got is None

    _run(go())


def test_find_outcome_by_trace_id_and_request_id_is_app_scoped():
    async def go():
        db = await _db()
        async with db.session() as s:
            await store.write_outcome(s, _outcome_row())
            await s.commit()

        async with db.session() as s:
            by_trace = await store.find_outcome(
                s, application_id="app-1", request_id=None, trace_id="trace-1")
            by_req = await store.find_outcome(
                s, application_id="app-1", request_id="req-1", trace_id=None)
            wrong_app = await store.find_outcome(
                s, application_id="other-app", request_id="req-1", trace_id=None)

        assert by_trace is not None and by_trace.request_id == "req-1"
        assert by_req is not None and by_req.request_id == "req-1"
        assert wrong_app is None

    _run(go())


def test_write_feedback_rows_are_returned_by_feedback_for():
    async def go():
        db = await _db()
        async with db.session() as s:
            await store.write_outcome(s, _outcome_row())
            await store.write_feedback(
                s, request_id="req-1", application_id="app-1",
                score_name="thumbs", value_num=1.0, value_cat=None, source="app")
            await store.write_feedback(
                s, request_id="req-1", application_id="app-1",
                score_name="rating", value_num=None, value_cat="good", source="admin")
            await s.commit()

        async with db.session() as s:
            fb = await store.feedback_for(s, ["req-1", "req-missing"])

        assert set(fb.keys()) == {"req-1"}
        assert len(fb["req-1"]) == 2
        names = {row.score_name for row in fb["req-1"]}
        assert names == {"thumbs", "rating"}

    _run(go())


def test_prune_outcomes_deletes_old_rows_from_both_tables():
    async def go():
        db = await _db()
        old_ts = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=120)
        new_ts = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=1)

        async with db.session() as s:
            await store.write_outcome(s, _outcome_row(request_id="old-1"))
            await store.write_outcome(s, _outcome_row(request_id="new-1"))
            await s.commit()

        # Backdate ts directly (write_outcome always stamps "now").
        async with db.session() as s:
            old_row = await s.get(RoutingOutcome, "old-1")
            old_row.ts = old_ts
            new_row = await s.get(RoutingOutcome, "new-1")
            new_row.ts = new_ts
            await store.write_feedback(
                s, request_id="old-1", application_id="app-1",
                score_name="thumbs", value_num=1.0, value_cat=None, source="app")
            await store.write_feedback(
                s, request_id="new-1", application_id="app-1",
                score_name="thumbs", value_num=1.0, value_cat=None, source="app")
            await s.commit()

        async with db.session() as s:
            fb_rows = (await s.execute(select(OutcomeFeedback))).scalars().all()
            for row in fb_rows:
                if row.request_id == "old-1":
                    row.ts = old_ts
            await s.commit()

        async with db.session() as s:
            deleted = await store.prune_outcomes(s, older_than_days=90)
            await s.commit()

        assert deleted == 1

        async with db.session() as s:
            assert await store.get_outcome(s, "old-1", application_id="app-1") is None
            assert await store.get_outcome(s, "new-1", application_id="app-1") is not None
            fb = await store.feedback_for(s, ["old-1", "new-1"])

        assert "old-1" not in fb
        assert "new-1" in fb

    _run(go())


def test_iter_outcomes_pages_by_ts_and_request_id_with_no_gaps_or_dupes():
    async def go():
        db = await _db()
        base = dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)
        async with db.session() as s:
            for i in range(5):
                await store.write_outcome(s, _outcome_row(request_id=f"req-{i}"))
                row = await s.get(RoutingOutcome, f"req-{i}")
                row.ts = base + dt.timedelta(seconds=i)
            await s.commit()

        since = base - dt.timedelta(seconds=1)
        until = base + dt.timedelta(seconds=100)

        async with db.session() as s:
            page1 = await store.iter_outcomes(s, since=since, until=until, page=2)
        assert [r.request_id for r in page1] == ["req-0", "req-1"]

        after = (page1[-1].ts, page1[-1].request_id)
        async with db.session() as s:
            page2 = await store.iter_outcomes(
                s, since=since, until=until, page=2, after=after)
        assert [r.request_id for r in page2] == ["req-2", "req-3"]

        after2 = (page2[-1].ts, page2[-1].request_id)
        async with db.session() as s:
            page3 = await store.iter_outcomes(
                s, since=since, until=until, page=2, after=after2)
        assert [r.request_id for r in page3] == ["req-4"]

        all_ids = [r.request_id for r in page1 + page2 + page3]
        assert all_ids == [f"req-{i}" for i in range(5)]
        assert len(set(all_ids)) == 5

    _run(go())


def test_schema_is_content_free():
    forbidden = {
        "prompt_text", "completion", "content", "message", "comment",
        "corrected", "reason", "error",
    }
    # Token COUNTS, not text -- the only exemptions. `prompt_tokens`/
    # `prompt_tokens_bucket` are the brief's explicit exceptions;
    # `completion_tokens` is the same class of column (a count) and the
    # brief's own RoutingOutcome model defines it, so it must be exempt too.
    allowed = {"prompt_tokens", "prompt_tokens_bucket", "completion_tokens"}
    for model in (RoutingOutcome, OutcomeFeedback):
        for col in model.__table__.columns:
            name = col.name
            if name in allowed:
                continue
            for bad in forbidden:
                assert bad not in name, f"{model.__name__}.{name} contains {bad!r}"
    assert "prompt_tokens" in RoutingOutcome.__table__.columns
    assert "prompt_tokens_bucket" in RoutingOutcome.__table__.columns
    assert "completion_tokens" in RoutingOutcome.__table__.columns
