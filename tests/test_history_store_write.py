import asyncio

from sqlalchemy import func, select

from app.apps.db import Database
from app.apps.models import RequestHistory
from app.apps import history_store as hs


def _resp(rid, model="local-8b", total=0.0, base=0.001, route_saved=0.001,
          comp_saved=0.0, onprem=False, entities=None, session_id=None,
          trace=None):
    return {
        "id": rid, "model": model, "model_display": model,
        "cost": {"total_usd": total, "baseline_usd": base,
                 "routing_saved_usd": route_saved, "compression_saved_usd": comp_saved},
        "policy": {"sensitivity": "public", "onprem_only": onprem,
                   "detected_entities": entities or []},
        "usage": {"total_tokens": 20},
        "quality": {"sum": 0.8, "chosen": 0.8, "source": "calibrated"},
        "trace": trace if trace is not None else [{"step": "router", "latency_ms": 12.0}],
        "session_id": session_id,
    }


def test_summarize_derives_hops_latency_savings_session():
    s = hs.summarize(_resp("a", total=0.001, route_saved=0.003,
                           session_id="wf-1",
                           trace=[{"step": "router", "latency_ms": 5.0},
                                  {"step": "verify", "latency_ms": 2.0},
                                  {"step": "agent_hop", "latency_ms": 7.0}]),
                     "acme", "cheap", "onprem")
    assert s.hops == 2                    # router + agent_hop only, not verify
    assert s.latency_ms == 14.0           # sum of all latency_ms
    assert s.routing_saved_usd == 0.003
    assert s.session_id == "wf-1"


def test_record_history_inserts_and_get_roundtrips():
    async def _inner():
        db = Database(url="sqlite+aiosqlite://")
        async with db.session() as s:
            summary = await hs.record_history(
                s, _resp("req-1", entities=["US_SSN"]), tenant_id="acme",
                tier="cheap", provider="onprem", retention_rows=5000)
            await s.commit()
        assert summary.id == "req-1"
        assert summary.created_seq >= 1
        async with db.session() as s:
            blob = await hs.get_history(s, "req-1")
            assert blob["id"] == "req-1"
            assert await hs.get_history(s, "nope") is None
        await db.dispose()

    asyncio.run(_inner())


def test_retention_prunes_oldest_by_seq():
    async def _inner():
        db = Database(url="sqlite+aiosqlite://")
        async with db.session() as s:
            for i in range(5):
                await hs.record_history(s, _resp(f"r{i}"), tenant_id="acme",
                                        tier="cheap", provider="onprem", retention_rows=3)
            await s.commit()
        async with db.session() as s:
            n = (await s.execute(select(func.count()).select_from(RequestHistory))).scalar_one()
            assert n == 3
            ids = [r.request_id for r in
                   (await s.execute(select(RequestHistory).order_by(RequestHistory.seq))).scalars()]
            assert ids == ["r2", "r3", "r4"]     # oldest two pruned
        await db.dispose()

    asyncio.run(_inner())
