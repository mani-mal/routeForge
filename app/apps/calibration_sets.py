"""Serving-side validator for a quality-calibration recommendation (P2b-3
Task 5, the P2b-3 offline quality-calibration design
§6).

Deliberately independent of `app/eval` -- `app/eval/calibration.py` is
file-based offline tooling (spec §3/§5) and must never be imported from
serving. This module carries its own `recommendation_set_id`, the same
canonical-JSON content hash as `app/eval/calibration.py::recommendation_set_id`;
a parity test (which may import both) asserts they agree on a fixture.
"""
from __future__ import annotations

import hashlib
import json
import math
import re

from app.apps.outcomes import _WORKLOAD_RE

RECOMMENDATION_SCHEMA = "routeforge.quality-recommendation/v1"
_SCHEMA_RE = re.compile(r"^routeforge\.quality-recommendation/v(\d+)(?:\.\d+)?$")
_SUPPORTED_SCHEMA_MAJORS = {1}

STATUS_RECOMMENDED = "recommended"
STATUS_NO_CHANGE = "no_change"
_BOUND_STATUSES = (STATUS_RECOMMENDED, STATUS_NO_CHANGE)

# Credible level assumed when a file does not state one. Restated here rather
# than imported from app/eval (serving must never import app/eval); it is the
# level app/eval/calibration.py writes at `options.ci_level`.
_DEFAULT_CI_LEVEL = 0.90


class CalibrationError(ValueError):
    """A recommendation fails validation (P2b-3 §6). The message names the
    problem; the admin route turns this into a 422."""


def _canonical(rec: dict) -> str:
    return json.dumps({k: v for k, v in rec.items() if k != "set_id"},
                      sort_keys=True, separators=(",", ":"))


def recommendation_set_id(rec: dict) -> str:
    """First 16 hex chars of sha256 over the canonical JSON of `rec` without
    its `set_id` key. Must match `app/eval/calibration.py::recommendation_set_id`
    byte-for-byte (see `tests/test_quality_calibrations_api.py` parity test)."""
    return hashlib.sha256(_canonical(rec).encode("utf-8")).hexdigest()[:16]


def _is_number(v: object) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _valid_workload(w: object) -> bool:
    """`None` (the `(model, *)` unit) or a workload slug exactly as the
    serving path stores one: already lower-case and matching the P2b-2 tag
    shape (`app/apps/outcomes.py::_WORKLOAD_RE`, <= 64 chars). A value the
    serving path would have to lower-case first is refused, not normalised:
    a unit keyed `Claims` could never match a request's `claims`."""
    return w is None or (isinstance(w, str) and _WORKLOAD_RE.fullmatch(w) is not None)


def _ci_level(rec: dict) -> float:
    options = rec.get("options")
    level = options.get("ci_level") if isinstance(options, dict) else None
    if _is_number(level) and math.isfinite(level) and 0.0 < level < 1.0:
        return float(level)
    return _DEFAULT_CI_LEVEL


def validate_bounds(rec: dict, *, registry) -> list[dict]:
    """Lower bounds a recommendation carries: one `{model, workload, lower,
    ci_level}` for every `recommended` or `no_change` unit with a numeric
    `ci_low`. Raises `CalibrationError` for a non-finite `ci_low`, one outside
    [0, 1] or above the unit's `mean`, an unknown model, an invalid workload,
    or a duplicate `(model, workload)`. A unit without `ci_low` carries no
    bound and is skipped."""
    units = rec.get("units")
    if not isinstance(units, list):
        raise CalibrationError("recommendation.units must be a list")
    level = _ci_level(rec)
    pool_names = {c.name for c in registry.effective_pool()}
    out: list[dict] = []
    seen: set[tuple[str, str | None]] = set()
    for u in units:
        if not isinstance(u, dict) or u.get("status") not in _BOUND_STATUSES:
            continue
        low = u.get("ci_low")
        if low is None:
            continue
        model = u.get("model")
        if not isinstance(model, str) or model not in pool_names:
            raise CalibrationError(f"unit names an unknown model: {model!r}")
        workload = u.get("workload")
        if not _valid_workload(workload):
            raise CalibrationError(
                f"unit for model {model!r} has an invalid workload: {workload!r} "
                f"(must be null or a lower-case workload slug of at most 64 chars)")
        if not _is_number(low) or not math.isfinite(low) or not (0.0 <= low <= 1.0):
            raise CalibrationError(
                f"unit ci_low out of [0, 1] for model {model!r}: {low!r}")
        mean = u.get("mean")
        if not _is_number(mean) or not math.isfinite(mean) or low > mean:
            raise CalibrationError(
                f"unit ci_low {low!r} exceeds its mean {mean!r} for model {model!r}")
        if (model, workload) in seen:
            raise CalibrationError(
                f"duplicate bound for model {model!r}, workload {workload!r}")
        seen.add((model, workload))
        out.append({"model": model, "workload": workload,
                    "lower": float(low), "ci_level": level})
    return out


