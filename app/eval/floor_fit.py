"""FeasibleRouteBench quality-floor arm support (B80 Task 9, spec §4.6 / C5).

Three pieces, all offline:

- **`split_cases`** -- a seeded 50/50 split of a suite into a *calibration*
  half and a *held-out* half, stratified by `tags["workload"]` (a case with
  no string workload tag is its own stratum). Bounds are fitted on the first
  half; the `router_floor` arm is scored on the second, so the arm is never
  graded on the cases its bounds were learnt from.
- **`fit_bounds`** -- turns each calibration-half sweep row
  (`matrix.jsonl`) into one record of the P2b-2 outcome export
  (`routeforge.outcome/v1`, the exact line shape `GET /admin/outcomes/export`
  writes), reads them back through the REAL loader
  (`app.eval.outcomes.load_export`) and runs the REAL estimator
  (`app.eval.calibration.estimate`) with its DEFAULT gates. A unit whose
  status is `recommended` or `no_change` contributes its `ci_low` as a lower
  bound; an `insufficient` unit contributes nothing (an unknown bound never
  clears a floor). Nothing is re-derived here.
- **`FloorArm`** -- the `router_floor` strategy: the real `Router` with the
  harness settings' `quality_floors.mode` forced to `enforce`, a cold signal
  store, and a `QualityResolver` loaded with the bounds. Each case's context
  gets `workload = tags["workload"]` and the floor `resolve_floor` returns
  for it -- the floor VALUES are the deployment's own
  (`routing.quality_floors.workloads`), never invented from the suite.

How a sweep row becomes an export record: `chosen` = the endpoint,
`workload` = the case's tag, one `quality` feedback score = `row.quality`,
`status` `ok`; an error row is an unserved request (`backend_failed`,
`chosen` null), which the estimator excludes. The prior the estimator reads
is the model's own published / tier-default quality term
(`QualityResolver.resolve`), recorded in `scoring.candidates` just as the
router records it. Records are content-free: ids, enums and numbers.

Offline harness code only -- never imported by the serving path.
"""
from __future__ import annotations

import copy
import datetime as dt
import hashlib
import json
import math
import random
import tempfile
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path
from typing import TYPE_CHECKING

from app.config.schema import Settings
from app.errors import NoEligibleCandidate
from app.eval.calibration import STATUS_NO_CHANGE, STATUS_RECOMMENDED, estimate
from app.eval.cases import Case
from app.eval.oracle import MatrixRow
from app.eval.outcomes import load_export
from app.eval.strategies import StrategyInput, router_attempts
from app.models import ExecutionPolicyContext
from app.routing.quality import QualityResolver
from app.routing.quality_floor import resolve_floor
from app.routing.router import Router
from app.routing.signals import SignalStore

if TYPE_CHECKING:
    from app.apps.registry import ModelRegistry

BOUNDS_SCHEMA = "routeforge.floor-bounds/v1"
EXPORT_SCHEMA = "routeforge.outcome/v1"
ARM = "router_floor"
SPLIT_HELDOUT = "heldout"
STATUS_NONE = "none"
# The router's floor statuses that mean "the floor was met" (enforce mode).
_MET = "met"
# Export records need a timestamp; a fixed synthetic clock keeps the fit
# byte-identical run to run (the estimator only orders by it).
_EPOCH = dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)
_FEEDBACK_SOURCE = "bench"

BoundsMap = dict[tuple[str, str | None], float]


def case_workload(case: Case) -> str | None:
    """The case's workload tag, or `None` when it has no string tag."""
    w = case.tags.get("workload")
    return w if isinstance(w, str) and w else None


