import logging
import re
import uuid

from fastapi import APIRouter, HTTPException, Request, Response

from app.agent.runtime import AgentCapExceeded
from app.apps import store
from app.apps.langfuse_provision import load_app_langfuse
from app.apps.policy import AppPolicyView, day_spend_usd, policy_view_from
from app.errors import BackendUnavailable, NoEligibleCandidate, PolicyGateError
from app.models import ChatRequest
from app.obs import metrics
from app.obs.export import export_trace
from app.obs.redact import redact
from app.pipeline import PipelineRejected

router = APIRouter()


# B17: opaque, client-supplied workflow-run correlation id. A label only —
# never a policy control — so it FAILS OPEN: absent / over-long / malformed
# values yield None (the request is still served, just ungrouped). Bounded at
# intake (length + narrow charset) so it is safe to render in the console and
# to place in an OTel attribute without escaping. It is NOT redacted on either
# surface — grouping needs the value intact — so RouteForge treats it as an
# opaque, non-PII label BY CONTRACT: the client must not put PII in the
# correlation id. The charset alone does not exclude PII (e.g. an SSN like
# 123-45-6789 is digit-and-hyphen only and would pass), so this is a contract,
# not a filter.
_SESSION_ID_RE = re.compile(r"[A-Za-z0-9._:-]+")


def _sanitize_session_id(raw: str | None) -> str | None:
    if not raw:
        return None
    raw = raw.strip()
    if not raw or len(raw) > 128 or not _SESSION_ID_RE.fullmatch(raw):
        return None
    return raw


def _read_session_id(request: Request) -> str | None:
    return _sanitize_session_id(request.headers.get("x-routeforge-session"))


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
    registry = getattr(request.app.state, "registry", None)
    pool = registry.effective_pool() if registry else s.routing.model_pool
    return {"status": "ok", "backend": "litellm",
            "models": [c.name for c in pool]}


