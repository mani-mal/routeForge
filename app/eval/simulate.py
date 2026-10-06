"""Policy simulator replay engine (P3a, spec §7): offline, operator-run.

Replays each recorded P2b-2 outcome row's **first routing decision** through
the real serving code -- `build_context` -> `feasible_set` ->
`Router.select` -- plus the gateway's deterministic context-window overflow
re-selection (P3c-2), under **both** arms:

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
- no confidence-escalation or failover replay -- only the first decision,
  and the overflow re-selection that is a pure function of declared data.

Only `NoEligibleCandidate` and `ContextWindowExceeded` are recorded as
rejections. Any other exception propagates: a simulator bug must surface,
never be recorded as a rejected request.
"""
from __future__ import annotations

import hashlib
import json
import statistics
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone

from app.config.schema import Settings
from app.errors import ContextWindowExceeded, NoEligibleCandidate
from app.eval.outcomes import UNTAGGED_WORKLOAD, _fmt, _mean, _nearest_rank, _round6
from app.eval.proposal import apply_proposal
from app.eval.scenario import Scenario, build_scenario, close_scenario
from app.models import Entity
from app.policy.context import build_context
from app.policy.feasible import feasible_set
from app.routing.pricing import price_call
from app.routing.router import expected_output_tokens

SIMULATED_STATUSES = frozenset({"ok", "backend_failed", "verify_failed",
                                "no_eligible", "context_overflow"})

# `before_since` is checked first, so a row older than `--since` never reaches
# the other checks (P3c-2 spec §6).
EXCLUSION_REASONS = ("before_since", "cache_hit", "unknown_status", "unknown_app",
                     "missing_features")


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
    latency_source: str | None    # "proposal" | "observed" | "signal" | "tier_default"
    quality: float | None
    quality_source: str | None
    core: tuple[str, ...] = ()       # P3b: NoEligibleCandidate.core.ids(), () when routed
    core_state: tuple[str, ...] = () # P3b: NoEligibleCandidate.core.state_ids(), () when routed
    # P3c-2: True when `workload.token_scale` shrank a row whose recorded
    # complexity was saturated, so its new complexity is an estimate.
    complexity_estimated: bool = False
    # P3c-2 spec §5: every model this arm selected, in order -- the picks
    # that overflowed their declared context window, then (when routed) the
    # one served. `attempted[0]` is the first routing decision.
    attempted: tuple[str, ...] = ()

    @property
    def overflow_hops(self) -> int:
        """Picks excluded because prompt + output exceeded their window."""
        served = 1 if self.outcome == "routed" else 0
        return max(0, len(self.attempted) - served)


@dataclass(frozen=True)
class RowResult:
    row: dict
    current: ArmResult
    proposed: ArmResult


def _is_str_list(value) -> bool:
    return isinstance(value, list) and all(isinstance(v, str) for v in value)


def _valid_ts(ts) -> bool:
    if ts is None:
        return True
    if not isinstance(ts, str):
        return False
    try:
        _parse_ts(ts)
    except ValueError:
        return False
    return True


def _has_features(row: dict) -> bool:
    """Every field replay and the summary read is present and well-typed:
    a malformed export row is counted under `missing_features`, never a
    `KeyError`/`ValueError` traceback (spec §8: bad input is exit 2 or a
    counted exclusion, not a crash)."""
    return (isinstance(row.get("request_id"), str) and bool(row["request_id"])
            and _is_number(row.get("complexity"))
            and _is_number(row.get("prompt_tokens"))
            and (row.get("completion_tokens") is None or _is_number(row["completion_tokens"]))
            and (row.get("detected_entity_types", ...) is None
                 or _is_str_list(row.get("detected_entity_types")))
            and (row.get("workload") is None or isinstance(row["workload"], str))
            and (row.get("attempts") is None or _is_str_list(row["attempts"]))
            # B55: absent (an export from before the columns existed) or null
            # means unpinned / no client cap.
            and (row.get("requested_model") is None
                 or (isinstance(row["requested_model"], str) and bool(row["requested_model"])))
            and (row.get("max_tokens") is None
                 or (isinstance(row["max_tokens"], int)
                     and not isinstance(row["max_tokens"], bool)
                     and row["max_tokens"] > 0))
            and _valid_ts(row.get("ts")))


