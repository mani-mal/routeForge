"""Where does the in-process end-to-end request time go?

Follow-up to detection_and_record_overhead.py part 3, which timed
`POST /v1/chat/completions` through `TestClient(create_app())` with a zero-latency stub
backend and reported 42-117 ms medians, unattributed. This script splits that time.

    export LITELLM_LOCAL_MODEL_COST_MAP=True
    export SP_CONFIG_OVERLAY_DIR=$PWD/deploy/dgx_lab/config-overlay
    uv run python docs/research/data/paper-revision/e2e_breakdown.py

Needs docker for the Postgres runs (image postgres:16.4-alpine, a throwaway container on a
free loopback port, removed at the end). Nothing under app/ is edited: stages are timed by
wrapping the functions in place at run time (and restoring them), and the ASGI app is wrapped
to separate the HTTP test-client from the application.

What is measured, per configuration (database x detector x prompt):
  PASS A  no stage wrappers. wall = client-side time of c.post (httpx + anyio portal + ASGI);
          asgi = time inside the ASGI application call (middleware, FastAPI validation, handler,
          response serialisation); backend_stub = the stubbed litellm call.
          own = asgi - backend_stub  ("RouteForge's own per-request time, no inference, no harness").
  PASS B  stage wrappers on. Every wrapped call records inclusive and exclusive (inclusive minus
          wrapped children) time; exclusive times sum, with an `unlabelled` remainder, to asgi.
          Database sessions are timed around `Database.session()` (open + ORM + SQL + commit +
          close) and tagged by the store function called inside.
Medians are per-request medians; the reconciliation table uses means because medians do not add.
"""
import asyncio
import contextvars
import functools
import os
import platform
import socket
import statistics
import subprocess
import sys
import time
import uuid
from collections import defaultdict
from contextlib import asynccontextmanager
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[3]
os.chdir(ROOT)
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(HERE))
os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "True")
os.environ.setdefault("SP_SEED_TEST_MODELS", "1")          # seeds local-8b / local-32b as onboarded rows
os.environ.setdefault("SP_SKIP_BACKEND_HEALTHCHECK", "1")
PG_IMAGE = "postgres:16.4-alpine"
H = {"Authorization": "Bearer demo-key"}


def pct(xs, p):
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(p * len(xs)))]


def med(xs):
    return statistics.median(xs) if xs else float("nan")


# ------------------------------------------------------------------ tracer
REC: contextvars.ContextVar = contextvars.ContextVar("rec", default=None)
STACK: list = []
TAG: list = [None]


def _enter():
    STACK.append([time.perf_counter(), 0.0])


def _exit(label, rec):
    t0, child = STACK.pop()
    inc = time.perf_counter() - t0
    if STACK:
        STACK[-1][1] += inc
    rec.append((label, inc, inc - child))


def span_wrapper(label, f):
    if asyncio.iscoroutinefunction(f):
        @functools.wraps(f)
        async def aw(*a, **k):
            rec = REC.get()
            if rec is None:
                return await f(*a, **k)
            _enter()
            try:
                return await f(*a, **k)
            finally:
                _exit(label, rec)
        return aw

    @functools.wraps(f)
    def sw(*a, **k):
        rec = REC.get()
        if rec is None:
            return f(*a, **k)
        _enter()
        try:
            return f(*a, **k)
        finally:
            _exit(label, rec)
    return sw


def tag_wrapper(tag, f):
    """Sets the database-session tag AND times the store function as its own span."""
    label = "store." + f.__name__

    @functools.wraps(f)
    async def tw(*a, **k):
        TAG[0] = tag
        rec = REC.get()
        if rec is None:
            return await f(*a, **k)
        _enter()
        try:
            return await f(*a, **k)
        finally:
            _exit(label, rec)
    return tw


PATCHES: list = []   # (obj, name, original)


def patch_attr(obj, name, new):
    PATCHES.append((obj, name, getattr(obj, name)))
    setattr(obj, name, new)


