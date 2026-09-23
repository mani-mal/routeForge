"""The FeasibleRouteBench runner (spec §5-§8).

Assembles RouteForge's REAL serving pipeline in-process -- gate,
`compile_policy`, `feasible_set`, redaction, compression, router, gateway --
over the harness's own scratch registry (`app/eval/endpoints.py`), and per
case:

1. evaluates the gate and asks `feasible_set` which endpoints the case may
   use (the post-gate set is authoritative: the gate may tighten residency);
2. sweeps every eligible endpoint with a PINNED request, so each model sees
   exactly the model-facing view production would show it, grading each
   completion (`matrix.jsonl`, written row by row);
3. scores every strategy's failover-aware ATTEMPT CHAIN (B48 spec §5) by
   looking each attempt up in that case's matrix rows (`choices.jsonl`) -- no
   strategy makes a model call. `router_online` then learns from its own
   attempts on this case, after it was scored (spec §6).

After the last case it computes the feasible-oracle metrics (`metrics.json`),
a human report (`report.md`) and a run stamp (`run.json`). Exit codes: `0` ok,
`1` load/usage error (including a `seconds_per_case` too small for this run's
endpoint count or attempt cap -- `strategies.min_seconds_per_case`), `2`
policy violation, empty-set mismatch, post-gate widening, pinned-served
mismatch (`_exit_code`), or a signal arm's own leak guard firing
(`strategies.LeakageError`, an integrity bug -- `run_bench`).

`metrics.json` carries NO timestamps, paths or timings -- for a fixed `--seed`
a simulated run's `metrics.json` is byte-identical run to run (global
constraint). Those live in `run.json` only.

Offline harness code only -- never imported by the serving path.
"""
from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import yaml

from app.apps.privacy import AppPrivacyProfile
from app.compression.ccr import CCRStore
from app.config.loader import load_settings
from app.errors import BackendUnavailable, ContextWindowExceeded, NoEligibleCandidate
from app.eval.cases import Case, SuiteError, load_suite
from app.eval.endpoints import (
    EndpointFileError,
    build_registry,
    harness_settings,
    load_endpoints,
)
from app.eval.graders import grade
from app.eval.oracle import (
    CaseInfo,
    Choice,
    MatrixRow,
    attempt_outcome,
    grouped,
    headline,
    strategy_metrics,
    violations,
    worst_groups,
)
from app.eval.simulated import SimulatedBackend
from app.eval.strategies import (
    DEFAULT_SECONDS_PER_CASE,
    AttemptStrategy,
    LeakageError,
    OnlineArm,
    StrategyInput,
    attempt_cap,
    best_published_quality_attempts,
    check_seconds_per_case,
    cheapest_attempts,
    fixed_attempts,
    min_seconds_per_case,
    router_attempts,
    variant_settings,
    warm_prior_signals,
)
from app.gateway.backends.base import count_tokens
from app.gateway.backends.factory import make_backend
from app.gateway.gateway import Gateway, counts_as_endpoint_unhealth
from app.models import ChatRequest, Message
from app.pipeline import Pipeline
from app.policy.detectors import make_detector
from app.policy.feasible import feasible_set
from app.policy.gate import Gate
from app.routing.router import Router, expected_output_tokens
from app.routing.signals import SignalStore

TENANT_ID = "feasible-bench"
PROFILE = AppPrivacyProfile(recognizers=(), rules=())
GROUP_TAGS = ("workload", "placement", "protection")
HEADLINE_ROUTER = "router_online"
HEADLINE_REFERENCE = "best_published_quality"

# run.json `notes` / report.md -- what each router arm means and what its
# numbers depend on (B48 spec §6).
ARM_NOTES = {
    "router_cold": (
        "router_cold scores every case with an empty signal store: the "
        "no-measurement baseline."),
    "router_online": (
        "router_online is the reported arm: its store holds only the attempts "
        "this arm itself made on earlier cases (failovers included), applied "
        "after each case is scored; an error counts only when production's "
        "counts_as_endpoint_unhealth would count it. B45 is read from "
        "router_online:<variant>."),
    "router_warm_prior": (
        "router_warm_prior replays every matrix row of every earlier case "
        "(all eligible endpoints) before scoring a case: a full-information "
        "reference (not an upper bound), never the reported number."),
    "breaker_off": (
        "router_online:breaker_off is the control: the same online arm with "
        "the step-7 circuit breaker disabled."),
    "case_order": (
        "router_online and router_warm_prior depend on the suite's case order: "
        "a case is scored with what earlier cases taught the arm, and outages "
        "are indexed by case position."),
    "case_clock": (
        "Both non-cold arms use a synthetic case clock: case k happens at "
        "t_k = k x seconds_per_case (1 ms sub-steps within a case) and is "
        "scored reading at t_k, so availability decay (half_life_s) and "
        "latency staleness depend on seconds_per_case -- quote it with any "
        "number. The step-7 breaker (router_online, unless breaker_off) runs "
        "on the SAME case clock, so seconds_per_case also sets how much of "
        "an open cooldown a case gap actually covers: at seconds_per_case == "
        "cooldown_s (30 by default) the FIRST open period ends exactly at "
        "the next case (its read lands at t >= open_until, i.e. already "
        "half-open), so that first cooldown is invisible in the chosen "
        "chains -- only a later, doubled cooldown can span a full case gap. "
        "At seconds_per_case >= max_cooldown_s no cooldown, however many "
        "times it has doubled, can ever outlast one case gap, so the "
        "breaker never excludes anything: router_online and "
        "router_online:breaker_off produce identical chains (a documented "
        "no-op, not a bug -- see TestBreakerCaseClockInteraction)."),
    "failure_latency": (
        "A failed attempt with unknown failure latency (e.g. a live failure) "
        "counts as 0 ms in latency figures."),
    "violations_units": (
        "policy.violations_total counts ATTEMPTS (every attempt outside the "
        "case's eligible set, plus sweep calls); each strategy's "
        "violation_rate is per CASE (at most one per case)."),
}

