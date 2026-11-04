"""Published (Artificial Analysis) metrics: pure payload parser + snapshot store.

Display-only. Nothing in routing/policy/gateway may import this module.
Every metric is nullable: null = not published, never 0.
"""
from __future__ import annotations

import datetime as dt
import math
from dataclasses import dataclass

from sqlalchemy import delete, select

from app.apps.models import ExternalModelMap, ExternalModelMetric, ExternalSnapshot

SOURCE = "artificial_analysis"

_METRIC_FIELDS = ("intelligence_index", "coding_index", "math_index", "price_in",
                  "price_out", "price_blended", "output_tps", "ttft_s", "ttfat_s")


@dataclass(frozen=True)
class AAModel:
    slug: str
    name: str
    creator: str | None
    intelligence_index: float | None
    coding_index: float | None
    math_index: float | None
    price_in: float | None
    price_out: float | None
    price_blended: float | None
    output_tps: float | None
    ttft_s: float | None
    ttfat_s: float | None


class AAPayloadError(ValueError):
    """The payload is not an Artificial Analysis model list; refused whole."""


def _num(v: object) -> float | None:
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    f = float(v)
    return f if math.isfinite(f) else None


def _sub(d: dict, key: str) -> dict:
    v = d.get(key)
    return v if isinstance(v, dict) else {}


def _text(v: object) -> str | None:
    return v if isinstance(v, str) and v else None


def parse_aa_payload(payload: object) -> list[AAModel]:
    entries = payload.get("data") if isinstance(payload, dict) else payload
    if not isinstance(entries, list):
        raise AAPayloadError("expected a list of models or an object with a 'data' list")
    if not entries:
        raise AAPayloadError("the model list is empty")
    out: dict[str, AAModel] = {}
    for i, e in enumerate(entries):
        if not isinstance(e, dict):
            raise AAPayloadError(f"entry {i} is not an object")
        slug = _text(e.get("slug")) or _text(e.get("id"))
        if slug is None:
            raise AAPayloadError(f"entry {i} has neither 'slug' nor 'id'")
        if slug in out:
            continue
        ev, pr = _sub(e, "evaluations"), _sub(e, "pricing")
        out[slug] = AAModel(
            slug=slug,
            name=_text(e.get("name")) or slug,
            creator=_text(_sub(e, "model_creator").get("name")),
            intelligence_index=_num(ev.get("artificial_analysis_intelligence_index")),
            coding_index=_num(ev.get("artificial_analysis_coding_index")),
            math_index=_num(ev.get("artificial_analysis_math_index")),
            price_in=_num(pr.get("price_1m_input_tokens")),
            price_out=_num(pr.get("price_1m_output_tokens")),
            price_blended=_num(pr.get("price_1m_blended_3_to_1")),
            output_tps=_num(e.get("median_output_tokens_per_second")),
            ttft_s=_num(e.get("median_time_to_first_token_seconds")),
            ttfat_s=_num(e.get("median_time_to_first_answer_token")),
        )
    return list(out.values())


async def store_snapshot(session, models: list[AAModel], *, origin: str,
                         retrieved_at: dt.datetime, index_version: str | None,
                         api_tier: str | None) -> int:
    """Add the snapshot + rows to the session and commit. Returns the snapshot id."""
    snap = ExternalSnapshot(source=SOURCE, origin=origin, retrieved_at=retrieved_at,
                            index_version=index_version, api_tier=api_tier,
                            row_count=len(models))
    session.add(snap)
    await session.flush()
    session.add_all([
        ExternalModelMetric(
            snapshot_id=snap.id, ext_model_slug=m.slug, ext_model_name=m.name[:200],
            ext_creator=m.creator, **{f: getattr(m, f) for f in _METRIC_FIELDS})
        for m in models])
    await session.commit()
    return snap.id


async def latest_snapshot(session) -> ExternalSnapshot | None:
    rows = await list_snapshots(session, limit=1)
    return rows[0] if rows else None


async def list_snapshots(session, *, limit: int = 20) -> list[ExternalSnapshot]:
    res = await session.execute(
        select(ExternalSnapshot)
        .order_by(ExternalSnapshot.retrieved_at.desc(), ExternalSnapshot.id.desc())
        .limit(limit))
    return list(res.scalars())


async def snapshot_models(session, snapshot_id: int) -> dict[str, AAModel]:
    res = await session.execute(
        select(ExternalModelMetric).where(ExternalModelMetric.snapshot_id == snapshot_id)
        .order_by(ExternalModelMetric.id))
    return {r.ext_model_slug: AAModel(
        slug=r.ext_model_slug, name=r.ext_model_name, creator=r.ext_creator,
        **{f: getattr(r, f) for f in _METRIC_FIELDS}) for r in res.scalars()}


async def get_map(session) -> dict[str, str]:
    res = await session.execute(select(ExternalModelMap))
    return {r.model: r.ext_model_slug for r in res.scalars()}


async def set_map(session, model: str, slug: str | None, *, by: str | None) -> None:
    if slug is None:
        await session.execute(delete(ExternalModelMap).where(ExternalModelMap.model == model))
    else:
        row = await session.get(ExternalModelMap, model)
        now = dt.datetime.now(dt.timezone.utc)
        if row is None:
            session.add(ExternalModelMap(model=model, ext_model_slug=slug,
                                         mapped_by=by, mapped_at=now))
        else:
            row.ext_model_slug, row.mapped_by, row.mapped_at = slug, by, now
    await session.commit()
