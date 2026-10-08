"""E9a (range width against n) and E9b (split-half coverage) on the live export."""
import json, os, random, subprocess, sys, concurrent.futures as cf
L = os.path.dirname(os.path.abspath(__file__)); REPO = "/home/manimal/projects/routeForge"
rows = open(f"{L}/outcomes.ndjson").read().splitlines()
scen = json.load(open(f"{L}/scen.json")); scen["scenarios"] = [s for s in scen["scenarios"] if s["name"] in ("raise-request-budget", "tighten-claims-budget-0.02", "window-4000", "smaller-prompts-0.6")]
json.dump(scen, open(f"{L}/scen4.json", "w"))
FIG = [("raise-request-budget", "newly_served"), ("tighten-claims-budget-0.02", "newly_rejected"), ("window-4000", "newly_rejected"), ("smaller-prompts-0.6", "newly_served")]
def run(tag, subset):
    d = f"{L}/runs/{tag}"; os.makedirs(d, exist_ok=True)
    open(f"{d}/in.ndjson", "w").write("\n".join(subset) + "\n")
    p = subprocess.run(["uv", "run", "python", "scripts/policy_simulate.py", "--export", f"{d}/in.ndjson", "--snapshot", f"{L}/snapshot.json",
                        "--scenarios", f"{L}/scen4.json", "--uncertainty", "--out", d], cwd=REPO, capture_output=True, text=True,
                       env={**os.environ, "LITELLM_LOCAL_MODEL_COST_MAP": "True"})
    if p.returncode != 0: return {"tag": tag, "error": p.stderr[-300:]}
    c = json.load(open(f"{d}/comparison.json")); out = {"tag": tag, "n": len(subset), "figs": {}}
    unc = {u["name"]: u for u in c["uncertainty"]["scenarios"]}
    for name, key in FIG:
        t = unc[name]["totals"]; out["figs"][f"{name}:{key}"] = t[key]
        out["figs"][f"{name}:cost_delta"] = t["cost_usd"]["delta"]
    out["strata"] = unc[FIG[0][0]].get("totals_strata")
    return out
jobs = []
for n in (50, 100, 200, 400, 800):
    for s in range(8):
        r = random.Random(f"w{n}-{s}"); jobs.append((f"w{n}-{s}", r.sample(rows, n)))
for s in range(100):
    r = random.Random(f"h{s}"); sh = rows[:]; r.shuffle(sh); h = len(sh) // 2
    jobs.append((f"hA{s}", sh[:h])); jobs.append((f"hB{s}", sh[h:2*h]))
with cf.ThreadPoolExecutor(4) as ex, open(f"{L}/exp.ndjson", "w") as f:
    for res in ex.map(lambda j: run(*j), jobs): f.write(json.dumps(res) + "\n"); f.flush()
print("done", len(jobs))
