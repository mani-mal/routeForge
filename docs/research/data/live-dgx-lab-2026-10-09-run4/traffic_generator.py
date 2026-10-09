"""Independent, varied synthetic traffic for the paper's live measurements.
Every request is drawn independently: app, content kind, size, max_tokens, PII or not.
Logs one JSON line per request with client wall-clock start/end. No prompt text is logged."""
import json, random, sys, time, threading, urllib.request, queue
B = "http://10.89.195.201:30808"
N, SEED, RPS, WORKERS = int(sys.argv[1]), int(sys.argv[2]), float(sys.argv[3]), 6
toks = json.load(open("toks.json")); rng = random.Random(SEED)
WORDS = ("claim policy member provider coverage deductible premium copay network referral appeal denial "
         "invoice ledger audit patient visit note lab result dosage schedule deploy rollback latency queue "
         "shard replica index cache tenant quota retry timeout budget forecast variance contract clause").split()
def prose(n):   return " ".join(rng.choice(WORDS) for _ in range(n)).capitalize() + "."
def code(n):
    out = []
    while sum(len(x) for x in out) < n * 5:
        f = rng.choice(WORDS); a = rng.choice(WORDS)
        out.append(f"def {f}_{a}(x, y={rng.randint(0,99)}):\n    if x.{a} > y:\n        return [{rng.randint(0,9)} * v for v in x.{f}]\n    return None\n")
    return "\n".join(out)
def logs(n):
    out = []
    while sum(len(x) for x in out) < n * 5:
        out.append(f"2026-10-08T{rng.randint(0,23):02d}:{rng.randint(0,59):02d}:{rng.randint(0,59):02d}Z {rng.choice(['INFO','WARN','ERROR'])} svc={rng.choice(WORDS)} req={rng.getrandbits(48):012x} dur_ms={rng.randint(1,9000)} status={rng.choice([200,200,200,404,500,503])}")
    return "\n".join(out)
def jsn(n):
    rows = []
    while len(json.dumps(rows)) < n * 5:
        rows.append({"id": rng.getrandbits(32), rng.choice(WORDS): rng.random(), "tags": [rng.choice(WORDS) for _ in range(3)], "ok": rng.random() < 0.8})
    return json.dumps(rows)
KIND = {"prose": prose, "code": code, "logs": logs, "json": jsn}
MIX = {"paper-claims": (["prose"]*6 + ["json"]*2 + ["logs"], "claims_qa"),
       "paper-dev":    (["code"]*5 + ["logs"]*3 + ["json"] + ["prose"], "dev_assist"),
       "paper-clinic": (["prose"]*7 + ["json"]*2 + ["logs"], "clinic_notes")}
ASK = {"prose": "Summarize the text above in one sentence.", "code": "Name one bug risk in the code above, in one sentence.",
       "logs": "How many ERROR lines are in the log above? Answer briefly.", "json": "Which field name appears most often above? Answer briefly."}
def size():           # words of context: heavy-tailed, 0 .. ~9000
    u = rng.random()
    return 0 if u < 0.15 else int(min(9000, rng.lognormvariate(6.2, 1.1)))
def make(i):
    app = rng.choice(list(MIX)); kinds, workload = MIX[app]; kind = rng.choice(kinds); n = size()
    pii = rng.random() < (0.5 if app == "paper-clinic" else 0.15)
    text = (KIND[kind](n) + "\n\n" if n else "")
    if pii: text += f"Patient contact: SSN {rng.randint(100,899)}-{rng.randint(10,99)}-{rng.randint(1000,9999)}, email {rng.choice(WORDS)}.{rng.choice(WORDS)}@example.com.\n"
    text += ASK[kind] if n else rng.choice(["What is a deductible? One sentence.", "What does HTTP 503 mean? One sentence.", "Name one cause of high p95 latency.", "What is a copay? One sentence."])
    mt = rng.choice([768, 1024, 1536])      # run 3: the cheap endpoint thinks before answering
    return {"i": i, "app": app, "kind": kind if n else "question", "words": n, "pii": pii, "max_tokens": mt, "workload": workload,
            "body": {"model": "auto", "max_tokens": mt, "messages": [{"role": "user", "content": text}]}}
plan = [make(i) for i in range(N)]
lock = threading.Lock(); out = open(f"client-{SEED}.ndjson", "a"); q = queue.Queue()
def worker():
    while True:
        item = q.get()
        if item is None: return
        rec = {k: item[k] for k in ("i", "app", "kind", "words", "pii", "max_tokens", "workload")}
        rec["chars"] = len(item["body"]["messages"][0]["content"])
        req = urllib.request.Request(B+"/v1/chat/completions", data=json.dumps(item["body"]).encode(),
              headers={"Authorization": "Bearer "+toks[item["app"]], "content-type": "application/json", "x-routeforge-workload": item["workload"]})
        rec["t_start"] = time.time()
        try:
            r = urllib.request.urlopen(req, timeout=600); d = json.load(r)
            rec.update(status=r.status, id=d.get("id"), model=d.get("model"), usage=d.get("usage"), cost=(d.get("cost") or {}).get("total_usd"), answer_chars=len(((d.get("choices") or [{}])[0].get("message") or {}).get("content") or ""))
        except urllib.error.HTTPError as e:
            raw = e.read().decode()[:4000]
            try: err = json.loads(raw).get("error", {})
            except Exception: err = {}
            rec.update(status=e.code, err_type=err.get("type"), err_code=err.get("code"), err_msg=(err.get("message") or raw)[:200])
        except Exception as e:
            rec.update(status=-1, err_msg=str(e)[:200])
        rec["t_end"] = time.time()
        with lock: out.write(json.dumps(rec) + "\n"); out.flush()
ths = [threading.Thread(target=worker, daemon=True) for _ in range(WORKERS)]
[t.start() for t in ths]
for item in plan:
    q.put(item); time.sleep(rng.expovariate(RPS))      # Poisson arrivals
[q.put(None) for _ in ths]; [t.join() for t in ths]
print("done", N)
