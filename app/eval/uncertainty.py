"""P3c-3: uncertainty ranges for the policy simulator (offline, operator-run).

Every simulator figure is a point estimate from one replay of one export.
This module puts a range beside it: a percentile interval from a bootstrap
over the simulated rows.

- **Rows are resampled, never re-simulated.** Replay is a deterministic
  function of a row, so each row's two `ArmResult`s are computed once
  (`app/eval/simulate.py`) and a replicate only draws row positions.
- **Stratified by application.** Each app's rows are resampled to the same
  count, so a per-app bucket never empties and the mix of traffic between
  apps is held fixed.
- **Paired.** The draw for `(seed, replicate, app)` depends on nothing else,
  so both arms, every scenario and every shadow-price relaxation see the
  same resampled rows. A delta's range is the range of the paired
  difference.
- **Deterministic.** Same rows, plan and Python minor version give the same
  numbers. Nothing here reads a clock or the global `random` state.

**Quality labels.** Where a routed row's quality is a P2b-3 calibrated
score AND the operator passed the recommendation file that set was applied
from (`--quality-posteriors`), each replicate also draws that
`(model, workload)` unit's quality from its Beta posterior -- one draw per
unit per replicate, shared by both arms and every scenario. A published or
tier-default quality has no stated uncertainty and none is invented: it
stays a point, and the block says how many rows that is.

The range is sampling variability of the recorded rows under this replay
model, and nothing else -- `UNCERTAINTY_CAVEAT` and `NOT_COVERED` say what
it leaves out, and every report prints the caveat.

RouteForge never ranks or recommends: nothing here orders scenarios or
relaxations, and no key in the output says which is better.

Never imported by serving code (`tests/eval/test_isolation.py`).
"""
from __future__ import annotations

import hashlib
import json
import math
import random
from collections import Counter
from dataclasses import dataclass, field

from app.eval.calibration import recommendation_set_id
from app.eval.outcomes import _fmt, _round6
from app.eval.simulate import ArmResult, RowResult, _bucket_stats, _parse_ts, _route_changed

METHOD = "stratified_paired_row_bootstrap"
STRATA = "application_id"

#: What a range does NOT include, as stable ids for `uncertainty.not_covered`.
NOT_COVERED = ("replay_model_error", "admission_estimate_error", "unrecorded_inputs",
               "traffic_shift", "per_request_latency")

MAX_REPLICATES = 20000
_COUNTS = ("route_changed", "newly_rejected", "newly_served")
ARMS = ("current", "proposed")

_RECOMMENDATION_PREFIX = "routeforge.quality-recommendation/"
_RECOMMENDATION_MAJOR = "v1"

#: `(set_id, model, workload)`; workload None is the `(model, *)` unit.
PosteriorKey = tuple[str, str, str | None]


class UncertaintyError(ValueError):
    """A `--quality-posteriors` file failed to load or validate. Names the
    file and the offending location, like `SnapshotError`."""

    def __init__(self, where: str, message: str) -> None:
        self.where = where
        self.message = message
        super().__init__(f"{where}: {message}")


@dataclass(frozen=True)
class Posterior:
    """One calibrated unit's `Beta(alpha, beta)`, and the mean that was
    applied as its score."""

    alpha: float
    beta: float
    mean: float


