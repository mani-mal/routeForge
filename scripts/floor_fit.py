#!/usr/bin/env python3
"""Fit quality lower bounds for FeasibleRouteBench's `router_floor` arm (B80).

    uv run python scripts/floor_fit.py \\
        --matrix results/<run>/matrix.jsonl --suite eval/suites/starter.jsonl \\
        --seed 0 --out bounds.json

Splits the suite 50/50 by workload with `--seed` (`app/eval/floor_fit.py::
split_cases`), turns the calibration half's sweep rows into outcome-export
records and runs the calibration estimator over them with its default gates.
Writes a `routeforge.floor-bounds/v1` file; score it with

    uv run python scripts/feasible_bench.py ... --floor-bounds bounds.json \\
        --floor-split heldout

The priors are the deployment's published / tier-default quality for each
endpoint slug (an endpoint the deployment's pool does not list gets the
neutral tier-default prior). Offline: no network, no DB. Exit 0 ok, 1 on a
load error.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "True")

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.config.loader import load_settings  # noqa: E402
from app.eval.cases import SuiteError, load_suite  # noqa: E402
from app.eval.floor_fit import dump_bounds, fit_bounds, load_matrix, split_cases  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="floor_fit",
        description="Fit router_floor lower bounds on a calibration split.")
    p.add_argument("--matrix", required=True, help="a bench run's matrix.jsonl")
    p.add_argument("--suite", required=True, help="the case suite that run used (JSONL)")
    p.add_argument("--seed", type=int, required=True, help="split seed")
    p.add_argument("--out", required=True, help="bounds file to write")
    args = p.parse_args(argv)
    try:
        cases, _ = load_suite(args.suite)
        rows = load_matrix(args.matrix)
        known = {c.id for c in cases}
        unknown = sorted({r.case_id for r in rows} - known)
        if unknown:
            raise ValueError(f"matrix rows name cases not in the suite: {unknown[:5]}")
        calib, held = split_cases(cases, args.seed)
        doc = fit_bounds(rows, cases, set(calib), load_settings(), seed=args.seed)
    except (SuiteError, OSError, ValueError) as exc:
        print(f"floor_fit: load error: {exc}", file=sys.stderr)
        return 1
    Path(args.out).write_text(dump_bounds(doc), encoding="utf-8")
    print(f"floor_fit: {len(doc['bounds'])} bound(s) from {len(calib)} calibration "
          f"case(s); {len(held)} held out -> {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
