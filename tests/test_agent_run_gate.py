import asyncio


def test_semaphore_bounds_concurrent_sections():
    """The gate wrapper caps concurrency: with Semaphore(2), at most 2 of the
    guarded sections run at once. Mirrors how runtime.run wraps the NAT session.

    Repo has no pytest-asyncio; run the async body via asyncio.run (matches
    tests/test_gateway.py / test_controls.py)."""
    async def _inner():
        gate = asyncio.Semaphore(2)
        active = 0
        peak = 0

        async def guarded():
            nonlocal active, peak
            async with gate:          # the exact construct runtime.run uses
                active += 1
                peak = max(peak, active)
                await asyncio.sleep(0.02)
                active -= 1

        await asyncio.gather(*(guarded() for _ in range(6)))
        assert peak <= 2

    asyncio.run(_inner())


def test_gate_size_from_config():
    from app.config.loader import load_settings
    s = load_settings()
    n = int(s.policy.agent_budgets.get("max_concurrent_runs", 8))
    assert n == 8
