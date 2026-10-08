"""P2 evaluation metrics, invariant E1: with none of the new flags
(`--group-report`, `--candidate`), every deterministic output of
`scripts/feasible_bench.py` is byte-identical to what it was before this
slice.

Four cases, all simulated. Each pins `metrics.json`, `matrix.jsonl`,
`choices.jsonl` and the body of `report.md` (everything from "## How to read
this" on: the "## Run" block above it holds timestamps and the git sha, and
`run.json` holds the same, so neither is pinned):

    starter        starter suite x healthcare-sim x the b45 weight variants,
                   seed 0
    starter_seed1  the same suite and endpoints at seed 1, no variants: at
                   this seed some cases have no passing endpoint
                   (infeasible at floor), a path seed 0 never takes
    outage         outage suite x outage-sim, seed 0 (error rows, failover
                   chains, the breaker arm and its control)
    empty          empty-set suite x cloud-only-sim, seed 0 (every case
                   rejected)

`fixtures/bench_golden/inputs/` holds a COPY of every input, the four config
YAMLs included, so the expected files do not move when `eval/` or `config/`
is edited: this pins the harness code, not today's configuration.

**The expected files were written from the tree as it was BEFORE any product
code of this slice existed** (`GENERATED_FROM` names the commit). That is what
makes the pin mean "unchanged from before": a file regenerated from the
current tree would only say the current tree agrees with itself. So never
regenerate a case to make a test pass; a failure is a real change to what an
operator who passes no new flag sees.

After a DELIBERATE change to the bench's default output, regenerate and
review every changed byte:

    LITELLM_LOCAL_MODEL_COST_MAP=True uv run python -m tests.eval.test_bench_golden --deliberate-change

Without `--deliberate-change` the command refuses to run in a tree that has
`app/eval/groups.py`.
"""
from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
FIXTURE = Path(__file__).parent / "fixtures" / "bench_golden"
INPUTS = FIXTURE / "inputs"
EXPECTED = FIXTURE / "expected"
RUN_TIMEOUT_S = 180
REPORT_BODY_START = "## How to read this"
PINNED = ("metrics.json", "matrix.jsonl", "choices.jsonl", "report_body.md")

#: case -> (suite, endpoints, variants or None, seed); file names under `inputs/`.
CASES: dict[str, tuple[str, str, str | None, int]] = {
    "starter": ("starter.jsonl", "healthcare-sim.yaml", "b45.yaml", 0),
    "starter_seed1": ("starter.jsonl", "healthcare-sim.yaml", None, 1),
    "outage": ("outage.jsonl", "outage-sim.yaml", None, 0),
    "empty": ("empty-set.jsonl", "cloud-only-sim.yaml", None, 0),
}

#: where each input is copied FROM when the fixture is (re)generated.
_SOURCES = {
    "starter.jsonl": "eval/suites/starter.jsonl",
    "outage.jsonl": "eval/suites/outage.jsonl",
    "empty-set.jsonl": "eval/suites/empty-set.jsonl",
    "healthcare-sim.yaml": "eval/endpoints/healthcare-sim.yaml",
    "outage-sim.yaml": "eval/endpoints/outage-sim.yaml",
    "cloud-only-sim.yaml": "eval/endpoints/cloud-only-sim.yaml",
    "b45.yaml": "eval/variants/b45.yaml",
    "config/serving.yaml": "config/serving.yaml",
    "config/routing.yaml": "config/routing.yaml",
    "config/policy.yaml": "config/policy.yaml",
    "config/pricing.yaml": "config/pricing.yaml",
}


def report_body(report: str) -> str:
    """`report.md` from the first section after "## Run" to the end."""
    return report[report.index(REPORT_BODY_START):]


def run_case(name: str, out: Path) -> dict[str, bytes]:
    """Run one golden case into `out`; returns the four pinned files' bytes.

    `runner.load_settings` is pointed at the fixture's own config copy, and
    `SP_PII_DETECTION` is forced to `regex`, for the duration of the run."""
    from app.config.loader import load_settings
    from app.eval import runner

    suite, endpoints, variants, seed = CASES[name]
    real_load, real_env = runner.load_settings, os.environ.get("SP_PII_DETECTION")
    runner.load_settings = lambda: load_settings(str(INPUTS / "config"))
    os.environ["SP_PII_DETECTION"] = "regex"
    try:
        result = asyncio.run(asyncio.wait_for(runner.run_bench(
            str(INPUTS / suite), str(INPUTS / endpoints), out_dir=str(out), seed=seed,
            variants_path=str(INPUTS / variants) if variants else None),
            timeout=RUN_TIMEOUT_S))
    finally:
        runner.load_settings = real_load
        if real_env is None:
            os.environ.pop("SP_PII_DETECTION", None)
        else:
            os.environ["SP_PII_DETECTION"] = real_env
    assert result.exit_code == 0, f"golden case {name!r} exited {result.exit_code}"
    files = {n: (out / n).read_bytes() for n in PINNED[:3]}
    files["report_body.md"] = report_body(
        (out / "report.md").read_text(encoding="utf-8")).encode("utf-8")
    return files


@pytest.mark.parametrize("name", sorted(CASES))
def test_e1_default_output_is_byte_identical(name, tmp_path):
    got = run_case(name, tmp_path / "out")
    for filename in PINNED:
        expected = (EXPECTED / name / filename).read_bytes()
        assert got[filename] == expected, (
            f"{name}/{filename} changed. The bench's default output is pinned "
            "(invariant E1): find the change that moved it; do not regenerate.")


def test_e1_the_default_run_writes_exactly_the_five_files(tmp_path):
    run_case("empty", tmp_path / "out")
    assert sorted(p.name for p in (tmp_path / "out").iterdir()) == [
        "choices.jsonl", "matrix.jsonl", "metrics.json", "report.md", "run.json"]


def test_the_fixture_names_the_commit_it_was_generated_from():
    text = (FIXTURE / "GENERATED_FROM").read_text(encoding="utf-8")
    assert "commit: " in text and "unknown" not in text


def _git_sha() -> str:
    proc = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True,
                          text=True, timeout=10)
    return proc.stdout.strip() if proc.returncode == 0 and proc.stdout.strip() else "unknown"


def _generate(deliberate: bool) -> int:
    if (ROOT / "app" / "eval" / "groups.py").exists() and not deliberate:
        print("refusing: app/eval/groups.py exists, so this tree is no longer the "
              "pre-slice tree. Pass --deliberate-change only after a deliberate "
              "change to the bench's default output.", file=sys.stderr)
        return 2
    for target, source in _SOURCES.items():
        dest = INPUTS / target
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / source, dest)
    scratch = FIXTURE / "_scratch"
    for name in sorted(CASES):
        shutil.rmtree(scratch, ignore_errors=True)
        files = run_case(name, scratch)
        (EXPECTED / name).mkdir(parents=True, exist_ok=True)
        for filename, data in files.items():
            (EXPECTED / name / filename).write_bytes(data)
    shutil.rmtree(scratch, ignore_errors=True)
    (FIXTURE / "GENERATED_FROM").write_text(
        f"commit: {_git_sha()}\n"
        "command: LITELLM_LOCAL_MODEL_COST_MAP=True uv run python -m "
        "tests.eval.test_bench_golden\n"
        "Never regenerate to make a test pass (see tests/eval/test_bench_golden.py).\n",
        encoding="utf-8")
    print(f"wrote {FIXTURE}")
    return 0


if __name__ == "__main__":
    sys.exit(_generate("--deliberate-change" in sys.argv[1:]))
