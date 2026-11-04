"""DB loaders for the Models page: one select per table, joined in Python."""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import select

from app.apps.models import EndpointObservation, OutcomeFeedback, RoutingOutcome, UsageRecord
from app.metrics.compute import FeedbackRow, ObservationRow, OutcomeRow, UsageRow

_CHUNK = 500


def placement_for_provider(provider: str | None) -> str:
    return "onprem" if provider == "onprem" else "cloud"


async def load_window(session, *, since: datetime, until: datetime,
                      application_id: str | None, placement: str | None,
                      ) -> tuple[list[UsageRow], list[OutcomeRow], list[FeedbackRow]]:
    uq = select(UsageRecord).where(UsageRecord.ts >= since, UsageRecord.ts < until)
    oq = select(RoutingOutcome).where(RoutingOutcome.ts >= since, RoutingOutcome.ts < until)
    if application_id:
        uq = uq.where(UsageRecord.application_id == application_id)
        oq = oq.where(RoutingOutcome.application_id == application_id)
    urecs = (await session.execute(uq)).scalars().all()
    orecs = (await session.execute(oq)).scalars().all()

    provider_of = {u.model: u.provider for u in urecs}
    if placement:
        urecs = [u for u in urecs if placement_for_provider(u.provider) == placement]
        orecs = [o for o in orecs
                 if o.chosen and placement_for_provider(provider_of.get(o.chosen)) == placement]

    usage = [UsageRow(
        request_id=u.request_id, application_id=u.application_id, ts=u.ts, model=u.model,
        provider=u.provider, status=u.status, cost_usd=u.cost_usd or 0.0,
        prompt_tokens=u.prompt_tokens or 0, cached_prompt_tokens=u.cached_prompt_tokens,
        compression_saved_usd=u.compression_saved_usd, routing_saved_usd=u.routing_saved_usd,
        prompt_cache_saved_usd=u.prompt_cache_saved_usd,
        semantic_cache_saved_usd=u.semantic_cache_saved_usd) for u in urecs]
    outcomes = [OutcomeRow(
        request_id=o.request_id, application_id=o.application_id, ts=o.ts, status=o.status,
        chosen=o.chosen, workload=o.workload, trajectory_key=o.trajectory_key,
        latency_ms=o.latency_ms, route_reason=o.route_reason,
        attempts=list(o.attempts or [])) for o in orecs]

    ids = sorted({o.request_id for o in outcomes})
    feedback: list[FeedbackRow] = []
    for i in range(0, len(ids), _CHUNK):
        fq = select(OutcomeFeedback).where(
            OutcomeFeedback.score_name == "task_success",
            OutcomeFeedback.request_id.in_(ids[i:i + _CHUNK]))
        for f in (await session.execute(fq)).scalars().all():
            feedback.append(FeedbackRow(request_id=f.request_id, ts=f.ts,
                                        score_name=f.score_name, value_num=f.value_num))
    return usage, outcomes, feedback


async def load_observations(session, *, since: datetime, until: datetime) -> list[ObservationRow]:
    q = select(EndpointObservation).where(
        EndpointObservation.ts >= since, EndpointObservation.ts < until)
    return [ObservationRow(ts=r.ts, model=r.model, outcome=r.outcome, latency_ms=r.latency_ms,
                           streamed=bool(r.streamed), ttft_ms=r.ttft_ms,
                           completion_tokens=r.completion_tokens)
            for r in (await session.execute(q)).scalars().all()]
