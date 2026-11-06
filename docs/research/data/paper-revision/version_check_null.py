"""Synthetic null experiment for the router-version check (feasible-route-bench.md 22.7).

Rebuilds the table in section 22.7 (whose original script was never committed).
Two arms are scored from ONE shared per-case distribution, so there is no true
difference; the decision rules are then applied to the paired comparison.

  * NEW rule  = the shipped `version_check` code. Per figure: `decide`; overall:
    `verdict_of`. Resampling uses the shipped `draw_indices` and `interval_of`.
    The per-resample means are computed with numpy for speed; the first
    `--verify` draws of every row are ALSO run through the shipped `run_check`
    and must agree on figure count, verdict and number of regressed figures
    (the script aborts otherwise).
  * FIRST rule = `first_rule_regressed` below: one line, a figure is regressed
    when its point estimate w is beyond its tolerance (range ignored).
  * BONFERRONI (NOT shipped): interval raised to 1 - 2*0.05/m for m judged
    figures, resamples raised to the minimum that level needs.

Run from the repo root:
    LITELLM_LOCAL_MODEL_COST_MAP=True uv run python docs/research/data/paper-revision/version_check_null.py
"""
from __future__ import annotations

import argparse
import asyncio
import math
import os
import sys
import tempfile
import time
from pathlib import Path

import numpy as np
import yaml

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT))

from app.eval import runner, version_check as vc  # noqa: E402
from app.eval.candidate import SCHEMA  # noqa: E402
from app.eval.cases import load_suite  # noqa: E402
from app.eval.groups import DIMENSIONS, case_keys, partition  # noqa: E402
from app.eval.oracle import CaseScore, _mean  # noqa: E402
from app.eval.uncertainty import MAX_REPLICATES, draw_indices, interval_of  # noqa: E402

METRICS = vc.CHECK_METRICS                     # 8 metrics, report order
FIELD = [vc._SCORE_FIELD[m] for m in METRICS]
ALL8 = {m: 0.0 for m in METRICS}
# (n, f, tolerances, published row) ; published = (figures, first, new_reg, new_inc, new_pass)
SETTINGS = [
    (22, 1.0, {"pass_rate": 0.05}, (14, 90.3, 24.7, 74.3, 1.0)),
    (22, 1.0, {"pass_rate": 0.10, "mean_chosen_cost": 0.25}, (28, 97.3, 30.7, 69.3, 0.0)),
    (22, 1.0, ALL8, (102, 100.0, 82.7, 17.3, 0.0)),
    (22, 0.3, {"pass_rate": 0.05}, (14, 66.3, 2.0, 66.7, 31.3)),
    (22, 0.3, {"pass_rate": 0.10, "mean_chosen_cost": 0.25}, (28, 73.0, 0.3, 92.7, 7.0)),
    (22, 0.3, ALL8, (108, 100.0, 55.0, 45.0, 0.0)),
    (200, 1.0, {"pass_rate": 0.05}, (14, 74.7, 9.3, 90.7, 0.0)),
]
COST_UNIT = 1000.0   # chosen_cost etc. expressed in USD per 1,000 requests (see README)


def first_rule_regressed(w: float | None, tol: float) -> bool:
    """The rule the module shipped with: the point estimate alone fails a figure."""
    return w is not None and round(w, 12) > tol


# --- the shared per-case distribution ---------------------------------------------

