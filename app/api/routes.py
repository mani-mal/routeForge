import asyncio
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import time
import uuid
from collections.abc import AsyncIterator, Callable

import anyio
from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from app.api.history import summarize
from app.api.openai_errors import (
    CODE_BACKEND_UNAVAILABLE, CODE_CONTEXT_LENGTH_EXCEEDED, CODE_INSUFFICIENT_QUOTA,
    CODE_INTERNAL_ERROR, CODE_INVALID_API_KEY, CODE_INVALID_VALUE, CODE_MODEL_NOT_FOUND,
    CODE_NO_ELIGIBLE_MODEL, CODE_POLICY_UNAVAILABLE, CODE_RATE_LIMIT_EXCEEDED,
    CODE_RESPONSE_FAILED_VERIFICATION, CODE_STREAMING_NOT_SUPPORTED, TYPE_INSUFFICIENT_QUOTA,
    ApiError,
)
from app.api.streaming import (
    SSE_DONE, SSE_KEEPALIVE, chunk_text, openai_chunk, sse_error_frame, sse_frame,
    sse_response,
)
from app.apps import store
from app.apps.langfuse_provision import load_app_langfuse
from app.apps.outcomes import (
    build_failure_outcome, build_ok_outcome, feedback_values, parse_workload,
)
from app.apps.policy import AppPolicyView, day_spend_usd, policy_view_from
from app.apps.privacy import AppPrivacyProfile
from app.errors import (
    BackendUnavailable, ContextWindowExceeded, NoEligibleCandidate, PolicyGateError,
)
from app.models import ChatRequest, ExecutionPolicyContext
from app.obs import metrics
from app.obs.export import export_trace, resolve_capture_io
from app.obs.redact import redacted_copy
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


_session_id_key_cache: bytes | None = None
_session_id_key_logged = False


def _reset_session_id_key_cache() -> None:
    """Test-only: clear the cached process-random key (and the "logged
    already" flag) so tests can exercise both the SP_SESSION_ID_KEY and the
    random-key path cleanly, and re-resolve SP_SESSION_ID_KEY after changing
    it via monkeypatch."""
    global _session_id_key_cache, _session_id_key_logged
    _session_id_key_cache = None
    _session_id_key_logged = False


def _session_id_key() -> bytes:
    """The HMAC key `_derive_session_id` signs with. `SP_SESSION_ID_KEY` (any
    non-empty string, UTF-8 encoded) when set -- the same value on every
    replica keeps derived ids stable across the deployment and across
    restarts. Otherwise a random 32-byte key generated once per process: ids
    stay stable for the life of this process but reset on restart and differ
    across replicas, which the one-time INFO log below calls out. The key
    itself is never logged."""
    global _session_id_key_cache, _session_id_key_logged
    env = os.environ.get("SP_SESSION_ID_KEY", "")
    if env:
        return env.encode()
    if _session_id_key_cache is None:
        _session_id_key_cache = secrets.token_bytes(32)
    if not _session_id_key_logged:
        logging.getLogger("serving.api").info(
            "SP_SESSION_ID_KEY is not set: derived session ids (auto-...) are "
            "signed with a random per-process key. They reset on restart and "
            "differ across replicas. Set SP_SESSION_ID_KEY to the same value "
            "on every replica for stable session grouping across restarts.")
        _session_id_key_logged = True
    return _session_id_key_cache


