"""Transparent, rule-based recommendation heuristics.

Every rule is a pure function `(Aggregates, RoutingCfg) -> Recommendation |
None`: it inspects the metering aggregates plus the CURRENT routing.yaml
knobs, and — only when its documented trigger condition holds — yields one
`Recommendation`. No ML, no model calls: these are boring if/then heuristics
with a threshold you can read (CLAUDE.md rule 4 — "mine -> recommend", never
"train -> promote"). Thresholds are module constants, not magic numbers
scattered inline.

A `Recommendation.field_path` is either a real dotted `config/routing.yaml`
path (e.g. "complexity_thresholds.mid_max" — `report.py` can turn its delta
into a proposed `routing_policy_version`), or an advisory, non-config path
(e.g. "infra.onprem_capacity" for an onboarding nudge — never a routing.yaml
value, so it's never folded into the proposed policy version). A rule may
also fire with `current_value == recommended_value` — a "review this" flag
with no concrete change to propose; `report.py` treats that as no delta.

Known instrumentation gap: `NoEligibleCandidate` (the onprem_only-with-no-
candidate reject) is not currently persisted to `UsageRecord` — only the
daily-budget reject path is, and it hardcodes `onprem_only=False`
(app/api/routes.py). `onprem_only_reject_share` is therefore computed
honestly from whatever the table holds today (likely 0 in production until
that gap is closed) rather than fabricated — see CLAUDE.md rule 5 (fail
closed / honest, never silently overclaim).
"""
from __future__ import annotations

from typing import Any, Callable

from pydantic import BaseModel

from app.config.schema import RoutingCfg
from tools.recommend.aggregate import Aggregates

# --- thresholds (documented, not magic numbers) -----------------------------

# Ignore a tier/bucket with fewer than this many requests — too small to be a
# meaningful signal, and we'd rather stay silent than emit noise (rule 5).
MIN_TIER_SAMPLE = 5
MIN_TIER_TOTAL_FOR_ERROR_RULE = 5
MIN_NON_OK_FOR_ONPREM_RULE = 5

# Rule 1 — frontier over-utilization.
FRONTIER_SHARE_TRIGGER = 0.30          # frontier carries > 30% of OK traffic
FRONTIER_MID_QUALITY_GAP = 0.05        # ...while mid trails frontier by <= this
MID_MAX_STEP = 0.05                    # nudge complexity_thresholds.mid_max up by
MID_MAX_CEILING = 0.95                 # never recommend past this

# Rule 2 — cheap under-utilization.
CHEAP_SHARE_TRIGGER = 0.20             # cheap carries < 20% of OK traffic
CHEAP_MAX_STEP = 0.05                  # nudge complexity_thresholds.cheap_max up by
CHEAP_MAX_CEILING = 0.60               # never recommend past this

# Rule 3 — high per-tier error/reject rate.
TIER_ERROR_RATE_TRIGGER = 0.05         # a tier's error_rate above 5% is worth a look

# Rule 4 — onprem_only reject pressure.
ONPREM_REJECT_SHARE_TRIGGER = 0.10     # >10% of non-OK rows were onprem_only


class Recommendation(BaseModel):
    field_path: str
    current_value: Any
    recommended_value: Any
    rationale: str
    evidence: str


def frontier_overutilization(agg: Aggregates, routing: RoutingCfg) -> Recommendation | None:
    """A large share of traffic lands on frontier while mid answers nearly as
    well — most of that spend could move to mid. Recommends widening the mid
    band (raising `complexity_thresholds.mid_max`)."""
    frontier = agg.tiers.get("frontier")
    mid = agg.tiers.get("mid")
    if frontier is None or mid is None:
        return None
    if frontier.ok_count < MIN_TIER_SAMPLE or mid.ok_count < MIN_TIER_SAMPLE:
        return None
    if frontier.share <= FRONTIER_SHARE_TRIGGER:
        return None
    if frontier.avg_quality - mid.avg_quality > FRONTIER_MID_QUALITY_GAP:
        return None
    current = routing.complexity_thresholds.get("mid_max", 0.0)
    recommended = min(MID_MAX_CEILING, round(current + MID_MAX_STEP, 4))
    if recommended <= current:
        return None
    return Recommendation(
        field_path="complexity_thresholds.mid_max",
        current_value=current,
        recommended_value=recommended,
        rationale=(
            "Frontier tier carries a large share of traffic while mid-tier "
            "quality is close to frontier's — widening the mid band should "
            "shift some of that traffic to a cheaper tier at little quality "
            "cost."),
        evidence=(
            f"frontier_share={frontier.share:.2f} (> {FRONTIER_SHARE_TRIGGER}), "
            f"frontier_avg_quality={frontier.avg_quality:.3f}, "
            f"mid_avg_quality={mid.avg_quality:.3f} "
            f"(gap={frontier.avg_quality - mid.avg_quality:.3f} "
            f"<= {FRONTIER_MID_QUALITY_GAP}), n_frontier={frontier.ok_count}, "
            f"n_mid={mid.ok_count}"),
    )


