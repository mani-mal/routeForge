import asyncio
import logging
import os
import uuid
from pathlib import Path

import yaml
from fastapi import FastAPI, Request
from fastapi.exception_handlers import (
    http_exception_handler, request_validation_exception_handler,
)
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.api.admin_routes import admin_router
from app.policy.trajectory import DbTrajectoryStore, prune_envelopes
from app.api.bus import DecisionBus
from app.api.openai_errors import CODE_INVALID_REQUEST, error_payload
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
from app.gateway.breaker import CircuitBreaker
from app.gateway.gateway import Gateway
from app.memory.store import DeterministicMemoryStore
from app.obs.log_config import configure_logging
from app.pipeline import Pipeline
from app.policy.detectors import make_detector
from app.policy.gate import Gate
from app.routing.observations import ObservationSink
from app.routing.quality import QualityResolver, QualityScore
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


async def flush_observations(db, sink) -> int:
    """Drain `sink` and persist the batch to `endpoint_observations` in one
    transaction (spec §3.2/§3.3). Called from the background flusher loop
    below AND from the shutdown handler -- neither may ever take the app down
    over a DB hiccup, so any exception (including one raised by `db.session()`
    itself, e.g. a bad connection) is logged and swallowed. Returns the number
    of rows written (0 on an empty drain or a swallowed failure). Also logs
    `sink.take_dropped()` when it is non-zero, independent of whether this
    particular batch was empty."""
    dropped = sink.take_dropped()
    if dropped:
        logging.getLogger("serving.routing").warning(
            "observation sink dropped %d observations (buffer full)", dropped)
    batch = sink.drain()
    if not batch:
        return 0
    try:
        async with db.session() as s:
            await apps_store.write_observations(s, batch)
            await s.commit()
        return len(batch)
    except Exception:
        logging.getLogger("serving.routing").warning(
            "observation flush failed; batch of %d observations dropped",
            len(batch), exc_info=True)
        return 0


async def _observation_loop(app: FastAPI) -> None:
    """Background flusher (spec §3.3), started by `start_observations` in the
    startup event -- runs on the real serving loop, never `create_app`'s
    throwaway one. Every `flush_interval_s`, flushes the sink; on the first
    iteration and then once per hour of loop time, additionally prunes rows
    older than `seed_window_hours` in their own session, swallowing errors so
    a prune failure can never kill the flusher.

    It is never cancelled mid-flush: shutdown sets `app.state.observation_stop`
    and the loop exits at its next wait, so a batch already drained from the
    sink always reaches its commit (the final flush in `stop_observations`
    picks up whatever was buffered after that)."""
    cfg = app.state.settings.routing.signals
    stop: asyncio.Event = app.state.observation_stop
    # Start "due": a process that lives less than an hour still prunes once.
    since_prune_s = 3600.0
    while not stop.is_set():
        try:
            await asyncio.wait_for(stop.wait(), cfg.flush_interval_s)
        except asyncio.TimeoutError:
            pass
        if stop.is_set():
            break   # the shutdown's final flush takes it from here
        await flush_observations(app.state.db, app.state.observation_sink)
        since_prune_s += cfg.flush_interval_s
        if since_prune_s >= 3600.0:
            since_prune_s = 0.0
            try:
                async with app.state.db.session() as s:
                    n = await apps_store.prune_observations(
                        s, older_than_hours=cfg.seed_window_hours)
                    # B72: expired trajectory-envelope observations.
                    await prune_envelopes(s)
                    await s.commit()
                logging.getLogger("serving.routing").info(
                    "observation log pruned rows=%d", n)
            except Exception:
                logging.getLogger("serving.routing").warning(
                    "observation log prune failed", exc_info=True)
            # P2b-2 Task 6: the same hourly beat also prunes routing_outcomes
            # (+ outcome_feedback), in its own guarded session -- a failure
            # here must never block the observation prune above (or vice
            # versa), so each gets its own try/except rather than sharing
            # one. `outcomes.enabled: false` skips it entirely (matches
            # app/api/routes.py's write-side gate on the same flag).
            outcomes_cfg = app.state.settings.serving.outcomes
            if outcomes_cfg.enabled:
                try:
                    async with app.state.db.session() as s:
                        n = await apps_store.prune_outcomes(
                            s, older_than_days=outcomes_cfg.retention_days)
                        await s.commit()
                    logging.getLogger("serving.routing").info(
                        "routing outcomes pruned rows=%d", n)
                except Exception:
                    logging.getLogger("serving.routing").warning(
                        "routing outcomes prune failed", exc_info=True)


def _log_flusher_death(task: "asyncio.Task[None]") -> None:
    """Done-callback: a flusher that died mid-life must not be silent. Normal
    exit (stop requested) and cancellation are not deaths."""
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        logging.getLogger("serving.routing").warning(
            "observation flusher died; observations will be flushed only at "
            "shutdown", exc_info=exc)