def _derive_session_id(body: ChatRequest, tenant: str) -> str | None:
    """A stable session id for a conversation whose client sent none: a keyed
    HMAC-SHA256 of the app id plus the conversation's opening turns (first
    system message, first user message). Later turns append history but keep
    those opening turns, so every request in one conversation shares the id.

    Keyed (not a raw unkeyed hash), per SP_SESSION_ID_KEY / `_session_id_key`
    above: this id is exported to Langfuse as `langfuse.session.id`, and an
    unkeyed hash over raw pre-gate text would let a telemetry viewer who sees
    the redacted prompt brute-force a low-entropy redacted value (e.g. an
    SSN) against it."""
    first_user = next((m.content for m in body.messages
                       if m.role == "user" and m.content), None)
    if not first_user:
        return None
    system = next((m.content or "" for m in body.messages if m.role == "system"), "")
    msg = f"{tenant}\x00{system}\x00{first_user}".encode()
    digest = hmac.new(_session_id_key(), msg, hashlib.sha256).hexdigest()[:24]
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
    the OpenAI contract clients rely on. Each entry also carries `max_model_len`
    (vLLM's own field name, the one clients already read) when the model's real
    context window is known (B50) -- omitted entirely, never null or 0, when
    unknown."""
    auth = await _authenticate(request)
    if auth is None:
        raise ApiError(401, "invalid or missing API token", code=CODE_INVALID_API_KEY)
    tenant, app_policy, _ = auth
    registry = request.app.state.registry
    await registry.ensure_loaded(request.app.state.db)
    models = requestable_models(request.app.state.settings, registry, tenant, app_policy)
    data = []
    for c in models:
        entry = {"id": c.name, "object": "model", "created": 0,
                 "owned_by": c.provider}
        # vLLM's field name: the one clients already read. Omitted entirely
        # when unknown -- a guessed context window overflows the request it
        # was meant to size (B50).
        if c.context_window:
            entry["max_model_len"] = c.context_window
        data.append(entry)
    return {"object": "list", "data": data}


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
        raise ApiError(400, _STREAM_REJECT, code=CODE_STREAMING_NOT_SUPPORTED)
    return None if mode in (None, STREAM_MODES[0]) else mode


def _is_pinned_model_rejection(exc: BaseException) -> bool:
    """A `PipelineRejected` for a client-pinned `model` the app may not use
    (`Pipeline.run`: "requested model ..."). Every other `PipelineRejected` on
    the serving path is a step-9 verification failure. One test, shared by the
    HTTP error mapping and the outcome recorder, so they can never disagree."""
    return isinstance(exc, PipelineRejected) and str(exc).startswith("requested model")


def _error_status_detail(exc: BaseException) -> tuple[int, str, str | None] | None:
    """The (status, detail, openai_code) the HTTP path returns for a serving
    exception. One mapping, used by the plain HTTP path and by the keepalive
    stream's error frame, so the two can never drift -- including the code the
    client branches on (B49)."""
    if isinstance(exc, PipelineRejected):
        message = str(exc)
        rejected_model = _is_pinned_model_rejection(exc)
        prefix = ("model request rejected: " if rejected_model
                  else "response failed verification: ")
        code = (CODE_MODEL_NOT_FOUND if rejected_model
                else CODE_RESPONSE_FAILED_VERIFICATION)
        return 422, f"{prefix}{message}", code
    if isinstance(exc, ContextWindowExceeded):
        return 400, (f"request exceeds the model's context window: {exc} -- "
                     "shorten the conversation (compact or summarise the history) "
                     "or lower max_tokens"), CODE_CONTEXT_LENGTH_EXCEEDED
    if isinstance(exc, NoEligibleCandidate):
        return (422, f"no model is available for this request's policy: {exc}",
                CODE_NO_ELIGIBLE_MODEL)
    if isinstance(exc, BackendUnavailable):
        return (502, f"no backend could serve this request: {exc}",
                CODE_BACKEND_UNAVAILABLE)
    if isinstance(exc, PolicyGateError):
        return 503, "policy evaluation unavailable", CODE_POLICY_UNAVAILABLE
    return None


class ClientDisconnected(Exception):
    """The client went away before a complete answer was ready."""


async def _wait_or_cancel_on_shutdown(task: "asyncio.Task", timeout: float):
    """`asyncio.wait` for `task`, but if THIS coroutine itself is cancelled
    before the wait completes (e.g. the request handler is torn down on
    server shutdown, not a client disconnect), cancel `task` too instead of
    re-raising and leaving it orphaned -- still running, unawaited, with no
    caller left to record or await its outcome. Re-raises either way; the
    caller's own cleanup (or lack of one, on shutdown) is unaffected."""
    try:
        return await asyncio.wait({task}, timeout=timeout)
    except asyncio.CancelledError:
        if not task.done():
            task.cancel()
        raise


async def _await_unless_disconnected(task: "asyncio.Task", is_disconnected,
                                     poll_s: float):
    """Await `task`, polling the client connection every `poll_s`. A client that
    disconnected gets its backend work cancelled; cancelling the task closes the
    in-flight model call, which OpenAI-compatible servers treat as an abort.
    Only for paths that wait for a complete answer; real token streaming keeps
    its drain-on-disconnect accounting."""
    while True:
        done, _ = await _wait_or_cancel_on_shutdown(task, poll_s)
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


async def _record_failed(request: Request, *, tenant: str, request_id: str,
                         started: float, model: str,
                         ctx: ExecutionPolicyContext | None = None,
                         s=None, internals: dict | None = None,
                         attempts: tuple[str, ...] = (),
                         workload: str | None = None,
                         workload_source: str = "none") -> None:
    """Meter a request that failed on the BACKEND (spec §4, B38): one usage row
    with `status="error"`, so an outage shows up in the console instead of
    vanishing from every usage figure.

    Written for exactly two cases: a request that ends in `BackendUnavailable`
    (the non-streamed 502, and the keepalive stream's error frame), and a real
    token stream whose backend raised before or during the stream. Nothing
    else earns one: a context-window overflow is the client's request (400),
    `NoEligibleCandidate` and policy rejections are policy outcomes, and a
    client disconnect is already metered `cancelled`.

    `model` is the last candidate attempted (or the streamed candidate); its
    tier/provider come from the registry. Tokens and cost are zero -- nothing
    was served -- and every spend/token/latency/budget aggregate filters on
    `status == "ok"`, so this row moves none of them. `metrics` is untouched:
    the caller's existing `observe_request` already counted the failure.

    It also writes the request's `backend_failed` routing outcome (P2b-2),
    naming `attempts` -- the candidates the gateway tried, in order."""
    try:
        cand = next((c for c in request.app.state.registry.effective_pool()
                     if c.name == model), None)
        async with request.app.state.db.session() as session:
            await store.record_usage(
                session, application_id=tenant, request_id=request_id,
                model=model,
                provider=cand.provider if cand is not None else "-",
                tier=cand.tier if cand is not None else "-",
                prompt_tokens=0, completion_tokens=0, total_tokens=0,
                cost_usd=0.0, baseline_usd=0.0,
                latency_ms=(time.monotonic() - started) * 1000,
                sensitivity=ctx.sensitivity if ctx is not None else "-",
                onprem_only=bool(ctx.onprem_only) if ctx is not None else False,
                status="error")
            await session.commit()
    except Exception:
        logging.getLogger("serving.obs").exception(
            "error-usage record failed (swallowed)")
    await _record_failure_outcome(
        request, "backend_failed", tenant=tenant, request_id=request_id,
        started=started, s=s, internals=internals, attempts=attempts,
        workload=workload, workload_source=workload_source)


def _failed_model(exc: BaseException) -> str:
    """The model an `error` row names for a `BackendUnavailable`: the last
    candidate the gateway attempted, or "-" when it recorded none."""
    attempted = getattr(exc, "attempted_models", ())
    return attempted[-1] if attempted else "-"


async def _record_outcome(request: Request, row: dict) -> None:
    """Write one `routing_outcomes` row (P2b-2) in its own session. Best
    effort, like every other record on the serving path: a failed write is
    logged and swallowed, so it can never fail or alter the response, and it
    never touches the usage row. It is inline, like the usage row, so it does
    add one DB commit to the request. `outcomes.enabled: false` writes
    nothing."""
    if not request.app.state.settings.serving.outcomes.enabled:
        return
    try:
        async with request.app.state.db.session() as session:
            await store.write_outcome(session, row)
            await session.commit()
    except Exception:
        logging.getLogger("serving.obs").warning(
            "outcome record failed request=%s", row.get("request_id"), exc_info=True)


def _registry_version(request: Request) -> str | None:
    """The live `ModelRegistry.registry_version()`, or None if it cannot be
    read. A failure row needs it (no response carries it), and reading it
    must never break the error path it is recorded from."""
    try:
        return request.app.state.registry.registry_version()
    except Exception:
        return None


def _outcome_failure_status(exc: BaseException) -> str | None:
    """The routing-outcome status for a serving exception that is NOT a
    backend failure (`_record_failed` writes those), or None when the
    exception is not a routing outcome at all: a pinned-model rejection, a
    policy-gate error, anything unmapped."""
    if isinstance(exc, ContextWindowExceeded):
        return "context_overflow"
    if isinstance(exc, NoEligibleCandidate):
        return "no_eligible"
    if isinstance(exc, PipelineRejected) and not _is_pinned_model_rejection(exc):
        return "verify_failed"
    return None


async def _record_failure_outcome(request: Request, status: str, *, tenant: str,
                                  request_id: str, started: float, s=None,
                                  internals: dict | None = None,
                                  attempts: tuple[str, ...] = (),
                                  rejected: tuple = (),
                                  workload: str | None = None,
                                  workload_source: str = "none") -> None:
    """Build and write a failed request's outcome row. Content-free: the
    gate's `outcome_ctx` (ids, enums, entity TYPES), the candidate names
    tried, and the `{name, code}` of each policy rejection -- never the
    exception's message."""
    try:
        s = s if s is not None else request.app.state.settings
        row = build_failure_outcome(
            status=status, request_id=request_id, application_id=tenant,
            outcome_ctx=(internals or {}).get("outcome_ctx") or {},
            routing_policy_version=s.routing_policy_version,
            endpoint_registry_version=_registry_version(request),
            workload=workload, workload_source=workload_source,
            attempts=tuple(attempts), rejected=tuple(rejected),
            latency_ms=(time.monotonic() - started) * 1000)
    except Exception:
        logging.getLogger("serving.obs").warning(
            "outcome record failed request=%s", request_id, exc_info=True)
        return
    await _record_outcome(request, row)


async def _record_exception_outcome(request: Request, exc: BaseException, *,
                                    tenant: str, request_id: str, started: float,
                                    s, internals: dict | None,
                                    workload: str | None,
                                    workload_source: str) -> None:
    """The outcome row for a mapped serving exception, shared by the plain
    HTTP path and the keepalive stream's error frame. A `BackendUnavailable`
    goes through `_record_failed` (usage row + outcome); the three routing
    outcomes in `_outcome_failure_status` get an outcome row only; anything
    else gets nothing."""
    if isinstance(exc, BackendUnavailable):
        await _record_failed(request, tenant=tenant, request_id=request_id,
                             started=started, model=_failed_model(exc), s=s,
                             internals=internals,
                             attempts=tuple(getattr(exc, "attempted_models", ())),
                             workload=workload, workload_source=workload_source)
        return
    status = _outcome_failure_status(exc)
    if status is None:
        return
    await _record_failure_outcome(
        request, status, tenant=tenant, request_id=request_id, started=started,
        s=s, internals=internals,
        # `NoEligibleCandidate.rejected` is `(name, code)` tuples; the builder
        # takes `{name, code}` mappings (the router trace's shape).
        rejected=tuple({"name": name, "code": code}
                       for name, code in getattr(exc, "rejected", ())),
        workload=workload, workload_source=workload_source)


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
            raise ApiError(400, _STREAM_REJECT, code=CODE_STREAMING_NOT_SUPPORTED)
        raise ApiError(401, "invalid or missing API token", code=CODE_INVALID_API_KEY)
    tenant, app_policy, app_langfuse = auth
    stream_mode = _resolve_stream_mode(body, app_policy, s)
    # P2b-2: the client's optional workload tag, for the routing outcome row
    # only. The header wins over `metadata.workload`; an invalid tag is
    # recorded as such and the request is served regardless. `metadata` is
    # never forwarded upstream (it is not in `FORWARDED_PARAMS`).
    workload, workload_source = parse_workload(
        request.headers.get("x-routeforge-workload"),
        (body.model_extra or {}).get("metadata"))
    outcome_kw = {"workload": workload, "workload_source": workload_source}

    # Per-application quotas (fail closed). Rate limit first (cheap, in-memory),
    # then the daily spend cap (one indexed sum). Both reject with 429; the
    # budget rejection is recorded as a usage row so it shows in the dashboard.
    if not request.app.state.rate_limiter.allow(tenant, app_policy.max_rpm):
        metrics.observe_request("-", "rate_limited")
        raise ApiError(429, "rate limit exceeded for this application",
                       code=CODE_RATE_LIMIT_EXCEEDED)
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
            raise ApiError(429, "daily budget exceeded for this application",
                           code=CODE_INSUFFICIENT_QUOTA, type=TYPE_INSUFFICIENT_QUOTA)

    # Read the opaque workflow-correlation id once, up front: the streaming
    # branch below returns before the old (post-pipeline) read site, and every
    # response shape's _finalize needs it. `client_session_id` is the raw
    # client-supplied header value ONLY -- it is the memory PARTITION key
    # (threaded into every `pipeline.run`/`run_stream` call below), so memory
    # semantics must never change based on derivation. `session_id` may be
    # server-derived from the app id + opening turns when the client sent none
    # (unless the operator has switched that off): it is for TRACE/CONSOLE
    # grouping only (_finalize/_finalize_stream/_keepalive_stream/
    # _stream_unverified_response/_incremental_verify_response), so an agent
    # conversation with no header still groups into one session there as its
    # history grows -- without touching what memory partition it reads/writes.
    client_session_id = _read_session_id(request)
    session_id = client_session_id
    if session_id is None and s.serving.derive_session_id:
        session_id = _derive_session_id(body, tenant)

    # The pipeline's out-parameter: the values `_finalize`'s two I/O surfaces
    # need and NOTHING may serialize -- the gate's raw flattened prompt and the
    # request's own detector (`Pipeline._publish_telemetry_views`). Kept off the
    # response dict deliberately; `resp` is serialized to the client, to
    # `request_snapshots.full_json` and to the console, and raw text on it is
    # only ever one forgotten `pop` from leaking. Whichever pipeline call below
    # actually serves the request fills it before any `_finalize` runs.
    internals: dict = {}
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
            # the gate and the runtime floor before returning, so a floored
            # request (onprem_only / require_verification) raises
            # StreamFallback here -- before any token is emitted -- and we
            # transparently fall through to the verified_then_stream path
            # below. The floor is enforced ONLY in the pipeline (Global
            # Constraint 1); the route never re-implements it.
            try:
                deltas, get_meta = await request.app.state.pipeline.run_stream(
                    request_id, tenant, body, app_policy=app_policy, profile=profile,
                    session_id=client_session_id, internals=internals)
            except StreamFallback:
                pass  # fall through: run the FULL verified pipeline, then chunk it
            else:
                if stream_mode == "stream_unverified":
                    return _stream_unverified_response(
                        request, request_id=request_id, deltas=deltas,
                        get_meta=get_meta, body=body, tenant=tenant,
                        app_policy=app_policy, app_langfuse=app_langfuse,
                        profile=profile, session_id=session_id, s=s,
                        internals=internals, started=started, started_ns=started_ns,
                        **outcome_kw)
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
                    internals=internals, started=started, started_ns=started_ns,
                    **outcome_kw)
        if stream_mode is not None:
            task = asyncio.create_task(request.app.state.pipeline.run(
                request_id, tenant, body, app_policy=app_policy, profile=profile,
                session_id=client_session_id, internals=internals))
            keepalive = s.serving.stream_keepalive_s
            done, _ = await _wait_or_cancel_on_shutdown(task, keepalive)
            if not done:
                return sse_response(_keepalive_stream(
                    request, task=task, keepalive_s=keepalive, request_id=request_id,
                    body=body, tenant=tenant, app_policy=app_policy,
                    app_langfuse=app_langfuse, profile=profile,
                    session_id=session_id, s=s, started=started,
                    internals=internals,
                    started_ns=started_ns, **outcome_kw))
            resp = task.result()   # raises -> the except clauses below map it
        else:
            task = asyncio.create_task(request.app.state.pipeline.run(
                request_id, tenant, body, app_policy=app_policy, profile=profile,
                session_id=client_session_id, internals=internals))
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
            logging.getLogger("serving.api").exception(
                "unhandled error for %s", request_id)
            metrics.observe_request("-", "error")
            raise ApiError(500, "internal error", code=CODE_INTERNAL_ERROR)
        if mapped[0] >= 500:
            logging.getLogger("serving.api").warning(
                "request %s failed with %d: %s", request_id, mapped[0], mapped[1])
        metrics.observe_request("-", "rejected")
        await _record_exception_outcome(
            request, e, tenant=tenant, request_id=request_id, started=started,
            s=s, internals=internals, **outcome_kw)
        raise ApiError(mapped[0], mapped[1], code=mapped[2])
    await _finalize(request, resp, body=body, tenant=tenant, app_policy=app_policy,
                    app_langfuse=app_langfuse, profile=profile,
                    session_id=session_id, s=s, request_id=request_id,
                    internals=internals, started_ns=started_ns, **outcome_kw)
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
                     raw_completion: str, raw_tool_calls: list[dict] | None = None
                     ) -> tuple[str, str, str, str]:
    """Trace input/output for `capture_io == "full"` (the app's OWN project only;
    the caller has already applied the full-eligibility clamp).

    Input: the whole request -- every message with its tool structure, plus
    `tools` / `tool_choice` -- as JSON, whenever there is more than one message
    or tools are offered. A single plain message stays plain text.

    Output: when the model called tools, the assistant message as JSON with its
    `tool_calls` (content is the pre-verify raw text, or null). Returns (prompt,
    completion, raw_completion, display_completion).

    `completion`/`raw_completion` are for `export_trace` ONLY -- the app's own
    Langfuse project, per CLAUDE.md rule 2's `capture_io: full` exception --
    and carry the model's RAW (pre-egress-redaction) tool_calls via
    `raw_tool_calls` (Task 2), preferred over `resp`'s `message.tool_calls`
    (which by this point has already been egress-redacted). Falls back to the
    (redacted) message tool_calls when no raw copy was produced (e.g. a
    response shape that never carries `raw_tool_calls`), so this stays a
    no-op there rather than dropping output entirely.

    `display_completion` is DIFFERENT on purpose (fix round 1): it is built
    ONLY from the CLIENT-FACING (already egress-redacted) message, never from
    `raw_completion`/`raw_tool_calls`. It feeds `resp["display"]["output"]`
    (`_finalize`, below), which is not a telemetry egress but IS part of the
    dict returned to the calling client on the non-stream path, AND is what
    the admin console's `/api/requests/{id}` drilldown reads back out of
    `request_snapshots.full_json=resp` -- neither of which is the app's OWN
    Langfuse project. `display` must never show raw, even for a full-eligible
    app (see tests/test_raw_completion_capture.py's contract)."""
    if len(body.messages) > 1 or body.tools:
        request_io: dict = {"messages": [m.model_dump(exclude_none=True)
                                         for m in body.messages]}
        if body.tools:
            request_io["tools"] = body.tools
        if body.tool_choice is not None:
            request_io["tool_choice"] = body.tool_choice
        prompt = json.dumps(request_io)
    try:
        message = resp["choices"][0]["message"]
    except (KeyError, IndexError, TypeError):
        message = {}
    redacted_tool_calls = message.get("tool_calls") if isinstance(message, dict) else None
    tool_calls = raw_tool_calls or redacted_tool_calls
    display_completion = completion
    if tool_calls:
        completion = raw_completion = json.dumps({
            "role": "assistant",
            "content": raw_completion or completion or None,
            "tool_calls": tool_calls})
    if redacted_tool_calls:
        # Built from ONLY the client-facing message -- never `raw_completion`/
        # `raw_tool_calls` -- so a full-capture app's raw arguments can reach
        # its own Langfuse project (above) without ever reaching `display`.
        display_completion = json.dumps({
            "role": "assistant",
            "content": message.get("content"),
            "tool_calls": redacted_tool_calls})
    return prompt, completion, raw_completion, display_completion


