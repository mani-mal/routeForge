"""P3c-3 invariant U1: without `--uncertainty`, every output of
`scripts/policy_simulate.py` is byte-identical to what it was before P3c-3.

The files under `tests/eval/fixtures/uncertainty_golden/` were written by
this module from the tree as it was BEFORE any P3c-3 product code existed
(plan Task 1, Step 2), in all four ways the CLI can be run:

    proposal          --proposal
    proposal_shadow   --proposal --shadow-prices
    shadow            --shadow-prices
    scenarios         --scenarios

`inputs/` holds the four input files, committed, so the expected outputs do
not move when `config/` or a fixture builder changes. To regenerate after a
DELIBERATE change to the simulator's default output, run

    LITELLM_LOCAL_MODEL_COST_MAP=True uv run python -m tests.eval.test_uncertainty_golden

and review the diff: every changed byte is a change to what an operator who
never passes `--uncertainty` sees.
"""
from __future__ import annotations

import importlib.util
import json
import shutil
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "policy_simulate.py"
GOLDEN = Path(__file__).parent / "fixtures" / "uncertainty_golden"
INPUTS = GOLDEN / "inputs"
EXPECTED = GOLDEN / "expected"

EXPORT, SNAPSHOT = INPUTS / "export.ndjson", INPUTS / "snapshot.json"
PROPOSAL, SCENARIOS = INPUTS / "proposal.json", INPUTS / "scenarios.json"

#: mode -> (the flags that select it, the two files it writes)
MODES: dict[str, tuple[list[str], tuple[str, str]]] = {
    "proposal": (["--proposal", str(PROPOSAL)], ("simulation.json", "report.md")),
    "proposal_shadow": (["--proposal", str(PROPOSAL), "--shadow-prices"],
                        ("simulation.json", "report.md")),
    "shadow": (["--shadow-prices"], ("simulation.json", "report.md")),
    "scenarios": (["--scenarios", str(SCENARIOS)], ("comparison.json", "report.md")),
}


def load_cli():
    """`scripts/policy_simulate.py` as a module, so `main(argv)` runs
    in-process (the pattern `tests/eval/test_policy_simulate_cli.py` uses)."""
    spec = importlib.util.spec_from_file_location("policy_simulate", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def run_mode(mode: str, out: Path, *extra: str) -> int:
    """Run the CLI in `mode` over the committed inputs, writing into `out`."""
    flags, _files = MODES[mode]
    return load_cli().main(["--export", str(EXPORT), "--snapshot", str(SNAPSHOT),
                            "--out", str(out), "--config-dir", str(ROOT / "config"),
                            *flags, *extra])


def _input_files() -> dict[str, str]:
    """The four input files' text, from the shared fixture builders.

    Three apps on a deployment whose frontier tier is cloud-only: `big` is
    pinned to the mid tier (an operational constraint that binds), `safe`
    is on-prem only (a security constraint that binds), `few` has six rows
    (below the default `--min-rows`). 111 rows over three days, every row a
    different prompt size."""
    from tests.eval.sim_fixtures import app_entry, make_row, make_snapshot, onboarded_row

    snapshot = make_snapshot(
        onboarded=[onboarded_row("local-cheap", "cheap"), onboarded_row("local-mid", "mid")],
        apps=[app_entry("big", allowed_tiers=["mid"]),
              app_entry("few", allowed_tiers=["mid"]),
              app_entry("safe", residency="onprem_only")],
        providers={"azure": {"enabled": True}})
    lines = []
    for app, n in (("big", 60), ("few", 6), ("safe", 45)):
        for i in range(n):
            row = make_row(f"{app}-{i:03d}", app, complexity=[0.05, 0.6, 1.0][i % 3],
                           prompt_tokens=[200, 1200, 3000][i % 3] + 5 * i,
                           ts=f"2026-09-{20 + i % 3:02d}T12:{i % 60:02d}:00+00:00")
            feedback = row.pop("feedback")
            lines.append(json.dumps({"schema": "routeforge.outcome/v1", "outcome": row,
                                     "feedback": feedback}, sort_keys=True))
    proposal = {"schema": "routeforge.policy-proposal/v1", "title": "Drop the mid endpoint",
                "endpoints": {"remove": ["local-mid"]}}
    scenarios = {"schema": "routeforge.policy-scenarios/v1", "title": "Golden options",
                 "scenarios": [
                     {"name": "no-change", "proposal": {}},
                     {"name": "drop-mid", "proposal": {"endpoints": {"remove": ["local-mid"]}}},
                     {"name": "half-tokens", "proposal": {"workload": {"token_scale": 0.5}}}]}
    return {"export.ndjson": "".join(line + "\n" for line in lines),
            "snapshot.json": json.dumps(snapshot, sort_keys=True, indent=2) + "\n",
            "proposal.json": json.dumps(proposal, sort_keys=True, indent=2) + "\n",
            "scenarios.json": json.dumps(scenarios, sort_keys=True, indent=2) + "\n"}


def regenerate() -> None:
    """Rewrite the inputs and every expected output from the current tree."""
    shutil.rmtree(GOLDEN, ignore_errors=True)
    INPUTS.mkdir(parents=True)
    for name, text in _input_files().items():
        (INPUTS / name).write_text(text, encoding="utf-8")
    for mode in MODES:
        code = run_mode(mode, EXPECTED / mode)
        if code != 0:
            raise SystemExit(f"{mode}: exit {code}")
        print(f"wrote {EXPECTED / mode}")


@pytest.mark.parametrize("mode", sorted(MODES))
def test_u1_output_without_the_flag_is_byte_identical(mode, tmp_path):     # U1
    out = tmp_path / "out"
    assert run_mode(mode, out) == 0
    _flags, files = MODES[mode]
    assert sorted(p.name for p in out.iterdir()) == sorted(files)
    for name in files:
        assert (out / name).read_bytes() == (EXPECTED / mode / name).read_bytes(), \
            f"{mode}/{name} changed without --uncertainty"


def test_the_golden_run_is_not_trivial():
    """The pin is only worth something if the outputs it pins have figures
    in them: routes that move, rows newly rejected, binding constraints of
    both kinds, and three scenarios that differ."""
    sim = json.loads((EXPECTED / "proposal_shadow" / "simulation.json").read_text())
    assert sim["rows"]["simulated"] == 111 and sim["baseline"]["agreement"] is not None
    assert sim["totals"]["route_changed"] > 0 and sim["totals"]["cost_usd"]["delta"] != 0
    kinds = {c["security"] for c in sim["shadow_prices"]["constraints"]}
    assert kinds == {True, False}
    doc = json.loads((EXPECTED / "scenarios" / "comparison.json").read_text())
    assert [row["name"] for row in doc["table"]] == ["no-change", "drop-mid", "half-tokens"]
    assert len({row["cost_delta_usd"] for row in doc["table"]}) == 3
    for mode in MODES:
        for path in (EXPECTED / mode).iterdir():
            assert "uncertainty" not in path.read_text().lower(), path


if __name__ == "__main__":
    regenerate()
