"""In-process cost of PII detection, the outcome-record write, and the whole serving path.

Offline: no network, no real model. Three parts:
  1. detector analyze()/anonymize() timed on the exact synthetic prompt texts of live runs 3 and 4
     (regenerated from each run's traffic_generator.py with its seed; nothing is sent).
  2. one routing_outcomes write + commit (SQLite file; Postgres in a throwaway docker container).
  3. POST /v1/chat/completions through the FastAPI app in process with a zero-latency stub backend.
"""
import asyncio, json, os, platform, re, statistics, subprocess, sys, tempfile, time, uuid
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
os.chdir(ROOT)
sys.path.insert(0, str(ROOT))
os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "True")
os.environ.setdefault("SP_SEED_TEST_MODELS", "1")          # seeds local-8b / local-32b as onboarded rows
os.environ.setdefault("SP_SKIP_BACKEND_HEALTHCHECK", "1")
os.environ["DATABASE_URL"] = "sqlite+aiosqlite://"          # in-memory app DB for part 3
PG_IMAGE = "postgres:16.4-alpine"
RUNS = {3: 4, 4: 5}   # run -> generator seed (recovered by matching the prompt lengths in client_log.ndjson)
N = 900
DATA = ROOT / "docs/research/data"


def q(xs, p):
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(p * len(xs)))]


def fmt(xs, unit=1000.0):
    """median / p99 / n in microseconds (inputs in ns)."""
    return f"median {statistics.median(xs)/unit:10.1f} us  p99 {q(xs,0.99)/unit:10.1f} us  n={len(xs)}"


def ms(xs):
    return f"median {statistics.median(xs)/1e6:8.3f} ms  p99 {q(xs,0.99)/1e6:8.3f} ms  n={len(xs)}"


# ---------------------------------------------------------------- part 1: texts
def build_plan(run, seed):
    src = (DATA / f"live-dgx-lab-2026-10-09-run{run}/traffic_generator.py").read_text()
    head = src.split("plan = [make")[0]            # everything before the send loop
    head = head.replace('toks = json.load(open("toks.json")); rng = random.Random(SEED)',
                        "rng = random.Random(SEED)")
    head = re.sub(r"N, SEED, RPS, WORKERS = .*", f"N, SEED, RPS, WORKERS = {N},{seed},1.0,6", head)
    g = {}
    exec(head, g)
    return [g["make"](i) for i in range(N)]


def load_texts():
    texts = []
    for run, seed in RUNS.items():
        plan = build_plan(run, seed)
        log = {}
        for l in open(DATA / f"live-dgx-lab-2026-10-09-run{run}/client_log.ndjson"):
            r = json.loads(l); log[r["i"]] = r
        for p in plan:
            t = p["body"]["messages"][0]["content"]
            assert len(t) == log[p["i"]]["chars"] and p["pii"] == log[p["i"]]["pii"], (run, p["i"])
            texts.append({"run": run, "kind": p["kind"], "pii": p["pii"], "text": t})
    return texts


BUCKETS = [(0, 1000, "<1k chars"), (1000, 5000, "1-5k"), (5000, 20000, "5-20k"),
           (20000, 60000, "20-60k"), (60000, 10**9, ">=60k")]


def bucket(n):
    return next(b[2] for b in BUCKETS if b[0] <= n < b[1])


def time_detector(name, det, texts, reps):
    res = []
    for t in texts:
        xs = []
        for _ in range(reps):
            a = time.perf_counter_ns(); ents = det.analyze(t["text"]); xs.append(time.perf_counter_ns() - a)
        res.append((t, statistics.median(xs), len(ents)))   # per-text median over reps
    return res


def report_groups(title, res):
    print(f"\n[{title}] per-text analyze() time (per-text median of reps; then median/p99 across texts)")
    for label, keyf in (("kind", lambda t: t["kind"] + ("+pii" if t["pii"] else "")),
                        ("length", lambda t: bucket(len(t["text"])))):
        groups = defaultdict(list)
        for t, x, _ in res:
            groups[keyf(t)].append(x)
        order = ([b[2] for b in BUCKETS] if label == "length" else sorted(groups))
        for k in order:
            if k in groups:
                print(f"  {label:6s} {k:12s} {fmt(groups[k])}")
    print(f"  all              {fmt([x for _, x, _ in res])}")
    # throughput proxy
    tot_chars = sum(len(t['text']) for t, _, _ in res); tot_ns = sum(x for _, x, _ in res)
    print(f"  mean {tot_ns/len(res)/1000:.1f} us/text, {tot_chars/(tot_ns/1e9)/1e6:.2f} M chars/s over {len(res)} texts")


