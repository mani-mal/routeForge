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
  - its **latest** `quality` label (by the feedback entry's own `ts`; ties
    go to the last in file order), when numeric and in [0, 1], adds that
    fraction as successes and 1 trial;
  - its latest `route_correct` label, when exactly 0 or 1, adds that value
    as successes and 1 trial;
  - a `verify_failed` row adds 0 successes and 1 trial, in addition to any
    label (it is a separate signal).
Only the latest label per score counts: a client posting the same label N
times for one request must not inflate `n_labels` and narrow the interval.
Non-dict feedback entries are skipped.
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

from app.eval.outcomes import UNKNOWN_REGISTRY_VERSION, UNTAGGED_WORKLOAD

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

LABEL_SCORES = ("quality", "route_correct")

# Iteration cap for `_betacf`; hitting it raises rather than returning an
# unconverged value (module-level so a test can shrink it).
_BETACF_MAX_ITER = 1000


def _round6(value):
    """Round every float leaf to 6 dp (as `outcomes._round6` does), and
    normalise `-0.0` to `0.0`: a signed zero serialises as `-0.0` and would
    change the canonical JSON, and so the `set_id`, for the same value."""
    if isinstance(value, bool):
        return value
    if isinstance(value, float):
        rounded = round(value, 6)
        return 0.0 if rounded == 0.0 else rounded
    if isinstance(value, dict):
        return {k: _round6(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_round6(v) for v in value]
    return value


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
    for m in range(1, _BETACF_MAX_ITER + 1):
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
            return h
    raise ArithmeticError(
        f"betacf did not converge in {_BETACF_MAX_ITER} iterations "
        f"(a={a}, b={b}, x={x})")


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
    """`(successes, trials)` one included row contributes: at most one
    trial per score in `LABEL_SCORES` -- the latest entry by its own `ts`
    (ties go to the last in file order) -- plus one failure trial when the
    row is `verify_failed`. Non-dict feedback entries are skipped."""
    floor = dt.datetime.min.replace(tzinfo=dt.timezone.utc)
    latest: dict[str, tuple[dt.datetime, object]] = {}
    for fb in record.get("feedback") or []:
        if not isinstance(fb, dict):
            continue
        name = fb.get("score_name")
        if name not in LABEL_SCORES:
            continue
        ts = _parse_ts(fb.get("ts")) or floor
        if name not in latest or ts >= latest[name][0]:
            latest[name] = (ts, fb.get("value"))
    s, t = 0.0, 0
    for name, (_, v) in latest.items():
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

def _validate_options(*, k: object, min_labels: object, max_ci_width: object,
                      min_shift: object) -> None:
    def finite(v: object) -> bool:
        return _is_number(v) and math.isfinite(v)

    if not finite(k) or k <= 0:
        raise ValueError(f"k must be a finite number > 0, got {k!r}")
    if not isinstance(min_labels, int) or isinstance(min_labels, bool) or min_labels < 0:
        raise ValueError(f"min_labels must be an int >= 0, got {min_labels!r}")
    for name, v in (("max_ci_width", max_ci_width), ("min_shift", min_shift)):
        if not finite(v) or v < 0:
            raise ValueError(f"{name} must be a finite number >= 0, got {v!r}")


def estimate(records: list[dict], *, k: float = 10.0, min_labels: int = 20,
             max_ci_width: float = 0.25, min_shift: float = 0.02,
             priors: dict[str, float] | None = None,
             registry_version: str | None = None) -> dict:
    """Per-(model, workload) and per-(model, *) Beta-posterior quality
    estimates for one registry-version group (spec §4)."""
    _validate_options(k=k, min_labels=min_labels, max_ci_width=max_ci_width,
                      min_shift=min_shift)
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


# --- replay diff (spec §5) ---------------------------------------------
#
# The replay re-ranks each logged decision with the *proposed* quality and
# reports which decisions would change. It reads ONLY the scoring record
# (`RankResult.to_record()`, app/routing/strategy.py) -- never the live
# config -- and it only reorders the logged eligible set, so it can never
# add a candidate or change eligibility or placement.
#
# The formula and tie-break mirror `DefaultRoutingStrategy.rank` exactly:
#
#   score = w.quality * q - w.cost * cost.norm
#           - w.latency * min(latency.ms / latency_scale_ms, max_latency_norm)
#           + w.availability * availability.v
#   order = sorted by (-score, cost.est_usd), stable
#
# The strategy has no "missing term" branch: every term is always present
# and its recorded value is what was used, whatever its `src` says
# (`peer_prior` carries the borrowed median; `disabled` carries the tier
# default / 1.0 the strategy actually scored with). So the replay uses the
# recorded values as-is, and a record that lacks one is *not recomputable*
# -- an equivalence failure, never a silent default.

REPLAY_CAVEAT = (
    "This replay shows which logged decisions would change. It cannot show "
    "what the changed decisions would have produced: production observes "
    "only the served model."
)

_WEIGHT_KEYS = ("quality", "cost", "latency", "availability")


class _NotRecomputable(ValueError):
    pass


def _num(obj: object, key: str, where: str) -> float:
    v = obj.get(key) if isinstance(obj, dict) else None
    if not _is_number(v) or not math.isfinite(v):
        raise _NotRecomputable(f"{where}.{key} missing or not a finite number")
    return v


def _parse_scoring(scoring: dict) -> tuple[dict, float, float, list[dict]]:
    weights = scoring.get("weights")
    w = {k: _num(weights, k, "weights") for k in _WEIGHT_KEYS}
    scale = _num(scoring, "latency_scale_ms", "scoring")
    if scale == 0:
        raise _NotRecomputable("scoring.latency_scale_ms is 0")
    cap = _num(scoring, "max_latency_norm", "scoring")
    cands = []
    for i, c in enumerate(scoring.get("candidates") or []):
        where = f"candidates[{i}]"
        if not isinstance(c, dict) or not isinstance(c.get("name"), str):
            raise _NotRecomputable(f"{where}.name missing")
        cands.append({
            "name": c["name"],
            "q": _num(c.get("quality"), "v", f"{where}.quality"),
            "lat_ms": _num(c.get("latency"), "ms", f"{where}.latency"),
            "avail": _num(c.get("availability"), "v", f"{where}.availability"),
            "cost_norm": _num(c.get("cost"), "norm", f"{where}.cost"),
            "est_usd": _num(c.get("cost"), "est_usd", f"{where}.cost"),
        })
    return w, scale, cap, cands


def _winner(w: dict, scale: float, cap: float, cands: list[dict],
            quality: dict[str, float]) -> str:
    """Mirror of `DefaultRoutingStrategy.rank`: same expression order (so
    float ties reproduce), same `(-score, est_cost)` stable sort."""
    scored = []
    for c in cands:
        lat_norm = min(c["lat_ms"] / scale, cap)
        score = (w["quality"] * quality.get(c["name"], c["q"])
                 - w["cost"] * c["cost_norm"]
                 - w["latency"] * lat_norm
                 + w["availability"] * c["avail"])
        scored.append((score, c["est_usd"], c["name"]))
    scored.sort(key=lambda s: (-s[0], s[1]))
    return scored[0][2]


def _proposed_units(recommendation: dict) -> dict[tuple[str, str | None], float]:
    out: dict[tuple[str, str | None], float] = {}
    for u in recommendation.get("units") or []:
        if u.get("status") != STATUS_RECOMMENDED or not _is_number(u.get("mean")):
            continue
        out[(u["model"], u.get("workload"))] = float(u["mean"])
    return out


def _latest_quality(record: dict) -> float | None:
    """The row's latest `quality` label in [0, 1] (same rule as
    `_row_evidence`), or None."""
    floor = dt.datetime.min.replace(tzinfo=dt.timezone.utc)
    best: tuple[dt.datetime, object] | None = None
    for fb in record.get("feedback") or []:
        if not isinstance(fb, dict) or fb.get("score_name") != "quality":
            continue
        ts = _parse_ts(fb.get("ts")) or floor
        if best is None or ts >= best[0]:
            best = (ts, fb.get("value"))
    if best is None:
        return None
    v = best[1]
    return float(v) if _is_number(v) and 0.0 <= v <= 1.0 else None


def _evidence(group: list[dict], model: str, workload: str | None):
    rows = [r for r in group
            if r.get("cache_hit") is not True and r.get("chosen") == model
            and r.get("workload") == workload]
    if not rows:
        return "no evidence"
    qualities = [q for q in (_latest_quality(r) for r in rows) if q is not None]
    costs = [float(r["cost_usd"]) for r in rows
             if r.get("status") == "ok" and _is_number(r.get("cost_usd"))]
    return {
        "served": len(rows),
        "quality_mean": sum(qualities) / len(qualities) if qualities else None,
        "verify_failed": sum(1 for r in rows if r.get("status") == "verify_failed"),
        "cost_mean": sum(costs) / len(costs) if costs else None,
    }


def replay(records: list[dict], recommendation: dict) -> dict:
    """Re-rank every logged decision in the recommendation's registry group
    twice -- unchanged (the equivalence check) and with the recommended
    qualities -- and report the per-workload diff (spec §5).

    Only `status == "recommended"` units apply. For a candidate the
    per-workload unit wins, else the `(model, *)` unit, else its logged
    quality. Records with no `scoring` (or an empty candidate list) are not
    decisions and are skipped (`n_skipped_no_scoring`). A scoring record
    that cannot be recomputed, or whose unchanged replay does not pick
    `scoring.candidates[0]`, is a mismatch and sets `equivalence_ok` False.
    """
    version = recommendation.get("registry_version")
    group = [r for r in records if _version_label(r) == version]
    proposed = _proposed_units(recommendation)

    mismatches: list[dict] = []
    skipped = 0
    per_wl: dict[str, dict] = {}
    new_picks: dict[str, set[str]] = {}
    for r in group:
        scoring = r.get("scoring")
        if not isinstance(scoring, dict) or not scoring.get("candidates"):
            skipped += 1
            continue
        rid = r.get("request_id")
        try:
            w, scale, cap, cands = _parse_scoring(scoring)
        except _NotRecomputable as exc:
            mismatches.append({"request_id": rid, "logged": None, "replayed": None,
                               "reason": f"not recomputable: {exc}"})
            continue
        logged = cands[0]["name"]
        unchanged = _winner(w, scale, cap, cands, {})
        if unchanged != logged:
            mismatches.append({"request_id": rid, "logged": logged,
                               "replayed": unchanged,
                               "reason": "unchanged replay picks a different candidate"})
            continue

        workload = r.get("workload")
        workload = workload if isinstance(workload, str) and workload else None
        quality: dict[str, float] = {}
        for c in cands:
            key = (c["name"], workload)
            if workload is not None and key in proposed:
                quality[c["name"]] = proposed[key]
            elif (c["name"], None) in proposed:
                quality[c["name"]] = proposed[(c["name"], None)]
        new = _winner(w, scale, cap, cands, quality)

        label = workload if workload is not None else UNTAGGED_WORKLOAD
        wl = per_wl.setdefault(label, {"decisions_total": 0, "decisions_changed": 0,
                                       "moves": {}, "_workload": workload})
        wl["decisions_total"] += 1
        if new != logged:
            wl["decisions_changed"] += 1
            move = f"{logged}->{new}"
            wl["moves"][move] = wl["moves"].get(move, 0) + 1
            new_picks.setdefault(label, set()).add(new)

    out_wl = {}
    for label in sorted(per_wl):
        wl = per_wl[label]
        out_wl[label] = {
            "decisions_total": wl["decisions_total"],
            "decisions_changed": wl["decisions_changed"],
            "moves": dict(sorted(wl["moves"].items())),
            "new_pick_evidence": {
                m: _evidence(group, m, wl["_workload"])
                for m in sorted(new_picks.get(label, ()))},
        }
    return _round6({
        "registry_version": version,
        "equivalence_ok": not mismatches,
        "mismatches": mismatches,
        "n_replayed": sum(wl["decisions_total"] for wl in per_wl.values()),
        "n_skipped_no_scoring": skipped,
        "per_workload": out_wl,
    })


# --- report ------------------------------------------------------------

def _fmt(value) -> str:
    if value is None:
        return "n/a"
    if isinstance(value, float):
        return f"{value:.6g}"
    return str(value)


def _wl(workload: str | None) -> str:
    return "*" if workload is None else workload


def render_report(estimate_dict: dict, replay_dict: dict) -> str:
    """`report.md`: `REPLAY_CAVEAT` verbatim first, then the recommended
    units, the no-change units, the insufficient units with their reasons,
    and the per-workload replay diff with each new pick's logged evidence."""
    units = estimate_dict.get("units") or []
    lines = [REPLAY_CAVEAT, "", "# Quality calibration recommendation", "",
             f"endpoint_registry_version: {_fmt(estimate_dict.get('registry_version'))}",
             ""]

    def section(title: str, status: str, cols: tuple[str, ...]) -> None:
        nonlocal lines
        rows = [u for u in units if u.get("status") == status]
        lines += [f"## {title} ({len(rows)})", ""]
        if not rows:
            lines += ["(none)", ""]
            return
        lines += ["| model | workload | " + " | ".join(cols) + " |",
                  "|" + "---|" * (2 + len(cols))]
        for u in rows:
            lines.append("| " + " | ".join(
                [u["model"], _wl(u.get("workload"))]
                + [_fmt(u.get(c)) for c in cols]) + " |")
        lines.append("")

    section("Recommended units", STATUS_RECOMMENDED,
            ("q0", "q0_source", "mean", "ci_low", "ci_high", "shift",
             "n_labels", "n_rows", "label_coverage"))
    section("No-change units", STATUS_NO_CHANGE,
            ("q0", "mean", "ci_low", "ci_high", "shift", "n_labels", "reason"))
    section("Insufficient units", STATUS_INSUFFICIENT,
            ("n_labels", "n_rows", "label_coverage", "reason"))

    per_wl = replay_dict.get("per_workload") or {}
    lines += ["## Replay diff", "",
              f"decisions replayed: {replay_dict.get('n_replayed', 0)}; "
              f"skipped (no scoring record): "
              f"{replay_dict.get('n_skipped_no_scoring', 0)}", "",
              "| workload | decisions_changed / decisions_total |", "|---|---|"]
    for label in sorted(per_wl):
        wl = per_wl[label]
        lines.append(f"| {label} | {wl['decisions_changed']} / {wl['decisions_total']} |")
    lines.append("")
    for label in sorted(per_wl):
        wl = per_wl[label]
        if not wl["moves"]:
            continue
        lines += [f"### {label}", "", "| move (from->to) | n |", "|---|---|"]
        lines += [f"| {m} | {n} |" for m, n in sorted(wl["moves"].items())]
        lines += ["", "| new pick | served | quality_mean | verify_failed | cost_mean |",
                  "|---|---|---|---|---|"]
        for m, ev in sorted(wl["new_pick_evidence"].items()):
            if ev == "no evidence":
                lines.append(f"| {m} | no evidence | | | |")
            else:
                lines.append(f"| {m} | {ev['served']} | {_fmt(ev['quality_mean'])} | "
                             f"{ev['verify_failed']} | {_fmt(ev['cost_mean'])} |")
        lines.append("")
    return "\n".join(lines) + "\n"
