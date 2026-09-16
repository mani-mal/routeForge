import asyncio
import hashlib
import json
import logging
import re
import time
import uuid
from collections.abc import AsyncIterator, Callable

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from app.agent.runtime import AgentCapExceeded
from app.api.history import summarize
from app.api.streaming import (
    SSE_DONE, SSE_KEEPALIVE, chunk_text, openai_chunk, sse_error_frame, sse_frame,
    sse_response,
)
from app.apps import store
from app.apps.langfuse_provision import load_app_langfuse
from app.apps.policy import AppPolicyView, day_spend_usd, policy_view_from
from app.apps.privacy import AppPrivacyProfile
from app.errors import BackendUnavailable, NoEligibleCandidate, PolicyGateError
from app.models import ChatRequest
from app.obs import metrics
from app.obs.export import export_trace
from app.obs.redact import redact
from app.obs.scores import SCORE_NAMES, ensure_score_config, post_score
from app.output.streaming_verify import StreamingVerifier
from app.pipeline import PipelineRejected, StreamFallback
from app.policy.context import requestable_models
from app.policy.enums import STREAM_MODES

router = APIRouter()

_STREAM_REJECT = ("streaming is not supported for this application: set the "
                  "application's stream_mode (verified_then_stream, stream_unverified "
                  "or incremental_verify)")


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


def _derive_session_id(body: ChatRequest, tenant: str) -> str | None:
    """A stable session id for a conversation whose client sent none: a one-way
    hash of the app id plus the conversation's opening turns (first system
    message, first user message). Later turns append history but keep those
    opening turns, so every request in one conversation shares the id."""
    first_user = next((m.content for m in body.messages
                       if m.role == "user" and m.content), None)
    if not first_user:
        return None
    system = next((m.content or "" for m in body.messages if m.role == "system"), "")
    digest = hashlib.sha256(
        f"{tenant}\x00{system}\x00{first_user}".encode()).hexdigest()[:24]
    return f"auto-{digest}"


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


@router.get("/v1/models")
async def list_models(request: Request) -> dict:
    """OpenAI model list, scoped to the calling application: exactly the ids it
    may send as `model` on /v1/chat/completions. App-token authenticated and
    fail closed, like inference. `created` is 0: onboarding time is not part of
    the OpenAI contract clients rely on."""
    auth = await _authenticate(request)
    if auth is None:
        raise HTTPException(401, "invalid or missing API token")
    tenant, app_policy, _ = auth
    registry = request.app.state.registry
    await registry.ensure_loaded(request.app.state.db)
    models = requestable_models(request.app.state.settings, registry, tenant, app_policy)
    return {"object": "list",
            "data": [{"id": c.name, "object": "model", "created": 0,
                      "owned_by": c.provider} for c in models]}


def _resolve_stream_mode(body: ChatRequest, app_policy: AppPolicyView, s) -> str | None:
    """None for a normal (non-stream) request -- including a `stream: true`
    request from an app whose effective mode is "not streaming". Resolves
    the app's `stream_mode` override for `stream: true`: `None`/`STREAM_MODES[0]`
    ("off", no/explicit-off per-app override) keeps today's behavior: reject
    with 400 when the deployment rejects inbound streaming
    (`server.reject_stream_requests`) -- backwards compatible for every app
    that hasn't opted into a stream mode. Normalizes "off" to None here (the
    single source of truth for "not streaming") so the dispatch in
    `chat_completions` only ever has to check `is None`, even when
    `reject_stream_requests` is False and an app explicitly set "off"."""
    if not body.stream:
        return None
    mode = app_policy.stream_mode
    if mode in (None, STREAM_MODES[0]) and s.serving.reject_stream_requests:
        raise HTTPException(400, _STREAM_REJECT)
    return None if mode in (None, STREAM_MODES[0]) else mode


