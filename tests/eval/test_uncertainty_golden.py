"""P3c-3 invariant U1: without `--uncertainty`, every output of
`scripts/policy_simulate.py` is byte-identical to what it was before P3c-3.

Two cases, each with its own fixture directory holding `inputs/` (the four
input files, committed, so the expected outputs do not move when `config/`
or a fixture builder changes) and `expected/` (the eight output files of the
four ways the CLI can be run):

    proposal          --proposal
    proposal_shadow   --proposal --shadow-prices
    shadow            --shadow-prices
    scenarios         --scenarios

- `routes` (`fixtures/uncertainty_golden/`): routes that move and binding
  constraints of both kinds. No row is rejected in either arm.
- `rejections` (`fixtures/uncertainty_golden_rejections/`): rows the record
  rejected, rows a proposal newly rejects and newly serves, and rows with no
  backend token count -- the paths the first case never reaches.

**Both were written from the tree as it was BEFORE any P3c-3 product code
existed** -- `routes` in plan Task 1, `rejections` from a `git archive` of
that same base commit (each directory's `GENERATED_FROM` names it). That is
what makes the pin mean "unchanged from before P3c-3": a file regenerated
from the current tree would only say the current tree agrees with itself.
So never regenerate a case here to make a test pass; a failure is a real
change to what an operator who never passes `--uncertainty` sees.

After a DELIBERATE change to the simulator's default output, regenerate ONE
case and review every changed byte:

    LITELLM_LOCAL_MODEL_COST_MAP=True uv run python -m tests.eval.test_uncertainty_golden \\
        <routes|rejections> --deliberate-change

Without `--deliberate-change` the command refuses to run in a tree that has
`app/eval/uncertainty.py`.
"""
from __future__ import annotations

import importlib.util
import json
import shutil
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "policy_simulate.py"
FIXTURES = Path(__file__).parent / "fixtures"

#: mode -> (the flags that select it besides the input files, the two files it writes)
_MODE_FILES: dict[str, tuple[str, str]] = {
    "proposal": ("simulation.json", "report.md"),
    "proposal_shadow": ("simulation.json", "report.md"),
    "shadow": ("simulation.json", "report.md"),
    "scenarios": ("comparison.json", "report.md"),
}


@dataclass(frozen=True)
class Case:
    """One golden case: a fixture directory and the builder of its inputs."""

    name: str
    root: Path
    build: Callable[[], dict[str, str]]

    @property
    def inputs(self) -> Path:
        return self.root / "inputs"

    @property
    def expected(self) -> Path:
        return self.root / "expected"

    def flags(self, mode: str) -> list[str]:
        proposal, scenarios = self.inputs / "proposal.json", self.inputs / "scenarios.json"
        return {"proposal": ["--proposal", str(proposal)],
                "proposal_shadow": ["--proposal", str(proposal), "--shadow-prices"],
                "shadow": ["--shadow-prices"],
                "scenarios": ["--scenarios", str(scenarios)]}[mode]


