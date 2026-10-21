"""B75 item 3: per-endpoint "does it report cached tokens" derived from served traffic."""
import asyncio
import datetime as dt

from app.apps import store
from app.apps.bootstrap import seed_demo_apps
from app.apps.db import Database
from app.apps.models import UsageRecord

_T0 = dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)
_ADMIN = {"email": "admin@routeforge.local", "password": "changeme"}


def _rows(model, cached_list, status="ok", start=0):
    return [UsageRecord(id=f"{model}-{status}-{start + i}", application_id="a",
                        request_id=f"r{start + i}", model=model,
                        ts=_T0 + dt.timedelta(seconds=start + i),
                        cached_prompt_tokens=c, status=status,
                        sensitivity="public", onprem_only=False)
            for i, c in enumerate(cached_list)]


def _status(rows, models, **kw):
    db = Database(url="sqlite+aiosqlite://", seed=seed_demo_apps)

    async def go():
        async with db.session() as s:
            s.add_all(rows)
            await s.flush()
            return await store.cache_reporting_status(s, models, **kw)
    return asyncio.run(go())


def test_reported_when_any_non_null():
    out = _status(_rows("m", [None] * 5 + [0]), ["m"])
    assert out == {"m": "reported"}


def test_not_reported_needs_twenty_all_null():
    assert _status(_rows("m", [None] * 19), ["m"]) == {"m": "unknown"}
    assert _status(_rows("m", [None] * 20), ["m"]) == {"m": "not_reported"}


def test_no_traffic_is_unknown_and_non_ok_ignored():
    rows = _rows("m", [None] * 30, status="error")
    assert _status(rows, ["m", "other"]) == {"m": "unknown", "other": "unknown"}


def test_window_limits_to_most_recent_rows():
    # An old reported row falls outside window=20; the 20 newer rows are all null.
    def rows():  # fresh ORM objects per DB
        return _rows("m", [5], start=0) + _rows("m", [None] * 20, start=1)
    assert _status(rows(), ["m"], window=20) == {"m": "not_reported"}
    assert _status(rows(), ["m"], window=21) == {"m": "reported"}


def test_models_are_independent():
    rows = _rows("a", [None] * 25) + _rows("b", [3] * 2)
    assert _status(rows, ["a", "b"]) == {"a": "not_reported", "b": "reported"}


def test_admin_listing_includes_field(client, monkeypatch):
    from app.apps.probe import ProbeResult

    async def ok(*a, **k):
        return ProbeResult(True, "ok")
    monkeypatch.setattr("app.api.admin_routes.probe_endpoint", ok)
    assert client.post("/admin/login", json=_ADMIN).status_code == 200
    r = client.post("/admin/infra/models", json={
        "slug": "cr-mid", "display_name": "CR", "tier": "mid",
        "endpoint": "http://cr/v1", "price_input": 0.2, "price_output": 0.2})
    assert r.status_code == 200
    row = next(m for m in client.get("/admin/infra/models").json() if m["slug"] == "cr-mid")
    assert row["cache_reporting"] == "unknown"
