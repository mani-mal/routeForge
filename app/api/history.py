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

# Providers that keep a request inside the residency boundary. Anything else
# serving an `onprem_only` request is a genuine Rule-3 breach (see CLAUDE.md #3).
# `mock` is the demo local stand-in; `?`/`-`/`""` are unknown/placeholder and
# never counted as a violation (fail toward not crying wolf — pool integrity is
# the config lint's job, not this honesty counter's). Any concrete cloud
# provider (azure, bedrock, …) is absent here and so correctly flags.
_LOCAL_PROVIDERS = frozenset({"onprem", "mock", "?", "-", ""})


class RequestSummary(BaseModel):
    id: str
    tenant_id: str
    sensitivity: str
    onprem_only: bool
    model: str
    model_display: str
    tier: str
    provider: str = "?"  # serving provider of the chosen model (onprem/azure/…)
    total_usd: float
    baseline_usd: float
    routing_saved_usd: float
    compression_saved_usd: float
    latency_ms: float
    hops: int
    detected_entities: list[str]
    created_seq: int
    total_tokens: int = 0
    quality_sum: float = 0.0            # Σ per-call quality — TID/CID numerator
    quality: float | None = None        # chosen model's score (display)
    quality_source: str | None = None   # provenance of `quality` (display)


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

    def record(self, resp: dict, tenant_id: str, tier: str,
               provider: str = "?") -> RequestSummary:
        # Evict the oldest full-response entry in lockstep with the deque's
        # own eviction, so `_full` never outlives `_summaries`.
        if len(self._summaries) == self._capacity and self._summaries:
            oldest = self._summaries[0]
            self._full.pop(oldest.id, None)

        cost = resp.get("cost", {})
        policy = resp.get("policy", {})
        usage = resp.get("usage", {})
        quality = resp.get("quality", {})
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
            created_seq=self._seq,
            total_tokens=int(usage.get("total_tokens", 0)),
            quality_sum=float(quality.get("sum", 0.0)),
            quality=quality.get("chosen"),
            quality_source=quality.get("source"),
        )
        self._summaries.append(summary)
        self._full[summary.id] = resp
        return summary

    def list(self, limit: int = 100, app: str | None = None) -> list[RequestSummary]:
        """Newest first. `app`, when set, restricts to summaries whose
        `tenant_id == app` (Task 1: per-app filter for the admin console).
        `limit <= 0` returns `[]`, matching the old slice-based `[:limit]`
        behavior for non-positive limits."""
        if limit <= 0:
            return []
        items = reversed(self._summaries)
        if app is not None:
            items = (s for s in items if s.tenant_id == app)
        out: list[RequestSummary] = []
        for s in items:
            out.append(s)
            if len(out) >= limit:
                break
        return out

    def get(self, request_id: str) -> dict | None:
        return self._full.get(request_id)

    def fleet(self) -> list[dict]:
        """One aggregate row per `tenant_id` (application) seen, sorted by
        `count` desc, for the fleet-overview table (Task 2)."""
        from collections import defaultdict
        import statistics
        groups: dict[str, list[RequestSummary]] = defaultdict(list)
        for s in self._summaries:
            groups[s.tenant_id].append(s)
        rows = []
        for app, items in groups.items():
            lat = sorted(s.latency_ms for s in items)
            tiers: dict[str, int] = defaultdict(int)
            for s in items:
                tiers[s.tier] += 1
            rows.append({
                "app": app,
                "count": len(items),
                "total_usd": sum(s.total_usd for s in items),
                "baseline_usd": sum(s.baseline_usd for s in items),
                "routing_saved_usd": sum(s.routing_saved_usd for s in items),
                "compression_saved_usd": sum(s.compression_saved_usd for s in items),
                "onprem_count": sum(1 for s in items if s.onprem_only),
                "pii_count": sum(1 for s in items if s.detected_entities),
                # Genuine residency breach: a request that MUST stay on-prem
                # (onprem_only — which already subsumes restricted entities)
                # but was served by an off-prem provider. 0 by construction;
                # >0 is a real alarm, unlike the old "any PII off-prem" heuristic.
                "onprem_violations": sum(
                    1 for s in items
                    if s.onprem_only and s.provider not in _LOCAL_PROVIDERS),
                "latency_p50_ms": statistics.median(lat) if lat else 0.0,
                "tier_distribution": dict(tiers),
                "token_intelligence_density": (
                    sum(s.quality_sum for s in items) / sum(s.total_tokens for s in items) * 1000.0
                    if sum(s.total_tokens for s in items) > 0 else 0.0),
                "cost_intelligence_density": (
                    sum(s.quality_sum for s in items) / sum(s.total_usd for s in items)
                    if sum(s.total_usd for s in items) > 0 else 0.0),
            })
        rows.sort(key=lambda r: r["count"], reverse=True)
        return rows

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
                "token_intelligence_density": 0.0,
                "cost_intelligence_density": 0.0,
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

        q_sum = sum(s.quality_sum for s in summaries)
        tok_sum = sum(s.total_tokens for s in summaries)
        usd_sum = sum(s.total_usd for s in summaries)

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
            "token_intelligence_density": (q_sum / tok_sum * 1000.0) if tok_sum > 0 else 0.0,
            "cost_intelligence_density": (q_sum / usd_sum) if usd_sum > 0 else 0.0,
        }