async def _finalize(request: Request, resp: dict, *, body: ChatRequest,
                    tenant: str, app_policy: AppPolicyView,
                    app_langfuse: object | None, profile: AppPrivacyProfile,
                    session_id: str | None, s, request_id: str,
                    internals: dict | None = None,
                    started_ns: int | None = None,
                    workload: str | None = None,
                    workload_source: str = "none") -> None:
    """Post-model-call processing shared by every response shape (non-stream
    and every streaming mode): telemetry export, the first-party console
    snapshot, the durable per-app usage row, and the live decision-stream
    publish. Mutates `resp` in place (session id, display block, deep links)
    and performs the same side effects `chat_completions` used to inline --
    behavior is unchanged, only the call site moved so streaming responses
    can reuse it too. `body`/`profile` are the request body and the app's
    resolved privacy profile the pipeline call itself used, needed here for
    the display-redaction block and egress recognizers.

    `internals` is the dict `chat_completions` handed the pipeline call
    (`Pipeline.run`/`run_stream`), carrying the gate's raw flattened prompt and
    the request's own detector. Both I/O surfaces here -- the Langfuse trace
    and `resp["display"]` -- build their own redacted copy from those, and
    neither value is ever on `resp`."""
    # OpenAI envelope (step 10): every response shape is assembled as a dict in
    # the pipeline WITHOUT these two fields, but OpenAI-compatible clients (the
    # `openai` SDK, LangChain's ChatOpenAI, etc.) require both — the SDK's
    # ChatCompletion model rejects a response missing `created`, which killed
    # every turn before it reached a tool. Setting them here, the single
    # post-model chokepoint every shape passes through (direct, cache hit,
    # and the streaming console record), keeps them consistent.
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
    # `sanitized_prompt` is the MODEL's view of the request. No I/O surface
    # reads it any more (that reuse WAS CE-1); it is popped purely so it cannot
    # reach the client, a snapshot or the usage row.
    resp.pop("sanitized_prompt", None)
    raw_completion = resp.pop("raw_completion", "")   # pre-verify; NEVER client-visible
    # Pre-egress-redaction tool_calls (Task 2) -- same never-client-visible
    # contract as `raw_completion` immediately above, popped at the same
    # chokepoint so it can never leak through any response shape.
    raw_tool_calls = resp.pop("raw_tool_calls", None)
    # The gate's RAW flattened scan text and the request's own detector, from
    # the pipeline's out-parameter (`Pipeline._publish_telemetry_views`) rather
    # than from `resp` -- neither may ever be serialized, and the way to
    # guarantee that is for them never to be on the dict that gets serialized.
    # Together they are what lets the two I/O surfaces below build their OWN
    # redacted copy instead of reusing the gate's model-facing view (design
    # §2.6 / CE-1). `detector is None` means nothing scanned this request, so
    # no redacted copy exists at all -- and an absent/empty `internals` (a
    # caller that supplied none) reads the same way, which is the fail-closed
    # direction.
    internals = internals or {}
    raw_prompt = internals.get("raw_prompt") or ""
    detector = internals.get("detector")
    # The gate's redaction OF `raw_prompt` with THAT detector -- the three come
    # off one frozen `Representations`, so the pairing holds. Both surfaces
    # below pass it as `prepared=` to skip re-running the detector over a
    # string it has already scanned; the regex egress pass still runs.
    redacted_prompt = internals.get("redacted_prompt")
    try:
        # Eval-capture (docs/archive/eval-logging-backlog.md; current capture_io
        # contract in docs/reference/per-app-config.md): when capture_io is
        # redacted|full, attach the request prompt + response completion to the
        # trace. `off` (demo default) passes empty strings and sets nothing.
        # The raw body prompt is redacted inside export_trace for `redacted`
        # mode — routes.py never decides what leaves; the exporter path does.
        # Per-app capture_io (B14) overrides the global default; null = inherit.
        capture_io = app_policy.capture_io or s.serving.observability.langfuse.capture_io
        # NO residency clamp here any more (design §2.6, CE-1). It used to turn
        # `full` into `redacted` for ANY onprem_only app whatever its
        # policy_mode -- and the `redacted` path then re-exported the gate's
        # model-facing view, which under `passthrough` IS the raw text. A
        # placement setting silently rewrote telemetry protection, into a mode
        # that did not protect. Placement and protection are separate axes
        # (CLAUDE.md rule 2a); `residency` decides where compute may run and
        # nothing else. What `full` still requires -- the app's own Langfuse
        # project, and a declared no-protection posture for an onprem_only app
        # -- is checked once, at write time, by `validate_effective`
        # (app/policy/compile.py), and enforced again independently at the
        # export boundary by `export_trace`/`resolve_capture_io`, which is
        # where a downgrade can be made while there is still nothing authored
        # to leak.
        prompt = (body.messages[-1].content or "") if body.messages else ""
        completion = ""
        try:
            completion = resp["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError, TypeError):
            completion = ""
        # `display_completion` is what `resp["display"]["output"]` (below) may
        # show -- client-facing (already egress-redacted) by default. `full`
        # mode is the only branch that can diverge `completion`/`raw_completion`
        # (own-project telemetry) away from it -- see `_full_capture_io`'s
        # docstring (fix round 1: display must never carry raw tool_calls).
        display_completion = completion
        # Resolve the capture BEFORE shaping anything for it: `full` needs the
        # app's own Langfuse project (CLAUDE.md rule 2) and `redacted` needs a
        # detector that actually ran, and both answers have to be known before
        # the whole-exchange `full` payload is built. Same function the export
        # boundary calls, so the two can't disagree.
        capture_io = resolve_capture_io(
            capture_io, has_own_project=app_langfuse is not None, detector=detector)
        if capture_io == "full":
            # `full` stores the WHOLE exchange in the app's own project, not
            # just the last message's text: an agentic client's earlier turns,
            # tool results, offered tools and the assistant's tool_calls are
            # the load-bearing part of its trace.
            prompt, completion, raw_completion, display_completion = _full_capture_io(
                body, resp, prompt, completion, raw_completion, raw_tool_calls)
        te = export_trace(
            request_id=resp["id"], tenant_id=tenant, model=resp["model"],
            cost=resp["cost"], policy=resp["policy"], trace=resp["trace"],
            application_id=tenant, app_langfuse=app_langfuse,
            capture_io=capture_io, prompt=prompt, completion=completion,
            # The gate's RAW flattened text and the request's OWN detector: the
            # exporter builds its own redacted copy from them (design §2.6) and
            # never reuses the view the model was given. `detector is None` is
            # its fail-closed signal, not an invitation to export raw.
            raw_prompt=raw_prompt, redacted_prompt=redacted_prompt,
            detector=detector, raw_completion=raw_completion,
            # Task 8: thread the app's custom recognizers (already resolved
            # above for the pipeline run, Task 7) into egress redaction so a
            # custom entity type (e.g. a hospital's MRN pattern) is masked in
            # telemetry, not just the built-in RegexDetector patterns.
            recognizers=profile.recognizers,
            session_id=session_id,
            routing_policy_version=s.routing_policy_version,
            prompt_version=s.prompt_version,
            started_ns=started_ns, usage=resp.get("usage"))
        # First-party console view (Phase C Part 1). Mode-appropriate bodies for
        # the admin drilldown ONLY — this is the request_snapshots cache, same
        # residency boundary as serving, NOT a telemetry egress.
        # `choices[]` stays raw for the calling client; this is additive metadata.
        #
        # It is the SECOND I/O surface, so it obeys the same two rules as the
        # first: it is keyed off what `export_trace` ACTUALLY exported
        # (`te.capture_io`, which can be one step lower than the value above if
        # the per-app tracer failed to build), and its `redacted` bodies are
        # built by the shared `redacted_copy` from the raw values, never from
        # the gate's model-facing view. Before this, `display` was fed
        # `sanitized_prompt` — which under `passthrough` is the raw prompt —
        # and so leaked exactly what telemetry now protects (CE-1).
        if te.capture_io == "full":
            resp["display"] = {"mode": "full", "input": prompt, "output": display_completion}
        elif te.capture_io == "redacted":
            resp["display"] = {
                "mode": "redacted",
                # `prepared` pairs with `raw_prompt` and only with it (the
                # gate redacted that exact string, not the `prompt` fallback).
                "input": (redacted_copy(raw_prompt, detector,
                                        profile.recognizers,
                                        prepared=redacted_prompt)
                          if raw_prompt else
                          redacted_copy(prompt, detector, profile.recognizers)),
                # The CLIENT-facing body (never `completion`, which on the
                # `full` branch above carries the model's raw tool_calls).
                "output": redacted_copy(display_completion, detector,
                                        profile.recognizers),
            }
        else:  # "off"
            resp["display"] = {"mode": "off", "input": None, "output": None}
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
                cached_prompt_tokens=int(usage.get("cached_prompt_tokens", 0)),
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
    # The request's routing outcome (P2b-2): content-free, built from the
    # trace and the gate's `outcome_ctx`. Every response shape reaches here --
    # a real-token stream only after `_finalize_stream` has stamped its
    # waived/incremental verify record, so `verify_mode` reads it.
    try:
        outcome = build_ok_outcome(
            resp, request_id=request_id, application_id=tenant,
            outcome_ctx=internals.get("outcome_ctx") or {},
            routing_policy_version=s.routing_policy_version,
            workload=workload, workload_source=workload_source)
    except Exception:
        logging.getLogger("serving.obs").warning(
            "outcome record failed request=%s", request_id, exc_info=True)
    else:
        await _record_outcome(request, outcome)
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
                            internals: dict | None = None,
                            started_ns: int | None = None,
                            workload: str | None = None,
                            workload_source: str = "none") -> AsyncIterator[str]:
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
                # Shielded: on a real ASGI disconnect Starlette's own
                # disconnect-listener cancel scope keeps re-cancelling this
                # generator (see the `finally` below), so an unshielded await
                # here would raise CancelledError before `_record_cancelled`
                # ever ran. Only retrieving the just-cancelled task and
                # writing the cancelled row are shielded -- no long backend
                # wait happens here.
                with anyio.CancelScope(shield=True):
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
                mapped = (500, "internal error", CODE_INTERNAL_ERROR)
            else:
                if mapped[0] >= 500:
                    logging.getLogger("serving.api").warning(
                        "request %s failed with %d: %s", request_id, mapped[0], mapped[1])
                metrics.observe_request("-", "rejected")
            # Shielded for the same reason `_finalize` is below: a
            # disconnect landing mid-write must not lose the row. Same
            # recorder as the plain HTTP path (usage `error` row for a backend
            # failure; the routing outcome for every routing failure).
            with anyio.CancelScope(shield=True):
                await _record_exception_outcome(
                    request, e, tenant=tenant, request_id=request_id,
                    started=started, s=s, internals=internals,
                    workload=workload, workload_source=workload_source)
            yield sse_error_frame(*mapped)
            yield SSE_DONE
            return
        # Shielded (ledger T2, deferred minor): `_finalize` writes the `ok`
        # usage row and snapshot. A disconnect landing mid-`_finalize` would
        # otherwise deliver Starlette's re-cancelling cancel scope into one of
        # its awaited DB writes -- caught by `except Exception` there for a
        # normal error, but CancelledError is a BaseException and would skip
        # right past it, silently losing the row.
        with anyio.CancelScope(shield=True):
            await _finalize(request, resp, body=body, tenant=tenant, app_policy=app_policy,
                            app_langfuse=app_langfuse, profile=profile,
                            session_id=session_id, s=s, request_id=request_id,
                            internals=internals, started_ns=started_ns,
                            workload=workload, workload_source=workload_source)
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
        # Shielded: on a real ASGI disconnect (uvicorn ASGI spec 2.3 +
        # Starlette 1.3.1), `StreamingResponse` runs `listen_for_disconnect`
        # in an anyio task group whose cancel scope cancels this generator
        # AND KEEPS RE-CANCELLING it -- so the first unshielded await below
        # would raise CancelledError again immediately, and `_record_cancelled`
        # would never run. `task.cancel()` above stays unshielded (no await);
        # only retrieving the already-cancelled/already-done task and writing
        # the cancelled row are shielded -- never a long wait on the backend.
        with anyio.CancelScope(shield=True):
            # Retrieve the outcome on every exit path -- a task that finished
            # (successfully or with an exception) between the last check and
            # generator close must still have its result/exception consumed, or
            # asyncio logs "Task exception was never retrieved".
            await asyncio.gather(task, return_exceptions=True)
            if not was_done and not cancelled_recorded:
                await _record_cancelled(request, tenant=tenant, request_id=request_id,
                                        started=started)


