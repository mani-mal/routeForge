"""Cost of the offline replay (scripts/policy_simulate.py) over live run 4.

    export LITELLM_LOCAL_MODEL_COST_MAP=True
    export SP_CONFIG_OVERLAY_DIR=$PWD/deploy/dgx_lab/config-overlay
    uv run python docs/research/data/paper-revision/replay_overhead.py

Part 1 times the CLI end to end (separate process each time, so interpreter start, imports,
loading the export and snapshot, building the scenario, replaying and writing the report are
all inside the number):
  (a) plain replay    : one empty proposal  (the CLI needs a proposal; {} changes nothing)
  (b) --scenarios     : run 4's scenarios.json (5 scenarios, one shared current arm)
  (c) --uncertainty   : (a) and (b) again with --uncertainty at its defaults
                        (1000 bootstrap resamples, 0.90 interval, seed 0, min-rows 30)
Part 2 times, in process, the pieces: loading, scenario build, and simulate_row per row
(median / p99 over the rows), plus the process start+import floor (`python -c "import ..."`).
"""
import asyncio
import json
import os
import platform
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[3]
os.chdir(ROOT)
sys.path.insert(0, str(ROOT))
os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "True")
RUN = ROOT / "docs/research/data/live-dgx-lab-2026-10-09-run4"
REPS = 5


def q(xs, p):
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(p * len(xs)))]


def timed_cli(label, args, reps=REPS):
    xs = []
    for _ in range(reps):
        with tempfile.TemporaryDirectory() as out:
            t = time.perf_counter()
            p = subprocess.run([sys.executable, "scripts/policy_simulate.py",
                                "--export", str(RUN / "outcomes.ndjson"),
                                "--snapshot", str(RUN / "config-snapshot.json"),
                                "--out", out, *args], capture_output=True, text=True)
            xs.append(time.perf_counter() - t)
            if p.returncode != 0:
                print(f"{label}: FAILED rc={p.returncode}: {p.stderr[-400:]}")
                return None
    print(f"{label:58s} median {statistics.median(xs):6.2f} s  min {min(xs):6.2f}  max {max(xs):6.2f}  "
          f"(n={len(xs)} runs)")
    return statistics.median(xs)


async def in_process():
    from app.config.loader import load_settings
    from app.eval.outcomes import load_export
    from app.eval.scenario import load_snapshot
    from app.eval.simulate import (build_scenario, close_scenario, observed_latencies, select_rows,
                                   simulate_row)
    t = time.perf_counter()
    records, _ = load_export(RUN / "outcomes.ndjson")
    snap, _ = load_snapshot(RUN / "config-snapshot.json")
    t_load = time.perf_counter() - t
    base = load_settings(ROOT / "config")
    t = time.perf_counter()
    scn = await build_scenario(snap, base=base)
    t_build = time.perf_counter() - t
    try:
        obs = observed_latencies(records)
        rows, excluded = select_rows(records, set(scn.apps))
        # warm-up pass (first calls import lazily / fill caches), then 5 timed passes over all rows
        for r in rows:
            simulate_row(scn, r, obs)
        per_row = []
        passes = []
        for _ in range(REPS):
            t0 = time.perf_counter()
            for r in rows:
                a = time.perf_counter_ns()
                simulate_row(scn, r, obs)
                per_row.append(time.perf_counter_ns() - a)
            passes.append(time.perf_counter() - t0)
    finally:
        await close_scenario(scn)
    print(f"rows: {len(records)} exported, {len(rows)} simulated, excluded {dict(excluded)}")
    print(f"load export + snapshot            : {t_load*1e3:8.1f} ms")
    print(f"build_scenario (in-memory DB etc.): {t_build*1e3:8.1f} ms")
    print(f"simulate_row, one arm, per row    : median {statistics.median(per_row)/1e3:8.1f} us  "
          f"p99 {q(per_row, .99)/1e3:8.1f} us  max {max(per_row)/1e3:8.1f} us  mean "
          f"{statistics.mean(per_row)/1e3:8.1f} us  (n={len(per_row)} = {len(rows)} rows x {REPS} passes)")
    print(f"simulate_row, whole run (all rows): median pass {statistics.median(passes)*1e3:8.1f} ms "
          f"(min {min(passes)*1e3:.1f}, max {max(passes)*1e3:.1f})")
    print("(simulate_row replays ONE arm, i.e. one config. A proposal run replays two arms (current "
          "and proposed); --scenarios replays the shared current arm once plus one arm per scenario.)")


def main():
    print("machine:", platform.platform(), "| python", platform.python_version(), "| nproc", os.cpu_count())
    print("cpu:", [l.split(":", 1)[1].strip() for l in open("/proc/cpuinfo") if l.startswith("model name")][0])
    print(f"1-minute load average at start: {os.getloadavg()[0]:.2f}")
    with tempfile.TemporaryDirectory() as d:
        empty = Path(d) / "empty-proposal.json"
        empty.write_text(json.dumps({"schema": "routeforge.policy-proposal/v1"}))
        scn = str(RUN / "scenarios.json")
        xs = []
        for _ in range(REPS):
            t = time.perf_counter()
            subprocess.run([sys.executable, "-c", "import app.eval.simulate, app.eval.scenarios"],
                           capture_output=True)
            xs.append(time.perf_counter() - t)
        print("\n== PART 1: scripts/policy_simulate.py end to end (fresh process each run) ==")
        print(f"{'process start + imports only (python -c import app.eval.simulate...)':58s} "
              f"median {statistics.median(xs):6.2f} s  min {min(xs):6.2f}  max {max(xs):6.2f}  (n={len(xs)} runs)")
        timed_cli("(a) plain replay (empty proposal)", ["--proposal", str(empty)])
        timed_cli("(b) --scenarios (5 scenarios)", ["--scenarios", scn])
        timed_cli("(c1) plain replay + --uncertainty (defaults)", ["--proposal", str(empty), "--uncertainty"])
        timed_cli("(c2) --scenarios + --uncertainty (defaults)", ["--scenarios", scn, "--uncertainty"])
    print("\n== PART 2: in-process pieces ==")
    asyncio.run(in_process())
    print(f"\n1-minute load average at end: {os.getloadavg()[0]:.2f}")


if __name__ == "__main__":
    main()
