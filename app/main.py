import asyncio
import logging
import os
from pathlib import Path

import yaml
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.agent.internal_endpoint import internal_router
from app.api.bus import DecisionBus
from app.api.history import HistoryStore
from app.apps.bootstrap import seed_demo_apps
from app.apps.db import Database
from app.apps.policy import RateLimiter
from app.api.routes import router
from app.api.ui_routes import router as ui_router
from app.compression.ccr import CCRStore
from app.config.lint import enforce
from app.config.loader import load_settings
from app.gateway.backends.factory import resolve_backend
from app.gateway.gateway import Gateway
from app.pipeline import Pipeline
from app.policy.detectors import make_detector
from app.policy.gate import Gate
from app.routing.router import Router

_DEFAULT_SERVER_PORT = 8080


def _self_port() -> int:
    """Port THIS app is listening on — the agent runtime's internal endpoint
    self-call target (see app/agent/runtime.py, app/pipeline.py).

    `SP_SERVER_PORT` wins when set (e.g. by tests/conftest.py's
    `live_internal_server` fixture, which boots on an ephemeral port and
    exports it so the app can self-call itself); otherwise falls back to
    `server.port` in config/serving.yaml, then a hardcoded default.
    """
    env = os.environ.get("SP_SERVER_PORT")
    if env is not None:
        return int(env)
    try:
        raw = yaml.safe_load(Path("config/serving.yaml").read_text()) or {}
        return int(raw.get("server", {}).get("port", _DEFAULT_SERVER_PORT))
    except Exception:
        return _DEFAULT_SERVER_PORT


def create_app() -> FastAPI:
    settings = load_settings()
    enforce(settings)
    from app.obs.otel import init_tracing
    try:
        init_tracing(settings)
    except Exception:  # telemetry must never break serving, not even at boot
        logging.getLogger("serving.obs").exception(
            "init_tracing failed (swallowed); telemetry disabled")
    detector = make_detector(settings.serving.pii_detection)
    app = FastAPI(title="Serving Plane", version="0.1.0")
    app.state.settings = settings
    # Multi-tenant application/token/usage store (spec 2026-07-25). The engine
    # is created here (no I/O); schema + demo seeding happen lazily on first DB
    # access inside the serving loop, so nothing binds to create_app's throwaway
    # asyncio.run loop used by resolve_backend below.
    app.state.db = Database(seed=seed_demo_apps)
    # Per-application rate limiter (in-memory sliding window; spec 2026-07-25).
    app.state.rate_limiter = RateLimiter()
    gate = Gate(settings, detector)
    app.state.gate = gate
    # Phase 9 Task 4: select + health-validate the configured backend.
    # create_app is sync but resolve_backend is async; this runs before
    # uvicorn's event loop exists (at import time / test fixture setup, never
    # from inside an already-running loop -- see app/gateway/backends/factory.py
    # and app/gateway/backends/README.md), so asyncio.run() is safe here.
    # mock/none never make a network call, so this is instant on the default
    # (demo) config; only vllm/dynamo can fail health and trigger
    # fallback-to-mock or sys.exit(1).
    backend = asyncio.run(resolve_backend(settings))
    # named model_router (not router) to avoid shadowing the APIRouter imported above;
    # app.include_router(router) below needs the APIRouter, not this Router instance.
    model_router = Router(settings)
    app.state.model_router = model_router
    gateway = Gateway(settings, backend, model_router)
    # Step 5 (Headroom compression): local, TTL-bounded CCR store — never
    # exported to telemetry, contents never leave this process. See
    # app/compression/ccr.py and CLAUDE.md rule 6.
    ccr = CCRStore(
        ttl_seconds=settings.serving.context_optimization.ccr.ttl_hours * 3600)
    # Stashed so the internal per-hop endpoint (app/agent/internal_endpoint.py)
    # can compress each agent hop's messages too — every LLM call the agent
    # makes must re-enter compression (5) and the router (6), not just the
    # outer request (CLAUDE.md). Reuses this same CCRStore instance, not a
    # second one, so CCR TTL/retrieval accounting is process-wide-consistent.
    app.state.co_cfg = settings.serving.context_optimization
    app.state.ccr = ccr
    # Computed once and reused for both the agent runtime's self-call
    # (Pipeline below) and the /api/traffic control's self-call (Phase 7
    # Task 4, app/api/ui_routes.py) — same own-port, single source.
    self_port = _self_port()
    app.state.self_port = self_port
    app.state.pipeline = Pipeline(
        settings, gate, detector, gateway, self_port=self_port,
        co_cfg_provider=lambda: app.state.co_cfg, ccr=ccr)
    # In-memory, redaction-safe request history for the Phase-6 UI's
    # list/detail/stats views (app/api/history.py). Process-local; resets
    # on restart — this is a demo prototype, not durable storage.
    app.state.history = HistoryStore(capacity=500)
    # Live decision stream (Phase 6a Task 2): in-process asyncio pub/sub
    # feeding GET /api/stream (SSE) for the flow visualizer. Bounded,
    # drop-oldest per-subscriber queues — see app/api/bus.py.
    app.state.bus = DecisionBus()
    # Phase 7 Task 4: /api/traffic control state (app/api/ui_routes.py).
    app.state.traffic_running = False
    app.state.traffic_last_summary: dict | None = None
    app.include_router(router)
    app.include_router(internal_router)
    app.include_router(ui_router)
    # Dev convenience: allow the Vite dev server to call the API directly.
    # Production serves the UI same-origin, where CORS is a no-op.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost:5173"],
        allow_methods=["*"],
        allow_headers=["*"],
    )
    return app


app = create_app()
