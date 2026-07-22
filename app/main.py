import logging
import os
from pathlib import Path

import yaml
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.agent.internal_endpoint import internal_router
from app.api.history import HistoryStore
from app.api.routes import router
from app.api.ui_routes import router as ui_router
from app.compression.ccr import CCRStore
from app.config.lint import enforce
from app.config.loader import load_settings
from app.gateway.backends.mock import MockBackend
from app.gateway.gateway import Gateway
from app.pipeline import Pipeline
from app.policy.detectors import make_detector
from app.policy.gate import Gate
from app.routing.router import Router

# static demo api-key -> tenant map (design §3). Unknown/missing -> default.
API_KEYS = {"acme-key": "acme", "hospital-key": "hospital", "demo-key": "default"}

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
    app.state.api_keys = API_KEYS
    gate = Gate(settings, detector)
    app.state.gate = gate
    backend = MockBackend(settings)          # 'none'/vllm/dynamo handled in later phases
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
    app.state.pipeline = Pipeline(
        settings, gate, detector, gateway, self_port=_self_port(),
        cfg=settings.serving.context_optimization, ccr=ccr)
    # In-memory, redaction-safe request history for the Phase-6 UI's
    # list/detail/stats views (app/api/history.py). Process-local; resets
    # on restart — this is a demo prototype, not durable storage.
    app.state.history = HistoryStore(capacity=500)
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