def validate_recommendation(rec: dict, *, registry, current_registry_version: str,
                            force_registry_mismatch: bool = False) -> list[dict]:
    """Validate a `recommendation.json` payload against the live registry
    (P2b-3 §6). Returns the units with `status == "recommended"`. Raises
    `CalibrationError(message)` naming the first problem found; the caller
    writes nothing on failure.

    Checks, in order: schema; `set_id` hash; every recommended unit's model
    is in `registry.effective_pool()`; its workload is `None` or a valid
    lower-case workload slug; its score is a finite value in [0, 1]; no two
    recommended units share a `(model, workload)`; at least one unit is
    recommended or carries a lower bound (an empty set would apply as a no-op that can be neither
    409-blocked nor reverted); the recommendation's `registry_version`
    matches `current_registry_version` unless `force_registry_mismatch` is
    set.
    """
    if not isinstance(rec, dict):
        raise CalibrationError("recommendation must be a JSON object")

    schema = rec.get("schema")
    m = _SCHEMA_RE.match(schema) if isinstance(schema, str) else None
    if not m or int(m.group(1)) not in _SUPPORTED_SCHEMA_MAJORS:
        raise CalibrationError(f"unsupported recommendation schema: {schema!r}")

    recomputed = recommendation_set_id(rec)
    if rec.get("set_id") != recomputed:
        raise CalibrationError(
            f"set_id mismatch: recommendation has {rec.get('set_id')!r}, "
            f"recomputed {recomputed!r} from its content")

    units = rec.get("units")
    if not isinstance(units, list):
        raise CalibrationError("recommendation.units must be a list")

    pool_names = {c.name for c in registry.effective_pool()}
    recommended: list[dict] = []
    seen: set[tuple[str, str | None]] = set()
    for u in units:
        if not isinstance(u, dict) or u.get("status") != STATUS_RECOMMENDED:
            continue
        model = u.get("model")
        if not isinstance(model, str) or model not in pool_names:
            raise CalibrationError(f"unit names an unknown model: {model!r}")
        workload = u.get("workload")
        if not _valid_workload(workload):
            raise CalibrationError(
                f"unit for model {model!r} has an invalid workload: {workload!r} "
                f"(must be null or a lower-case workload slug of at most 64 chars)")
        score = u.get("mean")
        if not _is_number(score) or not math.isfinite(score) or not (0.0 <= score <= 1.0):
            raise CalibrationError(
                f"unit score out of [0, 1] for model {model!r}: {score!r}")
        if (model, workload) in seen:
            raise CalibrationError(
                f"duplicate recommended unit for model {model!r}, "
                f"workload {workload!r}")
        seen.add((model, workload))
        recommended.append(u)

    # A file of only `no_change` units is still a bounds-only set; it is empty
    # (and refused) only when it carries no bound either.
    if not recommended and not validate_bounds(rec, registry=registry):
        raise CalibrationError("no recommended units")

    rec_version = rec.get("registry_version")
    if rec_version != current_registry_version and not force_registry_mismatch:
        raise CalibrationError(
            f"registry_version mismatch: recommendation was built against "
            f"{rec_version!r}, current registry is {current_registry_version!r} "
            f"(retry with force_registry_mismatch=true to override)")

    return recommended