BASE_RPV_NOTE = (
    "base_routing_policy_version is the deployment's routing+pricing hash BEFORE "
    "the harness overrides (emptied model_pool, endpoint-file prices, "
    "max_escalations 0, and any --variants weights); it identifies the base "
    "config, not the exact settings the sweep ran under.")

DEFERRED_METRICS = (
    "calibration (the router emits no confidence)",
    "policy/endpoint churn robustness",
    "noninterference failure rate (owned by P1a's policy-compiler harness)",
)


GENERATE_COUNT_MISMATCH = "GenerateCountMismatch"


class BenchError(Exception):
    """A load/usage error: the run stops with exit code 1."""


@dataclass(frozen=True)
class RunResult:
    exit_code: int
    out_dir: str
    metrics: dict


class _CountingBackend:
    """Delegates to the real/simulated backend, counting `generate` calls
    per case -- the evidence behind `CaseInfo.rejected_ok`'s "no model call"
    clause for an empty case."""

    def __init__(self, inner) -> None:
        self._inner = inner
        self._case_id: str | None = None
        self.calls: dict[str, int] = {}

    def set_case(self, case: Case, index: int) -> None:
        self._case_id = case.id
        if hasattr(self._inner, "set_case"):
            self._inner.set_case(case, index)

    async def generate(self, *args, **kwargs):
        if self._case_id is not None:
            self.calls[self._case_id] = self.calls.get(self._case_id, 0) + 1
        return await self._inner.generate(*args, **kwargs)

    def __getattr__(self, name):
        return getattr(self._inner, name)


# --------------------------------------------------------------------------
# loading
# --------------------------------------------------------------------------

def _load_variants(path: str | None) -> dict[str, dict[str, float]]:
    """A `--variants` file: a YAML mapping `name -> weights mapping`. Each
    weights mapping is validated later by constructing it through
    `strategies.variant_settings` (which runs the real `RoutingCfg`
    validation and names any missing key)."""
    if path is None:
        return {}
    try:
        doc = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise BenchError(f"variants file {path}: {exc}") from exc
    if not isinstance(doc, dict) or not doc:
        raise BenchError(f"variants file {path}: expected a non-empty mapping "
                         "of variant name -> weights mapping")
    variants: dict[str, dict[str, float]] = {}
    for name, weights in doc.items():
        if not isinstance(name, str) or not isinstance(weights, dict):
            raise BenchError(f"variants file {path}: variant {name!r} must map "
                             "a name to a weights mapping")
        variants[name] = dict(weights)
    return dict(sorted(variants.items()))


def _git_sha() -> str:
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=Path(__file__).resolve().parents[2],
            capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    sha = proc.stdout.strip()
    return sha if proc.returncode == 0 and sha else "unknown"


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _dump(obj) -> str:
    return json.dumps(obj, sort_keys=True, indent=2) + "\n"


# --------------------------------------------------------------------------
# per-case work
# --------------------------------------------------------------------------

def _trace_step(resp: dict, step: str) -> list[dict]:
    return [rec for rec in resp.get("trace", []) if rec.get("step") == step]


# The only two exception types `Gateway._exhausted` returns and
# `Gateway.run` raises `from` the failing attempt -- see `_backend_exception`.
_GATEWAY_EXHAUSTED_TYPES = (BackendUnavailable, ContextWindowExceeded)


