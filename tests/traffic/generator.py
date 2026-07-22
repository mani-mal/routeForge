"""Async traffic generator (traffic-and-tests skill, Phase 7 Task 2).

Drives an `httpx.AsyncClient` against a *running* Serving Plane API — it
never imports the pipeline directly, so it exercises the exact HTTP surface
the UI watches (CLAUDE.md demo narrative step 5: the dashboard fills live).

Tier resolution: `/v1/chat/completions`'s response body carries `cost` and
`policy.onprem_only` but not the routing `tier` (that's resolved server-side
in `app/api/routes.py` from `Settings.routing.model_pool` and stashed only
in `HistoryStore`, on `app.state.history`). Rather than duplicate that
model->tier mapping here (and risk it drifting from `config/routing.yaml`),
this module fires all requests first, then does a single `GET
/api/requests?limit=...` at the end and aggregates tier/cost/onprem straight
from the server's own `RequestSummary` records, filtered down to just the
ids this run produced. That keeps the generator thin and the numbers
provably consistent with what the UI itself shows.

Latency (p50/p95) is measured client-side with `time.monotonic()` around
each HTTP round trip — that's the number a demo audience actually feels,
not the server's internal per-step trace latency.
"""

from __future__ import annotations

import asyncio
import random
import time
from dataclasses import dataclass, field

import httpx

from tests.traffic.personas import Persona, load_personas, sample


@dataclass
class Summary:
    count: int
    tier_distribution: dict[str, int]
    total_usd: float
    baseline_usd: float
    pct_saved: float
    routing_saved_usd: float
    compression_saved_usd: float
    onprem_count: int
    p50_ms: float
    p95_ms: float
    errors: list[dict] = field(default_factory=list)


def _percentile(sorted_vals: list[float], pct: float) -> float:
    if not sorted_vals:
        return 0.0
    idx = min(len(sorted_vals) - 1, int(round(pct * (len(sorted_vals) - 1))))
    return sorted_vals[idx]


async def _fire_one(
    client: httpx.AsyncClient,
    base_url: str,
    persona: Persona,
    rng: random.Random,
    latencies: list[float],
    ok_ids: list[str],
    errors: list[dict],
) -> None:
    body = persona.build(rng)
    headers = {"Authorization": f"Bearer {persona.api_key}"}
    start = time.monotonic()
    try:
        resp = await client.post(
            f"{base_url}/v1/chat/completions", json=body, headers=headers)
    except Exception as exc:  # network errors must not crash the run
        latencies.append((time.monotonic() - start) * 1000)
        errors.append({"persona": persona.name, "error": repr(exc)})
        return
    latencies.append((time.monotonic() - start) * 1000)
    if resp.status_code == 200:
        ok_ids.append(resp.json()["id"])
    else:  # a 422/503/etc — does NOT crash the run
        errors.append({
            "persona": persona.name,
            "status": resp.status_code,
            "body": resp.text[:200],
        })


async def run_traffic(
    base_url: str,
    *,
    n: int,
    rps: float,
    seed: int,
    personas: list[Persona] | None = None,
    headroom: bool | None = None,
    client: httpx.AsyncClient | None = None,
) -> Summary:
    """Fire `n` seeded, weighted-persona requests at `base_url` and return
    an aggregated `Summary`.

    Reproducible: the same `seed` always samples the same persona sequence
    and builds the same request bodies (personas.sample/build both draw from
    a single `random.Random(seed)`), so the same `--seed` produces the same
    numbers run to run.
    """
    owns_client = client is None
    http = client if client is not None else httpx.AsyncClient(timeout=30.0)
    try:
        if headroom is not None:
            resp = await http.post(
                f"{base_url}/api/controls", json={"headroom_enabled": headroom})
            resp.raise_for_status()

        if personas is None:
            personas = load_personas()
        rng = random.Random(seed)
        picked = sample(personas, rng, n)

        latencies: list[float] = []
        ok_ids: list[str] = []
        errors: list[dict] = []

        delay = (1.0 / rps) if rps and rps > 0 else 0.0
        tasks: list[asyncio.Task] = []
        for persona in picked:
            tasks.append(asyncio.create_task(
                _fire_one(http, base_url, persona, rng, latencies, ok_ids, errors)))
            if delay:
                await asyncio.sleep(delay)
        await asyncio.gather(*tasks)

        tier_distribution: dict[str, int] = {}
        total_usd = 0.0
        baseline_usd = 0.0
        routing_saved_usd = 0.0
        compression_saved_usd = 0.0
        onprem_count = 0

        if ok_ids:
            id_set = set(ok_ids)
            resp = await http.get(
                f"{base_url}/api/requests", params={"limit": max(n, len(ok_ids))})
            resp.raise_for_status()
            for s in resp.json():
                if s["id"] not in id_set:
                    continue
                tier_distribution[s["tier"]] = tier_distribution.get(s["tier"], 0) + 1
                total_usd += s["total_usd"]
                baseline_usd += s["baseline_usd"]
                routing_saved_usd += s["routing_saved_usd"]
                compression_saved_usd += s["compression_saved_usd"]
                if s["onprem_only"]:
                    onprem_count += 1

        pct_saved = (
            (baseline_usd - total_usd) / baseline_usd * 100.0
            if baseline_usd > 0 else 0.0
        )
        latencies.sort()

        return Summary(
            count=n,
            tier_distribution=tier_distribution,
            total_usd=total_usd,
            baseline_usd=baseline_usd,
            pct_saved=pct_saved,
            routing_saved_usd=routing_saved_usd,
            compression_saved_usd=compression_saved_usd,
            onprem_count=onprem_count,
            p50_ms=_percentile(latencies, 0.50),
            p95_ms=_percentile(latencies, 0.95),
            errors=errors,
        )
    finally:
        if owns_client:
            await http.aclose()
