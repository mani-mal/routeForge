"""Switchyard advisor shadow report (B105): offline, operator-run.

Reads the content-free `routing_outcomes` rows a `routing.switchyard.mode:
shadow` deployment wrote and says, before anyone turns enforce on (B108):

1. **Agreement.** Per app and per workload, how often the advisor's proposed
   tier equalled the served tier (`switchyard_agreed`), its role split
   (efficient / capable), why it sometimes made no proposal
   (`switchyard_fallback_cause`), and how many rows carry no advice at all.
2. **Counterfactual cost of following it.** Each advised row is replayed
   through the real `feasible_set` -> `Router.select` twice on the snapshot
   as exported: as recorded (the baseline) and with the advisor's tier
   standing in for the length-derived target (`simulate_row(advisor_tier=)`).
   The risk floor, `min_tier` and the best-allowed clamp apply to it exactly
   as live (design §2): an `efficient` proposal on a high-risk row stays at
   the floor, a `capable` one on an `onprem_only` row clamps to the best
   on-prem tier. Nothing in the live router changed to allow this.

**This is a fixed-feature replay.** It predicts which tier and model, and
what cost, the router would have chosen on the SAME recorded features. It
does not predict task success or answer quality -- the advisor trades cost
against quality and this report cannot see the quality side (B109). Reads
only content-free columns (ids, enums, counts, a number, a bool).
"""
from __future__ import annotations

import hashlib
from collections import Counter

from app.config.schema import Settings
from app.eval.outcomes import UNTAGGED_WORKLOAD, _fmt, _round6
from app.eval.scenario import build_scenario, close_scenario
from app.eval.simulate import (
    ArmResult, RowResult, _baseline, _excluded_lines, _rate, observed_latencies, rows_block,
    select_rows, simulate_row, _before_since,
)
from app.eval.uncertainty import (
    UncertaintyPlan, draw_indices, draw_pool, leaf, level_text, plan_block,
)
from app.routing.floor_select import _TIER_ORDER

SCHEMA = "routeforge.switchyard-report/v1"
ROLES = ("efficient", "capable")

REPORT_CAVEAT = (
    "**Fixed-feature replay, not an outcome study.** This report replays recorded requests\n"
    "through the real eligibility and routing code on a frozen snapshot, once as recorded and\n"
    "once with the Switchyard advisor's tier standing in for the length-derived tier target (the\n"
    "risk floor, escalation and residency clamp still apply). It predicts which tier and model,\n"
    "and what cost, would have been chosen on the same features. It does NOT predict task\n"
    "success or answer quality, so a cheaper counterfactual is not a better one. Nothing here\n"
    "has been applied."
)


def _bool(v) -> bool | None:
    return v if isinstance(v, bool) else None


def _proposal(row: dict) -> str | None:
    """The advisor's proposed tier, or None when the row has no usable one
    (advisor off, fell back, or a row from before the columns existed)."""
    tier = row.get("switchyard_tier")
    return tier if isinstance(tier, str) and tier in _TIER_ORDER else None


def _recorded_cost(row: dict) -> float | None:
    c = row.get("cost_usd")
    return float(c) if isinstance(c, (int, float)) and not isinstance(c, bool) else None


def _agreement_block(rows: list[dict]) -> dict:
    advised = [r for r in rows if _proposal(r)]
    served = [r for r in advised if _bool(r.get("switchyard_agreed")) is not None]
    agreed = sum(1 for r in served if r["switchyard_agreed"])
    roles = Counter(r.get("switchyard_role") for r in advised)
    causes = Counter(r["switchyard_fallback_cause"] for r in rows
                     if not _proposal(r) and isinstance(r.get("switchyard_fallback_cause"), str))
    fallback = sum(causes.values())
    no_advice = len(rows) - len(advised)
    return {
        "rows": len(rows),
        "advised": len(advised),
        "advised_served": len(served),
        "agreed": agreed,
        "disagreed": len(served) - agreed,
        "agreement": agreed / len(served) if served else None,
        "role": {**{role: roles.get(role, 0) for role in ROLES},
                 "other": sum(n for k, n in roles.items() if k not in ROLES)},
        "fallback_cause": dict(sorted(causes.items())),
        "without_advice": {"total": no_advice, "fallback": fallback,
                           "not_run": no_advice - fallback},
    }