def load_cli():
    """`scripts/policy_simulate.py` as a module, so `main(argv)` runs
    in-process (the pattern `tests/eval/test_policy_simulate_cli.py` uses)."""
    spec = importlib.util.spec_from_file_location("policy_simulate", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def run_case(case: Case, mode: str, out: Path, *extra: str) -> int:
    """Run the CLI in `mode` over `case`'s committed inputs, writing into `out`."""
    return load_cli().main(["--export", str(case.inputs / "export.ndjson"),
                            "--snapshot", str(case.inputs / "snapshot.json"),
                            "--out", str(out), "--config-dir", str(ROOT / "config"),
                            *case.flags(mode), *extra])


def _export_lines(rows: list[dict]) -> str:
    lines = []
    for row in rows:
        feedback = row.pop("feedback")
        lines.append(json.dumps({"schema": "routeforge.outcome/v1", "outcome": row,
                                 "feedback": feedback}, sort_keys=True))
    return "".join(line + "\n" for line in lines)


def _dump(doc: dict) -> str:
    return json.dumps(doc, sort_keys=True, indent=2) + "\n"


def _input_files() -> dict[str, str]:
    """The `routes` case's four input files, from the shared fixture builders.

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


def _rejection_input_files() -> dict[str, str]:
    """The `rejections` case's four input files.

    Four apps, 40 rows each, on a deployment whose frontier tier is
    cloud-only:

    - `capped` has a per-request cost cap. Its largest requests were
      rejected live (`no_eligible`): no backend answered, so they carry a
      `routing_tokens` count and no `prompt_tokens` -- recorded rejections,
      sized and priced from RouteForge's own estimate.
    - `pinned` may use one model only, and a quarter of its requests name it.
    - `safe` is on-prem only (a security constraint that binds).
    - `steady` has no constraint.

    The proposal lifts `capped`'s cap (rows newly SERVED) and removes the
    one model `pinned` may use (rows newly REJECTED)."""
    from tests.eval.sim_fixtures import app_entry, make_row, make_snapshot, onboarded_row

    snapshot = make_snapshot(
        onboarded=[onboarded_row("local-cheap", "cheap"), onboarded_row("local-mid", "mid")],
        apps=[app_entry("capped", max_cost_usd_per_request=0.0004),
              app_entry("pinned", allowed_models=["local-mid"]),
              app_entry("safe", residency="onprem_only"),
              app_entry("steady")],
        providers={"azure": {"enabled": True}})
    rows = []
    for i in range(40):
        size = [200, 1200, 6000][i % 3] + 5 * i
        over = dict(complexity=[0.05, 0.6, 1.0][i % 3], prompt_tokens=size, routing_tokens=size,
                    ts=f"2026-09-{20 + i % 3:02d}T12:{i % 60:02d}:00+00:00")
        if i % 3 == 2:      # too large for the cap: rejected live, no backend usage
            over.update(status="no_eligible", prompt_tokens=0, completion_tokens=None,
                        attempts=[], chosen=None, latency_ms=None)
        rows.append(make_row(f"capped-{i:03d}", "capped", **over))
    for app in ("pinned", "safe", "steady"):
        for i in range(40):
            size = [200, 1200, 3000][i % 3] + 5 * i
            rows.append(make_row(
                f"{app}-{i:03d}", app, complexity=[0.05, 0.6, 1.0][i % 3], prompt_tokens=size,
                routing_tokens=size,
                requested_model="local-mid" if app == "pinned" and i % 4 == 0 else None,
                ts=f"2026-09-{20 + i % 3:02d}T12:{i % 60:02d}:00+00:00"))
    lift = {"apps": {"capped": {"max_cost_usd_per_request": None}}}
    drop = {"endpoints": {"remove": ["local-mid"]}}
    proposal = {"schema": "routeforge.policy-proposal/v1", "title": "Lift the cap, drop mid",
                **lift, **drop}
    scenarios = {"schema": "routeforge.policy-scenarios/v1", "title": "Golden rejections",
                 "scenarios": [{"name": "no-change", "proposal": {}},
                               {"name": "lift-cap", "proposal": lift},
                               {"name": "drop-mid", "proposal": drop},
                               {"name": "lift-cap+drop-mid", "proposal": {**lift, **drop}}]}
    return {"export.ndjson": _export_lines(rows), "snapshot.json": _dump(snapshot),
            "proposal.json": _dump(proposal), "scenarios.json": _dump(scenarios)}


ROUTES = Case("routes", FIXTURES / "uncertainty_golden", _input_files)
REJECTIONS = Case("rejections", FIXTURES / "uncertainty_golden_rejections",
                  _rejection_input_files)
CASES: dict[str, Case] = {case.name: case for case in (ROUTES, REJECTIONS)}

# The first case under the names the other P3c-3 test modules import.
GOLDEN, INPUTS, EXPECTED = ROUTES.root, ROUTES.inputs, ROUTES.expected
EXPORT, SNAPSHOT = INPUTS / "export.ndjson", INPUTS / "snapshot.json"
PROPOSAL, SCENARIOS = INPUTS / "proposal.json", INPUTS / "scenarios.json"
MODES: dict[str, tuple[list[str], tuple[str, str]]] = {
    mode: (ROUTES.flags(mode), files) for mode, files in _MODE_FILES.items()}


def run_mode(mode: str, out: Path, *extra: str) -> int:
    """Run the CLI in `mode` over the `routes` case's committed inputs."""
    return run_case(ROUTES, mode, out, *extra)


def regenerate(case: Case, *, deliberate_change: bool = False) -> None:
    """Rewrite ONE case's inputs and expected outputs from the tree this
    module is running in. Only that case's directory is touched. Refuses a
    tree that already has P3c-3 product code unless `deliberate_change`."""
    import app.eval.simulate as simulate_module

    here = Path(simulate_module.__file__).resolve()
    if ROOT not in here.parents:
        raise SystemExit(f"app.eval is {here}, not under {ROOT}: wrong tree on sys.path")
    if (ROOT / "app" / "eval" / "uncertainty.py").exists() and not deliberate_change:
        raise SystemExit(
            "refusing: this tree has app/eval/uncertainty.py. The golden files pin the output "
            "from BEFORE P3c-3 -- generate them from a checkout of the base commit, or pass "
            "--deliberate-change after a deliberate change to the default output.")
    print(f"generating {case.name} from {ROOT}")
    shutil.rmtree(case.root, ignore_errors=True)
    case.inputs.mkdir(parents=True)
    for name, text in case.build().items():
        (case.inputs / name).write_text(text, encoding="utf-8")
    for mode in _MODE_FILES:
        code = run_case(case, mode, case.expected / mode)
        if code != 0:
            raise SystemExit(f"{case.name}/{mode}: exit {code}")
        print(f"wrote {case.expected / mode}")


_PINNED = [(case, mode) for case in CASES for mode in sorted(_MODE_FILES)]


@pytest.mark.parametrize("case,mode", _PINNED, ids=[f"{c}-{m}" for c, m in _PINNED])
def test_u1_output_without_the_flag_is_byte_identical(case, mode, tmp_path):     # U1
    golden = CASES[case]
    out = tmp_path / "out"
    assert run_case(golden, mode, out) == 0
    files = _MODE_FILES[mode]
    assert sorted(p.name for p in out.iterdir()) == sorted(files)
    for name in files:
        assert (out / name).read_bytes() == (golden.expected / mode / name).read_bytes(), \
            f"{case}/{mode}/{name} changed without --uncertainty"


def test_the_golden_run_is_not_trivial():
    """The pin is only worth something if the outputs it pins have figures
    in them: routes that move, binding constraints of both kinds, and three
    scenarios that differ. (No row of this case is rejected in either arm;
    `test_the_rejections_case_exercises_rejections` covers that.)"""
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


def test_the_rejections_case_exercises_rejections():
    """The second case pins the paths the first never reaches: recorded
    rejections, rows with no backend token count, rows a proposal newly
    rejects and newly serves, in the proposal, in a scenario row of each
    kind and in the shadow prices."""
    expected = REJECTIONS.expected
    sim = json.loads((expected / "proposal_shadow" / "simulation.json").read_text())
    assert sim["rows"]["simulated"] == 160
    assert sim["rows"]["estimated_tokens"] > 0
    assert sim["baseline"]["recorded_rejected"] > 0
    assert sim["totals"]["newly_rejected"] > 0 and sim["totals"]["newly_served"] > 0
    assert sim["new_rejections"]["by_app"] and sim["new_rejections"]["by_core"]
    doc = json.loads((expected / "scenarios" / "comparison.json").read_text())
    rows = {row["name"]: row for row in doc["table"]}
    assert list(rows) == ["no-change", "lift-cap", "drop-mid", "lift-cap+drop-mid"]
    assert rows["no-change"]["newly_rejected"] == rows["no-change"]["newly_served"] == 0
    assert rows["lift-cap"]["newly_served"] > 0 and rows["lift-cap"]["newly_rejected"] == 0
    assert rows["drop-mid"]["newly_rejected"] > 0 and rows["drop-mid"]["newly_served"] == 0
    both = rows["lift-cap+drop-mid"]
    assert both["newly_rejected"] > 0 and both["newly_served"] > 0
    assert doc["baseline"]["recorded_rejected"] > 0 and doc["rows"]["estimated_tokens"] > 0
    for mode in ("shadow", "proposal_shadow"):
        block = json.loads((expected / mode / "simulation.json").read_text())["shadow_prices"]
        assert any(c["rejections_avoided"] > 0 for c in block["constraints"]), mode
        assert {c["security"] for c in block["constraints"]} == {True, False}, mode
    for mode in _MODE_FILES:
        for path in (expected / mode).iterdir():
            assert "uncertainty" not in path.read_text().lower(), path
    # Written from the commit BEFORE P3c-3, like the first case (module docstring).
    assert (REJECTIONS.root / "GENERATED_FROM").read_text().split()[0] == "566acc7"


@pytest.mark.parametrize("case", sorted(CASES))
def test_regenerating_in_a_tree_with_p3c3_code_is_refused(case):
    """The files pin the output from BEFORE P3c-3. `regenerate` must not
    rewrite them from this tree unless the caller says the change is
    deliberate -- and it must refuse before it deletes anything."""
    golden = CASES[case]
    before = sorted(p.name for p in golden.root.rglob("*"))
    with pytest.raises(SystemExit, match="refusing"):
        regenerate(golden)
    assert sorted(p.name for p in golden.root.rglob("*")) == before and len(before) >= 14


if __name__ == "__main__":
    _args = [arg for arg in sys.argv[1:] if arg != "--deliberate-change"]
    if len(_args) != 1 or _args[0] not in CASES:
        raise SystemExit(f"usage: python -m tests.eval.test_uncertainty_golden "
                         f"<{'|'.join(CASES)}> [--deliberate-change]")
    regenerate(CASES[_args[0]], deliberate_change="--deliberate-change" in sys.argv[1:])
