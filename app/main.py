import logging

from fastapi import FastAPI

from app.agent.internal_endpoint import internal_router
from app.api.routes import router
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
    app.state.pipeline = Pipeline(settings, gate, detector, gateway)
    app.include_router(router)
    app.include_router(internal_router)
    return app


app = create_app()
