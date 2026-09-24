"""Policy simulator replay engine (P3a, spec §7): offline, operator-run.

Replays each recorded P2b-2 outcome row's **FIRST routing decision only**
through the real serving code -- `build_context` -> `feasible_set` ->
`Router.select` -- under **both** arms:

- `current`  = the config snapshot as exported;
- `proposed` = the snapshot with a policy proposal applied.

Both arms are simulated, so the headline diff is always simulated-vs-simulated
and drift since the export window affects both sides equally. Nothing here
touches the network or a deployment database: each scenario owns a scratch
in-memory SQLite registry (`app/eval/scenario.py`) and a frozen signal
reading shared by both arms.

Deliberate non-goals (spec §2):
- no live dry-run of an admin write;
- no explanation of why a proposal rejects (P3b), no shadow prices or
  uncertainty ranges (P3c);
- no re-running PII detection -- recorded entity TYPES are replayed as-is;
- no daily-spend / RPM admission simulation;
- no circuit-breaker state (both arms run with no open circuits);
- no confidence-escalation or failover replay -- only the first decision.

Only `NoEligibleCandidate` and `ContextWindowExceeded` are recorded as
rejections. Any other exception propagates: a simulator bug must surface,
never be recorded as a rejected request.
"""
from __future__ import annotations

import statistics
from dataclasses import dataclass

from app.config.schema import Settings
from app.errors import ContextWindowExceeded, NoEligibleCandidate
from app.eval.proposal import apply_proposal
from app.eval.scenario import Scenario, build_scenario, close_scenario
from app.models import Entity
from app.policy.context import build_context
from app.policy.feasible import feasible_set
from app.routing.pricing import price_call

SIMULATED_STATUSES = frozenset({"ok", "backend_failed", "verify_failed",
                                "no_eligible", "context_overflow"})

EXCLUSION_REASONS = ("cache_hit", "unknown_status", "unknown_app", "missing_features")


@dataclass(frozen=True)
class ArmResult:
    outcome: str                  # "routed" | "rejected"
    model: str | None
    provider: str | None
    tier: str | None
    eligible: tuple[str, ...]     # policy-eligible names (feasible_set), sorted
    reject_codes: tuple[str, ...] # sorted, empty when routed
    cost_usd: float | None
    latency_ms: float | None
    latency_source: str | None    # "observed" | "signal" | "tier_default"
    quality: float | None
    quality_source: str | None


@dataclass(frozen=True)
class RowResult:
    row: dict
    current: ArmResult
    proposed: ArmResult


def select_rows(records: list[dict], app_ids: set[str]) -> tuple[list[dict], dict[str, int]]:
    """The rows to simulate, sorted by `(ts, request_id)`, plus a count per
    exclusion reason. Every excluded row is counted under exactly one reason
    (first match, in `EXCLUSION_REASONS` order) -- never silently dropped."""
    excluded = {reason: 0 for reason in EXCLUSION_REASONS}
    kept: list[dict] = []
    for row in records:
        if row.get("cache_hit"):
            excluded["cache_hit"] += 1
        elif row.get("status") not in SIMULATED_STATUSES:
            excluded["unknown_status"] += 1
        elif row.get("application_id") not in app_ids:
            excluded["unknown_app"] += 1
        elif row.get("complexity") is None or row.get("prompt_tokens") is None:
            excluded["missing_features"] += 1
        else:
            kept.append(row)
    kept.sort(key=lambda r: (r.get("ts") or "", r["request_id"]))
    return kept, excluded