def _backend_exception(exc: BaseException) -> BaseException | None:
    """The real backend exception behind `exc`, or `None` when `exc` makes no
    endpoint claim at all (B48c fix round 2; final review F1).

    Exactly one hop, via `__cause__` only, and only when `exc` is itself one
    of `Gateway._exhausted`'s two return types (`app/gateway/gateway.py`),
    always raised `from` the attempt that triggered exhaustion -- never
    `__context__`, and never deeper: a live backend's own internal exception
    chaining (e.g. `openai`/`litellm` wrapping) is not ours to walk, and
    doing so previously overshot the exception `counts_as_endpoint_unhealth`
    is meant to classify. Anything else (`PipelineRejected`,
    `NoEligibleCandidate`, `PolicyGateError`, ...) never reached gateway
    exhaustion and says nothing about endpoint health.

    One `__cause__` shape is a false positive rather than a deeper chain:
    `Gateway.run` also raises `_exhausted(...) from exc` when a `failed_models`
    exclusion makes `Router.select` itself raise `NoEligibleCandidate` (every
    reachable candidate already excluded) -- that cause is the router saying
    "no candidate left", not a backend's verdict on the last one it tried, so
    it is treated the same as no cause at all."""
    if (isinstance(exc, _GATEWAY_EXHAUSTED_TYPES) and exc.__cause__ is not None
            and not isinstance(exc.__cause__, NoEligibleCandidate)):
        return exc.__cause__
    return None


async def _sweep_one(pipeline: Pipeline, backend: _CountingBackend, case: Case,
                     endpoint: str, app_policy, index: int, *, keep_text: bool
                     ) -> tuple[MatrixRow, dict, str | None, int | None]:
    """One pinned call. Returns `(row, jsonl_row, served, tokens_after)` --
    `served` is `None` when the call raised (nothing was served).

    A pinned sweep call is a single-shot measurement: exactly one backend
    `generate` call. A successful call that made any other number (e.g. a
    confidence escalation re-issuing the pin) is scored as an error row
    (`GENERATE_COUNT_MISMATCH`), never graded on whichever answer came last.

    `index` is the case's 0-based position in the suite (spec §3): the
    simulated backend's outage spans are indexed by suite order, not wall
    time."""
    backend.set_case(case, index)
    calls_before = backend.calls.get(case.id, 0)
    request = ChatRequest(
        model=endpoint, messages=[Message.model_validate(m) for m in case.messages])
    served: str | None = None
    tokens_after: int | None = None
    text: str | None = None
    prompt_tokens = cached_tokens = None
    counts_unhealth: bool | None = None
    failure_latency_ms: float | None = None
    error_class: str | None = None
    try:
        resp = await pipeline.run(f"{case.id}:{endpoint}", TENANT_ID, request,
                                  app_policy=app_policy, profile=PROFILE)
    except Exception as exc:  # spec §8: an error row, never dropped
        error: str | None = f"{type(exc).__name__}: {exc}"
        backend_exc = _backend_exception(exc)
        if backend_exc is not None:
            counts_unhealth = counts_as_endpoint_unhealth(backend_exc)
            failure_latency_ms = getattr(backend_exc, "failure_latency_ms", None)
            error_class = type(backend_exc).__name__
        else:
            # `exc` never reached gateway exhaustion (spec §4 / fix round 2):
            # not an endpoint verdict, so False, not a guess.
            counts_unhealth = False
    else:
        error = None
        served = resp["model"]
        text = resp["choices"][0]["message"].get("content") or ""
        usage = resp.get("usage", {})
        prompt_tokens = usage.get("prompt_tokens")
        cached_tokens = usage.get("cached_prompt_tokens", 0)
        comp = _trace_step(resp, "compression")
        tokens_after = comp[-1]["tokens_after"] if comp else None
        if served != endpoint:
            # The pinned call must serve the pin (spec §5 step 2). Anything
            # else is scored as a failed call, never credited to `endpoint`.
            error = f"ServedMismatch: pinned {endpoint!r} but served {served!r}"
        else:
            generate_calls = backend.calls.get(case.id, 0) - calls_before
            if generate_calls != 1:
                error = (f"{GENERATE_COUNT_MISMATCH}: pinned call to {endpoint!r} "
                         f"made {generate_calls} generate calls, expected 1")

    if error is None:
        quality = grade(case.grader, text)
        cost = float(resp["cost"]["total_usd"])
        routed = _trace_step(resp, "router")
        latency = float(routed[-1]["latency_ms"]) if routed else None
    else:
        quality, cost, latency, tokens_after = 0.0, 0.0, None, None

    row = MatrixRow(case_id=case.id, endpoint=endpoint, quality=quality,
                    passed=error is None and quality >= case.quality_floor,
                    cost_usd=cost, latency_ms=latency, error=error,
                    counts_unhealth=counts_unhealth,
                    failure_latency_ms=failure_latency_ms,
                    error_class=error_class)
    line = {
        "case_id": case.id, "endpoint": endpoint, "served": served,
        "quality": row.quality, "passed": row.passed, "cost_usd": row.cost_usd,
        "latency_ms": row.latency_ms, "prompt_tokens": prompt_tokens,
        "cached_prompt_tokens": cached_tokens, "error": error,
        "error_class": row.error_class,
        "counts_unhealth": row.counts_unhealth,
        "failure_latency_ms": row.failure_latency_ms,
    }
    if keep_text:
        line["text"] = text
    return row, line, served, tokens_after