@dataclass(frozen=True)
class UncertaintyPlan:
    """What `--uncertainty` asked for. The defaults are the CLI's."""

    replicates: int = 1000
    interval: float = 0.90
    seed: int = 0
    min_rows: int = 30
    # `load_posteriors`' two results: the units, and one `(set_id, sha256)`
    # per file. Empty when no `--quality-posteriors` file was given.
    posteriors: dict[PosteriorKey, Posterior] = field(default_factory=dict)
    posterior_files: tuple[tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        if (not isinstance(self.replicates, int) or isinstance(self.replicates, bool)
                or not 1 <= self.replicates <= MAX_REPLICATES):
            raise ValueError(f"replicates must be an int from 1 to {MAX_REPLICATES}; "
                             f"got {self.replicates!r}")
        # `not (0 < x < 1)` also refuses NaN, which compares false both ways.
        if isinstance(self.interval, bool) or not 0.0 < self.interval < 1.0:
            raise ValueError(f"interval must be strictly between 0 and 1; got {self.interval!r}")
        if not isinstance(self.seed, int) or isinstance(self.seed, bool) or self.seed < 0:
            raise ValueError(f"seed must be an int >= 0; got {self.seed!r}")
        if (not isinstance(self.min_rows, int) or isinstance(self.min_rows, bool)
                or self.min_rows < 1):
            raise ValueError(f"min_rows must be an int >= 1; got {self.min_rows!r}")


@dataclass(frozen=True)
class Bootstrap:
    """Raw replicate values. `totals` maps a figure name to one value per
    replicate, or is None when there are fewer than `min_rows` rows.
    `by_app` maps an app id to `(row count, the same map or None)`."""

    n: int
    totals: dict[str, list] | None
    by_app: dict[str, tuple[int, dict[str, list] | None]]


@dataclass(frozen=True)
class _Stratum:
    app: str
    n: int
    cls: list[int]                      # class id per row, in row order
    cols: list[list[float]]             # the DISTINCT cost columns
    col_of: tuple[int, ...]             # column index of (current, proposed, both-current, both-proposed)


def window_days(window: dict | None) -> float | None:
    """The export window in days, or None when there is no window or it is
    shorter than one day -- the rule `simulate._per_day` and
    `shadow._window_days` apply before printing a per-day figure."""
    if window is None:
        return None
    days = (_parse_ts(window["end"]) - _parse_ts(window["start"])).total_seconds() / 86400
    return days if days >= 1 else None


def _finite(value) -> bool:
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(value))


def load_posteriors(paths: list[str]
                    ) -> tuple[dict[PosteriorKey, Posterior], tuple[tuple[str, str], ...]]:
    """Read `routeforge.quality-recommendation/v1` files (P2b-3's
    `recommendation.json`). Returns the `recommended` units' posteriors keyed
    `(set_id, model, workload)`, and one `(set_id, sha256 of the file)` per
    distinct file, sorted.

    A file's `set_id` must equal the hash of its own content
    (`calibration.recommendation_set_id`): that is the same check the admin
    API ran when the set was applied, so a file that passes it and whose
    `set_id` is in the snapshot IS the set the snapshot's score came from.
    Units that were not `recommended` were never applied and are skipped.
    Raises `UncertaintyError` naming the file and the location."""
    posteriors: dict[PosteriorKey, Posterior] = {}
    files: set[tuple[str, str]] = set()
    for path in paths:
        with open(path, "rb") as f:
            raw = f.read()
        try:
            rec = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise UncertaintyError(path, f"invalid JSON: {exc}") from exc
        if not isinstance(rec, dict):
            raise UncertaintyError(path, "must be an object")
        schema = rec.get("schema")
        if (not isinstance(schema, str) or not schema.startswith(_RECOMMENDATION_PREFIX)
                or schema[len(_RECOMMENDATION_PREFIX):].split(".", 1)[0]
                != _RECOMMENDATION_MAJOR):
            raise UncertaintyError(
                f"{path}: schema",
                f"must be {_RECOMMENDATION_PREFIX}{_RECOMMENDATION_MAJOR}; got {schema!r}")
        set_id = rec.get("set_id")
        if not isinstance(set_id, str) or set_id != recommendation_set_id(rec):
            raise UncertaintyError(f"{path}: set_id",
                                   "does not match the file's own content")
        units = rec.get("units")
        if not isinstance(units, list):
            raise UncertaintyError(f"{path}: units", "must be a list")
        for i, unit in enumerate(units):
            where = f"{path}: units[{i}]"
            if not isinstance(unit, dict):
                raise UncertaintyError(where, "must be an object")
            if unit.get("status") != "recommended":
                continue
            model, workload = unit.get("model"), unit.get("workload")
            if not isinstance(model, str) or not model:
                raise UncertaintyError(f"{where}.model", "must be a non-empty string")
            if workload is not None and not isinstance(workload, str):
                raise UncertaintyError(f"{where}.workload", "must be null or a string")
            for key in ("alpha", "beta"):
                if not _finite(unit.get(key)) or unit[key] <= 0:
                    raise UncertaintyError(f"{where}.{key}", "must be a finite number > 0")
            if not _finite(unit.get("mean")) or not 0.0 <= unit["mean"] <= 1.0:
                raise UncertaintyError(f"{where}.mean", "must be a finite number in [0, 1]")
            posteriors[(set_id, model, workload)] = Posterior(
                float(unit["alpha"]), float(unit["beta"]), float(unit["mean"]))
        files.add((set_id, hashlib.sha256(raw).hexdigest()))
    return posteriors, tuple(sorted(files))