class _WatchedDeltas:
    """A real-token stream's deltas, iterated once, noting whether the stream
    ended in a BACKEND failure (spec §4): any `Exception` out of the backend
    stream except `ContextWindowExceeded` (the client's request) and
    `PipelineRejected` (a pipeline outcome, e.g. no terminal result). A client
    disconnect is `GeneratorExit`/`CancelledError` in the consuming generator
    -- a `BaseException`, raised at ITS yield, never through here -- so it is
    never mistaken for one. Only the backend fetch is watched: an error in the
    caller's own loop body (e.g. the egress verifier) is not a backend
    failure.

    `failed` alone over-reports: the pipeline's `_deltas()` also runs its own
    post-stream work (`_assemble_stream_meta`) inside this iteration, after
    the backend stream has ended and the gateway has already observed it
    `ok`. Callers therefore meter `error` only when `failed` AND the pipeline
    did not flag `get_meta.backend_completed` (`_backend_failed`)."""

    def __init__(self, deltas: AsyncIterator[str]) -> None:
        self._deltas = deltas
        self.failed = False

    async def __aiter__(self) -> AsyncIterator[str]:
        try:
            async for delta in self._deltas:
                yield delta
        except Exception as exc:
            self.failed = not isinstance(exc, (ContextWindowExceeded, PipelineRejected))
            raise


