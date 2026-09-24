"""P3a Task 5: the CLI end to end, including the live-app baseline (§9.7)."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from fastapi.testclient import TestClient

from app.main import create_app
from tests.eval.sim_fixtures import make_row, make_snapshot
from tests.test_admin_api import _login

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "policy_simulate.py"
P = "routeforge.policy-proposal/v1"


def _run_cli(tmp_path, export_lines, snapshot, proposal, *extra):
    exp, snap, prop = tmp_path / "e.ndjson", tmp_path / "s.json", tmp_path / "p.json"
    exp.write_text("".join(json.dumps(line) + "\n" for line in export_lines))
    snap.write_text(json.dumps(snapshot))
    prop.write_text(json.dumps(proposal))
    out = tmp_path / "out"
    proc = subprocess.run([sys.executable, str(SCRIPT), "--export", str(exp),
                           "--snapshot", str(snap), "--proposal", str(prop),
                           "--out", str(out), *extra],
                          capture_output=True, text=True, cwd=ROOT,
                          env={**__import__("os").environ,
                               "LITELLM_LOCAL_MODEL_COST_MAP": "True"})
    return proc, out


def _line(row):
    feedback = row.pop("feedback", [])
    return {"schema": "routeforge.outcome/v1", "outcome": row, "feedback": feedback}


def test_cli_writes_deterministic_outputs(tmp_path):
    lines = [_line(make_row(f"r{i}", "app-a")) for i in range(3)]
    proc, out = _run_cli(tmp_path, lines, make_snapshot(), {"schema": P})
    assert proc.returncode == 0, proc.stderr
    first = (out / "simulation.json").read_text()
    assert (out / "report.md").read_text().startswith("> **Simulation only.**")
    proc2, _ = _run_cli(tmp_path, lines, make_snapshot(), {"schema": P})
    assert proc2.returncode == 0 and (out / "simulation.json").read_text() == first


def test_cli_exit_2_on_bad_proposal(tmp_path):
    proc, _ = _run_cli(tmp_path, [], make_snapshot(), {"schema": P, "bogus": 1})
    assert proc.returncode == 2 and "bogus" in proc.stderr


def test_cli_exit_2_on_snapshot_deep_validation_failure(tmp_path):
    """`load_snapshot`'s shallow structural check passes (schema, required
    top-level keys, unique app ids), but `simulate()`'s deep validation
    (`build_settings`, via `apply_proposal`) rejects a provider the running
    deployment's serving.yaml never declared. Spec §8: exit 2 covers "an
    input file failed to load OR VALIDATE" -- not just the narrower load
    errors caught before `simulate()` runs."""
    snap = make_snapshot()
    snap["settings"]["providers"]["mystery-provider"] = {
        "enabled": True, "region": "onprem", "prompt_retention": "none",
        "accepts_raw_sensitive": True}
    proc, _ = _run_cli(tmp_path, [], snap, {"schema": P})
    assert proc.returncode == 2, proc.stderr
    assert "mystery-provider" in proc.stderr


def test_cli_exit_3_on_new_rejections_when_flagged(tmp_path):
    from tests.eval.sim_fixtures import app_entry
    snap = make_snapshot(apps=[app_entry("app-a", allowed_models=["local-mid"])])
    lines = [_line(make_row("r1", "app-a"))]
    prop = {"schema": P, "endpoints": {"remove": ["local-mid"]}}
    proc, _ = _run_cli(tmp_path, lines, snap, prop, "--fail-on-new-rejections")
    assert proc.returncode == 3
    proc2, _ = _run_cli(tmp_path, lines, snap, prop)
    assert proc2.returncode == 0


def test_live_round_trip_baseline_is_perfect(tmp_path):                   # §9.7
    with TestClient(create_app()) as c:
        for _ in range(4):
            r = c.post("/v1/chat/completions", headers={"Authorization": "Bearer demo-key"},
                       json={"messages": [{"role": "user", "content": "hello"}]})
            assert r.status_code == 200, r.text
        _login(c)
        snapshot = c.get("/admin/config-snapshot").json()
        export = [json.loads(x) for x in
                  c.get("/admin/outcomes/export").text.strip().splitlines()]
    proc, out = _run_cli(tmp_path, export, snapshot, {"schema": P})
    assert proc.returncode == 0, proc.stderr
    sim = json.loads((out / "simulation.json").read_text())
    assert sim["rows"]["simulated"] == 4
    assert sim["baseline"]["agreement"] == 1.0