def _error_status_detail(exc: BaseException) -> tuple[int, str] | None:
    """The (status, detail) the HTTP path returns for a serving exception. One
    mapping, used by the plain HTTP path and by the keepalive stream's error
    frame, so the two can never drift."""
    if isinstance(exc, PipelineRejected):
        message = str(exc)
        prefix = ("model request rejected: " if message.startswith("requested model")
                  else "response failed verification: ")
        return 422, f"{prefix}{message}"
    if isinstance(exc, NoEligibleCandidate):
        return 422, f"no model is available for this request's policy: {exc}"
    if isinstance(exc, AgentCapExceeded):
        return 500, "agent did not converge within its step limit"
    if isinstance(exc, BackendUnavailable):
        return 502, f"no backend could serve this request: {exc}"
    if isinstance(exc, PolicyGateError):
        return 503, "policy evaluation unavailable"
    return None


class ClientDisconnected(Exception):
    """The client went away before a complete answer was ready."""


async def _await_unless_disconnected(task: "asyncio.Task", is_disconnected,
                                     poll_s: float):
    """Await `task`, polling the client connection every `poll_s`. A client that
    disconnected gets its backend work cancelled; cancelling the task closes the
    in-flight model call, which OpenAI-compatible servers treat as an abort.
    Only for paths that wait for a complete answer; real token streaming keeps
    its drain-on-disconnect accounting."""
    while True:
        done, _ = await asyncio.wait({task}, timeout=poll_s)
        if done:
            return task.result()
        if await is_disconnected():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                # Swallow whatever `task` raises here -- its own cancellation,
                # or a real pipeline exception that happened to land while we
                # were awaiting `is_disconnected()`. Either way the client is
                # already gone, so no status is ever read for this request.
                pass
            raise ClientDisconnected()


async def _record_cancelled(request: Request, *, tenant: str, request_id: str,
                            started: float) -> None:
    """Meter a request the client abandoned. Distinct from `ok` so dashboards
    and budgets don't count work nobody received as served."""
    metrics.observe_request("-", "cancelled")
    logging.getLogger("serving.api").info(
        "client disconnected; cancelled %s after %.1fs", request_id,
        time.monotonic() - started)
    try:
        async with request.app.state.db.session() as session:
            await store.record_usage(
                session, application_id=tenant, request_id=request_id,
                model="-", provider="-", tier="-", prompt_tokens=0,
                completion_tokens=0, total_tokens=0, cost_usd=0.0,
                baseline_usd=0.0,
                latency_ms=(time.monotonic() - started) * 1000,
                sensitivity="-", onprem_only=False, status="cancelled")
            await session.commit()
    except Exception:
        logging.getLogger("serving.obs").exception(
            "cancelled-usage record failed (swallowed)")


