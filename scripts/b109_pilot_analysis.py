"""B109 Switchyard pilot -- gate analysis (protocol: docs/research/switchyard-pilot-protocol.md).

Standard library only. Reads, from one directory:
  b109-run5-results.json   DevFoundry's frozen export (per spec x arm verdicts, attempt ids)
  outcomes.csv             RouteForge routing_outcomes rows for the run window (content-free)
  advisor_latency.csv      sp.switchyard.latency_ms per arm-D call (Langfuse root spans)
  gateway-infra.json       RouteForge gateway-infra sessions (section 5e)

and prints a JSON report: per-arm metrics (section 4), the four gates (section 5) for the
primary analysis, and the pre-registered sensitivity variants (section 5e gateway-infra
included as failures; the IR-20 doc-hunk scoring note), plus the section 5g cross-traffic
report. Nothing here is tuned after seeing results: the gate thresholds are the protocol's.

Bootstrap: paired over specs (each resample draws whole specs, all four arms together),
percentile 90% intervals, B=20000, seed = the frozen plan seed 375454972.
"""
from __future__ import annotations

import csv
import json
import math
import random
import statistics
import sys
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

ARMS = ("A", "B", "C", "D")
APP = {"A": "b109-a-efficient", "B": "b109-b-capable", "C": "b109-c-router", "D": "b109-d-switchyard"}
TIER = {"qwen38-cheap-b109": "cheap", "glm-5.3-flash": "mid"}
TIER_RANK = {"cheap": 0, "mid": 1, "frontier": 2}
RISK_FLOOR = {"low": "cheap", "medium": "mid", "high": "frontier"}  # config/routing.yaml
BEST_ONPREM = "mid"  # the lab's best on-prem tier; a frontier floor clamps here (rule 8)
B = 20000
SEED = 375454972
NONINF_MARGIN = -0.10
P99_BUDGET_MS = 20.0
BIG_PROMPT = 50_000


def ts(s: str) -> datetime:
    s = s.replace("Z", "+00:00")
    if "+" not in s[10:]:
        s += "+00:00"
    if s.endswith("+00"):
        s += ":00"
    return datetime.fromisoformat(s.replace(" ", "T"))


def wilson(k: int, n: int, z: float = 1.959964) -> tuple[float, float]:
    if n == 0:
        return (float("nan"), float("nan"))
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return (round(c - h, 4), round(c + h, 4))


def pct(xs: list[float], q: float) -> float:
    xs = sorted(xs)
    i = q * (len(xs) - 1)
    lo, hi = math.floor(i), math.ceil(i)
    return xs[lo] + (xs[hi] - xs[lo]) * (i - lo)


def load(d: Path):
    exp = json.loads((d / "b109-run5-results.json").read_text())
    rows = exp["rows"]
    out = list(csv.DictReader((d / "outcomes.csv").open()))
    adv = list(csv.DictReader((d / "advisor_latency.csv").open()))
    gi = json.loads((d / "gateway-infra.json").read_text())["sessions"]
    return exp, rows, out, adv, gi


def sessions_of(r: dict) -> set[str]:
    return set(r["attempt_ids"]) | set(r["diagnosis_attempt_ids"]) | set(r["repair_attempt_ids"])


def success(r: dict, check_field: str) -> bool:
    return r["validation_attempt1"] == "passed" and r[check_field] == "passed"


def build(rows, out, check_field="scripted_check"):
    calls = defaultdict(list)
    for o in out:
        if o["application_id"].startswith("b109-") and o["session"]:
            calls[(o["application_id"], o["session"])].append(o)
    cell = {}
    for r in rows:
        cs = [c for s in sessions_of(r) for c in calls.get((APP[r["arm"]], s), [])]
        cell[(r["spec_id"], r["arm"])] = {
            "row": r,
            "success": success(r, check_field),
            "cost": sum(float(c["cost_usd"]) for c in cs),
            "calls": len(cs),
            "tokens": sum(int(c["prompt_tokens"]) + int(c["completion_tokens"]) for c in cs),
            "class": r["failure_class"],
        }
    return cell