def patch_everywhere(orig, make):
    """Replace `orig` with make(orig) in every app/tests module namespace that holds it
    (covers `from x import f` bindings as well as `x.f` attribute lookups)."""
    new = make(orig)
    for m in list(sys.modules.values()):
        n = getattr(m, "__name__", "")
        if not (n.startswith("app") or n.startswith("tests")):
            continue
        for k, v in list(vars(m).items()):
            if v is orig:
                patch_attr(m, k, new)


def install_stage_wrappers():
    import app.api.bus as bus_mod
    import app.api.history as history_mod
    import app.api.routes as routes
    import app.apps.db as dbmod
    import app.apps.outcomes as outcomes_mod
    import app.apps.store as store
    import app.obs.export as export_mod
    import app.output.verify as verify_mod
    import app.pipeline as pipeline_mod
    import app.policy.compile as compile_mod
    import app.policy.context as context_mod
    import app.policy.detectors as det_mod
    import app.policy.feasible as feasible_mod
    import app.policy.gate as gate_mod
    import app.routing.router as router_mod
    import app.gateway.gateway as gw_mod
    from sqlalchemy.ext.asyncio import AsyncSession

    def span(label):
        return lambda f: span_wrapper(label, f)
    for label, fn in (
            ("auth (_authenticate)", routes._authenticate),
            ("compile_policy", compile_mod.compile_policy),
            ("build_context", context_mod.build_context),
            ("feasible_set", feasible_mod.feasible_set),
            ("verify (step 9)", verify_mod.verify),
            ("redact_tool_calls", gate_mod.redact_tool_calls_with_entities),
            ("export_trace (telemetry)", export_mod.export_trace),
            ("build_ok_outcome", outcomes_mod.build_ok_outcome),
            ("summarize (snapshot)", history_mod.summarize),
            ("_record_outcome", routes._record_outcome),
            ("_finalize", routes._finalize)):
        patch_everywhere(fn, span(label))
    for cls, name, label in (
            (pipeline_mod.Pipeline, "run", "pipeline.run"),
            (pipeline_mod.Pipeline, "_gate_inputs", "pipeline._gate_inputs"),
            (gate_mod.Gate, "evaluate", "gate.evaluate"),
            (router_mod.Router, "select", "router.select (step 6)"),
            (gw_mod.Gateway, "run", "gateway.run (step 7)"),
            (bus_mod.DecisionBus, "publish", "bus.publish"),
            (AsyncSession, "commit", "db commit")):
        patch_attr(cls, name, span_wrapper(label, getattr(cls, name)))
    for cls in (det_mod.RegexDetector, det_mod.PresidioDetector, det_mod.NullDetector):
        for meth, label in (("analyze", "detect.analyze"), ("anonymize", "detect.anonymize")):
            patch_attr(cls, meth, span_wrapper(label, getattr(cls, meth)))
    import app.apps.tokens as tokens_mod
    patch_everywhere(tokens_mod.hash_token, lambda f: span_wrapper("hash_token", f))
    from sqlalchemy import event
    from sqlalchemy.engine import Engine

    def _count(*a, **k):
        rec = REC.get()
        if rec is not None:
            rec.append(("#sql statements", 0.0, 0.0))

    def _commit(*a, **k):
        rec = REC.get()
        if rec is not None:
            rec.append(("#sql commits", 0.0, 0.0))
    event.listen(Engine, "before_cursor_execute", _count)
    event.listen(Engine, "commit", _commit)
    PATCHES.append((None, "events", (Engine, _count, _commit)))
    for fn, tag in ((store.resolve_application, "db:auth lookup"),
                    (store.get_privacy_profile, "db:profile load"),
                    (store.record_snapshot, "db:snapshot write"), (store.prune_snapshots, "db:snapshot write"),
                    (store.record_usage, "db:usage write"), (store.write_outcome, "db:outcome write")):
        patch_everywhere(fn, lambda f, tag=tag: tag_wrapper(tag, f))

    orig_session = dbmod.Database.session

    @asynccontextmanager
    async def session(self):
        rec = REC.get()
        if rec is None:
            async with orig_session(self) as s:
                yield s
            return
        TAG[0] = None
        _enter()
        try:
            async with orig_session(self) as s:
                yield s
        finally:
            _exit(TAG[0] or "db:other", rec)
    patch_attr(dbmod.Database, "session", session)