@router.post("/v1/chat/completions", response_model=None)
async def chat_completions(body: ChatRequest, request: Request) -> dict | StreamingResponse:
    s = request.app.state.settings
    request_id = f"req-{uuid.uuid4().hex[:8]}"
    started = time.monotonic()
    started_ns = time.time_ns()
    auth = await _authenticate(request)
    if auth is None:
        # Fail closed: no valid token -> reject. Never fall through to a
        # default tenant (that was the old fail-open behavior). A stream
        # request with no resolvable app policy at all keeps the old,
        # unconditional 400 (the reject guard used to run before auth) --
        # backwards compatible even for this edge case.
        metrics.observe_request("-", "unauthorized")
        if body.stream and s.serving.reject_stream_requests:
            raise HTTPException(400, _STREAM_REJECT)
        raise HTTPException(401, "invalid or missing API token")
    tenant, app_policy, app_langfuse = auth
    stream_mode = _resolve_stream_mode(body, app_policy, s)

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

    # Read the opaque workflow-correlation id once, up front: the streaming
    # branch below returns before the old (post-pipeline) read site, and every
    # response shape's _finalize needs it. If the client sent none, derive a
    # stable one from the app id + opening turns (unless the operator has
    # switched that off) so an agent conversation with no header still groups
    # into one session as its history grows.
    session_id = _read_session_id(request)
    if session_id is None and s.serving.derive_session_id:
        session_id = _derive_session_id(body, tenant)

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
        if stream_mode in ("stream_unverified", "incremental_verify"):
            # Real backend token streaming (Tasks 6-7). run_stream EAGERLY runs
            # the gate, task-mode decision, and the runtime floor before
            # returning, so a floored request (multi_step / onprem_only /
            # require_verification) raises StreamFallback here -- before any
            # token is emitted -- and we transparently fall through to the
            # verified_then_stream path below. The floor is enforced ONLY in the
            # pipeline (Global Constraint 1); the route never re-implements it.
            try:
                deltas, get_meta = await request.app.state.pipeline.run_stream(
                    request_id, tenant, body, app_policy=app_policy, profile=profile,
                    session_id=session_id)
            except StreamFallback:
                pass  # fall through: run the FULL verified pipeline, then chunk it
            else:
                if stream_mode == "stream_unverified":
                    return _stream_unverified_response(
                        request, request_id=request_id, deltas=deltas,
                        get_meta=get_meta, body=body, tenant=tenant,
                        app_policy=app_policy, app_langfuse=app_langfuse,
                        profile=profile, session_id=session_id, s=s,
                        started_ns=started_ns)
                # incremental_verify (Task 7): stream real tokens, but pipe each
                # delta through a hold-back StreamingVerifier so no un-redacted
                # text is ever released. Build the verifier on the request's
                # EFFECTIVE detector + ctx (exposed on get_meta by run_stream),
                # so egress redaction matches this app's intake.
                return _incremental_verify_response(
                    request, request_id=request_id, deltas=deltas,
                    get_meta=get_meta, body=body, tenant=tenant,
                    app_policy=app_policy, app_langfuse=app_langfuse,
                    profile=profile, session_id=session_id, s=s,
                    started_ns=started_ns)
        if stream_mode is not None:
            task = asyncio.create_task(request.app.state.pipeline.run(
                request_id, tenant, body, app_policy=app_policy, profile=profile,
                session_id=session_id))
            keepalive = s.serving.stream_keepalive_s
            done, _ = await asyncio.wait({task}, timeout=keepalive)
            if not done:
                return sse_response(_keepalive_stream(
                    request, task=task, keepalive_s=keepalive, request_id=request_id,
                    body=body, tenant=tenant, app_policy=app_policy,
                    app_langfuse=app_langfuse, profile=profile,
                    session_id=session_id, s=s, started=started,
                    started_ns=started_ns))
            resp = task.result()   # raises -> the except clauses below map it
        else:
            task = asyncio.create_task(request.app.state.pipeline.run(
                request_id, tenant, body, app_policy=app_policy, profile=profile,
                session_id=session_id))
            resp = await _await_unless_disconnected(
                task, request.is_disconnected, s.serving.stream_keepalive_s)
    except HTTPException:
        raise
    except ClientDisconnected:
        await _record_cancelled(request, tenant=tenant, request_id=request_id,
                                started=started)
        return Response(status_code=499)
    except Exception as e:
        mapped = _error_status_detail(e)
        if mapped is None:
            metrics.observe_request("-", "error")
            raise HTTPException(500, "internal error")
        metrics.observe_request("-", "rejected")
        raise HTTPException(mapped[0], mapped[1])
    await _finalize(request, resp, body=body, tenant=tenant, app_policy=app_policy,
                    app_langfuse=app_langfuse, profile=profile,
                    session_id=session_id, s=s, request_id=request_id,
                    started_ns=started_ns)
    if stream_mode is None:
        return resp
    # verified_then_stream: the pipeline already ran the FULL request above
    # and step 9 already verified it (same `pipeline.run` call as the
    # non-stream path) -- this only chunks the finished, already-verified
    # text out as SSE. Reached for `verified_then_stream`, and for
    # `stream_unverified`/`incremental_verify` requests that hit the runtime
    # floor (StreamFallback above) -- a floored request is always verified as a
    # complete response, never streamed unverified/incrementally.
    message = resp["choices"][0]["message"]
    return sse_response(chunk_text(request_id, resp["model"], message["content"] or "",
                                   tool_calls=message.get("tool_calls")))