def real_pool() -> tuple[list[CaseScore], list, dict]:
    """Per-case scores of an actual simulated bench of the 22-case starter suite
    (router_online arm, healthcare-sim endpoints), captured from the runner."""
    cap: dict = {}
    orig = runner.run_check

    def spy(b, c, keys, *a, **k):
        cap["b"] = b
        return orig(b, c, keys, *a, **k)

    runner.run_check = spy
    try:
        d = tempfile.mkdtemp()
        cand = os.path.join(d, "c.yaml")
        w = {"quality": 0.4, "cost": 0.4, "latency": 0.2, "availability": 0.2}
        yaml.safe_dump({"schema": SCHEMA, "name": "null", "routing": {"weights": w}},
                       open(cand, "w"))
        asyncio.run(runner.run_bench(str(ROOT / "eval/suites/starter.jsonl"),
                                     str(ROOT / "eval/endpoints/healthcare-sim.yaml"),
                                     out_dir=d + "/o", candidate_path=cand))
    finally:
        runner.run_check = orig
    cases, _ = load_suite(str(ROOT / "eval/suites/starter.jsonl"))
    tags = [(c.tags, c.app_policy) for c in cases]
    return sorted(cap["b"], key=lambda s: s.case_id), tags, {}


def _arr(scores: list[CaseScore]) -> np.ndarray:
    a = np.array([[np.nan if getattr(s, f) is None else float(getattr(s, f)) for f in FIELD]
                  for s in scores])
    a[:, [METRICS.index(m) for m in ("mean_chosen_cost", "cost_regret")]] *= COST_UNIT
    return a


class EmpiricalDist:
    """Dist A: a case's whole 8-metric vector (None pattern included) is drawn
    uniformly from the 22 real starter-suite rows."""
    name = "A empirical (22 real starter rows)"

    def __init__(self, pool: list[CaseScore]):
        self.rows = _arr(pool)

    def draw(self, rng: np.random.Generator, k: int) -> np.ndarray:
        return self.rows[rng.integers(0, len(self.rows), size=k)]


class ParametricDist:
    """Dist B: independent parametric draws. pass ~ Bern(0.8); quality_regret = 1-pass
    (U(.2,1) if failed); floor_miss = 1-pass; chosen_cost lognormal(median 1.0, s=0.8);
    cost_regret / latency_regret only when passed (0.3*cost*U, Exp(100 ms));
    wasted ~ Poisson(0.3); exhausted ~ Bern(0.05)."""
    name = "B parametric (pass 0.8, lognormal cost)"

    def draw(self, rng: np.random.Generator, k: int) -> np.ndarray:
        p = (rng.random(k) < 0.8).astype(float)
        out = np.full((k, len(METRICS)), np.nan)
        cost = rng.lognormal(0.0, 0.8, k)
        col = {m: METRICS.index(m) for m in METRICS}
        out[:, col["pass_rate"]] = p
        out[:, col["quality_regret"]] = (1 - p) * rng.uniform(0.2, 1.0, k)
        out[:, col["floor_miss_rate"]] = 1 - p
        out[:, col["mean_chosen_cost"]] = cost
        ok = p == 1
        out[ok, col["cost_regret"]] = 0.3 * cost[ok] * rng.random(ok.sum())
        out[ok, col["latency_regret"]] = rng.exponential(100.0, ok.sum())
        out[:, col["wasted_attempts"]] = rng.poisson(0.3, k)
        out[:, col["exhausted_rate"]] = (rng.random(k) < 0.05).astype(float)
        return out


# --- suite structure -----------------------------------------------------------

def suite_keys(tags: list, n: int) -> tuple[list[str], dict[str, dict[str, str]]]:
    """Case ids and group keys for n cases. n=22: the real starter suite. n>22: case j
    takes the tags and app_policy of starter case (j mod 22), so group proportions are
    the starter suite's (each group ~n/22 times larger)."""
    ids = [f"c{j:04d}" for j in range(n)]
    return ids, {ids[j]: case_keys(*tags[j % len(tags)]) for j in range(n)}


class Scopes:
    """Positions of each scope's cases and the resample indices for one (replicates,
    seed). Indices come from the shipped `draw_indices`, so a scope's resamples
    are exactly the shipped check's."""

    def __init__(self, ids, keys, min_group, replicates, seed=0):
        self.min_group, self.R = min_group, replicates
        pos = {c: i for i, c in enumerate(ids)}
        self.scopes: list[tuple[str, np.ndarray, bool]] = [("overall", np.arange(len(ids)), False)]
        for dim in DIMENSIONS:
            for label, members in partition(ids, keys, dim).items():
                self.scopes.append((f"{dim}={label}", np.array([pos[c] for c in members]),
                                    len(members) < min_group))
        self.idx = {s: np.array([draw_indices(seed, b, s, len(p)) for b in range(replicates)])
                    for s, p, small in self.scopes if not small and len(p)}


