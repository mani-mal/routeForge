"""DB loaders for the Models page.

Only the columns the metrics read are selected (no ORM entities, no JSON
columns), and feedback comes from a subquery on the outcome window rather than
id chunks. The scan covers the whole window on purpose: a row cap would bias the
figures silently (see docs/reference/metrics-api.md).
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import select

from app.apps.models import EndpointObservation, OutcomeFeedback, RoutingOutcome, UsageRecord
from app.metrics.compute import FeedbackRow, ObservationRow, OutcomeRow, UsageRow


async def load_window(session, *, since: datetime, until: datetime,
                      application_id: str | None,
                      ) -> tuple[list[UsageRow], list[OutcomeRow], list[FeedbackRow]]:
    """Every usage, outcome and task_success row of one window. Placement and
    workload filters are applied by the caller, after attribution."""
    U, O = UsageRecord, RoutingOutcome
    uq = select(U.request_id, U.application_id, U.ts, U.model, U.provider, U.status,
                U.cost_usd, U.prompt_tokens, U.cached_prompt_tokens,
                U.compression_saved_usd, U.routing_saved_usd, U.prompt_cache_saved_usd,
                U.semantic_cache_saved_usd).where(U.ts >= since, U.ts < until)
    oq = select(O.request_id, O.application_id, O.ts, O.status, O.chosen, O.workload,
                O.trajectory_key, O.latency_ms, O.route_reason, O.attempts
                ).where(O.ts >= since, O.ts < until)
    window_ids = select(O.request_id).where(O.ts >= since, O.ts < until)
    if application_id:
        uq = uq.where(U.application_id == application_id)
        oq = oq.where(O.application_id == application_id)
        window_ids = window_ids.where(O.application_id == application_id)
    fq = select(OutcomeFeedback.id, OutcomeFeedback.request_id, OutcomeFeedback.ts,
                OutcomeFeedback.score_name, OutcomeFeedback.value_num).where(
        OutcomeFeedback.score_name == "task_success",
        OutcomeFeedback.request_id.in_(window_ids))

    usage = [UsageRow(
        request_id=u.request_id, application_id=u.application_id, ts=u.ts, model=u.model,
        provider=u.provider, status=u.status, cost_usd=u.cost_usd or 0.0,
        prompt_tokens=u.prompt_tokens or 0, cached_prompt_tokens=u.cached_prompt_tokens,
        compression_saved_usd=u.compression_saved_usd, routing_saved_usd=u.routing_saved_usd,
        prompt_cache_saved_usd=u.prompt_cache_saved_usd,
        semantic_cache_saved_usd=u.semantic_cache_saved_usd)
        for u in (await session.execute(uq)).all()]
    outcomes = [OutcomeRow(
        request_id=o.request_id, application_id=o.application_id, ts=o.ts, status=o.status,
        chosen=o.chosen, workload=o.workload, trajectory_key=o.trajectory_key,
        latency_ms=o.latency_ms, route_reason=o.route_reason,
        attempts=list(o.attempts or []))
        for o in (await session.execute(oq)).all()]
    feedback = [FeedbackRow(request_id=f.request_id, ts=f.ts, score_name=f.score_name,
                            value_num=f.value_num, id=f.id)
                for f in (await session.execute(fq)).all()]
    return usage, outcomes, feedback


async def load_observations(session, *, since: datetime, until: datetime) -> list[ObservationRow]:
    q = select(EndpointObservation.ts, EndpointObservation.model, EndpointObservation.outcome,
               EndpointObservation.latency_ms, EndpointObservation.streamed,
               EndpointObservation.ttft_ms, EndpointObservation.completion_tokens).where(
        EndpointObservation.ts >= since, EndpointObservation.ts < until)
    return [ObservationRow(ts=r.ts, model=r.model, outcome=r.outcome, latency_ms=r.latency_ms,
                           streamed=bool(r.streamed), ttft_ms=r.ttft_ms,
                           completion_tokens=r.completion_tokens)
            for r in (await session.execute(q)).all()]