def posterior_key(arm: ArmResult, row: dict,
                  posteriors: dict[PosteriorKey, Posterior]) -> PosteriorKey | None:
    """The posterior behind this arm's quality, or None when it has none: the
    arm is not routed, its quality is not `calibrated`, or no loaded unit of
    that set matches. The resolver's own order is followed -- the row's
    workload, then `(model, *)` -- and the unit's mean must be the score the
    arm carries, so a file can never attach to a score it did not produce."""
    if (arm.outcome != "routed" or arm.quality_source != "calibrated"
            or arm.quality_set_id is None or arm.quality is None):
        return None
    for workload in (row.get("workload"), None):
        key = (arm.quality_set_id, arm.model, workload)
        post = posteriors.get(key)
        if post is not None and abs(post.mean - arm.quality) <= 1e-6:
            return key
    return None


def _arm_sig(arm: ArmResult, row: dict, posteriors: dict[PosteriorKey, Posterior]) -> tuple:
    """Everything a figure reads from one arm of one row EXCEPT its cost:
    `(routed, on-prem, latency_ms, quality, posterior key)`. Latency and
    quality are properties of the picked model (and workload), so rows share
    a small number of these."""
    if arm.outcome != "routed":
        return (False, False, None, None, None)
    return (True, arm.provider == "onprem", arm.latency_ms, arm.quality,
            posterior_key(arm, row, posteriors))


def _strata(results: list[RowResult], posteriors: dict[PosteriorKey, Posterior]
           ) -> tuple[list[_Stratum], list[tuple]]:
    """One stratum per application (sorted by id), plus the class table.

    A row's CLASS is `(current signature, proposed signature, route
    changed)`. Every figure except cost is a function of how many rows of
    each class a replicate drew, so a replicate counts classes instead of
    walking rows. Cost is the one per-row value: four columns per stratum,
    with equal columns stored once (an app the proposal does not touch has
    one distinct column, not four)."""
    classes: dict[tuple, int] = {}
    by_app: dict[str, list[RowResult]] = {}
    for r in results:
        by_app.setdefault(r.row["application_id"], []).append(r)
    strata: list[_Stratum] = []
    for app in sorted(by_app):
        cls: list[int] = []
        columns: tuple[list[float], ...] = ([], [], [], [])
        for r in by_app[app]:
            key = (_arm_sig(r.current, r.row, posteriors),
                   _arm_sig(r.proposed, r.row, posteriors), _route_changed(r))
            cls.append(classes.setdefault(key, len(classes)))
            current = (r.current.cost_usd or 0.0) if r.current.outcome == "routed" else 0.0
            proposed = (r.proposed.cost_usd or 0.0) if r.proposed.outcome == "routed" else 0.0
            both = r.current.outcome == "routed" and r.proposed.outcome == "routed"
            for column, value in zip(columns, (current, proposed,
                                               current if both else 0.0,
                                               proposed if both else 0.0)):
                column.append(value)
        cols: list[list[float]] = []
        col_of: list[int] = []
        for column in columns:
            for i, seen in enumerate(cols):
                if seen == column:
                    col_of.append(i)
                    break
            else:
                cols.append(column)
                col_of.append(len(cols) - 1)
        strata.append(_Stratum(app, len(cls), cls, cols, tuple(col_of)))
    return strata, list(classes)


def _weighted_rank(weights: dict[float, int], pct: float) -> float | None:
    """`outcomes._nearest_rank` over a multiset given as `{value: count}`."""
    n = sum(weights.values())
    if n == 0:
        return None
    rank = max(1, min(n, math.ceil(pct / 100 * n)))
    seen = 0
    for value in sorted(weights):
        seen += weights[value]
        if seen >= rank:
            return value
    return None


def _put(out: dict, name: str, current: float | None, proposed: float | None) -> None:
    out[f"{name}.current"] = current
    out[f"{name}.proposed"] = proposed
    out[f"{name}.delta"] = (proposed - current
                            if current is not None and proposed is not None else None)