def parse_since(text: str) -> datetime:
    """A `--since` value as an aware UTC datetime. Accepts what
    `datetime.fromisoformat` accepts plus a trailing `Z`; a value with no
    offset is read as UTC. Raises `ValueError` on anything else."""
    return _utc(_parse_ts(text))


def _utc(ts: datetime) -> datetime:
    """`ts` as an aware UTC datetime; a naive value is read as UTC."""
    if ts.tzinfo is None:
        return ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(timezone.utc)


def _before_since(row: dict, since: datetime | None) -> bool:
    """True when `row` carries a parseable `ts` strictly before `since`. A
    row with no usable `ts` is never `before_since` -- `select_rows` counts
    it under `missing_features` instead when a cut was asked for."""
    ts = row.get("ts")
    if since is None or not isinstance(ts, str):
        return False
    try:
        return _utc(_parse_ts(ts)) < since
    except ValueError:
        return False


def select_rows(records: list[dict], app_ids: set[str], *,
                since: datetime | None = None) -> tuple[list[dict], dict[str, int]]:
    """The rows to simulate, sorted by `(ts, request_id)`, plus a count per
    exclusion reason. Every excluded row is counted under exactly one reason
    (first match, in `EXCLUSION_REASONS` order) -- never silently dropped.
    `before_since` (P3c-2) is a row whose `ts` is strictly before `since`;
    with a `since`, a row that has no `ts` cannot be placed in the window and
    is counted under `missing_features`.
    `missing_features` covers a missing or mistyped `request_id`,
    `complexity`, `prompt_tokens`, `completion_tokens`, `workload`,
    `attempts`, `ts`, or `detected_entity_types` (list of strings, or null
    -- but the key must be present), and a mistyped `requested_model`
    (non-empty string or null) or `max_tokens` (int > 0 or null). Those two
    (B55) may be ABSENT: an export from before they were recorded replays
    unpinned at the default output estimate."""
    excluded = {reason: 0 for reason in EXCLUSION_REASONS}
    kept: list[dict] = []
    for row in records:
        status = row.get("status")
        app_id = row.get("application_id")
        if _before_since(row, since):
            excluded["before_since"] += 1
        elif row.get("cache_hit"):
            excluded["cache_hit"] += 1
        elif not isinstance(status, str) or status not in SIMULATED_STATUSES:
            excluded["unknown_status"] += 1
        elif not isinstance(app_id, str) or app_id not in app_ids:
            excluded["unknown_app"] += 1
        elif not _has_features(row) or (since is not None and row.get("ts") is None):
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
        if row.get("status") != "ok" or not _is_str_list(attempts) or len(attempts) != 1:
            continue
        if row.get("chosen") != attempts[0] or not _is_number(row.get("latency_ms")):
            continue
        samples.setdefault(attempts[0], []).append(float(row["latency_ms"]))
    return {model: float(statistics.median(vals)) for model, vals in samples.items()}


def _rejected(eligible: tuple[str, ...], codes: tuple[str, ...], *,
              core: tuple[str, ...] = (), core_state: tuple[str, ...] = (),
              complexity_estimated: bool = False,
              attempted: tuple[str, ...] = ()) -> ArmResult:
    return ArmResult("rejected", None, None, None, eligible, codes,
                     None, None, None, None, None, core, core_state,
                     complexity_estimated=complexity_estimated, attempted=attempted)


# app/pipeline.py: `complexity = min(1.0, comp.tokens_after / 2000)`. The
# literal has no name there to import; tests/eval/test_proposal_workload.py
# pins it against the pipeline source so the two cannot drift apart.
_COMPLEXITY_SATURATION_TOKENS = 2000


def scaled_request(prompt_tokens: int, complexity: float,
                   scale: float) -> tuple[int, float, bool]:
    """`(prompt_tokens', complexity', estimated)` for one row under
    `workload.token_scale` (P3c-2 spec §4.1).

    - `scale == 1.0` is the identity, exactly.
    - Tokens: `max(1, round(prompt_tokens * scale))`. A row that recorded no
      prompt tokens (every failure row records 0) stays at 0: its size is
      unknown, and flooring it to 1 would make cost fall as the scale rises.
    - Complexity below 1 is linear in the token count, so `complexity *
      scale` (capped at 1) is exact.
    - A saturated complexity (>= 1) hides the real token count, which was at
      least the saturation point. So the scaled complexity is at least
      `scale`, and otherwise estimated from the scaled prompt tokens.
      Scaling UP therefore keeps it at exactly 1 (known, not an estimate);
      scaling DOWN is an estimate, and `estimated` says so."""
    if scale == 1.0:
        return prompt_tokens, complexity, False
    scaled = max(1, round(prompt_tokens * scale)) if prompt_tokens > 0 else 0
    if complexity < 1.0:
        return scaled, min(1.0, complexity * scale), False
    estimate = max(scale, scaled / _COMPLEXITY_SATURATION_TOKENS)
    return scaled, min(1.0, estimate), scale < 1.0


