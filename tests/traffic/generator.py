"""Async traffic generator (traffic-and-tests skill, Phase 7 Task 2).

Drives an `httpx.AsyncClient` against a *running* Serving Plane API — it
never imports the pipeline directly, so it exercises the exact HTTP surface
the UI watches (CLAUDE.md demo narrative step 5: the dashboard fills live).

Aggregation source: each `/v1/chat/completions` response carries everything
the summary needs — `cost` (total/baseline/routing_saved/compression_saved)
and `policy.onprem_only`. Tier is the one field the response omits, so we
resolve `model -> tier` ONCE from the ModelRegistry's effective pool (static
`config/routing.yaml` model_pool + enabled onboarded on-prem rows -- pass the
in-process `registry` when driving the live demo's own server, see
app/api/ui_routes.py) and map each response's `model`. We deliberately do NOT
re-fetch `GET /api/requests` to aggregate: `HistoryStore`
is a fixed 500-entry ring buffer, so a run of >500 requests would evict its
own early entries before aggregation and silently under-count the demo's
headline numbers — exactly the >=500-request volume this generator exists
for. Aggregating straight from the responses we already hold is correct for
any `n`.

Latency (p50/p95) is measured client-side with `time.monotonic()` around
each HTTP round trip — that's the number a demo audience actually feels,
not the server's internal per-step trace latency.
"""

from __future__ import annotations

import asyncio
import os
import random
import time
from dataclasses import dataclass, field

import httpx

from app.config.loader import load_settings
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


def _model_tier_map(registry=None) -> dict[str, str]:
    """model name -> tier. Reads the registry's merged pool (static YAML +
    enabled onboarded rows) when given -- onboarded on-prem models (e.g.
    local-8b) never appear in the static config/routing.yaml model_pool any
    more, so a registry-blind map would bucket every on-prem response under
    the "?" tier instead of "cheap"/"mid". Falls back to the static pool
    (same config the server loads) when no registry is available, e.g. the
    standalone CLI driving a separately-running server."""
    pool = registry.effective_pool() if registry else load_settings().routing.model_pool
    return {c.name: c.tier for c in pool}


async def _fire_one(
    client: httpx.AsyncClient,
    base_url: str,
    persona: Persona,
    rng: random.Random,
    latencies: list[float],
    results: list[dict],
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
    if resp.status_code != 200:  # a 422/503/etc — does NOT crash the run
        errors.append({
            "persona": persona.name,
            "status": resp.status_code,
            "body": resp.text[:200],
        })
        return
    try:  # a malformed 200 body must also not crash the run
        results.append(resp.json())
    except Exception as exc:
        errors.append({"persona": persona.name, "error": f"bad body: {exc!r}"})


async def run_traffic(
    base_url: str,
    *,
    n: int,
    rps: float,
    seed: int,
    personas: list[Persona] | None = None,
    headroom: bool | None = None,
    client: httpx.AsyncClient | None = None,
    registry=None,
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
            # /api/controls is admin-gated (app/apps/admin_auth.require_admin),
            # so authenticate first — the session cookie then persists on this
            # httpx client. The per-request /v1 calls below carry their own
            # bearer token and are unaffected by the admin cookie.
            login = await http.post(f"{base_url}/admin/login", json={
                "email": os.environ.get("ADMIN_EMAIL", "admin@routeforge.local"),
                "password": os.environ.get("ADMIN_PASSWORD", "changeme"),
            })
            login.raise_for_status()
            resp = await http.post(
                f"{base_url}/api/controls", json={"headroom_enabled": headroom})
            resp.raise_for_status()

        if personas is None:
            personas = load_personas()
        rng = random.Random(seed)
        picked = sample(personas, rng, n)
        tier_map = _model_tier_map(registry)

        latencies: list[float] = []
        results: list[dict] = []
        errors: list[dict] = []

        delay = (1.0 / rps) if rps and rps > 0 else 0.0
        tasks: list[asyncio.Task] = []
        for persona in picked:
            tasks.append(asyncio.create_task(
                _fire_one(http, base_url, persona, rng, latencies, results, errors)))
            if delay:
                await asyncio.sleep(delay)
        # return_exceptions: a single task blowing up must not abort the burst
        await asyncio.gather(*tasks, return_exceptions=True)

        tier_distribution: dict[str, int] = {}
        total_usd = 0.0
        baseline_usd = 0.0
        routing_saved_usd = 0.0
        compression_saved_usd = 0.0
        onprem_count = 0

        # Aggregate straight from the responses we already hold — correct for
        # any n, independent of the server's ring-buffer history size.
        for r in results:
            cost = r.get("cost", {})
            tier = tier_map.get(r.get("model", ""), "?")
            tier_distribution[tier] = tier_distribution.get(tier, 0) + 1
            total_usd += cost.get("total_usd", 0.0)
            baseline_usd += cost.get("baseline_usd", 0.0)
            routing_saved_usd += cost.get("routing_saved_usd", 0.0)
            compression_saved_usd += cost.get("compression_saved_usd", 0.0)
            if r.get("policy", {}).get("onprem_only"):
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
