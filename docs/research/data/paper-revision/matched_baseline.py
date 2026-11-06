"""Matched-baseline experiment: the shipped eligibility filter vs (1) a plain
~40-line Python filter and (2) an OPA/Rego policy, both written from the documented
semantics. Each baseline's eligible set is fed to the SAME router (Router.select,
with only the eligibility seam replaced), so any decision difference comes from
eligibility alone.

    export LITELLM_LOCAL_MODEL_COST_MAP=True
    export SP_CONFIG_OVERLAY_DIR=$PWD/deploy/dgx_lab/config-overlay
    uv run python docs/research/data/paper-revision/matched_baseline.py

Needs docker (image openpolicyagent/opa:0.70.0). The gate, compile_policy and the
placement resolution stay shipped code and feed all three filters identically; only
the filter (feasible_set vs baseline) differs.
"""
import ast
import asyncio
import contextlib
import copy
import io
import itertools
import json
import os
import statistics
import subprocess
import sys
import tempfile
import time
import tokenize
from collections import Counter, defaultdict
from pathlib import Path

os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "True")
os.environ.setdefault("SP_PII_DETECTION", "regex")
HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[3]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(HERE))

import app.routing.router as router_mod  # noqa: E402
import baseline_filter  # noqa: E402
from app.compression.ccr import CCRStore  # noqa: E402
from app.config.loader import load_settings  # noqa: E402
from app.config.schema import PoolCandidate  # noqa: E402
from app.errors import NoEligibleCandidate  # noqa: E402
from app.eval.cases import load_suite  # noqa: E402
from app.eval.endpoints import build_registry, harness_settings, load_endpoints  # noqa: E402
from app.eval.outcomes import load_export  # noqa: E402
from app.eval.runner import PROFILE, TENANT_ID  # noqa: E402
from app.eval.scenario import load_snapshot  # noqa: E402
from app.eval.simulate import (_first_pick, build_scenario, close_scenario,  # noqa: E402
                               expected_output_tokens, observed_latencies, replay_tokens,
                               scaled_request, select_rows, simulate_row)
from app.gateway.backends.base import count_tokens  # noqa: E402
from app.gateway.gateway import Gateway  # noqa: E402
from app.models import ChatRequest, Entity, ExecutionPolicyContext, Message  # noqa: E402
from app.pipeline import Pipeline  # noqa: E402
from app.policy.context import build_context  # noqa: E402
from app.policy.detectors import make_detector  # noqa: E402
from app.policy.feasible import (DataHandling, FeasibleSet, Rejection,  # noqa: E402
                                 _ctx_scope, candidate_rejection, feasible_set)
from app.apps.db import Database  # noqa: E402
from app.apps.registry import ModelRegistry  # noqa: E402
from app.apps.store import upsert_onboarded  # noqa: E402
from app.policy.gate import Gate  # noqa: E402
from app.routing.router import Router  # noqa: E402
from app.routing.signals import SignalStore  # noqa: E402

OPA_IMAGE = "openpolicyagent/opa:0.70.0"
D = ROOT / "docs/research/data"
RUNS = {1: "live-dgx-lab-2026-10-08", 2: "live-dgx-lab-2026-10-08-run2",
        3: "live-dgx-lab-2026-10-09-run3", 4: "live-dgx-lab-2026-10-09-run4"}
SUITES = [  # (set name, suite, endpoint file)
    ("starter/healthcare-sim", "eval/suites/starter.jsonl", "eval/endpoints/healthcare-sim.yaml"),
    ("extended/healthcare-sim", "eval/suites/extended.jsonl", "eval/endpoints/healthcare-sim.yaml"),
    ("extended/dgx-lab-live-cloud", "eval/suites/extended.jsonl",
     "eval/endpoints/dgx-lab-live-cloud.example.yaml"),
    ("empty-set/cloud-only-sim", "eval/suites/empty-set.jsonl", "eval/endpoints/cloud-only-sim.yaml"),
]


# ----------------------------------------------------------------------------- facts
def endpoint_facts(settings, registry):
    pool = registry.effective_pool() if registry is not None else settings.routing.model_pool
    return pool, [dict(
        name=c.name, provider=c.provider, tier=c.tier, region=c.region,
        prompt_retention=c.prompt_retention, accepts_raw_sensitive=c.accepts_raw_sensitive,
        disabled=bool(c.provider == "onprem" and registry is not None
                      and not registry.is_enabled(c.name))) for c in pool]


def request_facts(rid, ctx, registry, tenant, pool, exclude=frozenset(), open_circuits=frozenset()):
    def lst(s):
        return None if s is None else sorted(s)
    return dict(
        id=rid, onprem_only=ctx.onprem_only, allowed_providers=sorted(ctx.allowed_providers),
        allowed_regions=lst(ctx.allowed_regions), require_no_retention=ctx.require_no_retention,
        sends_raw_sensitive=ctx.sends_raw_sensitive, allowed_tiers=lst(ctx.app_allowed_tiers),
        allowed_models=lst(ctx.app_allowed_models), requested_model=ctx.requested_model,
        exclude=sorted(exclude), open_circuits=sorted(open_circuits),
        app_denied=[c.name for c in pool
                    if registry is not None and not registry.is_allowed_for_app(c.name, tenant)])


