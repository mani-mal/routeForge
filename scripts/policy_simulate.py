"""Offline policy-proposal simulator CLI (P3a Task 5, spec §8).

    uv run python scripts/policy_simulate.py --export outcomes.ndjson \
        --snapshot snapshot.json --proposal proposal.json --out results/sim

    uv run python scripts/policy_simulate.py --export outcomes.ndjson \
        --snapshot snapshot.json --scenarios scenarios.json --out results/cmp

Reads a `routing_outcomes` NDJSON export (`GET /admin/outcomes/export`), a
config snapshot (`GET /admin/config-snapshot`) and an operator-written policy
proposal (`routeforge.policy-proposal/v1`); replays every simulated row's
first routing decision under the snapshot as exported (`current`) and the
snapshot with the proposal applied (`proposed`); and writes a deterministic
`simulation.json` plus a human-readable `report.md` into `--out`.

With `--scenarios` (P3c-2) it instead replays every scenario of a
`routeforge.policy-scenarios/v1` file against one shared `current` arm and
writes `comparison.json` plus `report.md`. Scenarios are reported in file
order; nothing ranks or recommends one.

**Simulation only.** It never applies the proposal to a live deployment --
see `app/eval/simulate.py::REPORT_CAVEAT` and spec §2/§7 for what this does
and does not model.

File-based only: no DB, no `app.api`, no serving import
(`tests/eval/test_isolation.py`).

With `--uncertainty` (P3c-3) every figure also gets a range: a percentile
interval from a bootstrap over the simulated rows, written under one new
`uncertainty` key (and `shadow_prices.uncertainty`). It is sampling
variability only -- see `app/eval/uncertainty.py::UNCERTAINTY_CAVEAT`.
Without the flag every output is byte-identical to what it was before.

Exit codes: 0 output written; 2 an input file failed to load, validate or
apply, or the flags do not combine; 3 the proposal (or any scenario)
introduces new rejections and `--fail-on-new-rejections` is set (for
CI/GitOps gating); 4 the baseline's agreement with the record -- the lower
of model agreement and outcome agreement -- is below `--min-agreement`
(nothing is written).
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
    ProposalError, apply_proposal, assumptions_block, load_proposal, normalized_edits,
)
from app.eval.scenario import SnapshotError, load_snapshot  # noqa: E402
from app.eval.scenarios import (  # noqa: E402
    ComparisonInvariantError, ScenarioError, compare, comparison_doc, load_scenarios,
    render_comparison,
)
from app.eval.shadow import ShadowInvariantError, render_shadow_section, shadow_prices  # noqa: E402
from app.eval.simulate import (  # noqa: E402
    REPORT_CAVEAT, agreement_ok, baseline_check, parse_since, render_report, simulate,
    summarize,
)
from app.eval.uncertainty import (  # noqa: E402
    MAX_REPLICATES, UncertaintyPlan, app_sizes, cost_line, level_text, load_posteriors,
    min_replicates, plan_block, plan_inputs, render_uncertainty_section, results_block,
    tail_index, window_days,
)


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="policy_simulate",
        description="Offline counterfactual simulator for a policy proposal.")
    p.add_argument("--export", required=True, help="routing_outcomes NDJSON export file")
    p.add_argument("--snapshot", required=True, help="GET /admin/config-snapshot JSON file")
    p.add_argument("--proposal", default=None,
                   help="routeforge.policy-proposal/v1 JSON file "
                        "(required unless --shadow-prices or --scenarios is given)")
    p.add_argument("--scenarios", default=None,
                   help="routeforge.policy-scenarios/v1 JSON file: compare its scenarios "
                        "side by side (P3c-2). Not combinable with --proposal or "
                        "--shadow-prices.")
    p.add_argument("--out", required=True, help="output directory")
    p.add_argument("--config-dir", default="config",
                   help="base config directory the snapshot is overlaid onto (default: config)")
    p.add_argument("--fail-on-new-rejections", action="store_true",
                   help="exit 3 if the proposal (or any scenario) newly rejects any "
                        "simulated row")
    p.add_argument("--shadow-prices", action="store_true",
                   help="price what each constraint costs (P3c-1); makes --proposal optional")
    p.add_argument("--max-relaxations", type=int, default=50,
                   help="cap on relaxations evaluated with --shadow-prices (default: 50)")
    p.add_argument("--since", default=None, metavar="TS",
                   help="ISO-8601 UTC timestamp; rows before it are excluded "
                        "(counted as before_since). Applies to every mode.")
    p.add_argument("--min-agreement", type=float, default=None, metavar="X",
                   help="0..1; exit 4 and write nothing when the baseline's agreement "
                        "with the record is below X (or not computable). The lower of two "
                        "rates is compared: model agreement (first pick vs the recorded "
                        "first attempt) and outcome agreement (routed/rejected vs the "
                        "recorded status)")
    p.add_argument("--uncertainty", action="store_true",
                   help="add a range to every figure (P3c-3): a percentile interval from "
                        "a bootstrap over the simulated rows, resampled within each "
                        "application. Sampling variability only. Applies to every mode; "
                        "off by default")
    p.add_argument("--bootstrap", type=int, default=None, metavar="N",
                   help=f"bootstrap resamples, 1..{MAX_REPLICATES} (default: 1000). "
                        "Needs --uncertainty")
    p.add_argument("--interval", type=float, default=None, metavar="P",
                   help="interval level, strictly between 0 and 1 (default: 0.90). "
                        "Needs --uncertainty")
    p.add_argument("--seed", type=int, default=None, metavar="S",
                   help="bootstrap seed, an integer >= 0 (default: 0). Needs --uncertainty")
    p.add_argument("--min-rows", type=int, default=None, metavar="K",
                   help="a bucket with fewer rows gets \"insufficient data\" instead of a "
                        "range (default: 30). Needs --uncertainty")
    p.add_argument("--quality-posteriors", action="append", default=None, metavar="FILE",
                   help="a P2b-3 recommendation.json that was applied; repeatable. Its "
                        "Beta posteriors add quality-label uncertainty for the calibrated "
                        "qualities of that set. Needs --uncertainty")
    return p


def _uncertainty_plan(args: argparse.Namespace) -> UncertaintyPlan | None:
    """The `--uncertainty` plan, or None without the flag. Raises
    `ValueError` (`UncertaintyError` is one) or `OSError` for a tuning flag
    given without `--uncertainty`, a value out of range, or a posterior file
    that does not load."""
    tuning = (("--bootstrap", args.bootstrap), ("--interval", args.interval),
              ("--seed", args.seed), ("--min-rows", args.min_rows),
              ("--quality-posteriors", args.quality_posteriors))
    if not args.uncertainty:
        given = [flag for flag, value in tuning if value is not None]
        if given:
            raise ValueError(f"{', '.join(given)} given without --uncertainty")
        return None
    defaults = UncertaintyPlan()
    posteriors, files = load_posteriors(args.quality_posteriors or [])
    return UncertaintyPlan(
        replicates=defaults.replicates if args.bootstrap is None else args.bootstrap,
        interval=defaults.interval if args.interval is None else args.interval,
        seed=defaults.seed if args.seed is None else args.seed,
        min_rows=defaults.min_rows if args.min_rows is None else args.min_rows,
        posteriors=posteriors, posterior_files=files)


def _note(line: str) -> None:
    """A progress line for the operator: stderr, never stdout (stdout is the
    two output paths)."""
    print(line, file=sys.stderr, flush=True)


def _shown(rate: float | None) -> str:
    return "n/a" if rate is None else f"{rate:.6g}"


def _below_min_agreement(baseline: dict, minimum: float) -> int:
    """Print why the `--min-agreement` gate refused the run; return exit 4.
    Both rates and both outcome-disagreement counts are named, so the
    operator can tell a wrong-model replay (B55) from a wrong-outcome one
    (B60) without re-running."""
    print(f"policy_simulate: baseline agreement {_shown(baseline['agreement'])} over "
          f"{baseline['rows_compared']} compared rows, outcome agreement "
          f"{_shown(baseline['outcome_agreement'])} over "
          f"{baseline['outcome_rows_compared']} simulated rows "
          f"({baseline['recorded_rejected_but_routed']} recorded rejected but routed, "
          f"{baseline['recorded_routed_but_rejected']} recorded routed but rejected): "
          f"below --min-agreement {minimum:g} (the lower of the two is compared; n/a "
          "never passes); nothing written", file=sys.stderr)
    return 4


def _compare_scenarios(args: argparse.Namespace, *, records: list[dict], warnings: list[str],
                       snapshot: dict, scenarios_doc: dict, inputs: dict, base,
                       since, plan: UncertaintyPlan | None) -> int:
    """`--scenarios` mode (P3c-2): replay every scenario against one shared
    `current` arm, then write `comparison.json` and `report.md`."""
    try:
        if plan is not None and args.min_agreement is not None:
            # P3c-3: `compare` bootstraps every scenario before it returns the
            # baseline the gate reads. One extra replay of the snapshot keeps
            # a run the gate refuses from paying for those bootstraps.
            early = asyncio.run(baseline_check(records, snapshot, base=base, since=since))
            if not agreement_ok(early, args.min_agreement):
                return _below_min_agreement(early, args.min_agreement)
        comparison = asyncio.run(compare(records, snapshot, scenarios_doc, base=base,
                                         since=since, uncertainty=plan, notify=_note))
    except (SnapshotError, ProposalError, ScenarioError) as exc:
        # All-or-nothing (spec §3): one invalid scenario fails the whole
        # file, named `scenarios[<i>](<name>).proposal.<path>`.
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except ComparisonInvariantError as exc:
        print(f"error: internal invariant: {exc}", file=sys.stderr)
        return 2

    if args.min_agreement is not None and not agreement_ok(comparison.baseline,
                                                           args.min_agreement):
        return _below_min_agreement(comparison.baseline, args.min_agreement)

    doc = comparison_doc(comparison, inputs=inputs)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    comparison_path = out / "comparison.json"
    report_path = out / "report.md"
    comparison_path.write_text(json.dumps(doc, sort_keys=True, indent=2) + "\n",
                               encoding="utf-8")
    report_path.write_text(render_comparison(doc), encoding="utf-8")

    print(f"policy_simulate: {doc['rows']['simulated']} rows simulated under "
          f"{len(doc['scenarios'])} scenario(s) ({len(warnings)} export warnings) -> {out}")
    print(str(comparison_path))
    print(str(report_path))

    if args.fail_on_new_rejections and any(row["newly_rejected"] > 0 for row in doc["table"]):
        return 3
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)

    if args.scenarios and (args.proposal or args.shadow_prices):
        # Shadow prices of N scenarios is out of scope (P3c-2 spec §6).
        parser.error("--scenarios cannot be combined with --proposal or --shadow-prices")
    if not (args.shadow_prices or args.proposal or args.scenarios):
        parser.error("--proposal is required unless --shadow-prices or --scenarios is given")

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
        plan = _uncertainty_plan(args)
    except (ValueError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if plan is not None and tail_index(plan.replicates, plan.interval) == 0:
        # Not an error: the run is still reproducible and says what it did
        # (`uncertainty.interval_is_min_max`). But nobody should find out
        # from the JSON that their "90% range" left nothing out.
        level = level_text(plan.interval)
        print(f"policy_simulate: warning: --bootstrap {plan.replicates} is too few for a "
              f"{level} interval (it needs at least {min_replicates(plan.interval)}): every "
              "range is the smallest and the largest resample value", file=sys.stderr)

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
        scenarios_doc, scenarios_sha256 = (None, None)
        if args.scenarios:
            scenarios_doc, scenarios_sha256 = load_scenarios(args.scenarios)
    except (SnapshotError, ProposalError, ScenarioError, ValueError, OSError,
            json.JSONDecodeError) as exc:
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

    uncertainty_inputs = {}
    if plan is not None:
        # Only with the flag: without it `inputs`, and so the document's id,
        # are exactly what they were before P3c-3.
        uncertainty_inputs = {"uncertainty": plan_inputs(plan)}
        # A posterior file for a set the snapshot does not use is probably
        # the wrong file: it loads, matches nothing, and the report's coverage
        # line will say 0 rows. Say so now.
        applied = {entry["set_id"] for entry in snapshot["quality"]["calibrated"]}
        for set_id, _sha in plan.posterior_files:
            if set_id not in applied:
                print(f"policy_simulate: warning: --quality-posteriors set {set_id} is not "
                      "a calibration set in the snapshot; it adds no quality uncertainty",
                      file=sys.stderr)

    # The deployment posture every app was compiled under -- the SNAPSHOT's
    # values, never this machine's local config.
    serving_policy = {k: snapshot["settings"]["serving_policy"][k]
                      for k in SERVING_POLICY_FIELDS}
    if args.scenarios:
        return _compare_scenarios(
            args, records=records, warnings=warnings, snapshot=snapshot,
            scenarios_doc=scenarios_doc, base=base, since=since, plan=plan,
            inputs={
                "export_sha256": export_sha256,
                "snapshot_sha256": snapshot_sha256,
                "scenarios_sha256": scenarios_sha256,
                "title": scenarios_doc.get("title"),
                "routing_policy_version": snapshot["routing_policy_version"],
                "registry_version": snapshot["registry_version"],
                "snapshot_generated_at": snapshot["generated_at"],
                "serving_policy": serving_policy,
                "since": since.isoformat() if since is not None else None,
                **uncertainty_inputs,
            })

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
        **uncertainty_inputs,
    }

    try:
        sim = baseline = None
        if args.proposal:
            results, excluded = asyncio.run(simulate(records, snapshot, proposal, base=base,
                                                     since=since))
            sim = summarize(results, excluded, inputs=inputs, edits=normalized_edits(proposal),
                            assumptions=assumptions_block(proposal))
            baseline = sim["baseline"]
        elif args.min_agreement is not None:
            # `--shadow-prices` alone has no `summarize()` output to read the
            # fidelity block from, so the gate replays the snapshot once.
            baseline = asyncio.run(baseline_check(records, snapshot, base=base, since=since))

        # The gate runs before the (expensive) shadow pass and before any
        # output directory or file exists (P3c-2 spec §6).
        if args.min_agreement is not None and not agreement_ok(baseline, args.min_agreement):
            return _below_min_agreement(baseline, args.min_agreement)

        # P3c-3: after the gate, so a refused run never pays for a bootstrap.
        # `results` are the rows `summarize` just read; nothing is re-simulated.
        uncertainty_block = None
        if plan is not None:
            uncertainty_block = plan_block(plan)
            if args.proposal:
                # Said before it starts: a large export at the default 1000
                # resamples is minutes, and nothing else is printed meanwhile.
                _note(cost_line(plan, [app_sizes(results)]))
                uncertainty_block.update(results_block(
                    results, plan, days=window_days(sim["inputs"]["window"])))

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
                baseline_label=baseline_label, since=since, uncertainty=plan,
                notify=_note))
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
        if uncertainty_block is not None:
            sim["uncertainty"] = uncertainty_block
            report_text += "\n" + render_uncertainty_section(uncertainty_block)
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
        report_text = caveat + "\n\n"
        if uncertainty_block is not None:
            sim["uncertainty"] = uncertainty_block
            report_text += render_uncertainty_section(uncertainty_block) + "\n"
        report_text += render_shadow_section(shadow_block)
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