def _counterfactual_block(items: list[tuple[dict, ArmResult, ArmResult]]) -> dict:
    """`items` are `(row, baseline arm, advisor arm)` for advised rows."""
    both = [(r, b, a) for r, b, a in items if b.outcome == "routed" and a.outcome == "routed"]
    base_cost = sum(b.cost_usd or 0.0 for _r, b, _a in both)
    adv_cost = sum(a.cost_usd or 0.0 for _r, _b, a in both)
    recorded = [_recorded_cost(r) for r, _b, _a in both]
    delta = adv_cost - base_cost
    moved = Counter()
    for _r, b, a in both:
        d = _TIER_ORDER[a.tier] - _TIER_ORDER[b.tier]
        moved["lower_tier" if d < 0 else "higher_tier" if d > 0 else "same_tier"] += 1
    return {
        "replayed": len(items),
        "both_routed": len(both),
        "newly_rejected": sum(1 for _r, b, a in items
                              if b.outcome == "routed" and a.outcome == "rejected"),
        "newly_served": sum(1 for _r, b, a in items
                            if b.outcome == "rejected" and a.outcome == "routed"),
        "cost_usd": {"baseline": base_cost, "advisor": adv_cost, "delta": delta,
                     "delta_share": delta / base_cost if base_cost > 0 else None},
        "recorded_cost_usd": (sum(recorded) if recorded and None not in recorded else None),
        "tier_vs_baseline": {k: moved.get(k, 0)
                             for k in ("lower_tier", "same_tier", "higher_tier")},
        "model_changed": sum(1 for _r, b, a in both if b.model != a.model),
        # The clamp, made visible: how often the floor/escalation/residency
        # kept the served tier away from what the advisor named.
        "held_above_proposal": sum(
            1 for r, _b, a in items
            if a.outcome == "routed" and _TIER_ORDER[a.tier] > _TIER_ORDER[_proposal(r)]),
        "clamped_below_proposal": sum(
            1 for r, _b, a in items
            if a.outcome == "routed" and _TIER_ORDER[a.tier] < _TIER_ORDER[_proposal(r)]),
    }


def _bucket(items: list[tuple[dict, ArmResult, ArmResult | None]]) -> dict:
    rows = [r for r, _b, _a in items]
    out = {"agreement": _agreement_block(rows),
           "counterfactual": _counterfactual_block(
               [(r, b, a) for r, b, a in items if a is not None])}
    return out


# --- ranges (reuses the simulator's bootstrap primitives) -----------------------

def _boot(values: list[tuple[str, float]], plan: UncertaintyPlan, stat) -> tuple[
        list[float] | None, dict[str, list[float]]]:
    """Stratified resamples of `(app, value)` pairs: an app with at least
    `plan.min_rows` values is its own stratum (`draw_indices`), the rest are
    drawn together (`draw_pool`) for the totals only. `stat` reduces a list
    of values. Totals are None below `min_rows` values in all."""
    if len(values) < plan.min_rows:
        return None, {}
    by_app: dict[str, list[float]] = {}
    for app, v in values:
        by_app.setdefault(app, []).append(v)
    own = sorted(a for a, vs in by_app.items() if len(vs) >= plan.min_rows)
    pooled = [v for a in sorted(by_app) if a not in own for v in by_app[a]]
    totals: list[float] = []
    per_app: dict[str, list[float]] = {a: [] for a in own}
    for b in range(plan.replicates):
        drawn: list[float] = []
        for a in own:
            vs = by_app[a]
            part = [vs[i] for i in draw_indices(plan.seed, b, a, len(vs))]
            per_app[a].append(stat(part))
            drawn += part
        if pooled:
            drawn += [pooled[i] for i in draw_pool(plan.seed, b, len(pooled))]
        totals.append(stat(drawn))
    return totals, per_app


def _mean(vs: list[float]) -> float:
    return sum(vs) / len(vs)


