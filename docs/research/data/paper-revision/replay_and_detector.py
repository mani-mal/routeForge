"""Recompute paper figures the review asked for, from the released run data."""
import asyncio, json, sys
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT))
from app.config.loader import load_settings
from app.eval.outcomes import load_export
from app.eval.scenario import load_snapshot
from app.eval.simulate import (build_scenario, close_scenario, observed_latencies,
                               select_rows, simulate_row, _first_pick, RECORDED_OUTCOME)

D = ROOT / "docs/research/data"
RUNS = {1: "live-dgx-lab-2026-10-08", 2: "live-dgx-lab-2026-10-08-run2",
        3: "live-dgx-lab-2026-10-09-run3", 4: "live-dgx-lab-2026-10-09-run4"}
CLOUD_PREFIX = ("openrouter",)


async def replay(run):
    p = D / RUNS[run]
    records, _ = load_export(p / "outcomes.ndjson")
    snap, _ = load_snapshot(p / "config-snapshot.json")
    base = load_settings(ROOT / "config")
    scn = await build_scenario(snap, base=base)
    try:
        obs = observed_latencies(records)
        rows, excluded = select_rows(records, set(scn.apps))
        arms = [simulate_row(scn, r, obs) for r in rows]
    finally:
        await close_scenario(scn)
    adm = sum(1 for r, a in zip(rows, arms) if RECORDED_OUTCOME[r["status"]] == a.outcome)
    ok = [(r, a) for r, a in zip(rows, arms) if r["status"] == "ok" and r.get("attempts")]
    first = sum(1 for r, a in ok if _first_pick(a) == r["attempts"][0])
    final = sum(1 for r, a in ok if _first_pick(a) == r["chosen"])
    multi = sum(1 for r, a in ok if len(r["attempts"]) > 1)
    print(f"run {run}: rows {len(rows)} excluded {dict(excluded)} admission {adm}/{len(rows)}"
          f" | ok {len(ok)} first-attempt agree {first} ({first/len(ok):.3f})"
          f" final-serving agree {final} ({final/len(ok):.3f}) multi-attempt rows {multi}")
    return records


def onprem(records, run):
    sel = [r for r in records if r.get("onprem_only")]
    st = Counter(r["status"] for r in sel)
    att = sum(len(r.get("attempts") or []) for r in sel)
    cloud = sum(1 for r in sel for e in (r.get("attempts") or []) if e.startswith(CLOUD_PREFIX))
    zero = sum(1 for r in sel if not r.get("attempts"))
    print(f"run {run} onprem_only rows {len(sel)} status {dict(st)} attempts {att}"
          f" cloud-attempts {cloud} rows-without-attempt {zero}")


def detector(records, run):
    log = {}
    for line in open(D / RUNS[run] / "client_log.ndjson"):
        j = json.loads(line)
        if "id" in j:
            log[j["id"]] = j
    by = defaultdict(Counter)
    for r in records:
        c = log.get(r["request_id"])
        if c is None:
            continue
        det = bool(r.get("detected_entity_types"))
        k = c.get("kind")
        by[k][("TP" if c["pii"] and det else "FN" if c["pii"] else "FP" if det else "TN")] += 1
        by["ALL"][("TP" if c["pii"] and det else "FN" if c["pii"] else "FP" if det else "TN")] += 1
    for k, v in sorted(by.items()):
        print(f"  run {run} detect {k}: {dict(v)}")
    # which entity types fire on negatives
    fp = Counter(t for r in records if (c := log.get(r["request_id"])) and not c["pii"]
                 for t in (r.get("detected_entity_types") or []))
    print(f"  run {run} entity types on negatives: {dict(fp)}")


def answers(run):
    by = defaultdict(Counter)
    for line in open(D / RUNS[run] / "client_log.ndjson"):
        j = json.loads(line)
        if j.get("status") != 200:
            by[j.get("model") or "-"]["non200"] += 1
            continue
        by[j.get("model")]["200"] += 1
        if not j.get("answer_chars"):
            by[j.get("model")]["200_empty"] += 1
    print(f"run {run} answers: {dict((k, dict(v)) for k, v in by.items())}")


async def main():
    for run in (1, 2, 3, 4):
        recs = await replay(run)
        if run in (3, 4):
            onprem(recs, run)
        if run in (3, 4):
            detector(recs, run)
            answers(run)

asyncio.run(main())
