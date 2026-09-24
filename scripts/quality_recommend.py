"""Offline quality-calibration CLI (P2b-3 Task 4, spec §5).

    uv run python scripts/quality_recommend.py --export outcomes.ndjson --out results/quality

Reads an admin NDJSON export (`GET /admin/outcomes/export`), estimates a
per-(model, workload) quality posterior from one `endpoint_registry_version`
group (the newest, or `--registry-version`), replays every logged decision
in that group with the proposed qualities, and writes a deterministic
`recommendation.json` and `report.md` into `--out`. It only *recommends*:
applying a recommendation is a separate, human-gated admin action.

File-based only: no DB, no `app.api`, no serving import
(`tests/eval/test_isolation.py`).

Exit codes: 0 ok; 1 load, option or numeric error (bad path, unrecognized
export schema, unknown registry version, invalid option or priors file, a
Beta quantile that failed to converge); 2 replay
equivalence failure -- for a fully-specified scoring record, the unchanged
replay disagreed with the logged candidate order, so the replay does not
mirror the router and no files are written. Records whose scoring lacks a
term (e.g. pre-P2a exports) are skipped and counted as not recomputable,
not treated as a failure.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.eval.calibration import (  # noqa: E402
    build_recommendation,
    dump_recommendation,
    estimate,
    render_report,
    replay,
)
from app.eval.outcomes import load_export  # noqa: E402


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="quality_recommend",
        description="Offline quality calibration over a routing_outcomes export.")
    p.add_argument("--export", required=True, help="NDJSON export file")
    p.add_argument("--out", required=True, help="output directory")
    p.add_argument("--registry-version", default=None,
                   help="endpoint_registry_version group (default: the newest)")
    p.add_argument("--k", type=float, default=10.0,
                   help="prior strength in pseudo-labels (default 10)")
    p.add_argument("--min-labels", type=int, default=20,
                   help="labels a unit needs to be recommended (default 20)")
    p.add_argument("--max-ci-width", type=float, default=0.25,
                   help="widest 90%% interval a recommendation may have (default 0.25)")
    p.add_argument("--min-shift", type=float, default=0.02,
                   help="smallest |mean - q0| worth recommending (default 0.02)")
    p.add_argument("--priors", default=None,
                   help='JSON file {"model": q0, ...} overriding the export priors')
    return p


def _load_priors(path: str) -> dict[str, float]:
    with open(path, encoding="utf-8") as f:
        priors = json.load(f)
    if not isinstance(priors, dict):
        raise ValueError(f"priors file {path!r} must hold a JSON object")
    return priors


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        records, warnings = load_export(args.export)
        export_sha256 = hashlib.sha256(Path(args.export).read_bytes()).hexdigest()
        priors = _load_priors(args.priors) if args.priors else None
        est = estimate(records, k=args.k, min_labels=args.min_labels,
                       max_ci_width=args.max_ci_width, min_shift=args.min_shift,
                       priors=priors, registry_version=args.registry_version)
    except (OSError, ValueError) as exc:
        print(f"quality_recommend: load error: {exc}", file=sys.stderr)
        return 1
    except ArithmeticError as exc:
        # e.g. the Beta quantile's continued fraction failed to converge for
        # an extreme posterior -- report it; never a traceback.
        print(f"quality_recommend: numeric error while estimating: {exc}",
              file=sys.stderr)
        return 1

    for w in warnings:
        print(f"quality_recommend: warning: {w}", file=sys.stderr)

    rec = build_recommendation(est, export_sha256=export_sha256)
    diff = replay(records, rec)
    if not diff["equivalence_ok"]:
        print("quality_recommend: replay equivalence FAILED -- the unchanged "
              "replay disagreed with the logged order, so the replay diff "
              "cannot be trusted and no recommendation is written:",
              file=sys.stderr)
        for m in diff["mismatches"]:
            print(f"  request_id={m['request_id']} logged={m['logged']} "
                  f"replayed={m['replayed']} ({m['reason']})", file=sys.stderr)
        return 2

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "recommendation.json").write_text(dump_recommendation(rec), encoding="utf-8")
    (out / "report.md").write_text(render_report(est, diff), encoding="utf-8")

    n_rec = sum(1 for u in rec["units"] if u["status"] == "recommended")
    if diff["not_recomputable"]:
        print(f"quality_recommend: warning: {diff['not_recomputable']} records "
              f"skipped (scoring record missing a term; not recomputable)",
              file=sys.stderr)
    print(f"quality_recommend: {len(records)} records ({len(warnings)} warnings), "
          f"{n_rec} recommended units, set_id {rec['set_id']} -> {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
