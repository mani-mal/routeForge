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


def test_p1a_axes_round_trip_through_policy_view_and_reset_to_null():
    """The four P1a columns write, read back, and reset to NULL through the
    same upsert_policy path pii_detection/stream_mode already exercise."""
    db = _fresh_db()

    async def go():
        async with db.session() as s:
            await store.upsert_policy(
                s, "acme", model_input="raw", client_output="redact",
                content_integrity="exact",
                escalate_residency_on_detection=False)
            await s.commit()

        async with db.session() as s:
            app = await store.get_application_with_policy(s, "acme")
            view = policy_view_from(app)
            assert view.model_input == "raw"
            assert view.client_output == "redact"
            assert view.content_integrity == "exact"
            assert view.escalate_residency_on_detection is False

        # reset path: explicit None writes NULL back (inherit).
        async with db.session() as s:
            await store.upsert_policy(
                s, "acme", model_input=None, client_output=None,
                content_integrity=None,
                escalate_residency_on_detection=None)
            await s.commit()

        async with db.session() as s:
            app = await store.get_application_with_policy(s, "acme")
            view = policy_view_from(app)
            assert view.model_input is None
            assert view.client_output is None
            assert view.content_integrity is None
            assert view.escalate_residency_on_detection is None

    _run(go())


def test_usage_summary_quality_aggregate_matches_history_formula():
    """usage_summary must emit token_intelligence_density/cost_intelligence_density
    computed exactly like app/api/history.py:145-146 (quality_sum/tok_sum*1000 and
    quality_sum/usd_sum), plus a quality_source_distribution counting provenance —
    additive to the existing fields (Task 2, quality axis)."""
    db = _fresh_db()

    async def go():
        async with db.session() as s:
            await store.record_usage(
                s, application_id="acme", request_id="q-1", model="local-8b",
                provider="onprem", tier="cheap", prompt_tokens=700,
                completion_tokens=300, total_tokens=1000, cost_usd=0.01,
                baseline_usd=0.05, latency_ms=10.0, sensitivity="public",
                onprem_only=False, quality=0.5, quality_source="tier_default")
            await store.record_usage(
                s, application_id="acme", request_id="q-2", model="local-70b",
                provider="onprem", tier="frontier", prompt_tokens=1500,
                completion_tokens=500, total_tokens=2000, cost_usd=0.02,
                baseline_usd=0.02, latency_ms=20.0, sensitivity="public",
                onprem_only=False, quality=1.0, quality_source="calibrated")
            await s.commit()

            summary = await store.usage_summary(s, application_id="acme")

            # Same numerator/denominators as the two records above.
            quality_sum = 0.5 + 1.0
            total_tokens = 1000 + 2000
            total_usd = 0.01 + 0.02
            expected_tid = quality_sum / total_tokens * 1000.0
            expected_cid = quality_sum / total_usd
            assert summary["token_intelligence_density"] == expected_tid
            assert summary["cost_intelligence_density"] == expected_cid
            assert summary["quality_source_distribution"] == {
                "tier_default": 1, "calibrated": 1}

            # Additive: pre-existing fields are untouched by the new ones.
            assert summary["count"] == 2
            assert summary["total_tokens"] == total_tokens
            assert abs(summary["total_usd"] - total_usd) < 1e-9

    _run(go())


def test_usage_summary_quality_zero_guards():
    """Zero total_tokens and zero total_usd must both floor the density
    fields to 0.0 rather than raising/NaN-ing (mirrors history.py's guards)."""
    db = _fresh_db()

    async def go():
        async with db.session() as s:
            await store.record_usage(
                s, application_id="acme", request_id="q-zero", model="local-8b",
                provider="onprem", tier="cheap", prompt_tokens=0,
                completion_tokens=0, total_tokens=0, cost_usd=0.0,
                baseline_usd=0.0, latency_ms=1.0, sensitivity="public",
                onprem_only=False, quality=0.5, quality_source="tier_default")
            await s.commit()

            summary = await store.usage_summary(s, application_id="acme")
            assert summary["token_intelligence_density"] == 0.0
            assert summary["cost_intelligence_density"] == 0.0
            assert summary["quality_source_distribution"] == {"tier_default": 1}

    _run(go())


def test_usage_by_application_quality_aggregate_matches_per_app():
    """usage_by_application's per-app rollup must carry the same quality
    aggregate (TID/CID/quality_source_distribution) as usage_summary scoped
    to that one application — the two views must reconcile."""
    db = _fresh_db()

    async def go():
        async with db.session() as s:
            await store.record_usage(
                s, application_id="acme", request_id="qa-1", model="local-8b",
                provider="onprem", tier="cheap", prompt_tokens=700,
                completion_tokens=300, total_tokens=1000, cost_usd=0.01,
                baseline_usd=0.05, latency_ms=10.0, sensitivity="public",
                onprem_only=False, quality=0.5, quality_source="tier_default")
            await store.record_usage(
                s, application_id="hospital", request_id="qa-2", model="local-70b",
                provider="onprem", tier="frontier", prompt_tokens=1500,
                completion_tokens=500, total_tokens=2000, cost_usd=0.02,
                baseline_usd=0.02, latency_ms=20.0, sensitivity="restricted",
                onprem_only=True, quality=1.0, quality_source="calibrated")
            await s.commit()

            rollup = {r["application_id"]: r
                     for r in await store.usage_by_application(s)}
            acme_summary = await store.usage_summary(s, application_id="acme")
            hospital_summary = await store.usage_summary(s, application_id="hospital")

            assert rollup["acme"]["token_intelligence_density"] == \
                acme_summary["token_intelligence_density"]
            assert rollup["acme"]["cost_intelligence_density"] == \
                acme_summary["cost_intelligence_density"]
            assert rollup["acme"]["quality_source_distribution"] == \
                {"tier_default": 1} == acme_summary["quality_source_distribution"]

            assert rollup["hospital"]["token_intelligence_density"] == \
                hospital_summary["token_intelligence_density"]
            assert rollup["hospital"]["quality_source_distribution"] == \
                {"calibrated": 1}

            # No internal-only accumulator key (e.g. a raw "_quality_sum")
            # leaks into the returned aggregate — counts/sums/ratios only.
            for row in rollup.values():
                assert all(not k.startswith("_") for k in row)

    _run(go())


def test_usage_by_application_quality_zero_guards():
    db = _fresh_db()

    async def go():
        async with db.session() as s:
            await store.record_usage(
                s, application_id="acme", request_id="qz-1", model="local-8b",
                provider="onprem", tier="cheap", prompt_tokens=0,
                completion_tokens=0, total_tokens=0, cost_usd=0.0,
                baseline_usd=0.0, latency_ms=1.0, sensitivity="public",
                onprem_only=False, quality=0.5, quality_source="tier_default")
            await s.commit()

            rollup = {r["application_id"]: r
                     for r in await store.usage_by_application(s)}
            assert rollup["acme"]["token_intelligence_density"] == 0.0
            assert rollup["acme"]["cost_intelligence_density"] == 0.0

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
