"""Phase 7 Task 1: RequestHistory ORM model for durable dashboard store.

Durable, bounded per-request record backing the console dashboards
(list/stats/fleet) and the drilldown. A NEW table -> create_all, no migration.
Distinct from UsageRecord: UsageRecord is the lean, unbounded metering/budget
ledger; RequestHistory is the rich, pruned dashboard store.
"""

import asyncio
import json

from app.apps.db import Database
from app.apps.models import RequestHistory


def test_create_all_makes_table_and_row_roundtrips():
    """Create an in-memory DB, add a RequestHistory row, verify it persists and
    roundtrips."""
    async def _run():
        db = Database(url="sqlite+aiosqlite://")  # in-memory; create_all runs on first session
        async with db.session() as s:
            s.add(RequestHistory(
                request_id="req-1", application_id="acme", created_at=None,
                tenant_id="acme", sensitivity="public", onprem_only=False,
                model="local-8b", model_display="Local 8B", tier="cheap", provider="onprem",
                total_usd=0.001, baseline_usd=0.004, routing_saved_usd=0.003,
                compression_saved_usd=0.0, latency_ms=12.0, hops=1, total_tokens=20,
                quality_sum=0.8, quality=0.8, quality_source="calibrated", session_id=None,
                detected_entities="[]", redacted_resp=json.dumps({"id": "req-1"})))
            await s.commit()
        async with db.session() as s:
            from sqlalchemy import select
            row = (await s.execute(select(RequestHistory))).scalars().one()
            assert row.seq >= 1                       # autoincrement populated
            assert row.request_id == "req-1"
            assert json.loads(row.redacted_resp)["id"] == "req-1"
        await db.dispose()

    asyncio.run(_run())