# --------------------------------------------------------------------------
# the run
# --------------------------------------------------------------------------

async def run_bench(suite_path: str, endpoints_path: str, *, out_dir: str,
                    seed: int = 0, live: bool = False, keep_text: bool = False,
                    variants_path: str | None = None, parity_tol: float = 0.02,
                    fixed: str | None = None,
                    seconds_per_case: float = DEFAULT_SECONDS_PER_CASE) -> RunResult:
    """Run the benchmark end to end. Load/usage errors print to stderr and
    return exit code 1 before `out_dir` is touched.

    `seconds_per_case` (> 0) is the synthetic case clock's spacing (B48 spec
    §6, amended): case `k` is scored at `t_k = k * seconds_per_case`."""
    args = {"suite": suite_path, "endpoints": endpoints_path, "seed": seed,
            "live": live, "keep_text": keep_text, "variants": variants_path,
            "parity_tol": parity_tol, "fixed": fixed,
            "seconds_per_case": seconds_per_case}
    started = _now()
    try:
        seconds_per_case = check_seconds_per_case(seconds_per_case)
        cases, suite_hash = load_suite(suite_path)
        base = load_settings()
        specs, endpoints_hash = load_endpoints(
            endpoints_path, live=live, tiers=base.routing.tiers)
        settings = harness_settings(base, specs)
        # The deployment's failover cap, read from `base` -- the harness
        # settings zero it so each sweep call is single-shot (ruling C).
        max_attempts = attempt_cap(base)
        # A case may apply up to one sub-step per endpoint swept (matrix rows)
        # or per attempt an online arm makes, whichever is larger -- fail fast
        # here (final review F3) rather than let `_case_time` raise mid-run.
        n_substeps = max(len(specs), max_attempts)
        floor = min_seconds_per_case(n_substeps)
        if not seconds_per_case > floor:
            raise BenchError(
                f"seconds_per_case={seconds_per_case} is too small for this run: "
                f"{n_substeps} sub-step(s) may be applied within one case "
                f"(max(len(endpoints)={len(specs)}, attempt_cap={max_attempts})), "
                f"so seconds_per_case must be > {floor}")
        variants = _load_variants(variants_path)
        slugs = {s.slug for s in specs}
        if fixed is not None and fixed not in slugs:
            raise BenchError(f"--fixed {fixed!r} is not an endpoint in {endpoints_path} "
                             f"(endpoints: {sorted(slugs)})")
        variant_cfgs: dict[str, object] = {}
        for name, weights in variants.items():
            try:  # validate every variant before any call is made
                variant_cfgs[name] = variant_settings(settings, weights)
            except Exception as exc:
                raise BenchError(f"variant {name!r}: {exc}") from exc
        try:
            detector = make_detector(settings.serving.pii_detection)
        except ImportError as exc:
            raise BenchError(
                f"pii_detection={settings.serving.pii_detection!r} is unavailable "
                f"({exc}); install it or set SP_PII_DETECTION=regex") from exc
    except (BenchError, SuiteError, EndpointFileError, OSError, ValueError) as exc:
        print(f"feasible_bench: load error: {exc}", file=sys.stderr)
        return RunResult(exit_code=1, out_dir=out_dir, metrics={})

    try:
        registry, db = await build_registry(settings, specs)
    except EndpointFileError as exc:
        print(f"feasible_bench: load error: {exc}", file=sys.stderr)
        return RunResult(exit_code=1, out_dir=out_dir, metrics={})
    try:
        try:
            return await _run_loaded(
                cases=cases, specs=specs, settings=settings, registry=registry,
                detector=detector, variant_cfgs=variant_cfgs,
                max_attempts=max_attempts, seconds_per_case=seconds_per_case,
                out_dir=out_dir, seed=seed,
                live=live, keep_text=keep_text, parity_tol=parity_tol, fixed=fixed,
                stamp={"suite_hash": suite_hash, "endpoints_hash": endpoints_hash,
                       "args": args, "started_at": started})
        except LeakageError as exc:
            # A signal arm's own leak guard fired (B48 global "no leakage"
            # constraint) -- a harness integrity bug, not a policy violation
            # (final review F2): report it the same way a load error is
            # reported, distinct wording, exit code 2 like every other
            # integrity failure (`_exit_code`).
            print(f"feasible_bench: integrity error: {exc}", file=sys.stderr)
            return RunResult(exit_code=2, out_dir=out_dir, metrics={})
    finally:
        await db.dispose()


