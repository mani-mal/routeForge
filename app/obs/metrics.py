"""Feature 1 — Prometheus exposition for routing observability.

`prometheus_client` ships in the opt-in `observability` extra (same extra as
the OTel/Langfuse exporter). When it is not installed, every observe_* helper
is a no-op and render() returns empty — the app still boots and serves, and
GET /metrics reports 503 (see app/api/routes.py). Labels are bounded-cardinality
enums / model ids ONLY: never tenant, request_id, or any free text (CLAUDE.md:
per-tenant rollups live in RouteForge's own store, not in Prometheus labels)."""
from __future__ import annotations

import contextlib
import time

try:
    from prometheus_client import (
        CONTENT_TYPE_LATEST, REGISTRY, Counter, Histogram, generate_latest,
    )
    AVAILABLE = True
except ImportError:  # observability extra not installed
    AVAILABLE = False

if AVAILABLE:
    _requests = Counter(
        "routeforge_requests_total", "Requests served, by model and status.",
        ["model", "status"])
    _decisions = Counter(
        "routeforge_routing_decisions_total",
        "Routing decisions, by chosen tier and whether it was an escalation.",
        ["tier", "escalated"])
    _overhead = Histogram(
        "routeforge_routing_overhead_seconds",
        "Wall-time spent in Router.select().")
    _tokens = Counter(
        "routeforge_tokens_total", "Tokens processed, by model and direction.",
        ["model", "direction"])


def observe_request(model: str, status: str) -> None:
    if AVAILABLE:
        _requests.labels(model=model or "-", status=status).inc()


def observe_tokens(model: str, tokens_in: int, tokens_out: int) -> None:
    if AVAILABLE:
        m = model or "-"
        _tokens.labels(model=m, direction="in").inc(max(0, tokens_in))
        _tokens.labels(model=m, direction="out").inc(max(0, tokens_out))


def observe_routing_decision(tier: str, escalated: bool) -> None:
    if AVAILABLE:
        _decisions.labels(tier=tier, escalated=str(bool(escalated)).lower()).inc()


@contextlib.contextmanager
def time_routing():
    if not AVAILABLE:
        yield
        return
    start = time.perf_counter()
    try:
        yield
    finally:
        _overhead.observe(time.perf_counter() - start)


def render() -> tuple[bytes, str]:
    if not AVAILABLE:
        return b"", ""
    return generate_latest(REGISTRY), CONTENT_TYPE_LATEST
