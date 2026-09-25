"""P3a Task 5: the CLI end to end, including the live-app baseline (§9.7)."""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
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


def test_live_round_trip_pinned_baseline_is_perfect(tmp_path):            # B55
    """Requests pinned to the model the unpinned router would NOT pick replay
    to that pin: the recorded `requested_model` (and `max_tokens`) survive the
    real export and the simulator honours them."""
    with TestClient(create_app()) as c:
        auth = {"Authorization": "Bearer demo-key"}
        hello = {"messages": [{"role": "user", "content": "hello"}]}
        unpinned = c.post("/v1/chat/completions", headers=auth, json=hello).json()["model"]
        pin = "local-32b" if unpinned != "local-32b" else "local-8b"
        for _ in range(3):
            r = c.post("/v1/chat/completions", headers=auth,
                       json={**hello, "model": pin, "max_tokens": 64})
            assert r.status_code == 200, r.text
            assert r.json()["model"] == pin
        _login(c)
        snapshot = c.get("/admin/config-snapshot").json()
        export = [json.loads(x) for x in
                  c.get("/admin/outcomes/export").text.strip().splitlines()]
    pinned_rows = [e for e in export if e["outcome"]["requested_model"] == pin]
    assert len(pinned_rows) == 3
    assert all(e["outcome"]["max_tokens"] == 64 for e in pinned_rows)
    proc, out = _run_cli(tmp_path, export, snapshot, {"schema": P})
    assert proc.returncode == 0, proc.stderr
    sim = json.loads((out / "simulation.json").read_text())
    assert sim["rows"]["simulated"] == 4
    assert sim["baseline"]["agreement"] == 1.0


def test_cli_echoes_snapshot_serving_policy(tmp_path):                  # Important 1
    snap = make_snapshot(serving_policy_over={"default_policy_mode": "passthrough"})
    proc, out = _run_cli(tmp_path, [_line(make_row("r1", "app-a"))], snap, {"schema": P})
    assert proc.returncode == 0, proc.stderr
    sim = json.loads((out / "simulation.json").read_text())
    assert sim["inputs"]["serving_policy"] == snap["settings"]["serving_policy"]
    assert sim["inputs"]["serving_policy"]["default_policy_mode"] == "passthrough"


def _drop(d, *path):
    for k in path[:-1]:
        d = d[k]
    del d[path[-1]]


_BAD_SNAPSHOTS = {
    "settings.routing": lambda s: _drop(s, "settings", "routing"),
    "settings.serving_policy": lambda s: _drop(s, "settings", "serving_policy"),
    "settings.providers.onprem.region": lambda s: _drop(s, "settings", "providers", "onprem",
                                                        "region"),
    "onboarded.local-cheap.tier": lambda s: _drop(s, "onboarded", 0, "tier"),
    "apps.app-a.policy": lambda s: s["apps"][0].update(policy=[]),
}
_BAD_PROPOSALS = {
    "apps": {"apps": []},
    "prices": {"prices": []},
    "endpoints.update": {"endpoints": {"update": []}},
    "endpoints.remove[0]": {"endpoints": {"remove": [{"slug": "local-mid"}]}},
    "apps.app-a.allowed_tiers": {"apps": {"app-a": {"allowed_tiers": [["cheap"]]}}},
    "apps.app-a.sensitivity_rules": {"apps": {"app-a": {"sensitivity_rules": 5}}},
    "endpoints.add.new-x.allowed_app_ids": {"endpoints": {"add": [{
        "slug": "new-x", "provider": "onprem", "tier": "mid",
        "price": {"input": 1, "output": 1, "cached_input": None},
        "allowed_app_ids": [["app-a"]]}]}},
}


@pytest.mark.parametrize("kind,where", [("snapshot", w) for w in _BAD_SNAPSHOTS]
                         + [("proposal", w) for w in _BAD_PROPOSALS]
                         + [("config_dir", "reject_stream_requests"),
                            ("config_dir", "--config-dir")])
def test_cli_malformed_input_exits_2_naming_location(tmp_path, kind, where):  # Important 3
    snap, prop, extra = make_snapshot(), {"schema": P}, []
    if kind == "snapshot":
        _BAD_SNAPSHOTS[where](snap)
    elif kind == "proposal":
        prop.update(_BAD_PROPOSALS[where])
    else:
        # A --config-dir that cannot load: a required key missing (KeyError
        # in load_settings) or unparseable YAML. Exit 2 naming --config-dir.
        cfg = tmp_path / "cfg"
        shutil.copytree(ROOT / "config", cfg)
        serving = cfg / "serving.yaml"
        if where == "reject_stream_requests":
            serving.write_text("\n".join(
                ln for ln in serving.read_text().splitlines()
                if "reject_stream_requests:" not in ln) + "\n")
        else:
            serving.write_text("providers: [unclosed\n")
        extra = ["--config-dir", str(cfg)]
    proc, _ = _run_cli(tmp_path, [_line(make_row("r1", "app-a"))], snap, prop, *extra)
    assert proc.returncode == 2, proc.stderr
    assert "Traceback" not in proc.stderr
    assert where in proc.stderr
    if kind == "config_dir":
        assert "--config-dir" in proc.stderr


def test_cli_malformed_export_rows_are_counted(tmp_path):                 # Important 3
    bad_types = make_row("r2", "app-a")
    del bad_types["detected_entity_types"]
    bad_id = make_row("r3", "app-a")
    del bad_id["request_id"]
    lines = [_line(make_row("r1", "app-a")), _line(bad_types), _line(bad_id),
             _line(make_row("r4", "app-a", prompt_tokens="many")), [1, 2]]
    proc, out = _run_cli(tmp_path, lines, make_snapshot(), {"schema": P})
    assert proc.returncode == 0, proc.stderr
    sim = json.loads((out / "simulation.json").read_text())
    assert sim["rows"]["simulated"] == 1
    assert sim["rows"]["excluded"]["missing_features"] == 3
    assert "line 5" in proc.stderr          # the non-object line is a skipped-line warning
