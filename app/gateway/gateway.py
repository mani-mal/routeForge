from __future__ import annotations

import logging
import re
import time
from typing import TYPE_CHECKING

from typing import AsyncIterator

from app.config.schema import Settings
from app.errors import BackendUnavailable, ContextWindowExceeded, NoEligibleCandidate
from app.gateway.backends.base import InferenceBackend, StreamChunk
from app.models import DecisionRecord, ExecutionPolicyContext, GenerationResult, Message
from app.obs import metrics
from app.obs.redact import redact_value
from app.routing.router import Router, expected_output_tokens
from app.routing.signals import SignalStore

if TYPE_CHECKING:
    from app.apps.registry import ModelRegistry
    from app.gateway.breaker import CircuitBreaker

_NEXT = {"cheap": "mid", "mid": "frontier", "frontier": None}

# Upstream error text reaches the client in the 502 detail. Endpoints (URLs,
# IPv4 addresses) are internal topology, so they are scrubbed first.
_URL = re.compile(r"https?://[^\s'\"<>()]+")
_IPV4 = re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}(?::\d+)?\b")


def _scrub_endpoints(text: str) -> str:
    return _IPV4.sub("<endpoint>", _URL.sub("<endpoint>", text))


def _format_upstream_error(model: str, exc: Exception) -> str:
    """The `<model>: <ErrorType>: <message>` string used in both the gateway
    log line and (via `BackendUnavailable`) the client-facing 502 detail.
    Endpoints are scrubbed first (internal topology), then the remaining text
    is redacted for PII (CLAUDE.md rule 2) -- an upstream error can echo back
    request content (e.g. a bad-request body), so it is an egress surface
    like any other, not just an internal-topology one."""
    return (f"{model}: {type(exc).__name__}: "
            f"{redact_value(_scrub_endpoints(str(exc)))[:200]}")


# Exception classes that mean "our code is wrong", not "the endpoint is sick"
# (see counts_as_endpoint_unhealth). json.JSONDecodeError is a ValueError.
_LOCAL_PROGRAMMING_ERRORS: tuple[type[BaseException], ...] = (
    TypeError, AttributeError, NameError, KeyError, IndexError,
    AssertionError, NotImplementedError, ValueError,
)


def counts_as_endpoint_unhealth(exc: Exception) -> bool:
    """Does this failure say something about the ENDPOINT, or about the request?

    A 4xx means the request was wrong -- an oversized prompt, a bad tool schema,
    a missing key. Counting it against availability would let one client's bad
    requests push traffic off a perfectly healthy endpoint, which is exactly the
    shape of the Qwen Code retry storm (B36).

    429 is the exception among 4xx: it is the endpoint refusing load, and it is
    the closest thing to admission pressure an OpenAI-compatible endpoint can
    express -- the one real capacity signal the protocol offers.

    Anything else with no status at all (connection refused, TLS failure,
    timeout) is endpoint unhealth.

    **Local programming errors are not** -- `TypeError`, `AttributeError`,
    `NameError`, `KeyError`, `IndexError`, `AssertionError`,
    `NotImplementedError` and `ValueError` (which includes
    `json.JSONDecodeError`), checked BEFORE the status code. These are bugs on
    OUR side of the wire (a changed Protocol signature raising `TypeError`
    inside the backend has been swallowed as a backend failure in this project
    before). This predicate feeds both the availability signal and the step-7
    circuit breaker, and the breaker is shared by every tenant: counting a
    client-side bug would let one request shape open an endpoint's circuit
    for everyone. The accepted cost: an endpoint that returns malformed JSON
    (a `JSONDecodeError` in the client library) no longer counts against it.

    No transport/API error class the backend can raise subclasses one of
    these (MRO-checked over litellm, openai and httpx: the only hits are
    `openai.InvalidWebhookSignatureError`, raised by webhook verification and
    never on a completion call, and litellm's `RouterRateLimitError[Basic]`,
    raised only by `litellm.Router`, which RouteForge does not use), so no
    explicit exemption is needed. Re-check if a new backend is added.
    """
    if isinstance(exc, ContextWindowExceeded):
        return False
    if isinstance(exc, _LOCAL_PROGRAMMING_ERRORS):
        return False
    code = getattr(exc, "status_code", None)
    if code is None:
        return True
    try:
        code = int(code)
    except (TypeError, ValueError):
        return True
    if code == 429:
        return True
    return not (400 <= code < 500)