def per_arm(cell, specs):
    res = {}
    for a in ARMS:
        cs = [cell[(s, a)] for s in specs]
        k = sum(c["success"] for c in cs)
        cost = sum(c["cost"] for c in cs)
        res[a] = {
            "n": len(cs), "successes": k, "success_rate": round(k / len(cs), 4),
            "wilson95": wilson(k, len(cs)),
            "total_cost_usd": round(cost, 6),
            "cost_per_success_usd": round(cost / k, 6) if k else None,
            "calls_per_task": round(statistics.mean(c["calls"] for c in cs), 2),
            "tokens_per_task": round(statistics.mean(c["tokens"] for c in cs)),
        }
    return res


def gates_2_3(cell, specs):
    def stats(sample):
        sd = sum(cell[(s, "D")]["success"] for s in sample)
        sc = sum(cell[(s, "C")]["success"] for s in sample)
        cd = sum(cell[(s, "D")]["cost"] for s in sample)
        cc = sum(cell[(s, "C")]["cost"] for s in sample)
        diff = (sd - sc) / len(sample)
        ratio = (cd / sd) / (cc / sc) if sd and sc and cc else float("inf")
        return diff, ratio

    point_diff, point_ratio = stats(specs)
    rng = random.Random(SEED)
    diffs, ratios = [], []
    for _ in range(B):
        sample = [specs[rng.randrange(len(specs))] for _ in specs]
        d, r = stats(sample)
        diffs.append(d)
        ratios.append(r)
    lo_diff, hi_diff = pct(diffs, 0.05), pct(diffs, 0.95)
    lo_ratio, hi_ratio = pct(ratios, 0.05), pct(ratios, 0.95)
    return {
        "gate2_noninferior": {
            "D_minus_C_pp": round(100 * point_diff, 2),
            "ci90_pp": [round(100 * lo_diff, 2), round(100 * hi_diff, 2)],
            "threshold_pp": 100 * NONINF_MARGIN,
            "pass": lo_diff > NONINF_MARGIN,
        },
        "gate3_cheaper": {
            "D_over_C_cost_per_success": round(point_ratio, 4),
            "ci90": [round(lo_ratio, 4), round(hi_ratio, 4)],
            "pass": point_ratio < 1.0 and hi_ratio < 1.0,
        },
    }


def gate1(out, d_sessions):
    v = []
    n = 0
    for o in out:
        if o["application_id"] != APP["D"] or o["session"] not in d_sessions:
            continue
        n += 1
        # Feasible set = what the router scored plus what its own tier/budget/score filters
        # set aside (rejected with code null); eligibility-stage rejections carry a code.
        # See docs/reference/routing-outcomes.md (`eligible`, `rejected`).
        elig = set(json.loads(o["eligible"] or "[]")) | {
            r["name"] for r in json.loads(o["rejected"] or "[]") if r.get("code") is None}
        att = json.loads(o["attempts"] or "[]")
        att = [a if isinstance(a, str) else a.get("model") for a in att]
        if o["chosen"] and o["chosen"] not in elig:
            v.append((o["request_id"], "chosen outside feasible set"))
        if any(a not in elig for a in att):
            v.append((o["request_id"], "attempt outside feasible set"))
        if o["onprem_only"] != "t":
            v.append((o["request_id"], "row not onprem_only (arm D policy is onprem_only)"))
        if any(m not in TIER for m in att + [o["chosen"]] if m):
            v.append((o["request_id"], "model outside the lab's on-prem pool"))
            continue
        if o["chosen"] and o["risk_class"]:
            floor = RISK_FLOOR[o["risk_class"]]
            if TIER_RANK[floor] > TIER_RANK[BEST_ONPREM]:
                floor = BEST_ONPREM
            if TIER_RANK[TIER[o["chosen"]]] < TIER_RANK[floor]:
                v.append((o["request_id"], f"below floor {floor}"))
    return {"rows_checked": n, "violations": v, "pass": not v}


def gate4(adv):
    lat = [float(a["latency_ms"]) for a in adv]
    return {
        "calls": len(lat),
        "p50_ms": round(pct(lat, 0.5), 3), "p99_ms": round(pct(lat, 0.99), 3),
        # Nearest-rank p99 (the more conservative convention); the gate uses the larger.
        "p99_nearest_rank_ms": round(sorted(lat)[math.ceil(0.99 * len(lat)) - 1], 3),
        "max_ms": round(max(lat), 3),
        "applied": sum(a["applied"] == "true" for a in adv),
        "roles": {r: sum(a["role"] == r for a in adv) for r in ("efficient", "capable")},
        "budget_ms": P99_BUDGET_MS,
        "pass": max(pct(lat, 0.99), sorted(lat)[math.ceil(0.99 * len(lat)) - 1]) <= P99_BUDGET_MS,
    }