def _ranges(infos: list[dict], plan: UncertaintyPlan) -> dict:
    """Ranges for the two headline figures, totals and per app: the cost
    delta (sum over rows routed in both replays) and the agreement rate."""
    delta_pts = [(i["app"], i["delta"]) for i in infos if i["delta"] is not None]
    agree_pts = [(i["app"], 1.0 if i["agreed"] else 0.0) for i in infos
                 if i["agreed"] is not None]
    out: dict = {"cost_usd_delta": {"totals": None, "by_app": {}},
                 "agreement": {"totals": None, "by_app": {}}}
    for name, pts, stat in (("cost_usd_delta", delta_pts, sum), ("agreement", agree_pts, _mean)):
        totals, per_app = _boot(pts, plan, stat)
        point_of = lambda sel: stat([v for a, v in pts if sel(a)])  # noqa: E731
        if totals is None:
            out[name]["totals"] = {"insufficient_data": True, "n": len(pts)}
            continue
        out[name]["totals"] = leaf(point_of(lambda a: True), totals, plan)
        out[name]["by_app"] = {a: leaf(point_of(lambda x, a=a: x == a), s, plan)
                               for a, s in sorted(per_app.items())}
    return out


# --- the replay -----------------------------------------------------------------

async def advisor_report(records: list[dict], snapshot: dict, *, base: Settings,
                         inputs: dict, since=None,
                         plan: UncertaintyPlan | None = None) -> dict:
    """The deterministic `switchyard.json` document. Builds one scenario from
    `snapshot`, replays every selectable row once as recorded and every
    advised row once more with the advisor's tier, closes the scenario."""
    scn = await build_scenario(snapshot, base=base)
    try:
        observed = observed_latencies([r for r in records if not _before_since(r, since)])
        rows, excluded = select_rows(records, set(scn.apps), since=since)
        items: list[tuple[dict, ArmResult, ArmResult | None]] = []
        for row in rows:
            baseline = simulate_row(scn, row, observed)
            tier = _proposal(row)
            items.append((row, baseline,
                          simulate_row(scn, row, observed, advisor_tier=tier)
                          if tier else None))
    finally:
        await close_scenario(scn)

    def group(key) -> dict:
        labels = sorted({key(r) for r, _b, _a in items})
        return {lab: _bucket([it for it in items if key(it[0]) == lab]) for lab in labels}

    modes = Counter(r.get("switchyard_mode") for r, _b, _a in items
                    if isinstance(r.get("switchyard_mode"), str))
    full_inputs = dict(inputs)
    doc = {
        "schema": SCHEMA,
        "kind": "fixed_feature_replay",
        "report_id": hashlib.sha256(
            repr(sorted(full_inputs.items())).encode("utf-8")).hexdigest()[:16],
        "inputs": full_inputs,
        "rows": rows_block(rows, excluded),
        "modes": dict(sorted(modes.items())),
        # Replay fidelity of the BASELINE arm against the record, over every
        # simulated row -- how far to trust the counterfactual's baseline.
        "baseline": _baseline([RowResult(r, b, b) for r, b, _a in items]),
        "totals": _bucket(items),
        "by_app": group(lambda r: r["application_id"]),
        "by_workload": group(lambda r: r.get("workload") or UNTAGGED_WORKLOAD),
    }
    if plan is not None:
        infos = []
        for r, b, a in items:
            routed = a is not None and b.outcome == "routed" and a.outcome == "routed"
            infos.append({"app": r["application_id"],
                          "delta": (a.cost_usd or 0.0) - (b.cost_usd or 0.0) if routed else None,
                          "agreed": _bool(r.get("switchyard_agreed"))
                          if _proposal(r) else None})
        doc["uncertainty"] = {**plan_block(plan), **_ranges(infos, plan)}
    return _round6(doc)


# --- rendering ------------------------------------------------------------------

def _cell(point: str, node: dict | None) -> str:
    if node is None:
        return point
    if node.get("insufficient_data"):
        return f"{point} [insufficient data, n = {node['n']}]"
    if node.get("lo") is None:
        return f"{point} [n/a]"
    return f"{point} [{_fmt(node['lo'])}, {_fmt(node['hi'])}]"