def simulate_row(scn: Scenario, row: dict, observed: dict[str, float]) -> ArmResult:
    """One row's routing decision under one scenario: the first decision,
    then -- only when the pick's declared context window is too small -- the
    same exclude-and-reselect the live gateway does (P3c-2 spec §5)."""
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
    # B55: re-apply the client's recorded pin exactly as the gate does
    # (app/policy/gate.py: `model: "auto"` -> None). A null or absent pin
    # replays unpinned. feasible_set and the router then admit only the
    # pinned model, so a proposal that drops it rejects the row.
    ctx = ctx.model_copy(update={"requested_model": row.get("requested_model")})
    eligible = tuple(sorted(
        c.name for c in feasible_set(scn.settings, scn.registry, ctx,
                                     tenant_id=row["application_id"]).eligible))
    # P3c-2: the scenario's workload assumptions, applied before routing and
    # pricing. A scenario built from a plain snapshot has none (scale 1.0,
    # share 0.0), so the `current` arm replays the row as recorded.
    prompt_tokens, complexity, estimated = scaled_request(
        int(row["prompt_tokens"]), float(row["complexity"]),
        scn.assumptions.scale_for(row["application_id"]))
    # B55: price at the client's recorded output cap, as the live gateway
    # does (`expected_output_tokens(settings, params)`); no cap recorded ->
    # the deployment's default estimate.
    output_tokens = expected_output_tokens(
        scn.settings, {"max_tokens": row["max_tokens"]} if row.get("max_tokens") else None)

    # P3c-2 spec §5: mirror `Gateway.run`'s overflow handling. A pick whose
    # DECLARED window cannot hold prompt + output is excluded and the router
    # re-selects, at most `max_failover_attempts` extra times -- the live
    # bound. An undeclared window (None) never overflows: replay cannot
    # invent a limit. This is the only later-hop behaviour replayed.
    max_failover = int(scn.settings.routing.max_failover_attempts)
    overflowed: list[str] = []          # picks that overflowed, in order
    while True:
        try:
            decision = scn.router.select(ctx, tokens_after=prompt_tokens,
                                         complexity=complexity,
                                         output_tokens=output_tokens,
                                         exclude=frozenset(overflowed))
        except NoEligibleCandidate as exc:
            if overflowed:
                # Every reachable candidate overflowed. The live gateway
                # reports that as ContextWindowExceeded (400), not a 422.
                return _rejected(eligible, ("context_window",),
                                 complexity_estimated=estimated,
                                 attempted=tuple(overflowed))
            # P3b: the router attaches the minimal conflicting core to every
            # NoEligibleCandidate it raises (None only if computing the core
            # itself failed) -- carry it through so the summary can group
            # rejections by why, not just by code.
            core = exc.core.ids() if exc.core else ()
            core_state = exc.core.state_ids() if exc.core else ()
            if not exc.rejected:
                return _rejected(eligible, ("empty_pool",), core=core, core_state=core_state,
                                 complexity_estimated=estimated)
            fallback = "over_budget" if "budget filter" in str(exc) else "unspecified"
            codes = tuple(sorted({code or fallback for _name, code in exc.rejected}))
            return _rejected(eligible, codes, core=core, core_state=core_state,
                            complexity_estimated=estimated)
        except ContextWindowExceeded:
            return _rejected(eligible, ("context_window",), complexity_estimated=estimated,
                             attempted=tuple(overflowed))
        pick = decision.chosen
        if pick.context_window is None or prompt_tokens + output_tokens <= pick.context_window:
            break
        overflowed.append(pick.name)
        if len(overflowed) > max_failover:
            return _rejected(eligible, ("context_window",), complexity_estimated=estimated,
                             attempted=tuple(overflowed))

    # P3c-2: `workload.cached_share` of the (scaled) prompt is billed at the
    # endpoint's cached-input price. The ROUTER still estimates at zero
    # cached tokens, as it does live, so the share moves cost and never a
    # route. `price_call` bills cached tokens at the input price when the
    # endpoint declares no cached price.
    cached_tokens = round(prompt_tokens * scn.assumptions.cached_for(row["application_id"]))
    cost = price_call(scn.registry.price_for(pick.name),
                      prompt_tokens=prompt_tokens,
                      completion_tokens=int(row.get("completion_tokens") or 0),
                      cached_prompt_tokens=cached_tokens)
    asserted = scn.signals.proposed_latency(pick.name)
    if asserted is not None:
        # P3c-2 spec §4.2: the proposal's asserted latency wins every rung.
        latency, latency_source = asserted, "proposal"
    elif pick.name in observed:
        latency, latency_source = observed[pick.name], "observed"
    else:
        snap = scn.signals.snapshot_latency(pick.name)
        if snap is not None:
            latency, latency_source = snap, "signal"
        else:
            latency = scn.signals.latency(pick.name, pick.tier).value
            latency_source = "tier_default"
    q = scn.quality.resolve(pick.name, row.get("workload"))
    # The endpoint served AFTER any overflow hops is the one costed.
    return ArmResult("routed", pick.name, pick.provider, pick.tier, eligible, (),
                     cost, latency, latency_source, q.score, q.source,
                     complexity_estimated=estimated,
                     attempted=(*overflowed, pick.name))