async def _run_loaded(*, cases, specs, settings, registry, detector,
                      variant_cfgs, max_attempts, seconds_per_case,
                      out_dir, seed, live, keep_text, parity_tol, fixed,
                      stamp) -> RunResult:
    inner = make_backend(settings, registry) if live else SimulatedBackend(specs, seed)
    backend = _CountingBackend(inner)
    gate = Gate(settings, detector, registry=registry)
    # The pipeline's OWN signal store: fed by the sweep's pinned calls, never
    # handed to a strategy (every router arm builds its own).
    pipeline_signals = SignalStore(settings.routing.signals)
    gateway = Gateway(settings, backend,
                      Router(settings, registry=registry, signals=pipeline_signals),
                      registry, signals=pipeline_signals)
    co_cfg = settings.serving.context_optimization
    pipeline = Pipeline(settings, gate, detector, gateway,
                        co_cfg_provider=lambda: co_cfg,
                        ccr=CCRStore(ttl_seconds=co_cfg.ccr.ttl_hours * 3600),
                        registry=registry)

    # Every router arm is built on the HARNESS `settings` (ruling C): its
    # emptied model_pool keeps stock models out of the candidate set. The
    # attempt cap is the deployment's (`max_attempts`, from `base`).
    scored_cfgs = {None: settings, **variant_cfgs}

    def arm_name(arm: str, variant: str | None) -> str:
        return arm if variant is None else f"{arm}:{variant}"

    # Strategies whose state does not depend on the sweep so far. The cold
    # store is never fed: `Router.select` only reads signals.
    cold_store = SignalStore(settings.routing.signals)
    static: dict[str, AttemptStrategy] = {
        "cheapest": cheapest_attempts(registry, max_attempts),
        "best_published_quality": best_published_quality_attempts(
            registry, settings, max_attempts),
    }
    if fixed is not None:
        static[f"fixed:{fixed}"] = fixed_attempts(fixed, registry, max_attempts)
    for variant, cfg in scored_cfgs.items():
        static[arm_name("router_cold", variant)] = router_attempts(
            cfg, registry, cold_store, max_attempts)
    # router_online: one arm per variant, each learning only from ITS OWN
    # attempts, observed after each case in suite order.
    online = {arm_name("router_online", variant): OnlineArm(
                  cfg, registry, max_attempts, seconds_per_case=seconds_per_case)
              for variant, cfg in scored_cfgs.items()}
    # router_online:breaker_off: the base (unvaried) config's control arm --
    # the same online arm with the step-7 breaker (Task 2/3) forced off, so
    # the reported router_online arm's numbers can be read against what the
    # breaker itself is doing (B45 Task 5). Only for the base config: a
    # weight variant already has its own arm and its own breaker_off would
    # multiply the strategy count for no signal the base one doesn't already
    # give.
    if settings.routing.breaker.enabled:
        online["router_online:breaker_off"] = OnlineArm(
            settings, registry, max_attempts, seconds_per_case=seconds_per_case,
            breaker=False)
    warm_prior_names = {arm_name("router_warm_prior", variant): cfg
                        for variant, cfg in scored_cfgs.items()}
    strategy_names = list(static) + list(online) + list(warm_prior_names)
    case_index = {case.id: index for index, case in enumerate(cases)}
    output_tokens = expected_output_tokens(settings, None)

    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    for stale in ("metrics.json", "report.md", "run.json"):
        (out / stale).unlink(missing_ok=True)

    case_infos: list[CaseInfo] = []
    case_summaries: list[dict] = []
    rows: list[MatrixRow] = []
    choices: list[Choice] = []
    sweep_served: list[tuple[str, str]] = []
    pinned_mismatches = 0
    generate_mismatches = 0
    widened = 0

    with open(out / "matrix.jsonl", "w", encoding="utf-8") as matrix_f, \
            open(out / "choices.jsonl", "w", encoding="utf-8") as choices_f:
        for index, case in enumerate(cases):
            app_policy = case.policy_view()
            messages = [Message.model_validate(m) for m in case.messages]
            pre = feasible_set(settings, registry, app_policy, tenant_id=TENANT_ID)
            # The detector/rules/effective policy exactly as `Pipeline.run`
            # composes them (`_gate_inputs` is that one composition point), so
            # the post-gate context here is the one each pinned call gets.
            try:
                det, rules, eff = pipeline._gate_inputs(app_policy, PROFILE)
                ctx, reps, eff = gate.evaluate(
                    f"{case.id}:gate", TENANT_ID,
                    ChatRequest(model="auto", messages=messages),
                    app_policy=app_policy, detector=det, rules=rules, eff=eff)
            except Exception as exc:
                print(f"feasible_bench: gate failed for case {case.id!r}: "
                      f"{type(exc).__name__}: {exc}", file=sys.stderr)
                return RunResult(exit_code=1, out_dir=out_dir, metrics={})
            post = feasible_set(settings, registry, ctx, tenant_id=TENANT_ID)
            pre_names = sorted(c.name for c in pre.eligible)
            post_names = sorted(c.name for c in post.eligible)
            extra = sorted(set(post_names) - set(pre_names))
            if extra:
                widened += 1  # the gate may only tighten placement

            tokens_seen: int | None = None
            rows_before_case = len(rows)   # rows[:rows_before_case] = cases < index
            for endpoint in post_names:
                row, line, served, tokens_after = await _sweep_one(
                    pipeline, backend, case, endpoint, app_policy, index,
                    keep_text=keep_text)
                rows.append(row)
                matrix_f.write(json.dumps(line, sort_keys=True) + "\n")
                matrix_f.flush()
                if served is not None:
                    sweep_served.append((case.id, served))
                    if served != endpoint:
                        pinned_mismatches += 1
                if row.error is not None and row.error.startswith(GENERATE_COUNT_MISMATCH):
                    generate_mismatches += 1
                if tokens_seen is None and tokens_after is not None:
                    tokens_seen = tokens_after

            # An empty case is not swept, but the REAL pipeline must be seen
            # to refuse it: one unpinned probe. It must raise (fail closed,
            # CLAUDE.md rule 5) with no model call. If it is served instead,
            # the served endpoint is outside the (empty) eligible set -> a
            # violation.
            probe_error: str | None = None
            if not post_names:
                backend.set_case(case, index)
                try:
                    probe = await pipeline.run(
                        f"{case.id}:empty-probe", TENANT_ID,
                        ChatRequest(model="auto", messages=messages),
                        app_policy=app_policy, profile=PROFILE)
                except Exception as exc:
                    probe_error = type(exc).__name__
                else:
                    sweep_served.append((case.id, probe["model"]))

            calls = backend.calls.get(case.id, 0)
            info = CaseInfo(
                case_id=case.id, eligible=tuple(post_names),
                expect_empty=case.expect_empty,
                rejected_ok=(not post_names and probe_error is not None
                             and calls == 0 and len(post.rejected) >= 1),
                tags=dict(case.tags))
            case_infos.append(info)
            case_summaries.append({
                "case_id": case.id, "expect_empty": case.expect_empty,
                "eligible_pre": pre_names, "eligible_post": post_names,
                "post_gate_widened": extra,
                "rejected": sorted([r.name, r.code] for r in post.rejected),
                "rejected_ok": info.rejected_ok, "calls": calls,
                "empty_probe_error": probe_error,
            })

            if tokens_seen is None:  # nothing succeeded: count the model-facing text
                model_text = (reps.text_raw if eff.protection.model_input == "raw"
                              else (reps.text_redacted or reps.text_raw))
                tokens_seen = count_tokens(model_text)
            inp = StrategyInput(case=case, ctx=ctx, eligible=tuple(post.eligible),
                                tokens_after=tokens_seen, output_tokens=output_tokens)
            rows_by_endpoint = {r.endpoint: r for r in rows[rows_before_case:]}

            # E: `failed` reads ONLY this case's matrix rows. An attempt with
            # no row here (outside E(c)) could not have been served: it counts
            # as failed, and the chain's eligibility check flags it.
            def failed(slug: str, rows_here=rows_by_endpoint) -> bool:
                row = rows_here.get(slug)
                return row is None or row.error is not None

            chains: dict[str, tuple[str, ...]] = {
                name: strategy(inp, failed) for name, strategy in static.items()}
            for name, arm in online.items():
                chains[name] = arm.choose(inp, failed, case_index=index)
            # router_warm_prior: every row of cases < index, read at t_index.
            # Only rows swept BEFORE this case are passed; the store refuses
            # any row of this case or later (LeakageError).
            prior_store = warm_prior_signals(
                settings, rows[:rows_before_case], case_index=case_index,
                scoring_case=index, seconds_per_case=seconds_per_case)
            for name, cfg in warm_prior_names.items():
                chains[name] = router_attempts(cfg, registry, prior_store,
                                               max_attempts)(inp, failed)

            for name in strategy_names:
                attempts = tuple(chains[name])
                served = _served(attempts, rows_by_endpoint)
                choices.append(Choice(case_id=case.id, strategy=name, endpoint=served,
                                      attempts=attempts))
                choices_f.write(json.dumps(
                    _choice_record(case.id, name, attempts, served, rows_by_endpoint,
                                   fixed), sort_keys=True) + "\n")
            choices_f.flush()

            # Learn AFTER scoring: case `index` is never visible to itself. A
            # chain naming an endpoint with no row on this case is a policy
            # violation (exit 2, scored above); there is no outcome to learn
            # from it, so the arm observes the case as empty.
            for name, arm in online.items():
                chain = chains[name]
                if not all(slug in rows_by_endpoint for slug in chain):
                    chain = ()
                arm.observe(index, chain, rows_by_endpoint)

    per_strategy = {n: strategy_metrics(case_infos, rows, choices, n)
                    for n in strategy_names}
    groups = {n: {tag: grouped(case_infos, rows, choices, n, tag) for tag in GROUP_TAGS}
              for n in strategy_names}
    worst = {n: worst_groups(case_infos, rows, choices, n) for n in strategy_names}
    head = headline(per_strategy, router=HEADLINE_ROUTER, reference=HEADLINE_REFERENCE,
                    parity_tol=parity_tol)
    total_violations = violations(case_infos, choices, sweep_served)
    sweep_violations = violations(case_infos, [], sweep_served)
    empty_mismatches = sum(
        1 for c in case_infos
        if c.expect_empty != (not c.eligible) or (not c.eligible and not c.rejected_ok))

    exit_code = _exit_code(total_violations, per_strategy, empty_mismatches,
                           widened, pinned_mismatches)

    metrics = {
        "strategies": per_strategy,
        "groups": groups,
        "worst_groups": worst,
        "headline": {**head, "router": HEADLINE_ROUTER, "reference": HEADLINE_REFERENCE,
                     "parity_tol": parity_tol},
        "policy": {
            "violations_total": total_violations,
            "sweep_violations": sweep_violations,
            "empty_set_mismatches": empty_mismatches,
            "post_gate_widened": widened,
            "pinned_mismatches": pinned_mismatches,
            "sweep_generate_mismatches": generate_mismatches,
        },
        "cases": case_summaries,
        "exit_code": exit_code,
    }
    # Round-trip so `RunResult.metrics` equals what is on disk (tuples ->
    # lists), then write deterministically: sorted keys, no timings/paths.
    metrics = json.loads(_dump(metrics))
    (out / "metrics.json").write_text(_dump(metrics), encoding="utf-8")

    run_stamp = {
        **stamp,
        "finished_at": _now(),
        "base_routing_policy_version": settings.routing_policy_version,
        "registry_version": registry.registry_version(),
        "seed": seed,
        "git_sha": _git_sha(),
        "mode": "live" if live else "simulated",
        "keep_text": keep_text,
        "strategies": strategy_names,
        "endpoints": sorted(s.slug for s in specs),
        "n_cases": len(cases),
        "exit_code": exit_code,
        "seconds_per_case": seconds_per_case,
        "attempt_cap": max_attempts,
        "notes": {**ARM_NOTES, "base_routing_policy_version": BASE_RPV_NOTE},
    }
    (out / "run.json").write_text(_dump(run_stamp), encoding="utf-8")
    (out / "report.md").write_text(_report(metrics, run_stamp), encoding="utf-8")
    return RunResult(exit_code=exit_code, out_dir=str(out), metrics=metrics)


