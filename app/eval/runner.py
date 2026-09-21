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
3. scores every strategy by looking its choice up in that matrix
   (`choices.jsonl`) -- no strategy makes a model call.

After the last case it computes the feasible-oracle metrics (`metrics.json`),
a human report (`report.md`) and a run stamp (`run.json`). Exit codes: `0` ok,
`1` load/usage error, `2` policy violation, empty-set mismatch, post-gate
widening or pinned-served mismatch (`_exit_code`).

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
    grouped,
    headline,
    strategy_metrics,
    violations,
    worst_groups,
)
from app.eval.simulated import SimulatedBackend
from app.eval.strategies import (
    Strategy,
    StrategyInput,
    best_published_quality,
    cheapest,
    fixed as fixed_strategy,
    router,
    router_variant,
    warm_signals,
)
from app.gateway.backends.base import count_tokens
from app.gateway.backends.factory import make_backend
from app.gateway.gateway import Gateway
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
HEADLINE_ROUTER = "router_warm"
HEADLINE_REFERENCE = "best_published_quality"

WARM_NOTE = (
    "router_warm replays observations from the same sweep it is scored on -- "
    "every row swept so far, up to and including the case being scored -- so "
    "it is optimistic by construction: it has seen this case's own outcomes.")

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

    def set_case(self, case: Case) -> None:
        self._case_id = case.id
        if hasattr(self._inner, "set_case"):
            self._inner.set_case(case)

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
    `strategies.router_variant` (which runs the real `RoutingCfg`
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


async def _sweep_one(pipeline: Pipeline, backend: _CountingBackend, case: Case,
                     endpoint: str, app_policy, *, keep_text: bool
                     ) -> tuple[MatrixRow, dict, str | None, int | None]:
    """One pinned call. Returns `(row, jsonl_row, served, tokens_after)` --
    `served` is `None` when the call raised (nothing was served).

    A pinned sweep call is a single-shot measurement: exactly one backend
    `generate` call. A successful call that made any other number (e.g. a
    confidence escalation re-issuing the pin) is scored as an error row
    (`GENERATE_COUNT_MISMATCH`), never graded on whichever answer came last."""
    backend.set_case(case)
    calls_before = backend.calls.get(case.id, 0)
    request = ChatRequest(
        model=endpoint, messages=[Message.model_validate(m) for m in case.messages])
    served: str | None = None
    tokens_after: int | None = None
    text: str | None = None
    prompt_tokens = cached_tokens = None
    try:
        resp = await pipeline.run(f"{case.id}:{endpoint}", TENANT_ID, request,
                                  app_policy=app_policy, profile=PROFILE)
    except Exception as exc:  # spec §8: an error row, never dropped
        error: str | None = f"{type(exc).__name__}: {exc}"
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
                    cost_usd=cost, latency_ms=latency, error=error)
    line = {
        "case_id": case.id, "endpoint": endpoint, "served": served,
        "quality": row.quality, "passed": row.passed, "cost_usd": row.cost_usd,
        "latency_ms": row.latency_ms, "prompt_tokens": prompt_tokens,
        "cached_prompt_tokens": cached_tokens, "error": error,
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
                    fixed: str | None = None) -> RunResult:
    """Run the benchmark end to end. Load/usage errors print to stderr and
    return exit code 1 before `out_dir` is touched."""
    args = {"suite": suite_path, "endpoints": endpoints_path, "seed": seed,
            "live": live, "keep_text": keep_text, "variants": variants_path,
            "parity_tol": parity_tol, "fixed": fixed}
    started = _now()
    try:
        cases, suite_hash = load_suite(suite_path)
        base = load_settings()
        specs, endpoints_hash = load_endpoints(
            endpoints_path, live=live, tiers=base.routing.tiers)
        settings = harness_settings(base, specs)
        variants = _load_variants(variants_path)
        slugs = {s.slug for s in specs}
        if fixed is not None and fixed not in slugs:
            raise BenchError(f"--fixed {fixed!r} is not an endpoint in {endpoints_path} "
                             f"(endpoints: {sorted(slugs)})")
        for name, weights in variants.items():
            try:  # validate every variant before any call is made
                router_variant(settings, weights, None,
                               SignalStore(settings.routing.signals))
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
        return await _run_loaded(
            cases=cases, specs=specs, settings=settings, registry=registry,
            detector=detector, variants=variants, out_dir=out_dir, seed=seed,
            live=live, keep_text=keep_text, parity_tol=parity_tol, fixed=fixed,
            stamp={"suite_hash": suite_hash, "endpoints_hash": endpoints_hash,
                   "args": args, "started_at": started})
    finally:
        await db.dispose()


