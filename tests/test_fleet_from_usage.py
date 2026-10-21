"""B76/B77: the console Overview is computed from durable usage_records over a
time window, and reports served-on-prem beside required-on-prem."""
from __future__ import annotations

import asyncio
import datetime as dt

from app.apps import store
from app.apps.db import Database
from app.apps.models import UsageRecord


def _run(coro):
    return asyncio.run(coro)


async def _usage(s, rid, *, app="a1", provider="onprem", onprem_only=False,
                 status="ok", tier="cheap", cost=0.01, base=0.04, ts=None):
    await store.record_usage(
        s, application_id=app, request_id=rid, model="m", provider=provider,
        tier=tier, prompt_tokens=10, completion_tokens=5, total_tokens=15,
        cost_usd=cost, baseline_usd=base, latency_ms=10.0, sensitivity="public",
        onprem_only=onprem_only, status=status, quality=0.5)
    if ts is not None:
        await s.execute(UsageRecord.__table__.update()
                        .where(UsageRecord.request_id == rid).values(ts=ts))


def test_fleet_from_usage_counts_served_required_and_status():
    async def go():
        db = Database(url="sqlite+aiosqlite://")
        await db.ensure_ready()
        async with db.session() as s:
            await _usage(s, "r1", onprem_only=True)                  # required + served
            await _usage(s, "r2", onprem_only=False)                 # passthrough served
            await _usage(s, "r3", provider="azure", tier="frontier")  # cloud
            await _usage(s, "r4", provider="azure", onprem_only=True)  # violation
            await _usage(s, "r5", status="error", cost=99.0)
            await _usage(s, "r6", status="rejected", cost=99.0)
            await _usage(s, "r7", status="cancelled", cost=99.0)
            await _usage(s, "r8", app="a2")
            await store.write_outcome(s, dict(
                request_id="r1", application_id="a1", status="ok",
                detected_entity_types=["US_SSN"]))
            await store.write_outcome(s, dict(
                request_id="r2", application_id="a1", status="ok",
                detected_entity_types=[]))
            await s.commit()
            return await store.fleet_from_usage(s, since=None)
    rows = {r["app"]: r for r in _run(go())}
    a = rows["a1"]
    assert a["count"] == 4
    assert a["served_onprem_count"] == 2
    assert a["required_onprem_count"] == a["onprem_count"] == 2
    assert a["onprem_violations"] == 1
    assert a["pii_count"] == 1
    assert (a["error_count"], a["cancelled_count"], a["rejected_count"]) == (1, 1, 1)
    assert abs(a["total_usd"] - 0.04) < 1e-9
    assert a["tier_distribution"] == {"cheap": 3, "frontier": 1}
    assert "latency_p50_ms" in a and "token_intelligence_density" in a
    assert "routing_saved_usd" not in a
    assert rows["a2"]["count"] == 1


def test_fleet_from_usage_since_filters():
    async def go():
        db = Database(url="sqlite+aiosqlite://")
        await db.ensure_ready()
        now = dt.datetime.now(dt.timezone.utc)
        async with db.session() as s:
            await _usage(s, "old", ts=now - dt.timedelta(days=10))
            await _usage(s, "new")
            await s.commit()
            all_rows = await store.fleet_from_usage(s, since=None)
            recent = await store.fleet_from_usage(s, since=now - dt.timedelta(days=1))
            return all_rows, recent
    all_rows, recent = _run(go())
    assert all_rows[0]["count"] == 2
    assert recent[0]["count"] == 1


def test_fleet_route_accepts_hours(client):
    from tests.test_ui_routes import _login
    _login(client)
    client.post("/v1/chat/completions", headers={"Authorization": "Bearer acme-key"},
                json={"messages": [{"role": "user", "content": "hi"}]})
    for q in ("", "?hours=24", "?hours=720"):
        r = client.get(f"/api/fleet{q}")
        assert r.status_code == 200
        row = next(x for x in r.json() if x["app"] == "acme")
        assert row["count"] >= 1 and "served_onprem_count" in row