# ----------------------------------------------------------------------------- routing
@contextlib.contextmanager
def eligibility_override(pool, names):
    """Replace ONLY the eligibility seam the router calls with a fixed set."""
    orig = router_mod.feasible_set

    def fake(settings, registry, ctx, **kw):
        keep = tuple(c for c in pool if c.name in names)
        rej = tuple(Rejection(c.name, c.provider, c.tier, c.display_name,
                              "baseline: not in eligible set", "baseline")
                    for c in pool if c.name not in names)
        return FeasibleSet(eligible=keep, rejected=rej)
    router_mod.feasible_set = fake
    try:
        yield
    finally:
        router_mod.feasible_set = orig


def route(router, item, names=None):
    """-> dict(outcome, pick, kind, core). names=None runs the shipped router unchanged."""
    ctx, pool = item["ctx"], item["pool"]
    args = (ctx, item["tokens"], item["complexity"])
    kw = dict(output_tokens=item["out_tokens"])
    try:
        if names is None:
            d = router.select(*args, **kw)
        else:
            with eligibility_override(pool, names):
                d = router.select(*args, **kw)
        return dict(outcome="routed", pick=d.chosen.name, kind=None, core=None)
    except NoEligibleCandidate as exc:
        kind = "eligibility" if "residency filter" in str(exc) else (
            "budget" if "budget filter" in str(exc) else "other")
        core = exc.core.ids() if exc.core else None
        return dict(outcome="rejected", pick=None, kind=kind, core=core)


# ----------------------------------------------------------------------------- item builders
async def suite_items(name, suite, endpoints):
    cases, _ = load_suite(str(ROOT / suite))
    base = load_settings()
    # an endpoint file with `live:` blocks only parses in live mode; nothing is called here
    specs, _ = load_endpoints(str(ROOT / endpoints), live="example" in endpoints,
                              tiers=base.routing.tiers)
    settings = harness_settings(base, specs)
    registry, db = await build_registry(settings, specs)
    detector = make_detector(settings.serving.pii_detection)
    gate = Gate(settings, detector, registry=registry)
    sig = SignalStore(settings.routing.signals)
    router = Router(settings, registry=registry, signals=sig)
    co = settings.serving.context_optimization
    pipeline = Pipeline(settings, gate, detector, Gateway(settings, object(), router, registry,
                                                          signals=sig),
                        co_cfg_provider=lambda: co,
                        ccr=CCRStore(ttl_seconds=co.ccr.ttl_hours * 3600), registry=registry)
    items = []
    for case in cases:
        ap = case.policy_view()
        msgs = [Message.model_validate(m) for m in case.messages]
        det, rules, eff = pipeline._gate_inputs(ap, PROFILE)
        ctx, reps, eff = gate.evaluate(f"{case.id}:gate", TENANT_ID,
                                       ChatRequest(model="auto", messages=msgs),
                                       app_policy=ap, detector=det, rules=rules, eff=eff)
        text = (reps.text_raw if eff.protection.model_input == "raw"
                else (reps.text_redacted or reps.text_raw))
        toks = count_tokens(text)
        pool, _ = endpoint_facts(settings, registry)
        items.append(dict(id=case.id, ctx=ctx, tenant=TENANT_ID, tokens=toks,
                          complexity=min(1.0, toks / 2000),
                          out_tokens=expected_output_tokens(settings, None), pool=pool,
                          expect_empty=case.expect_empty))
    return dict(name=name, settings=settings, registry=registry, router=router, items=items,
                db=db, kind="suite")


async def run_items(run):
    p = D / RUNS[run]
    records, _ = load_export(p / "outcomes.ndjson")
    snap, _ = load_snapshot(p / "config-snapshot.json")
    scn = await build_scenario(snap, base=load_settings(ROOT / "config"))
    obs = observed_latencies(records)
    rows, _ = select_rows(records, set(scn.apps))
    items, sane = [], 0
    for r in rows:
        app = scn.apps[r["application_id"]]
        ents = [Entity(type=t, start=0, end=0, value="") for t in r["detected_entity_types"] or []]
        ctx = build_context(scn.settings, r["request_id"], r["application_id"], ents, None,
                            app_policy=app.view, app_rules=app.rules, registry=scn.registry,
                            escalate_residency=app.eff.escalate_residency_on_detection,
                            content_integrity=app.eff.protection.content_integrity,
                            model_input=app.eff.protection.model_input, workload=r.get("workload"))
        ctx = ctx.model_copy(update={"requested_model": r.get("requested_model")})
        rt, _pt, _est = replay_tokens(r)
        toks, cx, _ = scaled_request(rt, float(r["complexity"]), 1.0)
        out = expected_output_tokens(scn.settings,
                                     {"max_tokens": r["max_tokens"]} if r.get("max_tokens") else None)
        pool, _ = endpoint_facts(scn.settings, scn.registry)
        it = dict(id=r["request_id"], ctx=ctx, tenant=r["application_id"], tokens=toks,
                  complexity=cx, out_tokens=out, pool=pool, expect_empty=None)
        # sanity: my shipped first pick must equal simulate_row's first pick
        mine = route(scn.router, it)
        ref = _first_pick(simulate_row(scn, r, obs))
        sane += (mine["pick"] == ref)
        items.append(it)
    return dict(name=f"run{run}", settings=scn.settings, registry=scn.registry, router=scn.router,
                items=items, scn=scn, kind="run", sanity=(sane, len(rows)))


