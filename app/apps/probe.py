"""Reachability probe for onboarding an on-prem model at runtime.

Called synchronously from the admin onboarding handlers (app/api/admin_routes.py)
before a row is persisted — the result gates `enabled`. A model that fails
this probe is saved but never routable (see ModelRegistry.has_healthy_onprem,
which keys on `enabled`). This is what makes activation probe-gated rather
than a post-hoc health check.
"""
from __future__ import annotations

import os
from dataclasses import dataclass

import httpx


@dataclass(frozen=True)
class ProbeResult:
    """What one reachability probe learned about an endpoint.

    `ok`/`detail` are the reachability verdict the admin routes gate
    `enabled` on. `max_model_len` is independent: a reachable endpoint that
    publishes no context limit is still a successful probe (B50).
    """
    ok: bool
    detail: str
    max_model_len: int | None = None


def parse_max_model_len(entry: object) -> int | None:
    """The context window an upstream `/v1/models` entry declares, or None.

    vLLM publishes `max_model_len`; other OpenAI-compatible servers may not
    publish anything. Accepts an integer, an integral float, or a numeric
    string, and only when strictly positive. Everything else -- missing,
    null, a bool, a fractional float, an unparseable string, zero, negative --
    is None. **No default is ever guessed**: an unknown context window stays
    unknown, because a wrong number is worse than no number (a client that
    trusts it overflows, which is exactly the B36 failure this is meant to
    prevent).
    """
    if not isinstance(entry, dict):
        return None
    value = entry.get("max_model_len")
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        parsed: int | None = value
    elif isinstance(value, float):
        parsed = int(value) if value.is_integer() else None
    elif isinstance(value, str):
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            parsed = None
    else:
        parsed = None
    if parsed is None or parsed <= 0:
        return None
    return parsed


async def probe_endpoint(endpoint: str, slug: str, api_key_env: str | None
                         ) -> ProbeResult:
    """GET `{endpoint}/models` and confirm `slug` is among the served ids.
    Any failure (network error, non-2xx, missing slug) returns a failed
    `ProbeResult` rather than raising — the caller persists the row disabled
    either way. `max_model_len` is captured from the matching entry when the
    probe succeeds; it is never allowed to turn a successful probe into a
    failure (B50)."""
    headers = {}
    if api_key_env and os.environ.get(api_key_env):
        headers["Authorization"] = f"Bearer {os.environ[api_key_env]}"
    url = endpoint.rstrip("/") + "/models"
    try:
        async with httpx.AsyncClient(timeout=5) as c:
            resp = await c.get(url, headers=headers)
            resp.raise_for_status()
            entries = resp.json().get("data", [])
            ids = {m.get("id") for m in entries}
        if slug not in ids:
            return ProbeResult(False, f"served-model-name {slug!r} not in {sorted(ids)}")
        max_model_len = None
        for entry in entries:
            if isinstance(entry, dict) and entry.get("id") == slug:
                max_model_len = parse_max_model_len(entry)
                break
        return ProbeResult(True, "ok", max_model_len)
    except Exception as e:
        return ProbeResult(False, str(e))
