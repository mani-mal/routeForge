"""P3a Task 5: the CLI end to end, including the live-app baseline (§9.7)."""
from __future__ import annotations

import importlib.util
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


def _load_policy_simulate():
    """Import `scripts/policy_simulate.py` as a module, in-process, so a
    test can monkeypatch a name the module looks up (e.g. `shadow_prices`)
    and then call `main()` directly. `_run_cli`'s subprocess invocation runs
    in a separate process, so nothing monkeypatched here would reach it."""
    spec = importlib.util.spec_from_file_location("policy_simulate", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


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


def _run_cli_no_proposal(tmp_path, export_lines, snapshot, *extra):
    exp, snap = tmp_path / "e.ndjson", tmp_path / "s.json"
    exp.write_text("".join(json.dumps(line) + "\n" for line in export_lines))
    snap.write_text(json.dumps(snapshot))
    out = tmp_path / "out"
    proc = subprocess.run([sys.executable, str(SCRIPT), "--export", str(exp),
                           "--snapshot", str(snap), "--out", str(out), *extra],
                          capture_output=True, text=True, cwd=ROOT,
                          env={**__import__("os").environ,
                               "LITELLM_LOCAL_MODEL_COST_MAP": "True"})
    return proc, out


# --- P3c-1 Task 3: shadow prices in the CLI -----------------------------------

def test_shadow_prices_without_proposal(tmp_path):
    from tests.eval.sim_fixtures import app_entry
    snap = make_snapshot(apps=[app_entry("app-a", allowed_models=["ghost-model"])])
    lines = [_line(make_row(f"r{i}", "app-a")) for i in range(3)]
    proc, out = _run_cli_no_proposal(tmp_path, lines, snap, "--shadow-prices")
    assert proc.returncode == 0, proc.stderr
    sim = json.loads((out / "simulation.json").read_text())
    assert "shadow_prices" in sim and sim["shadow_prices"]["constraints"]
    assert sim["inputs"]["proposal_sha256"] is None
    assert sim["inputs"]["proposal_title"] is None
    report = (out / "report.md").read_text()
    assert "## Shadow prices" in report
    assert "What each constraint costs" in report


def test_shadow_prices_with_proposal_appends_to_p3a_output(tmp_path):
    from tests.eval.sim_fixtures import app_entry
    snap = make_snapshot(apps=[app_entry("app-a", allowed_models=["ghost-model"])])
    lines = [_line(make_row("r1", "app-a"))]
    proc, out = _run_cli(tmp_path, lines, snap, {"schema": P}, "--shadow-prices")
    assert proc.returncode == 0, proc.stderr
    sim = json.loads((out / "simulation.json").read_text())
    assert sim["shadow_prices"]["baseline"] == "snapshot+proposal"
    assert "totals" in sim  # P3a output still present
    report = (out / "report.md").read_text()
    assert report.startswith("> **Simulation only.**")
    assert "## Shadow prices" in report


def test_max_relaxations_zero_exits_2(tmp_path):
    proc, _ = _run_cli(tmp_path, [], make_snapshot(), {"schema": P},
                       "--shadow-prices", "--max-relaxations", "0")
    assert proc.returncode == 2, proc.stderr


def test_neither_proposal_nor_shadow_prices_exits_2(tmp_path):
    proc, _ = _run_cli_no_proposal(tmp_path, [], make_snapshot())
    assert proc.returncode == 2, proc.stderr


def test_shadow_prices_cli_deterministic(tmp_path):
    from tests.eval.sim_fixtures import app_entry
    snap = make_snapshot(apps=[app_entry("app-a", allowed_tiers=["frontier"])])
    lines = [_line(make_row("r1", "app-a"))]
    proc, out = _run_cli_no_proposal(tmp_path, lines, snap, "--shadow-prices")
    assert proc.returncode == 0, proc.stderr
    first = (out / "simulation.json").read_text()
    proc2, _ = _run_cli_no_proposal(tmp_path, lines, snap, "--shadow-prices")
    assert proc2.returncode == 0 and (out / "simulation.json").read_text() == first


def test_live_round_trip_shadow_prices_allowed_tiers(tmp_path):
    # Seeded on-prem models are local-8b (cheap) and local-32b (mid)
    # (app/apps/bootstrap.py); no model is seeded in "frontier", so an app
    # restricted to that tier gets a 422 on every request.
    with TestClient(create_app()) as c:
        _login(c)
        created = c.post("/admin/applications", json={"id": "shadow-app", "name": "Shadow"})
        assert created.status_code == 200, created.text
        token = created.json()["token"]
        put = c.put("/admin/applications/shadow-app/policy",
                    json={"allowed_tiers": ["frontier"]})
        assert put.status_code == 200, put.text
        for _ in range(2):
            resp = c.post("/v1/chat/completions", headers={"Authorization": f"Bearer {token}"},
                          json={"messages": [{"role": "user", "content": "hello"}]})
            assert resp.status_code == 422, resp.text
        snapshot = c.get("/admin/config-snapshot").json()
        export = [json.loads(x) for x in
                  c.get("/admin/outcomes/export").text.strip().splitlines()]
    proc, out = _run_cli_no_proposal(tmp_path, export, snapshot, "--shadow-prices")
    assert proc.returncode == 0, proc.stderr
    sim = json.loads((out / "simulation.json").read_text())
    entries = [e for e in sim["shadow_prices"]["constraints"]
              if e["requirement"] == "allowed_tiers" and e["scope"] == {"app": "shadow-app"}]
    assert len(entries) == 1, sim["shadow_prices"]["constraints"]
    assert entries[0]["rejections_avoided"] == 2


def test_shadow_invariant_breach_exits_2(tmp_path, monkeypatch, capsys):
    # Fix round 1 (reviewer finding): scripts/policy_simulate.py:140-145 --
    # a `ShadowInvariantError` from `shadow_prices` must exit 2 with
    # "internal invariant" on stderr. Patched in-process on the name the
    # script module actually looks up (imported there as
    # `from app.eval.shadow import ... shadow_prices`, so it lives as
    # `policy_simulate.shadow_prices`), and `main()` is called directly --
    # `_run_cli`'s subprocess invocation runs in a separate process, so a
    # monkeypatch here would never reach it.
    from app.eval.shadow import ShadowInvariantError

    ps = _load_policy_simulate()

    async def _boom(*_a, **_k):
        raise ShadowInvariantError("boom")
    monkeypatch.setattr(ps, "shadow_prices", _boom)

    exp, snap = tmp_path / "e.ndjson", tmp_path / "s.json"
    exp.write_text("")
    snap.write_text(json.dumps(make_snapshot()))
    out = tmp_path / "out"

    rc = ps.main(["--export", str(exp), "--snapshot", str(snap), "--out", str(out),
                 "--shadow-prices"])
    captured = capsys.readouterr()
    assert rc == 2
    assert "error: internal invariant" in captured.err
    assert "boom" in captured.err
    # The script only calls `out.mkdir` and writes simulation.json/report.md
    # after the try/except around simulate()/shadow_prices() returns
    # normally (scripts/policy_simulate.py:153+); an invariant breach
    # returns 2 from inside that except block, before `out` is even
    # created.
    assert not out.exists()
    assert not (out / "simulation.json").exists()


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