def suite_hash(cases: list[Case]) -> str:
    """sha256 over the suite's cases: sorted by id, each as its canonical
    JSON (`model_dump(mode="json")`, sorted keys, compact). Ties a bounds
    file to the suite it was fitted on -- independent of case order and of
    the file's formatting."""
    canonical = json.dumps(
        [[c.id, c.model_dump(mode="json")] for c in sorted(cases, key=lambda c: c.id)],
        sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------
# the split
# --------------------------------------------------------------------------

def split_cases(cases: list[Case], seed: int) -> tuple[list[str], list[str]]:
    """`(calibration ids, held-out ids)`: a 50/50 split stratified by
    workload, deterministic for `seed` and independent of suite order.

    Each stratum's ids are sorted, shuffled by a `random.Random` seeded from
    `(seed, workload)`, and cut in half. An odd stratum's extra case goes to
    whichever half is currently smaller overall (calibration on a tie), so
    both halves stay within one case of each other per stratum and
    near-balanced in total. Both lists come back sorted."""
    strata: dict[str | None, list[str]] = {}
    for case in cases:
        strata.setdefault(case_workload(case), []).append(case.id)
    calib: list[str] = []
    held: list[str] = []
    for workload in sorted(strata, key=lambda w: (w is None, w or "")):
        ids = sorted(strata[workload])
        random.Random(f"{seed}:{workload}").shuffle(ids)
        half = len(ids) // 2
        if len(ids) % 2 and len(calib) <= len(held):
            half += 1
        calib += ids[:half]
        held += ids[half:]
    return sorted(calib), sorted(held)


# --------------------------------------------------------------------------
# export-shaped records and the fit
# --------------------------------------------------------------------------

def export_lines(rows: Iterable[MatrixRow], cases: list[Case], calib_ids: set[str],
                 settings: Settings, registry: "ModelRegistry | None" = None
                 ) -> list[dict]:
    """One `routeforge.outcome/v1` export line per calibration-half row, in
    row order (see the module docstring for the mapping)."""
    workloads = {c.id: case_workload(c) for c in cases}
    resolver = QualityResolver(settings, registry=registry)
    lines: list[dict] = []
    for i, row in enumerate(r for r in rows if r.case_id in calib_ids):
        ts = (_EPOCH + dt.timedelta(seconds=i)).isoformat()
        served = row.error is None
        prior = resolver.resolve(row.endpoint)
        lines.append({
            "schema": EXPORT_SCHEMA,
            "outcome": {
                "request_id": f"{row.case_id}:{row.endpoint}",
                "ts": ts,
                "status": "ok" if served else "backend_failed",
                "endpoint_registry_version": None,
                "workload": workloads.get(row.case_id),
                "chosen": row.endpoint if served else None,
                "attempts": [row.endpoint],
                "cache_hit": False,
                "cost_usd": row.cost_usd,
                "latency_ms": row.latency_ms if row.latency_ms is not None else 0.0,
                "scoring": {"candidates": [{
                    "name": row.endpoint,
                    "quality": {"v": prior.score, "src": prior.source}}]},
            },
            "feedback": ([{"score_name": "quality", "value": row.quality,
                           "source": _FEEDBACK_SOURCE, "ts": ts}] if served else []),
        })
    return lines


def fit_bounds(rows: list[MatrixRow], cases: list[Case], calib_ids: set[str],
               settings: Settings, *, seed: int,
               registry: "ModelRegistry | None" = None) -> dict:
    """The bounds document `{"schema", "seed", "suite_hash", "bounds":
    [{model, workload, lower}]}` fitted on the calibration half (module
    docstring). `seed` is the split seed the calibration ids came from and
    `suite_hash` is `suite_hash(cases)`; the runner checks both."""
    lines = export_lines(rows, cases, calib_ids, settings, registry)
    # Through the real loader: the estimator sees exactly the shape the
    # export produces, never one assembled here.
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "export.ndjson"
        path.write_text("".join(json.dumps(line, sort_keys=True) + "\n" for line in lines),
                        encoding="utf-8")
        records, warnings = load_export(str(path))
    if warnings:   # a line we wrote did not load: a bug here, not bad input
        raise ValueError(f"export records did not round-trip: {warnings}")
    bounds = [{"model": u["model"], "workload": u["workload"], "lower": u["ci_low"]}
              for u in estimate(records)["units"]
              if u["status"] in (STATUS_RECOMMENDED, STATUS_NO_CHANGE)]
    return {"schema": BOUNDS_SCHEMA, "seed": seed, "suite_hash": suite_hash(cases),
            "bounds": bounds}


def dump_bounds(doc: dict) -> str:
    return json.dumps(doc, sort_keys=True, indent=2) + "\n"


# --------------------------------------------------------------------------
# files
# --------------------------------------------------------------------------

def load_floor_bounds(path: str) -> tuple[BoundsMap, int, str | None]:
    """Read a `routeforge.floor-bounds/v1` file: `({(model, workload):
    lower}, seed, suite_hash)` -- `suite_hash` is `None` when the file has
    none (a hand-written file; the runner refuses it). Raises `ValueError` on a wrong schema, a missing or
    non-int seed, an entry without a string model, a workload that is not a
    string or null, a lower bound that is not a finite number in [0, 1], or a
    duplicate `(model, workload)`."""
    try:
        doc = json.loads(Path(path).read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"floor bounds {path}: invalid JSON ({exc})") from exc
    if not isinstance(doc, dict) or doc.get("schema") != BOUNDS_SCHEMA:
        raise ValueError(f"floor bounds {path}: schema must be {BOUNDS_SCHEMA!r}")
    seed = doc.get("seed")
    if isinstance(seed, bool) or not isinstance(seed, int):
        raise ValueError(f"floor bounds {path}: seed must be an int, got {seed!r}")
    digest = doc.get("suite_hash")
    if digest is not None and not (isinstance(digest, str) and digest):
        raise ValueError(f"floor bounds {path}: suite_hash must be a non-empty string")
    entries = doc.get("bounds")
    if not isinstance(entries, list):
        raise ValueError(f"floor bounds {path}: bounds must be a list")
    mapping: BoundsMap = {}
    for i, b in enumerate(entries):
        where = f"floor bounds {path}: bounds[{i}]"
        if not isinstance(b, dict) or not isinstance(b.get("model"), str) or not b["model"]:
            raise ValueError(f"{where}: model must be a non-empty string")
        workload = b.get("workload")
        if workload is not None and not (isinstance(workload, str) and workload):
            raise ValueError(f"{where}: workload must be a string or null")
        lower = b.get("lower")
        if (isinstance(lower, bool) or not isinstance(lower, (int, float))
                or not math.isfinite(lower) or not 0.0 <= lower <= 1.0):
            raise ValueError(f"{where}: lower must be a finite number in [0, 1], "
                             f"got {lower!r}")
        key = (b["model"], workload)
        if key in mapping:
            raise ValueError(f"{where}: duplicate bound for {key}")
        mapping[key] = float(lower)
    return mapping, seed, digest


def load_matrix(path: str) -> list[MatrixRow]:
    """Read a run's `matrix.jsonl` back into `MatrixRow`s (extra keys such as
    `served`, `prompt_tokens` or `text` are ignored)."""
    rows: list[MatrixRow] = []
    with open(path, encoding="utf-8") as f:
        for lineno, raw in enumerate(f, start=1):
            if not raw.strip():
                continue
            try:
                obj = json.loads(raw)
                rows.append(MatrixRow(
                    case_id=obj["case_id"], endpoint=obj["endpoint"],
                    quality=float(obj["quality"]), passed=bool(obj["passed"]),
                    cost_usd=float(obj["cost_usd"]), latency_ms=obj.get("latency_ms"),
                    error=obj.get("error"), counts_unhealth=obj.get("counts_unhealth"),
                    failure_latency_ms=obj.get("failure_latency_ms"),
                    error_class=obj.get("error_class")))
            except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
                raise ValueError(f"matrix {path}: line {lineno}: {exc}") from exc
    return rows


# --------------------------------------------------------------------------
# the arm
# --------------------------------------------------------------------------

def floor_settings(settings: Settings) -> Settings:
    """A deep copy of the harness `settings` with `quality_floors.mode`
    forced to `enforce`. The per-workload floors stay the deployment's."""
    out = copy.deepcopy(settings)
    out.routing.quality_floors.mode = "enforce"
    return out


class FloorArm:
    """The `router_floor` strategy (module docstring). `context` fills a
    case's floor fields; calling the arm returns its failover chain;
    `status` is the first hop's floor status (`none` when no floor applied,
    `None` when the router made no choice)."""

    def __init__(self, settings: Settings, registry: "ModelRegistry",
                 bounds: Mapping[tuple[str, str | None], float], set_id: str,
                 max_attempts: int) -> None:
        self.settings = floor_settings(settings)
        self.resolver = QualityResolver(self.settings, registry=registry)
        self.resolver.set_bounds({k: (v, set_id) for k, v in bounds.items()})
        cold = SignalStore(self.settings.routing.signals)   # never fed
        self._attempts = router_attempts(self.settings, registry, cold, max_attempts,
                                         quality=self.resolver)
        self._probe = Router(self.settings, quality_resolver=self.resolver,
                             registry=registry, signals=cold)

    def context(self, case: Case, ctx: ExecutionPolicyContext) -> ExecutionPolicyContext:
        workload = case_workload(case)
        floor, mode = resolve_floor(self.settings, case.policy_view(), workload)
        return ctx.model_copy(update={"workload": workload, "quality_floor": floor,
                                      "quality_floor_mode": mode})

    def __call__(self, inp: StrategyInput, failed: Callable[[str], bool]
                 ) -> tuple[str, ...]:
        return self._attempts(inp, failed)

    def status(self, inp: StrategyInput) -> str | None:
        try:
            decision = self._probe.select(
                inp.ctx, inp.tokens_after, min(1.0, inp.tokens_after / 2000),
                output_tokens=inp.output_tokens)
        except NoEligibleCandidate:
            return None
        if decision.quality_floor is None:
            return STATUS_NONE
        return decision.quality_floor["status"]


def coverage(status_counts: Mapping[str, int]) -> float | None:
    """Share of floored cases (status other than `none`; a case where the
    router made no choice is not counted) whose floor was met. `None` when
    no case had a floor -- never 0.0, which would read as "all unmet"."""
    floored = sum(n for s, n in status_counts.items() if s not in (STATUS_NONE, "no_choice"))
    if not floored:
        return None
    return status_counts.get(_MET, 0) / floored