def _backend_failed(watched: _WatchedDeltas, get_meta: Callable[[], dict]) -> bool:
    """A real-token stream is metered `error` only for a BACKEND failure: the
    watched iteration raised, and the backend stream had not already ended
    normally (`Pipeline.run_stream` sets `get_meta.backend_completed`)."""
    return watched.failed and not getattr(get_meta, "backend_completed", False)


async def _record_failed_stream(request: Request, *, tenant: str, request_id: str,
                                started: float, get_meta: Callable[[], dict],
                                s=None, internals: dict | None = None,
                                workload: str | None = None,
                                workload_source: str = "none") -> None:
    """The `error` row for a real-token stream whose backend failed: named for
    the router's chosen model and the gate's ctx, both known eagerly on
    `get_meta` (`Pipeline.run_stream`). A real-token stream makes exactly one
    backend attempt (no gateway fallback once tokens flow), so that model is
    the outcome row's only attempt. Shielded: it runs in the generator's
    `finally`, where a disconnect's re-cancelling scope would otherwise cut
    the write short."""
    chosen = getattr(get_meta, "chosen_model", None)
    with anyio.CancelScope(shield=True):
        await _record_failed(request, tenant=tenant, request_id=request_id,
                             started=started, model=chosen or "-",
                             ctx=getattr(get_meta, "ctx", None), s=s,
                             internals=internals,
                             attempts=(chosen,) if chosen else (),
                             workload=workload, workload_source=workload_source)


