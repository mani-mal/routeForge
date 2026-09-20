import asyncio
import logging
import os
import uuid
from pathlib import Path

import yaml
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.admin_routes import admin_router
from app.api.bus import DecisionBus
from app.api.pg_bridge import PgDecisionBridge
from app.apps.admin_auth import SessionStore
from app.apps.bootstrap import seed_all
from app.apps.db import Database
from app.apps.policy import RateLimiter
from app.apps import store as apps_store
from app.api.routes import router
from app.api.ui_routes import router as ui_router
from app.cache.store import DeterministicSemanticCache
from app.compression.ccr import CCRStore
from app.config.lint import enforce
from app.config.loader import load_settings
from app.gateway.backends.factory import resolve_backend
from app.gateway.gateway import Gateway
from app.memory.store import DeterministicMemoryStore
from app.pipeline import Pipeline
from app.policy.detectors import make_detector
from app.policy.gate import Gate
from app.routing.router import Router
from app.routing.signals import SignalStore

_DEFAULT_SERVER_PORT = 8080


def _self_port() -> int:
    """Port THIS app is listening on — the /api/traffic control's self-call
    target (see app/api/ui_routes.py).

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
    # Force the detector's lazy first-call loading (spaCy pipeline + recognizer
    # registry for Presidio) at boot, so the demo's opening request doesn't pay
    # cold-start latency — which was observed tripping the fail-closed gate into a
    # one-off 503 on the first live call. Non-fatal: the gate is fail-closed and
    # the regex fallback exists, so a warmup hiccup must never block serving.
    try:
        detector.warmup()
    except Exception:
        logging.getLogger("serving.policy").exception(
            "detector warmup failed (swallowed); first request may pay cold-start")
    app = FastAPI(title="Serving Plane", version="0.1.0")
    app.state.settings = settings
    # Multi-tenant application/token/usage store (spec 2026-07-25). The engine
    # is created here (no I/O); schema + demo seeding happen lazily on first DB
    # access inside the serving loop, so nothing binds to create_app's throwaway
    # asyncio.run loop used by resolve_backend below.
    app.state.db = Database(seed=seed_all)
    # Per-application rate limiter (in-memory sliding window; spec 2026-07-25).
    app.state.rate_limiter = RateLimiter()
    # Per-app privacy profile cache (Task 9): in-memory, single-node, cleared
    # on the admin PUT (Task 10). routes.py reads it before pipeline.run;
    # a miss loads from the DB and populates it. See app/apps/privacy.py.
    from app.apps.privacy import ProfileCache
    app.state.profile_cache = ProfileCache()
    # Admin dashboard sessions (in-memory; single-node, admin-only).
    app.state.admin_sessions = SessionStore()
    # Merged model source (model-onboarding plan): static YAML pool (cloud
    # built-ins) + enabled onboarded on-prem rows. Replaces InfraRoster. Sync
    # reads on the request path; lazily loaded on the first request (routes.py)
    # and refreshed on every admin write. Injected into gate/router/gateway/
    # backend so onboarded models are visible and on-prem availability is
    # derived (not a boot flag).
    from app.apps.registry import ModelRegistry
    registry = ModelRegistry(settings)
    app.state.registry = registry
    gate = Gate(settings, detector, registry=registry)
    app.state.gate = gate
    # Single LiteLLM egress backend, registry-injected so onboarded on-prem
    # endpoints are resolvable. No boot-time health probe (per-model probe is at
    # onboard time); resolve_backend now just constructs it.
    backend = asyncio.run(resolve_backend(settings, registry))
    app.state.backend = backend
    # P2a measured routing signals (app/routing/signals.py): ONE persistent,
    # traffic-fed store shared by the Router (ranking) and the Gateway
    # (observation), constructed here with NO I/O -- just like `registry`
    # above. Seeded from `usage_records` in the startup event below, never
    # here: seeding needs a real DB read, and per the comment on
    # `app.state.db` above, no DB access may happen inside create_app's
    # throwaway asyncio.run loop (that's the exact bug class
    # app/gateway/backends/factory.py's resolve_backend split was fixed for).
    signals = SignalStore(settings.routing.signals)
    app.state.signals = signals
    # named model_router (not router) to avoid shadowing the APIRouter imported above;
    # app.include_router(router) below needs the APIRouter, not this Router instance.
    model_router = Router(settings, registry=registry, signals=signals)
    app.state.model_router = model_router
    gateway = Gateway(settings, backend, model_router, registry, signals=signals)
    # Step 5 (Headroom compression): local, TTL-bounded CCR store — never
    # exported to telemetry, contents never leave this process. See
    # app/compression/ccr.py and CLAUDE.md rule 6.
    ccr = CCRStore(
        ttl_seconds=settings.serving.context_optimization.ccr.ttl_hours * 3600)
    app.state.co_cfg = settings.serving.context_optimization
    app.state.ccr = ccr
    # Feature 5 (Task 2): single shared, local, TTL-bounded memory store --
    # parallels `ccr` above exactly. Constructed ALWAYS (like CCRStore),
    # regardless of `memory.enabled`; Pipeline only reads/writes it when the
    # flag is on (see app/memory/__init__.py's bypass idiom), so a disabled
    # store just sits empty and unused. No runtime toggle endpoint exists for
    # memory today, so there is no `app.state.memory_cfg` to mirror
    # `app.state.co_cfg` -- Pipeline reads `settings.serving.memory` directly.
    memory = DeterministicMemoryStore(
        ttl_seconds=settings.serving.memory.ttl_hours * 3600)
    app.state.memory = memory
    # Feature #6 — semantic cache (Task 2). ALWAYS constructed (mirrors ccr
    # above), only USED by the pipeline when settings.serving.semantic_cache
    # .enabled is true (C7: with it false, nothing in the request path ever
    # calls into this instance). Local, TTL-bounded, tenant+residency-
    # partitioned — see app/cache/store.py and
    # docs/architecture/specs/semantic-cache-residency-spec.md.
    cache = DeterministicSemanticCache(
        ttl_seconds=settings.serving.semantic_cache.ttl_hours * 3600)
    app.state.cache = cache
    # Reused by the /api/traffic control's self-call (Phase 7 Task 4,
    # app/api/ui_routes.py) — own-port, single source.
    self_port = _self_port()
    app.state.self_port = self_port
    app.state.pipeline = Pipeline(
        settings, gate, detector, gateway,
        co_cfg_provider=lambda: app.state.co_cfg, ccr=ccr, registry=registry,
        memory=memory,
        cache=cache)
    # Live decision stream (Phase 6a Task 2): in-process asyncio pub/sub
    # feeding GET /api/stream (SSE) for the flow visualizer. Bounded,
    # drop-oldest per-subscriber queues — see app/api/bus.py.
    app.state.bus = DecisionBus()
    # B3: cross-replica bridge for the live decision stream. Each pod gets a
    # random replica_id (distinguishes "my own NOTIFY, already emitted
    # locally" from "another pod's NOTIFY, needs fan-out"); inert unless the
    # DB is Postgres (see PgDecisionBridge.active). The Phase-6 UI's
    # list/detail/stats views now read the durable `request_snapshots` table
    # (app/apps/store.py) directly, not a process-local HistoryStore.
    app.state.replica_id = uuid.uuid4().hex
    app.state.bridge = PgDecisionBridge(
        app.state.db, app.state.bus, replica_id=app.state.replica_id)

    @app.on_event("shutdown")
    async def _stop_bridge() -> None:
        await app.state.bridge.stop()

    @app.on_event("startup")
    async def _seed_signals() -> None:
        """Seed the shared SignalStore from recent `usage_records` so a
        freshly booted replica doesn't start blind. Runs on the REAL serving
        loop (the ASGI lifespan startup event), not create_app's throwaway
        asyncio.run loop -- see the comment where `signals` is constructed
        above. Fails to the boring side: a failed or empty seed query logs a
        warning and leaves the store empty, which reports tier_default/
        neutral for every model -- exactly pre-P2a behaviour -- and must
        never prevent the app from serving.
        """
        try:
            async with app.state.db.session() as session:
                rows = await apps_store.aggregate_model_signals(
                    session, since_hours=settings.routing.signals.seed_window_hours)
            app.state.signals.seed(rows)
            logging.getLogger("serving.routing").info(
                "signal store seeded models=%d", len(rows))
        except Exception:
            logging.getLogger("serving.routing").warning(
                "signal store seeding failed; starting with an empty store",
                exc_info=True)

    # Phase 7 Task 4: /api/traffic control state (app/api/ui_routes.py).
    app.state.traffic_running = False
    app.state.traffic_last_summary: dict | None = None
    app.include_router(router)
    app.include_router(ui_router)
    app.include_router(admin_router)
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