def remove_stage_wrappers():
    while PATCHES:
        obj, name, orig = PATCHES.pop()
        if obj is None:
            from sqlalchemy import event
            eng, c1, c2 = orig
            event.remove(eng, "before_cursor_execute", c1)
            event.remove(eng, "commit", c2)
            continue
        setattr(obj, name, orig)


# ------------------------------------------------------------------ ASGI timing wrapper
class Timed:
    """Wraps the ASGI app. Per http request: asgi_ns, plus (pass B) the span list."""

    def __init__(self, app):
        self.app = app
        self.log = []          # (asgi_s, spans|None)
        self.collect_spans = False

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        rec = [] if self.collect_spans else None
        tok = REC.set(rec) if self.collect_spans else None
        STACK.clear()
        t0 = time.perf_counter()
        try:
            await self.app(scope, receive, send)
        finally:
            self.log.append((time.perf_counter() - t0, rec))
            if tok is not None:
                REC.reset(tok)


# ------------------------------------------------------------------ stub backend
STUB_T = []


async def _stub(**kw):
    from tests import _litellm_fake as fk
    t0 = time.perf_counter()
    r = fk.make_response()
    dt = time.perf_counter() - t0
    STUB_T.append(dt)
    rec = REC.get()
    if rec is not None:        # a leaf span so it is subtracted from gateway.run
        rec.append(("backend_stub (litellm call, stubbed)", dt, dt))
        if STACK:
            STACK[-1][1] += dt
    return r


