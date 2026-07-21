from fastapi import FastAPI

from app.api.routes import router
from app.config.lint import enforce
from app.config.loader import load_settings
from app.policy.detectors import make_detector
from app.policy.gate import Gate

# static demo api-key -> tenant map (design §3). Unknown/missing -> default.
API_KEYS = {"acme-key": "acme", "hospital-key": "hospital", "demo-key": "default"}


def create_app() -> FastAPI:
    settings = load_settings()
    enforce(settings)
    detector = make_detector(settings.serving.pii_detection)
    app = FastAPI(title="Serving Plane", version="0.1.0")
    app.state.settings = settings
    app.state.api_keys = API_KEYS
    app.state.gate = Gate(settings, detector)
    app.include_router(router)
    return app


app = create_app()
