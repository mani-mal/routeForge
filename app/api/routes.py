from fastapi import APIRouter, Request

router = APIRouter()


@router.get("/health")
def health(request: Request) -> dict:
    s = request.app.state.settings
    return {
        "status": "ok",
        "backend": s.serving.inference_backend,
        "models": [c.name for c in s.routing.model_pool],
    }
