"""Reachability probe for onboarding an on-prem model at runtime.

Called synchronously from the admin onboarding handlers (app/api/admin_routes.py)
before a row is persisted — the result gates `enabled`. A model that fails
this probe is saved but never routable (see ModelRegistry.has_healthy_onprem,
which keys on `enabled`). This is what makes activation probe-gated rather
than a post-hoc health check.
"""
from __future__ import annotations

import os

import httpx


async def probe_endpoint(endpoint: str, slug: str, api_key_env: str | None
                         ) -> tuple[bool, str]:
    """GET `{endpoint}/models` and confirm `slug` is among the served ids.
    Any failure (network error, non-2xx, missing slug) returns (False, detail)
    rather than raising — the caller persists the row disabled either way."""
    headers = {}
    if api_key_env and os.environ.get(api_key_env):
        headers["Authorization"] = f"Bearer {os.environ[api_key_env]}"
    url = endpoint.rstrip("/") + "/models"
    try:
        async with httpx.AsyncClient(timeout=5) as c:
            resp = await c.get(url, headers=headers)
            resp.raise_for_status()
            ids = {m.get("id") for m in resp.json().get("data", [])}
        if slug not in ids:
            return False, f"served-model-name {slug!r} not in {sorted(ids)}"
        return True, "ok"
    except Exception as e:
        return False, str(e)