def _stream_unverified_response(
        request: Request, *, request_id: str,
        deltas: AsyncIterator[str], get_meta: Callable[[], dict],
        body: ChatRequest, tenant: str, app_policy: AppPolicyView,
        app_langfuse: object | None, profile: AppPrivacyProfile,
        session_id: str | None, s, internals: dict | None = None,
        started: float,
        started_ns: int | None = None,
        workload: str | None = None,
        workload_source: str = "none") -> StreamingResponse:
    """Live token-forwarding SSE for `stream_unverified` (real backend
    streaming). Emits an OpenAI-style role frame, forwards each `deltas` token
    as a `content` frame, then (after the stream drains) a `finish_reason`
    frame + `[DONE]`. `_finalize` runs in a `finally` so the DecisionRecord,
    usage row, and bus publish are recorded for EVERY streamed request --
    including a client that disconnects mid-stream (Global Constraint 2).
    The one exception is a backend failure (`_backend_failed`): there is
    no final metadata to finalize, so it is metered as an `error` row instead
    (spec §4) and the exception propagates as before.

    `created` is computed once and held constant across every frame (Task 4),
    and `model` uses the app's requested model string as a stable per-frame
    label; the resolved serving model lands in the durable usage row via
    `_finalize` (built from the pipeline's final metadata)."""
    created = int(time.time())
    model = body.model  # stable frame label; resolved model is in the usage row

    async def _gen() -> AsyncIterator[str]:
        meta: dict | None = None
        watched = _WatchedDeltas(deltas)
        try:
            yield sse_frame(openai_chunk(request_id, model, role="assistant",
                                         created=created))
            async for delta in watched:
                yield sse_frame(openai_chunk(request_id, model, delta=delta,
                                             created=created))
            meta = get_meta()  # valid only now that `deltas` is fully drained
            yield sse_frame(openai_chunk(request_id, model, finish_reason="stop",
                                         created=created))
            yield SSE_DONE
        finally:
            # A backend failure has no final metadata to finalize: meter it
            # as `error` instead (spec §4). Every other exit -- normal
            # completion and a mid-stream client disconnect alike -- gets the
            # best-effort finalize. Accounting must not be lost.
            if _backend_failed(watched, get_meta):
                await _record_failed_stream(request, tenant=tenant,
                                            request_id=request_id, started=started,
                                            get_meta=get_meta, s=s,
                                            internals=internals, workload=workload,
                                            workload_source=workload_source)
            else:
                await _finalize_stream(
                    request, request_id=request_id, deltas=deltas,
                    get_meta=get_meta, meta=meta, body=body, tenant=tenant,
                    app_policy=app_policy, app_langfuse=app_langfuse,
                    profile=profile, session_id=session_id, s=s,
                    internals=internals, started_ns=started_ns,
                    workload=workload, workload_source=workload_source)

    return sse_response(_gen())