def part1():
    from app.policy.detectors import RegexDetector, PresidioDetector
    texts = load_texts()
    print("== PART 1: PII detection on live-run prompt texts ==")
    print(f"texts: {len(texts)} (runs 3 and 4, seeds {RUNS}); lengths chars: min {min(len(t['text']) for t in texts)} "
          f"median {int(statistics.median(len(t['text']) for t in texts))} max {max(len(t['text']) for t in texts)}; "
          f"with planted PII {sum(t['pii'] for t in texts)}")
    cnt = defaultdict(int)
    for t in texts:
        cnt[bucket(len(t['text']))] += 1
    print("  length buckets:", dict(cnt))
    kinds = defaultdict(int)
    for t in texts:
        kinds[t['kind'] + ('+pii' if t['pii'] else '')] += 1
    print("  kinds:", dict(kinds))

    rx = RegexDetector()
    rres = time_detector("regex", rx, texts, reps=5)
    report_groups("regex", rres)

    t0 = time.perf_counter_ns(); pd = PresidioDetector(); t1 = time.perf_counter_ns()
    print(f"\n[presidio] constructor (AnalyzerEngine + spaCy model load): {(t1-t0)/1e9:.2f} s")
    a = time.perf_counter_ns(); pd.analyze(texts[0]["text"]); b = time.perf_counter_ns()
    print(f"[presidio] cold first analyze() call (before warmup): {(b-a)/1e6:.1f} ms")
    a = time.perf_counter_ns(); pd.warmup(); b = time.perf_counter_ns()
    print(f"[presidio] warmup() call: {(b-a)/1e6:.1f} ms")
    pres = time_detector("presidio", pd, texts, reps=1)
    report_groups("presidio", pres)

    # entities found vs planted
    for nm, res in (("regex", rres), ("presidio", pres)):
        planted = [(t, n) for t, _, n in res if t["pii"]]
        print(f"  {nm}: entities>0 on {sum(1 for _, n in planted if n>0)}/{len(planted)} planted-PII texts; "
              f"on {sum(1 for t,_,n in res if not t['pii'] and n>0)}/{sum(1 for t,_,_ in res if not t['pii'])} no-PII texts")

    # anonymize on texts with entities
    print("\n[anonymize] on texts where the detector found entities")
    for nm, det in (("regex", rx), ("presidio", pd)):
        xs = []; counts = []
        for t in texts:
            ents = det.analyze(t["text"])
            if not ents:
                continue
            for _ in range(5):
                a = time.perf_counter_ns(); det.anonymize(t["text"], ents); xs.append(time.perf_counter_ns() - a)
            counts.append(len(ents))
        print(f"  {nm:8s} anonymize {fmt(xs)}  (texts={len(counts)}, entities/text median {statistics.median(counts)}, max {max(counts)})")

    # versions
    import importlib.metadata as md
    print("\n[versions] presidio-analyzer", md.version("presidio-analyzer"), "| spacy", md.version("spacy"))
    try:
        nlp = pd._analyzer.nlp_engine.nlp["en"]
        print("  spaCy model:", nlp.meta.get("name"), nlp.meta.get("version"), "lang", nlp.meta.get("lang"))
    except Exception as e:
        print("  spaCy model: unavailable", e)
    return texts


# ---------------------------------------------------------------- part 2: outcome write
def _sample_row():
    """A real outcome row produced by one request through the app (content-free)."""
    from fastapi.testclient import TestClient
    from sqlalchemy import select
    from app.apps.models import RoutingOutcome
    from app.gateway.backends import litellm_backend
    from app.main import create_app
    from tests._litellm_fake import fake_acompletion
    litellm_backend.acompletion = fake_acompletion
    with TestClient(create_app()) as c:
        r = c.post("/v1/chat/completions", headers={"Authorization": "Bearer demo-key"},
                   json={"messages": [{"role": "user", "content": "My SSN is 123-45-6789, summarize."}]})
        assert r.status_code == 200, r.text
        async def go():
            async with c.app.state.db.session() as s:
                return (await s.execute(select(RoutingOutcome))).scalars().first()
        row = c.portal.call(go)
        return {col.name: getattr(row, col.name) for col in RoutingOutcome.__table__.columns}


async def time_outcome_write(url, row, n=2000, warm=100):
    from app.apps import store
    from app.apps.bootstrap import seed_all
    from app.apps.db import Database
    db = Database(url, seed=seed_all)
    await db.ensure_ready()
    async def once(i):
        r = dict(row); r["request_id"] = "req-" + uuid.uuid4().hex
        a = time.perf_counter_ns()
        async with db.session() as s:
            await store.write_outcome(s, r)
            await s.commit()
        return time.perf_counter_ns() - a
    for i in range(warm):
        await once(i)
    xs = [await once(i) for i in range(n)]
    await db.engine.dispose()
    return xs