def run_rows(records: list[dict], current: Scenario, proposed: Scenario, *,
             since: datetime | None = None) -> tuple[list[RowResult], dict[str, int]]:
    """Simulate every selectable row under both scenarios. Observed latencies
    come from every record in the window (not only the selected ones) and
    are shared by both arms; only apps present in both scenarios are
    simulated. `since` cuts the export first: a row before it is counted
    `before_since` and feeds nothing, so the result equals a run over an
    export that never held those rows."""
    observed = observed_latencies([r for r in records if not _before_since(r, since)])
    app_ids = set(current.apps) & set(proposed.apps)
    rows, excluded = select_rows(records, app_ids, since=since)
    results = [RowResult(row, simulate_row(current, row, observed),
                         simulate_row(proposed, row, observed))
               for row in rows]
    return results, excluded


async def simulate(records: list[dict], snapshot: dict, proposal: dict, *,
                   base: Settings, since: datetime | None = None
                   ) -> tuple[list[RowResult], dict[str, int]]:
    """Build both scenarios from one base `Settings`, replay, close both."""
    proposed_doc = apply_proposal(snapshot, proposal, base=base)
    current = await build_scenario(snapshot, base=base)
    try:
        proposed = await build_scenario(proposed_doc, base=base)
        try:
            return run_rows(records, current, proposed, since=since)
        finally:
            await close_scenario(proposed)
    finally:
        await close_scenario(current)


async def baseline_check(records: list[dict], snapshot: dict, *, base: Settings,
                         since: datetime | None = None) -> dict:
    """The fidelity block (`_baseline`) for the snapshot AS EXPORTED, with no
    proposal: build the one scenario, replay each selectable row once, close.
    For a caller that has no `summarize()` output to read the block from
    (`--shadow-prices` without `--proposal`) but still has to gate on
    agreement (P3c-2 spec §6, `--min-agreement`)."""
    current = await build_scenario(snapshot, base=base)
    try:
        observed = observed_latencies([r for r in records if not _before_since(r, since)])
        rows, _excluded = select_rows(records, set(current.apps), since=since)
        arms = [simulate_row(current, row, observed) for row in rows]
    finally:
        await close_scenario(current)
    return _round6(_baseline([RowResult(row, arm, arm) for row, arm in zip(rows, arms)]))


def agreement_ok(baseline: dict, minimum: float) -> bool:
    """The `--min-agreement` gate: False when the baseline has no comparable
    row (`agreement` is None -- nothing vouches for the replay) or agrees
    with the record less often than `minimum`."""
    agreement = baseline["agreement"]
    return agreement is not None and agreement >= minimum


# --- summary and report (spec §8) -------------------------------------------

