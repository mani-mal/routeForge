"""The FeasibleRouteBench `router_floor` arm (B80 Task 9, spec §4.6 / C5):
the real router with quality floors on `enforce` and a `QualityResolver`
loaded with split-fitted lower bounds, scored beside the existing arms.

Simulated runs only, through the REAL gate, `feasible_set`, pipeline and
`Router.select`. The deployment's per-workload floors are operator config
(`routing.quality_floors.workloads`); these tests set them on the settings the
runner loads, never from the suite.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
import yaml

from app.eval import runner
from app.eval.floor_fit import BOUNDS_SCHEMA, split_cases, suite_hash
from app.eval.cases import load_suite
from app.eval.runner import run_bench
from app.eval.strategies import router_attempts
from app.routing.quality import QualityResolver

RUN_TIMEOUT_S = 120


def _endpoint(slug, *, provider="onprem", tier="cheap", price=0.1, quality=0.9,
              raw_ok=True, latency=100.0) -> dict:
    return {
        "slug": slug, "provider": provider, "tier": tier,
        "price": {"input": price, "output": price},
        "region": "us", "prompt_retention": "none", "accepts_raw_sensitive": raw_ok,
        "simulated": {"quality": {"default": quality},
                      "latency_ms": {"mean": latency, "jitter": 20}},
    }


ENDPOINTS = [
    _endpoint("local-a", tier="cheap", price=0.1, quality=0.8, latency=300),
    _endpoint("local-b", tier="mid", price=0.5, quality=0.95, latency=500),
    _endpoint("cloud-c", provider="azure", tier="frontier", price=5.0, quality=1.0,
              raw_ok=False, latency=900),
]


def _case(case_id, content, *, workload="faq") -> dict:
    return {
        "id": case_id,
        "messages": [{"role": "user", "content": content}],
        "app_policy": {"residency": "any", "policy_mode": "enforced",
                       "pii_detection": "regex"},
        "grader": {"kind": "contains", "expected": "paris"},
        "tags": {"workload": workload},
    }


# A long prompt pushes the complexity tier up (tokens_after / 2000), so the
# floor-free router would not pick the cheap tier on it.
LONG = "Please answer carefully. " * 400 + "What is the capital of France?"
CASES = [
    _case("faq-1", "What is the capital of France?"),
    _case("faq-2", LONG),
    _case("faq-3", "Name the French capital city."),
    _case("faq-4", LONG + " Again."),
]


def _write(tmp_path, cases=CASES) -> tuple[str, str]:
    suite = tmp_path / "suite.jsonl"
    suite.write_text("\n".join(json.dumps(c) for c in cases) + "\n")
    eps = tmp_path / "endpoints.yaml"
    eps.write_text(yaml.safe_dump({"endpoints": ENDPOINTS}))
    return str(suite), str(eps)


def _bounds(tmp_path, bounds, seed=0, name="bounds.json", digest="suite") -> str:
    """A bounds file stamped with the suite written by `_write` (default),
    an explicit `digest`, or none (`digest=None`, a hand-written file)."""
    doc = {"schema": BOUNDS_SCHEMA, "seed": seed, "bounds": bounds}
    if digest == "suite":
        cases, _ = load_suite(str(tmp_path / "suite.jsonl"))
        doc["suite_hash"] = suite_hash(cases)
    elif digest is not None:
        doc["suite_hash"] = digest
    path = tmp_path / name
    path.write_text(json.dumps(doc))
    return str(path)


FAVOUR_CHEAP = [
    {"model": "local-a", "workload": "faq", "lower": 0.9},
    {"model": "local-b", "workload": "faq", "lower": 0.6},
    {"model": "cloud-c", "workload": None, "lower": 0.95},
]


@pytest.fixture
def floors(monkeypatch):
    """Set the deployment's per-workload floors on the settings the runner
    loads (operator config, as `config/routing.yaml` would)."""
    def _set(workloads: dict[str, float], mode: str = "off"):
        real = runner.load_settings

        def patched():
            s = real()
            s.routing.quality_floors.workloads = dict(workloads)
            s.routing.quality_floors.mode = mode
            return s
        monkeypatch.setattr(runner, "load_settings", patched)
    return _set


def _run(suite, eps, out, **kwargs):
    return asyncio.run(asyncio.wait_for(
        run_bench(suite, eps, out_dir=str(out), **kwargs), timeout=RUN_TIMEOUT_S))


def _jsonl(path) -> list[dict]:
    return [json.loads(line) for line in Path(path).read_text().splitlines() if line.strip()]


# --------------------------------------------------------------------------
# strategies.router_attempts takes the resolver
# --------------------------------------------------------------------------

def test_router_attempts_passes_the_quality_resolver(monkeypatch):
    seen = {}

    class Spy:
        def __init__(self, settings, quality_resolver=None, registry=None, signals=None):
            seen["quality"] = quality_resolver

    from app.eval import strategies
    monkeypatch.setattr(strategies, "Router", Spy)
    from app.config.loader import load_settings
    s = load_settings()
    q = QualityResolver(s)
    router_attempts(s, None, None, 1, quality=q)
    assert seen["quality"] is q
    router_attempts(s, None, None, 1)
    assert seen["quality"] is None


# --------------------------------------------------------------------------
# the arm
# --------------------------------------------------------------------------

def test_floor_arm_runs_and_is_no_dearer_than_router_cold(tmp_path, floors):
    floors({"faq": 0.8})
    suite, eps = _write(tmp_path)
    out = tmp_path / "out"
    result = _run(suite, eps, out, floor_bounds_path=_bounds(tmp_path, FAVOUR_CHEAP),
                  floor_allow_unsplit=True)
    assert result.exit_code == 0
    metrics = json.loads((out / "metrics.json").read_text())
    strategies = metrics["strategies"]
    assert "router_floor" in strategies
    assert strategies["router_floor"]["violation_rate"] == 0.0
    assert (strategies["router_floor"]["mean_chosen_cost"]
            <= strategies["router_cold"]["mean_chosen_cost"])

    floor = metrics["router_floor"]
    assert floor["mode"] == "enforce"
    assert floor["workload_floors"] == {"faq": 0.8}
    assert floor["split"] is None and floor["n_cases"] == 4
    assert floor["heldout"] is False
    assert "not a held-out result" in floor["warning"]
    assert floor["effective_mode"] == {"enforce": 4}
    assert floor["floor_status"] == {"met": 4}
    assert floor["coverage"] == 1.0
    # quality, coverage and cost reported together, against every baseline
    for arm in ("router_floor", "router_cold", "cheapest", "best_published_quality"):
        row = floor["comparison"][arm]
        assert set(row) >= {"pass_rate", "mean_chosen_cost", "quality_regret"}
    assert set(floor["vs_router_cold"]) == {"pass_rate_delta", "mean_chosen_cost_delta"}

    choices = [c for c in _jsonl(out / "choices.jsonl") if c["strategy"] == "router_floor"]
    assert [c["endpoint"] for c in choices] == ["local-a"] * 4
    assert all(c["floor_status"] == "met" for c in choices)
    # other arms' choice records carry no floor field
    assert all("floor_status" not in c for c in _jsonl(out / "choices.jsonl")
               if c["strategy"] != "router_floor")
    report = (out / "report.md").read_text()
    assert "## Quality floor arm" in report
    run = json.loads((out / "run.json").read_text())
    assert "router_floor" in run["strategies"] and "router_floor" in run["notes"]
    assert run["args"]["floor_bounds"].endswith("bounds.json")


def test_floor_arm_with_no_qualifier_reports_unmet_not_hidden(tmp_path, floors):
    floors({"faq": 0.99})
    suite, eps = _write(tmp_path)
    out = tmp_path / "out"
    result = _run(suite, eps, out, floor_bounds_path=_bounds(tmp_path, FAVOUR_CHEAP),
                  floor_allow_unsplit=True)
    assert result.exit_code == 0
    floor = json.loads((out / "metrics.json").read_text())["router_floor"]
    assert floor["floor_status"] == {"unmet_no_qualifier": 4}
    assert floor["coverage"] == 0.0


def test_floor_arm_without_configured_floors_reports_none(tmp_path, floors):
    floors({})
    suite, eps = _write(tmp_path)
    out = tmp_path / "out"
    result = _run(suite, eps, out, floor_bounds_path=_bounds(tmp_path, FAVOUR_CHEAP),
                  floor_allow_unsplit=True)
    assert result.exit_code == 0
    metrics = json.loads((out / "metrics.json").read_text())
    floor = metrics["router_floor"]
    assert floor["workload_floors"] == {}
    assert floor["floor_status"] == {"none": 4}
    assert floor["coverage"] is None
    # no floor anywhere -> the arm IS the cold router (identity, global constraint)
    choices = _jsonl(out / "choices.jsonl")
    pick = lambda arm: [c["attempts"] for c in choices if c["strategy"] == arm]  # noqa: E731
    assert pick("router_floor") == pick("router_cold")


def test_floor_arm_forces_enforce_over_the_deployment_mode(tmp_path, floors):
    floors({"faq": 0.8}, mode="off")
    suite, eps = _write(tmp_path)
    out = tmp_path / "out"
    _run(suite, eps, out, floor_bounds_path=_bounds(tmp_path, FAVOUR_CHEAP),
                  floor_allow_unsplit=True)
    assert json.loads((out / "metrics.json").read_text())["router_floor"][
        "floor_status"] == {"met": 4}


def test_heldout_split_scores_only_the_held_out_cases(tmp_path, floors):
    floors({"faq": 0.8})
    suite, eps = _write(tmp_path)
    out = tmp_path / "out"
    result = _run(suite, eps, out, floor_bounds_path=_bounds(tmp_path, FAVOUR_CHEAP, seed=3),
                  floor_split="heldout")
    assert result.exit_code == 0
    cases, _ = load_suite(suite)
    _, held = split_cases(cases, seed=3)
    metrics = json.loads((out / "metrics.json").read_text())
    assert sorted(c["case_id"] for c in metrics["cases"]) == sorted(held)
    for name, m in metrics["strategies"].items():
        assert m["n_cases"] == len(held), name
    floor = metrics["router_floor"]
    assert floor["split"] == "heldout" and floor["split_seed"] == 3
    assert floor["heldout"] is True and "warning" not in floor
    assert floor["n_cases"] == len(held)
    assert {r["case_id"] for r in _jsonl(out / "matrix.jsonl")} == set(held)


def test_no_bounds_leaves_outputs_without_the_arm(tmp_path, floors):
    floors({"faq": 0.8})
    suite, eps = _write(tmp_path)
    out = tmp_path / "out"
    _run(suite, eps, out)
    metrics = json.loads((out / "metrics.json").read_text())
    assert "router_floor" not in metrics
    assert "router_floor" not in metrics["strategies"]
    assert all("floor_status" not in c for c in _jsonl(out / "choices.jsonl"))
    run = json.loads((out / "run.json").read_text())
    assert "floor_bounds" not in run["args"] and "router_floor" not in run["notes"]


@pytest.mark.parametrize("kwargs, needs_bounds", [
    ({"floor_split": "heldout"}, False),
    ({"split_seed": 1}, False),
    ({"floor_split": "train"}, True),
    ({"floor_split": "heldout", "split_seed": 9}, True),   # != the bounds' seed
    ({"floor_allow_unsplit": True}, False),
    ({}, True),                                            # bounds, no split, no opt-out
    ({"floor_split": "heldout", "floor_allow_unsplit": True}, True),
])
def test_bad_floor_arguments_exit_1(tmp_path, floors, kwargs, needs_bounds, capsys):
    floors({"faq": 0.8})
    suite, eps = _write(tmp_path)
    if needs_bounds:
        kwargs = {**kwargs, "floor_bounds_path": _bounds(tmp_path, FAVOUR_CHEAP, seed=0)}
    out = tmp_path / "out"
    result = _run(suite, eps, out, **kwargs)
    assert result.exit_code == 1
    assert "load error" in capsys.readouterr().err
    assert not out.exists()


def test_bad_bounds_file_exits_1(tmp_path, floors):
    floors({"faq": 0.8})
    suite, eps = _write(tmp_path)
    path = tmp_path / "bounds.json"
    path.write_text(json.dumps({"schema": "nope", "seed": 0, "bounds": []}))
    result = _run(suite, eps, tmp_path / "out", floor_bounds_path=str(path),
                  floor_split="heldout")
    assert result.exit_code == 1


def test_bounds_without_split_are_refused_by_default(tmp_path, floors, capsys):
    floors({"faq": 0.8})
    suite, eps = _write(tmp_path)
    result = _run(suite, eps, tmp_path / "out",
                  floor_bounds_path=_bounds(tmp_path, FAVOUR_CHEAP))
    assert result.exit_code == 1
    assert "--floor-split heldout" in capsys.readouterr().err
    assert not (tmp_path / "out").exists()


def test_unsplit_opt_out_warns_on_stderr(tmp_path, floors, capsys):
    floors({"faq": 0.8})
    suite, eps = _write(tmp_path)
    out = tmp_path / "out"
    result = _run(suite, eps, out, floor_bounds_path=_bounds(tmp_path, FAVOUR_CHEAP),
                  floor_allow_unsplit=True)
    assert result.exit_code == 0
    assert "not a held-out result" in capsys.readouterr().err
    assert "**Warning:**" in (out / "report.md").read_text()


def test_bounds_fitted_on_another_suite_are_refused(tmp_path, floors, capsys):
    floors({"faq": 0.8})
    suite, eps = _write(tmp_path)
    result = _run(suite, eps, tmp_path / "out", floor_split="heldout",
                  floor_bounds_path=_bounds(tmp_path, FAVOUR_CHEAP, digest="0" * 64))
    assert result.exit_code == 1
    assert "different suite" in capsys.readouterr().err


def test_bounds_without_suite_hash_are_refused(tmp_path, floors, capsys):
    floors({"faq": 0.8})
    suite, eps = _write(tmp_path)
    result = _run(suite, eps, tmp_path / "out", floor_split="heldout",
                  floor_bounds_path=_bounds(tmp_path, FAVOUR_CHEAP, digest=None))
    assert result.exit_code == 1
    assert "refit" in capsys.readouterr().err


def test_effective_mode_counts_show_per_app_overrides(tmp_path, floors):
    floors({"faq": 0.8})
    cases = [dict(c) for c in CASES]
    cases[0] = {**cases[0], "app_policy": {**cases[0]["app_policy"],
                                           "quality_floor_mode": "shadow"}}
    suite, eps = _write(tmp_path, cases)
    out = tmp_path / "out"
    result = _run(suite, eps, out, floor_bounds_path=_bounds(tmp_path, FAVOUR_CHEAP),
                  floor_allow_unsplit=True)
    assert result.exit_code == 0
    floor = json.loads((out / "metrics.json").read_text())["router_floor"]
    assert floor["mode"] == "enforce"
    assert floor["effective_mode"] == {"enforce": 3, "shadow": 1}
    assert floor["floor_status"] == {"met": 3, "shadow_met": 1}


# --------------------------------------------------------------------------
# CLI flags
# --------------------------------------------------------------------------

def test_feasible_bench_cli_passes_floor_flags(monkeypatch, tmp_path):
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "feasible_bench_cli",
        Path(__file__).resolve().parents[2] / "scripts" / "feasible_bench.py")
    cli = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cli)
    seen = {}

    async def fake_run_bench(suite, endpoints, **kwargs):
        seen.update(kwargs)
        return runner.RunResult(exit_code=0, out_dir=str(tmp_path), metrics={})

    monkeypatch.setattr(cli, "run_bench", fake_run_bench)
    assert cli.main(["--suite", "s", "--endpoints", "e", "--out", str(tmp_path),
                     "--floor-bounds", "b.json", "--floor-split", "heldout",
                     "--split-seed", "4"]) == 0
    assert (seen["floor_bounds_path"], seen["floor_split"], seen["split_seed"]) == (
        "b.json", "heldout", 4)
    assert seen["floor_allow_unsplit"] is False
    seen.clear()
    assert cli.main(["--suite", "s", "--endpoints", "e", "--out", str(tmp_path),
                     "--floor-bounds", "b.json", "--floor-allow-unsplit"]) == 0
    assert seen["floor_allow_unsplit"] is True
    seen.clear()
    assert cli.main(["--suite", "s", "--endpoints", "e", "--out", str(tmp_path)]) == 0
    assert (seen["floor_bounds_path"], seen["floor_split"], seen["split_seed"]) == (
        None, None, None)
