"""Offline quality calibration (P2b-3 Task 3): turn a P2b-2 outcome export
into per-(model, workload) Beta-posterior quality estimates and a
hash-identified recommendation file an admin reviews and applies.

**File-based only.** No DB, no `app.api`, no serving import -- like
`app/eval/outcomes.py`, this reads a file an operator downloaded from
`GET /admin/outcomes/export` and nothing else (see
`tests/eval/test_isolation.py`). It only ever *recommends*; applying a
recommendation is a separate, human-gated admin action (spec §6).

**Grouping.** Records are grouped by `endpoint_registry_version` (`null` is
its own `"(unknown)"` group) and a recommendation is computed from exactly
one group -- never pooled across groups, since a registry change can change
which endpoints exist. The default group is the **newest**: the group that
contains the record with the latest `ts` (ISO-8601, compared as parsed
datetimes; an unparseable or missing `ts` never wins). A tie between groups
on that latest `ts` goes to the lexicographically greatest version label, so
the choice is deterministic. `registry_version=` selects a group explicitly.

**Credit and evidence** (spec §4). Credit goes to the served model
(`chosen`). Rows are excluded when `cache_hit` is true, `chosen` is null, or
the status is `no_eligible`, `backend_failed` or `context_overflow` -- those
say nothing about answer quality. Per included row:
  - each numeric feedback `quality` value in [0, 1] adds that fraction as
    successes and 1 trial;
  - each `route_correct` value of exactly 0 or 1 adds that value as
    successes and 1 trial;
  - a `verify_failed` row adds 0 successes and 1 trial.
A row with no evidence adds nothing but still counts toward `n_rows` and
`label_coverage` (= rows with at least one piece of evidence / `n_rows`).
`n_labels` is the trial count `t`.

**Prior.** `Beta(k*q0, k*(1-q0))`, posterior `Beta(a0 + s, b0 + t - s)`.
`q0` is the model's quality term (`{"v", "src"}`) from the group's
`scoring.candidates`, taken only when `src` is `published` or
`tier_default`: a `calibrated` or `customer` term is itself a previous
recommendation or a declaration, and recalibrating against it would compound
one calibration on top of another. Among qualifying terms the one from the
newest record wins (latest `ts`, then greatest `request_id`, then first in
candidate order). `priors[model]` overrides the export (`q0_source` =
`"prior"`; a prior must lie strictly inside (0, 1), and an export term of
exactly 0 or 1 is skipped as degenerate). With no source at all the model's
units are `insufficient` with reason `no prior`.

**Beta quantiles** are pure Python (`_beta_ppf`: `math.lgamma` plus the
Numerical Recipes `betacf` continued fraction, inverted by bisection to
1e-10). scipy is not a dependency, and one implementation everywhere keeps
the recommendation byte-identical across environments.

**Determinism.** Every float is rounded to 6 decimal places, units are
sorted by `(model, workload is None, workload)`, and `set_id` hashes the
canonical (sorted-key, compact) JSON, so the same input and options always
produce the same `set_id` and byte-identical `dump_recommendation` output.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import math

from app.eval.outcomes import UNKNOWN_REGISTRY_VERSION, _round6

RECOMMENDATION_SCHEMA = "routeforge.quality-recommendation/v1"

# 90% equal-tailed interval (spec §4).
CI_LEVEL = 0.90
_CI_LOW_P = (1.0 - CI_LEVEL) / 2
_CI_HIGH_P = 1.0 - _CI_LOW_P

EXCLUDED_STATUSES = ("no_eligible", "backend_failed", "context_overflow")
PRIOR_SOURCES = ("published", "tier_default")

STATUS_RECOMMENDED = "recommended"
STATUS_INSUFFICIENT = "insufficient"
STATUS_NO_CHANGE = "no_change"


# --- Beta distribution (pure Python) -----------------------------------

def _betacf(a: float, b: float, x: float) -> float:
    """Continued fraction for the incomplete beta (Numerical Recipes
    `betacf`, modified Lentz)."""
    fpmin = 1e-300
    qab, qap, qam = a + b, a + 1.0, a - 1.0
    c = 1.0
    d = 1.0 - qab * x / qap
    if abs(d) < fpmin:
        d = fpmin
    d = 1.0 / d
    h = d
    for m in range(1, 1000):
        m2 = 2 * m
        aa = m * (b - m) * x / ((qam + m2) * (a + m2))
        d = 1.0 + aa * d
        if abs(d) < fpmin:
            d = fpmin
        c = 1.0 + aa / c
        if abs(c) < fpmin:
            c = fpmin
        d = 1.0 / d
        h *= d * c
        aa = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
        d = 1.0 + aa * d
        if abs(d) < fpmin:
            d = fpmin
        c = 1.0 + aa / c
        if abs(c) < fpmin:
            c = fpmin
        d = 1.0 / d
        delta = d * c
        h *= delta
        if abs(delta - 1.0) < 3e-16:
            break
    return h


def _beta_cdf(x: float, a: float, b: float) -> float:
    """Regularized incomplete beta `I_x(a, b)`."""
    if x <= 0.0:
        return 0.0
    if x >= 1.0:
        return 1.0
    ln_front = (math.lgamma(a + b) - math.lgamma(a) - math.lgamma(b)
                + a * math.log(x) + b * math.log1p(-x))
    front = math.exp(ln_front)
    # Use the continued fraction where it converges fast; else symmetry.
    if x < (a + 1.0) / (a + b + 2.0):
        return front * _betacf(a, b, x) / a
    return 1.0 - front * _betacf(b, a, 1.0 - x) / b


def _beta_ppf(p: float, a: float, b: float, *, tol: float = 1e-10) -> float:
    """Inverse of `_beta_cdf` in x, by bisection to `tol`."""
    if p <= 0.0:
        return 0.0
    if p >= 1.0:
        return 1.0
    lo, hi = 0.0, 1.0
    while hi - lo > tol:
        mid = (lo + hi) / 2
        if _beta_cdf(mid, a, b) < p:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


# --- record helpers ----------------------------------------------------

def _parse_ts(raw: object) -> dt.datetime | None:
    if not isinstance(raw, str):
        return None
    try:
        parsed = dt.datetime.fromisoformat(raw)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed


def _version_label(record: dict) -> str:
    v = record.get("endpoint_registry_version")
    return v if isinstance(v, str) and v else UNKNOWN_REGISTRY_VERSION


def _is_number(v: object) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _pick_group(records: list[dict], registry_version: str | None
                ) -> tuple[str | None, list[dict]]:
    groups: dict[str, list[dict]] = {}
    for r in records:
        groups.setdefault(_version_label(r), []).append(r)
    if registry_version is not None:
        if registry_version not in groups:
            raise ValueError(
                f"registry_version {registry_version!r} not in export "
                f"(have {sorted(groups)})")
        return registry_version, groups[registry_version]
    if not groups:
        return None, []
    floor = dt.datetime.min.replace(tzinfo=dt.timezone.utc)

    def newest(label: str) -> tuple[dt.datetime, str]:
        stamps = [_parse_ts(r.get("ts")) for r in groups[label]]
        return max((s for s in stamps if s is not None), default=floor), label

    label = max(groups, key=newest)
    return label, groups[label]


def _included(record: dict) -> bool:
    if record.get("cache_hit") is True:
        return False
    chosen = record.get("chosen")
    if not isinstance(chosen, str) or not chosen:
        return False
    return record.get("status") not in EXCLUDED_STATUSES


def _row_evidence(record: dict) -> tuple[float, int]:
    """`(successes, trials)` one included row contributes."""
    s, t = 0.0, 0
    for fb in record.get("feedback") or []:
        name, v = fb.get("score_name"), fb.get("value")
        if not _is_number(v):
            continue
        if name == "quality" and 0.0 <= v <= 1.0:
            s += float(v)
            t += 1
        elif name == "route_correct" and v in (0, 1):
            s += float(v)
            t += 1
    if record.get("status") == "verify_failed":
        t += 1
    return s, t


def _valid_q0(v: object) -> bool:
    """A usable prior mean: a finite number strictly inside (0, 1). At 0 or
    1 one Beta shape parameter is 0 and the prior is degenerate."""
    return _is_number(v) and 0.0 < v < 1.0


def _export_priors(records: list[dict]) -> dict[str, tuple[float, str]]:
    """`{model: (q0, src)}` from `scoring.candidates[*].quality`, restricted
    to `PRIOR_SOURCES`; the newest record wins (see module docstring)."""
    floor = dt.datetime.min.replace(tzinfo=dt.timezone.utc)
    best: dict[str, tuple[tuple, float, str]] = {}
    for r in records:
        scoring = r.get("scoring")
        if not isinstance(scoring, dict):
            continue
        key = (_parse_ts(r.get("ts")) or floor, str(r.get("request_id") or ""))
        for cand in scoring.get("candidates") or []:
            if not isinstance(cand, dict):
                continue
            name, q = cand.get("name"), cand.get("quality")
            if not isinstance(name, str) or not isinstance(q, dict):
                continue
            v, src = q.get("v"), q.get("src")
            if src not in PRIOR_SOURCES or not _valid_q0(v):
                continue
            if name not in best or key > best[name][0]:
                best[name] = (key, float(v), src)
    return {m: (v, src) for m, (_, v, src) in best.items()}


def _unit(model: str, workload: str | None, rows: list[dict],
          prior: tuple[float, str] | None, *, k: float, min_labels: int,
          max_ci_width: float, min_shift: float) -> dict:
    s, t, labelled = 0.0, 0, 0
    for r in rows:
        rs, rt = _row_evidence(r)
        s += rs
        t += rt
        labelled += 1 if rt else 0
    unit = {
        "model": model, "workload": workload,
        "q0": None, "q0_source": None, "alpha": None, "beta": None,
        "mean": None, "ci_low": None, "ci_high": None, "shift": None,
        "n_labels": t, "n_rows": len(rows),
        "label_coverage": labelled / len(rows) if rows else None,
        "status": STATUS_INSUFFICIENT, "reason": None,
    }
    if prior is None:
        unit["reason"] = "no prior"
        return unit
    q0, src = prior
    alpha = k * q0 + s
    beta = k * (1.0 - q0) + (t - s)
    mean = alpha / (alpha + beta)
    ci_low = _beta_ppf(_CI_LOW_P, alpha, beta)
    ci_high = _beta_ppf(_CI_HIGH_P, alpha, beta)
    shift = mean - q0
    unit.update(q0=q0, q0_source=src, alpha=alpha, beta=beta, mean=mean,
                ci_low=ci_low, ci_high=ci_high, shift=shift)
    width = round(ci_high - ci_low, 6)
    if t < min_labels:
        unit["reason"] = f"n_labels {t} < min_labels {min_labels}"
    elif width > max_ci_width:
        unit["reason"] = f"ci width {width} > max_ci_width {max_ci_width}"
    elif abs(round(shift, 6)) < min_shift:
        unit["status"] = STATUS_NO_CHANGE
        unit["reason"] = f"|shift| {abs(round(shift, 6))} < min_shift {min_shift}"
    else:
        unit["status"] = STATUS_RECOMMENDED
    return unit


# --- public API --------------------------------------------------------

def estimate(records: list[dict], *, k: float = 10.0, min_labels: int = 20,
             max_ci_width: float = 0.25, min_shift: float = 0.02,
             priors: dict[str, float] | None = None,
             registry_version: str | None = None) -> dict:
    """Per-(model, workload) and per-(model, *) Beta-posterior quality
    estimates for one registry-version group (spec §4)."""
    for model, v in (priors or {}).items():
        if not _valid_q0(v):
            raise ValueError(f"prior for {model!r} must be in (0, 1), got {v!r}")
    version, group = _pick_group(records, registry_version)
    export_priors = _export_priors(group)

    by_model: dict[str, list[dict]] = {}
    for r in group:
        if _included(r):
            by_model.setdefault(r["chosen"], []).append(r)

    opts = dict(k=k, min_labels=min_labels, max_ci_width=max_ci_width,
                min_shift=min_shift)
    units: list[dict] = []
    for model, rows in by_model.items():
        if priors and model in priors:
            prior: tuple[float, str] | None = (float(priors[model]), "prior")
        else:
            prior = export_priors.get(model)
        by_workload: dict[str, list[dict]] = {}
        for r in rows:
            w = r.get("workload")
            if isinstance(w, str) and w:
                by_workload.setdefault(w, []).append(r)
        for w, wrows in by_workload.items():
            units.append(_unit(model, w, wrows, prior, **opts))
        units.append(_unit(model, None, rows, prior, **opts))
    units.sort(key=lambda u: (u["model"], u["workload"] is None, u["workload"] or ""))

    return _round6({
        "registry_version": version,
        "options": {
            "k": float(k), "min_labels": min_labels,
            "max_ci_width": float(max_ci_width), "min_shift": float(min_shift),
            "ci_level": CI_LEVEL,
            "priors": ({m: float(v) for m, v in sorted(priors.items())}
                       if priors else None),
        },
        "units": units,
    })


def _canonical(rec: dict) -> str:
    return json.dumps({k: v for k, v in rec.items() if k != "set_id"},
                      sort_keys=True, separators=(",", ":"))


def recommendation_set_id(rec: dict) -> str:
    """First 16 hex chars of sha256 over the canonical JSON of `rec`
    without its `set_id` key. Task 5's serving-side validator carries a copy
    (serving never imports `app.eval`)."""
    return hashlib.sha256(_canonical(rec).encode("utf-8")).hexdigest()[:16]


def build_recommendation(estimate_dict: dict, *, export_sha256: str) -> dict:
    """The `recommendation.json` payload (spec §5). All units are carried;
    apply acts only on the `recommended` ones."""
    rec = _round6({
        "schema": RECOMMENDATION_SCHEMA,
        "export_sha256": export_sha256,
        "registry_version": estimate_dict["registry_version"],
        "options": estimate_dict["options"],
        "units": [dict(u) for u in estimate_dict["units"]],
    })
    rec["set_id"] = recommendation_set_id(rec)
    return rec


def dump_recommendation(rec: dict) -> str:
    """Byte-stable text for `recommendation.json`."""
    return json.dumps(rec, sort_keys=True, indent=2) + "\n"