def judge(base: np.ndarray, cand: np.ndarray, sc: Scopes, tols: dict[str, float],
          level: float) -> tuple[list[str], int]:
    """Mirror of `run_check`'s judging for one draw, calling the shipped `decide`,
    `worsening`, `interval_of`. Returns (states of every judged figure, number of
    judged figures whose point estimate is beyond its tolerance = first rule)."""
    states, first = [], 0
    for scope, pos, small in sc.scopes:
        if small:
            continue                      # insufficient group: listed, not judged
        b, c = base[pos], cand[pos]
        ib = sc.idx[scope]
        bb, cc = b[ib], c[ib]             # R x m x 8
        with np.errstate(invalid="ignore", divide="ignore"):
            bm = np.nansum(bb, 1) / (~np.isnan(bb)).sum(1)
            cm = np.nansum(cc, 1) / (~np.isnan(cc)).sum(1)
        for k, m in enumerate(METRICS):
            tol = tols.get(m)
            if tol is None:
                continue
            sb = [v for v in b[:, k] if not math.isnan(v)]
            sc_ = [v for v in c[:, k] if not math.isnan(v)]
            if min(len(sb), len(sc_)) < sc.min_group:
                if len(sb) >= sc.min_group:        # candidate thinned it: judged, inconclusive
                    states.append("inconclusive")
                continue
            w = vc.worsening(m, _mean(sb), _mean(sc_))
            r = (bm[:, k] - cm[:, k]) if m in vc.LOWER_IS_WORSE else (cm[:, k] - bm[:, k])
            lo = hi = None
            if np.all(np.isfinite(r)):
                lo, hi = interval_of(r.tolist(), level)
            if w is not None and not math.isfinite(w):
                w = lo = hi = None
            st = vc.decide(w, lo, hi, tol)
            states.append(st)
            first += first_rule_regressed(w, tol)
    return states, first


def to_scores(arr: np.ndarray, ids: list[str]) -> list[CaseScore]:
    out = []
    for cid, row in zip(ids, arr):
        v = {f: (None if np.isnan(x) else float(x)) for f, x in zip(FIELD, row)}
        out.append(CaseScore(case_id=cid, empty=False, violation=False, empty_mismatch=False,
                             infeasible_at_floor=False, **v))
    return out


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return 100 * max(0.0, c - h), 100 * min(1.0, c + h)


def fmt(k: int, n: int) -> str:
    lo, hi = wilson(k, n)
    return f"{100 * k / n:5.1f} [{lo:4.1f},{hi:4.1f}]"