def start_observations(app: FastAPI) -> None:
    """Start the background flusher (`_observation_loop`) with its stop event
    and death logging. Called from the startup event."""
    app.state.observation_stop = asyncio.Event()
    task = asyncio.create_task(_observation_loop(app))
    task.add_done_callback(_log_flusher_death)
    app.state.observation_task = task


async def stop_observations(app: FastAPI) -> None:
    """Graceful shutdown of the flusher, then one final flush so everything
    buffered reaches the table. Never raises.

    The loop is asked to stop (never cancelled mid-flush, which would lose a
    batch already drained from the sink) and given `flush_interval_s + 5` s to
    finish; cancellation is the last resort on timeout. A flusher that already
    died with an exception is logged, not re-raised, so the final flush still
    runs."""
    cfg = app.state.settings.routing.signals
    app.state.observation_stop.set()
    try:
        await asyncio.wait_for(app.state.observation_task,
                               timeout=cfg.flush_interval_s + 5)
    except (Exception, asyncio.CancelledError):
        logging.getLogger("serving.routing").warning(
            "observation flusher did not stop cleanly", exc_info=True)
    await flush_observations(app.state.db, app.state.observation_sink)


async def refresh_quality(app: FastAPI) -> bool:
    """Reload `model_quality` into the shared resolver (P2b-3 §7). Called at
    boot, every `routing.quality_refresh_s` by `_quality_loop`, and by the
    calibration apply/revert routes so a change takes effect at once.

    Serialised by `app.state.quality_lock`: the DB read AND the snapshot swap
    happen under the lock, so refreshes install in the order they read. Without
    it a slow timer refresh that read BEFORE an apply committed could finish
    AFTER the apply's own refresh and put the pre-apply snapshot back.

    Returns True when the new snapshot was installed. On ANY failure the last
    good snapshot stays in place (quality only reorders; a stale snapshot is
    always safe), a warning is logged, and False is returned -- it never
    raises. Must run on the real serving loop, never in create_app's
    throwaway one."""
    lock = getattr(app.state, "quality_lock", None)
    if lock is None:
        lock = app.state.quality_lock = asyncio.Lock()
    async with lock:
        try:
            async with app.state.db.session() as session:
                rows = await apps_store.load_quality_overrides(session)
            app.state.quality.set_overrides({
                key: [QualityScore(score, source, set_id)
                      for source, (score, set_id) in by_source.items()]
                for key, by_source in rows.items()})
            return True
        except Exception:
            logging.getLogger("serving.routing").warning(
                "quality refresh failed; keeping the last snapshot", exc_info=True)
            return False


async def _quality_loop(app: FastAPI) -> None:
    """Background refresher: `refresh_quality` every
    `routing.quality_refresh_s` until `app.state.quality_stop` is set. A
    failed refresh is logged inside `refresh_quality` and never ends the
    loop."""
    stop: asyncio.Event = app.state.quality_stop
    while not stop.is_set():
        try:
            await asyncio.wait_for(
                stop.wait(), app.state.settings.routing.quality_refresh_s)
        except asyncio.TimeoutError:
            pass
        if stop.is_set():
            break
        await refresh_quality(app)