def _figures(n: int, counts: dict[int, int], costs: list[float], classes: list[tuple],
             days: float | None, qdraw: dict[PosteriorKey, float] | None = None
             ) -> dict[str, float | None]:
    """Every figure of one bucket, from its class counts and four cost sums.
    The same definitions as `simulate._bucket_stats`, stated on counts;
    `tests/eval/test_uncertainty.py` pins the two against each other.
    `qdraw` is this replicate's quality draw per posterior key; without it
    (the point estimate) every quality is the score the arm carries."""
    tally = dict.fromkeys(_COUNTS, 0)
    routed, onprem, q_n = [0, 0], [0, 0], [0, 0]
    q_sum = [0.0, 0.0]
    latency: tuple[dict, dict] = ({}, {})
    for cid, m in counts.items():
        current, proposed, changed = classes[cid]
        if changed:
            tally["route_changed"] += m
        if current[0] and not proposed[0]:
            tally["newly_rejected"] += m
        if proposed[0] and not current[0]:
            tally["newly_served"] += m
        for a, (is_routed, is_onprem, lat, quality, post) in enumerate((current, proposed)):
            if not is_routed:
                continue
            routed[a] += m
            if is_onprem:
                onprem[a] += m
            if lat is not None:
                latency[a][lat] = latency[a].get(lat, 0) + m
            if quality is not None:
                q_sum[a] += m * (quality if post is None or qdraw is None else qdraw[post])
                q_n[a] += m

    out: dict[str, float | None] = {}
    _put(out, "cost_usd", costs[0], costs[1])
    _put(out, "cost_usd_both_routed", costs[2], costs[3])
    for name in _COUNTS:
        out[name] = tally[name]
        out[f"{name}_share"] = tally[name] / n if n else None
    for label, pct in (("p50", 50), ("p95", 95)):
        _put(out, f"latency_ms.{label}",
             _weighted_rank(latency[0], pct), _weighted_rank(latency[1], pct))
    _put(out, "quality_mean",
         q_sum[0] / q_n[0] if q_n[0] else None, q_sum[1] / q_n[1] if q_n[1] else None)
    out["onprem_share.current"] = onprem[0] / routed[0] if routed[0] else None
    out["onprem_share.proposed"] = onprem[1] / routed[1] if routed[1] else None
    if days is not None:
        _put(out, "per_day.cost_usd", costs[0] / days, costs[1] / days)
        # The comparison table's own arithmetic (`scenarios._table_row` divides
        # the ROUNDED like-for-like delta), so this leaf's point is that cell.
        out["per_day.cost_usd_both_routed.delta"] = round(costs[3] - costs[2], 6) / days
    return out


def draw_indices(seed: int, replicate: int, app: str, n: int) -> list[int]:
    """The row positions replicate `replicate` draws for one app: `n` draws
    with replacement from `range(n)`. A function of its four arguments and
    nothing else -- that is the pairing (module docstring)."""
    return random.Random(f"{seed}:{replicate}:app:{app}").choices(range(n), k=n)


def draw_quality(seed: int, replicate: int, key: PosteriorKey, post: Posterior) -> float:
    """Replicate `replicate`'s quality for one calibrated unit: one draw from
    its `Beta(alpha, beta)`, a function of the arguments alone, so both arms
    and every scenario use the same value in the same replicate."""
    set_id, model, workload = key
    rng = random.Random(f"{seed}:{replicate}:quality:{set_id}:{model}:{workload or '*'}")
    return rng.betavariate(post.alpha, post.beta)


def _record(samples: dict[str, list], figures: dict[str, float | None]) -> None:
    for name, value in figures.items():
        samples.setdefault(name, []).append(value)


