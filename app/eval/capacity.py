"""P3c-4: a capacity model for the policy simulator (offline, operator-run).

The simulator predicts routes, cost, latency and rejections under a policy
change. This module adds the question it could not answer: can the endpoints
that traffic lands on carry it?

**Stated capacity against replayed demand.** Nothing here is measured, and
nothing here manages capacity -- RouteForge deploys, scales and load-balances
no inference. An operator STATES limits for an endpoint (a `--capacity` file
for the deployment as it is, a proposal's `capacity` key for what a change
would make it), exactly as they state `workload.token_scale` or an
endpoint's `latency_ms`. Demand is computed from the rows replay already
routed: per arm, per endpoint, per time bucket.

    requests/min   = requests arriving in the bucket, per minute
    tokens/min     = prompt + completion tokens of those requests, per minute
    out tokens/min = completion tokens of those requests, per minute
    concurrency    = sum of their replayed latency / bucket length
                     (the Erlang offered load of the bucket)

Utilisation is demand divided by the stated limit. There is no queueing
model, no predicted rejection and no predicted delay: RouteForge does not
know what an endpoint does above a stated limit (`CAPACITY_CAVEAT`).

Never ranks a scenario and never proposes a remedy. Never imported by
serving code (`tests/eval/test_isolation.py`).
"""
from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone

from app.eval.proposal import (CAPACITY_LIMITS, MAX_VOLUME_SCALE, ProposalError, _check_keys,
                               _finite_number, _opt_dict, validate_capacity_limits,
                               volume_scale_block)
from app.eval.scenario import ALL_APPS
from app.eval.simulate import RowResult, _parse_ts, _utc, replay_tokens

SCHEMA = "routeforge.capacity-assumptions/v1"
_SCHEMA_PREFIX = "routeforge.capacity-assumptions/"
_SCHEMA_MAJOR = "v1"
METHOD = "bucketed_offered_load"

DEFAULT_BUCKET_SECONDS = 60
MAX_BUCKET_SECONDS = 86_400
DEFAULT_MIN_BUCKETS = 10

ARMS = ("current", "proposed")
#: stated limit -> the demand it is compared with.
DEMAND_FOR = {"max_concurrency": "concurrency",
              "requests_per_minute": "requests_per_minute",
              "tokens_per_minute": "tokens_per_minute",
              "output_tokens_per_minute": "output_tokens_per_minute"}
DEMANDS = tuple(DEMAND_FOR[limit] for limit in CAPACITY_LIMITS)

#: What a capacity figure does NOT model, as stable ids for `capacity.not_modelled`.
NOT_MODELLED = ("queueing", "batching", "autoscaling", "engine_concurrency", "external_load",
                "sub_bucket_bursts", "failover_load", "per_request_latency",
                "overload_behaviour")

_TOP_KEYS = frozenset({"schema", "title", "endpoints", "workload"})
_WORKLOAD_KEYS = frozenset({"volume_scale", "apps"})
_WORKLOAD_APP_KEYS = frozenset({"volume_scale"})

# A bucket is "over" a limit only when demand exceeds it by more than float
# noise: demand of exactly the limit is 100%, which is not above 100%.
_OVER = 1e-9


class CapacityError(ValueError):
    """A `--capacity` file load/validate error, or a capacity flag out of
    range. Always names the offending location, like `ProposalError`."""

    def __init__(self, where: str, message: str) -> None:
        self.where = where
        self.message = message
        super().__init__(f"{where}: {message}")


