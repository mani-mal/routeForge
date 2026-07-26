"""Phase 6a Task 3: runtime Headroom toggle via GET/POST /api/controls.

The internal per-hop endpoint already reads the mutable
`request.app.state.co_cfg` on every call. The direct pipeline path used to
capture a FROZEN `ContextOptimizationCfg` in `Pipeline.__init__`, so a
runtime toggle never reached it. `Pipeline` now takes a
`co_cfg_provider: Callable[[], ContextOptimizationCfg]` and calls it on
every `run()` instead of reading a stored value — this file proves both
the API surface (GET/POST /api/controls) AND that the callback actually
changes DIRECT-path behavior between two calls on the SAME Pipeline
instance (not just that a fresh Pipeline would pick up a new value).
"""
from __future__ import annotations

import asyncio

from app.models import ChatRequest, Message

_ADMIN = {"email": "admin@routeforge.local", "password": "changeme"}


def test_toggle_headroom_runtime(client):
    # /api/* is admin-gated; log in so the session cookie rides the TestClient.
    assert client.post("/admin/login", json=_ADMIN).status_code == 200
    got = client.get("/api/controls").json()
    assert got["headroom_enabled"] is False  # config/serving.yaml default
    assert got["min_tokens"] == 2000
    assert "deferred_controls" in got and len(got["deferred_controls"]) == 2

    r = client.post("/api/controls", json={"headroom_enabled": True})
    assert r.status_code == 200
    assert r.json()["headroom_enabled"] is True

    assert client.get("/api/controls").json()["headroom_enabled"] is True

    # Flip back off — proves POST isn't a one-way ratchet.
    r = client.post("/api/controls", json={"headroom_enabled": False})
    assert r.status_code == 200 and r.json()["headroom_enabled"] is False
    assert client.get("/api/controls").json()["headroom_enabled"] is False


def test_pipeline_honors_runtime_cfg_toggle_between_calls():
    """Behavioral proof the toggle reaches the DIRECT pipeline path.

    Builds ONE Pipeline with a `co_cfg_provider` reading from a mutable
    holder (standing in for `app.state.co_cfg`, exactly as main.py wires
    `co_cfg_provider=lambda: app.state.co_cfg`). The SAME long, single
    -message request is run twice: once while the provider returns a
    disabled cfg (compression_saved_usd == 0), once after flipping the
    holder to an enabled cfg (compression_saved_usd > 0) — with no new
    Pipeline construction in between, so this isolates the callback
    mechanism itself, not just "a fresh Pipeline reads fresh config".

    NOTE on why this isn't driven through the live API instead: the direct
    path collapses ALL request messages into a single sanitized message
    (app/pipeline.py: `san_messages = [Message(role=..., content=sanitized)]`,
    built from Gate.evaluate's newline-joined redaction of every message).
    With the default `bypass_on.user_prompt=True`, that single (and
    therefore always-last) message is always protected from compression —
    see app/compression/__init__.py's `protect_last` — regardless of
    `headroom_enabled`, no matter how many messages the caller sends. Since
    `/api/controls` only exposes `headroom_enabled` (by design — see
    `_DEFERRED_CONTROLS`), a pure HTTP-level test cannot observe
    compression on the direct path at all under default bypass settings.
    This unit test instead forces `bypass_on.user_prompt=False` (mirroring
    `compression_on_pipeline` in conftest.py) so compression can actually
    apply, and toggles only `enabled` — the one thing `/api/controls`
    controls — between the two calls.
    """
    from app.compression.ccr import CCRStore
    from app.config.loader import load_settings
    from app.gateway.backends.mock import MockBackend
    from app.gateway.gateway import Gateway
    from app.pipeline import Pipeline
    from app.policy.detectors import make_detector
    from app.policy.gate import Gate
    from app.routing.router import Router

    settings = load_settings()
    bypass_on = settings.serving.context_optimization.bypass_on.model_copy(
        update={"user_prompt": False})
    co_off = settings.serving.context_optimization.model_copy(
        update={"enabled": False, "min_tokens": 1, "bypass_on": bypass_on})
    co_on = co_off.model_copy(update={"enabled": True})

    holder = {"cfg": co_off}

    detector = make_detector(settings.serving.pii_detection)
    gate = Gate(settings, detector)
    backend = MockBackend(settings)
    model_router = Router(settings)
    gateway = Gateway(settings, backend, model_router)
    ccr = CCRStore(ttl_seconds=co_off.ccr.ttl_hours * 3600)

    pipe = Pipeline(settings, gate, detector, gateway, self_port=8080,
                     co_cfg_provider=lambda: holder["cfg"], ccr=ccr)

    long_body = "INFO log line\n" * 800
    req = ChatRequest(messages=[Message(role="user", content=long_body)])

    out_off = asyncio.run(pipe.run("req-off", "acme", req))
    assert out_off["cost"]["compression_saved_usd"] == 0.0

    holder["cfg"] = co_on  # flip the "runtime toggle" — same Pipeline instance
    out_on = asyncio.run(pipe.run("req-on", "acme", req))
    assert out_on["cost"]["compression_saved_usd"] > 0.0