#: The report's opening blockquote, verbatim from spec §8, one string per
#: source line (without the leading "> " -- `render_report` adds that on
#: every line so a multi-line quote stays a single blockquote).
REPORT_CAVEAT = (
    "**Simulation only.** Nothing in this report has been applied. It predicts the first routing\n"
    "decision of recorded requests under the proposal, plus deterministic context-window overflow\n"
    "re-selection, using the real eligibility and routing code and a frozen snapshot of signals.\n"
    "It does not model other failover, escalation, breaker state or caching."
)


def _parse_ts(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def _route_changed(r: RowResult) -> bool:
    return (r.current.outcome == "routed" and r.proposed.outcome == "routed"
            and r.current.model != r.proposed.model)


def _newly_rejected(r: RowResult) -> bool:
    return r.current.outcome == "routed" and r.proposed.outcome == "rejected"


def _newly_served(r: RowResult) -> bool:
    return r.current.outcome == "rejected" and r.proposed.outcome == "routed"


def _delta(current: float | None, proposed: float | None) -> dict:
    delta = proposed - current if current is not None and proposed is not None else None
    return {"current": current, "proposed": proposed, "delta": delta}


def _bucket_stats(rows: list[RowResult]) -> dict:
    """The `{requests, route_changed, newly_rejected, newly_served, cost_usd,
    cost_usd_both_routed, latency_ms, quality_mean, onprem_share, overflow_hops}` block shared by `totals`,
    `by_app.<app>` and `by_workload.<workload>` (spec §8)."""
    cost = {}
    lat_p50 = {}
    lat_p95 = {}
    quality = {}
    onprem_share = {}
    overflow_hops = {}
    for arm in ("current", "proposed"):
        routed = [getattr(r, arm) for r in rows if getattr(r, arm).outcome == "routed"]
        cost[arm] = sum(a.cost_usd or 0.0 for a in routed)
        latencies = sorted(a.latency_ms for a in routed if a.latency_ms is not None)
        lat_p50[arm] = _nearest_rank(latencies, 50)
        lat_p95[arm] = _nearest_rank(latencies, 95)
        q_vals = [a.quality for a in routed if a.quality is not None]
        quality[arm] = _mean(q_vals)
        onprem_share[arm] = ((sum(1 for a in routed if a.provider == "onprem") / len(routed))
                              if routed else None)
        # Over every row, routed or not: a row rejected for overflowing every
        # reachable endpoint spent hops too.
        overflow_hops[arm] = sum(getattr(r, arm).overflow_hops for r in rows)

    # Like-for-like cost: only rows routed in BOTH arms. `cost_usd` above
    # sums each arm's routed rows, so a newly rejected row simply drops out
    # of the proposed sum and reads as a saving (P3a final review, M1).
    both = [r for r in rows
            if r.current.outcome == "routed" and r.proposed.outcome == "routed"]
    both_current = sum(r.current.cost_usd or 0.0 for r in both)
    both_proposed = sum(r.proposed.cost_usd or 0.0 for r in both)

    return {
        "requests": len(rows),
        "route_changed": sum(1 for r in rows if _route_changed(r)),
        "newly_rejected": sum(1 for r in rows if _newly_rejected(r)),
        "newly_served": sum(1 for r in rows if _newly_served(r)),
        "cost_usd": _delta(cost["current"], cost["proposed"]),
        "cost_usd_both_routed": _delta(both_current, both_proposed),
        "latency_ms": {"p50": _delta(lat_p50["current"], lat_p50["proposed"]),
                       "p95": _delta(lat_p95["current"], lat_p95["proposed"])},
        "quality_mean": _delta(quality["current"], quality["proposed"]),
        "onprem_share": {"current": onprem_share["current"], "proposed": onprem_share["proposed"]},
        "overflow_hops": {"current": overflow_hops["current"],
                          "proposed": overflow_hops["proposed"]},
    }


def _first_pick(arm: ArmResult) -> str | None:
    """The model the arm's FIRST select chose, before any overflow
    re-selection; None when the arm rejected the row. An `ArmResult` built
    without `attempted` (older callers) reads as its served model."""
    if arm.outcome != "routed":
        return None
    return arm.attempted[0] if arm.attempted else arm.model


def _baseline(results: list[RowResult]) -> dict:
    """Fidelity check (spec §7): does the `current` arm's pick agree with the
    recorded first attempt, over simulated `ok` rows with a recorded
    attempt. Never compares `proposed` -- the headline diff is always
    `current` vs `proposed`, both simulated."""
    compared = [r for r in results
                if r.row.get("status") == "ok" and (r.row.get("attempts") or [])]
    rows_compared = len(compared)
    agreement = None
    by_app: dict[str, float] = {}
    disagreements: Counter = Counter()
    if rows_compared:
        # First decision against first recorded attempt: an overflow hop
        # replay reproduces (P3c-2) must not read as a disagreement.
        agree_count = sum(1 for r in compared
                          if _first_pick(r.current) == r.row["attempts"][0])
        agreement = agree_count / rows_compared
        for app_id in sorted({r.row["application_id"] for r in compared}):
            sub = [r for r in compared if r.row["application_id"] == app_id]
            by_app[app_id] = sum(
                1 for r in sub if _first_pick(r.current) == r.row["attempts"][0]) / len(sub)
        for r in compared:
            recorded = r.row["attempts"][0]
            simulated = _first_pick(r.current) or "(rejected)"
            if recorded != simulated:
                disagreements[(recorded, simulated)] += 1

    top = sorted(disagreements.items(), key=lambda kv: (-kv[1], kv[0][0], kv[0][1]))[:10]
    top_disagreements = [{"recorded": rec, "simulated": sim, "count": n}
                         for (rec, sim), n in top]
    return {
        "rows_compared": rows_compared,
        "agreement": agreement,
        "warning": agreement is not None and agreement < 0.90,
        "by_app": by_app,
        "top_disagreements": top_disagreements,
    }


def _moves(results: list[RowResult]) -> list[dict]:
    counts: Counter = Counter()
    for r in results:
        frm = r.current.model or "(rejected)"
        to = r.proposed.model or "(rejected)"
        if frm != to:
            counts[(frm, to)] += 1
    top = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0][0], kv[0][1]))[:20]
    return [{"from": frm, "to": to, "count": n} for (frm, to), n in top]


