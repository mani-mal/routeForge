import uuid

from fastapi import APIRouter, HTTPException, Request

from app.models import ChatRequest

router = APIRouter()


def _tenant_of(request: Request) -> str:
    auth = request.headers.get("authorization", "")
    key = auth.removeprefix("Bearer ").strip()
    return request.app.state.api_keys.get(key, "default")


@router.get("/health")
def health(request: Request) -> dict:
    s = request.app.state.settings
    return {"status": "ok", "backend": s.serving.inference_backend,
            "models": [c.name for c in s.routing.model_pool]}


@router.post("/v1/chat/completions")
def chat_completions(body: ChatRequest, request: Request) -> dict:
    s = request.app.state.settings
    if body.stream and s.serving.reject_stream_requests:
        raise HTTPException(400, "streaming is not supported in the demo")
    request_id = f"req-{uuid.uuid4().hex[:8]}"
    tenant = _tenant_of(request)
    try:
        ctx, _sanitized, _map = request.app.state.gate.evaluate(
            request_id, tenant, body)
    except Exception:
        raise HTTPException(503, "policy evaluation unavailable")
    # pipeline wiring lands in Task 14; return the gate verdict for now
    return {"request_id": request_id, "sensitivity": ctx.sensitivity,
            "onprem_only": ctx.onprem_only,
            "allowed_providers": sorted(ctx.allowed_providers),
            "detected_entities": list(ctx.detected_entities)}