def _full_capture_io(body: ChatRequest, resp: dict, prompt: str, completion: str,
                     raw_completion: str) -> tuple[str, str, str]:
    """Trace input/output for `capture_io == "full"` (the app's OWN project only;
    the caller has already applied the full-eligibility clamp).

    Input: the whole request -- every message with its tool structure, plus
    `tools` / `tool_choice` -- as JSON, whenever there is more than one message
    or tools are offered. A single plain message stays plain text.

    Output: when the model called tools, the assistant message as JSON with its
    `tool_calls` (content is the pre-verify raw text, or null). Otherwise the
    plain-text completion is unchanged. Returns (prompt, completion,
    raw_completion) with raw_completion carrying the same value, so the
    exporter's "prefer raw_completion" rule picks it up."""
    if len(body.messages) > 1 or body.tools:
        request_io: dict = {"messages": [m.model_dump(exclude_none=True)
                                         for m in body.messages]}
        if body.tools:
            request_io["tools"] = body.tools
        if body.tool_choice is not None:
            request_io["tool_choice"] = body.tool_choice
        prompt = json.dumps(request_io)
    try:
        tool_calls = resp["choices"][0]["message"].get("tool_calls")
    except (KeyError, IndexError, TypeError, AttributeError):
        tool_calls = None
    if tool_calls:
        completion = raw_completion = json.dumps({
            "role": "assistant",
            "content": raw_completion or completion or None,
            "tool_calls": tool_calls})
    return prompt, completion, raw_completion


