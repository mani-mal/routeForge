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

    requests/min   = requests ARRIVING in the bucket, per minute
    tokens/min     = prompt + completion tokens of those requests, per minute
    out tokens/min = completion tokens of those requests, per minute
    concurrency    = milliseconds of service time that fall IN the bucket,
                     over the bucket's length: the mean number of requests
                     in service during it, had none ever waited

A request is in service from its estimated arrival for its service time
(`service_time`: its own recorded latency where replay serves it on the
endpoint that served it), and that time is spread over every bucket it runs
through. So one request is never more than one request of concurrency.

Utilisation is demand divided by the stated limit. A peak is a maximum over
buckets and is never reported alone: beside it are the mean over the whole
window (`mean_window`, the time-average) and the mean over the endpoint's
active buckets (`mean_active`). There is no queueing model, no predicted
rejection and no predicted delay: RouteForge does not know what an endpoint
does above a stated limit (`CAPACITY_CAVEAT`).

Everything that decides a figure is exact integer arithmetic on
milliseconds, requests and tokens; a volume scale multiplies the exact
integer total of the applications that share it, once. The block does not
depend on the order rows come in, and a uniform scale moves no peak to
another bucket.

Never ranks a scenario and never proposes a remedy. Never imported by
serving code (`tests/eval/test_isolation.py`).
"""
from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

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
#: `moved_request_latency`: a request replay moves to an endpoint it never ran
#: on gets that endpoint's mean service time, not one of its own.
NOT_MODELLED = ("queueing", "batching", "autoscaling", "engine_concurrency", "external_load",
                "sub_bucket_bursts", "failover_load", "moved_request_latency",
                "overload_behaviour")

_TOP_KEYS = frozenset({"schema", "title", "endpoints", "workload"})
_WORKLOAD_KEYS = frozenset({"volume_scale", "apps"})
_WORKLOAD_APP_KEYS = frozenset({"volume_scale"})

#: Why a `max_concurrency` entry carries no figure (`limits.<limit>.not_computed`):
#: the only service time replay has for the endpoint in that arm is the tier
#: default of `config/routing.yaml`, a routing-score constant.
TIER_DEFAULT_SERVICE_TIME = "tier_default_service_time"

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


# --- placement (spec §5.2) ----------------------------------------------------------

_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
_MILLISECOND = timedelta(milliseconds=1)
#: The longest service time the model will place: 24 hours. A recorded
#: latency above it is unusable (like a negative or non-finite one); an
#: asserted or replayed one above it is refused.
MAX_SERVICE_MS = 86_400_000


def written_ms(row: dict) -> int | None:
    """When the outcome row was written, in whole milliseconds since the
    Unix epoch (floor, exact integer arithmetic), or None with no `ts`."""
    ts = row.get("ts")
    if not ts:
        return None
    return (_utc(_parse_ts(ts)) - _EPOCH) // _MILLISECOND


def recorded_ms(row: dict) -> int | None:
    """The row's recorded `latency_ms` in whole milliseconds (nearest), or
    None when it is unusable: missing, not a number, negative, non-finite
    or above `MAX_SERVICE_MS`."""
    latency = row.get("latency_ms")
    if not _finite_number(latency) or latency < 0 or latency > MAX_SERVICE_MS:
        return None
    return round(latency)


def arrival_ms(row: dict) -> int | None:
    """The row's ESTIMATED ARRIVAL in milliseconds since the epoch, or None
    with no `ts`. `ts` is when the outcome row was written -- the END of the
    request (`docs/reference/routing-outcomes.md` §3.2) -- so arrival is `ts`
    minus the row's recorded latency; an unusable latency subtracts nothing.
    It is a property of the recorded request: the same in both arms,
    whichever service time an arm gives the row."""
    written = written_ms(row)
    return None if written is None else written - (recorded_ms(row) or 0)


def arrival_bucket(row: dict, bucket_seconds: int) -> int | None:
    """The index of the bucket `row` ARRIVED in, or None when it has no
    `ts`. Buckets are aligned to the Unix epoch, so a row's bucket does not
    depend on which other rows the export holds. Integer milliseconds
    throughout: no float decides a boundary."""
    arrival = arrival_ms(row)
    return None if arrival is None else arrival // (bucket_seconds * 1000)


def _last_recorded_bucket(row: dict, bucket_seconds: int) -> int | None:
    """The last bucket the RECORDED request was in service in: the bucket
    of its last millisecond, `[arrival, written)`; its arrival bucket when
    it has no usable latency."""
    written = written_ms(row)
    if written is None:
        return None
    return (written - (1 if recorded_ms(row) else 0)) // (bucket_seconds * 1000)


def bucket_start(index: int, bucket_seconds: int) -> str:
    """A bucket's start as an ISO-8601 UTC timestamp."""
    return (_EPOCH + timedelta(seconds=index * bucket_seconds)).isoformat()


