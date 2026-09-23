"""Offline routing-outcomes report CLI (P2b-2 Task 7).

    uv run python scripts/outcome_report.py --export outcomes.ndjson --out results/outcomes

Reads an admin NDJSON export (`GET /admin/outcomes/export`, Task 6), groups
records by `endpoint_registry_version` (never pooling across versions -- see
`app/eval/outcomes.py`), and writes a deterministic `summary.json` and a
`report.md` into `--out`. File-based only: no DB, no `app.api`, no serving
import (`tests/eval/test_isolation.py`).

Exit codes: 0 ok; 1 load error (bad path, or an unrecognized schema major
such as `routeforge.outcome/v2`).
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.eval.outcomes import load_export, render_report, summarize  # noqa: E402


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="outcome_report",
        description="Offline report over a routing_outcomes NDJSON export.")
    p.add_argument("--export", required=True, help="NDJSON export file")
    p.add_argument("--out", required=True, help="output directory")
    return p


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        records, warnings = load_export(args.export)
    except (OSError, ValueError) as exc:
        print(f"outcome_report: load error: {exc}", file=sys.stderr)
        return 1

    for w in warnings:
        print(f"outcome_report: warning: {w}", file=sys.stderr)

    summary = summarize(records)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "summary.json").write_text(
        json.dumps(summary, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    (out / "report.md").write_text(render_report(summary), encoding="utf-8")

    print(f"outcome_report: {len(records)} records ({len(warnings)} warnings) -> {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
