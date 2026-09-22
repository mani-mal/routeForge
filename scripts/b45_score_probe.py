#!/usr/bin/env python3
"""B45 score probe: print `router_online`'s per-candidate score terms at the
FIRST hop of every case, for each availability weight in `b45.yaml`.

Research notes: docs/research/feasible-route-bench.md §9.5 quotes score values
(leads, availabilities, switch margins) that `choices.jsonl` does not record,
because it records attempt chains, not scores. This script reproduces them.

    SP_PII_DETECTION=regex uv run python scripts/b45_score_probe.py \\
        --seed 0 --seconds-per-case 30 --out /tmp/b45-probe-s0-30

(§9.5 uses seed 0 at --seconds-per-case 1, 30 and 300, and seed 4 at 30.)

Method: this script only observes. It changes no scoring and no outputs, so
the run's `metrics.json` is byte-identical to the same
`scripts/feasible_bench.py` run. It runs the normal `run_bench` on the
outage scenario (`eval/suites/outage.jsonl` x `eval/endpoints/outage-sim.yaml`
x `eval/variants/b45.yaml`) after wrapping two functions:

- `OnlineArm.choose`, to mark "an online arm is choosing case k";
- `DefaultRoutingStrategy.rank`, to log that choice's first `rank` call (the
  first hop) as the router's own `ScoredCandidate` terms.

For each weight it prints one line per case: each candidate's score,
availability (value, source, samples), latency (value, source) and cost_norm,
plus the score lead of `fast-cheap` over `steady` and the pick. The unvaried
`router_online` and `router_online:default` both run at weight 0.2 and give
identical lines; the first one logged is shown.

Evaluation tooling only: nothing on the serving path imports this.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

# BEFORE any `app.*` import (see scripts/feasible_bench.py): litellm fetches
# its cost map over the network at import unless this is set.
os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "True")

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.eval import runner  # noqa: E402
from app.eval import strategies as eval_strategies  # noqa: E402
from app.routing import strategy as routing_strategy  # noqa: E402

SUITE = "eval/suites/outage.jsonl"
ENDPOINTS = "eval/endpoints/outage-sim.yaml"
VARIANTS = "eval/variants/b45.yaml"

_log: list[dict] = []
_state: dict = {"case": None, "first_hop_pending": False}


def _install_probes() -> None:
    orig_rank = routing_strategy.DefaultRoutingStrategy.rank
    orig_choose = eval_strategies.OnlineArm.choose

    def rank(self, eligible, **kw):
        result = orig_rank(self, eligible, **kw)
        if _state["first_hop_pending"]:
            _state["first_hop_pending"] = False
            _log.append({
                "case": _state["case"],
                "w": kw["weights"]["availability"],
                "terms": {sc.candidate.name: sc for sc in result.ordered},
                "pick": result.ordered[0].candidate.name,
            })
        return result

    def choose(self, inp, failed, *, case_index):
        _state.update(case=case_index, first_hop_pending=True)
        try:
            return orig_choose(self, inp, failed, case_index=case_index)
        finally:
            _state["first_hop_pending"] = False

    routing_strategy.DefaultRoutingStrategy.rank = rank
    eval_strategies.OnlineArm.choose = choose


def _fmt(sc) -> str:
    a, lat = sc.availability, sc.latency
    return (f"{sc.score:.4f}(a={a.value:.4f} {a.source}/{a.samples} "
            f"lat={lat.value:.1f} {lat.source} cn={sc.cost_norm:.4f})")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--seconds-per-case", type=float, default=30.0)
    ap.add_argument("--out", required=True,
                    help="run output directory (keep it outside the repository)")
    args = ap.parse_args(argv)
    os.chdir(REPO_ROOT)
    _install_probes()
    result = asyncio.run(runner.run_bench(
        SUITE, ENDPOINTS, out_dir=args.out, seed=args.seed,
        variants_path=VARIANTS, seconds_per_case=args.seconds_per_case))
    print(f"exit {result.exit_code}  seed {args.seed}  "
          f"seconds_per_case {args.seconds_per_case:g}")
    for w in sorted({e["w"] for e in _log}):
        print(f"\n--- availability weight {w:g}")
        seen: set[int] = set()
        for e in _log:
            if e["w"] != w or e["case"] in seen:
                continue
            seen.add(e["case"])
            f, s = e["terms"]["fast-cheap"], e["terms"]["steady"]
            print(f"case {e['case']:2d} F={_fmt(f)} S={_fmt(s)} "
                  f"lead={f.score - s.score:+.7f} -> {e['pick']}")
    return result.exit_code


if __name__ == "__main__":
    sys.exit(main())
