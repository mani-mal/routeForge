"""Durable, DB-backed request history for the console dashboards (B3).

Replaces the process-local in-memory HistoryStore (formerly
app/api/history.py, deleted) as the source for list/stats/fleet/drilldown, so
history survives restart and is coherent across API replicas. Stores each
completed request's already-redacted pipeline response (types-only
detected_entities, no raw PII, no CCR contents; CLAUDE.md 2 & 6) plus
denormalized summary columns. Percentiles are computed in Python (sqlite has
no percentile function) for sqlite/Postgres parity.

`RequestSummary` (the list/stream shape) and `_LOCAL_PROVIDERS` live here.
"""
from __future__ import annotations

import json
import statistics
from collections import defaultdict

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


def _pctile(sorted_vals: list[float], pct: float) -> float:
    if not sorted_vals:
        return 0.0
    idx = min(len(sorted_vals) - 1, int(round(pct * (len(sorted_vals) - 1))))
    return sorted_vals[idx]


def _to_summary(row: RequestHistory) -> RequestSummary:
    return RequestSummary(
        id=row.request_id, tenant_id=row.tenant_id, sensitivity=row.sensitivity,
        onprem_only=row.onprem_only, model=row.model, model_display=row.model_display,
        tier=row.tier, provider=row.provider, total_usd=row.total_usd,
        baseline_usd=row.baseline_usd, routing_saved_usd=row.routing_saved_usd,
        compression_saved_usd=row.compression_saved_usd, latency_ms=row.latency_ms,
        hops=row.hops, detected_entities=json.loads(row.detected_entities or "[]"),
        created_seq=row.seq, total_tokens=row.total_tokens, quality_sum=row.quality_sum,
        quality=row.quality, quality_source=row.quality_source, session_id=row.session_id)


async def list_history(session: AsyncSession, *, limit: int = 100,
                       app: str | None = None) -> list[RequestSummary]:
    """Newest first (seq desc). `app` filters tenant_id; limit<=0 -> []."""
    if limit <= 0:
        return []
    stmt = select(RequestHistory).order_by(RequestHistory.seq.desc()).limit(limit)
    if app is not None:
        stmt = stmt.where(RequestHistory.tenant_id == app)
    return [_to_summary(r) for r in (await session.execute(stmt)).scalars()]


async def _all_summaries(session: AsyncSession) -> list[RequestSummary]:
    # Bounded table (retention_rows) -> a full scan here is bounded by construction.
    stmt = select(RequestHistory).order_by(RequestHistory.seq.desc())
    return [_to_summary(r) for r in (await session.execute(stmt)).scalars()]


async def stats(session: AsyncSession) -> dict:
    rows = await _all_summaries(session)
    count = len(rows)
    if count == 0:
        return {"count": 0, "total_usd": 0.0, "baseline_usd": 0.0,
                "routing_saved_usd": 0.0, "compression_saved_usd": 0.0,
                "onprem_count": 0, "tier_distribution": {}, "latency_p50_ms": 0.0,
                "latency_p95_ms": 0.0, "token_intelligence_density": 0.0,
                "cost_intelligence_density": 0.0}
    tier_distribution: dict[str, int] = {}
    for s in rows:
        tier_distribution[s.tier] = tier_distribution.get(s.tier, 0) + 1
    latencies = sorted(s.latency_ms for s in rows)
    q_sum = sum(s.quality_sum for s in rows)
    tok_sum = sum(s.total_tokens for s in rows)
    usd_sum = sum(s.total_usd for s in rows)
    return {
        "count": count,
        "total_usd": sum(s.total_usd for s in rows),
        "baseline_usd": sum(s.baseline_usd for s in rows),
        "routing_saved_usd": sum(s.routing_saved_usd for s in rows),
        "compression_saved_usd": sum(s.compression_saved_usd for s in rows),
        "onprem_count": sum(1 for s in rows if s.onprem_only),
        "tier_distribution": tier_distribution,
        "latency_p50_ms": _pctile(latencies, 0.50),
        "latency_p95_ms": _pctile(latencies, 0.95),
        "token_intelligence_density": (q_sum / tok_sum * 1000.0) if tok_sum > 0 else 0.0,
        "cost_intelligence_density": (q_sum / usd_sum) if usd_sum > 0 else 0.0,
    }


async def fleet(session: AsyncSession) -> list[dict]:
    rows = await _all_summaries(session)
    groups: dict[str, list[RequestSummary]] = defaultdict(list)
    for s in rows:
        groups[s.tenant_id].append(s)
    out = []
    for app, items in groups.items():
        lat = sorted(s.latency_ms for s in items)
        tiers: dict[str, int] = defaultdict(int)
        for s in items:
            tiers[s.tier] += 1
        tok = sum(s.total_tokens for s in items)
        usd = sum(s.total_usd for s in items)
        qsum = sum(s.quality_sum for s in items)
        out.append({
            "app": app,
            "count": len(items),
            "total_usd": usd,
            "baseline_usd": sum(s.baseline_usd for s in items),
            "routing_saved_usd": sum(s.routing_saved_usd for s in items),
            "compression_saved_usd": sum(s.compression_saved_usd for s in items),
            "onprem_count": sum(1 for s in items if s.onprem_only),
            "pii_count": sum(1 for s in items if s.detected_entities),
            "onprem_violations": sum(
                1 for s in items
                if s.onprem_only and s.provider not in _LOCAL_PROVIDERS),
            "latency_p50_ms": statistics.median(lat) if lat else 0.0,
            "tier_distribution": dict(tiers),
            "token_intelligence_density": (qsum / tok * 1000.0) if tok > 0 else 0.0,
            "cost_intelligence_density": (qsum / usd) if usd > 0 else 0.0,
        })
    out.sort(key=lambda r: r["count"], reverse=True)
    return out
