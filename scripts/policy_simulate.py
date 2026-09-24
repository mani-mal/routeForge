"""Offline policy-proposal simulator CLI (P3a Task 5, spec §8).

    uv run python scripts/policy_simulate.py --export outcomes.ndjson \
        --snapshot snapshot.json --proposal proposal.json --out results/sim

Reads a `routing_outcomes` NDJSON export (`GET /admin/outcomes/export`), a
config snapshot (`GET /admin/config-snapshot`) and an operator-written policy
proposal (`routeforge.policy-proposal/v1`); replays every simulated row's
first routing decision under the snapshot as exported (`current`) and the
snapshot with the proposal applied (`proposed`); and writes a deterministic
`simulation.json` plus a human-readable `report.md` into `--out`.

**Simulation only.** It never applies the proposal to a live deployment --
see `app/eval/simulate.py::REPORT_CAVEAT` and spec §2/§7 for what this does
and does not model.

File-based only: no DB, no `app.api`, no serving import
(`tests/eval/test_isolation.py`).

Exit codes: 0 simulation written; 2 an input file failed to load, validate
or apply; 3 the proposal introduces new rejections and
`--fail-on-new-rejections` is set (for CI/GitOps gating).
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.config.loader import load_settings  # noqa: E402
from app.eval.outcomes import load_export  # noqa: E402
from app.eval.proposal import ProposalError, load_proposal, normalized_edits  # noqa: E402
from app.eval.scenario import SnapshotError, load_snapshot  # noqa: E402
from app.eval.simulate import render_report, simulate, summarize  # noqa: E402


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="policy_simulate",
        description="Offline counterfactual simulator for a policy proposal.")
    p.add_argument("--export", required=True, help="routing_outcomes NDJSON export file")
    p.add_argument("--snapshot", required=True, help="GET /admin/config-snapshot JSON file")
    p.add_argument("--proposal", required=True,
                   help="routeforge.policy-proposal/v1 JSON file")
    p.add_argument("--out", required=True, help="output directory")
    p.add_argument("--config-dir", default="config",
                   help="base config directory the snapshot is overlaid onto (default: config)")
    p.add_argument("--fail-on-new-rejections", action="store_true",
                   help="exit 3 if the proposal newly rejects any simulated row")
    return p


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)

    try:
        records, warnings = load_export(args.export)
        export_sha256 = hashlib.sha256(Path(args.export).read_bytes()).hexdigest()
        snapshot, snapshot_sha256 = load_snapshot(args.snapshot)
        proposal, proposal_sha256 = load_proposal(args.proposal)
        base = load_settings(args.config_dir)
    except (SnapshotError, ProposalError, ValueError, OSError, json.JSONDecodeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    for w in warnings:
        print(f"policy_simulate: warning: {w}", file=sys.stderr)

    try:
        results, excluded = asyncio.run(simulate(records, snapshot, proposal, base=base))
    except ProposalError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    inputs = {
        "export_sha256": export_sha256,
        "snapshot_sha256": snapshot_sha256,
        "proposal_sha256": proposal_sha256,
        "proposal_title": proposal.get("title"),
        "routing_policy_version": snapshot["routing_policy_version"],
        "registry_version": snapshot["registry_version"],
        "snapshot_generated_at": snapshot["generated_at"],
    }
    sim = summarize(results, excluded, inputs=inputs, edits=normalized_edits(proposal))

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    sim_path = out / "simulation.json"
    report_path = out / "report.md"
    sim_path.write_text(json.dumps(sim, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    report_path.write_text(render_report(sim), encoding="utf-8")

    print(f"policy_simulate: {sim['rows']['simulated']} rows simulated "
          f"({len(warnings)} export warnings) -> {out}")
    print(str(sim_path))
    print(str(report_path))

    if args.fail_on_new_rejections and sim["totals"]["newly_rejected"] > 0:
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())
