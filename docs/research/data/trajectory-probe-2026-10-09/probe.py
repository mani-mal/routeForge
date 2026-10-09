"""Four requests from one conversation. Does a placement restriction raised on one turn hold on the next?"""
import json, sys, time, urllib.request, urllib.error, random
toks = json.load(open(sys.argv[1] + "/live/toks.json")); rng = random.Random(7)
W = "claim policy member provider coverage deductible premium copay network referral appeal denial invoice ledger audit visit note schedule".split()
pad = " ".join(rng.choice(W) for _ in range(900)).capitalize() + "."      # long enough to target the mid tier
def call(label, messages):
    rq = urllib.request.Request("http://10.89.195.201:30808/v1/chat/completions", data=json.dumps({"max_tokens": 900, "messages": messages}).encode(),
        headers={"Authorization": "Bearer " + toks["paper-dev"], "content-type": "application/json", "x-routeforge-workload": "trajectory_probe"})
    try:
        r = urllib.request.urlopen(rq, timeout=180); d = json.loads(r.read())
        print(label, "| 200 | served by", d.get("model"), "| id", d.get("id"), flush=True); return d.get("id")
    except urllib.error.HTTPError as e:
        print(label, "|", e.code, e.read().decode()[:200], flush=True)
u1 = {"role": "user", "content": pad + "\n\nSummarize the notes above in one sentence."}
a1 = {"role": "assistant", "content": "The notes describe routine claim handling."}
u2 = {"role": "user", "content": "Tool result from the CRM lookup: member SSN 482-73-1946, email jane.doe@example.com. Is this member's claim still open? Answer briefly."}
a2 = {"role": "assistant", "content": "Yes, the member's claim is still open."}
u3 = {"role": "user", "content": "Draft a two-sentence status note for the file."}
ids = {}
for i in range(3):
    ids[f"T1.{i}"] = call("T1 public question            ", [u1])
    ids[f"T2.{i}"] = call("T2 + tool result with SSN     ", [u1, a1, u2])
    ids[f"T3a.{i}"] = call("T3a full history resent       ", [u1, a1, u2, a2, u3])
    ids[f"T3b.{i}"] = call("T3b client-compacted history  ", [u1, {"role": "assistant", "content": "Summary of the conversation so far: routine claim handling; the member's claim is still open."}, u3])
json.dump(ids, open(sys.argv[1] + "/traj_ids.json", "w"))