def run_bootstrap(results: list[RowResult], plan: UncertaintyPlan, *,
                  days: float | None = None) -> Bootstrap:
    """`plan.replicates` stratified resamples of `results`. `days` (the
    export window, fixed, never resampled) adds the `per_day.*` figures to
    the totals. A bucket with fewer than `plan.min_rows` rows gets no
    samples; its rows are still drawn, because they are part of the totals.
    With fewer than `min_rows` rows in all, nothing is drawn."""
    strata, classes = _strata(results, plan.posteriors)
    n_total = len(results)
    used = sorted({sig[4] for cls in classes for sig in cls[:2] if sig[4] is not None},
                  key=lambda k: (k[0], k[1], k[2] or ""))
    if n_total < plan.min_rows:
        return Bootstrap(n_total, None, {s.app: (s.n, None) for s in strata})
    totals: dict[str, list] = {}
    by_app: dict[str, tuple[int, dict[str, list] | None]] = {
        s.app: (s.n, {} if s.n >= plan.min_rows else None) for s in strata}
    for b in range(plan.replicates):
        qdraw = {key: draw_quality(plan.seed, b, key, plan.posteriors[key]) for key in used}
        total_counts: Counter = Counter()
        total_costs = [0.0, 0.0, 0.0, 0.0]
        for s in strata:
            idx = draw_indices(plan.seed, b, s.app, s.n)
            counts = Counter(map(s.cls.__getitem__, idx))
            sums = [sum(map(col.__getitem__, idx)) for col in s.cols]
            costs = [sums[i] for i in s.col_of]
            app_samples = by_app[s.app][1]
            if app_samples is not None:
                _record(app_samples, _figures(s.n, counts, costs, classes, None, qdraw))
            total_counts.update(counts)
            for j, value in enumerate(costs):
                total_costs[j] += value
        _record(totals, _figures(n_total, total_counts, total_costs, classes, days, qdraw))
    return Bootstrap(n_total, totals, by_app)


def _flatten(stats: dict, n: int, days: float | None) -> dict[str, float | None]:
    """A `_bucket_stats` block as the engine's flat figure names -- the
    POINT of every leaf, taken from the code that writes the figure itself."""
    out: dict[str, float | None] = {}
    for name in ("cost_usd", "cost_usd_both_routed", "quality_mean"):
        for key in ("current", "proposed", "delta"):
            out[f"{name}.{key}"] = stats[name][key]
    for label in ("p50", "p95"):
        for key in ("current", "proposed", "delta"):
            out[f"latency_ms.{label}.{key}"] = stats["latency_ms"][label][key]
    for name in _COUNTS:
        out[name] = stats[name]
        out[f"{name}_share"] = stats[name] / n if n else None
    out["onprem_share.current"] = stats["onprem_share"]["current"]
    out["onprem_share.proposed"] = stats["onprem_share"]["proposed"]
    if days is not None:
        for key in ("current", "proposed", "delta"):
            value = stats["cost_usd"][key]
            out[f"per_day.cost_usd.{key}"] = value / days if value is not None else None
        both = stats["cost_usd_both_routed"]["delta"]
        out["per_day.cost_usd_both_routed.delta"] = (round(both, 6) / days
                                                     if both is not None else None)
    return out


def interval_of(values: list[float], level: float) -> tuple[float, float]:
    """The equal-tailed percentile interval of `values` at `level`: with
    `k = floor((1 - level) / 2 * N)`, the `(k+1)`-th smallest and the
    `(k+1)`-th largest. One replicate gives `lo == hi`."""
    ordered = sorted(values)
    k = int(math.floor((1.0 - level) / 2 * len(ordered) + 1e-9))
    return ordered[k], ordered[len(ordered) - 1 - k]


def zero_bound(n: int, level: float) -> float:
    """For a count that is 0 in all `n` rows: the largest per-row rate that
    would still show no such row in `n` independent draws with probability
    `1 - level`, i.e. `1 - (1 - level) ** (1 / n)`. At the default 0.90 it
    is about `2.3 / n` (the "rule of three" is the same formula at 0.95).
    It assumes independent rows; correlated rows make the true bound larger."""
    return 1.0 - (1.0 - level) ** (1.0 / n)


def leaf(point: float | None, values: list, plan: UncertaintyPlan, *,
         zero_n: int | None = None) -> dict:
    """One figure's `{"point", "lo", "hi"}`, rounded to 6 dp.

    - A figure undefined (None) in ANY replicate has no range: `lo`/`hi` are
      null and `undefined_replicates` says in how many. An interval over the
      defined replicates only would be conditional on the figure existing.
    - `point_outside: true` when the point is not inside its own range. A
      percentile interval does not promise that; it is reported, not hidden
      by widening the range.
    - `zero_n` (a share whose count is 0 in all `zero_n` rows) adds
      `zero_bound`."""
    out: dict = {"point": point, "lo": None, "hi": None}
    undefined = sum(1 for v in values if v is None)
    if undefined:
        out["undefined_replicates"] = undefined
        return _round6(out)
    out["lo"], out["hi"] = interval_of(values, plan.interval)
    if zero_n:
        out["zero_bound"] = zero_bound(zero_n, plan.interval)
    out = _round6(out)
    if out["point"] is not None and not out["lo"] <= out["point"] <= out["hi"]:
        out["point_outside"] = True
    return out


