from fastapi import FastAPI

from app.api.routes import router
from app.config.lint import enforce
from app.config.loader import load_settings


def create_app() -> FastAPI:
    settings = load_settings()
    enforce(settings)
    app = FastAPI(title="Serving Plane", version="0.1.0")
    app.state.settings = settings
    app.include_router(router)
    return app


app = create_app()