@dataclass(frozen=True)
class CapacityPlan:
    """Everything the capacity model was told, apart from what a proposal
    adds: the bucket length, the shortest window it will report on, and the
    BASE assumptions of a `--capacity` file -- limits and a volume scale
    that hold in BOTH arms. All empty without a file."""

    bucket_seconds: int = DEFAULT_BUCKET_SECONDS
    min_buckets: int = DEFAULT_MIN_BUCKETS
    limits: dict[str, dict[str, float]] = field(default_factory=dict)
    volume: dict[str, float] = field(default_factory=dict)
    file_sha256: str | None = None

    def __post_init__(self) -> None:
        for flag, value, top in (("--capacity-bucket", self.bucket_seconds, MAX_BUCKET_SECONDS),
                                 ("--capacity-min-buckets", self.min_buckets, None)):
            if (not isinstance(value, int) or isinstance(value, bool) or value < 1
                    or (top is not None and value > top)):
                raise CapacityError(
                    flag, "must be an integer >= 1" + (f" and <= {top}" if top else ""))


def load_capacity(path: str) -> tuple[dict, str]:
    """Read a `--capacity` JSON file. Returns `(doc, sha256 hex)` -- the hash
    is over the raw file bytes. Shape only: the schema, the closed key sets
    and every value's range. `build_plan` checks it against a snapshot."""
    with open(path, "rb") as f:
        raw = f.read()
    digest = hashlib.sha256(raw).hexdigest()
    try:
        doc = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise CapacityError("file", f"invalid JSON: {exc}") from exc
    try:
        _validate_file(doc)
    except ProposalError as exc:
        raise CapacityError(exc.where, exc.message) from exc
    return doc, digest


def _validate_file(doc) -> None:
    schema = doc.get("schema") if isinstance(doc, dict) else None
    if not isinstance(schema, str) or not schema.startswith(_SCHEMA_PREFIX):
        raise CapacityError("schema", f"must start with {_SCHEMA_PREFIX!r}; got {schema!r}")
    major = schema[len(_SCHEMA_PREFIX):].split(".", 1)[0]
    if major != _SCHEMA_MAJOR:
        raise CapacityError("schema", f"major version must be {_SCHEMA_MAJOR!r}; got {major!r}")
    _check_keys(doc, _TOP_KEYS, "")
    if doc.get("title") is not None and not isinstance(doc["title"], str):
        raise CapacityError("title", "must be a string")
    for slug, limits in _opt_dict(doc, "endpoints", "endpoints").items():
        validate_capacity_limits(f"endpoints.{slug}", limits)
    workload = _opt_dict(doc, "workload", "workload")
    _check_keys(workload, _WORKLOAD_KEYS, "workload")
    _validate_volume("workload", workload)
    for app_id, fields in _opt_dict(workload, "apps", "workload.apps").items():
        _check_keys(fields, _WORKLOAD_APP_KEYS, f"workload.apps.{app_id}")
        _validate_volume(f"workload.apps.{app_id}", fields)


def _validate_volume(loc: str, fields: dict) -> None:
    if "volume_scale" in fields:
        value = fields["volume_scale"]
        if not (_finite_number(value) and 0 < value <= MAX_VOLUME_SCALE):
            raise CapacityError(f"{loc}.volume_scale",
                                f"must be a finite number > 0 and <= {MAX_VOLUME_SCALE}")


def build_plan(doc: dict | None, sha256: str | None, snapshot: dict, *,
               bucket_seconds: int = DEFAULT_BUCKET_SECONDS,
               min_buckets: int = DEFAULT_MIN_BUCKETS) -> CapacityPlan:
    """The plan for one run. `doc` is a loaded `--capacity` file or None.
    Every endpoint it names must be in `snapshot` -- an onboarded row
    (enabled or not) or a priced YAML pool model -- and every application
    too: a typo must never read as "no limit stated"."""
    if doc is None:
        return CapacityPlan(bucket_seconds=bucket_seconds, min_buckets=min_buckets)
    pricing = snapshot["settings"].get("pricing")
    priced = pricing.get("models") if isinstance(pricing, dict) else None
    known = ({row["slug"] for row in snapshot["onboarded"]}
             | (set(priced) if isinstance(priced, dict) else set()))
    limits: dict[str, dict[str, float]] = {}
    for slug, stated in sorted((doc.get("endpoints") or {}).items()):
        if slug not in known:
            raise CapacityError(f"endpoints.{slug}",
                                "not an onboarded endpoint or a priced model in the snapshot")
        limits[slug] = {key: float(stated[key]) for key in CAPACITY_LIMITS if key in stated}
    app_ids = {entry["application_id"] for entry in snapshot["apps"]}
    workload = doc.get("workload") or {}
    for app_id in workload.get("apps") or {}:
        if app_id == ALL_APPS:
            raise CapacityError(f"workload.apps.{app_id}",
                                "application id '*' is reserved for the global value")
        if app_id not in app_ids:
            raise CapacityError(f"workload.apps.{app_id}", "unknown application")
    return CapacityPlan(bucket_seconds=bucket_seconds, min_buckets=min_buckets, limits=limits,
                        volume=volume_scale_block(workload), file_sha256=sha256)


