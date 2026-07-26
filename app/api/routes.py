import logging
import uuid

from fastapi import APIRouter, HTTPException, Request

from app.models import ChatRequest
from app.obs.export import export_trace
from app.pipeline import PipelineRejected

router = APIRouter()


def _tenant_of(request: Request) -> str:
    key = request.headers.get("authorization", "").removeprefix("Bearer ").strip()
    return request.app.state.api_keys.get(key, "default")


@router.get("/health")
def health(request: Request) -> dict:
    s = request.app.state.settings
    return {"status": "ok", "backend": s.serving.inference_backend,
            "models": [c.name for c in s.routing.model_pool]}


@router.post("/v1/chat/completions")
async def chat_completions(body: ChatRequest, request: Request) -> dict:
    s = request.app.state.settings
    if body.stream and s.serving.reject_stream_requests:
        raise HTTPException(400, "streaming is not supported in the demo")
    request_id = f"req-{uuid.uuid4().hex[:8]}"
    tenant = _tenant_of(request)
    try:
        resp = await request.app.state.pipeline.run(request_id, tenant, body)
    except PipelineRejected as e:
        raise HTTPException(422, f"response failed verification: {e}")
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(503, "policy evaluation unavailable")
    try:
        lf_url = export_trace(
            request_id=resp["id"], tenant_id=tenant, model=resp["model"],
            cost=resp["cost"], policy=resp["policy"], trace=resp["trace"])
        # Live deep-link for the UI's "Langfuse trace" button (None when
        # telemetry is off → the UI hides it). Set before history.record so the
        # inspector's full detail carries it.
        if lf_url:
            resp["langfuse_url"] = lf_url
    except Exception:  # export_trace already swallows; this is belt-and-suspenders
        logging.getLogger("serving.obs").exception(
            "export_trace raised before its own guard (swallowed)")
    summary = None
    try:
        tier = next(
            (c.tier for c in s.routing.model_pool if c.name == resp["model"]), "?")
        if tier == "?":  # model not in the pool — surfaces as a "?" tier bucket
            logging.getLogger("serving.obs").debug(
                "history: no tier for model %r (not in model_pool)", resp["model"])
        summary = request.app.state.history.record(resp, tenant, tier)
    except Exception:  # history must never break serving
        logging.getLogger("serving.obs").exception(
            "history.record failed (swallowed)")
    try:
        # Live decision stream for the UI's flow visualizer (Phase 6a Task
        # 2). Same redaction-safe data history already stored — no raw PII,
        # no CCR contents — so this publish is safe to fan out as-is.
        # Guarded: a stalled/absent bus must never break serving.
        if summary is not None:
            bus = request.app.state.bus
            await bus.publish({"type": "request", "summary": summary.model_dump()})
            for index, record in enumerate(resp.get("trace") or []):
                await bus.publish({
                    "type": "step", "request_id": resp["id"],
                    "record": record, "index": index,
                })
    except Exception:  # streaming must never break serving
        logging.getLogger("serving.obs").exception(
            "bus.publish failed (swallowed)")
    return resp
