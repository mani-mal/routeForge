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
`--fail-on-new-rejections` is set (for CI/GitOps gating); 4 the baseline's
agreement with the record is below `--min-agreement` (nothing is written).
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import sys
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.apps.snapshot import SERVING_POLICY_FIELDS  # noqa: E402
from app.config.lint import ConfigLintError  # noqa: E402
from app.config.loader import load_settings  # noqa: E402
from app.eval.outcomes import load_export  # noqa: E402
from app.eval.proposal import (  # noqa: E402
    ProposalError, apply_proposal, load_proposal, normalized_edits,
)
from app.eval.scenario import SnapshotError, load_snapshot  # noqa: E402
from app.eval.shadow import ShadowInvariantError, render_shadow_section, shadow_prices  # noqa: E402
from app.eval.simulate import (  # noqa: E402
    REPORT_CAVEAT, agreement_ok, baseline_check, parse_since, render_report, simulate,
    summarize,
)


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="policy_simulate",
        description="Offline counterfactual simulator for a policy proposal.")
    p.add_argument("--export", required=True, help="routing_outcomes NDJSON export file")
    p.add_argument("--snapshot", required=True, help="GET /admin/config-snapshot JSON file")
    p.add_argument("--proposal", default=None,
                   help="routeforge.policy-proposal/v1 JSON file "
                        "(required unless --shadow-prices is given)")
    p.add_argument("--out", required=True, help="output directory")
    p.add_argument("--config-dir", default="config",
                   help="base config directory the snapshot is overlaid onto (default: config)")
    p.add_argument("--fail-on-new-rejections", action="store_true",
                   help="exit 3 if the proposal newly rejects any simulated row")
    p.add_argument("--shadow-prices", action="store_true",
                   help="price what each constraint costs (P3c-1); makes --proposal optional")
    p.add_argument("--max-relaxations", type=int, default=50,
                   help="cap on relaxations evaluated with --shadow-prices (default: 50)")
    p.add_argument("--since", default=None, metavar="TS",
                   help="ISO-8601 UTC timestamp; rows before it are excluded "
                        "(counted as before_since). Applies to every mode.")
    p.add_argument("--min-agreement", type=float, default=None, metavar="X",
                   help="0..1; exit 4 and write nothing when the baseline's agreement "
                        "with the recorded first attempts is below X (or not computable)")
    return p