def _incremental_verify_response(
        request: Request, *, request_id: str,
        deltas: AsyncIterator[str], get_meta: Callable[[], dict],
        body: ChatRequest, tenant: str, app_policy: AppPolicyView,
        app_langfuse: object | None, profile: AppPrivacyProfile,
        session_id: str | None, s, internals: dict | None = None,
        started: float,
        started_ns: int | None = None,
        workload: str | None = None,
        workload_source: str = "none") -> StreamingResponse:
    """Live token streaming for `incremental_verify` (Task 7). Real backend
    deltas are piped through a hold-back `StreamingVerifier` before egress, so
    an entity split across chunk boundaries never leaks its raw prefix; the
    verifier's `flush()` releases the held-back tail as a final content frame.

    The verifier is built on the request's EFFECTIVE detector + ctx (the exact
    per-app detector the gate used, surfaced on `get_meta` by `run_stream`) so
    egress redaction matches intake. `_finalize` runs in a `finally` for EVERY
    exit but a backend failure (metered `error` instead, as in
    `_stream_unverified_response`) -- normal completion and a mid-stream
    disconnect alike -- and records
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
        watched = _WatchedDeltas(deltas)
        try:
            yield sse_frame(openai_chunk(request_id, model, role="assistant",
                                         created=created))
            async for delta in watched:
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
            # A backend failure is metered `error` instead of finalized (no
            # final metadata exists; spec §4). Otherwise best-effort finalize
            # on EVERY exit. `client_text` is exactly what the client
            # received (redacted); on a mid-stream disconnect it is the
            # partial released prefix, which is honest.
            if _backend_failed(watched, get_meta):
                await _record_failed_stream(request, tenant=tenant,
                                            request_id=request_id, started=started,
                                            get_meta=get_meta, s=s,
                                            internals=internals, workload=workload,
                                            workload_source=workload_source)
            else:
                await _finalize_stream(
                    request, request_id=request_id, deltas=deltas,
                    get_meta=get_meta, meta=meta, body=body, tenant=tenant,
                    app_policy=app_policy, app_langfuse=app_langfuse,
                    profile=profile, session_id=session_id, s=s,
                    internals=internals,
                    verify_performed=True, verify_reason="incremental",
                    client_text="".join(released),
                    started_ns=started_ns,
                    workload=workload, workload_source=workload_source)

    return sse_response(_gen())


async def _finalize_stream(
        request: Request, *, request_id: str, deltas: AsyncIterator[str],
        get_meta: Callable[[], dict], meta: dict | None,
        body: ChatRequest, tenant: str, app_policy: AppPolicyView,
        app_langfuse: object | None, profile: AppPrivacyProfile,
        session_id: str | None, s, internals: dict | None = None,
        verify_performed: bool = False,
        verify_reason: str = "waived: stream_unverified",
        client_text: str | None = None,
        started_ns: int | None = None,
        workload: str | None = None,
        workload_source: str = "none") -> None:
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
    require_verification, and not a client-driven tool conversation, so
    recording step 9 as waived/incremental (rather than a complete-response
    verdict) is sound."""
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
                        internals=internals, request_id=request_id,
                        started_ns=started_ns,
                        workload=workload, workload_source=workload_source)
    except Exception:  # finalize must never break the (already sent) stream
        log.exception("streaming finalize failed (swallowed)")