def cheap_underutilization(agg: Aggregates, routing: RoutingCfg) -> Recommendation | None:
    """Cheap tier is barely used, but the requests that DO land there score at
    or above cheap's own quality rung — there's headroom to route more there.
    Recommends widening the cheap band (raising
    `complexity_thresholds.cheap_max`)."""
    cheap = agg.tiers.get("cheap")
    if cheap is None or cheap.ok_count < MIN_TIER_SAMPLE:
        return None
    if cheap.share >= CHEAP_SHARE_TRIGGER:
        return None
    cheap_rung = routing.tier_quality.get("cheap")
    if cheap_rung is None or cheap.avg_quality < cheap_rung:
        return None
    current = routing.complexity_thresholds.get("cheap_max", 0.0)
    recommended = min(CHEAP_MAX_CEILING, round(current + CHEAP_MAX_STEP, 4))
    if recommended <= current:
        return None
    return Recommendation(
        field_path="complexity_thresholds.cheap_max",
        current_value=current,
        recommended_value=recommended,
        rationale=(
            "Cheap tier is under-utilized, but requests already landing there "
            "meet or beat its quality rung — widening the cheap band should "
            "capture more traffic at the lowest tier without a quality "
            "regression."),
        evidence=(
            f"cheap_share={cheap.share:.2f} (< {CHEAP_SHARE_TRIGGER}), "
            f"cheap_avg_quality={cheap.avg_quality:.3f} "
            f"(>= tier_quality.cheap={cheap_rung:.3f}), n_cheap={cheap.ok_count}"),
    )


def high_tier_error_rate(agg: Aggregates, routing: RoutingCfg) -> Recommendation | None:
    """A tier's error/reject rate is elevated — flag it for review. This is a
    NON-value recommendation: current_value == recommended_value (nothing
    concrete to change), just a pointer at `fallback`/`max_escalations` and
    the evidence that triggered it.

    Only considers real, configured tiers (`routing.tiers`) — `agg.tiers` can
    also carry synthetic non-tier bucket keys such as `"-"` (the reject
    bucket `UsageRecord.tier` is written as for budget rejects) or `"?"`;
    those aren't routing tiers and must never be recommended for review."""
    worst: Any = None
    for tier in sorted(agg.tiers):
        if tier not in routing.tiers:
            continue
        stats = agg.tiers[tier]
        if stats.total_count < MIN_TIER_TOTAL_FOR_ERROR_RULE:
            continue
        if stats.error_rate <= TIER_ERROR_RATE_TRIGGER:
            continue
        if worst is None or stats.error_rate > worst.error_rate:
            worst = stats
    if worst is None:
        return None
    return Recommendation(
        field_path=f"fallback (tier={worst.tier})",
        current_value=routing.fallback,
        recommended_value=routing.fallback,  # review-only: no concrete delta
        rationale=(
            f"Tier '{worst.tier}' has an elevated error/reject rate. The "
            "current fallback/escalation settings may not be recovering from "
            "backend failures on this tier — review `fallback`, "
            "`max_escalations`, and the health of the endpoints serving this "
            "tier."),
        evidence=(
            f"tier={worst.tier} error_rate={worst.error_rate:.2f} "
            f"(> {TIER_ERROR_RATE_TRIGGER}) over {worst.total_count} requests "
            f"({worst.error_count} error/rejected)"),
    )


def onprem_capacity_pressure(agg: Aggregates, routing: RoutingCfg) -> Recommendation | None:
    """A meaningful share of rejected/errored requests were flagged
    onprem_only — on-prem capacity looks like a binding constraint. This is
    an advisory recommendation, NOT a routing.yaml value: it points at
    onboarding more on-prem capacity (docs/onboarding-models.md / Task 2's
    trigger hook), which is out of this job's scope by design (rule 4)."""
    non_ok = agg.n_rejected + agg.n_error
    if non_ok < MIN_NON_OK_FOR_ONPREM_RULE:
        return None
    if agg.onprem_only_reject_share <= ONPREM_REJECT_SHARE_TRIGGER:
        return None
    return Recommendation(
        field_path="infra.onprem_capacity",
        current_value=None,
        recommended_value="onboard additional on-prem capacity",
        rationale=(
            "A meaningful share of rejected/errored requests were flagged "
            "onprem_only, suggesting on-prem capacity is a binding "
            "constraint. This is not a routing.yaml value — onboard more "
            "on-prem endpoints (see docs/onboarding-models.md)."),
        evidence=(
            f"onprem_only_reject_share={agg.onprem_only_reject_share:.2f} "
            f"(> {ONPREM_REJECT_SHARE_TRIGGER}) over {non_ok} non-OK requests"),
    )


ALL_RULES: list[Callable[[Aggregates, RoutingCfg], Recommendation | None]] = [
    frontier_overutilization,
    cheap_underutilization,
    high_tier_error_rate,
    onprem_capacity_pressure,
]


def run_rules(agg: Aggregates, routing: RoutingCfg) -> list[Recommendation]:
    """Run every rule and collect the ones that fired, in a stable order."""
    out: list[Recommendation] = []
    for rule in ALL_RULES:
        rec = rule(agg, routing)
        if rec is not None:
            out.append(rec)
    return out