def _core_key(core: tuple[str, ...], core_state: tuple[str, ...]) -> str:
    """The `by_core` grouping key for one rejected arm (P3b design §5.11):
    the requirement ids joined with "+" when the core is non-empty, else the
    state ids in the same parenthesised form, else "(unknown)" when the
    router's core computation itself failed and both are empty."""
    if core:
        return "+".join(core)
    if core_state:
        return "(state:" + ",".join(core_state) + ")"
    return "(unknown)"


def _new_rejections(results: list[RowResult]) -> dict:
    by_app: Counter = Counter()
    by_code: Counter = Counter()
    by_core: Counter = Counter()
    for r in results:
        if _newly_rejected(r):
            by_app[r.row["application_id"]] += 1
            for code in r.proposed.reject_codes:
                by_code[code] += 1
            # A context-window rejection has no conflicting core -- no
            # requirement refused it, the request is too large -- so it gets
            # its own key instead of reading as "(unknown)".
            if r.proposed.reject_codes == ("context_window",):
                by_core["(context_window)"] += 1
            else:
                by_core[_core_key(r.proposed.core, r.proposed.core_state)] += 1
    return {"by_app": dict(by_app), "by_code": dict(by_code), "by_core": dict(by_core)}


def _sources(results: list[RowResult]) -> dict:
    out: dict[str, dict] = {}
    for arm in ("current", "proposed"):
        latency: Counter = Counter()
        quality: Counter = Counter()
        for r in results:
            a = getattr(r, arm)
            if a.outcome == "routed":
                latency[a.latency_source] += 1
                quality[a.quality_source] += 1
        out[arm] = {"latency": dict(latency), "quality": dict(quality)}
    return out


def _window(results: list[RowResult]) -> dict | None:
    ts_list = [r.row["ts"] for r in results if r.row.get("ts")]
    if not ts_list:
        return None
    ts_list = sorted(ts_list, key=_parse_ts)
    return {"start": ts_list[0], "end": ts_list[-1]}


def _per_day(window: dict | None, cost_usd: dict) -> dict | None:
    if window is None:
        return None
    days = (_parse_ts(window["end"]) - _parse_ts(window["start"])).total_seconds() / 86400
    if days < 1:
        return None
    return {"cost_usd": {k: (v / days if v is not None else None) for k, v in cost_usd.items()}}