async def _find_outcome_safe(request: Request, *, tenant: str, request_id: str | None,
                             trace_id: str | None):
    """`store.find_outcome`, best-effort like every other record on the
    serving path (`_record_outcome` above): a DB error here must never break
    feedback intake or block a Langfuse post that only needs a `trace_id`
    the caller already supplied directly -- log and return None, same as "no
    matching outcome" (P2b-2 Task 5 fix round 1)."""
    try:
        async with request.app.state.db.session() as session:
            return await store.find_outcome(
                session, application_id=tenant, request_id=request_id, trace_id=trace_id)
    except Exception:
        logging.getLogger("serving.obs").warning(
            "outcome lookup failed for feedback request=%s trace=%s",
            request_id, trace_id, exc_info=True)
        return None


async def _write_feedback_safe(request: Request, *, request_id: str, application_id: str,
                                score_name: str, value_num: float | None,
                                value_cat: str | None, source: str) -> bool:
    """`store.write_feedback` + commit, best-effort: a DB error here must
    never change the HTTP status, `stored`, or block a Langfuse post that
    hasn't happened yet (the `corrected_output` local write runs before its
    own Langfuse post) -- log and return False (P2b-2 Task 5 fix round 1)."""
    try:
        async with request.app.state.db.session() as session:
            await store.write_feedback(
                session, request_id=request_id, application_id=application_id,
                score_name=score_name, value_num=value_num, value_cat=value_cat,
                source=source)
            await session.commit()
        return True
    except Exception:
        logging.getLogger("serving.obs").warning(
            "local feedback write failed request=%s score=%s", request_id, score_name,
            exc_info=True)
        return False


class FeedbackIn(BaseModel):
    """Body for POST /v1/feedback. `value` is numeric for quality-style scores
    or a string for categorical scores (e.g. failure_type); Langfuse decides
    dataType by which of those it is (see `app.obs.scores.post_score`).

    One of `trace_id`/`request_id` is required (P2b-2 Task 5): `request_id`
    links the score to the local, app-scoped `outcome_feedback` row and, when
    `trace_id` is omitted, also supplies the Langfuse trace to post to -- the
    matching `routing_outcomes` row's own `trace_id`, if it has one."""
    trace_id: str | None = None
    request_id: str | None = None
    score_name: str
    value: float | str
    comment: str | None = None
    corrected_output: str | None = None


@router.post("/v1/feedback")
async def feedback(body: FeedbackIn, request: Request) -> dict:
    """App-token-authenticated eval/feedback intake (F1 Phase D / B16; local
    storage added P2b-2 Task 5).

    Langfuse (`stored`): posts straight to the calling app's OWN
    residency-local Langfuse project via REST — never the shared project,
    and never anywhere at all when the app has no provisioned project or no
    trace_id is known (fail-closed; `post_score` handles the no-project case
    and returns False, which becomes `stored: false` here, HTTP 200 — a
    missing project is not the caller's fault and must not surface as an
    error).

    Local (`recorded`): app-scoped by construction -- `store.find_outcome`
    only ever matches a `routing_outcomes` row owned by the AUTHENTICATED
    caller, so another app's request is indistinguishable from a request
    that doesn't exist, and both give `recorded: false` without a local
    write. `comment` and `corrected_output` TEXT are never written locally;
    a `corrected_output` submission instead writes a second, fixed
    `value_num=1.0` row under that score name. Gated on
    `serving.outcomes.enabled` -- disabled means `recorded` is always False
    and Langfuse behaviour is unchanged."""
    auth = await _authenticate(request)
    if auth is None:
        raise ApiError(401, "invalid or missing API token", code=CODE_INVALID_API_KEY)
    tenant, app_policy, app_langfuse = auth
    if body.trace_id is None and body.request_id is None:
        raise ApiError(422, "one of trace_id or request_id is required",
                       code=CODE_INVALID_VALUE)
    if body.score_name not in SCORE_NAMES:
        raise ApiError(422, f"unknown score_name: {body.score_name!r}",
                       code=CODE_INVALID_VALUE)

    outcome = None
    if request.app.state.settings.serving.outcomes.enabled:
        outcome = await _find_outcome_safe(
            request, tenant=tenant, request_id=body.request_id, trace_id=body.trace_id)

    # With only request_id given, fall back to the outcome row's own
    # trace_id (None if telemetry never exported one, or if the lookup
    # itself failed) for the Langfuse post.
    trace_id = body.trace_id or (outcome.trace_id if outcome is not None else None)

    recorded = False
    if outcome is not None:
        parsed = feedback_values(body.score_name, body.value)
        if parsed is not None:
            value_num, value_cat = parsed
            recorded = await _write_feedback_safe(
                request, request_id=outcome.request_id, application_id=tenant,
                score_name=body.score_name, value_num=value_num, value_cat=value_cat,
                source="app")

    stored = False
    if trace_id is not None:
        await ensure_score_config(app_langfuse, body.score_name)  # best-effort, never raises
        stored = await post_score(app_langfuse, trace_id, body.score_name,
                                  body.value, body.comment)
    if body.corrected_output:
        if outcome is not None:
            await _write_feedback_safe(
                request, request_id=outcome.request_id, application_id=tenant,
                score_name="corrected_output", value_num=1.0, value_cat=None,
                source="app")
        if trace_id is not None:
            await ensure_score_config(app_langfuse, "corrected_output")
            await post_score(app_langfuse, trace_id, "corrected_output", 1,
                             body.corrected_output)
    return {"stored": stored, "recorded": recorded}