def _nest(flat: dict[str, dict]) -> dict:
    out: dict = {}
    for name, value in flat.items():
        node = out
        *parents, last = name.split(".")
        for part in parents:
            node = node.setdefault(part, {})
        node[last] = value
    return out


def _bucket(n: int, points: dict[str, float | None], samples: dict[str, list] | None,
            plan: UncertaintyPlan) -> dict:
    """One bucket of the block: `{"n", <figure tree of leaves>}`, or
    `{"insufficient_data": true, "n"}` below `plan.min_rows`."""
    if samples is None:
        return {"insufficient_data": True, "n": n}
    flat = {}
    for name, values in samples.items():
        zero = n if name.endswith("_share") and points[name] == 0 else None
        flat[name] = leaf(points[name], values, plan, zero_n=zero)
    return {"n": n, **_nest(flat)}


def _count_outside(node) -> int:
    if isinstance(node, dict):
        return (1 if node.get("point_outside") else 0) + sum(
            _count_outside(v) for v in node.values())
    return 0


def quality_coverage(results: list[RowResult], plan: UncertaintyPlan) -> dict:
    """Per arm: `routed` rows that carry a quality, how many of those have a
    `calibrated` quality, and how many of THOSE have a loaded posterior. The
    quality range carries label uncertainty for `posterior` rows only; the
    rest contribute a point."""
    out = {}
    for name in ARMS:
        arms = [(getattr(r, name), r.row) for r in results]
        routed = [(a, row) for a, row in arms if a.outcome == "routed" and a.quality is not None]
        out[name] = {
            "routed": len(routed),
            "calibrated": sum(1 for a, _row in routed if a.quality_source == "calibrated"),
            "posterior": sum(1 for a, row in routed
                             if posterior_key(a, row, plan.posteriors) is not None)}
    return out


def results_block(results: list[RowResult], plan: UncertaintyPlan, *,
                  days: float | None = None) -> dict:
    """The ranges for ONE proposal's rows: `totals`, `by_app` (sorted),
    `per_day` (None without a window of at least a day), `quality_posterior`
    (`quality_coverage`) and `points_outside_interval`. Points come from
    `simulate._bucket_stats`, so a leaf's `point` is the figure the document
    already carries."""
    boot = run_bootstrap(results, plan, days=days)
    totals = _bucket(boot.n, _flatten(_bucket_stats(results), boot.n, days), boot.totals, plan)
    per_day = totals.pop("per_day", None)
    rows_of: dict[str, list[RowResult]] = {}
    for r in results:
        rows_of.setdefault(r.row["application_id"], []).append(r)
    by_app = {app: _bucket(n, _flatten(_bucket_stats(rows_of[app]), n, None), samples, plan)
              for app, (n, samples) in boot.by_app.items()}
    block = {"totals": totals, "by_app": by_app, "per_day": per_day}
    block["points_outside_interval"] = _count_outside(block)
    block["quality_posterior"] = quality_coverage(results, plan)
    return block


def table_ranges(block: dict) -> dict:
    """The leaves behind one scenario's comparison-table row, under the
    table's own column keys (`scenarios._table_row`): the like-for-like cost
    deltas, the three counts, the latency and quality deltas and the two
    on-prem shares. `{"insufficient_data": true, "n"}` when the totals have
    no ranges. `overflow_hops` and `affected_apps` have no range."""
    totals = block["totals"]
    if totals.get("insufficient_data"):
        return dict(totals)
    per_day = block["per_day"]
    return {
        "cost_per_day_delta_usd": (per_day["cost_usd_both_routed"]["delta"]
                                   if per_day is not None else None),
        "cost_delta_usd": totals["cost_usd_both_routed"]["delta"],
        "route_changed": totals["route_changed"],
        "newly_rejected": totals["newly_rejected"],
        "newly_served": totals["newly_served"],
        "latency_p50_delta_ms": totals["latency_ms"]["p50"]["delta"],
        "latency_p95_delta_ms": totals["latency_ms"]["p95"]["delta"],
        "quality_mean_delta": totals["quality_mean"]["delta"],
        "onprem_share_current": totals["onprem_share"]["current"],
        "onprem_share_proposed": totals["onprem_share"]["proposed"],
    }