def create_app() -> FastAPI:
    # B57: first, so every startup line below (and every serving.* INFO line
    # after it) reaches stderr. Idempotent; touches only the `serving` logger.
    configure_logging()
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
    # above. `sink` is the in-process half of the observation log (signal
    # hardening spec §3.2/§3.3): every observation the store applies is
    # handed to it on the serving path (no I/O, never blocks, never raises),
    # and the background flusher started in the startup event below drains it
    # into `endpoint_observations`. Seeded by REPLAYING that table -- not by
    # aggregating `usage_records` -- in the startup event below, never here:
    # seeding needs a real DB read, and per the comment on `app.state.db`
    # above, no DB access may happen inside create_app's throwaway asyncio.run
    # loop (that's the exact bug class app/gateway/backends/factory.py's
    # resolve_backend split was fixed for).
    sink = ObservationSink(settings.routing.signals.max_buffer)
    app.state.observation_sink = sink
    signals = SignalStore(settings.routing.signals, sink=sink)
    app.state.signals = signals
    # named model_router (not router) to avoid shadowing the APIRouter imported above;
    # app.include_router(router) below needs the APIRouter, not this Router instance.
    # P2b-3: ONE quality resolver for the whole process, stored on app.state
    # and shared with the Router, so a refresh (boot, timer, apply/revert)
    # swaps the snapshot the router reads. Constructed with NO I/O and no
    # overrides -- the rows are loaded in the startup event below (never in
    # create_app's throwaway loop, see `signals` above). Until then, and
    # whenever the table is empty, it resolves exactly as before.
    quality = QualityResolver(settings, registry=registry)
    app.state.quality = quality
    model_router = Router(settings, quality_resolver=quality,
                          registry=registry, signals=signals)
    app.state.model_router = model_router
    # Step-7 circuit breaker (B45): ONE per-process instance, shared by the
    # Gateway (records outcomes, reads open_set() every hop) and the admin
    # models list (reads state() for display). Per-process/in-memory like
    # SignalStore -- see app/gateway/breaker.py's module docstring.
    breaker = CircuitBreaker(settings.routing.breaker)
    app.state.breaker = breaker
    gateway = Gateway(settings, backend, model_router, registry, signals=signals,
                      breaker=breaker)
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
    # B72: trajectory envelopes live in the deployment's own database so
    # every replica reads the same one. Lifetime is the operator's choice.
    app.state.trajectory_store = DbTrajectoryStore(
        app.state.db,
        ttl_seconds=float(os.environ.get("SP_TRAJECTORY_TTL_HOURS", "24")) * 3600)
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
        """Seed the shared SignalStore by REPLAYING the observation log
        (`endpoint_observations`, app/routing/observations.py, spec §3.4) --
        NOT by aggregating `usage_records` (pre-signal-hardening behaviour) --
        so a freshly booted replica doesn't start blind. Each row is replayed
        through the store's own observe_* methods at its TRUE age, so
        staleness and availability decay apply to seeded state exactly as to
        live state (that's what closes the stale-as-fresh defect). Runs on
        the REAL serving loop (the ASGI lifespan startup event), not
        create_app's throwaway asyncio.run loop -- see the comment where
        `signals`/`sink` are constructed above. Fails to the boring side: a
        failed or empty seed query logs a warning and leaves the store empty,
        which reports tier_default/neutral for every model -- exactly
        pre-P2a behaviour -- and must never prevent the app from serving.

        The background flusher is started AFTER this block, unconditionally
        (whether or not seeding succeeded), so live traffic still reaches the
        table even on a seed failure.
        """
        cfg = settings.routing.signals
        try:
            async with app.state.db.session() as session:
                obs = await apps_store.load_seed_observations(
                    session, since_hours=cfg.seed_window_hours,
                    per_model_cap=cfg.max_seed_obs_per_model)
            app.state.signals.replay(obs)
            logging.getLogger("serving.routing").info(
                "signal store seeded observations=%d models=%d",
                len(obs), len({o.model for o in obs}))
        except Exception:
            logging.getLogger("serving.routing").warning(
                "signal store seeding failed; starting with an empty store",
                exc_info=True)
        start_observations(app)

    @app.on_event("startup")
    async def _load_quality() -> None:
        """Boot-load `model_quality` into the shared resolver, then start the
        timed refresher. A failed load logs a warning (inside
        `refresh_quality`) and serves with no overrides -- the pre-P2b-3
        behaviour -- until a later refresh succeeds."""
        await refresh_quality(app)
        app.state.quality_stop = asyncio.Event()
        app.state.quality_task = asyncio.create_task(_quality_loop(app))

    @app.on_event("shutdown")
    async def _stop_quality() -> None:
        """Stop the refresher. It only ever reads, so cancelling it on a
        timeout loses nothing. Never raises."""
        stop = getattr(app.state, "quality_stop", None)
        task = getattr(app.state, "quality_task", None)
        if stop is None or task is None:
            return   # startup never ran (or failed before starting the loop)
        stop.set()
        try:
            await asyncio.wait_for(task, timeout=5)
        except (Exception, asyncio.CancelledError):
            logging.getLogger("serving.routing").warning(
                "quality refresher did not stop cleanly", exc_info=True)

    @app.on_event("shutdown")
    async def _stop_observations() -> None:
        """Stop the flusher gracefully and flush what is buffered
        (`stop_observations`); never raises."""
        await stop_observations(app)

    # Phase 7 Task 4: /api/traffic control state (app/api/ui_routes.py).
    app.state.traffic_running = False
    app.state.traffic_last_summary: dict | None = None
    app.include_router(router)
    app.include_router(ui_router)
    app.include_router(admin_router)

    # B49: OpenAI-shaped errors on /v1/* only. /admin/* and /api/* keep
    # FastAPI's {"detail": ...} -- the console reads it. Handlers are
    # app-level in FastAPI, so the surface is selected by path here.
    @app.exception_handler(StarletteHTTPException)
    async def _http_exception_handler(request: Request, exc: StarletteHTTPException):
        if not request.url.path.startswith("/v1/"):
            return await http_exception_handler(request, exc)
        return JSONResponse(
            status_code=exc.status_code,
            content=error_payload(exc.status_code, str(exc.detail),
                                  code=getattr(exc, "openai_code", None),
                                  type=getattr(exc, "openai_type", None),
                                  extra=getattr(exc, "openai_extra", None)),
            headers=getattr(exc, "headers", None))

    @app.exception_handler(RequestValidationError)
    async def _validation_handler(request: Request, exc: RequestValidationError):
        if not request.url.path.startswith("/v1/"):
            return await request_validation_exception_handler(request, exc)
        first = exc.errors()[0] if exc.errors() else {}
        loc = ".".join(str(p) for p in first.get("loc", ()) if p != "body")
        message = first.get("msg", "invalid request body")
        return JSONResponse(
            status_code=422,
            content=error_payload(422, f"{loc}: {message}" if loc else message,
                                  code=CODE_INVALID_REQUEST))

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
