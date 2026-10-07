#!/usr/bin/env python3
"""FeasibleRouteBench CLI -- score routing strategies against the feasible
oracle (docs/superpowers/specs/2026-09-21-feasible-route-bench-design.md).

    uv run python scripts/feasible_bench.py \\
        --suite eval/suites/starter.jsonl \\
        --endpoints eval/endpoints/healthcare-sim.yaml \\
        --variants eval/variants/b45.yaml --seed 0 --seconds-per-case 30

Simulated by default: no network call is made. `--live` calls the real
endpoints in the endpoint file (every eligible endpoint, for every case) and
first prints the call budget and asks for confirmation (`--yes` skips the
prompt for non-interactive use).

Writes `matrix.jsonl`, `choices.jsonl`, `metrics.json`, `report.md` and
`run.json` into `--out` (default `results/<utc timestamp>`). `--group-report`
adds `groups.json` and `groups.md`: every metric by placement, protection,
application and workload.

Exit codes: 0 ok; 1 load/usage error (or a declined live run); 2 policy
violation, empty-set mismatch, post-gate widening or pinned-served mismatch.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

# BEFORE any `app.*` import: litellm (imported transitively) fetches its model
# cost map over the network at import time unless this is set -- an
# air-gapped run would hang there, and "no network call in simulated mode"
# would be false. setdefault: an operator's explicit value still wins.
os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "True")

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.config.loader import load_settings  # noqa: E402
from app.eval.cases import SuiteError, load_suite  # noqa: E402
from app.eval.endpoints import EndpointFileError, load_endpoints  # noqa: E402
from app.eval.groups import DEFAULT_MIN_GROUP_SIZE  # noqa: E402
from app.eval.runner import run_bench  # noqa: E402
from app.eval.strategies import DEFAULT_SECONDS_PER_CASE  # noqa: E402


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="feasible_bench",
        description="Score routing strategies against the feasible oracle.")
    p.add_argument("--suite", required=True, help="case suite (JSONL)")
    p.add_argument("--endpoints", required=True, help="endpoint file (YAML)")
    p.add_argument("--out", default=None,
                   help="output directory (default: results/<utc timestamp>)")
    p.add_argument("--seed", type=int, default=0, help="simulation seed (default 0)")
    p.add_argument("--live", action="store_true",
                   help="call the real endpoints (costs money; asks first)")
    p.add_argument("--yes", action="store_true",
                   help="with --live: skip the confirmation prompt")
    p.add_argument("--keep-text", action="store_true",
                   help="persist completion text in matrix.jsonl")
    p.add_argument("--variants", default=None,
                   help="YAML mapping of variant name -> routing weights")
    p.add_argument("--fixed", default=None,
                   help="also score fixed:<slug> (falls back to cheapest when ineligible)")
    p.add_argument("--parity-tol", type=float, default=0.02,
                   help="max pass-rate gap for the headline cost claim (default 0.02)")
    p.add_argument("--seconds-per-case", type=float,
                   default=DEFAULT_SECONDS_PER_CASE,
                   help="synthetic case-clock spacing: case k is scored at "
                        "k x this many seconds; drives signal decay "
                        f"(default {DEFAULT_SECONDS_PER_CASE:g}; must be > 0)")
    p.add_argument("--group-report", action="store_true",
                   help="also write groups.json and groups.md: every metric by "
                        "placement, protection, application and workload")
    p.add_argument("--min-group-size", type=int, default=None,
                   help="fewest cases behind a printed group rate "
                        f"(default {DEFAULT_MIN_GROUP_SIZE}; needs --group-report)")
    return p


def _confirm_live(suite: str, endpoints: str, assume_yes: bool) -> bool:
    """Print the live call budget and endpoint list; ask unless `--yes`.
    Load errors here raise, exactly as they would inside the run."""
    cases, _ = load_suite(suite)
    specs, _ = load_endpoints(endpoints, live=True, tiers=load_settings().routing.tiers)
    n, m = len(cases), len(specs)
    print(f"LIVE run: {n} cases × {m} endpoints ≤ {n * m} calls")
    for spec in specs:
        print(f"  - {spec.slug} ({spec.provider}/{spec.tier}) -> {spec.live.endpoint}")
    if assume_yes:
        return True
    try:
        answer = input("Proceed? [y/N] ")
    except EOFError:
        answer = ""
    return answer.strip().lower() in ("y", "yes")


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.min_group_size is not None and not args.group_report:
        # A flag that would silently do nothing is a usage error.
        print("feasible_bench: load error: --min-group-size needs --group-report",
              file=sys.stderr)
        return 1
    out = args.out or str(
        REPO_ROOT / "results" / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"))

    if args.live:
        try:
            if not _confirm_live(args.suite, args.endpoints, args.yes):
                print("feasible_bench: live run declined; no calls made", file=sys.stderr)
                return 1
        except (SuiteError, EndpointFileError, OSError, ValueError) as exc:
            print(f"feasible_bench: load error: {exc}", file=sys.stderr)
            return 1

    result = asyncio.run(run_bench(
        args.suite, args.endpoints, out_dir=out, seed=args.seed, live=args.live,
        keep_text=args.keep_text, variants_path=args.variants,
        parity_tol=args.parity_tol, fixed=args.fixed,
        seconds_per_case=args.seconds_per_case,
        group_report=args.group_report,
        min_group_size=(DEFAULT_MIN_GROUP_SIZE if args.min_group_size is None
                        else args.min_group_size)))
    if result.exit_code != 1:
        print(f"feasible_bench: results in {result.out_dir} (exit {result.exit_code})")
    return result.exit_code


if __name__ == "__main__":
    sys.exit(main())