def plan_inputs(plan: CapacityPlan) -> dict:
    """What goes into a document's `inputs.capacity`, and so into its id."""
    return {"file_sha256": plan.file_sha256, "bucket_seconds": plan.bucket_seconds,
            "min_buckets": plan.min_buckets}


def states_capacity(proposal: dict | None) -> bool:
    """True when the proposal states a limit for at least one endpoint."""
    return any(((proposal or {}).get("capacity") or {}).values())


def sets_volume(proposal: dict | None) -> bool:
    """True when the proposal sets `workload.volume_scale` anywhere."""
    return bool(volume_scale_block((proposal or {}).get("workload")))


# --- demand (spec §5) -----------------------------------------------------------

def arrival_bucket(row: dict, bucket_seconds: int) -> int | None:
    """The index of the bucket `row` ARRIVED in, or None when it has no `ts`.

    `ts` is when the outcome row was written -- the END of the request
    (`docs/reference/routing-outcomes.md` §3.2). Arrival is estimated as `ts`
    minus the row's recorded `latency_ms`; a missing, negative or non-finite
    latency subtracts nothing. Buckets are aligned to the Unix epoch, so a
    row's bucket does not depend on which other rows the export holds.
    Integer milliseconds throughout: no float decides a boundary."""
    ts = row.get("ts")
    if not ts:
        return None
    written_ms = round(_utc(_parse_ts(ts)).timestamp() * 1000)
    latency = row.get("latency_ms")
    service_ms = round(latency) if _finite_number(latency) and latency > 0 else 0
    return (written_ms - service_ms) // (bucket_seconds * 1000)


def bucket_start(index: int, bucket_seconds: int) -> str:
    """A bucket's start as an ISO-8601 UTC timestamp."""
    return datetime.fromtimestamp(index * bucket_seconds, tz=timezone.utc).isoformat()


def volume_for(volume: dict[str, float], app_id: str) -> float:
    """The app's own `volume_scale`, else the global one, else 1.0."""
    return volume.get(app_id, volume.get(ALL_APPS, 1.0))


#: One endpoint's load in one bucket: `[requests, tokens, output_tokens, busy_ms]`.
Load = list[float]


def arm_demand(results: list[RowResult], arm: str, *, bucket_seconds: int,
               volume: Callable[[str], float]) -> dict[str, dict[int, Load]]:
    """`{endpoint: {bucket: Load}}` for one arm: every row the arm ROUTED,
    added to the endpoint replay served it from, in the bucket it arrived
    in, `volume(application_id)` times. A rejected row adds nothing; a row
    with no `ts` cannot be placed and adds nothing (the block counts those).
    Tokens are the counts the arm priced the call at (`ArmResult`)."""
    demand: dict[str, dict[int, Load]] = {}
    for r in results:
        result = getattr(r, arm)
        if result.outcome != "routed":
            continue
        bucket = arrival_bucket(r.row, bucket_seconds)
        if bucket is None:
            continue
        times = volume(r.row["application_id"])
        completion = result.completion_tokens or 0
        load = demand.setdefault(result.model, {}).setdefault(bucket, [0.0, 0.0, 0.0, 0.0])
        load[0] += times
        load[1] += times * ((result.prompt_tokens or 0) + completion)
        load[2] += times * completion
        load[3] += times * (result.latency_ms or 0.0)
    return demand


