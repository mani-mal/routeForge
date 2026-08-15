from __future__ import annotations

from typing import TYPE_CHECKING

from app.config.schema import Settings
from app.errors import BackendUnavailable
from app.gateway.backends.base import InferenceBackend
from app.models import DecisionRecord, ExecutionPolicyContext, GenerationResult, Message
from app.obs import metrics
from app.routing.router import Router

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

    async def run(self, ctx: ExecutionPolicyContext, messages: list[Message],
                  tokens_after: int, complexity: float, hint: dict
                  ) -> tuple[GenerationResult, list[DecisionRecord]]:
        records: list[DecisionRecord] = []
        threshold = self._s.routing.confidence_escalation_threshold
        max_esc = self._s.routing.max_escalations
        min_tier: str | None = None
        escalations = 0
        failed_tiers: set[str] = set()
        result: GenerationResult | None = None

        while True:
            with metrics.time_routing():
                decision = self._router.select(ctx, tokens_after, complexity, min_tier)
            cand = decision.chosen
            # Fail closed: if residency clamping keeps returning a tier that already
            # failed (e.g. onprem_only can't advance past the best on-prem tier),
            # stop rather than loop forever.
            if cand.tier in failed_tiers:
                raise BackendUnavailable(
                    "backend failed on all reachable tiers for this request")
            price = (self._reg.price_for(cand.name) if self._reg
                     else self._s.pricing.models[cand.name])
            try:
                result = await self._backend.generate(
                    cand.name, messages, price, cand.tier, hint)
            except Exception as exc:
                records.append(DecisionRecord(
                    step="gateway", chosen=None,
                    reason=f"{cand.name} failed; escalating/fallback",
                    rejected=decision.rejected, est_cost_usd=decision.est_cost_usd,
                    latency_ms=0.0, hop_index=len(records)))
                failed_tiers.add(cand.tier)
                nxt = _NEXT[cand.tier]
                if nxt is None:
                    raise BackendUnavailable(
                        "backend failed on all reachable tiers for this request"
                    ) from exc
                min_tier = nxt
                continue

            records.append(DecisionRecord(
                step="router", chosen=cand.name,
                chosen_display_name=cand.display_name, reason=decision.reason,
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