def _below_min_agreement(baseline: dict, minimum: float) -> int:
    """Print why the `--min-agreement` gate refused the run; return exit 4."""
    agreement = baseline["agreement"]
    shown = "n/a" if agreement is None else f"{agreement:.6g}"
    print(f"policy_simulate: baseline agreement {shown} over "
          f"{baseline['rows_compared']} compared rows is below --min-agreement "
          f"{minimum:g}; nothing written", file=sys.stderr)
    return 4


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)

    if not args.shadow_prices and not args.proposal:
        parser.error("--proposal is required unless --shadow-prices is given")

    if args.max_relaxations < 1:
        print("error: --max-relaxations must be >= 1", file=sys.stderr)
        return 2

    # `not (0 <= x <= 1)` also refuses NaN, which compares false both ways.
    if args.min_agreement is not None and not (0.0 <= args.min_agreement <= 1.0):
        print("error: --min-agreement must be between 0 and 1", file=sys.stderr)
        return 2

    since = None
    if args.since is not None:
        try:
            since = parse_since(args.since)
        except ValueError:
            print(f"error: --since {args.since!r}: not an ISO-8601 timestamp",
                  file=sys.stderr)
            return 2

    try:
        records, warnings = load_export(args.export)
    except (ValueError, OSError, json.JSONDecodeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    # Printed right after `load_export` succeeds, not after every load, so
    # they still surface if a later file (snapshot/proposal) fails to load.
    for w in warnings:
        print(f"policy_simulate: warning: {w}", file=sys.stderr)

    try:
        export_sha256 = hashlib.sha256(Path(args.export).read_bytes()).hexdigest()
        snapshot, snapshot_sha256 = load_snapshot(args.snapshot)
        proposal, proposal_sha256 = (None, None)
        if args.proposal:
            proposal, proposal_sha256 = load_proposal(args.proposal)
    except (SnapshotError, ProposalError, ValueError, OSError, json.JSONDecodeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    # The base config is an input too: a directory that fails the boot lint
    # (`ConfigLintError`, not a ValueError), has a missing key or bad YAML is
    # exit 2 naming it, never a traceback.
    try:
        base = load_settings(args.config_dir)
    except (ConfigLintError, ValueError, KeyError, OSError, yaml.YAMLError) as exc:
        print(f"error: --config-dir {args.config_dir}: {exc}", file=sys.stderr)
        return 2

    # The deployment posture every app was compiled under -- the SNAPSHOT's
    # values, never this machine's local config.
    serving_policy = {k: snapshot["settings"]["serving_policy"][k]
                      for k in SERVING_POLICY_FIELDS}
    inputs = {
        "export_sha256": export_sha256,
        "snapshot_sha256": snapshot_sha256,
        "proposal_sha256": proposal_sha256,
        "proposal_title": proposal.get("title") if proposal else None,
        "routing_policy_version": snapshot["routing_policy_version"],
        "registry_version": snapshot["registry_version"],
        "snapshot_generated_at": snapshot["generated_at"],
        "serving_policy": serving_policy,
        "since": since.isoformat() if since is not None else None,
    }

    try:
        sim = baseline = None
        if args.proposal:
            results, excluded = asyncio.run(simulate(records, snapshot, proposal, base=base,
                                                     since=since))
            sim = summarize(results, excluded, inputs=inputs, edits=normalized_edits(proposal))
            baseline = sim["baseline"]
        elif args.min_agreement is not None:
            # `--shadow-prices` alone has no `summarize()` output to read the
            # fidelity block from, so the gate replays the snapshot once.
            baseline = asyncio.run(baseline_check(records, snapshot, base=base, since=since))

        # The gate runs before the (expensive) shadow pass and before any
        # output directory or file exists (P3c-2 spec §6).
        if args.min_agreement is not None and not agreement_ok(baseline, args.min_agreement):
            return _below_min_agreement(baseline, args.min_agreement)

        shadow_block = None
        if args.shadow_prices:
            # P3c-1: the CALLER applies any proposal first -- shadow_prices
            # itself never sees a proposal, only the (possibly proposal-
            # applied) baseline doc, and is told which in `baseline_label`.
            if args.proposal:
                baseline_doc = apply_proposal(snapshot, proposal, base=base)
                baseline_label = "snapshot+proposal"
            else:
                baseline_doc = snapshot
                baseline_label = "snapshot"
            shadow_block = asyncio.run(shadow_prices(
                records, baseline_doc, base=base, cap=args.max_relaxations,
                baseline_label=baseline_label, since=since))
    except (SnapshotError, ProposalError) as exc:
        # Spec §8 governs over the narrower "catch ProposalError" reading:
        # exit 2 is "an input file failed to load OR VALIDATE" -- a
        # snapshot that passes `load_snapshot`'s shallow structural check
        # but fails `build_settings`'s deep validation (e.g. an undeclared
        # provider) is exactly that, and `apply_proposal`/`build_scenario`
        # inside `simulate()`/`shadow_prices()` can raise either error type.
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except ShadowInvariantError as exc:
        # A relaxation narrowed eligibility or rejected a baseline-routed row
        # -- never a reportable result (spec §3 invariant); exit 2, not a
        # traceback.
        print(f"error: internal invariant: {exc}", file=sys.stderr)
        return 2

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    sim_path = out / "simulation.json"
    report_path = out / "report.md"

    if args.proposal:
        report_text = render_report(sim)
        if shadow_block is not None:
            sim["shadow_prices"] = shadow_block
            report_text += "\n" + render_shadow_section(shadow_block)
        newly_rejected = sim["totals"]["newly_rejected"]
        print(f"policy_simulate: {sim['rows']['simulated']} rows simulated "
              f"({len(warnings)} export warnings) -> {out}")
    else:
        # `--shadow-prices` with no `--proposal`: the P3a baseline-vs-
        # proposal sections do not exist (spec §5), so `simulation.json` is
        # just the inputs (proposal fields null) plus the shadow block, and
        # `report.md` is the P3a caveat followed by the shadow section.
        sim = {"schema": "routeforge.simulation/v1", "inputs": inputs,
              "shadow_prices": shadow_block}
        caveat = "\n".join("> " + line for line in REPORT_CAVEAT.split("\n"))
        report_text = caveat + "\n\n" + render_shadow_section(shadow_block)
        newly_rejected = 0
        print(f"policy_simulate: shadow prices only "
              f"({len(warnings)} export warnings) -> {out}")

    sim_path.write_text(json.dumps(sim, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    report_path.write_text(report_text, encoding="utf-8")

    print(str(sim_path))
    print(str(report_path))

    if args.fail_on_new_rejections and newly_rejected > 0:
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())