def _rates(load: Load, bucket_seconds: int) -> dict[str, float]:
    """One bucket's load as the four demands, each in its limit's unit."""
    per_minute = 60.0 / bucket_seconds
    return {"concurrency": load[3] / (bucket_seconds * 1000.0),
            "requests_per_minute": load[0] * per_minute,
            "tokens_per_minute": load[1] * per_minute,
            "output_tokens_per_minute": load[2] * per_minute}


def _latencies(results: list[RowResult], arm: str) -> dict[str, tuple[float, str | None]]:
    """The one `(latency_ms, source)` replay gave each endpoint in `arm`:
    the first routed row's. Replay's ladder is a function of the endpoint
    alone -- `proposal`, `observed`, `signal` or `tier_default`."""
    out: dict[str, tuple[float, str | None]] = {}
    for r in results:
        result = getattr(r, arm)
        if result.outcome == "routed" and result.latency_ms is not None:
            out.setdefault(result.model, (float(result.latency_ms), result.latency_source))
    return out


def _arm_block(buckets: dict[int, Load], limits: dict[str, float],
               replayed: tuple[float, str | None] | None, *,
               active: int, bucket_seconds: int) -> dict:
    """One endpoint in one arm: demand per dimension, and utilisation of
    each STATED limit. `active` is the number of buckets the export has any
    traffic in -- the same denominator for every endpoint and both arms."""
    latency, source = replayed if replayed is not None else (None, None)
    rates = {index: _rates(load, bucket_seconds) for index, load in sorted(buckets.items())}
    requests = sum(load[0] for load in buckets.values())
    demand = {}
    for name in DEMANDS:
        values = [rate[name] for rate in rates.values()]
        demand[name] = {"peak": max(values, default=0.0), "mean": sum(values) / active}
    stated = {}
    for limit in CAPACITY_LIMITS:
        if limit not in limits:
            continue
        cap, name = limits[limit], DEMAND_FOR[limit]
        peak_at = None
        for index, rate in rates.items():           # ascending: the earliest peak wins
            if rate[name] > (rates[peak_at][name] if peak_at is not None else 0.0):
                peak_at = index
        over = [index for index, rate in rates.items() if rate[name] > cap * (1 + _OVER)]
        stated[limit] = {
            "limit": cap,
            "peak": demand[name]["peak"] / cap,
            "mean": demand[name]["mean"] / cap,
            "peak_bucket": None if peak_at is None else bucket_start(peak_at, bucket_seconds),
            "buckets_over": len(over),
            "buckets_over_share": len(over) / active,
            "requests_over_share": (sum(buckets[index][0] for index in over) / requests
                                    if requests else None),
        }
    return {"requests": requests, "latency_ms": latency, "latency_source": source,
            "latency_exceeds_bucket": latency is not None and latency > bucket_seconds * 1000,
            "demand": demand, "limits": stated}


def _peak(endpoints: dict, arm: str) -> dict | None:
    """The most loaded STATED limit of `arm`: the largest peak utilisation,
    the first in (endpoint, `CAPACITY_LIMITS`) order on a tie. None when the
    arm has no stated limit at all."""
    top = None
    for slug in sorted(endpoints):
        for limit, entry in endpoints[slug][arm]["limits"].items():
            if top is None or entry["peak"] > top["utilisation"]:
                top = {"endpoint": slug, "limit": limit, "utilisation": entry["peak"],
                       "bucket": entry["peak_bucket"]}
    return top


#: Excluded rows that load no endpoint inside the window, so are not "missing demand".
_NOT_DEMAND = ("cache_hit", "before_since")