def grid_set():
    """Hand-built contexts over a synthetic pool whose declarations include every
    undeclared / odd-cased shape. Eligibility only (no scoring)."""
    settings = copy.deepcopy(load_settings())
    decl = [  # name, provider, tier, region, retention, raw
        ("m0", "onprem", "cheap", "us", "none", "true"),
        ("m1", "onprem", "mid", "US ", "None", "false"),
        ("m2", "onprem", "frontier", None, None, None),
        ("m3", "azure", "cheap", "eu", "retained", "false"),
        ("m4", "azure", "mid", "us", "none", "unknown"),
        ("m5", "azure", "mid", "", "unknown", ""),
        ("m6", "bedrock", "frontier", "  EU", "NONE", "true"),
        ("m7", "bedrock", "cheap", "us", "retained", None),
        ("m8", "openrouter", "mid", "unknown", "none", "false"),
        ("m9", "openrouter", "frontier", "de", None, "True")]
    settings.routing.model_pool = [PoolCandidate(
        name=n, provider=p, tier=t, region=rg, prompt_retention=rt, accepts_raw_sensitive=rw)
        for n, p, t, rg, rt, rw in decl]
    provs = [["onprem", "azure", "bedrock", "openrouter"], ["onprem"], ["azure"], ["onprem", "azure"]]
    regions = [None, ["us"], ["eu", "de"], []]
    tiers = [None, ["cheap"], ["mid", "frontier"]]
    models = [None, ["m0", "m4"], []]
    pins = [None, "m1", "nope"]
    items = []
    n = 0
    for op, pv, rg, nr, rs, tr, md, pin, ex in itertools.product(
            (False, True), provs, regions, (False, True), (False, True), tiers, models, pins,
            ([], ["m1"])):
        ctx = ExecutionPolicyContext(
            request_id=f"g{n}", tenant_id="t", sensitivity="public", onprem_only=op,
            allowed_providers=frozenset(pv), allowed_models=frozenset(c.name for c in settings.routing.model_pool),
            app_allowed_tiers=None if tr is None else frozenset(tr),
            app_allowed_models=None if md is None else frozenset(md), requested_model=pin,
            max_cost_usd=1.0, max_latency_ms=1000, risk_class="low", require_verification=False,
            detected_entities=(), redaction_map_id=None,
            allowed_regions=None if rg is None else frozenset(rg), require_no_retention=nr,
            sends_raw_sensitive=rs)
        items.append(dict(id=f"g{n}", ctx=ctx, tenant="t", pool=settings.routing.model_pool,
                          exclude=frozenset(ex), expect_empty=None))
        n += 1
    return dict(name="synthetic-grid", settings=settings, registry=None, router=None, items=items,
                kind="grid", core_router=Router(settings))


# endpoint-state grid: exercises the three stages the first grid never reached --
# the endpoint-side app allowlist (`allowed_app_ids`), a disabled model, an open circuit.
# Every endpoint is an ONBOARDED row in a scratch in-memory registry (the only place the
# allowlist and the enabled flag live). Disabled rows are absent from
# registry.effective_pool() (shipped behaviour), so the shipped filter never sees them;
# the baselines are handed them with disabled=True.
STATE_DECL = [  # slug, provider, tier, region, retention, raw, allowed_app_ids, enabled
    ("n0", "onprem", "cheap", "us", "none", True, None, True),
    ("n1", "onprem", "cheap", "us", "none", True, ["app-a"], True),
    ("n2", "onprem", "mid", "us", "none", False, ["app-b"], True),
    ("n3", "onprem", "frontier", "eu", "retained", None, ["app-a", "app-b"], True),
    ("n4", "azure", "cheap", "us", "none", True, None, True),
    ("n5", "azure", "mid", "eu", "none", False, ["app-a"], True),
    ("n6", "bedrock", "frontier", "us", "retained", True, None, True),
    ("n7", "openrouter", "mid", "de", "none", None, ["app-c"], True),
    ("d0", "onprem", "cheap", "us", "none", True, None, False),
    ("d1", "onprem", "mid", "us", "none", True, ["app-a"], False),
    ("d2", "azure", "mid", "us", "none", True, None, False),
]


async def state_registry(settings, allowlists=True):
    db = Database(url="sqlite+aiosqlite://")
    await db.ensure_ready()
    async with db.session() as session:
        for slug, prov, tier, rg, rt, rw, ids, en in STATE_DECL:
            await upsert_onboarded(
                session, slug, display_name=slug, provider=prov, tier=tier,
                quality=0.8, price_input=0.1, price_output=0.2,
                price_cached_input=None, endpoint=f"sim://{slug}", api_key_env=None,
                enabled=en, probe_status="healthy", region=rg, prompt_retention=rt,
                accepts_raw_sensitive=rw, allowed_app_ids=(ids if allowlists else None))
        await session.commit()
    reg = ModelRegistry(settings)
    await reg.refresh(db)
    return reg, db