def _bucket_lines(b: dict, ranges: dict | None, app: str | None) -> list[str]:
    ag, cf = b["agreement"], b["counterfactual"]
    rng = lambda name: (None if ranges is None else  # noqa: E731
                        (ranges[name]["totals"] if app is None
                         else ranges[name]["by_app"].get(app)))
    wa = ag["without_advice"]
    lines = [
        f"rows: {ag['rows']} &middot; advised: {ag['advised']} "
        f"&middot; without advice: {wa['total']} (fallback {wa['fallback']}, "
        f"advisor not run {wa['not_run']})",
        "",
        "| metric | value |",
        "|---|---|",
        f"| agreement (proposed tier = served tier) | "
        f"{_cell(_rate(ag['agreement']), rng('agreement'))} "
        f"({ag['agreed']} of {ag['advised_served']}) |",
        f"| role efficient / capable / other | "
        f"{ag['role']['efficient']} / {ag['role']['capable']} / {ag['role']['other']} |",
        f"| fallback causes | {ag['fallback_cause'] or 'none'} |",
        f"| counterfactual rows (replayed / routed in both) | "
        f"{cf['replayed']} / {cf['both_routed']} |",
        f"| cost_usd served (replayed baseline) | {_fmt(cf['cost_usd']['baseline'])} |",
        f"| cost_usd following advisor | {_fmt(cf['cost_usd']['advisor'])} |",
        f"| cost_usd delta | {_cell(_fmt(cf['cost_usd']['delta']), rng('cost_usd_delta'))} "
        f"({_rate(cf['cost_usd']['delta_share'])} of baseline) |",
        f"| tier vs baseline lower / same / higher | "
        f"{cf['tier_vs_baseline']['lower_tier']} / {cf['tier_vs_baseline']['same_tier']} / "
        f"{cf['tier_vs_baseline']['higher_tier']} |",
        f"| held above proposal / clamped below proposal | "
        f"{cf['held_above_proposal']} / {cf['clamped_below_proposal']} |",
        f"| newly rejected / newly served | {cf['newly_rejected']} / {cf['newly_served']} |",
    ]
    return lines


def render_advisor_report(doc: dict) -> str:
    """The `switchyard.md` document: caveat, replay fidelity, totals, by app,
    by workload, excluded rows."""
    lines = ["> " + line for line in REPORT_CAVEAT.split("\n")]
    lines.append("")
    base = doc["baseline"]
    lines.append(f"Baseline replay agreement with the record: model {_rate(base['agreement'])} "
                 f"over {base['rows_compared']} compared rows; outcome "
                 f"{_rate(base['outcome_agreement'])} over "
                 f"{base['outcome_rows_compared']} simulated rows.")
    lines.append("")
    if base["warning"] or base["outcome_warning"]:
        lines += ["> **Warning:** the baseline replay disagrees with the record on more than "
                  "10% of rows. Treat the counterfactual cost as noisier than usual.", ""]
    unc = doc.get("uncertainty")
    if unc:
        lines += [f"Ranges: {level_text(unc['interval'])} percentile intervals from "
                  f"{unc['replicates']} bootstrap resamples of the replayed rows, within each "
                  f"application (seed {unc['seed']}). Sampling variability only; they do not "
                  "cover replay error or future traffic shift.", ""]
    lines += ["## Totals", ""] + _bucket_lines(doc["totals"], unc, None) + [""]
    lines += ["## By app", ""]
    for app in sorted(doc["by_app"]):
        lines += [f"### {app}", ""] + _bucket_lines(doc["by_app"][app], unc, app) + [""]
    lines += ["## By workload", ""]
    for wl in sorted(doc["by_workload"]):
        lines += [f"### {wl}", ""] + _bucket_lines(doc["by_workload"][wl], None, None) + [""]
    lines += ["## Excluded rows", ""]
    lines += _excluded_lines(doc["rows"]["excluded"])
    return "\n".join(lines) + "\n"