def part2():
    print("\n== PART 2: routing_outcomes write (session open + insert + commit, one row) ==")
    row = _sample_row()
    print("row columns:", len(row), "| JSON size of row ~", len(json.dumps(row, default=str)), "bytes")
    with tempfile.TemporaryDirectory() as d:
        url = f"sqlite+aiosqlite:///{d}/o.db"
        xs = asyncio.run(time_outcome_write(url, row))
        print("SQLite file DB (default journal):", ms(xs))
    # Postgres in docker, pinned image
    name = "rf-bench-pg-" + uuid.uuid4().hex[:6]
    try:
        port = None
        import socket
        s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()
        subprocess.run(["docker", "run", "-d", "--rm", "--name", name, "-e", "POSTGRES_PASSWORD=bench",
                        "-e", "POSTGRES_DB=bench", "-p", f"127.0.0.1:{port}:5432", PG_IMAGE],
                       check=True, capture_output=True)
        for _ in range(60):
            p = subprocess.run(["docker", "exec", name, "pg_isready", "-U", "postgres", "-d", "bench"],
                               capture_output=True)
            if p.returncode == 0:
                time.sleep(1); break
            time.sleep(1)
        ver = subprocess.run(["docker", "exec", name, "postgres", "--version"], capture_output=True, text=True).stdout.strip()
        url = f"postgresql+asyncpg://postgres:bench@127.0.0.1:{port}/bench"
        xs = asyncio.run(time_outcome_write(url, row))
        print(f"Postgres ({PG_IMAGE}, {ver}; same host, loopback docker port, default fsync):", ms(xs))
    except Exception as e:
        print("Postgres: NOT MEASURED:", repr(e))
    finally:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True)


# ---------------------------------------------------------------- part 3: end to end
def part3(texts):
    from fastapi.testclient import TestClient
    from app.gateway.backends import litellm_backend
    from app.main import create_app
    from tests import _litellm_fake as fk
    print("\n== PART 3: in-process POST /v1/chat/completions, zero-latency stub backend ==")
    async def stub(**kw):          # no sleep, no network: returns a canned 'ok' completion
        return fk.make_response()
    litellm_backend.acompletion = stub
    # prompts: a short fixed pair, and a real run-3 text (median-length prose and its PII twin)
    prose = [t for t in texts if t["run"] == 3 and t["kind"] == "prose" and not t["pii"]]
    prose.sort(key=lambda t: len(t["text"]))
    mid = prose[len(prose) // 2]["text"]
    ssn = "Patient contact: SSN 456-78-9012, email alpha.beta@example.com.\n"
    prompts = {
        "short no-PII (62 ch)": "What is a deductible? Answer in one sentence, please, thanks.",
        "short PII (SSN+email)": ssn + "What is a copay? One sentence.",
        f"run3 prose no-PII ({len(mid)} ch)": mid,
        f"run3 prose + PII ({len(mid)+len(ssn)} ch)": mid + "\n\n" + ssn,
    }
    H = {"Authorization": "Bearer demo-key"}
    for mode, n in (("regex", 400), ("presidio", 150)):
        os.environ["SP_PII_DETECTION"] = mode
        with TestClient(create_app()) as c:
            for _ in range(20):
                c.get("/health")
            hx = []
            for _ in range(300):
                a = time.perf_counter_ns(); c.get("/health"); hx.append(time.perf_counter_ns() - a)
            print(f"\n[detection={mode}] harness baseline GET /health (TestClient + ASGI, no pipeline): {ms(hx)}")
            for label, text in prompts.items():
                body = {"messages": [{"role": "user", "content": text}]}
                for _ in range(15):
                    r = c.post("/v1/chat/completions", headers=H, json=body); assert r.status_code == 200, r.text
                xs = []
                for _ in range(n):
                    a = time.perf_counter_ns(); r = c.post("/v1/chat/completions", headers=H, json=body)
                    xs.append(time.perf_counter_ns() - a)
                    assert r.status_code == 200
                j = r.json()
                print(f"  {label:36s} {ms(xs)}  model={j.get('model')}")
            # same requests with the outcome write switched off (serving.outcomes.enabled=false):
            # the difference is the in-situ cost of the routing_outcomes write on this app DB.
            st = c.app.state.settings
            off = st.serving.outcomes.model_copy(update={"enabled": False})
            c.app.state.settings = st.model_copy(update={"serving": st.serving.model_copy(update={"outcomes": off})})
            print(f"  -- outcomes.enabled=false (usage row still written) --")
            for label, text in prompts.items():
                body = {"messages": [{"role": "user", "content": text}]}
                for _ in range(15):
                    c.post("/v1/chat/completions", headers=H, json=body)
                xs = []
                for _ in range(n):
                    a = time.perf_counter_ns(); r = c.post("/v1/chat/completions", headers=H, json=body)
                    xs.append(time.perf_counter_ns() - a)
                    assert r.status_code == 200
                print(f"  {label:36s} {ms(xs)}")


def main():
    print("machine:", platform.platform(), "| python", platform.python_version(), "| nproc", os.cpu_count())
    try:
        print("cpu:", [l.split(":", 1)[1].strip() for l in open("/proc/cpuinfo") if l.startswith("model name")][0])
    except Exception:
        pass
    parts = os.environ.get("RF_PARTS", "1,2,3").split(",")   # RF_PARTS=2,3 skips the slow Presidio sweep
    texts = part1() if "1" in parts else load_texts()
    if "2" in parts:
        part2()
    if "3" in parts:
        part3(texts)


if __name__ == "__main__":
    main()