def _assumptions_summary(results: list[RowResult], assumptions: dict | None) -> dict:
    """The `assumptions` block of `simulation.json` (P3c-2 spec §4): what the
    proposal assumed (`proposal.assumptions_block`'s three maps, empty when
    it assumed nothing) plus `saturated_complexity_estimated`, the number of
    rows whose scaled complexity is an estimate."""
    block = {key: dict((assumptions or {}).get(key) or {})
             for key in ("token_scale", "cached_share", "latency_ms")}
    block["saturated_complexity_estimated"] = sum(
        1 for r in results if r.proposed.complexity_estimated)
    return block


def summarize(results: list[RowResult], excluded: dict[str, int], *, inputs: dict,
             edits: dict, assumptions: dict | None = None) -> dict:
    """The deterministic `simulation.json` document (spec §8). `inputs` is
    the caller's partial input record (no `window` yet -- this function
    computes it from the simulated rows and adds it before hashing, so
    `simulation_id` covers the window too)."""
    full_inputs = dict(inputs)
    full_inputs["window"] = _window(results)
    simulation_id = hashlib.sha256(
        json.dumps(full_inputs, sort_keys=True).encode("utf-8")).hexdigest()[:16]

    by_app_ids = sorted({r.row["application_id"] for r in results})
    by_workload_labels = sorted({r.row.get("workload") or UNTAGGED_WORKLOAD for r in results})
    totals = _bucket_stats(results)

    sim = {
        "schema": "routeforge.simulation/v1",
        "simulation_id": simulation_id,
        "inputs": full_inputs,
        "edits": edits,
        "assumptions": _assumptions_summary(results, assumptions),
        "rows": {"simulated": len(results), "excluded": dict(excluded)},
        "baseline": _baseline(results),
        "totals": totals,
        "by_app": {app_id: _bucket_stats([r for r in results
                                          if r.row["application_id"] == app_id])
                  for app_id in by_app_ids},
        "by_workload": {wl: _bucket_stats([r for r in results
                                           if (r.row.get("workload") or UNTAGGED_WORKLOAD) == wl])
                       for wl in by_workload_labels},
        "moves": _moves(results),
        "new_rejections": _new_rejections(results),
        "affected_apps": sorted({r.row["application_id"] for r in results
                                 if _route_changed(r) or _newly_rejected(r)
                                 or _newly_served(r)}),
        "sources": _sources(results),
        "per_day": _per_day(full_inputs["window"], totals["cost_usd"]),
    }
    return _round6(sim)


def _metrics_table(stats: dict) -> list[str]:
    lines = [
        f"requests: {stats['requests']} &middot; route_changed: {stats['route_changed']} "
        f"&middot; newly_rejected: {stats['newly_rejected']} "
        f"&middot; newly_served: {stats['newly_served']}",
        "",
        "| metric | current | proposed | delta |",
        "|---|---|---|---|",
    ]
    c = stats["cost_usd"]
    lines.append(f"| cost_usd | {_fmt(c['current'])} | {_fmt(c['proposed'])} | {_fmt(c['delta'])} |")
    b = stats["cost_usd_both_routed"]
    lines.append(f"| cost_usd (both routed) | {_fmt(b['current'])} | {_fmt(b['proposed'])} | "
                f"{_fmt(b['delta'])} |")
    p50 = stats["latency_ms"]["p50"]
    lines.append(f"| latency_ms p50 | {_fmt(p50['current'])} | {_fmt(p50['proposed'])} | "
                f"{_fmt(p50['delta'])} |")
    p95 = stats["latency_ms"]["p95"]
    lines.append(f"| latency_ms p95 | {_fmt(p95['current'])} | {_fmt(p95['proposed'])} | "
                f"{_fmt(p95['delta'])} |")
    q = stats["quality_mean"]
    lines.append(f"| quality_mean | {_fmt(q['current'])} | {_fmt(q['proposed'])} | {_fmt(q['delta'])} |")
    o = stats["onprem_share"]
    lines.append(f"| onprem_share | {_fmt(o['current'])} | {_fmt(o['proposed'])} | n/a |")
    h = stats["overflow_hops"]
    lines.append(f"| overflow_hops | {h['current']} | {h['proposed']} | "
                 f"{h['proposed'] - h['current']} |")
    return lines


#: Printed whenever a proposal sets `workload.cached_share` (spec §4.1).
CACHED_SHARE_NOTE = (
    "`cached_share` is an assumption: outcome rows do not record cached prompt tokens, so "
    "the current arm prices none as cached."
)