def _is_number(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def observed_latencies(records: list[dict]) -> dict[str, float]:
    """Median observed `latency_ms` per model, over `ok` rows the model served
    on a single, first attempt -- the only rows whose latency is that one
    model's alone (a failover row's latency includes the failed attempts)."""
    samples: dict[str, list[float]] = {}
    for row in records:
        attempts = row.get("attempts") or []
        if row.get("status") != "ok" or len(attempts) != 1:
            continue
        if row.get("chosen") != attempts[0] or not _is_number(row.get("latency_ms")):
            continue
        samples.setdefault(attempts[0], []).append(float(row["latency_ms"]))
    return {model: float(statistics.median(vals)) for model, vals in samples.items()}


def _rejected(eligible: tuple[str, ...], codes: tuple[str, ...]) -> ArmResult:
    return ArmResult("rejected", None, None, None, eligible, codes,
                     None, None, None, None, None)


def simulate_row(scn: Scenario, row: dict, observed: dict[str, float]) -> ArmResult:
    """One row's first routing decision under one scenario."""
    app = scn.apps[row["application_id"]]
    # Placeholder entities: only `.type` is read on the context path.
    entities = [Entity(type=t, start=0, end=0, value="")
                for t in row["detected_entity_types"] or []]
    ctx = build_context(
        scn.settings, row["request_id"], row["application_id"], entities, None,
        app_policy=app.view, app_rules=app.rules, registry=scn.registry,
        escalate_residency=app.eff.escalate_residency_on_detection,
        content_integrity=app.eff.protection.content_integrity,
        model_input=app.eff.protection.model_input,
        workload=row.get("workload"))
    eligible = tuple(sorted(
        c.name for c in feasible_set(scn.settings, scn.registry, ctx,
                                     tenant_id=row["application_id"]).eligible))
    prompt_tokens = int(row["prompt_tokens"])
    try:
        decision = scn.router.select(ctx, tokens_after=prompt_tokens,
                                     complexity=float(row["complexity"]))
    except NoEligibleCandidate as exc:
        if not exc.rejected:
            return _rejected(eligible, ("empty_pool",))
        fallback = "over_budget" if "budget filter" in str(exc) else "unspecified"
        codes = tuple(sorted({code or fallback for _name, code in exc.rejected}))
        return _rejected(eligible, codes)
    except ContextWindowExceeded:
        return _rejected(eligible, ("context_window",))

    pick = decision.chosen
    cost = price_call(scn.registry.price_for(pick.name),
                      prompt_tokens=prompt_tokens,
                      completion_tokens=int(row.get("completion_tokens") or 0),
                      cached_prompt_tokens=0)
    if pick.name in observed:
        latency, latency_source = observed[pick.name], "observed"
    else:
        snap = scn.signals.snapshot_latency(pick.name)
        if snap is not None:
            latency, latency_source = snap, "signal"
        else:
            latency = scn.signals.latency(pick.name, pick.tier).value
            latency_source = "tier_default"
    q = scn.quality.resolve(pick.name, row.get("workload"))
    return ArmResult("routed", pick.name, pick.provider, pick.tier, eligible, (),
                     cost, latency, latency_source, q.score, q.source)


def run_rows(records: list[dict], current: Scenario,
             proposed: Scenario) -> tuple[list[RowResult], dict[str, int]]:
    """Simulate every selectable row under both scenarios. Observed latencies
    come from ALL records (not only the selected ones) and are shared by both
    arms; only apps present in both scenarios are simulated."""
    observed = observed_latencies(records)
    app_ids = set(current.apps) & set(proposed.apps)
    rows, excluded = select_rows(records, app_ids)
    results = [RowResult(row, simulate_row(current, row, observed),
                         simulate_row(proposed, row, observed))
               for row in rows]
    return results, excluded


async def simulate(records: list[dict], snapshot: dict, proposal: dict, *,
                   base: Settings) -> tuple[list[RowResult], dict[str, int]]:
    """Build both scenarios from one base `Settings`, replay, close both."""
    proposed_doc = apply_proposal(snapshot, proposal, base=base)
    current = await build_scenario(snapshot, base=base)
    try:
        proposed = await build_scenario(proposed_doc, base=base)
        try:
            return run_rows(records, current, proposed)
        finally:
            await close_scenario(proposed)
    finally:
        await close_scenario(current)