async def grid_state_set():
    settings = copy.deepcopy(load_settings())
    settings.routing.model_pool = []     # every endpoint is an onboarded row
    reg, db = await state_registry(settings)
    reg_noallow, db2 = await state_registry(settings, allowlists=False)
    enabled_names = frozenset(r[0] for r in STATE_DECL if r[7])
    extra_eps = [dict(name=slug, provider=prov, tier=tier, region=rg, prompt_retention=rt,
                      accepts_raw_sensitive=rw, disabled=True)
                 for slug, prov, tier, rg, rt, rw, ids, en in STATE_DECL if not en]
    opens = [[], ["n0"], ["n0", "n1", "n2", "n3"], ["n4", "n5", "n6", "n7"], sorted(enabled_names)]
    provs = [["onprem", "azure", "bedrock", "openrouter"], ["onprem"]]
    items, n = [], 0
    for tn, oc, op, pv, pin, tr, rg, nr in itertools.product(
            ("app-a", "app-b", "app-c"), opens, (False, True), provs,
            (None, "n0", "n5", "d0"), (None, ["cheap"]), (None, ["us"]), (False, True)):
        ctx = ExecutionPolicyContext(
            request_id=f"s{n}", tenant_id=tn, sensitivity="public", onprem_only=op,
            allowed_providers=frozenset(pv), allowed_models=enabled_names,
            app_allowed_tiers=None if tr is None else frozenset(tr), app_allowed_models=None,
            requested_model=pin, max_cost_usd=1.0, max_latency_ms=1000, risk_class="low",
            require_verification=False, detected_entities=(), redaction_map_id=None,
            allowed_regions=None if rg is None else frozenset(rg), require_no_retention=nr,
            sends_raw_sensitive=False)
        items.append(dict(id=f"s{n}", ctx=ctx, tenant=tn, pool=None, exclude=frozenset(),
                          open_circuits=frozenset(oc), expect_empty=None))
        n += 1
    return dict(name="synthetic-state-grid", settings=settings, registry=reg, router=None,
                items=items, kind="grid", core_router=Router(settings, registry=reg),
                db=db, db2=db2, reg_noallow=reg_noallow, extra_eps=extra_eps)


# ----------------------------------------------------------------------------- OPA
def opa_batch(sets_input):
    """ONE container, ONE `opa eval` for every request of every input set."""
    with tempfile.TemporaryDirectory() as td:
        (Path(td) / "input.json").write_text(json.dumps({"sets": sets_input}))
        t0 = time.perf_counter()
        p = subprocess.run(
            ["docker", "run", "--rm", "-i", "-v", f"{HERE}:/policy:ro", OPA_IMAGE, "eval", "-I",
             "-d", "/policy/eligibility.rego", "-f", "json", "--metrics", "data.eligibility.batch"],
            stdin=open(Path(td) / "input.json"), capture_output=True, text=True)
        wall = time.perf_counter() - t0
    if p.returncode:
        raise SystemExit("opa eval failed: " + p.stderr[-2000:])
    out = json.loads(p.stdout)
    return out["result"][0]["expressions"][0]["value"], out["metrics"], wall


def opa_bench(endpoints, request, policy="/policy/eligibility.rego", query="data.eligibility.single",
              extra_mount=None):
    with tempfile.TemporaryDirectory() as td:
        (Path(td) / "i.json").write_text(json.dumps({"request": request, "endpoints": endpoints}))
        mounts = ["-v", f"{HERE}:/policy:ro", "-v", f"{td}:/in:ro"]
        if extra_mount:
            mounts += ["-v", f"{extra_mount}:/floor:ro"]
        p = subprocess.run(
            ["docker", "run", "--rm", *mounts, OPA_IMAGE, "bench", "-d", policy,
             "-i", "/in/i.json", "-f", "json", query], capture_output=True, text=True)
    if p.returncode:
        raise SystemExit("opa bench failed: " + p.stderr[-2000:])
    out = json.loads(p.stdout)
    x = out["Extra"]
    return dict(n=out["N"], mean_us=out["T"] / out["N"] / 1000,
                median_us=x["histogram_timer_rego_query_eval_ns_median"] / 1000,
                p99_us=x["histogram_timer_rego_query_eval_ns_99%"] / 1000)


def opa_startup():
    ts = []
    for _ in range(5):
        t0 = time.perf_counter()
        subprocess.run(["docker", "run", "--rm", OPA_IMAGE, "version"], capture_output=True)
        ts.append(time.perf_counter() - t0)
    return statistics.median(ts), min(ts), max(ts)


# ----------------------------------------------------------------------------- LOC
def loc_py(path, funcs=None):
    """Non-blank, non-comment, non-docstring physical lines; optionally only inside the
    named top-level functions."""
    src = Path(path).read_text()
    tree = ast.parse(src)
    skip, keep = set(), set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.ClassDef, ast.AsyncFunctionDef)):
            b = node.body
            if b and isinstance(b[0], ast.Expr) and isinstance(getattr(b[0], "value", None), ast.Constant) \
                    and isinstance(b[0].value.value, str):
                skip.update(range(b[0].lineno, b[0].end_lineno + 1))
        if funcs and isinstance(node, ast.FunctionDef) and node.name in funcs:
            keep.update(range(node.lineno, node.end_lineno + 1))
    lines = set()
    for tok in tokenize.generate_tokens(io.StringIO(src).readline):
        if tok.type in (tokenize.COMMENT, tokenize.NL, tokenize.NEWLINE, tokenize.INDENT,
                        tokenize.DEDENT, tokenize.ENDMARKER):
            continue
        lines.add(tok.start[0])
    lines -= skip
    if funcs:
        lines &= keep
    return len(lines)


def loc_rego(path):
    return sum(1 for ln in Path(path).read_text().splitlines()
               if ln.strip() and not ln.strip().startswith("#"))


# ----------------------------------------------------------------------------- main
def pct(a, b):
    return f"{a}/{b}" + (f" ({100*a/b:.1f}%)" if b else "")


def describe_diff(item_pool_rej, shipped, other):
    only_s, only_o = sorted(shipped - other), sorted(other - shipped)
    return only_s, only_o