def _served(attempts: tuple[str, ...], rows_by_endpoint: dict[str, MatrixRow]
            ) -> str | None:
    """The chain's served endpoint (ruling D): its first attempt whose row on
    this case succeeded, or `None` (exhausted, no choice, or an attempt with
    no row here -- which the eligibility check already flags)."""
    for slug in attempts:
        row = rows_by_endpoint.get(slug)
        if row is None:
            return None
        if row.error is None:
            return slug
    return None


def _choice_record(case_id: str, strategy: str, attempts: tuple[str, ...],
                   served: str | None, rows_by_endpoint: dict[str, MatrixRow],
                   fixed: str | None) -> dict:
    """One `choices.jsonl` line: the attempt chain, the served endpoint and
    the chain's outcome (`oracle.attempt_outcome`). Outcome fields are
    `None` when the chain names an endpoint with no row on this case (a
    violation, scored by `strategy_metrics`)."""
    scorable = all(slug in rows_by_endpoint for slug in attempts)
    outcome = attempt_outcome(rows_by_endpoint, attempts) if scorable else None
    served_row = rows_by_endpoint.get(served) if served is not None else None
    record = {
        "case_id": case_id, "strategy": strategy, "endpoint": served,
        "attempts": list(attempts),
        "quality": outcome["quality"] if outcome else None,
        "passed": served_row.passed if served_row is not None else (
            False if outcome else None),
        "cost_usd": outcome["cost_usd"] if outcome else None,
        "latency_ms": outcome["latency_ms"] if outcome else None,
        "wasted_attempts": outcome["wasted_attempts"] if outcome else None,
        "exhausted": outcome["exhausted"] if outcome else None,
    }
    if strategy.startswith("fixed:"):
        record["fallback_used"] = not attempts or attempts[0] != fixed
    return record