# ------------------------------------------------------------------ one configuration
def build_prompts():
    from detection_and_record_overhead import load_texts
    texts = load_texts()
    prose = sorted((t for t in texts if t["run"] == 3 and t["kind"] == "prose" and not t["pii"]),
                   key=lambda t: len(t["text"]))
    mid = prose[len(prose) // 2]["text"]
    return {"short (62 ch)": "What is a deductible? Answer in one sentence, please, thanks.",
            f"long ({len(mid)} ch)": mid}


def run_config(db_label, db_url, detect, prompts, n, warm=20):
    from fastapi.testclient import TestClient
    from app.gateway.backends import litellm_backend
    from app.main import create_app
    os.environ["DATABASE_URL"] = db_url
    os.environ["SP_PII_DETECTION"] = detect
    litellm_backend.acompletion = _stub
    out = {}
    print(f"\n#### database={db_label}  detection={detect}  (n={n} per prompt)")
    app = create_app()
    timed = Timed(app)
    with TestClient(timed) as c:
        hw = []
        for _ in range(warm):
            c.get("/health")
        for _ in range(200):
            t = time.perf_counter(); c.get("/health"); hw.append(time.perf_counter() - t)
        ha = [a for a, _ in timed.log[-200:]]
        print(f"GET /health (no pipeline): wall median {med(hw)*1e3:.3f} ms, asgi median {med(ha)*1e3:.3f} ms "
              f"=> test-client overhead (wall - asgi) {(med(hw)-med(ha))*1e3:.3f} ms")
        for label, text in prompts.items():
            body = {"messages": [{"role": "user", "content": text}]}
            for _ in range(warm):
                r = c.post("/v1/chat/completions", headers=H, json=body)
                assert r.status_code == 200, r.text
            # ---- pass A
            timed.log.clear(); STUB_T.clear()
            wall = []
            for _ in range(n):
                t = time.perf_counter(); r = c.post("/v1/chat/completions", headers=H, json=body)
                wall.append(time.perf_counter() - t)
                assert r.status_code == 200
            asgi = [a for a, _ in timed.log]
            stub = list(STUB_T)
            own = [a - s for a, s in zip(asgi, stub)]
            harness = [w - a for w, a in zip(wall, asgi)]
            # ---- pass B
            install_stage_wrappers()
            timed.collect_spans = True
            timed.log.clear()
            wallB = []
            try:
                for _ in range(n):
                    t = time.perf_counter(); r = c.post("/v1/chat/completions", headers=H, json=body)
                    wallB.append(time.perf_counter() - t)
                    assert r.status_code == 200
            finally:
                timed.collect_spans = False
                remove_stage_wrappers()
            logB = list(timed.log)
            print(f"\n-- prompt {label}")
            print(f"   PASS A (no wrappers): wall median {med(wall)*1e3:7.2f} ms p99 {pct(wall,.99)*1e3:7.2f} | "
                  f"asgi median {med(asgi)*1e3:7.2f} | harness (wall-asgi) median {med(harness)*1e3:6.2f} | "
                  f"backend_stub median {med(stub)*1e6:6.1f} us | OWN (asgi-stub) median {med(own)*1e3:7.2f} "
                  f"p99 {pct(own,.99)*1e3:7.2f} ms")
            print(f"   PASS B (stage wrappers): wall median {med(wallB)*1e3:7.2f} ms (instrumentation overhead "
                  f"{(med(wallB)-med(wall))*1e3:+.2f} ms vs pass A)")
            incl, excl = defaultdict(list), defaultdict(list)
            tot_a, stmts, commits = [], [], []
            for (a, spans) in logB:
                ci, ce, cnt = defaultdict(float), defaultdict(float), defaultdict(int)
                for lab, i, e in spans:
                    if lab.startswith("#"):
                        cnt[lab] += 1
                        continue
                    ci[lab] += i; ce[lab] += e
                tot_a.append(a)
                stmts.append(cnt["#sql statements"]); commits.append(cnt["#sql commits"])
                for lab in ci:
                    incl[lab].append(ci[lab]); excl[lab].append(ce[lab])
                explained = sum(ce.values())
                excl["unlabelled (ASGI/FastAPI/pydantic/json/loop)"].append(a - explained)
            nB = len(logB)
            print(f"   SQL per request: {med(stmts):.0f} statements (median), {med(commits):.0f} commits")
            # pad missing entries (label absent in a request) with zeros for mean
            def mean0(xs):
                return sum(xs) / nB
            rows = sorted(excl, key=lambda k: -mean0(excl[k]))
            asgi_mean = sum(tot_a) / nB
            print(f"   exclusive time per request, instrumented (asgi mean {asgi_mean*1e3:.2f} ms, median {med(tot_a)*1e3:.2f}):")
            print(f"     {'component':48s} {'mean ms':>8s} {'median ms':>9s} {'% of asgi':>9s}")
            for k in rows:
                print(f"     {k:48s} {mean0(excl[k])*1e3:8.3f} {med(excl[k] + [0]*(nB-len(excl[k])))*1e3:9.3f} "
                      f"{100*mean0(excl[k])/asgi_mean:8.1f}%")
            dbk = [k for k in incl if k.startswith("db:")]
            print(f"   database sessions, inclusive (open+ORM+SQL+commit+close), mean ms per request: " +
                  ", ".join(f"{k} {mean0(incl[k])*1e3:.2f}" for k in sorted(dbk)) +
                  f"; all sessions {sum(mean0(incl[k]) for k in dbk)*1e3:.2f} ms; of which commit "
                  f"{mean0(incl['db commit'])*1e3 if 'db commit' in incl else 0:.2f} ms")
            for k in ("pipeline.run", "_finalize", "gate.evaluate", "router.select (step 6)",
                      "gateway.run (step 7)", "verify (step 9)", "export_trace (telemetry)"):
                if k in incl:
                    print(f"   inclusive {k:32s} mean {mean0(incl[k])*1e3:8.3f} ms median "
                          f"{med(incl[k])*1e3:8.3f} ms")
            non_db = asgi_mean - sum(mean0(incl[k]) for k in dbk) - mean0(incl.get(
                "backend_stub (litellm call, stubbed)", [0] * nB))
            print(f"   RouteForge compute excluding stub and ALL database sessions (mean): {non_db*1e3:.2f} ms")
            out[label] = dict(non_db=non_db, stmts=med(stmts), wall=med(wall), asgi=med(asgi), own=med(own), own_p99=pct(own, .99),
                              harness=med(harness), asgi_mean=asgi_mean,
                              db_mean=sum(mean0(incl[k]) for k in dbk))
    return out


# ------------------------------------------------------------------ event-loop evidence
def loop_evidence(db_label, db_url, n=60):
    """Count event-loop iterations per request: shows the aiosqlite wake-up poll."""
    import asyncio.base_events as be
    from fastapi.testclient import TestClient
    from app.gateway.backends import litellm_backend
    from app.main import create_app
    os.environ["DATABASE_URL"] = db_url
    os.environ["SP_PII_DETECTION"] = "regex"
    litellm_backend.acompletion = _stub
    cnt = [0]
    orig = be.BaseEventLoop._run_once

    def counted(self):
        cnt[0] += 1
        return orig(self)
    be.BaseEventLoop._run_once = counted
    try:
        with TestClient(create_app()) as c:
            body = {"messages": [{"role": "user", "content": "What is a deductible?"}]}
            for _ in range(20):
                c.post("/v1/chat/completions", headers=H, json=body)
            a = cnt[0]
            for _ in range(n):
                c.post("/v1/chat/completions", headers=H, json=body)
            per = (cnt[0] - a) / n
            a = cnt[0]
            for _ in range(n):
                c.get("/health")
            ph = (cnt[0] - a) / n
    finally:
        be.BaseEventLoop._run_once = orig
    print(f"event-loop iterations per request, {db_label}: POST /v1/chat/completions {per:.0f}, GET /health {ph:.0f}")


def switch_interval_probe(prompts, n=200):
    """Diagnostic: does the GIL switch interval govern the SQLite figure?"""
    from fastapi.testclient import TestClient
    from app.gateway.backends import litellm_backend
    from app.main import create_app
    os.environ["DATABASE_URL"] = "sqlite+aiosqlite://"
    os.environ["SP_PII_DETECTION"] = "regex"
    litellm_backend.acompletion = _stub
    body = {"messages": [{"role": "user", "content": list(prompts.values())[0]}]}
    print("diagnostic: sqlite in-memory, short prompt, regex, asgi-call median vs sys.setswitchinterval")
    for si in (0.005, 0.0005, 0.00005):
        sys.setswitchinterval(si)
        timed = Timed(create_app())
        with TestClient(timed) as c:
            for _ in range(20):
                c.post("/v1/chat/completions", headers=H, json=body)
            timed.log.clear()
            for _ in range(n):
                c.post("/v1/chat/completions", headers=H, json=body)
        print(f"  switchinterval {si*1e3:7.3f} ms: asgi median {med([a for a,_ in timed.log])*1e3:7.2f} ms")
    sys.setswitchinterval(0.005)


# ------------------------------------------------------------------ postgres
def db_rtt(url, n=1000):
    """Median round trip of `SELECT 1` on a warm pooled connection, and of an INSERT+COMMIT into a
    scratch table (the cost the app pays per commit), in microseconds."""
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    async def go():
        eng = create_async_engine(url)
        async with eng.connect() as c:
            await c.execute(text("select 1"))
            xs = []
            for _ in range(n):
                t = time.perf_counter(); await c.execute(text("select 1")); xs.append(time.perf_counter() - t)
            await c.execute(text("create temp table rtt(x int)"))
            await c.commit()
            ys = []
            for _ in range(n // 5):
                t = time.perf_counter(); await c.execute(text("insert into rtt values (1)")); await c.commit()
                ys.append(time.perf_counter() - t)
        await eng.dispose()
        return xs, ys
    xs, ys = asyncio.run(go())
    return med(xs) * 1e6, pct(xs, .99) * 1e6, med(ys) * 1e6


def start_pg(extra=()):
    name = "rf-e2e-pg-" + uuid.uuid4().hex[:6]
    s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()
    subprocess.run(["docker", "run", "-d", "--rm", "--name", name, "-e", "POSTGRES_PASSWORD=bench",
                    "-e", "POSTGRES_DB=bench", "-p", f"127.0.0.1:{port}:5432", PG_IMAGE, *extra],
                   check=True, capture_output=True)
    for _ in range(60):
        if subprocess.run(["docker", "exec", name, "pg_isready", "-U", "postgres", "-d", "bench"],
                          capture_output=True).returncode == 0:
            time.sleep(1)
            break
        time.sleep(1)
    ver = subprocess.run(["docker", "exec", name, "postgres", "--version"], capture_output=True,
                         text=True).stdout.strip()
    return name, f"postgresql+asyncpg://postgres:bench@127.0.0.1:{port}/bench", ver


def main():
    print("machine:", platform.platform(), "| python", platform.python_version(), "| nproc", os.cpu_count())
    print("cpu:", [l.split(":", 1)[1].strip() for l in open("/proc/cpuinfo") if l.startswith("model name")][0])
    print(f"1-minute load average at start: {os.getloadavg()[0]:.2f}")
    prompts = build_prompts()
    results = {}
    cfgs = [("sqlite in-memory (app default for this harness)", "sqlite+aiosqlite://")]
    names = []
    try:
        n1, pg_url, ver = start_pg(); names.append(n1)
        cfgs.append((f"Postgres default fsync ({PG_IMAGE}, {ver}; loopback docker port)", pg_url))
        n2, pg2_url, _ = start_pg(("-c", "fsync=off", "-c", "synchronous_commit=off")); names.append(n2)
        cfgs.append(("Postgres fsync=off,synchronous_commit=off (same image; diagnostic)", pg2_url))
        for lab, u in cfgs[1:]:
            r1, r99, rc = db_rtt(u)
            print(f"{lab}: SELECT 1 round trip median {r1:.0f} us p99 {r99:.0f} us; INSERT+COMMIT median {rc:.0f} us")
    except Exception as e:
        print("Postgres: NOT RUN:", repr(e))
    try:
        print("\n=== event-loop evidence ===")
        loop_evidence("sqlite in-memory", "sqlite+aiosqlite://")
        if len(cfgs) > 1:
            loop_evidence("Postgres (default fsync)", cfgs[1][1])
        print("\n=== diagnostic: GIL switch interval ===")
        switch_interval_probe(prompts)
        for db_label, url in cfgs:
            for detect, n in (("regex", 300), ("presidio", 120)):
                results[(db_label, detect)] = run_config(db_label, url, detect, prompts, n)
    finally:
        for name in names:
            subprocess.run(["docker", "rm", "-f", name], capture_output=True)
            print(f"\n(removed container {name})")
    print("\n=== SUMMARY: median per-request time, ms ===")
    print(f"{'database':60s} {'detect':9s} {'prompt':16s} {'wall':>7s} {'asgi':>7s} {'harness':>8s} {'OWN':>7s} "
          f"{'own p99':>8s} {'db(mean)':>8s} {'non-db':>7s} {'sql':>4s}")
    for (dbl, det), res in results.items():
        for pl, r in res.items():
            print(f"{dbl[:60]:60s} {det:9s} {pl:16s} {r['wall']*1e3:7.2f} {r['asgi']*1e3:7.2f} "
                  f"{r['harness']*1e3:8.2f} {r['own']*1e3:7.2f} {r['own_p99']*1e3:8.2f} {r['db_mean']*1e3:8.2f} {r['non_db']*1e3:7.2f} {r['stmts']:4.0f}")


if __name__ == "__main__":
    main()