async def main():
    print("== matched baseline: shipped feasible_set vs plain filter vs OPA/Rego ==")
    print(f"OPA image: {OPA_IMAGE}")
    sets = []
    for name, suite, ep in SUITES:
        sets.append(await suite_items(name, suite, ep))
    for r in (1, 2, 3, 4):
        sets.append(await run_items(r))
    sets.append(grid_set())
    sets.append(await grid_state_set())
    for s in sets:
        if s["kind"] == "run":
            print(f"sanity {s['name']}: my shipped first pick == simulate_row first pick on "
                  f"{s['sanity'][0]}/{s['sanity'][1]} rows")

    # ---- shipped eligible sets, hop 1 and hop 2 (exclude shipped first pick) ----
    opa_in, reqs_by_set = {}, {}
    for s in sets:
        reg, st = s["registry"], s["settings"]
        eps = None
        reqs = []
        for it in s["items"]:
            tenant = it["tenant"]
            pool, eps_here = endpoint_facts(st, reg)
            eps = eps_here + s.get("extra_eps", [])
            ex1 = it.get("exclude", frozenset())
            oc1 = it.get("open_circuits", frozenset())
            sf = feasible_set(st, reg, it["ctx"], tenant_id=tenant, exclude=ex1, open_circuits=oc1)
            it["shipped"] = {c.name for c in sf.eligible}
            it["shipped_rej"] = {r.name: r.code for r in sf.rejected}
            it["shipped_reason"] = {r.name: r.reason for r in sf.rejected}
            it["req1"] = request_facts(f"h1:{it['id']}", it["ctx"], reg, tenant, pool, ex1, oc1)
            reqs.append(it["req1"])
            if s["kind"] == "grid":
                continue
            it["ship_route"] = route(s["router"], it)
            pk = it["ship_route"]["pick"]
            ex2 = frozenset({pk}) if pk else None
            if ex2:
                sf2 = feasible_set(st, reg, it["ctx"], tenant_id=tenant, exclude=ex2)
                it["shipped2"] = {c.name for c in sf2.eligible}
                it["shipped2_rej"] = {r.name: r.code for r in sf2.rejected}
                it["req2"] = request_facts(f"h2:{it['id']}", it["ctx"], reg, tenant, pool, ex2)
                reqs.append(it["req2"])
            else:
                it["shipped2"] = None
        opa_in[s["name"]] = dict(endpoints=eps, requests=reqs)
        s["eps"] = eps

    print("\n== Predicate exercise: endpoint rejections by shipped stage code, hop 1 (all endpoints x requests) ==")
    for s in sets:
        cc, sizes = Counter(), []
        for it in s["items"]:
            cc.update(it["shipped_rej"].values())
            sizes.append(len(it["shipped"]))
        print(f"{s['name']:32s} eligible-set size min/median/max {min(sizes)}/{statistics.median(sizes)}/"
              f"{max(sizes)}, empty {sum(1 for z in sizes if z == 0)}; rejections {dict(sorted(cc.items()))}")

    opa_out, opa_metrics, opa_wall = opa_batch(opa_in)
    total_req = sum(len(v["requests"]) for v in opa_in.values())
    print(f"\nOPA batch: {total_req} requests over {len(opa_in)} input sets in ONE `opa eval` "
          f"(one container); wall incl. container start {opa_wall*1000:.0f} ms; "
          f"query eval {opa_metrics['timer_rego_query_eval_ns']/1e6:.1f} ms total "
          f"= {opa_metrics['timer_rego_query_eval_ns']/total_req/1000:.1f} us/request (batch)")

    # ---- agreement ----
    disagreements = []   # (baseline, set, hop, id, only_shipped, only_baseline, codes)
    summary = {}
    core_stats = {}
    for s in sets:
        n = len(s["items"])
        st = dict(n=n, h2n=0)
        for b in ("filter", "opa"):
            st[b] = Counter()
        st["ship_empty"] = st["ship_budget_rej"] = 0
        cores = defaultdict(Counter)
        for it in s["items"]:
            sh = it["shipped"]
            ep = s["eps"]
            for b in ("filter", "opa"):
                if b == "filter":
                    got = baseline_filter.eligible(it["req1"], ep)
                else:
                    got = set(opa_out[s["name"]][it["req1"]["id"]])
                it[b] = got
                st[b]["set_eq"] += (got == sh)
                st[b]["adm_eq"] += ((not got) == (not sh))
                if got != sh:
                    disagreements.append((b, s["name"], 1, it["id"], sorted(sh - got),
                                          sorted(got - sh),
                                          sorted({it["shipped_rej"].get(x, "?") for x in got - sh}),
                                          it["req1"]))
                if s["kind"] == "grid":
                    continue
                r = route(s["router"], it, names=got)
                it[b + "_route"] = r
                sr = it["ship_route"]
                st[b]["outcome_eq"] += (r["outcome"] == sr["outcome"])
                st[b]["decision_eq"] += ((r["outcome"], r["pick"]) == (sr["outcome"], sr["pick"]))
                if sr["outcome"] == "routed":
                    st[b]["routed_n"] += 1
                    st[b]["pick_eq"] += (r["pick"] == sr["pick"])
                # hop 2
                if it.get("shipped2") is not None:
                    st[b]["h2_n"] += 1
                    if b == "filter":
                        got2 = baseline_filter.eligible(it["req2"], ep)
                    else:
                        got2 = set(opa_out[s["name"]][it["req2"]["id"]])
                    st[b]["h2_eq"] += (got2 == it["shipped2"])
                    if got2 != it["shipped2"]:
                        disagreements.append((b, s["name"], 2, it["id"], sorted(it["shipped2"] - got2),
                                              sorted(got2 - it["shipped2"]),
                                              sorted({it["shipped2_rej"].get(x, "?") for x in got2 - it["shipped2"]}),
                                              it["req2"]))
            if s["kind"] != "grid":
                sr = it["ship_route"]
                if sr["outcome"] == "rejected":
                    cores[sr["kind"]][len(sr["core"]) if sr["core"] is not None else "none"] += 1
                if not sh:
                    st["ship_empty"] += 1
            else:
                st["ship_empty"] += (not sh)
                if not sh:   # the shipped conflicting core for this empty set (hop 1)
                    # the core is policy-only (circuits are state, never policy): compute it
                    # only when the set is policy-empty, i.e. empty without the circuits too
                    if s["name"] == "synthetic-state-grid" and it.get("open_circuits"):
                        pol = feasible_set(s["settings"], s["registry"], it["ctx"],
                                           tenant_id=it["tenant"],
                                           exclude=it.get("exclude", frozenset()))
                        if pol.eligible:
                            st["empty_circuit_only"] = st.get("empty_circuit_only", 0) + 1
                            continue
                    c = s["core_router"]._policy_core(it["ctx"], it.get("exclude", frozenset()))
                    cores["eligibility"][len(c.ids())] += 1
                    st.setdefault("core_req", Counter()).update(c.ids())
                    st.setdefault("core_state", Counter()).update(c.state_ids())
        st["kind"] = s["kind"]
        if s["kind"] != "grid":
            st["ship_rej_total"] = sum(sum(c.values()) for c in cores.values())
        st["cores"] = {k: dict(v) for k, v in cores.items()}
        summary[s["name"]] = st
        if s["kind"] == "suite":
            ee = [it for it in s["items"] if it["expect_empty"]]
            st["expect_empty_n"] = len(ee)
            st["expect_empty_ok"] = {k: sum(1 for it in ee if not it[k]) for k in ("shipped", "filter", "opa")}

    print("\n== Agreement with the shipped eligible set (exact set equality), hop 1 ==")
    print(f"{'input set':32s} {'n':>5s} | {'filter set=':>14s} {'filter adm=':>14s} | {'OPA set=':>14s} {'OPA adm=':>14s}")
    for name, st in summary.items():
        print(f"{name:32s} {st['n']:5d} | {pct(st['filter']['set_eq'], st['n']):>14s} "
              f"{pct(st['filter']['adm_eq'], st['n']):>14s} | {pct(st['opa']['set_eq'], st['n']):>14s} "
              f"{pct(st['opa']['adm_eq'], st['n']):>14s}")
    print("\n== Same router on each eligible set: outcome, first pick, hop-2 (exclude shipped pick) ==")
    print(f"{'input set':32s} | {'baseline':>7s} {'outcome=':>14s} {'pick= (shipped routed)':>24s} "
          f"{'hop2 set=':>14s}")
    for name, st in summary.items():
        if st["kind"] == "grid":
            continue
        for b in ("filter", "opa"):
            print(f"{name:32s} | {b:>7s} {pct(st[b]['outcome_eq'], st['n']):>14s} "
                  f"{pct(st[b]['pick_eq'], st[b]['routed_n']):>24s} {pct(st[b]['h2_eq'], st[b]['h2_n']):>14s}")
    print("\n== expect_empty cases (suite): empty under each filter ==")
    for name, st in summary.items():
        if "expect_empty_n" in st:
            print(f"{name:32s} n={st['expect_empty_n']} empty: {st['expect_empty_ok']}")

    print("\n== What only the shipped implementation produces: conflicting cores on rejection ==")
    print("(router rejections under the SHIPPED filter, by kind -> core-size distribution)")
    for name, st in summary.items():
        if st["kind"] == "grid":
            print(f"{name:32s} eligible-empty requests: {st['ship_empty']}/{st['n']}; "
                  f"shipped core size distribution {st['cores']}"
                  + (f"; empty only because of open circuits (policy set non-empty, no core): "
                     f"{st['empty_circuit_only']}" if "empty_circuit_only" in st else ""))
            continue
        print(f"{name:32s} eligibility-empty at hop 1: {st['ship_empty']}/{st['n']}; "
              f"router rejections by kind {st['cores'] or '{}'}")
    print("Each baseline reports the empty set only: no reason code, no core, no named "
          "requirement. (Router rejections in this harness still carry a core under the baselines "
          "because the shared router is shipped code; that is not a baseline capability.)")

    # ---- endpoint-state grid: stage exercise, empties by cause, agreement ----
    g = next(x for x in sets if x["name"] == "synthetic-state-grid")
    gst, greg, gset = g["settings"], g["registry"], g
    print("\n== Endpoint-state grid (allowlist / disabled / open circuit): shipped rejections ==")
    print(f"{len(g['items'])} requests x {len(gst.routing.model_pool) + len(greg.effective_pool())} "
          f"enabled endpoints in the pool (+{len(g['extra_eps'])} disabled onboarded rows "
          f"{[e['name'] for e in g['extra_eps']]}, which the registry keeps OUT of the pool)")
    ga0 = summary["synthetic-state-grid"]
    cr = Counter()
    for it in g["items"]:
        for nme, code in it["shipped_rej"].items():
            cr[(code, it["shipped_reason"][nme])] += 1
    for (code, reason), c in sorted(cr.items()):
        print(f"  {code:24s} {reason:42s} {c:7d}")
    print(f"  stage nonzero? endpoint allowlist (model_scope / 'not permitted for application'): "
          f"{cr[('model_scope', 'not permitted for application')] > 0}; "
          f"open circuit (circuit_open): {cr[('circuit_open', 'excluded \u00b7 circuit open')] > 0}; "
          f"disabled via feasible_set (model_scope / 'disabled by admin'): "
          f"{cr[('model_scope', 'disabled by admin')] > 0}")
    # predicate-level exercise of the disabled stage: the pool never contains a disabled
    # onboarded row, so feasible_set cannot reject one; candidate_rejection can, if handed it
    dis_rej, dis_n = Counter(), 0
    for it in g["items"]:
        ctx = it["ctx"].model_copy(update={"allowed_models": frozenset(r[0] for r in STATE_DECL)})
        provs_, onp = frozenset(ctx.allowed_providers), ctx.onprem_only
        if onp:
            provs_ = provs_ & {"onprem"}
        for slug, prov, tier, rg, rt, rw, ids, en in STATE_DECL:
            if en:
                continue
            c = PoolCandidate(name=slug, provider=prov, tier=tier, region=rg,
                              prompt_retention=rt,
                              accepts_raw_sensitive=None if rw is None else str(rw).lower())
            r = candidate_rejection(
                c, allowed_providers=provs_, onprem_only=onp, in_scope=_ctx_scope(ctx),
                requested_model=ctx.requested_model, exclude=frozenset(), registry=greg,
                tenant_id=it["tenant"], data_handling=DataHandling.from_context(ctx),
                open_circuits=it["open_circuits"])
            dis_n += 1
            dis_rej[(r.code, r.reason) if r else None] += 1
    print("  disabled stage at predicate level (candidate_rejection handed each disabled row, "
          f"{dis_n} evaluations): { {str(k): v for k, v in sorted(dis_rej.items(), key=str)} }")
    print(f"  shipped conflicting cores of the policy-empty requests: requirement ids named "
          f"{dict(sorted(ga0['core_req'].items()))}; endpoint state reported "
          f"{dict(sorted(ga0['core_state'].items()))}")
    # empties by cause (counterfactual: remove one stage, does the set become non-empty?)
    emp = [it for it in g["items"] if not it["shipped"]]
    by = Counter()
    for it in emp:
        base = dict(settings=gst, ctx=it["ctx"], tenant_id=it["tenant"], exclude=it["exclude"])
        no_circ = feasible_set(gst, greg, it["ctx"], tenant_id=it["tenant"],
                               exclude=it["exclude"]).eligible
        no_allow = feasible_set(gst, g["reg_noallow"], it["ctx"], tenant_id=it["tenant"],
                                exclude=it["exclude"], open_circuits=it["open_circuits"]).eligible
        no_both = feasible_set(gst, g["reg_noallow"], it["ctx"], tenant_id=it["tenant"],
                               exclude=it["exclude"]).eligible
        by["empty total"] += 1
        by["empty, would be non-empty without the open circuits"] += bool(no_circ)
        by["empty, would be non-empty without the endpoint allowlists"] += bool(no_allow)
        by["empty, non-empty only if BOTH removed"] += (not no_circ and not no_allow and bool(no_both))
        by["empty regardless of circuits and allowlists"] += (not no_both)
    for k, v in by.items():
        print(f"  {k:62s} {v:6d}/{len(g['items'])}")
    # does the router still route when circuits empty the eligible set? (it bypasses stage 7)
    rr = Counter()
    rtr = Router(gst, registry=greg)
    for it in emp:
        pol = feasible_set(gst, greg, it["ctx"], tenant_id=it["tenant"], exclude=it["exclude"])
        if not pol.eligible:
            continue
        try:
            rtr.select(it["ctx"], 200, 0.1, output_tokens=100, open_circuits=it["open_circuits"])
            rr["routed (breaker bypass within the policy set)"] += 1
        except NoEligibleCandidate:
            rr["rejected"] += 1
    print(f"  router on the circuit-emptied requests (policy set non-empty): {dict(rr)}")
    ga = summary["synthetic-state-grid"]
    print("  agreement on this grid: "
          f"filter set= {pct(ga['filter']['set_eq'], ga['n'])}, OPA set= {pct(ga['opa']['set_eq'], ga['n'])}")
    # agreement restricted to requests where each stage actually fired
    for label, pred in (
            ("allowlist rejected >=1 endpoint", lambda it: any(c == "model_scope" and rs.startswith("not permitted")
                                                        for c, rs in ((it["shipped_rej"][n], it["shipped_reason"][n]) for n in it["shipped_rej"]))),
            ("open circuit rejected >=1 endpoint", lambda it: "circuit_open" in it["shipped_rej"].values()),
            ("empty set (any cause)", lambda it: not it["shipped"])):
        sub = [it for it in g["items"] if pred(it)]
        print(f"  {label:36s} n={len(sub):5d}  filter set= {pct(sum(it['filter'] == it['shipped'] for it in sub), len(sub))}"
              f"  OPA set= {pct(sum(it['opa'] == it['shipped'] for it in sub), len(sub))}")

    # ---- disagreements ----
    print("\n== Disagreements (baseline eligible set != shipped) ==")
    if not disagreements:
        print("none")
    grp = defaultdict(list)
    for d in disagreements:
        grp[(d[0], d[1], tuple(d[4]) and "shipped-only" or "", tuple(d[5]) and "baseline-only" or "")].append(d)
    for key, lst in sorted(grp.items()):
        print(f"{key[0]:7s} {key[1]:32s} {len(lst):5d} disagreements "
              f"(baseline missing endpoints: {sum(1 for d in lst if d[4])}, "
              f"baseline extra endpoints: {sum(1 for d in lst if d[5])})")
    shown = Counter()
    for d in disagreements:
        b, sname, hop, rid, only_s, only_b, codes, req = d
        sig = (b, sname, tuple(only_s), tuple(only_b), tuple(codes))
        shown[sig] += 1
    print("-- distinct disagreement signatures (baseline, set, shipped-only endpoints, "
          "baseline-only endpoints, shipped reject code of baseline-only) : count")
    for sig, c in sorted(shown.items(), key=lambda kv: (-kv[1], kv[0]))[:60]:
        print(f"  {c:5d} x  {sig}")
    if len(shown) > 60:
        print(f"  ... {len(shown) - 60} more signatures")
    ex = {}
    for d in disagreements:
        ex.setdefault((d[0], d[1]), d)
    print("-- one full example per (baseline, set):")
    for k, d in ex.items():
        print(f"  {k}: id={d[3]} hop={d[2]} shipped-only={d[4]} baseline-only={d[5]} "
              f"shipped-code-of-baseline-only={d[6]}\n      request={json.dumps(d[7], sort_keys=True)}")

    # ---- LOC ----
    print("\n== Lines of policy code (non-blank, non-comment, non-docstring physical lines) ==")
    ff = ROOT / "app/policy/feasible.py"
    verdict_fns = {"provider_placeable", "_declared", "_region_rejection", "_retention_rejection",
                   "_raw_sensitive_rejection", "tier_in_scope", "model_in_scope", "not_requested",
                   "endpoint_allows_app", "is_disabled", "candidate_rejection", "feasible_set",
                   "_resolve_placement", "_ctx_scope", "_data_handling_rejection", "_placement_providers",
                   "_pool"}
    print(f"shipped app/policy/feasible.py, whole file          : {loc_py(ff)}")
    print(f"shipped feasible.py, verdict-path functions only     : {loc_py(ff, verdict_fns)} "
          f"({len(verdict_fns)} functions: {', '.join(sorted(verdict_fns))})")
    print(f"shipped app/policy/core.py (conflicting-core search) : {loc_py(ROOT / 'app/policy/core.py')}")
    print(f"plain filter baseline_filter.py, whole file          : {loc_py(HERE / 'baseline_filter.py')}")
    print(f"plain filter, eligible()+declared() only             : "
          f"{loc_py(HERE / 'baseline_filter.py', {'eligible', 'declared'})}")
    print(f"OPA eligibility.rego, whole file                     : {loc_rego(HERE / 'eligibility.rego')}")

    # ---- timing ----
    print("\n== Per-request eligibility time (microseconds) ==")
    print(f"(1-minute load average at timing start: {os.getloadavg()[0]:.1f} on {os.cpu_count()} CPUs)")

    def timeit(fn, items, rounds=15):
        for it in items:
            fn(it)
        xs = []
        for _ in range(rounds):
            for it in items:
                a = time.perf_counter_ns()
                fn(it)
                xs.append(time.perf_counter_ns() - a)
        xs.sort()
        return statistics.median(xs) / 1000, xs[int(0.99 * len(xs))] / 1000, len(xs)

    for s in sets:
        if s["kind"] == "grid":
            continue
        sample = s["items"][:200]
        st_, reg = s["settings"], s["registry"]
        sh = timeit(lambda it: feasible_set(st_, reg, it["ctx"], tenant_id=it["tenant"]), sample)
        fl = timeit(lambda it: baseline_filter.eligible(it["req1"], s["eps"]), sample)
        print(f"{s['name']:32s} shipped feasible_set median {sh[0]:7.1f} p99 {sh[1]:7.1f} (n={sh[2]}) | "
              f"plain filter median {fl[0]:6.2f} p99 {fl[1]:6.2f}")
    print("(shipped number includes registry.effective_pool(); the filter's endpoint/request dicts "
          "are pre-built outside the timed region, so its figure is the predicate cost alone.)")
    print("\nOPA `opa bench` (eval of data.eligibility.single per request; container start excluded;"
          " bench's own default sampling):")
    for s in sets:
        if s["kind"] == "grid":
            continue
        it = s["items"][0]
        o = opa_bench(s["eps"], it["req1"])
        print(f"{s['name']:32s} {len(s['eps'])} endpoints: N={o['n']} mean {o['mean_us']:.1f} us "
              f"median {o['median_us']:.1f} us p99 {o['p99_us']:.1f} us")
    with tempfile.TemporaryDirectory() as fd:
        (Path(fd) / "floor.rego").write_text(
            "package floor\nimport rego.v1\nsingle := {e.name | some e in input.endpoints}\n")
        s0 = next(x for x in sets if x["kind"] == "run")
        o = opa_bench(s0["eps"], s0["items"][0]["req1"], policy="/floor/floor.rego",
                      query="data.floor.single", extra_mount=fd)
    print(f"{'calibration: trivial policy':32s} (returns all endpoint names, no predicates): "
          f"N={o['n']} mean {o['mean_us']:.1f} us median {o['median_us']:.1f} us p99 {o['p99_us']:.1f} us")
    c = opa_startup()
    print(f"OPA container start+exit (`opa version`, 5 runs): median {c[0]*1000:.0f} ms "
          f"(min {c[1]*1000:.0f}, max {c[2]*1000:.0f}) -- NOT a per-request cost; paid once per batch")

    for s in sets:
        for k in ("db", "db2"):
            if s.get(k) is not None:
                await s[k].dispose()
        if s.get("scn") is not None:
            await close_scenario(s["scn"])


asyncio.run(main())
