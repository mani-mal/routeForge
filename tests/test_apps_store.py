"""Application-layer store tests (Phase 1).

No pytest-asyncio in this repo, so async work runs through a small asyncio.run
helper. Each test builds its own in-memory Database for full isolation.
"""
import asyncio

from sqlalchemy import update

from app.apps.bootstrap import seed_demo_apps
from app.apps.db import Database
from app.apps import store
from app.apps.models import AppToken, Application
from app.apps.policy import policy_view_from
from app.apps.tokens import hash_token, new_token, token_prefix


def _fresh_db() -> Database:
    # Unique in-memory DB per test (StaticPool keeps the one connection alive).
    return Database(url="sqlite+aiosqlite://", seed=seed_demo_apps)


def _run(coro):
    return asyncio.run(coro)


def test_token_hash_is_deterministic_and_prefix_is_safe():
    t = new_token()
    assert t.startswith("rf_live_")
    assert hash_token(t) == hash_token(t)
    assert hash_token(t) != hash_token(new_token())
    # prefix never reveals the whole token
    assert token_prefix(t) != t
    assert t.startswith(token_prefix(t).rstrip("…"))


def test_seed_creates_demo_apps_with_legacy_tokens():
    db = _fresh_db()

    async def go():
        async with db.session() as s:
            acme = await store.get_application(s, "acme")
            assert acme is not None and acme.status == "active"
            assert acme.default_residency == "private_cloud_ok"
            apps = await store.list_applications(s)
            assert {a.id for a in apps} == {"acme", "hospital", "default"}

    _run(go())


def test_resolve_known_token_returns_application():
    db = _fresh_db()

    async def go():
        async with db.session() as s:
            app = await store.resolve_application(s, "hospital-key")
            assert app is not None and app.id == "hospital"

    _run(go())


def test_resolve_is_fail_closed():
    db = _fresh_db()

    async def go():
        async with db.session() as s:
            assert await store.resolve_application(s, "") is None
            assert await store.resolve_application(s, None) is None
            assert await store.resolve_application(s, "totally-unknown") is None

    _run(go())


def test_revoked_token_and_disabled_app_are_rejected():
    db = _fresh_db()

    async def go():
        async with db.session() as s:
            # revoke acme's token (explicit UPDATE — no lazy relationship load)
            await s.execute(update(AppToken)
                            .where(AppToken.application_id == "acme")
                            .values(status="revoked"))
            await s.commit()
            assert await store.resolve_application(s, "acme-key") is None

            # disable the hospital app (its token stays active)
            await s.execute(update(Application)
                            .where(Application.id == "hospital")
                            .values(status="disabled"))
            await s.commit()
            assert await store.resolve_application(s, "hospital-key") is None

    _run(go())


def test_archived_app_is_rejected():
    db = _fresh_db()

    async def go():
        async with db.session() as s:
            # archiving an app must disable its tokens even though they stay active
            await s.execute(update(Application)
                            .where(Application.id == "hospital")
                            .values(status="archived"))
            await s.commit()
            assert await store.resolve_application(s, "hospital-key") is None

    _run(go())


def test_record_and_list_usage():
    db = _fresh_db()

    async def go():
        async with db.session() as s:
            await store.record_usage(
                s, application_id="acme", request_id="req-1", model="local-8b",
                provider="onprem", tier="cheap", prompt_tokens=10,
                completion_tokens=5, total_tokens=15, cost_usd=0.001,
                baseline_usd=0.02, latency_ms=123.0, sensitivity="public",
                onprem_only=False)
            await store.record_usage(
                s, application_id="hospital", request_id="req-2", model="local-8b",
                provider="onprem", tier="cheap", prompt_tokens=1, completion_tokens=1,
                total_tokens=2, cost_usd=0.0, baseline_usd=0.0, latency_ms=1.0,
                sensitivity="restricted", onprem_only=True)
            await s.commit()

            all_rows = await store.list_usage(s)
            assert len(all_rows) == 2
            acme_rows = await store.list_usage(s, application_id="acme")
            assert len(acme_rows) == 1 and acme_rows[0].request_id == "req-1"

    _run(go())


def test_pii_detection_and_stream_mode_round_trip_through_policy_view():
    db = _fresh_db()

    async def go():
        async with db.session() as s:
            await store.upsert_policy(
                s, "acme", pii_detection="regex", stream_mode="stream_unverified")
            await s.commit()

        async with db.session() as s:
            app = await store.get_application_with_policy(s, "acme")
            view = policy_view_from(app)
            assert view.pii_detection == "regex"
            assert view.stream_mode == "stream_unverified"

    _run(go())


def test_pii_detection_and_stream_mode_null_round_trip_as_none():
    db = _fresh_db()

    async def go():
        async with db.session() as s:
            # No policy row at all -> policy_view_from's None-app.policy branch.
            app = await store.get_application_with_policy(s, "acme")
            view = policy_view_from(app)
            assert view.pii_detection is None
            assert view.stream_mode is None

            # A policy row exists but leaves these two fields unset (NULL).
            await store.upsert_policy(s, "acme", max_rpm=10)
            await s.commit()

        async with db.session() as s:
            app = await store.get_application_with_policy(s, "acme")
            view = policy_view_from(app)
            assert view.max_rpm == 10
            assert view.pii_detection is None
            assert view.stream_mode is None

    _run(go())