@router.get("/metrics")
def prometheus_metrics() -> Response:
    """Prometheus scrape surface. Open like /health; auth is a production
    concern (same posture as the decision-stream SSE)."""
    if not metrics.AVAILABLE:
        raise HTTPException(503, "metrics unavailable: install the observability extra")
    body, content_type = metrics.render()
    return Response(content=body, media_type=content_type)


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
        metrics.observe_request("-", "unauthorized")
        raise HTTPException(401, "invalid or missing API token")
    tenant, app_policy, app_langfuse = auth

    # Per-application quotas (fail closed). Rate limit first (cheap, in-memory),
    # then the daily spend cap (one indexed sum). Both reject with 429; the
    # budget rejection is recorded as a usage row so it shows in the dashboard.
    if not request.app.state.rate_limiter.allow(tenant, app_policy.max_rpm):
        metrics.observe_request("-", "rate_limited")
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
            metrics.observe_request("-", "budget_exceeded")
            raise HTTPException(429, "daily budget exceeded for this application")

    try:
        # Per-app privacy profile (custom recognizers + sensitivity rules,
        # Task 7), now cache-aware (Task 9): app.state.profile_cache is
        # checked first; a miss loads from the DB and populates the cache so
        # subsequent requests for the same app skip the DB round trip. The
        # admin PUT (Task 10) calls cache.clear(app_id) on writes so edits
        # take effect on the next request. A profile-load failure must not
        # silently fall back to the base detector/global rules for a request
        # an app has customized -- so the setup block below is wrapped to
        # raise PolicyGateError on any failure, which the taxonomy maps to a
        # fail-closed 503 (B1). Pipeline failures downstream map to their own
        # honest statuses (422/500/502), not this 503.
        try:
            cache = request.app.state.profile_cache
            profile = cache.get(tenant)
            if profile is None:
                async with request.app.state.db.session() as session:
                    profile = await store.get_privacy_profile(session, tenant)
                cache.put(tenant, profile)
            # Merged model registry: lazily loaded once, then cheap. Refreshed
            # by the admin model-onboarding routes so onboarded/enabled
            # on-prem models appear in (or drop out of) the router's
            # candidate pool promptly.
            await request.app.state.registry.ensure_loaded(request.app.state.db)
        except Exception as e:
            raise PolicyGateError("policy setup failed") from e
        resp = await request.app.state.pipeline.run(
            request_id, tenant, body, app_policy=app_policy, profile=profile)
    except PipelineRejected as e:
        metrics.observe_request("-", "rejected")
        message = str(e)
        prefix = ("model request rejected: "
                  if message.startswith("requested model")
                  else "response failed verification: ")
        raise HTTPException(422, f"{prefix}{message}")
    except NoEligibleCandidate as e:
        metrics.observe_request("-", "rejected")
        raise HTTPException(422, f"no model is available for this request's policy: {e}")
    except AgentCapExceeded:
        metrics.observe_request("-", "rejected")
        raise HTTPException(500, "agent did not converge within its step limit")
    except BackendUnavailable:
        metrics.observe_request("-", "rejected")
        raise HTTPException(502, "no backend could serve this request")
    except PolicyGateError:
        metrics.observe_request("-", "rejected")
        raise HTTPException(503, "policy evaluation unavailable")
    except HTTPException:
        raise
    except Exception:
        metrics.observe_request("-", "error")
        raise HTTPException(500, "internal error")
    # B17: capture the client's workflow-run correlation id (opaque label).
    # Threaded onto the response dict so both history.record and the drilldown
    # (which read `resp`) carry it with no signature change, and passed to
    # export_trace below for the Langfuse session stamp.
    session_id = _read_session_id(request)
    resp["session_id"] = session_id
    metrics.observe_request(resp["model"], "ok")
    usage = resp.get("usage", {})
    metrics.observe_tokens(resp["model"],
                           usage.get("prompt_tokens", 0),
                           usage.get("completion_tokens", 0))
    try:
        # Eval-capture (docs/eval-logging-backlog.md): when capture_io is
        # redacted|full, attach the request prompt + response completion to the
        # trace. `off` (demo default) passes empty strings and sets nothing.
        # The raw body prompt is redacted inside export_trace for `redacted`
        # mode — routes.py never decides what leaves; the exporter path does.
        # Per-app capture_io (B14) overrides the global default; null = inherit.
        capture_io = app_policy.capture_io or s.serving.observability.langfuse.capture_io
        # Fail-closed clamp: `full` stores RAW, permitted only for a non-onprem app
        # with its OWN Langfuse project (so raw lands only in a residency-local
        # project). Otherwise downgrade to `redacted` — never store raw (CLAUDE.md rules 2/8).
        if capture_io == "full" and (
                app_policy.residency == "onprem_only" or app_langfuse is None):
            capture_io = "redacted"
        prompt = body.messages[-1].content if body.messages else ""
        sanitized_prompt = resp.pop("sanitized_prompt", "")
        raw_completion = resp.pop("raw_completion", "")   # pre-verify; NEVER client-visible
        completion = ""
        try:
            completion = resp["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError, TypeError):
            completion = ""
        # First-party console view (Phase C Part 1). Mode-appropriate bodies for
        # the admin drilldown ONLY — this is history/HistoryStore, same residency
        # boundary as serving, NOT a telemetry egress (export_trace is unchanged).
        # `choices[]` stays raw for the calling client; this is additive metadata.
        if capture_io == "full":
            resp["display"] = {"mode": "full", "input": prompt, "output": completion}
        elif capture_io == "redacted":
            resp["display"] = {
                "mode": "redacted",
                "input": sanitized_prompt or redact(prompt, recognizers=profile.recognizers),
                "output": redact(completion, recognizers=profile.recognizers),
            }
        else:  # "off"
            resp["display"] = {"mode": "off", "input": None, "output": None}
        lf_url = export_trace(
            request_id=resp["id"], tenant_id=tenant, model=resp["model"],
            cost=resp["cost"], policy=resp["policy"], trace=resp["trace"],
            application_id=tenant, app_langfuse=app_langfuse,
            capture_io=capture_io, prompt=prompt, completion=completion,
            sanitized_prompt=sanitized_prompt, raw_completion=raw_completion,
            # Task 8: thread the app's custom recognizers (already resolved
            # above for the pipeline run, Task 7) into egress redaction so a
            # custom entity type (e.g. a hospital's MRN pattern) is masked in
            # telemetry, not just the built-in RegexDetector patterns.
            recognizers=profile.recognizers,
            session_id=session_id,
            routing_policy_version=s.routing_policy_version)
        # Live deep-link for the UI's "Langfuse trace" button (None when
        # telemetry is off → the UI hides it). Set before history.record so the
        # inspector's full detail carries it.
        if lf_url:
            resp["langfuse_url"] = lf_url
    except Exception:  # export_trace already swallows; this is belt-and-suspenders
        logging.getLogger("serving.obs").exception(
            "export_trace raised before its own guard (swallowed)")
    # Onboarded on-prem models (e.g. local-8b) live only in the registry's
    # effective pool, not the static `s.routing.model_pool` -- both lookups
    # below must consult it too, or every on-prem model's tier/provider
    # silently reports "?" once it left the static YAML pool.
    registry = getattr(request.app.state, "registry", None)
    pool = registry.effective_pool() if registry else s.routing.model_pool
    summary = None
    try:
        tier = next(
            (c.tier for c in pool if c.name == resp["model"]), "?")
        if tier == "?":  # model not in the pool — surfaces as a "?" tier bucket
            logging.getLogger("serving.obs").debug(
                "history: no tier for model %r (not in model_pool)", resp["model"])
        # Serving provider of the chosen model — lets the fleet aggregate spot a
        # genuine residency breach (an onprem_only request served off-prem), so
        # the warning flags real violations, not merely any detected entity.
        provider = next(
            (c.provider for c in pool if c.name == resp["model"]), "?")
        summary = request.app.state.history.record(resp, tenant, tier, provider)
    except Exception:  # history must never break serving
        logging.getLogger("serving.obs").exception(
            "history.record failed (swallowed)")
    try:
        # Durable per-application usage row (spec 2026-07-25). The in-memory
        # HistoryStore above is the UI's hot cache; this is the persistent
        # record that survives restarts and feeds per-app dashboards. Guarded:
        # metering must never break serving.
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
                quality=(resp.get("quality") or {}).get("chosen") or 0.0,
                quality_source=(resp.get("quality") or {}).get("source") or "?",
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