def advisor_behaviour(out, d_sessions):
    rs = [o for o in out if o["application_id"] == APP["D"] and o["session"] in d_sessions]
    by = defaultdict(int)
    changed = 0
    for o in rs:
        by[(o["switchyard_tier"], TIER.get(o["chosen"], o["chosen"]), o["route_reason"])] += 1
        if o["switchyard_tier"] and TIER.get(o["chosen"]) != o["switchyard_tier"]:
            changed += 1
    return {
        "rows": len(rs),
        "proposed_tier__served_tier__reason": {"|".join(k): v for k, v in sorted(by.items())},
        "floor_or_clamp_changed_proposal": changed,
        "fallback_causes": sorted({o["switchyard_fallback_cause"] for o in rs} - {""}),
    }


def cross_traffic(rows, out):
    """Section 5g: per-session overhead and overlapping >=50k non-b109 calls."""
    ext = []
    w0 = min(ts(r["started_at"]) for r in rows)
    w1 = max(ts(r["ended_at"]) for r in rows)
    for o in out:
        if o["application_id"].startswith("b109-"):
            continue
        end = ts(o["ts"])
        if end < w0 or end - timedelta(milliseconds=float(o["latency_ms"])) > w1:
            continue
        ext.append((end - timedelta(milliseconds=float(o["latency_ms"])), end, int(o["prompt_tokens"]),
                    o["application_id"]))
    calls = defaultdict(list)
    for o in out:
        if o["application_id"].startswith("b109-") and o["session"]:
            calls[o["session"]].append(o)
    all_overheads = defaultdict(list)
    flagged, timed_out = [], []
    for r in rows:
        for s in sessions_of(r):
            cs = sorted(calls.get(s, []), key=lambda o: o["ts"])
            ovh = []
            for prev, nxt in zip(cs, cs[1:]):
                # Section 5g as written: the inter-call gap minus a call's own latency_ms. `ts` is
                # recorded at completion, so this is (end_next - end_prev) - latency_next, the
                # idle time between one call ending and the next starting. Negative when an
                # agent overlaps calls; those are counted and kept.
                gap = (ts(nxt["ts"]) - ts(prev["ts"])).total_seconds() - float(nxt["latency_ms"]) / 1000
                ovh.append(gap)
            all_overheads[r["arm"]].extend(ovh)
            timed = r["failure_category"] == "timeout" or "timed out" in r["failure_reason"] \
                or "failure_category=timeout" in r["failure_evidence"]
            if not timed:
                continue
            if not cs:
                continue
            s0 = ts(cs[0]["ts"]) - timedelta(milliseconds=float(cs[0]["latency_ms"]))
            s1 = ts(cs[-1]["ts"])
            big = [(a.isoformat(), p, app) for a, b, p, app in ext if a < s1 and b > s0 and p >= BIG_PROMPT]
            entry = {
                "spec": r["spec_id"], "arm": r["arm"], "session": s, "class": r["failure_class"],
                "calls": len(cs),
                "overhead_s": {"p50": round(pct(ovh, .5), 2), "p90": round(pct(ovh, .9), 2),
                               "max": round(max(ovh), 2)} if ovh else None,
                "overlapping_nonb109_calls_ge50k": len(big),
                "max_overlapping_prompt_tokens": max((p for _, p, _ in big), default=0),
            }
            timed_out.append(entry)
            if big:
                flagged.append((r["spec_id"], r["arm"]))
    return {
        "note": "inter-call gap minus the call's own latency_ms (ts is at completion): bounds gateway "
                "pre/post-processing plus client think time; negative = overlapping calls",
        "negative_gaps_by_arm": {a: sum(x < 0 for x in v) for a, v in sorted(all_overheads.items())},
        "overhead_s_by_arm": {a: {"n": len(v), "p50": round(pct(v, .5), 2), "p90": round(pct(v, .9), 2),
                                  "p99": round(pct(v, .99), 2)} for a, v in sorted(all_overheads.items()) if v},
        "nonb109_calls_in_window": len(ext),
        "nonb109_calls_ge50k": sum(p >= BIG_PROMPT for *_, p, _ in ext),
        "timed_out_sessions": timed_out,
        "flagged_spec_arm": flagged,
    }


