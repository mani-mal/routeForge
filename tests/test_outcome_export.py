"""P2b-2 Task 6: `GET /admin/outcomes/export`, an admin-only NDJSON export of
`routing_outcomes` (+ joined `outcome_feedback`).

No pytest-asyncio in this repo (see tests/test_apps_store.py) -- seeding
helpers below use asyncio.run, matching tests/test_outcome_store.py.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import json

from app.apps import store as apps_store
from app.apps.models import RoutingOutcome
from tests.test_admin_api import _login


def _seed(app, rows: list[tuple[str, str, dt.datetime]]) -> None:
    """Write a minimal content-free outcome row for each (request_id,
    application_id, ts) and backdate `ts` (write_outcome always stamps
    "now" -- same two-pass pattern as test_outcome_store.py)."""
    async def go():
        async with app.state.db.session() as s:
            for request_id, application_id, _ts in rows:
                await apps_store.write_outcome(s, dict(
                    request_id=request_id, application_id=application_id,
                    status="ok", chosen="onprem/m", cost_usd=0.01,
                    latency_ms=10.0, prompt_tokens=5, completion_tokens=5))
            await s.commit()
        async with app.state.db.session() as s:
            for request_id, _application_id, ts in rows:
                row = await s.get(RoutingOutcome, request_id)
                row.ts = ts
            await s.commit()
    asyncio.run(go())


def _write_feedback(app, request_id: str, application_id: str, **kw) -> None:
    async def go():
        async with app.state.db.session() as s:
            await apps_store.write_feedback(
                s, request_id=request_id, application_id=application_id, **kw)
            await s.commit()
    asyncio.run(go())


def _lines(resp) -> list[dict]:
    return [json.loads(line) for line in resp.text.strip().splitlines() if line]


def test_export_returns_all_outcomes_with_feedback_joined(client):
    _login(client)
    now = dt.datetime.now(dt.timezone.utc)
    _seed(client.app, [
        ("r1", "A", now - dt.timedelta(hours=1)),
        ("r2", "A", now - dt.timedelta(hours=2)),
        ("r3", "B", now - dt.timedelta(hours=3)),
    ])
    _write_feedback(client.app, "r1", "A", score_name="thumbs", value_num=1.0,
                    value_cat=None, source="app")

    r = client.get("/admin/outcomes/export")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/x-ndjson")

    lines = _lines(r)
    assert len(lines) == 3
    for ln in lines:
        assert ln["schema"] == "routeforge.outcome/v1"
        # ts must be real ISO-8601 (round-trips through fromisoformat)
        dt.datetime.fromisoformat(ln["outcome"]["ts"])

    ids = {ln["outcome"]["request_id"] for ln in lines}
    assert ids == {"r1", "r2", "r3"}

    r1 = next(ln for ln in lines if ln["outcome"]["request_id"] == "r1")
    assert len(r1["feedback"]) == 1
    fb = r1["feedback"][0]
    assert fb["score_name"] == "thumbs"
    assert fb["value"] == 1.0
    assert fb["source"] == "app"
    dt.datetime.fromisoformat(fb["ts"])

    r2 = next(ln for ln in lines if ln["outcome"]["request_id"] == "r2")
    assert r2["feedback"] == []


def test_export_feedback_value_falls_back_to_value_cat(client):
    _login(client)
    now = dt.datetime.now(dt.timezone.utc)
    _seed(client.app, [("r1", "A", now)])
    _write_feedback(client.app, "r1", "A", score_name="rating", value_num=None,
                    value_cat="good", source="admin")

    r = client.get("/admin/outcomes/export")
    (line,) = _lines(r)
    assert line["feedback"] == [{"score_name": "rating", "value": "good",
                                 "source": "admin", "ts": line["feedback"][0]["ts"]}]


def test_export_filters_by_application_id(client):
    _login(client)
    now = dt.datetime.now(dt.timezone.utc)
    _seed(client.app, [
        ("r1", "A", now - dt.timedelta(hours=1)),
        ("r2", "A", now - dt.timedelta(hours=2)),
        ("r3", "B", now - dt.timedelta(hours=3)),
    ])
    r = client.get("/admin/outcomes/export", params={"application_id": "A"})
    lines = _lines(r)
    assert len(lines) == 2
    assert {ln["outcome"]["application_id"] for ln in lines} == {"A"}


def test_export_filters_by_since_until(client):
    _login(client)
    now = dt.datetime.now(dt.timezone.utc)
    _seed(client.app, [
        ("old", "A", now - dt.timedelta(days=10)),
        ("mid", "A", now - dt.timedelta(days=2)),
        ("new", "A", now - dt.timedelta(hours=1)),
    ])
    since = (now - dt.timedelta(days=3)).isoformat()
    until = (now - dt.timedelta(hours=12)).isoformat()
    r = client.get("/admin/outcomes/export", params={"since": since, "until": until})
    lines = _lines(r)
    assert {ln["outcome"]["request_id"] for ln in lines} == {"mid"}


def test_export_limit_caps_lines(client):
    _login(client)
    now = dt.datetime.now(dt.timezone.utc)
    _seed(client.app, [(f"r{i}", "A", now - dt.timedelta(minutes=i)) for i in range(5)])
    r = client.get("/admin/outcomes/export", params={"limit": 2})
    lines = _lines(r)
    assert len(lines) == 2


def test_export_limit_below_one_is_422(client):
    _login(client)
    r = client.get("/admin/outcomes/export", params={"limit": 0})
    assert r.status_code == 422


def test_export_pages_across_a_boundary(client, monkeypatch):
    from app.api import admin_routes
    monkeypatch.setattr(admin_routes, "_EXPORT_PAGE_SIZE", 2)
    _login(client)
    now = dt.datetime.now(dt.timezone.utc)
    _seed(client.app, [(f"r{i}", "A", now - dt.timedelta(minutes=i)) for i in range(5)])

    r = client.get("/admin/outcomes/export")
    ids = [ln["outcome"]["request_id"] for ln in _lines(r)]
    assert len(ids) == 5
    assert len(set(ids)) == 5


def test_export_requires_admin(client):
    r = client.get("/admin/outcomes/export")
    assert r.status_code == 401


def test_export_bad_since_is_422(client):
    _login(client)
    r = client.get("/admin/outcomes/export", params={"since": "not-a-date"})
    assert r.status_code == 422


def test_export_bad_until_is_422(client):
    _login(client)
    r = client.get("/admin/outcomes/export", params={"until": "not-a-date"})
    assert r.status_code == 422