async def _run_loaded(*, cases, specs, settings, registry, detector, variants,
                      out_dir, seed, live, keep_text, parity_tol, fixed,
                      stamp) -> RunResult:
    inner = make_backend(settings, registry) if live else SimulatedBackend(specs, seed)
    backend = _CountingBackend(inner)
    gate = Gate(settings, detector, registry=registry)
    # The pipeline's OWN signal store: fed by the sweep's pinned calls, never
    # handed to a strategy (router_cold/router_warm build their own).
    pipeline_signals = SignalStore(settings.routing.signals)
    gateway = Gateway(settings, backend,
                      Router(settings, registry=registry, signals=pipeline_signals),
                      registry, signals=pipeline_signals)
    co_cfg = settings.serving.context_optimization
    pipeline = Pipeline(settings, gate, detector, gateway,
                        co_cfg_provider=lambda: co_cfg,
                        ccr=CCRStore(ttl_seconds=co_cfg.ccr.ttl_hours * 3600),
                        registry=registry)

    # Strategies whose state does not depend on the sweep so far. The cold
    # store is never fed: `Router.select` only reads signals.
    cold_store = SignalStore(settings.routing.signals)
    static: dict[str, Strategy] = {
        "cheapest": cheapest(registry),
        "best_published_quality": best_published_quality(registry, settings),
    }
    if fixed is not None:
        static[f"fixed:{fixed}"] = fixed_strategy(fixed, cheapest(registry))
    static["router_cold"] = router(settings, registry, cold_store)
    for name, weights in variants.items():
        static[f"router_cold:{name}"] = router_variant(settings, weights, registry,
                                                       cold_store)
    strategy_names = list(static) + ["router_warm"] + [
        f"router_warm:{name}" for name in variants]
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
        for case in cases:
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
            for endpoint in post_names:
                row, line, served, tokens_after = await _sweep_one(
                    pipeline, backend, case, endpoint, app_policy, keep_text=keep_text)
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
                backend.set_case(case)
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

            # router_warm: a store replaying every row swept SO FAR, including
            # this case's own rows (WARM_NOTE), built immediately before use.
            warm_store = warm_signals(settings, rows)
            strategies = dict(static)
            strategies["router_warm"] = router(settings, registry, warm_store)
            for name, weights in variants.items():
                strategies[f"router_warm:{name}"] = router_variant(
                    settings, weights, registry, warm_store)

            row_by_endpoint = {r.endpoint: r for r in rows if r.case_id == case.id}
            for name in strategy_names:
                slug = strategies[name](inp)
                choices.append(Choice(case_id=case.id, strategy=name, endpoint=slug))
                chosen = row_by_endpoint.get(slug) if slug is not None else None
                record = {
                    "case_id": case.id, "strategy": name, "endpoint": slug,
                    "quality": chosen.quality if chosen else None,
                    "passed": chosen.passed if chosen else None,
                    "cost_usd": chosen.cost_usd if chosen else None,
                }
                if name.startswith("fixed:"):
                    record["fallback_used"] = slug != fixed
                choices_f.write(json.dumps(record, sort_keys=True) + "\n")
            choices_f.flush()

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
        "routing_policy_version": settings.routing_policy_version,
        "registry_version": registry.registry_version(),
        "seed": seed,
        "git_sha": _git_sha(),
        "mode": "live" if live else "simulated",
        "keep_text": keep_text,
        "strategies": strategy_names,
        "endpoints": sorted(s.slug for s in specs),
        "n_cases": len(cases),
        "exit_code": exit_code,
        "notes": {"router_warm": WARM_NOTE},
    }
    (out / "run.json").write_text(_dump(run_stamp), encoding="utf-8")
    (out / "report.md").write_text(_report(metrics, run_stamp), encoding="utf-8")
    return RunResult(exit_code=exit_code, out_dir=str(out), metrics=metrics)


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
    for key in ("mode", "seed", "suite_hash", "endpoints_hash", "routing_policy_version",
                "registry_version", "git_sha", "n_cases", "started_at", "finished_at"):
        lines.append(f"- **{key}**: {_fmt(run.get(key))}")
    lines.append(f"- **endpoints**: {', '.join(run['endpoints']) or 'n/a'}")
    lines.append(f"- **completion text kept (--keep-text)**: {_fmt(run['keep_text'])}")
    lines.append(f"- **exit code**: {metrics['exit_code']}")
    lines += ["", f"> {WARM_NOTE}", ""]

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
            "latency_regret", "floor_miss_rate", "mean_chosen_cost")
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
    lines += ["## Policy", ""]
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