def capacity_block(results: list[RowResult], plan: CapacityPlan, proposal: dict | None = None,
                   *, excluded: dict[str, int] | None = None) -> dict:
    """The `capacity` block for one proposal (spec §7).

    The `current` arm is measured against the plan's base limits at the
    base volume. The `proposed` arm is measured against the base limits
    overridden, limit by limit, by the proposal's `capacity`, at the base
    volume TIMES the proposal's `workload.volume_scale`.

    With fewer than `plan.min_buckets` buckets holding any traffic the block
    says `window_too_short` and carries no endpoint figure at all."""
    proposal = proposal or {}
    bucket_seconds = plan.bucket_seconds
    proposal_volume = volume_scale_block(proposal.get("workload"))
    stamped = [arrival_bucket(r.row, bucket_seconds) for r in results]
    active = sorted({index for index in stamped if index is not None})
    too_short = len(active) < plan.min_buckets
    block = {
        "method": METHOD,
        "bucket_seconds": bucket_seconds,
        "min_buckets": plan.min_buckets,
        "window": {
            "first_bucket": bucket_start(active[0], bucket_seconds) if active else None,
            "last_bucket": bucket_start(active[-1], bucket_seconds) if active else None,
            "buckets": active[-1] - active[0] + 1 if active else 0,
            "active_buckets": len(active)},
        "rows": {
            "simulated": len(results),
            "without_ts": stamped.count(None),
            "estimated_tokens": sum(1 for r, index in zip(results, stamped)
                                    if index is not None and replay_tokens(r.row)[2]),
            "excluded_not_counted": sum(n for reason, n in (excluded or {}).items()
                                        if reason not in _NOT_DEMAND)},
        "volume_scale": {"base": dict(plan.volume), "proposal": proposal_volume},
        "not_modelled": list(NOT_MODELLED),
        "window_too_short": too_short,
        "endpoints": {},
        "peak": {arm: None for arm in ARMS},
    }
    if too_short:
        return block

    stated = proposal.get("capacity") or {}
    limits = {"current": plan.limits,
              "proposed": {slug: {**plan.limits.get(slug, {}),
                                  **{key: float(value)
                                     for key, value in stated.get(slug, {}).items()}}
                           for slug in {*plan.limits, *stated}}}
    volumes = {"current": lambda app_id: volume_for(plan.volume, app_id),
               "proposed": lambda app_id: (volume_for(plan.volume, app_id)
                                           * volume_for(proposal_volume, app_id))}
    demand = {arm: arm_demand(results, arm, bucket_seconds=bucket_seconds, volume=volumes[arm])
              for arm in ARMS}
    latency = {arm: _latencies(results, arm) for arm in ARMS}
    slugs = sorted({slug for arm in ARMS for slug in (*demand[arm], *limits[arm])})
    block["endpoints"] = {
        slug: {arm: _arm_block(demand[arm].get(slug, {}), limits[arm].get(slug, {}),
                               latency[arm].get(slug), active=len(active),
                               bucket_seconds=bucket_seconds)
               for arm in ARMS}
        for slug in slugs}
    block["peak"] = {arm: _peak(block["endpoints"], arm) for arm in ARMS}
    return block


# --- report text (spec §8, §9) ----------------------------------------------------

#: Opens every capacity section, verbatim (spec §9.1, invariant C12), one
#: string per source line without the leading "> ".
CAPACITY_CAVEAT = (
    "**Stated capacity against replayed demand.** Every limit below is a figure the operator\n"
    "stated; RouteForge has not measured it and does not manage capacity. Demand is the replayed\n"
    "route of each recorded request, grouped into buckets by estimated arrival time (the time\n"
    "the record was written minus its recorded latency). Nothing here models queueing, batching,\n"
    "autoscaling, the serving engine's real concurrency behaviour, load that reaches an endpoint\n"
    "without passing through RouteForge, bursts shorter than a bucket, or the load of failover\n"
    "attempts. Replay gives each endpoint one latency, so offered concurrency is the request\n"
    "rate times that latency. RouteForge does not know what an endpoint does above a stated\n"
    "limit (queue, shed or slow down), so no rejection and no delay is predicted. An export of\n"
    "a few buckets says little."
)

