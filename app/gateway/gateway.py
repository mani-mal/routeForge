from __future__ import annotations

from typing import TYPE_CHECKING

from typing import AsyncIterator

from app.config.schema import Settings
from app.errors import BackendUnavailable, NoEligibleCandidate
from app.gateway.backends.base import InferenceBackend, StreamChunk
from app.models import DecisionRecord, ExecutionPolicyContext, GenerationResult, Message
from app.obs import metrics
from app.routing.router import Router, expected_output_tokens

if TYPE_CHECKING:
    from app.apps.registry import ModelRegistry

_NEXT = {"cheap": "mid", "mid": "frontier", "frontier": None}


class Gateway:
    def __init__(self, settings: Settings, backend: InferenceBackend,
                 router: Router, registry: "ModelRegistry | None" = None) -> None:
        self._s = settings
        self._backend = backend
        self._router = router
        self._reg = registry

    @property
    def router(self) -> Router:
        """Exposes the SAME `Router` instance `run`/`run_stream` use, so
        Task 2's optional step-4 LLM classifier (`app/routing/task_mode_llm.
        py::classify_task_mode`) routes through the identical
        residency-filtered entry point instead of the pipeline constructing
        (or the classifier being handed) a second one."""
        return self._router

    @property
    def backend(self) -> InferenceBackend:
        """Exposes the SAME `InferenceBackend` instance `run`/`run_stream`
        call, for the same reason as `router` above -- the classifier makes
        its (post-selection) call on this instance, never a new one."""
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
        failover_attempts = 0
        result: GenerationResult | None = None
        out_tokens = expected_output_tokens(self._s, params)

        while True:
            try:
                with metrics.time_routing():
                    decision = self._router.select(
                        ctx, tokens_after, complexity, min_tier,
                        exclude=frozenset(failed_models), output_tokens=out_tokens)
            except NoEligibleCandidate:
                # Every reachable candidate has now been excluded by prior
                # failures -> exhausted, not a genuine no-candidate reject.
                if failed_models:
                    raise BackendUnavailable(
                        "backend failed on all reachable candidates for this request "
                        f"(last error: {last_error})")
                raise
            cand = decision.chosen
            price = (self._reg.price_for(cand.name) if self._reg
                     else self._s.pricing.models[cand.name])
            try:
                result = await self._backend.generate(
                    cand.name, messages, price, cand.tier, hint,
                    tools=tools, tool_choice=tool_choice, params=params)
            except Exception as exc:
                last_error = f"{cand.name}: {type(exc).__name__}: {str(exc)[:200]}"
                records.append(DecisionRecord(
                    step="gateway", chosen=None,
                    reason=f"{cand.name} failed; retrying peer/escalating",
                    rejected=decision.rejected, est_cost_usd=decision.est_cost_usd,
                    latency_ms=0.0, hop_index=len(records)))
                failed_models.add(cand.name)
                failover_attempts += 1
                if failover_attempts > max_failover:
                    raise BackendUnavailable(
                        "backend failed on all reachable candidates for this request "
                        f"(last error: {last_error})"
                    ) from exc
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
            async for chunk in self._backend.generate_stream(
                    cand.name, messages, price, cand.tier, hint,
                    tools=tools, tool_choice=tool_choice, params=params):
                if chunk.final is not None:
                    record.actual_cost_usd = chunk.final.cost_usd
                    record.latency_ms = chunk.final.latency_ms
                yield chunk

        return _agen(), records