def _assumption_items(block: dict) -> list[str]:
    """One short string per assumption a proposal set, e.g.
    `"token_scale: *=0.6, claims=0.5"`; empty when it assumed nothing. With
    a `token_scale`, the last item says how many rows had a saturated
    complexity estimated."""
    items = []
    for key in ("token_scale", "cached_share", "latency_ms"):
        if block[key]:
            shown = ", ".join(f"{name}={_fmt(value)}" for name, value in sorted(block[key].items()))
            items.append(f"{key}: {shown}")
    if block["token_scale"]:
        items.append("rows whose saturated complexity was estimated from scaled prompt "
                     f"tokens: {block['saturated_complexity_estimated']}")
    return items


def _assumption_lines(block: dict) -> list[str]:
    """The report's "Assumptions" section for one `assumptions` block, or
    nothing at all when the proposal assumed nothing."""
    items = _assumption_items(block)
    if not items:
        return []
    lines = ["## Assumptions", ""] + [f"- {item}" for item in items]
    if block["cached_share"]:
        lines.append(f"- {CACHED_SHARE_NOTE}")
    lines.append("")
    return lines


def render_report(sim: dict) -> str:
    """The `report.md` document (spec §8), in the order: caveat, baseline
    warning, headline, new rejections, moves, by-app, by-workload,
    latency/quality sources, excluded rows."""
    lines: list[str] = ["> " + line for line in REPORT_CAVEAT.split("\n")]
    lines.append("")

    baseline = sim["baseline"]
    if baseline["warning"]:
        lines.append(
            f"> **Warning:** baseline agreement is only {baseline['agreement'] * 100:.1f}% "
            f"across {baseline['rows_compared']} compared rows (below the 90% threshold). "
            "Treat the headline diff as noisier than usual -- see spec §7 for causes.")
        lines.append("")

    lines += _assumption_lines(sim["assumptions"])

    lines.append("## Headline")
    lines.append("")
    lines += _metrics_table(sim["totals"])
    lines.append("")

    lines.append("## New rejections")
    lines.append("")
    new_rej = sim["new_rejections"]
    if not new_rej["by_app"] and not new_rej["by_code"]:
        lines.append("None.")
    else:
        lines.append("| app | count |")
        lines.append("|---|---|")
        for app_id in sorted(new_rej["by_app"]):
            lines.append(f"| {app_id} | {new_rej['by_app'][app_id]} |")
        lines.append("")
        lines.append("| code | count |")
        lines.append("|---|---|")
        for code in sorted(new_rej["by_code"]):
            lines.append(f"| {code} | {new_rej['by_code'][code]} |")
        lines.append("")
        lines.append("**By conflicting core**")
        lines.append("")
        lines.append("| core | count |")
        lines.append("|---|---|")
        for key, count in sorted(new_rej["by_core"].items(), key=lambda kv: (-kv[1], kv[0])):
            lines.append(f"| {key} | {count} |")
    lines.append("")

    lines.append("## Moves")
    lines.append("")
    if not sim["moves"]:
        lines.append("None.")
    else:
        lines.append("| from | to | count |")
        lines.append("|---|---|---|")
        for move in sim["moves"]:
            lines.append(f"| {move['from']} | {move['to']} | {move['count']} |")
    lines.append("")

    lines.append("## By app")
    lines.append("")
    for app_id in sorted(sim["by_app"]):
        lines.append(f"### {app_id}")
        lines.append("")
        lines += _metrics_table(sim["by_app"][app_id])
        lines.append("")

    lines.append("## By workload")
    lines.append("")
    for wl in sorted(sim["by_workload"]):
        lines.append(f"### {wl}")
        lines.append("")
        lines += _metrics_table(sim["by_workload"][wl])
        lines.append("")

    lines.append("## Latency and quality sources")
    lines.append("")
    sources = sim["sources"]
    for arm in ("current", "proposed"):
        lines.append(f"### {arm}")
        lines.append(f"- latency: {sources[arm]['latency']}")
        lines.append(f"- quality: {sources[arm]['quality']}")
    lines.append("")

    lines.append("## Excluded rows")
    lines.append("")
    excluded = sim["rows"]["excluded"]
    lines.append("| reason | count |")
    lines.append("|---|---|")
    for reason in sorted(excluded):
        lines.append(f"| {reason} | {excluded[reason]} |")

    return "\n".join(lines) + "\n"
