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
    _breaker = Counter(
        "routeforge_breaker_transitions_total",
        "Circuit-breaker state transitions, by the state entered.",
        ["to_state"])
    # B119 step 1: detector-executor jobs (Presidio work), by call-site hop.
    # Buckets reach past a minute: a queued 56k-token pass waits ~16 s per job
    # ahead of it, and the default buckets stop at 10 s.
    _DETECTOR_BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5,
                         5.0, 10.0, 20.0, 40.0, 80.0)
    _detector_queue = Histogram(
        "routeforge_detector_queue_seconds",
        "Time a detector-executor job waited in the queue before it started.",
        ["hop"], buckets=_DETECTOR_BUCKETS)
    _detector_run = Histogram(
        "routeforge_detector_run_seconds",
        "Time a detector-executor job ran on the worker.",
        ["hop"], buckets=_DETECTOR_BUCKETS)


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


def observe_breaker_transition(to_state: str) -> None:
    if AVAILABLE:
        _breaker.labels(to_state=to_state).inc()


def observe_detector_job(hop: str, queue_s: float, run_s: float) -> None:
    """One detector-executor job (B119). `hop` is a fixed call-site label from
    `app.policy.detector_pool` -- bounded, never tenant or request data."""
    if AVAILABLE:
        _detector_queue.labels(hop=hop).observe(queue_s)
        _detector_run.labels(hop=hop).observe(run_s)


def render() -> tuple[bytes, str]:
    if not AVAILABLE:
        return b"", ""
    return generate_latest(REGISTRY), CONTENT_TYPE_LATEST