async def _finalize(request: Request, resp: dict, *, body: ChatRequest,
                    tenant: str, app_policy: AppPolicyView,
                    app_langfuse: object | None, profile: AppPrivacyProfile,
                    session_id: str | None, s, request_id: str,
                    started_ns: int | None = None) -> None:
    """Post-model-call processing shared by every response shape (non-stream
    and every streaming mode): telemetry export, the first-party console
    snapshot, the durable per-app usage row, and the live decision-stream
    publish. Mutates `resp` in place (session id, display block, deep links)
    and performs the same side effects `chat_completions` used to inline --
    behavior is unchanged, only the call site moved so streaming responses
    can reuse it too. `body`/`profile` are the request body and the app's
    resolved privacy profile the pipeline call itself used, needed here for
    the display-redaction block and egress recognizers."""
    # OpenAI envelope (step 10): every response shape is assembled as a dict in
    # the pipeline WITHOUT these two fields, but OpenAI-compatible clients (the
    # `openai` SDK, LangChain's ChatOpenAI, etc.) require both — the SDK's
    # ChatCompletion model rejects a response missing `created`, which killed
    # every turn before it reached a tool. Setting them here, the single
    # post-model chokepoint every shape passes through (direct, cache hit,
    # multi-step, and the streaming console record), keeps them consistent.
    # `setdefault` so a shape that ever sets its own value wins.
    resp.setdefault("object", "chat.completion")
    resp.setdefault("created", int(time.time()))
    # B17: capture the client's workflow-run correlation id (opaque label).
    # Threaded onto the response dict so both history.record and the drilldown
    # (which read `resp`) carry it with no signature change, and passed to
    # export_trace below for the Langfuse session stamp.
    resp["session_id"] = session_id
    metrics.observe_request(resp["model"], "ok")
    usage = resp.get("usage", {})
    metrics.observe_tokens(resp["model"],
                           usage.get("prompt_tokens", 0),
                           usage.get("completion_tokens", 0))
    # Strip the two never-client-visible keys off `resp` BEFORE the telemetry
    # try-block runs. `raw_completion` is the pre-verify model output (may carry
    # PII step-9 masked from the client reply) and `sanitized_prompt` is the
    # gate's internal redaction — neither may reach the caller. Popping them
    # here, outside the try, makes their removal structural: even if anything in
    # the telemetry block below raised, `resp` (returned to the client) can no
    # longer carry them. They're consumed only inside the try, via these locals.
    sanitized_prompt = resp.pop("sanitized_prompt", "")
    raw_completion = resp.pop("raw_completion", "")   # pre-verify; NEVER client-visible
    try:
        # Eval-capture (docs/archive/eval-logging-backlog.md; current capture_io
        # contract in docs/reference/per-app-config.md): when capture_io is
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
        prompt = (body.messages[-1].content or "") if body.messages else ""
        completion = ""
        try:
            completion = resp["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError, TypeError):
            completion = ""
        if capture_io == "full":
            # `full` stores the WHOLE exchange in the app's own project, not
            # just the last message's text: an agentic client's earlier turns,
            # tool results, offered tools and the assistant's tool_calls are
            # the load-bearing part of its trace.
            prompt, completion, raw_completion = _full_capture_io(
                body, resp, prompt, completion, raw_completion)
        # First-party console view (Phase C Part 1). Mode-appropriate bodies for
        # the admin drilldown ONLY — this is the request_snapshots cache, same
        # residency boundary as serving, NOT a telemetry egress (export_trace
        # is unchanged).
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
        te = export_trace(
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
            routing_policy_version=s.routing_policy_version,
            prompt_version=s.prompt_version,
            started_ns=started_ns, usage=resp.get("usage"))
        # Live deep-link for the UI's "Langfuse trace" button, and the raw hex
        # trace id for an external eval/RLHF consumer (F1 Phase D task 2).
        # Both None when telemetry is off → the UI hides the link. Set before
        # history.record so the inspector's full detail carries them.
        if te.url:
            resp["langfuse_url"] = te.url
        if te.trace_id:
            resp["trace_id"] = te.trace_id
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
        summary = summarize(resp, tenant, tier, provider)
    except Exception:  # snapshot summary must never break serving
        logging.getLogger("serving.obs").exception(
            "snapshot summary failed (swallowed)")
    try:
        # Durable, shared UI cache (B3): every replica writes the redaction-safe
        # summary + full response to `request_snapshots` so any pod's console
        # reads see it, not just the one that served the request. Guarded:
        # the snapshot cache must never break serving.
        if summary is not None:
            async with request.app.state.db.session() as session:
                await store.record_snapshot(
                    session, request_id=resp["id"], application_id=tenant,
                    summary_json=summary.model_dump(), full_json=resp)
                await store.prune_snapshots(session, capacity=store.SNAPSHOT_CAPACITY)
                await session.commit()
    except Exception:  # snapshot cache must never break serving
        logging.getLogger("serving.obs").exception(
            "snapshot write failed (swallowed)")
    try:
        # Durable per-application usage row (spec 2026-07-25). The
        # `request_snapshots` write above is the UI's hot cache (full
        # response + summary, keyed by request_id); this is the separate
        # per-app aggregate that survives restarts and feeds admin
        # dashboards. Guarded: metering must never break serving.
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
            # Cross-replica fan-out (B3): NOTIFY other pods so their locally
            # connected SSE subscribers see this request too. Inert on sqlite.
            await request.app.state.bridge.publish_remote(resp["id"])
    except Exception:  # streaming must never break serving
        logging.getLogger("serving.obs").exception(
            "bus.publish failed (swallowed)")


async def _keepalive_stream(request: Request, *, task: "asyncio.Task[dict]",
                            keepalive_s: float, request_id: str, body: ChatRequest,
                            tenant: str, app_policy: AppPolicyView,
                            app_langfuse: object | None, profile: AppPrivacyProfile,
                            session_id: str | None, s, started: float,
                            started_ns: int | None = None) -> AsyncIterator[str]:
    """SSE for a streamed request whose complete, verified answer is not ready
    within one keepalive interval. Sends a keepalive comment each interval until
    the pipeline task finishes, then the same frames `verified_then_stream`
    sends. A failure after the headers are out cannot change the HTTP status,
    so it goes out as one error frame (same status and detail as the HTTP path)
    followed by [DONE].

    Primary disconnect detection is an explicit `request.is_disconnected()`
    poll once per keepalive interval (the same pattern
    `_await_unless_disconnected` uses for the non-stream path): relying only
    on Starlette to cancel this generator is not safe across ASGI server
    versions. Starlette cancels a streaming response via a concurrent
    disconnect-listener task on ASGI spec_version < 2.4, or by letting
    `send()` raise OSError on spec_version >= 2.4. uvicorn 0.51.0 advertises
    2.3 today, so the listener path runs -- but uvicorn's h11 `send()`
    silently returns instead of raising once the connection is already
    closed (h11_impl.py:464), so a future uvicorn that advertises >= 2.4
    would leave this generator with no cancellation signal at all: it would
    stream into the void until the task finished and get recorded `ok`.
    Polling `is_disconnected()` directly doesn't depend on any of that.

    Wrapped in try/finally as a second line of defence (e.g. if Starlette's
    own cancellation lands first): the `finally` retrieves the task's outcome
    on every exit path -- normal completion, an error frame, or a disconnect
    -- so a still-running task is always cancelled and awaited (never leaving
    a dangling "Task exception was never retrieved" task), and a disconnect
    that happened before the answer was delivered is metered as `cancelled`,
    never `ok`. `cancelled_recorded` keeps the two paths from double-recording
    the same disconnect."""
    cancelled_recorded = False
    try:
        yield SSE_KEEPALIVE  # one immediately: the route already waited one
                             # full interval before opening this stream, so
                             # waiting a second interval here would delay the
                             # first byte to ~2x keepalive_s
        while True:
            done, _ = await asyncio.wait({task}, timeout=keepalive_s)
            if done:
                break
            if await request.is_disconnected():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                await _record_cancelled(request, tenant=tenant, request_id=request_id,
                                        started=started)
                cancelled_recorded = True
                return  # client is gone -- no keepalive/answer/error frame follows
            yield SSE_KEEPALIVE
        try:
            resp = task.result()
        except Exception as e:
            mapped = _error_status_detail(e)
            if mapped is None:
                logging.getLogger("serving.api").exception(
                    "unhandled error for %s after stream opened", request_id)
                metrics.observe_request("-", "error")
                mapped = (500, "internal error")
            else:
                metrics.observe_request("-", "rejected")
            yield sse_error_frame(*mapped)
            yield SSE_DONE
            return
        await _finalize(request, resp, body=body, tenant=tenant, app_policy=app_policy,
                        app_langfuse=app_langfuse, profile=profile,
                        session_id=session_id, s=s, request_id=request_id,
                        started_ns=started_ns)
        message = resp["choices"][0]["message"]
        async for frame in chunk_text(request_id, resp["model"], message["content"] or "",
                                      tool_calls=message.get("tool_calls")):
            yield frame
    finally:
        # Snapshot before cancel: task.cancel() doesn't flip done() right
        # away, so this is the one true read of "did the backend finish
        # before the client left". Not done -> the client left while the
        # pipeline was still working: cancel it and meter `cancelled`. Done
        # (success, error frame already sent, or a disconnect mid-answer-send
        # after `_finalize` already recorded `ok`) -> nothing to cancel and
        # nothing to re-meter.
        was_done = task.done()
        if not was_done:
            task.cancel()
        # Retrieve the outcome on every exit path -- a task that finished
        # (successfully or with an exception) between the last check and
        # generator close must still have its result/exception consumed, or
        # asyncio logs "Task exception was never retrieved".
        await asyncio.gather(task, return_exceptions=True)
        if not was_done and not cancelled_recorded:
            await _record_cancelled(request, tenant=tenant, request_id=request_id,
                                    started=started)


def _stream_unverified_response(
        request: Request, *, request_id: str,
        deltas: AsyncIterator[str], get_meta: Callable[[], dict],
        body: ChatRequest, tenant: str, app_policy: AppPolicyView,
        app_langfuse: object | None, profile: AppPrivacyProfile,
        session_id: str | None, s,
        started_ns: int | None = None) -> StreamingResponse:
    """Live token-forwarding SSE for `stream_unverified` (real backend
    streaming). Emits an OpenAI-style role frame, forwards each `deltas` token
    as a `content` frame, then (after the stream drains) a `finish_reason`
    frame + `[DONE]`. `_finalize` runs in a `finally` so the DecisionRecord,
    usage row, and bus publish are recorded for EVERY streamed request --
    including a client that disconnects mid-stream (Global Constraint 2).

    `created` is computed once and held constant across every frame (Task 4),
    and `model` uses the app's requested model string as a stable per-frame
    label; the resolved serving model lands in the durable usage row via
    `_finalize` (built from the pipeline's final metadata)."""
    created = int(time.time())
    model = body.model  # stable frame label; resolved model is in the usage row

    async def _gen() -> AsyncIterator[str]:
        meta: dict | None = None
        try:
            yield sse_frame(openai_chunk(request_id, model, role="assistant",
                                         created=created))
            async for delta in deltas:
                yield sse_frame(openai_chunk(request_id, model, delta=delta,
                                             created=created))
            meta = get_meta()  # valid only now that `deltas` is fully drained
            yield sse_frame(openai_chunk(request_id, model, finish_reason="stop",
                                         created=created))
            yield SSE_DONE
        finally:
            # Best-effort finalize on EVERY exit -- normal completion and a
            # mid-stream client disconnect alike. Accounting must not be lost.
            await _finalize_stream(
                request, request_id=request_id, deltas=deltas,
                get_meta=get_meta, meta=meta, body=body, tenant=tenant,
                app_policy=app_policy, app_langfuse=app_langfuse,
                profile=profile, session_id=session_id, s=s,
                started_ns=started_ns)

    return sse_response(_gen())


def _incremental_verify_response(
        request: Request, *, request_id: str,
        deltas: AsyncIterator[str], get_meta: Callable[[], dict],
        body: ChatRequest, tenant: str, app_policy: AppPolicyView,
        app_langfuse: object | None, profile: AppPrivacyProfile,
        session_id: str | None, s,
        started_ns: int | None = None) -> StreamingResponse:
    """Live token streaming for `incremental_verify` (Task 7). Real backend
    deltas are piped through a hold-back `StreamingVerifier` before egress, so
    an entity split across chunk boundaries never leaks its raw prefix; the
    verifier's `flush()` releases the held-back tail as a final content frame.

    The verifier is built on the request's EFFECTIVE detector + ctx (the exact
    per-app detector the gate used, surfaced on `get_meta` by `run_stream`) so
    egress redaction matches intake. `_finalize` runs in a `finally` for EVERY
    exit -- normal completion and a mid-stream disconnect alike -- and records
    step 9 as `performed=True, reason="incremental"` (a deliberately weaker
    guarantee than the complete-response verdict; see streaming_verify.py).
    The client-visible (redacted) text is threaded into `_finalize` so the
    console/usage record shows what actually left, not the raw model output."""
    created = int(time.time())
    model = body.model  # stable frame label; resolved model is in the usage row
    verifier = StreamingVerifier(get_meta.detector, get_meta.ctx)  # type: ignore[attr-defined]

    async def _gen() -> AsyncIterator[str]:
        meta: dict | None = None
        released: list[str] = []
        try:
            yield sse_frame(openai_chunk(request_id, model, role="assistant",
                                         created=created))
            async for delta in deltas:
                out = verifier.feed(delta)
                if out:
                    released.append(out)
                    yield sse_frame(openai_chunk(request_id, model, delta=out,
                                                 created=created))
            tail = verifier.flush()  # release the held-back window
            if tail:
                released.append(tail)
                yield sse_frame(openai_chunk(request_id, model, delta=tail,
                                             created=created))
            meta = get_meta()  # valid only now that `deltas` is fully drained
            yield sse_frame(openai_chunk(request_id, model, finish_reason="stop",
                                         created=created))
            yield SSE_DONE
        finally:
            # Best-effort finalize on EVERY exit. `client_text` is exactly what
            # the client received (redacted); on a mid-stream disconnect it is
            # the partial released prefix, which is honest.
            await _finalize_stream(
                request, request_id=request_id, deltas=deltas,
                get_meta=get_meta, meta=meta, body=body, tenant=tenant,
                app_policy=app_policy, app_langfuse=app_langfuse,
                profile=profile, session_id=session_id, s=s,
                verify_performed=True, verify_reason="incremental",
                client_text="".join(released),
                started_ns=started_ns)

    return sse_response(_gen())


async def _finalize_stream(
        request: Request, *, request_id: str, deltas: AsyncIterator[str],
        get_meta: Callable[[], dict], meta: dict | None,
        body: ChatRequest, tenant: str, app_policy: AppPolicyView,
        app_langfuse: object | None, profile: AppPrivacyProfile,
        session_id: str | None, s,
        verify_performed: bool = False,
        verify_reason: str = "waived: stream_unverified",
        client_text: str | None = None,
        started_ns: int | None = None) -> None:
    """Assemble the `resp` dict from the pipeline's final streaming metadata and
    run the shared `_finalize`. Guarded end to end: a failure here must never
    surface to the (already-streaming) client. Shared by both real-streaming
    modes; the caller supplies how step 9 is recorded:

      * `stream_unverified` (Task 6): defaults -- `performed=False`,
        `reason="waived: stream_unverified"`; the client received the RAW deltas,
        so `client_text=None` and the recorded completion is `raw_completion`.
      * `incremental_verify` (Task 7): `performed=True`, `reason="incremental"`;
        the client received the hold-back-REDACTED text, passed in as
        `client_text` so the recorded/displayed completion matches what actually
        left (the raw model output stays in `raw_completion` for eval capture,
        exactly as on the non-stream path).

    On a mid-stream disconnect `meta` is None (the generator never reached
    `get_meta`); we best-effort drain the remaining backend deltas so the final
    accounting (cost, tokens, decision) is recovered rather than silently lost.
    run_stream only returns normally when the request is NOT onprem_only, NOT
    require_verification, and NOT multi_step, so recording step 9 as
    waived/incremental (rather than a complete-response verdict) is sound."""
    log = logging.getLogger("serving.obs")
    try:
        if meta is None:
            try:
                async for _delta in deltas:
                    pass
                meta = get_meta()
            except Exception:
                log.exception(
                    "streaming: drain-on-disconnect failed (swallowed)")
                return
        resp = dict(meta)
        # The completion recorded/displayed == what the client actually
        # received: the hold-back-redacted text for incremental_verify
        # (`client_text`), else the raw streamed text (stream_unverified).
        text = client_text if client_text is not None else (
            meta.get("raw_completion") or "")
        resp["choices"] = [{"index": 0,
                            "message": {"role": "assistant", "content": text},
                            "finish_reason": "stop"}]
        # Make the trace's VerificationRecord say WHY (Global Constraint 2).
        # run_stream stamps a performed=False/passed=True "waived" record; name
        # the reason + performed flag for this specific mode so the console and
        # Langfuse trace read "waived: stream_unverified" vs "incremental".
        for rec in resp.get("trace", []):
            if isinstance(rec, dict) and rec.get("step") == "verify":
                rec["reason"] = verify_reason
                rec["performed"] = verify_performed
        await _finalize(request, resp, body=body, tenant=tenant,
                        app_policy=app_policy, app_langfuse=app_langfuse,
                        profile=profile, session_id=session_id, s=s,
                        request_id=request_id, started_ns=started_ns)
    except Exception:  # finalize must never break the (already sent) stream
        log.exception("streaming finalize failed (swallowed)")


class FeedbackIn(BaseModel):
    """Body for POST /v1/feedback. `value` is numeric for quality-style scores
    or a string for categorical scores (e.g. failure_type); Langfuse decides
    dataType by which of those it is (see `app.obs.scores.post_score`)."""
    trace_id: str
    score_name: str
    value: float | str
    comment: str | None = None
    corrected_output: str | None = None


@router.post("/v1/feedback")
async def feedback(body: FeedbackIn, request: Request) -> dict:
    """App-token-authenticated eval/feedback intake (F1 Phase D / B16). Posts
    straight to the calling app's OWN residency-local Langfuse project via
    REST — never the shared project, and never anywhere at all when the app
    has no provisioned project (fail-closed; `post_score` handles that case
    and returns False, which becomes `{"stored": false}` here, HTTP 200 —
    a missing project is not the caller's fault and must not surface as an
    error)."""
    auth = await _authenticate(request)
    if auth is None:
        raise HTTPException(401, "invalid or missing API token")
    tenant, app_policy, app_langfuse = auth
    if body.score_name not in SCORE_NAMES:
        raise HTTPException(422, f"unknown score_name: {body.score_name!r}")
    await ensure_score_config(app_langfuse, body.score_name)  # best-effort, never raises
    stored = await post_score(app_langfuse, body.trace_id, body.score_name,
                              body.value, body.comment)
    if body.corrected_output:
        await ensure_score_config(app_langfuse, "corrected_output")
        await post_score(app_langfuse, body.trace_id, "corrected_output", 1,
                         body.corrected_output)
    return {"stored": stored}
