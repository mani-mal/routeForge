import asyncio

from tests.traffic.generator import run_traffic
from tests._litellm_fake import make_test_registry
from app.config.loader import load_settings
import pytest


def test_generator_fills_summary(live_internal_server):
    base = f"http://127.0.0.1:{live_internal_server.port}"
    # live_internal_server boots a real create_app() seeded with the same
    # SP_SEED_TEST_MODELS onboarded rows (its own isolated in-memory DB) --
    # build an independent registry with the identical seed for the tier map.
    registry = make_test_registry(load_settings())
    summary = asyncio.run(run_traffic(base, n=40, rps=50, seed=42, registry=registry))
    assert summary.count == 40
    assert sum(summary.tier_distribution.values()) == 40
    assert summary.onprem_count > 0          # pii/high_risk personas kept on-prem
    assert summary.baseline_usd >= summary.total_usd  # savings non-negative in aggregate
    assert "cheap" in summary.tier_distribution  # simple_qa dominates


def test_aggregation_reads_responses_not_history_ring_buffer():
    """Regression guard: the summary must aggregate from each chat RESPONSE,
    never re-fetch GET /api/requests (a 500-entry ring buffer that would evict
    a >500-request run's own early entries and under-count the demo's headline
    numbers). Uses a MockTransport so we can run far past 500 cheaply AND fail
    loudly if /api/requests is ever touched.
    """
    import httpx

    N = 750  # comfortably past the 500-entry HistoryStore ring buffer
    seen_paths: set[str] = set()

    def handler(request: httpx.Request) -> httpx.Response:
        seen_paths.add(request.url.path)
        if request.url.path == "/api/controls":
            return httpx.Response(200, json={"headroom_enabled": False})
        if request.url.path == "/v1/chat/completions":
            return httpx.Response(200, json={
                "id": "req-x", "model": "local-8b",
                "cost": {"total_usd": 0.0, "baseline_usd": 0.001,
                         "routing_saved_usd": 0.001, "compression_saved_usd": 0.0},
                "policy": {"onprem_only": True},
            })
        raise AssertionError(f"generator must not call {request.url.path}")

    registry = make_test_registry(load_settings())

    async def _run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
            return await run_traffic("http://x", n=N, rps=0, seed=1, client=c,
                                     registry=registry)

    summary = asyncio.run(_run())
    assert "/api/requests" not in seen_paths  # the bug source — never touched
    assert summary.count == N
    # every one of the 750 responses is aggregated — not capped at 500
    assert sum(summary.tier_distribution.values()) == N
    assert summary.tier_distribution == {"cheap": N}  # local-8b -> cheap
    assert abs(summary.baseline_usd - 0.001 * N) < 1e-6
    assert summary.onprem_count == N