def _exit_code(total_violations: int, per_strategy: dict[str, dict],
               empty_mismatches: int, widened: int, pinned_mismatches: int) -> int:
    """`2` on any integrity failure, else `0`: a policy violation (raw count
    or any strategy's rate), an empty-set mismatch, a gate that widened the
    eligible set, or a pinned call served by another endpoint (impossible by
    construction, so any occurrence is an integrity bug)."""
    failed = (
        total_violations > 0
        or any((m["violation_rate"] or 0) > 0 for m in per_strategy.values())
        or empty_mismatches > 0
        or widened > 0
        or pinned_mismatches > 0
    )
    return 2 if failed else 0


# --------------------------------------------------------------------------
# report.md
# --------------------------------------------------------------------------

def _fmt(value) -> str:
    """Render a metric for the report: `None` is "n/a", never "None"."""
    if value is None:
        return "n/a"
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, float):
        return f"{value:.6g}"
    return str(value)


def _report(metrics: dict, run: dict) -> str:
    lines: list[str] = ["# FeasibleRouteBench report", ""]
    lines += ["## Run", ""]
    for key in ("mode", "seed", "seconds_per_case", "attempt_cap", "suite_hash",
                "endpoints_hash", "base_routing_policy_version", "registry_version",
                "git_sha", "n_cases", "started_at", "finished_at"):
        lines.append(f"- **{key}**: {_fmt(run.get(key))}")
    lines.append(f"- **endpoints**: {', '.join(run['endpoints']) or 'n/a'}")
    lines.append(f"- **completion text kept (--keep-text)**: {_fmt(run['keep_text'])}")
    lines.append(f"- **exit code**: {metrics['exit_code']}")
    lines += ["", "## How to read this", ""]
    lines.append("- **B45 (is the availability weight large enough?) is read from "
                 "`router_online:<variant>`** -- never from `router_warm_prior`, "
                 "which is a full-information reference (not an upper bound), "
                 "nor `router_cold`.")
    lines += [f"- {text}" for text in ARM_NOTES.values()]
    lines.append(f"- This run used **seconds_per_case = {_fmt(run['seconds_per_case'])}**; "
                 "the online and warm-prior arms' numbers change with it and with "
                 "the case order.")
    lines.append("")

    head = metrics["headline"]
    lines += ["## Headline", ""]
    if head["comparable"]:
        lines.append(
            f"`{head['router']}` costs {_fmt(head['saving_pct'])}% less than "
            f"`{head['reference']}` at comparable pass rate "
            f"({_fmt(head['router_pass_rate'])} vs {_fmt(head['reference_pass_rate'])}, "
            f"tolerance {_fmt(head['parity_tol'])}).")
    else:
        lines.append(
            f"Not comparable -- no cost claim is made. `{head['router']}` pass rate "
            f"{_fmt(head['router_pass_rate'])} vs `{head['reference']}` "
            f"{_fmt(head['reference_pass_rate'])} (parity tolerance "
            f"{_fmt(head['parity_tol'])}); a claim also needs a defined, non-zero "
            "reference cost.")
    lines.append("")

    cols = ("violation_rate", "pass_rate", "quality_regret", "cost_regret",
            "latency_regret", "floor_miss_rate", "mean_chosen_cost",
            "wasted_attempts", "exhausted_rate")
    lines += ["## Strategies", "",
              "| strategy | " + " | ".join(cols) + " |",
              "|---|" + "---|" * len(cols)]
    for name, m in metrics["strategies"].items():
        lines.append(f"| {name} | " + " | ".join(_fmt(m[c]) for c in cols) + " |")
    lines.append("")

    lines += ["## Worst groups", "", "| strategy | metric | tag | value | metric value |",
              "|---|---|---|---|---|"]
    for name, per_metric in metrics["worst_groups"].items():
        for metric, worst in per_metric.items():
            tag, value, metric_value = worst if worst is not None else (None, None, None)
            lines.append(f"| {name} | {metric} | {_fmt(tag)} | {_fmt(value)} | "
                         f"{_fmt(metric_value)} |")
    lines.append("")

    policy = metrics["policy"]
    lines += ["## Policy", "",
              "`violations_total` counts attempts; `violation_rate` (above) is per case.",
              ""]
    for key in ("violations_total", "sweep_violations", "empty_set_mismatches",
                "post_gate_widened", "pinned_mismatches", "sweep_generate_mismatches"):
        lines.append(f"- **{key}**: {_fmt(policy[key])}")
    lines.append("")

    lines += ["## Empty-set cases", "",
              "| case | expect_empty | eligible | rejected_ok | calls | exclusions |",
              "|---|---|---|---|---|---|"]
    empty_cases = [c for c in metrics["cases"]
                   if c["expect_empty"] or not c["eligible_post"]]
    if not empty_cases:
        lines.append("| n/a | n/a | n/a | n/a | n/a | no expected or actual empty-set case |")
    for c in empty_cases:
        reasons = ", ".join(f"{n} ({code})" for n, code in c["rejected"]) or "n/a"
        lines.append(f"| {c['case_id']} | {_fmt(c['expect_empty'])} | "
                     f"{len(c['eligible_post'])} | {_fmt(c['rejected_ok'])} | "
                     f"{c['calls']} | {reasons} |")
    lines.append("")

    lines += ["## Deferred metrics", ""]
    lines += [f"- {item}" for item in DEFERRED_METRICS]
    lines.append("")
    return "\n".join(lines)
