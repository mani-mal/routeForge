"""Durable, DB-backed request history for the console dashboards (B3).

Replaces the process-local in-memory HistoryStore (app/api/history.py) as the
source for list/stats/fleet/drilldown, so history survives restart and is
coherent across API replicas. Stores each completed request's already-redacted
pipeline response (types-only detected_entities, no raw PII, no CCR contents;
CLAUDE.md 2 & 6) plus denormalized summary columns. Percentiles are computed in
Python (sqlite has no percentile function) for sqlite/Postgres parity.

`RequestSummary` (the list/stream shape) and `_LOCAL_PROVIDERS` live here now;
app/api/history.py re-imports them until it is deleted.
"""
from __future__ import annotations

import json

from pydantic import BaseModel
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.apps.models import RequestHistory

# Providers that keep a request inside the residency boundary. Anything else
# serving an `onprem_only` request is a genuine Rule-3 breach (CLAUDE.md #3).
_LOCAL_PROVIDERS = frozenset({"onprem", "mock", "?", "-", ""})


class RequestSummary(BaseModel):
    id: str
    tenant_id: str
    sensitivity: str
    onprem_only: bool
    model: str
    model_display: str
    tier: str
    provider: str = "?"
    total_usd: float
    baseline_usd: float
    routing_saved_usd: float
    compression_saved_usd: float
    latency_ms: float
    hops: int
    detected_entities: list[str]
    created_seq: int
    total_tokens: int = 0
    quality_sum: float = 0.0
    quality: float | None = None
    quality_source: str | None = None
    session_id: str | None = None


def summarize(resp: dict, tenant_id: str, tier: str,
              provider: str = "?") -> RequestSummary:
    """Pure derivation of a RequestSummary from a redacted pipeline response.
    `created_seq` is 0 here — record_history sets the real value from the DB
    row seq. Used directly for the live bus publish (created_seq irrelevant to
    the SSE animation; the DB-backed list carries the authoritative seq)."""
    cost = resp.get("cost", {})
    policy = resp.get("policy", {})
    usage = resp.get("usage", {})
    quality = resp.get("quality", {})
    trace = resp.get("trace") or []
    latency_ms = sum(
        float(r.get("latency_ms", 0.0)) for r in trace
        if r.get("latency_ms") is not None)
    hops = sum(1 for r in trace if r.get("step") in ("router", "agent_hop"))
    return RequestSummary(
        id=resp["id"],
        tenant_id=tenant_id,
        sensitivity=policy.get("sensitivity", "?"),
        onprem_only=bool(policy.get("onprem_only", False)),
        model=resp.get("model", "?"),
        model_display=resp.get("model_display", resp.get("model", "?")),
        tier=tier,
        provider=provider,
        total_usd=float(cost.get("total_usd", 0.0)),
        baseline_usd=float(cost.get("baseline_usd", 0.0)),
        routing_saved_usd=float(cost.get("routing_saved_usd", 0.0)),
        compression_saved_usd=float(cost.get("compression_saved_usd", 0.0)),
        latency_ms=latency_ms,
        hops=hops,
        detected_entities=list(policy.get("detected_entities", [])),
        created_seq=0,
        total_tokens=int(usage.get("total_tokens", 0)),
        quality_sum=float(quality.get("sum", 0.0)),
        quality=quality.get("chosen"),
        quality_source=quality.get("source"),
        session_id=resp.get("session_id"),
    )


async def record_history(session: AsyncSession, resp: dict, *, tenant_id: str,
                         tier: str, provider: str = "?",
                         retention_rows: int = 5000) -> RequestSummary:
    """Persist one request_history row (caller commits) and prune to
    `retention_rows` newest by seq. Returns the summary with created_seq set to
    the real DB row seq (for the live bus publish)."""
    s = summarize(resp, tenant_id, tier, provider)
    row = RequestHistory(
        request_id=s.id, application_id=tenant_id, tenant_id=tenant_id,
        sensitivity=s.sensitivity, onprem_only=s.onprem_only, model=s.model,
        model_display=s.model_display, tier=s.tier, provider=s.provider,
        total_usd=s.total_usd, baseline_usd=s.baseline_usd,
        routing_saved_usd=s.routing_saved_usd,
        compression_saved_usd=s.compression_saved_usd, latency_ms=s.latency_ms,
        hops=s.hops, total_tokens=s.total_tokens, quality_sum=s.quality_sum,
        quality=s.quality, quality_source=s.quality_source, session_id=s.session_id,
        detected_entities=json.dumps(s.detected_entities),
        redacted_resp=json.dumps(resp))
    session.add(row)
    await session.flush()          # populate row.seq
    s.created_seq = row.seq
    # Prune: single indexed DELETE, no COUNT, portable (sqlite + Postgres).
    threshold = row.seq - retention_rows
    if threshold > 0:
        await session.execute(
            delete(RequestHistory).where(RequestHistory.seq <= threshold))
    return s


async def get_history(session: AsyncSession, request_id: str) -> dict | None:
    """The redacted response blob for the drilldown, or None if absent/pruned."""
    row = (await session.execute(
        select(RequestHistory).where(RequestHistory.request_id == request_id)
    )).scalars().first()
    if row is None:
        return None
    return json.loads(row.redacted_resp)