def plan_inputs(plan: UncertaintyPlan) -> dict:
    """What enters a document's `inputs` (and so its id) with `--uncertainty`."""
    return {"replicates": plan.replicates, "interval": plan.interval, "seed": plan.seed,
            "min_rows": plan.min_rows,
            "quality_posteriors_sha256": [sha for _set_id, sha in plan.posterior_files]}


def plan_block(plan: UncertaintyPlan) -> dict:
    """The head of every `uncertainty` block: how the ranges were made and
    what they leave out."""
    return {"method": METHOD, "strata": STRATA, "replicates": plan.replicates,
            "interval": plan.interval, "seed": plan.seed, "min_rows": plan.min_rows,
            "quality_posterior_sets": [set_id for set_id, _sha in plan.posterior_files],
            "not_covered": list(NOT_COVERED)}


# --- report text (spec §10) ---------------------------------------------------

#: Printed in every report that shows a range, verbatim (spec §8), one string
#: per source line without the leading "> " -- the same convention as
#: `simulate.REPORT_CAVEAT`. Pinned by tests/eval/test_uncertainty_report.py.
UNCERTAINTY_CAVEAT = (
    "**Ranges show sampling variability only.** Each range is a percentile interval from\n"
    "resampling the recorded rows with replacement, within each application, under this replay\n"
    "model. It does not include replay model error (first-decision replay, frozen signals,\n"
    "predicted overflow), the error of the admission estimate for rows no backend answered,\n"
    "inputs that were not recorded, or future traffic shift. Replay gives each endpoint one\n"
    "latency, so a latency range reflects only which endpoints the resampled rows used. A narrow\n"
    "range on a biased replay is still biased."
)

#: Why a zero count is not "no risk" (spec §7). `{}` fields: n, level, bound, one_in.
ZERO_COUNT_NOTE = (
    "newly_rejected is 0 in all {n} rows. That is not zero risk: at the {level} level these "
    "rows are consistent with a newly-rejected rate of up to {bound} (about 1 request in "
    "{one_in}), if the rows are independent draws from the same traffic. Correlated rows, "
    "such as the calls of one agent loop, make the true bound larger."
)

# (label, path into a bucket) for the range table, in `_metrics_table`'s order.
_FIGURE_ROWS = tuple(
    [(f"cost_usd {k}", ("cost_usd", k)) for k in ("current", "proposed", "delta")]
    + [(f"cost_usd (both routed) {k}", ("cost_usd_both_routed", k))
       for k in ("current", "proposed", "delta")]
    + [(name, (name,)) for count in _COUNTS for name in (count, f"{count}_share")]
    + [(f"latency_ms {p} {k}", ("latency_ms", p, k))
       for p in ("p50", "p95") for k in ("current", "proposed", "delta")]
    + [(f"quality_mean {k}", ("quality_mean", k)) for k in ("current", "proposed", "delta")]
    + [(f"onprem_share {k}", ("onprem_share", k)) for k in ARMS])

_PER_DAY_ROWS = (
    ("cost_usd/day current", ("cost_usd", "current")),
    ("cost_usd/day proposed", ("cost_usd", "proposed")),
    ("cost_usd/day delta", ("cost_usd", "delta")),
    ("cost_usd/day (both routed) delta", ("cost_usd_both_routed", "delta")),
)


def level_text(level: float) -> str:
    """`0.9` as `90%`."""
    return f"{level * 100:g}%"


def caveat_lines() -> list[str]:
    return ["> " + line for line in UNCERTAINTY_CAVEAT.split("\n")]


def method_line(block: dict) -> str:
    """One sentence saying how the ranges were made, from the block's head."""
    return (f"{level_text(block['interval'])} percentile intervals from {block['replicates']} "
            "bootstrap resamples of the simulated rows, drawn with replacement within each "
            f"application (seed {block['seed']}). Both arms, and every scenario or relaxation, "
            f"use the same resampled rows. A bucket with fewer than {block['min_rows']} rows "
            "gets no range.")


