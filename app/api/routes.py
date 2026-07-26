import logging
import uuid

from fastapi import APIRouter, HTTPException, Request

from app.apps import store
from app.apps.langfuse_provision import load_app_langfuse
from app.apps.policy import AppPolicyView, day_spend_usd, policy_view_from
from app.models import ChatRequest
from app.obs.export import export_trace
from app.pipeline import PipelineRejected

router = APIRouter()


async def _authenticate(request: Request
                        ) -> tuple[str, AppPolicyView, object | None] | None:
    """Resolve the bearer token to (application id, policy view, per-app Langfuse
    or None), or None (fail closed). Unknown/missing/revoked token or a disabled
    application -> None -> 401. The application id equals the tenant slug, so
    downstream policy (policy.yaml tenants) is unchanged."""
    raw = request.headers.get("authorization", "").removeprefix("Bearer ").strip()
    async with request.app.state.db.session() as session:
        app = await store.resolve_application(session, raw)
        if app is None:
            return None
        app_id = app.id
        policy = policy_view_from(app)  # app.policy eager-loaded in the query
        app_langfuse = load_app_langfuse(app)  # None if no project provisioned
        await session.commit()  # persist last_used_at
        return app_id, policy, app_langfuse


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
    auth = await _authenticate(request)
    if auth is None:
        # Fail closed: no valid token -> reject. Never fall through to a
        # default tenant (that was the old fail-open behavior).
        raise HTTPException(401, "invalid or missing API token")
    tenant, app_policy, app_langfuse = auth

    # Per-application quotas (fail closed). Rate limit first (cheap, in-memory),
    # then the daily spend cap (one indexed sum). Both reject with 429; the
    # budget rejection is recorded as a usage row so it shows in the dashboard.
    if not request.app.state.rate_limiter.allow(tenant, app_policy.max_rpm):
        raise HTTPException(429, "rate limit exceeded for this application")
    if app_policy.max_spend_usd_per_day is not None:
        try:
            async with request.app.state.db.session() as session:
                spent = await day_spend_usd(session, tenant)
        except Exception:  # budget read must not hard-fail serving on a DB hiccup
            spent = 0.0
        if spent >= app_policy.max_spend_usd_per_day:
            try:
                async with request.app.state.db.session() as session:
                    await store.record_usage(
                        session, application_id=tenant, request_id=request_id,
                        model="-", provider="-", tier="-", prompt_tokens=0,
                        completion_tokens=0, total_tokens=0, cost_usd=0.0,
                        baseline_usd=0.0, latency_ms=0.0, sensitivity="-",
                        onprem_only=False, status="rejected")
                    await session.commit()
            except Exception:
                logging.getLogger("serving.obs").exception(
                    "rejected-usage record failed (swallowed)")
            raise HTTPException(429, "daily budget exceeded for this application")

    try:
        resp = await request.app.state.pipeline.run(
            request_id, tenant, body, app_policy=app_policy)
    except PipelineRejected as e:
        raise HTTPException(422, f"response failed verification: {e}")
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(503, "policy evaluation unavailable")
    try:
        lf_url = export_trace(
            request_id=resp["id"], tenant_id=tenant, model=resp["model"],
            cost=resp["cost"], policy=resp["policy"], trace=resp["trace"],
            application_id=tenant, app_langfuse=app_langfuse)
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
        # Durable per-application usage row (spec 2026-07-25). The in-memory
        # HistoryStore above is the UI's hot cache; this is the persistent
        # record that survives restarts and feeds per-app dashboards. Guarded:
        # metering must never break serving.
        pool = s.routing.model_pool
        model = resp.get("model", "?")
        tier = next((c.tier for c in pool if c.name == model), "?")
        provider = next((c.provider for c in pool if c.name == model), "?")
        usage = resp.get("usage", {})
        cost = resp.get("cost", {})
        policy = resp.get("policy", {})
        trace = resp.get("trace") or []
        latency_ms = sum(
            float(r.get("latency_ms", 0.0)) for r in trace
            if r.get("latency_ms") is not None)
        async with request.app.state.db.session() as session:
            await store.record_usage(
                session,
                application_id=tenant,
                request_id=resp["id"],
                model=model,
                provider=provider,
                tier=tier,
                prompt_tokens=int(usage.get("prompt_tokens", 0)),
                completion_tokens=int(usage.get("completion_tokens", 0)),
                total_tokens=int(usage.get("total_tokens", 0)),
                cost_usd=float(cost.get("total_usd", 0.0)),
                baseline_usd=float(cost.get("baseline_usd", 0.0)),
                latency_ms=latency_ms,
                sensitivity=str(policy.get("sensitivity", "?")),
                onprem_only=bool(policy.get("onprem_only", False)),
                status="ok",
            )
            await session.commit()
    except Exception:  # usage metering must never break serving
        logging.getLogger("serving.obs").exception(
            "usage record failed (swallowed)")
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