def _exhausted(last_error: str, overflow_errors: list[str], failures: int,
                attempted: list[str]) -> Exception:
    """The error for a request every reachable candidate failed. When EVERY
    failure was a context-window refusal, the request itself is too large --
    the client's error, so it surfaces as `ContextWindowExceeded` (400) with
    the upstream reason kept whole (its token counts are the actionable part,
    so it is not cut to the 200-char log form). Any other failure in the mix
    keeps it an outage (502). Both carry `attempted_models` -- the candidates
    tried, in order (signal hardening, spec §4) -- so the route can meter the
    last one on its `error` usage row and name them on the routing outcome."""
    if overflow_errors and len(overflow_errors) == failures:
        return ContextWindowExceeded(overflow_errors[-1],
                                     attempted_models=tuple(attempted))
    return BackendUnavailable(
        "backend failed on all reachable candidates for this request "
        f"(last error: {last_error})", attempted_models=tuple(attempted))


log = logging.getLogger("serving.gateway")


class Gateway:
    def __init__(self, settings: Settings, backend: InferenceBackend,
                 router: Router, registry: "ModelRegistry | None" = None,
                 signals: "SignalStore | None" = None,
                 breaker: "CircuitBreaker | None" = None) -> None:
        self._s = settings
        self._backend = backend
        self._router = router
        self._reg = registry
        self._signals = signals
        self._breaker = breaker

    def _open_circuits(self) -> frozenset[str]:
        return self._breaker.open_set() if self._breaker is not None else frozenset()

    @property
    def router(self) -> Router:
        """Exposes the SAME `Router` instance `run`/`run_stream` use. If
        step 4's rules-first task-mode decision (`app/routing/task_mode.py`)
        is ever replaced by a routed LLM call, that call must go through this
        identical, residency-filtered entry point instead of the pipeline
        constructing a second one -- see CLAUDE.md's "the step-4 classifier
        is a routed call"."""
        return self._router

    @property
    def backend(self) -> InferenceBackend:
        """Exposes the SAME `InferenceBackend` instance `run`/`run_stream`
        call, for the same reason as `router` above -- any future step-4
        classifier call must be made on this instance, never a new one."""
        return self._backend

    async def run(self, ctx: ExecutionPolicyContext, messages: list[Message],
                  tokens_after: int, complexity: float, hint: dict,
                  tools: list[dict] | None = None,
                  tool_choice: str | dict | None = None,
                  params: dict | None = None
                  ) -> tuple[GenerationResult, list[DecisionRecord]]:
        records: list[DecisionRecord] = []
        threshold = self._s.routing.confidence_escalation_threshold
        max_esc = self._s.routing.max_escalations
        max_failover = self._s.routing.max_failover_attempts
        min_tier: str | None = None
        escalations = 0
        # Failure-driven lateral switch: exclude every model that has already
        # failed on this request and re-select. Because excluded models leave the
        # candidate set, the router picks a same-tier peer first and escalates on
        # its own once the tier empties -- one mechanism for both. Bounded by
        # max_failover_attempts; residency is preserved because exclude only ever
        # removes candidates (see Router.select).
        failed_models: set[str] = set()
        attempted: list[str] = []
        last_error = ""
        overflow_errors: list[str] = []
        failover_attempts = 0
        result: GenerationResult | None = None
        out_tokens = expected_output_tokens(self._s, params)

        while True:
            try:
                with metrics.time_routing():
                    decision = self._router.select(
                        ctx, tokens_after, complexity, min_tier,
                        exclude=frozenset(failed_models), output_tokens=out_tokens,
                        open_circuits=self._open_circuits())
            except NoEligibleCandidate as exc:
                # A confidence escalation found nothing eligible at the higher
                # tier (e.g. it is over the per-request budget the served answer
                # fell back under). Keep the answer already served -- and its
                # cost, which is metered from `records` -- and say why.
                if result is not None and escalations > 0 and min_tier is not None:
                    records.append(DecisionRecord(
                        step="gateway", chosen=None,
                        reason=f"escalation skipped: no eligible candidate at {min_tier} ({exc})",
                        latency_ms=0.0, hop_index=len(records)))
                    break
                # Every reachable candidate has now been excluded by prior
                # failures -> exhausted, not a genuine no-candidate reject.
                if failed_models:
                    raise _exhausted(
                        last_error, overflow_errors, failover_attempts, attempted) from exc
                raise
            cand = decision.chosen
            attempted.append(cand.name)
            price = (self._reg.price_for(cand.name) if self._reg
                     else self._s.pricing.models[cand.name])
            try:
                result = await self._backend.generate(
                    cand.name, messages, price, cand.tier, hint,
                    tools=tools, tool_choice=tool_choice, params=params)
            except Exception as exc:
                last_error = _format_upstream_error(cand.name, exc)
                if self._signals is not None and counts_as_endpoint_unhealth(exc):
                    # This hop's model failed -- observe THAT model, not the
                    # peer that succeeds on the next iteration.
                    self._signals.observe_error(cand.name)
                if self._breaker is not None and counts_as_endpoint_unhealth(exc):
                    self._breaker.record_failure(cand.name)
                if isinstance(exc, ContextWindowExceeded):
                    overflow_errors.append(
                        redact_value(_scrub_endpoints(str(exc)))[:1000])
                log.warning("backend call failed request=%s model=%s attempt=%d: %s",
                            ctx.request_id, cand.name, failover_attempts + 1, last_error)
                records.append(DecisionRecord(
                    step="gateway", chosen=None,
                    reason=f"{cand.name} failed; retrying peer/escalating",
                    rejected=decision.rejected, est_cost_usd=decision.est_cost_usd,
                    latency_ms=0.0, hop_index=len(records),
                    # The decision's tier target and breaker bypass are as real
                    # as its scoring: after a failover this record IS the
                    # request's first routing decision (app/apps/outcomes.py).
                    tier_target=decision.tier_target,
                    circuit_bypass=decision.circuit_bypass,
                    # This hop's chosen field stays None (nothing was SERVED),
                    # but the scoring that led to picking `cand` is still real
                    # and otherwise permanently lost the instant this record
                    # is appended -- no step="router" record is ever emitted
                    # for a candidate that failed. Failover is exactly where
                    # the availability signal matters, so keep it: the failed
                    # candidate's name is recoverable as
                    # `scoring["candidates"][0]["name"]` even though `.chosen`
                    # is None here.
                    scoring=decision.scoring.to_record() if decision.scoring else None))
                failed_models.add(cand.name)
                failover_attempts += 1
                if failover_attempts > max_failover:
                    raise _exhausted(
                        last_error, overflow_errors, failover_attempts, attempted) from exc
                continue

            if self._signals is not None:
                self._signals.observe_success(cand.name, result.latency_ms)
            if self._breaker is not None:
                self._breaker.record_success(cand.name)

            records.append(DecisionRecord(
                step="router", chosen=cand.name,
                chosen_display_name=cand.display_name,
                chosen_tier=cand.tier, chosen_provider=cand.provider,
                tier_target=decision.tier_target,
                reason=decision.reason,
                rejected=decision.rejected, est_cost_usd=decision.est_cost_usd,
                actual_cost_usd=result.cost_usd, latency_ms=result.latency_ms,
                hop_index=len(records),
                quality_score=decision.quality_score,
                quality_source=decision.quality_source,
                scoring=decision.scoring.to_record() if decision.scoring else None,
                circuit_bypass=decision.circuit_bypass))
            metrics.observe_routing_decision(cand.tier, escalated=escalations > 0)

            # For onprem_only requests the router clamps an out-of-reach tier back
            # down, so an escalation may re-issue the same tier — bounded by
            # max_escalations, never reaching cloud.
            if (result.confidence < threshold and escalations < max_esc
                    and _NEXT[cand.tier] is not None):
                escalations += 1
                min_tier = _NEXT[cand.tier]
                hint = {k: v for k, v in hint.items() if k != "mock_confidence"}
                continue
            break

        return result, records

    async def run_stream(self, ctx: ExecutionPolicyContext, messages: list[Message],
                         tokens_after: int, complexity: float, hint: dict,
                         tools: list[dict] | None = None,
                         tool_choice: str | dict | None = None,
                         params: dict | None = None
                         ) -> tuple[AsyncIterator[StreamChunk], list[DecisionRecord]]:
        """Real streaming counterpart of `run()` for the direct (single-shot)
        path. Performs the SAME residency-clamped router selection and emits the
        SAME step="router" DecisionRecord (chosen + rejected `sp.candidates`,
        quality) as `run()`, then delegates to `backend.generate_stream` on the
        chosen candidate. There is NO confidence-escalation loop here (streaming
        can't retry a partially-emitted response) and NO verify record (step 9 is
        waived on this path -- the pipeline records that in its metadata).

        The stream observes availability (spec §5): a terminal chunk logs an
        `ok` observation (TTFT measured, latency state untouched -- stream
        timing is not the quantity the latency EWMA holds); a backend
        exception that `counts_as_endpoint_unhealth` logs an `error`
        observation. A client disconnect (`GeneratorExit`/`CancelledError`,
        both `BaseException`) and a stream that ends without a terminal chunk
        are NOT observed -- neither is attributable to the endpoint.

        Ordering contract: the returned `records` list is COMPLETE only after the
        returned iterator is fully consumed. The router record's `actual_cost_usd`
        and `latency_ms` are unknown until the terminal `StreamChunk(final=...)`
        arrives, so the iterator patches the record in place when it sees `final`.
        """
        with metrics.time_routing():
            decision = self._router.select(
                ctx, tokens_after, complexity, None,
                output_tokens=expected_output_tokens(self._s, params),
                open_circuits=self._open_circuits())
        cand = decision.chosen
        price = (self._reg.price_for(cand.name) if self._reg
                 else self._s.pricing.models[cand.name])
        record = DecisionRecord(
            step="router", chosen=cand.name,
            chosen_display_name=cand.display_name,
            chosen_tier=cand.tier, chosen_provider=cand.provider,
            tier_target=decision.tier_target,
            reason=decision.reason,
            rejected=decision.rejected, est_cost_usd=decision.est_cost_usd,
            actual_cost_usd=None, latency_ms=0.0, hop_index=0,
            quality_score=decision.quality_score,
            quality_source=decision.quality_source,
            scoring=decision.scoring.to_record() if decision.scoring else None,
            circuit_bypass=decision.circuit_bypass)
        records: list[DecisionRecord] = [record]
        metrics.observe_routing_decision(cand.tier, escalated=False)

        async def _agen() -> AsyncIterator[StreamChunk]:
            t0 = time.perf_counter()
            ttft_ms: float | None = None
            try:
                async for chunk in self._backend.generate_stream(
                        cand.name, messages, price, cand.tier, hint,
                        tools=tools, tool_choice=tool_choice, params=params):
                    if ttft_ms is None and chunk.delta is not None:
                        ttft_ms = (time.perf_counter() - t0) * 1000.0
                    if chunk.final is not None:
                        record.actual_cost_usd = chunk.final.cost_usd
                        record.latency_ms = chunk.final.latency_ms
                        if self._signals is not None:
                            # Availability only -- a stream's timing is not the
                            # quantity the latency EWMA holds (spec §5).
                            self._signals.observe_stream_success(cand.name, ttft_ms)
                        if self._breaker is not None:
                            self._breaker.record_success(cand.name)
                    yield chunk
            except Exception as exc:
                if self._signals is not None and counts_as_endpoint_unhealth(exc):
                    self._signals.observe_error(cand.name, streamed=True)
                if self._breaker is not None and counts_as_endpoint_unhealth(exc):
                    self._breaker.record_failure(cand.name)
                error = _format_upstream_error(cand.name, exc)
                log.warning("backend stream failed request=%s model=%s: %s",
                            ctx.request_id, cand.name, error)
                raise

        return _agen(), records
