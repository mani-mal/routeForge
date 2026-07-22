import asyncio

from tests.traffic.generator import run_traffic


def test_generator_fills_summary(live_internal_server):
    base = f"http://127.0.0.1:{live_internal_server.port}"
    summary = asyncio.run(run_traffic(base, n=40, rps=50, seed=42))
    assert summary.count == 40
    assert sum(summary.tier_distribution.values()) == 40
    assert summary.onprem_count > 0          # pii/high_risk personas kept on-prem
    assert summary.baseline_usd >= summary.total_usd  # savings non-negative in aggregate
    assert "cheap" in summary.tier_distribution  # simple_qa dominates