def main(d: Path):
    exp, rows, out, adv, gi = load(d)
    specs_all = sorted({r["spec_id"] for r in rows})
    excluded = sorted({r["spec_id"] for r in rows if r["failure_class"] == "infra"})
    # Section 5a mechanical rule, recomputed from RouteForge rows: a failed task with a
    # backend_failed / no_eligible row is infra. Reported so a disagreement is visible.
    bad = defaultdict(set)
    for o in out:
        if o["application_id"].startswith("b109-") and o["status"] in ("backend_failed", "no_eligible"):
            bad[o["application_id"]].add(o["session"])
    mech = sorted({r["spec_id"] for r in rows if not success(r, "scripted_check")
                   and sessions_of(r) & bad[APP[r["arm"]]]})
    included = [s for s in specs_all if s not in excluded]
    d_sessions = set().union(*(sessions_of(r) for r in rows if r["arm"] == "D"))

    report = {
        "export_sha_note": exp.get("plan_sha256"),
        "specs": len(specs_all), "excluded_specs": excluded,
        "excluded_share": round(len(excluded) / len(specs_all), 4),
        "inconclusive_on_exclusions": len(excluded) / len(specs_all) > 0.10,
        "mechanical_infra_specs": mech,
        "mechanical_matches_export": set(mech) <= set(excluded),
        "gate1_violations": gate1(out, d_sessions),
        "gate4_advisor_latency": gate4(adv),
        "advisor_behaviour": advisor_behaviour(out, d_sessions),
    }
    d_fallback = sum(o["switchyard_fallback_cause"] in ("timeout", "error")
                     for o in out if o["application_id"] == APP["D"] and o["session"] in d_sessions)
    report["d_timeout_error_fallback_share"] = round(d_fallback / max(1, report["gate1_violations"]["rows_checked"]), 4)

    variants = {}
    # Primary (section 5a, IR-20 fails as pre-registered).
    cell = build(rows, out, "scripted_check")
    variants["primary"] = {"specs": len(included), "arms": per_arm(cell, included), **gates_2_3(cell, included)}
    # Scoring sensitivity: IR-20 out-of-scope doc hunk ignored.
    cell_s = build(rows, out, "scripted_check_sensitivity")
    variants["ir20_doc_hunk_ignored"] = {"specs": len(included), "arms": per_arm(cell_s, included),
                                         **gates_2_3(cell_s, included)}
    # Section 5e: gateway-infra specs included; each gateway-infra session scored a failure for
    # its arm; other arms on that spec as they finished.
    cell_e = build(rows, out, "scripted_check")
    gi_sessions = set(gi)
    for (spec, arm), c in cell_e.items():
        if sessions_of(c["row"]) & gi_sessions:
            c["success"] = False
    other_infra = sorted({r["spec_id"] for r in rows if r["failure_class"] == "infra"
                          and not (sessions_of(r) & gi_sessions)})
    inc_e = [s for s in specs_all if s not in other_infra]
    variants["s5e_gateway_infra_as_failure"] = {"specs": len(inc_e), "still_excluded": other_infra,
                                                "arms": per_arm(cell_e, inc_e), **gates_2_3(cell_e, inc_e)}
    report["variants"] = variants

    def verdict(v):
        if report["inconclusive_on_exclusions"]:
            return "INCONCLUSIVE (exclusions > 10%)"
        if report["d_timeout_error_fallback_share"] > 0.05:
            return "INCONCLUSIVE (D timeout/error fallbacks > 5%)"
        g = [report["gate1_violations"]["pass"], v["gate2_noninferior"]["pass"],
             v["gate3_cheaper"]["pass"], report["gate4_advisor_latency"]["pass"]]
        return "GO" if all(g) else "NO-GO or INCONCLUSIVE (a gate failed; read the intervals)"
    report["gates_all_pass"] = {k: verdict(v) for k, v in variants.items()}
    report["s5g_cross_traffic"] = cross_traffic(rows, out)
    report["manual_rows_per_arm"] = {a: sum(r["scripted_check_manual"] for r in rows if r["arm"] == a) for a in ARMS}
    print(json.dumps(report, indent=1, default=str))


if __name__ == "__main__":
    main(Path(sys.argv[1]))