#: Printed under the caveat, verbatim (spec §9.4, invariant C10).
NO_REMEDY_NOTE = (
    "This section reports load against stated limits and proposes no remedy: not more "
    "capacity, and not a change to any policy or protection."
)

#: Printed when the same run also has `--uncertainty` (spec §9.3, invariant C9).
NO_RANGE_NOTE = (
    "Capacity figures are points and carry no range. `--uncertainty` resamples rows as if "
    "their order did not matter; capacity depends on when requests arrive, so a resample of "
    "rows says nothing about it. Resampling whole blocks of time is not built."
)

#: Printed under the comparison table when it has the peak-load column.
PEAK_COLUMN_NOTE = (
    "Peak load is the proposed arm's most loaded stated limit and the endpoint it is on. It "
    "is listed in file order like every other column: a lower figure does not make a "
    "scenario the one to choose."
)


def caveat_lines() -> list[str]:
    return ["> " + line for line in CAPACITY_CAVEAT.split("\n")]


def _pct(value: float | None) -> str:
    return "n/a" if value is None else f"{value * 100:.1f}%"


def _num(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.6g}"


def _latency_text(arm: dict) -> str:
    if arm["latency_ms"] is None:
        return "none (no row routed there)"
    return f"{arm['latency_ms']:.6g} ms ({arm['latency_source']})"


def too_short_text(block: dict) -> str:
    """The explicit state a short export gets instead of a utilisation."""
    window = block["window"]
    return (f"window too short ({window['active_buckets']} of the {block['min_buckets']} "
            f"buckets needed)")


def peak_text(block: dict, arm: str) -> str:
    """One arm's most loaded stated limit, for a table cell or a line:
    `230.0% (local-llama, max_concurrency)`, `not stated`, or the
    window-too-short state."""
    if block["window_too_short"]:
        return too_short_text(block)
    peak = block["peak"][arm]
    if peak is None:
        return "not stated"
    return f"{_pct(peak['utilisation'])} ({peak['endpoint']}, {peak['limit']})"


def _scale_text(scale: dict[str, float]) -> str:
    return ", ".join(f"{name}={_num(value)}" for name, value in sorted(scale.items())) or "none"


def window_lines(block: dict) -> list[str]:
    """What every scenario of one run shares: the buckets, the window and
    the rows that could not be counted."""
    window, rows = block["window"], block["rows"]
    lines = [f"Buckets of {block['bucket_seconds']} s by estimated arrival time. "
             f"{window['active_buckets']} of {window['buckets']} bucket(s) in the window hold "
             f"traffic" + (f" ({window['first_bucket']} to {window['last_bucket']})"
                           if window["active_buckets"] else "")
             + f"; {rows['simulated']} simulated row(s). Means are over the buckets that hold "
             "traffic.", ""]
    if block["volume_scale"]["base"]:
        lines += ["Volume scale stated for both arms: "
                  f"{_scale_text(block['volume_scale']['base'])}.", ""]
    if rows["without_ts"]:
        lines += [f"**Note:** {rows['without_ts']} simulated row(s) have no timestamp and are "
                  "in no bucket: demand is understated by them.", ""]
    if rows["excluded_not_counted"]:
        lines += [f"**Note:** {rows['excluded_not_counted']} excluded row(s) (see \"Excluded "
                  "rows\") may have loaded an endpoint and are not in the demand.", ""]
    if rows["estimated_tokens"]:
        lines += [f"**Note:** {rows['estimated_tokens']} row(s) have no backend token count; "
                  "their token demand is RouteForge's own estimate.", ""]
    return lines


def _limit_rows(entry: dict, active: int) -> list[str]:
    rows = []
    for limit in CAPACITY_LIMITS:
        for arm in ARMS:
            stated = entry[arm]["limits"].get(limit)
            if stated is None:
                if limit in entry["current"]["limits"] or limit in entry["proposed"]["limits"]:
                    rows.append(f"| {limit} | {arm} | not stated | n/a | n/a | n/a | n/a | n/a |")
                continue
            rows.append(
                f"| {limit} | {arm} | {_num(stated['limit'])} | {_pct(stated['peak'])} | "
                f"{_pct(stated['mean'])} | {stated['buckets_over']} of {active} | "
                f"{_pct(stated['requests_over_share'])} | {stated['peak_bucket'] or 'n/a'} |")
    return rows