def volume_for(volume: dict[str, float], app_id: str) -> float:
    """The app's own `volume_scale`, else the global one, else 1.0."""
    return volume.get(app_id, volume.get(ALL_APPS, 1.0))


# --- service time (spec §5.4) -------------------------------------------------------

#: Where a row's service time came from, in the order they are tried.
SERVICE_TIME_SOURCES = ("recorded", "endpoint_mean", "replay", "asserted")


def served_alone_by(row: dict) -> str | None:
    """The endpoint whose service time the row's recorded latency is: the
    one that served it (`status == "ok"`) on a single attempt, when that
    latency is usable. None otherwise -- a row that failed over carries the
    failed attempts' time too, and it cannot be divided between them."""
    attempts = row.get("attempts") or []
    if row.get("status") != "ok" or len(attempts) != 1 or row.get("chosen") != attempts[0]:
        return None
    return attempts[0] if recorded_ms(row) is not None else None


def endpoint_means(results: list[RowResult]) -> dict[str, int]:
    """Each endpoint's MEAN recorded latency in whole milliseconds, over the
    simulated rows it served alone (`served_alone_by`). The mean, not the
    median replay reports: offered load is rate times MEAN service time."""
    sums: dict[str, list[int]] = {}
    for r in results:
        slug = served_alone_by(r.row)
        if slug is not None:
            pair = sums.setdefault(slug, [0, 0])
            pair[0] += recorded_ms(r.row)
            pair[1] += 1
    # Round half up, in integers: independent of the order rows came in.
    return {slug: (2 * total + n) // (2 * n) for slug, (total, n) in sums.items()}


def _placeable_ms(latency: float | None, slug: str, what: str) -> int:
    if not _finite_number(latency) or latency < 0 or latency > MAX_SERVICE_MS:
        raise CapacityError(
            f"endpoints.{slug}.latency_ms",
            f"{what} latency {latency!r} ms cannot be placed: a service time is a finite "
            f"number of milliseconds from 0 to {MAX_SERVICE_MS} (24 hours)")
    return round(latency)


def service_time(r: RowResult, arm: str, means: dict[str, int]) -> tuple[int, str]:
    """`(milliseconds, source)` for a row `arm` routed (spec §5.4):

    - `asserted`: the proposal asserts a latency for the arm's endpoint. It
      is the operator's claim about the endpoint under that scenario, so it
      replaces every other source;
    - `recorded`: the arm serves the row on the endpoint that served it
      alone, so the row's own recorded latency is its service time there;
    - `endpoint_mean`: the endpoint's mean recorded latency (`means`);
    - `replay`: no simulated row was served alone by the endpoint, so the
      one latency replay has for it -- the snapshot's signal, else the tier
      default (`ArmResult.latency_source` says which). A tier default is
      placed like any other, so service time stays conserved, but it is not
      a service time: `_arm_block` computes no concurrency figure from it."""
    result = getattr(r, arm)
    if result.latency_source == "proposal":
        return _placeable_ms(result.latency_ms, result.model, "the asserted"), "asserted"
    if served_alone_by(r.row) == result.model:
        return recorded_ms(r.row), "recorded"
    if result.model in means:
        return means[result.model], "endpoint_mean"
    return _placeable_ms(result.latency_ms, result.model, "the replayed"), "replay"


# --- demand (spec §5) ---------------------------------------------------------------

#: Indices into one application's exact integer load in one bucket:
#: `[requests, tokens, output_tokens, service_ms]`.
REQUESTS, TOKENS, OUTPUT_TOKENS, SERVICE_MS = range(4)


@dataclass
class EndpointLoad:
    """One endpoint's load in one arm, as EXACT integers per (bucket,
    application): no volume scale is applied here, and no float is summed,
    so the figures cannot depend on the order rows came in.

    A row's requests and tokens are in the bucket it ARRIVED in (a rate
    limit counts admissions). Its service time is in every bucket its
    service interval `[arrival, arrival + service time)` overlaps, by the
    milliseconds of overlap -- so one request is never more than one
    request's worth of concurrency in any bucket."""

    cells: dict[int, dict[str, list[int]]] = field(default_factory=dict)
    # Service milliseconds past the window's last bucket, per application:
    # in no bucket, and reported so service time is conserved.
    after_window: dict[str, int] = field(default_factory=dict)
    rows: int = 0
    sources: dict[str, int] = field(
        default_factory=lambda: {source: 0 for source in SERVICE_TIME_SOURCES})
    service_ms: int = 0              # every row's service time, unscaled
    service_max_ms: int = 0
    replay_ms: int = 0               # the part of `service_ms` from source "replay"
    replay_sources: set[str] = field(default_factory=set)
    estimated_token_rows: int = 0

    def cell(self, bucket: int, app_id: str) -> list[int]:
        return self.cells.setdefault(bucket, {}).setdefault(app_id, [0, 0, 0, 0])


def arm_load(results: list[RowResult], arm: str, *, bucket_seconds: int,
             last_bucket: int | None = None,
             means: dict[str, int] | None = None) -> dict[str, EndpointLoad]:
    """`{endpoint: EndpointLoad}` for one arm: every row the arm ROUTED, on
    the endpoint replay served it from. A rejected row adds nothing; a row
    with no `ts` cannot be placed and adds nothing (the block counts those).
    Tokens are the counts the arm priced the call at (`ArmResult`).

    Service time past `last_bucket` (the window's last) goes to
    `after_window` instead of a bucket; with `last_bucket=None` nothing is
    cut off. `means` defaults to `endpoint_means(results)`."""
    means = endpoint_means(results) if means is None else means
    bucket_ms = bucket_seconds * 1000
    loads: dict[str, EndpointLoad] = {}
    for r in results:
        result = getattr(r, arm)
        if result.outcome != "routed":
            continue
        start = arrival_ms(r.row)
        if start is None:
            continue
        app_id = r.row["application_id"]
        load = loads.setdefault(result.model, EndpointLoad())
        service, source = service_time(r, arm, means)
        load.rows += 1
        load.sources[source] += 1
        load.service_ms += service
        load.service_max_ms = max(load.service_max_ms, service)
        if source == "replay":
            load.replay_ms += service
            load.replay_sources.add(result.latency_source or "unknown")
        if replay_tokens(r.row)[2]:
            load.estimated_token_rows += 1
        completion = result.completion_tokens or 0
        bucket = start // bucket_ms
        arrived = load.cell(bucket, app_id)
        arrived[REQUESTS] += 1
        arrived[TOKENS] += (result.prompt_tokens or 0) + completion
        arrived[OUTPUT_TOKENS] += completion
        end = start + service
        while bucket * bucket_ms < end:
            overlap = min(end, (bucket + 1) * bucket_ms) - max(start, bucket * bucket_ms)
            if last_bucket is not None and bucket > last_bucket:
                load.after_window[app_id] = load.after_window.get(app_id, 0) + overlap
            else:
                load.cell(bucket, app_id)[SERVICE_MS] += overlap
            bucket += 1
    return loads


def _scaled_sum(per_app: dict[str, int], volume: Callable[[str], float]) -> float:
    """Per-application exact integers as one volume-scaled float. The
    integers of applications that share a volume factor are added FIRST, as
    integers, and each distinct factor multiplies once; the products are
    summed with `math.fsum`. So a uniform scale is one multiplication of an
    exact total -- `0.7 * (a + b + c)`, never `0.7 * a + 0.7 * b + 0.7 * c`,
    which differs in the last place and could move a peak to another bucket."""
    by_factor: dict[float, int] = {}
    for app_id, value in per_app.items():
        factor = volume(app_id)
        by_factor[factor] = by_factor.get(factor, 0) + value
    return math.fsum(factor * by_factor[factor] for factor in sorted(by_factor))


def _scaled(per_app: dict[str, list[int]], volume: Callable[[str], float]) -> list[float]:
    """One bucket's (or one total's) per-application integers as four
    volume-scaled floats (`_scaled_sum`, once per figure)."""
    return [_scaled_sum({app_id: cell[i] for app_id, cell in per_app.items()}, volume)
            for i in range(4)]


def _rates(scaled: list[float], bucket_seconds: int) -> dict[str, float]:
    """A scaled load over ONE bucket as the four demands, each in its
    limit's unit."""
    return {"concurrency": scaled[SERVICE_MS] / (bucket_seconds * 1000),
            "requests_per_minute": scaled[REQUESTS] * 60 / bucket_seconds,
            "tokens_per_minute": scaled[TOKENS] * 60 / bucket_seconds,
            "output_tokens_per_minute": scaled[OUTPUT_TOKENS] * 60 / bucket_seconds}


def _arm_block(load: EndpointLoad | None, limits: dict[str, float],
               volume: Callable[[str], float], *, window_buckets: int,
               bucket_seconds: int) -> dict:
    """One endpoint in one arm: demand per dimension, and utilisation of
    each STATED limit. Three figures per demand, never a peak alone:

    - `peak`: the largest bucket;
    - `mean_window`: the total over EVERY bucket of the window, empty ones
      included (`window.buckets`) -- the time-average;
    - `mean_active`: the total over the endpoint's own `active_buckets`,
      those in which it had an arrival or a request in service.

    **No service time, no concurrency.** When the arm's service time on the
    endpoint is replay's TIER DEFAULT -- no simulated row was served by it
    alone, the snapshot has no latency signal for it and the proposal
    asserts none -- there is nothing to compute a concurrency from: the
    default is a routing-score constant (200 / 500 / 1000 ms by tier), and
    placing real requests at it reads as a load cut that never happened. So
    `demand.concurrency` is all null and a stated `max_concurrency` carries
    `not_computed` and null figures. Requests and tokens are unaffected."""
    load = load or EndpointLoad()
    no_service_time = "tier_default" in load.replay_sources
    scaled = {index: _scaled(per_app, volume) for index, per_app in sorted(load.cells.items())}
    rates = {index: _rates(values, bucket_seconds) for index, values in scaled.items()}
    # Totals from exact per-application integers, scaled once.
    totals: dict[str, list[int]] = {}
    for per_app in load.cells.values():
        for app_id, cell in per_app.items():
            total = totals.setdefault(app_id, [0, 0, 0, 0])
            for i in range(4):
                total[i] += cell[i]
    whole = _scaled(totals, volume)
    whole_rates = _rates(whole, bucket_seconds)
    active = len(load.cells)
    demand, peak_at = {}, {}
    for name in DEMANDS:
        if name == "concurrency" and no_service_time:
            peak_at[name] = None
            demand[name] = {"peak": None, "mean_window": None, "mean_active": None}
            continue
        peak, at = 0.0, None
        for index, rate in rates.items():           # ascending: the earliest peak wins
            if rate[name] > peak:
                peak, at = rate[name], index
        peak_at[name] = at
        demand[name] = {"peak": peak, "mean_window": whole_rates[name] / window_buckets,
                        "mean_active": whole_rates[name] / active if active else 0.0}
    stated = {}
    for limit in CAPACITY_LIMITS:
        if limit not in limits:
            continue
        cap, name = limits[limit], DEMAND_FOR[limit]
        if demand[name]["peak"] is None:
            # A state, not a number -- like `window_too_short`.
            stated[limit] = {
                "limit": cap, "not_computed": TIER_DEFAULT_SERVICE_TIME, "peak": None,
                "peak_bucket": None, "mean_window": None, "mean_active": None,
                "buckets_over": None, "buckets_over_share": None, "requests_over_share": None}
            continue
        over = [index for index, rate in rates.items() if rate[name] > cap * (1 + _OVER)]
        stated[limit] = {
            "limit": cap,
            "not_computed": None,
            "peak": demand[name]["peak"] / cap,
            "peak_bucket": (None if peak_at[name] is None
                            else bucket_start(peak_at[name], bucket_seconds)),
            "mean_window": demand[name]["mean_window"] / cap,
            "mean_active": demand[name]["mean_active"] / cap,
            "buckets_over": len(over),
            "buckets_over_share": len(over) / window_buckets,
            "requests_over_share": (
                math.fsum(scaled[index][REQUESTS] for index in over) / whole[REQUESTS]
                if whole[REQUESTS] else None),
        }
    # Service time past the window's last bucket is in no bucket (`arm_load`).
    # As a share of everything the arm placed, it is how much of this
    # endpoint's concurrency the window does not show.
    after = _scaled_sum(load.after_window, volume)
    placed = whole[SERVICE_MS] + after
    return {
        "requests": whole[REQUESTS],
        "active_buckets": active,
        "service_time_sources": dict(load.sources),
        "service_time_ms": {"mean": load.service_ms / load.rows if load.rows else None,
                            "max": load.service_max_ms if load.rows else None},
        "replay_share": load.replay_ms / load.service_ms if load.service_ms else None,
        "replay_source": min(load.replay_sources) if load.replay_sources else None,
        "service_ms_after_window": after,
        "service_after_window_share": (None if no_service_time or not placed
                                       else after / placed),
        "estimated_token_rows": load.estimated_token_rows,
        "demand": demand,
        "limits": stated,
    }


def _peak(endpoints: dict, arm: str, *, bucket_seconds: int, window_buckets: int) -> dict | None:
    """The most loaded STATED limit of `arm`: the largest peak utilisation,
    the first in (endpoint, `CAPACITY_LIMITS`) order on a tie. A limit whose
    utilisation is `not_computed` is never it (`_not_computed` lists those).
    None when the arm has no stated limit with a figure. It carries its own
    context -- the time-average and the share of the window over the limit,
    the bucket length and count they were computed over, and the share of
    that endpoint's service time the window cut off -- because a peak is a
    maximum over buckets and means little without them."""
    top = None
    for slug in sorted(endpoints):
        for limit, entry in endpoints[slug][arm]["limits"].items():
            if entry["not_computed"]:
                continue
            if top is None or entry["peak"] > top["utilisation"]:
                top = {"endpoint": slug, "limit": limit, "utilisation": entry["peak"],
                       "bucket": entry["peak_bucket"], "mean_window": entry["mean_window"],
                       "buckets_over_share": entry["buckets_over_share"],
                       "bucket_seconds": bucket_seconds, "buckets": window_buckets,
                       "service_after_window_share": (
                           endpoints[slug][arm]["service_after_window_share"])}
    return top


def _not_computed(endpoints: dict, arm: str) -> list[str]:
    """The endpoints whose stated `max_concurrency` has no figure in `arm`,
    sorted: what the arm's headline peak was chosen WITHOUT."""
    return [slug for slug in sorted(endpoints)
            if (endpoints[slug][arm]["limits"].get("max_concurrency") or {}).get("not_computed")]


#: Excluded rows that load no endpoint inside the window, so are not "missing demand".
_NOT_DEMAND = ("cache_hit", "before_since")


def capacity_block(results: list[RowResult], plan: CapacityPlan, proposal: dict | None = None,
                   *, excluded: dict[str, int] | None = None) -> dict:
    """The `capacity` block for one proposal (spec §7).

    The `current` arm is measured against the plan's base limits at the
    base volume. The `proposed` arm is measured against the base limits
    overridden, limit by limit, by the proposal's `capacity`, at the base
    volume TIMES the proposal's `workload.volume_scale`.

    The WINDOW runs from the bucket of the first estimated arrival to the
    last bucket a recorded request was still in service in. It comes from
    the recorded rows alone, so it is the same for both arms, every
    endpoint and every scenario.

    With fewer than `plan.min_buckets` buckets holding an ARRIVAL the block
    says `window_too_short` and carries no endpoint figure at all."""
    proposal = proposal or {}
    bucket_seconds = plan.bucket_seconds
    proposal_volume = volume_scale_block(proposal.get("workload"))
    stamped = [arrival_bucket(r.row, bucket_seconds) for r in results]
    arrivals = sorted({index for index in stamped if index is not None})
    ends = [index for index in (_last_recorded_bucket(r.row, bucket_seconds) for r in results)
            if index is not None]
    first, last = (arrivals[0], max(ends)) if arrivals else (None, None)
    window_buckets = last - first + 1 if arrivals else 0
    too_short = len(arrivals) < plan.min_buckets
    block = {
        "method": METHOD,
        "bucket_seconds": bucket_seconds,
        "min_buckets": plan.min_buckets,
        "window": {
            "first_bucket": bucket_start(first, bucket_seconds) if arrivals else None,
            "last_bucket": bucket_start(last, bucket_seconds) if arrivals else None,
            "buckets": window_buckets,
            "arrival_buckets": len(arrivals)},
        "rows": {
            "simulated": len(results),
            "without_ts": stamped.count(None),
            "unusable_latency": sum(1 for r, index in zip(results, stamped)
                                    if index is not None and recorded_ms(r.row) is None),
            "excluded_not_counted": sum(n for reason, n in (excluded or {}).items()
                                        if reason not in _NOT_DEMAND)},
        "volume_scale": {"base": dict(plan.volume), "proposal": proposal_volume},
        "not_modelled": list(NOT_MODELLED),
        "window_too_short": too_short,
        "endpoints": {},
        "peak": {arm: None for arm in ARMS},
        "concurrency_not_computed": {arm: [] for arm in ARMS},
    }
    if too_short:
        return block

    stated = proposal.get("capacity") or {}
    limits = {"current": plan.limits,
              "proposed": {slug: {**plan.limits.get(slug, {}),
                                  **{key: float(value)
                                     for key, value in stated.get(slug, {}).items()}}
                           for slug in sorted({*plan.limits, *stated})}}
    volumes = {"current": lambda app_id: volume_for(plan.volume, app_id),
               "proposed": lambda app_id: (volume_for(plan.volume, app_id)
                                           * volume_for(proposal_volume, app_id))}
    means = endpoint_means(results)
    loads = {arm: arm_load(results, arm, bucket_seconds=bucket_seconds, last_bucket=last,
                           means=means)
             for arm in ARMS}
    slugs = sorted({slug for arm in ARMS for slug in (*loads[arm], *limits[arm])})
    block["endpoints"] = {
        slug: {arm: _arm_block(loads[arm].get(slug), limits[arm].get(slug, {}), volumes[arm],
                               window_buckets=window_buckets, bucket_seconds=bucket_seconds)
               for arm in ARMS}
        for slug in slugs}
    block["peak"] = {arm: _peak(block["endpoints"], arm, bucket_seconds=bucket_seconds,
                                window_buckets=window_buckets)
                     for arm in ARMS}
    block["concurrency_not_computed"] = {arm: _not_computed(block["endpoints"], arm)
                                         for arm in ARMS}
    return block


# --- report text (spec §8, §9) ----------------------------------------------------

#: Opens every capacity section, verbatim (spec §9.1, invariant C12), one
#: string per source line without the leading "> ".
CAPACITY_CAVEAT = (
    "**Stated capacity against replayed demand.** Every limit below is a figure the operator\n"
    "stated; RouteForge has not measured it and does not manage capacity. Demand is the replayed\n"
    "route of each recorded request. A request arrives at its estimated arrival time (the time\n"
    "the record was written minus its recorded latency) and is in service from then for its\n"
    "service time: requests and tokens are counted in the bucket of arrival, service time in\n"
    "every bucket it runs through. Service time is the request's own recorded latency where\n"
    "replay serves it on the endpoint that served it alone; otherwise that endpoint's mean\n"
    "recorded latency; otherwise replay's latency for the endpoint. A latency the proposal\n"
    "asserts replaces all three. A recorded latency includes the time RouteForge spent before\n"
    "and after the backend call, and for a request that failed over it cannot be attributed to\n"
    "one endpoint, so it is not used. A peak is a maximum over buckets: it grows with a longer\n"
    "window and with a shorter bucket even for a steady stream, so every peak is printed with\n"
    "the average over the whole window. Nothing here models queueing, batching, autoscaling, the\n"
    "serving engine's real concurrency behaviour, load that reaches an endpoint without passing\n"
    "through RouteForge, bursts shorter than a bucket, or the load of failover attempts.\n"
    "RouteForge does not know what an endpoint does above a stated limit (queue, shed or slow\n"
    "down), so no rejection and no delay is predicted. An export of a few buckets says little."
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
    "Peak load is the proposed arm's most loaded stated limit and the endpoint it is on: its "
    "largest bucket, then its average over the whole window. A peak is a maximum over "
    "buckets, so read it with the average. It is listed in file order like every other "
    "column: a lower figure does not make a scenario the one to choose."
)

#: Says which bucket set each of the two averages is over (spec §6.2).
AVERAGES_NOTE = (
    "`avg (window)` is over every bucket of the window, empty ones included: the "
    "time-average. `avg (active)` is over the buckets in which the endpoint had an arrival "
    "or a request in service; that count is printed with it."
)

#: A share of an endpoint's service time from replay's latency for the
#: endpoint, rather than from recorded requests, that gets a sentence.
MATERIAL_REPLAY_SHARE = 0.1
#: A share of an arm's service time on an endpoint falling after the window
#: that is said in the headline, not only in the endpoint's own note.
MATERIAL_AFTER_WINDOW_SHARE = 0.1


def caveat_lines() -> list[str]:
    return ["> " + line for line in CAPACITY_CAVEAT.split("\n")]


def _pct(value: float | None) -> str:
    return "n/a" if value is None else f"{value * 100:.1f}%"


def _num(value: float | None) -> str:
    """Six significant figures, and never an exponent: `8.64e+07 ms` is not
    something to read off a report."""
    if value is None:
        return "n/a"
    text = f"{value:.6g}"
    if "e" not in text:
        return text
    return f"{value:,.0f}" if abs(value) >= 1 else f"{value:.12f}".rstrip("0")


def _share(value: float) -> str:
    """A share that is known to be above zero: never printed as `0.0%`."""
    return "less than 0.1%" if value < 0.0005 else _pct(value)


def _seconds(milliseconds: float) -> str:
    """Milliseconds as seconds, for a sentence: `17,760 s`, `0.4 s`. Never an
    exponent, however long."""
    return f"{milliseconds / 1000:,.1f}".removesuffix(".0") + " s"


def _after_window(endpoints: dict, arm: str) -> list[tuple[str, float]]:
    """`(endpoint, share)` for every endpoint whose stated `max_concurrency`
    is understated in `arm` by a material share of service time falling
    after the window, sorted by endpoint."""
    late = []
    for slug in sorted(endpoints):
        figures = endpoints[slug][arm]
        stated = figures["limits"].get("max_concurrency")
        share = figures["service_after_window_share"]
        if (stated is not None and not stated["not_computed"] and share is not None
                and share >= MATERIAL_AFTER_WINDOW_SHARE):
            late.append((slug, share))
    return late


def _service_text(arm: dict) -> str:
    """`mean 6000 ms, longest 6000 ms over 160 row(s) (160 recorded)`."""
    sources = arm["service_time_sources"]
    rows = sum(sources.values())
    if not rows:
        return "none (no row routed there)"
    used = ", ".join(f"{sources[source]} {source}" for source in SERVICE_TIME_SOURCES
                     if sources[source])
    return (f"mean {_num(arm['service_time_ms']['mean'])} ms, longest "
            f"{_num(arm['service_time_ms']['max'])} ms over {rows} row(s) ({used})")


def too_short_text(block: dict) -> str:
    """The explicit state a short export gets instead of a utilisation."""
    window = block["window"]
    return (f"window too short ({window['arrival_buckets']} of the {block['min_buckets']} "
            f"buckets needed)")


def peak_text(block: dict, arm: str) -> str:
    """One arm's most loaded stated limit, for a table cell or a line:
    `peak 230.0% / avg 45.0% (local-llama, max_concurrency, 60 s buckets)`,
    `not stated`, or the window-too-short state. Never a peak without the
    average over the window beside it (invariant C20).

    The headline is a maximum over endpoints and limits, and a figure that
    is missing from a maximum, or understated in it, must not leave it
    silently. So two things are named after it, whichever limit it is on:

    - `...; 16.4% of service time on e falls after the window` for an
      endpoint whose stated `max_concurrency` is understated by a material
      share of service time the window cut off;
    - `...; concurrency not computed for new` for an endpoint whose stated
      `max_concurrency` could not be computed. With nothing else stated the
      cell is `n/a; ...`."""
    if block["window_too_short"]:
        return too_short_text(block)
    peak = block["peak"][arm]
    left_out = "".join(f"; concurrency not computed for {slug}"
                       for slug in block["concurrency_not_computed"][arm])
    if peak is None:
        return "n/a" + left_out if left_out else "not stated"
    cut_off = "".join(f"; {_pct(share)} of service time on {slug} falls after the window"
                      for slug, share in _after_window(block["endpoints"], arm))
    return (f"peak {_pct(peak['utilisation'])} / avg {_pct(peak['mean_window'])} "
            f"({peak['endpoint']}, {peak['limit']}, {peak['bucket_seconds']} s buckets)"
            + cut_off + left_out)


def _scale_text(scale: dict[str, float]) -> str:
    return ", ".join(f"{name}={_num(value)}" for name, value in sorted(scale.items())) or "none"


def window_lines(block: dict) -> list[str]:
    """What every scenario of one run shares: the buckets, the window and
    the rows that could not be counted."""
    window, rows = block["window"], block["rows"]
    lines = [f"Buckets of {block['bucket_seconds']} s. The window is {window['buckets']} "
             "bucket(s), from the first estimated arrival to the end of the last recorded "
             "request" + (f" ({window['first_bucket']} to {window['last_bucket']})"
                          if window["buckets"] else "")
             + f"; a request arrived in {window['arrival_buckets']} of them; "
             f"{rows['simulated']} simulated row(s).", "", AVERAGES_NOTE, ""]
    if block["volume_scale"]["base"]:
        lines += ["Volume scale stated for both arms: "
                  f"{_scale_text(block['volume_scale']['base'])}.", ""]
    if rows["without_ts"]:
        lines += [f"**Note:** {rows['without_ts']} simulated row(s) have no timestamp and are "
                  "in no bucket: demand is understated by them.", ""]
    if rows["unusable_latency"]:
        lines += [f"**Note:** {rows['unusable_latency']} simulated row(s) have no usable "
                  "recorded latency (missing, negative, or above 24 hours): each is placed at "
                  "the time it was written, and its service time is not its own.", ""]
    if rows["excluded_not_counted"]:
        lines += [f"**Note:** {rows['excluded_not_counted']} excluded row(s) (see \"Excluded "
                  "rows\") may have loaded an endpoint and are not in the demand.", ""]
    return lines


def _no_service_time_text(slug: str, arm: str) -> str:
    """What a `max_concurrency` row prints instead of a utilisation when the
    arm has no service time for the endpoint: the INPUT that is missing. A
    proposal's `latency_ms` is the proposed arm's, so the current arm's row
    names what the record lacks and no key."""
    if arm == "proposed":
        return f"n/a (no service time: state endpoints.{slug}.latency_ms)"
    return f"n/a (no service time: no recorded request or latency signal for {slug})"


def _limit_rows(slug: str, entry: dict, window_buckets: int) -> list[str]:
    rows = []
    for limit in CAPACITY_LIMITS:
        for arm in ARMS:
            stated = entry[arm]["limits"].get(limit)
            if stated is None:
                if limit in entry["current"]["limits"] or limit in entry["proposed"]["limits"]:
                    rows.append(f"| {limit} | {arm} | not stated | n/a | n/a | n/a | n/a | n/a "
                                "| n/a |")
                continue
            if stated["not_computed"]:
                rows.append(f"| {limit} | {arm} | {_num(stated['limit'])} | "
                            f"{_no_service_time_text(slug, arm)} | n/a | n/a | n/a | n/a | n/a |")
                continue
            rows.append(
                f"| {limit} | {arm} | {_num(stated['limit'])} | {_pct(stated['peak'])} | "
                f"{_pct(stated['mean_window'])} | {_pct(stated['mean_active'])} | "
                f"{stated['buckets_over']} of {window_buckets} | "
                f"{_pct(stated['requests_over_share'])} | {stated['peak_bucket'] or 'n/a'} |")
    return rows


def _arm_notes(arm: str, figures: dict) -> list[str]:
    """The sentences one arm of one endpoint gets only when they apply."""
    lines = []
    share = figures["replay_share"]
    no_service_time = figures["demand"]["concurrency"]["peak"] is None
    if no_service_time:
        # At any share: one row placed at a tier default is enough.
        lines += [f"**Note:** the {arm} arm's service time on this endpoint is a tier default: "
                  "no simulated row was served by it alone and the snapshot has no latency "
                  "signal for it. A tier default is a routing constant, not a service time, so "
                  "no concurrency figure is computed for this endpoint in this arm. Requests "
                  "and tokens are counted as usual.", ""]
    elif share is not None and share >= MATERIAL_REPLAY_SHARE:
        lines += [f"**Note:** {_pct(share)} of the {arm} arm's service time on this endpoint is "
                  f"replay's latency for the endpoint ({figures['replay_source']}), not a "
                  "recorded request's: no simulated row was served by it alone.", ""]
    if figures["service_ms_after_window"] and not no_service_time:
        lines += [f"**Note:** {_share(figures['service_after_window_share'])} of the {arm} "
                  "arm's service time on this endpoint "
                  f"({_seconds(figures['service_ms_after_window'])}) falls after the window's "
                  "last bucket and is in no bucket, so this arm's average on this endpoint is "
                  "understated and its peak may be.", ""]
    if figures["estimated_token_rows"]:
        lines += [f"**Note:** {figures['estimated_token_rows']} row(s) routed here in the {arm} "
                  "arm have no backend token count; their token demand is RouteForge's own "
                  "estimate.", ""]
    return lines


def endpoint_lines(slug: str, entry: dict, block: dict, *, depth: int) -> list[str]:
    """One endpoint: requests, active buckets and service time per arm,
    utilisation of each stated limit, then the demand itself (stated limit
    or not). Every peak has both averages in the same row."""
    current, proposed = entry["current"], entry["proposed"]
    window_buckets = block["window"]["buckets"]
    lines = [f"{'#' * depth} {slug}", "",
             f"Requests: current {_num(current['requests'])}, proposed "
             f"{_num(proposed['requests'])}. Active buckets (an arrival or a request in "
             f"service): current {current['active_buckets']} of {window_buckets}, proposed "
             f"{proposed['active_buckets']} of {window_buckets}.", "",
             f"Service time: current {_service_text(current)}; proposed "
             f"{_service_text(proposed)}.", ""]
    for arm in ARMS:
        lines += _arm_notes(arm, entry[arm])
    limit_rows = _limit_rows(slug, entry, window_buckets)
    if limit_rows:
        lines += ["| limit | arm | stated | peak | avg (window) | avg (active) | "
                  "buckets over 100% | requests arriving in those buckets | peak bucket |",
                  "|---|---|---|---|---|---|---|---|---|", *limit_rows, ""]
    else:
        lines += ["No limit is stated for this endpoint; demand only.", ""]
    lines += ["| demand | arm | peak | avg (window) | avg (active) |", "|---|---|---|---|---|"]
    for name in DEMANDS:
        for arm in ARMS:
            demand = entry[arm]["demand"][name]
            lines.append(f"| {name} | {arm} | {_num(demand['peak'])} | "
                         f"{_num(demand['mean_window'])} | {_num(demand['mean_active'])} |")
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
        return lines + [f"No utilisation figure: {too_short_text(block)}. A bucket counts "
                        "when a request arrived in it; `--capacity-min-buckets` sets the "
                        "number needed.", ""]
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
