"""Process-local, in-memory request history (Phase 6a Task 1).

Stores each completed request's already-redacted pipeline response dict
(`app.state.pipeline.run(...)` output — types-only `detected_entities`, no
raw PII, no CCR contents; see CLAUDE.md rules 2 & 6) in a bounded ring
buffer, and derives a summary for the list/stats views.

This module intentionally does NOT depend on `app.config` — the caller
(`app/api/routes.py`) resolves the routing tier from `Settings.routing`
and passes it in, so `HistoryStore` stays a plain, settings-agnostic data
structure.
"""

from __future__ import annotations

from collections import deque

from pydantic import BaseModel


class RequestSummary(BaseModel):
    id: str
    tenant_id: str
    sensitivity: str
    onprem_only: bool
    model: str
    tier: str
    total_usd: float
    baseline_usd: float
    routing_saved_usd: float
    compression_saved_usd: float
    latency_ms: float
    hops: int
    detected_entities: list[str]
    created_seq: int


class HistoryStore:
    """Bounded ring buffer of request summaries + full responses.

    `deque(maxlen=capacity)` handles eviction of summaries; a parallel
    `dict[id, full_resp]` is kept in sync by hand (popping the oldest id
    whenever the deque is about to evict), so `get()` returns None for an
    evicted request instead of stale data.
    """

    def __init__(self, capacity: int = 500) -> None:
        self._capacity = capacity
        self._summaries: deque[RequestSummary] = deque(maxlen=capacity)
        self._full: dict[str, dict] = {}
        self._seq = 0

    def record(self, resp: dict, tenant_id: str, tier: str) -> RequestSummary:
        # Evict the oldest full-response entry in lockstep with the deque's
        # own eviction, so `_full` never outlives `_summaries`.
        if len(self._summaries) == self._capacity and self._summaries:
            oldest = self._summaries[0]
            self._full.pop(oldest.id, None)

        cost = resp.get("cost", {})
        policy = resp.get("policy", {})
        trace = resp.get("trace") or []
        latency_ms = sum(
            float(rec.get("latency_ms", 0.0)) for rec in trace if rec.get("latency_ms") is not None
        )
        # A "hop" is one model-generating routing decision: exactly 1 for a
        # direct request (step "router"), N for a multi-step agent run (one
        # "agent_hop" per LLM call). Counting raw trace records instead would
        # inflate this (verify + compression records are not hops) and mislead
        # the flow visualizer's single-call-vs-multi-step story.
        hops = sum(1 for rec in trace if rec.get("step") in ("router", "agent_hop"))

        self._seq += 1
        summary = RequestSummary(
            id=resp["id"],
            tenant_id=tenant_id,
            sensitivity=policy.get("sensitivity", "?"),
            onprem_only=bool(policy.get("onprem_only", False)),
            model=resp.get("model", "?"),
            tier=tier,
            total_usd=float(cost.get("total_usd", 0.0)),
            baseline_usd=float(cost.get("baseline_usd", 0.0)),
            routing_saved_usd=float(cost.get("routing_saved_usd", 0.0)),
            compression_saved_usd=float(cost.get("compression_saved_usd", 0.0)),
            latency_ms=latency_ms,
            hops=hops,
            detected_entities=list(policy.get("detected_entities", [])),
            created_seq=self._seq,
        )
        self._summaries.append(summary)
        self._full[summary.id] = resp
        return summary

    def list(self, limit: int = 100) -> list[RequestSummary]:
        """Newest first."""
        return list(reversed(self._summaries))[:limit]

    def get(self, request_id: str) -> dict | None:
        return self._full.get(request_id)

    def stats(self) -> dict:
        summaries = list(self._summaries)
        count = len(summaries)
        if count == 0:
            return {
                "count": 0,
                "total_usd": 0.0,
                "baseline_usd": 0.0,
                "routing_saved_usd": 0.0,
                "compression_saved_usd": 0.0,
                "onprem_count": 0,
                "tier_distribution": {},
                "latency_p50_ms": 0.0,
                "latency_p95_ms": 0.0,
            }

        tier_distribution: dict[str, int] = {}
        for s in summaries:
            tier_distribution[s.tier] = tier_distribution.get(s.tier, 0) + 1

        latencies = sorted(s.latency_ms for s in summaries)

        def _pctile(sorted_vals: list[float], pct: float) -> float:
            if not sorted_vals:
                return 0.0
            idx = min(len(sorted_vals) - 1, int(round(pct * (len(sorted_vals) - 1))))
            return sorted_vals[idx]

        return {
            "count": count,
            "total_usd": sum(s.total_usd for s in summaries),
            "baseline_usd": sum(s.baseline_usd for s in summaries),
            "routing_saved_usd": sum(s.routing_saved_usd for s in summaries),
            "compression_saved_usd": sum(s.compression_saved_usd for s in summaries),
            "onprem_count": sum(1 for s in summaries if s.onprem_only),
            "tier_distribution": tier_distribution,
            "latency_p50_ms": _pctile(latencies, 0.50),
            "latency_p95_ms": _pctile(latencies, 0.95),
        }
