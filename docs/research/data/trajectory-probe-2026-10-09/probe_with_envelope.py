"""The three-turn conversation of trajectory-feasible-routing.md §2.1, under each trajectory_mode.
usage: traj2.py SCRATCH   (sets paper-dev's trajectory_mode through the admin API, restores it to off at the end)"""
import json, sys, time, urllib.request, urllib.error, random, uuid
sys.path.insert(0, sys.argv[1] + "/live"); import adm
toks = json.load(open(sys.argv[1] + "/live/toks.json")); rng = random.Random(7); admin = adm.client()
W = "claim policy member provider coverage deductible premium copay network referral appeal denial invoice ledger audit visit note schedule".split()
pad = " ".join(rng.choice(W) for _ in range(900)).capitalize() + "."
u1 = {"role": "user", "content": pad + "\n\nSummarize the notes above in one sentence."}
a1 = {"role": "assistant", "content": "The notes describe routine claim handling."}
u2 = {"role": "user", "content": "Tool result from the CRM lookup: member SSN 482-73-1946, email jane.doe@example.com. Is this member's claim still open? Answer briefly."}
u3 = {"role": "user", "content": "Draft a two-sentence status note for the file."}
summ = {"role": "assistant", "content": "Summary of the conversation so far: routine claim handling; the member's claim is still open."}
def call(messages, tid):
    h = {"Authorization": "Bearer " + toks["paper-dev"], "content-type": "application/json", "x-routeforge-workload": "trajectory_probe"}
    if tid: h["x-routeforge-trajectory"] = tid
    rq = urllib.request.Request("http://10.89.195.201:30808/v1/chat/completions", data=json.dumps({"max_tokens": 900, "messages": messages}).encode(), headers=h)
    try:
        d = json.loads(urllib.request.urlopen(rq, timeout=180).read()); p = d.get("policy", {})
        return {"status": 200, "model": d.get("model"), "onprem_only": p.get("onprem_only"), "inherited": p.get("inherited_entity_types"), "mode": p.get("trajectory_mode"), "id": d.get("id")}
    except urllib.error.HTTPError as e:
        return {"status": e.code, "err": e.read().decode()[:200]}
def setmode(m):
    s, r = admin("PUT", "/admin/applications/paper-dev/policy", {"trajectory_mode": m}); assert s == 200, (s, r)
out = []
try:
    for arm, mode, last in (("off", "off", "same"), ("header", "header", "same"), ("header, new id on last turn", "header", "new"), ("header, no id on last turn", "header", "none"), ("sticky, new id on last turn", "sticky", "new"), ("sticky, no id on last turn", "sticky", "none")):
        setmode(mode); time.sleep(1)
        for rep in range(3):
            tid = "probe-" + uuid.uuid4().hex[:12]
            last_id = {"same": tid, "new": "probe-" + uuid.uuid4().hex[:12], "none": None}[last]
            r1 = call([u1], tid); r2 = call([u1, a1, u2], tid); r3 = call([u1, summ, u3], last_id)
            out.append({"arm": arm, "rep": rep, "T1": r1, "T2": r2, "T3b": r3})
            print(arm, "| rep", rep, "| T1", r1.get("model"), "| T2", r2.get("model"), "| T3b", r3.get("model"), "inherited", r3.get("inherited"), flush=True)
        if mode == "sticky":       # a sticky envelope would restrict the next arm; none follows, but say so
            pass
finally:
    setmode("off")
json.dump(out, open(sys.argv[1] + "/traj2_results.json", "w"), indent=1)