def quality_line(coverage: dict) -> str:
    """How much of the quality range is label uncertainty (spec §6)."""
    cur, prop = coverage["current"], coverage["proposed"]
    text = (f"Quality uncertainty covers {cur['posterior']} of {cur['routed']} routed rows in "
            f"the current arm and {prop['posterior']} of {prop['routed']} in the proposed arm; "
            "the rest use a point prior.")
    missing = max(cur["calibrated"] - cur["posterior"], prop["calibrated"] - prop["posterior"])
    if missing:
        text += (f" Up to {missing} row(s) per arm have a calibrated quality with no posterior "
                 "loaded: pass the file that set was applied from with --quality-posteriors.")
    return text


def insufficient_text(bucket: dict) -> str:
    return f"insufficient data (n = {bucket['n']})"


def range_text(node: dict) -> str:
    """A leaf's range for a table cell: `lo to hi`, with what is unusual
    about it said in words."""
    if node["lo"] is None:
        return f"n/a (undefined in {node.get('undefined_replicates', 0)} resamples)"
    text = f"{_fmt(node['lo'])} to {_fmt(node['hi'])}"
    if node.get("point_outside"):
        text += " (point outside range)"
    return text


def with_range(text: str, node: dict | None) -> str:
    """`text [lo, hi]` for a cell of a table that already shows the point;
    `text` unchanged when there is no leaf for it."""
    if node is None:
        return text
    if node["lo"] is None:
        return f"{text} [n/a]"
    return f"{text} [{_fmt(node['lo'])}, {_fmt(node['hi'])}]"


def _dig(bucket: dict, path: tuple[str, ...]) -> dict:
    node = bucket
    for part in path:
        node = node[part]
    return node


def _table(bucket: dict, rows: tuple, level: float) -> list[str]:
    lines = [f"| figure | point | {level_text(level)} range |", "|---|---|---|"]
    for label, path in rows:
        node = _dig(bucket, path)
        lines.append(f"| {label} | {_fmt(node['point'])} | {range_text(node)} |")
    return lines


def zero_count_note(bucket: dict, level: float) -> str | None:
    """`ZERO_COUNT_NOTE` for a bucket whose `newly_rejected` is 0, else None."""
    if bucket.get("insufficient_data"):
        return None
    bound = bucket["newly_rejected_share"].get("zero_bound")
    if bound is None:
        return None
    return ZERO_COUNT_NOTE.format(n=bucket["n"], level=level_text(level),
                                  bound=f"{bound * 100:.3g}%",
                                  one_in=f"{max(1, round(1 / bound)):,}")


def bucket_lines(bucket: dict, level: float) -> list[str]:
    """One bucket for a report: its range table and, when `newly_rejected`
    is 0, the zero-count note -- or the insufficient-data line alone."""
    if bucket.get("insufficient_data"):
        return [insufficient_text(bucket)]
    lines = _table(bucket, _FIGURE_ROWS, level)
    note = zero_count_note(bucket, level)
    if note:
        lines += ["", note]
    return lines


def outside_line(count: int) -> str | None:
    if not count:
        return None
    return (f"{count} figure(s) have a point estimate outside their own range (marked). A "
            "percentile interval does not guarantee otherwise; nothing was widened.")


def render_uncertainty_section(block: dict) -> str:
    """The "Uncertainty ranges" section of a single-proposal `report.md`,
    from `simulation.json`'s `uncertainty` block alone: caveat, method, then
    (when the block has them -- a shadow-prices-only run does not) the
    quality coverage, the totals, the per-day figures and each app."""
    lines: list[str] = ["## Uncertainty ranges", ""]
    lines += caveat_lines()
    lines += ["", method_line(block), ""]
    if "totals" not in block:
        return "\n".join(lines)
    level = block["interval"]
    lines += [quality_line(block["quality_posterior"]), ""]
    outside = outside_line(block["points_outside_interval"])
    if outside:
        lines += [outside, ""]
    lines += [f"### Totals (n = {block['totals']['n']})", ""]
    lines += bucket_lines(block["totals"], level)
    lines.append("")
    if block["per_day"] is not None:
        lines += ["### Per day", ""]
        lines += _table(block["per_day"], _PER_DAY_ROWS, level)
        lines.append("")
    lines += ["### By app", ""]
    for app_id in sorted(block["by_app"]):
        bucket = block["by_app"][app_id]
        lines += [f"#### {app_id} (n = {bucket['n']})", ""]
        lines += bucket_lines(bucket, level)
        lines.append("")
    return "\n".join(lines)