def endpoint_lines(slug: str, entry: dict, block: dict, *, depth: int) -> list[str]:
    """One endpoint: requests and latency per arm, utilisation of each
    stated limit, then the demand itself (stated limit or not)."""
    current, proposed = entry["current"], entry["proposed"]
    lines = [f"{'#' * depth} {slug}", "",
             f"Requests: current {_num(current['requests'])}, proposed "
             f"{_num(proposed['requests'])}. Replayed latency: current "
             f"{_latency_text(current)}, proposed {_latency_text(proposed)}.", ""]
    for arm in ARMS:
        if entry[arm]["latency_source"] == "tier_default":
            lines += [f"**Note:** the {arm} arm's latency for this endpoint is a tier default, "
                      "not a measurement: its offered concurrency rests on that default alone.",
                      ""]
        if entry[arm]["latency_exceeds_bucket"]:
            lines += [f"**Note:** the {arm} arm's replayed latency is longer than a bucket, so a "
                      "request's whole service time is counted in the bucket it arrived in. "
                      "A longer `--capacity-bucket` avoids that.", ""]
    limit_rows = _limit_rows(entry, block["window"]["active_buckets"])
    if limit_rows:
        lines += ["| limit | arm | stated | peak | mean | buckets over 100% | "
                  "requests in those buckets | peak bucket |",
                  "|---|---|---|---|---|---|---|---|", *limit_rows, ""]
    else:
        lines += ["No limit is stated for this endpoint; demand only.", ""]
    lines += ["| demand | current peak | current mean | proposed peak | proposed mean |",
              "|---|---|---|---|---|"]
    for name in DEMANDS:
        lines.append(f"| {name} | {_num(current['demand'][name]['peak'])} | "
                     f"{_num(current['demand'][name]['mean'])} | "
                     f"{_num(proposed['demand'][name]['peak'])} | "
                     f"{_num(proposed['demand'][name]['mean'])} |")
    lines.append("")
    return lines


def scenario_lines(block: dict, *, depth: int) -> list[str]:
    """One proposal's (or one scenario's) capacity figures: its own volume
    scale, each arm's peak load, then every endpoint -- or the
    window-too-short state and nothing else."""
    lines = []
    if block["volume_scale"]["proposal"]:
        lines += [f"Volume scale assumed by the proposal (proposed arm only, multiplying any "
                  f"stated for both arms): {_scale_text(block['volume_scale']['proposal'])}.", ""]
    if block["window_too_short"]:
        return lines + [f"No utilisation figure: {too_short_text(block)}. "
                        "`--capacity-min-buckets` sets the number needed.", ""]
    lines += [f"Peak load: current {peak_text(block, 'current')}; proposed "
              f"{peak_text(block, 'proposed')}.", ""]
    for slug in sorted(block["endpoints"]):
        lines += endpoint_lines(slug, block["endpoints"][slug], block, depth=depth)
    return lines


def intro_lines(block: dict, *, ranged: bool) -> list[str]:
    """The top of a capacity section: the caveat, the no-remedy sentence,
    the shared window, and -- with `--uncertainty` -- why there is no range."""
    lines = [*caveat_lines(), "", NO_REMEDY_NOTE, "", *window_lines(block)]
    if ranged:
        lines += [NO_RANGE_NOTE, ""]
    return lines


def render_capacity_section(block: dict, *, ranged: bool = False) -> str:
    """The `## Capacity` section of a single-proposal report. `ranged` is
    True when the same report also carries `--uncertainty` ranges. Ends with
    exactly one newline."""
    lines = ["## Capacity", "", *intro_lines(block, ranged=ranged),
             *scenario_lines(block, depth=3)]
    return "\n".join(lines).rstrip("\n") + "\n"
