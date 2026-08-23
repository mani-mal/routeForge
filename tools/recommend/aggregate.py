"""Pure aggregation over UsageRecord-shaped rows.

No DB, no I/O, no `app.*` imports — `aggregate()` only ever reads attributes
off whatever it's handed (a real `UsageRecord` ORM row, a plain dataclass, or
a dict), so it's testable with lightweight fixtures and safe to reuse from
any future mining entry point. Mines ONLY the local, redaction-safe
`UsageRecord` metering table (model/provider/tier/tokens/quality/cost/
latency/sensitivity/onprem_only/status/ts) — never raw prompt/completion
text, never a Langfuse fetch (CLAUDE.md rule 2).
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Any, Iterable


def _get(row: Any, name: str) -> Any:
    """Read one UsageRecord-shaped field off `row`. `row` may be a dict, a
    plain dataclass, or a real `UsageRecord` ORM instance — this is the only
    place that cares which."""
    if isinstance(row, dict):
        return row[name]
    return getattr(row, name)


@dataclass(frozen=True)
class TierStats:
    tier: str
    ok_count: int
    # ok_count / n_ok over the whole window. 0.0 when there are no OK rows.
    share: float
    avg_quality: float
    avg_cost_usd: float
    avg_latency_ms: float
    # rows with this tier value and status != "ok" (rejects/errors that were
    # attributed to this tier — see the module docstring in rules.py for the
    # current instrumentation gap on rejects raised before a tier is chosen).
    error_count: int
    total_count: int  # ok_count + error_count
    error_rate: float  # error_count / total_count. 0.0 when total_count == 0.


@dataclass(frozen=True)
class Aggregates:
    since: dt.datetime | None
    until: dt.datetime | None
    n_requests: int  # every row in the window, any status
    n_ok: int
    n_rejected: int
    n_error: int
    # (n_rejected + n_error) / n_requests. 0.0 when n_requests == 0.
    overall_reject_error_rate: float
    # share of OK rows with onprem_only True — on-prem demand pressure.
    onprem_only_share: float
    # share of non-OK (rejected/error) rows with onprem_only True. 0.0 when
    # there are no non-OK rows.
    onprem_only_reject_share: float
    tiers: dict[str, TierStats]


def _avg(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


_EMPTY = Aggregates(
    since=None, until=None, n_requests=0, n_ok=0, n_rejected=0, n_error=0,
    overall_reject_error_rate=0.0, onprem_only_share=0.0,
    onprem_only_reject_share=0.0, tiers={},
)


def aggregate(rows: Iterable[Any]) -> Aggregates:
    """Turn a list of UsageRecord-shaped rows into metering aggregates: per-
    tier counts/shares/avg-quality/avg-cost/avg-latency/error-rate, overall
    reject/error rate, onprem_only shares, total request count, and the time
    window covered. Empty input returns a well-defined empty Aggregates."""
    rows = list(rows)
    n_requests = len(rows)
    if n_requests == 0:
        return _EMPTY

    timestamps = [_get(r, "ts") for r in rows]
    ok_rows = [r for r in rows if _get(r, "status") == "ok"]
    rejected_rows = [r for r in rows if _get(r, "status") == "rejected"]
    error_rows = [r for r in rows if _get(r, "status") == "error"]
    non_ok_rows = rejected_rows + error_rows

    n_ok = len(ok_rows)

    onprem_only_share = _avg([1.0 if _get(r, "onprem_only") else 0.0 for r in ok_rows])
    onprem_only_reject_share = _avg(
        [1.0 if _get(r, "onprem_only") else 0.0 for r in non_ok_rows])

    tier_names = {_get(r, "tier") for r in rows}
    tiers: dict[str, TierStats] = {}
    for tier in sorted(tier_names):
        tier_ok = [r for r in ok_rows if _get(r, "tier") == tier]
        tier_non_ok = [r for r in non_ok_rows if _get(r, "tier") == tier]
        ok_count = len(tier_ok)
        error_count = len(tier_non_ok)
        total_count = ok_count + error_count
        tiers[tier] = TierStats(
            tier=tier,
            ok_count=ok_count,
            share=(ok_count / n_ok) if n_ok else 0.0,
            avg_quality=_avg([float(_get(r, "quality")) for r in tier_ok]),
            avg_cost_usd=_avg([float(_get(r, "cost_usd")) for r in tier_ok]),
            avg_latency_ms=_avg([float(_get(r, "latency_ms")) for r in tier_ok]),
            error_count=error_count,
            total_count=total_count,
            error_rate=(error_count / total_count) if total_count else 0.0,
        )

    return Aggregates(
        since=min(timestamps),
        until=max(timestamps),
        n_requests=n_requests,
        n_ok=n_ok,
        n_rejected=len(rejected_rows),
        n_error=len(error_rows),
        overall_reject_error_rate=(len(rejected_rows) + len(error_rows)) / n_requests,
        onprem_only_share=onprem_only_share,
        onprem_only_reject_share=onprem_only_reject_share,
        tiers=tiers,
    )