def run_row(dist, n, f, tols, tags, args, seed, bonferroni_m=None):
    ids, keys = suite_keys(tags, n)
    plan = vc.CheckPlan(tolerances=tols, replicates=args.resamples, min_group_size=args.min_group)
    level = plan.interval
    R = args.resamples
    if bonferroni_m:
        level = 1 - 2 * 0.05 / bonferroni_m
        R = min(MAX_REPLICATES, max(R, vc.min_replicates(level)))
    sc = Scopes(ids, keys, args.min_group, R)
    rng = np.random.default_rng(seed)
    fig, first_reg, reg, inc, pas = [], 0, 0, 0, 0
    for d in range(args.draws):
        base = dist.draw(rng, n)
        cand = base.copy()
        redraw = rng.choice(n, size=round(f * n), replace=False)
        cand[redraw] = dist.draw(rng, len(redraw))
        states, first = judge(base, cand, sc, tols, level)
        verdict = vc.verdict_of(False, states, True)
        fig.append(len(states)); first_reg += first > 0
        reg += verdict == "regressed"; inc += verdict == "inconclusive"; pas += verdict == "pass"
        if not bonferroni_m and d < args.verify:       # check against the shipped run_check
            doc = vc.run_check(to_scores(base, ids), to_scores(cand, ids), keys,
                               ({}, {}), plan, arms=("a", "b"), candidate_doc={},
                               run_exit_code=0, mode="simulated")
            assert doc["judged"]["figures"] == len(states), (doc["judged"], len(states))
            assert doc["verdict"] == verdict, (doc["verdict"], verdict)
            assert len(doc["regressed"]) == states.count("regressed")
    return dict(fig=sum(fig) / len(fig), fmin=min(fig), fmax=max(fig), first=first_reg,
                reg=reg, inc=inc, pas=pas)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--draws", type=int, default=300)
    ap.add_argument("--resamples", type=int, default=400)
    ap.add_argument("--min-group", type=int, default=5, help="shipped default 5")
    ap.add_argument("--verify", type=int, default=3, help="draws per row cross-checked vs run_check")
    ap.add_argument("--dists", default="AB")
    ap.add_argument("--no-bonferroni", action="store_true")
    ap.add_argument("--seed", type=int, default=20261106)
    args = ap.parse_args()
    t0 = time.time()
    pool, tags, _ = real_pool()
    dists = {"A": EmpiricalDist(pool), "B": ParametricDist()}
    ids22, keys22 = suite_keys(tags, 22)
    print(f"min_group_size={args.min_group}  draws={args.draws}  resamples={args.resamples}  "
          f"seed={args.seed}  cost unit=USD per 1000 requests")
    for dim in DIMENSIONS:
        print(" ", dim, {k: len(v) for k, v in partition(ids22, keys22, dim).items()})
    print("pool pass rate %.3f" % np.mean([s.passed for s in pool]))
    for dn in args.dists:
        dist = dists[dn]
        print(f"\n=== distribution {dist.name}; rates % [95% Wilson interval, 300 draws] ===")
        hdr = ("n   f    tolerances                         figs(pub) | first rule | new regressed |"
               " new inconclusive | new pass")
        print(hdr)
        print("published row:  first / regressed / inconclusive / pass")
        bon_rows = []
        for i, (n, f, tols, pub) in enumerate(SETTINGS):
            r = run_row(dist, n, f, tols, tags, args, args.seed + i)
            tl = "all 8 at 0" if tols is ALL8 else "+".join(f"{m} {t:g}" for m, t in tols.items())
            D = args.draws
            print(f"{n:<3} {f:<4} {tl:<34} {r['fig']:5.1f}({pub[0]:>3}) | {fmt(r['first'], D)} | "
                  f"{fmt(r['reg'], D)} | {fmt(r['inc'], D)} | {fmt(r['pas'], D)}")
            print(f"{'':>45}published: {pub[1]:5.1f} / {pub[2]:5.1f} / {pub[3]:5.1f} / {pub[4]:4.1f}"
                  f"   (figures judged min-max {r['fmin']}-{r['fmax']})")
            bon_rows.append((n, f, tols, tl, math.ceil(r["fig"])))
        if not args.no_bonferroni:
            print(f"--- NOT SHIPPED: Bonferroni variant (interval 1-0.1/m, m = mean figures judged, "
                  f"resamples raised to the minimum that level needs) ---")
            for i, (n, f, tols, tl, m) in enumerate(bon_rows):
                r = run_row(dist, n, f, tols, tags, args, args.seed + i, bonferroni_m=m)
                D = args.draws
                print(f"{n:<3} {f:<4} {tl:<34} m={m:<4} | regressed {fmt(r['reg'], D)} | "
                      f"inconclusive {fmt(r['inc'], D)} | pass {fmt(r['pas'], D)}")
    print(f"\nruntime {time.time() - t0:.0f} s")


if __name__ == "__main__":
    main()
