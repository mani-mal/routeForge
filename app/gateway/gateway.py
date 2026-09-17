from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING

from typing import AsyncIterator

from app.config.schema import Settings
from app.errors import BackendUnavailable, ContextWindowExceeded, NoEligibleCandidate
from app.gateway.backends.base import InferenceBackend, StreamChunk
from app.models import DecisionRecord, ExecutionPolicyContext, GenerationResult, Message
from app.obs import metrics
from app.obs.redact import redact_value
from app.routing.router import Router, expected_output_tokens

if TYPE_CHECKING:
    from app.apps.registry import ModelRegistry

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


def _exhausted(last_error: str, overflow_errors: list[str], failures: int) -> Exception:
    """The error for a request every reachable candidate failed. When EVERY
    failure was a context-window refusal, the request itself is too large --
    the client's error, so it surfaces as `ContextWindowExceeded` (400) with
    the upstream reason kept whole (its token counts are the actionable part,
    so it is not cut to the 200-char log form). Any other failure in the mix
    keeps it an outage (502)."""
    if overflow_errors and len(overflow_errors) == failures:
        return ContextWindowExceeded(overflow_errors[-1])
    return BackendUnavailable(
        "backend failed on all reachable candidates for this request "
        f"(last error: {last_error})")


log = logging.getLogger("serving.gateway")


class Gateway:
    def __init__(self, settings: Settings, backend: InferenceBackend,
                 router: Router, registry: "ModelRegistry | None" = None) -> None:
        self._s = settings
        self._backend = backend
        self._router = router
        self._reg = registry

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
                        exclude=frozenset(failed_models), output_tokens=out_tokens)
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
                    raise _exhausted(last_error, overflow_errors, failover_attempts) from exc
                raise
            cand = decision.chosen
            price = (self._reg.price_for(cand.name) if self._reg
                     else self._s.pricing.models[cand.name])
            try:
                result = await self._backend.generate(
                    cand.name, messages, price, cand.tier, hint,
                    tools=tools, tool_choice=tool_choice, params=params)
            except Exception as exc:
                last_error = _format_upstream_error(cand.name, exc)
                if isinstance(exc, ContextWindowExceeded):
                    overflow_errors.append(
                        redact_value(_scrub_endpoints(str(exc)))[:1000])
                log.warning("backend call failed request=%s model=%s attempt=%d: %s",
                            ctx.request_id, cand.name, failover_attempts + 1, last_error)
                records.append(DecisionRecord(
                    step="gateway", chosen=None,
                    reason=f"{cand.name} failed; retrying peer/escalating",
                    rejected=decision.rejected, est_cost_usd=decision.est_cost_usd,
                    latency_ms=0.0, hop_index=len(records)))
                failed_models.add(cand.name)
                failover_attempts += 1
                if failover_attempts > max_failover:
                    raise _exhausted(
                        last_error, overflow_errors, failover_attempts) from exc
                continue

            records.append(DecisionRecord(
                step="router", chosen=cand.name,
                chosen_display_name=cand.display_name,
                chosen_tier=cand.tier, chosen_provider=cand.provider,
                reason=decision.reason,
                rejected=decision.rejected, est_cost_usd=decision.est_cost_usd,
                actual_cost_usd=result.cost_usd, latency_ms=result.latency_ms,
                hop_index=len(records),
                quality_score=decision.quality_score,
                quality_source=decision.quality_source))
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

        Ordering contract: the returned `records` list is COMPLETE only after the
        returned iterator is fully consumed. The router record's `actual_cost_usd`
        and `latency_ms` are unknown until the terminal `StreamChunk(final=...)`
        arrives, so the iterator patches the record in place when it sees `final`.
        """
        with metrics.time_routing():
            decision = self._router.select(
                ctx, tokens_after, complexity, None,
                output_tokens=expected_output_tokens(self._s, params))
        cand = decision.chosen
        price = (self._reg.price_for(cand.name) if self._reg
                 else self._s.pricing.models[cand.name])
        record = DecisionRecord(
            step="router", chosen=cand.name,
            chosen_display_name=cand.display_name,
            chosen_tier=cand.tier, chosen_provider=cand.provider,
            reason=decision.reason,
            rejected=decision.rejected, est_cost_usd=decision.est_cost_usd,
            actual_cost_usd=None, latency_ms=0.0, hop_index=0,
            quality_score=decision.quality_score,
            quality_source=decision.quality_source)
        records: list[DecisionRecord] = [record]
        metrics.observe_routing_decision(cand.tier, escalated=False)

        async def _agen() -> AsyncIterator[StreamChunk]:
            try:
                async for chunk in self._backend.generate_stream(
                        cand.name, messages, price, cand.tier, hint,
                        tools=tools, tool_choice=tool_choice, params=params):
                    if chunk.final is not None:
                        record.actual_cost_usd = chunk.final.cost_usd
                        record.latency_ms = chunk.final.latency_ms
                    yield chunk
            except Exception as exc:
                error = _format_upstream_error(cand.name, exc)
                log.warning("backend stream failed request=%s model=%s: %s",
                            ctx.request_id, cand.name, error)
                raise

        return _agen(), records
