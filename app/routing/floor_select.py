"""B80: which candidates qualify under a quality floor (spec §4.4 steps 3-4).

Pure: no settings, no registry, no I/O. The router hands in the feasible set
(`allowed`, already residency/scope filtered -- rule 3), the clamped hard tier
floor (rule 8) and two callables, and gets back the qualifying set
cost-ascending plus why every other member was refused. The router alone
decides what to do with the answer (`Router._select_floor`).
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from app.config.schema import PoolCandidate

# The one tier ordering; app/routing/router.py imports it as `_ORDER`.
_TIER_ORDER = {"cheap": 0, "mid": 1, "frontier": 2}

BELOW_QUALITY_FLOOR = "below quality floor"
BELOW_RISK_FLOOR = "below risk floor"
CONTEXT_TOO_SMALL = "context window too small"
OVER_BUDGET = "over budget"


@dataclass(frozen=True)
class FloorPick:
    # Candidates passing every filter, cheapest first (stable on ties).
    qualifiers: list[tuple[PoolCandidate, float]]
    # met | unmet_no_qualifier | unmet_context | unmet_budget
    status: str
    # name -> the FIRST filter that member failed, for every non-qualifier.
    rejections: dict[str, str]
    # name -> (lower, source) for every member of `allowed`; (None, None)
    # when unknown. Content-free.
    bounds: dict[str, tuple[float | None, str | None]]


def qualify(allowed: list[PoolCandidate], *, floor: float, workload: str,
            hard_tier: str, prompt_tokens: int, output_tokens: int,
            max_cost_usd: float,
            est_cost: Callable[[PoolCandidate], float],
            lower: Callable[[str], tuple[float | None, str | None]]) -> FloorPick:
    """Filter `allowed` in spec order: tier >= `hard_tier` and a known lower
    bound >= `floor` (step 3), declared context window fits (3b), estimated
    cost within the cap (4). An unknown bound never qualifies; an undeclared
    window is assumed to fit. `status` names the step that emptied the set.

    `lower` is already bound to the request's `workload`; the slug is taken
    here only so the signature states which workload the bounds are for."""
    bounds = {c.name: lower(c.name) for c in allowed}
    rejections: dict[str, str] = {}
    need = prompt_tokens + output_tokens

    step3: list[PoolCandidate] = []
    for c in allowed:
        lb, _src = bounds[c.name]
        if _TIER_ORDER[c.tier] < _TIER_ORDER[hard_tier]:
            rejections[c.name] = BELOW_RISK_FLOOR
        elif lb is None or lb < floor:
            rejections[c.name] = BELOW_QUALITY_FLOOR
        else:
            step3.append(c)

    step3b: list[PoolCandidate] = []
    for c in step3:
        if c.context_window is not None and c.context_window < need:
            rejections[c.name] = CONTEXT_TOO_SMALL
        else:
            step3b.append(c)

    qualifiers: list[tuple[PoolCandidate, float]] = []
    for c in step3b:
        est = est_cost(c)
        if est > max_cost_usd:
            rejections[c.name] = OVER_BUDGET
        else:
            qualifiers.append((c, est))
    qualifiers.sort(key=lambda ce: ce[1])

    if not step3:
        status = "unmet_no_qualifier"
    elif not step3b:
        status = "unmet_context"
    elif not qualifiers:
        status = "unmet_budget"
    else:
        status = "met"
    return FloorPick(qualifiers=qualifiers, status=status,
                     rejections=rejections, bounds=bounds)
