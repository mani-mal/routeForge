"""P3c-1 shadow prices: relaxations and their deterministic enumeration
(spec §3).

Offline, operator-run tooling only -- this module is never imported by any
serving module (`tests/eval/test_isolation.py` enforces it).

A **relaxation** is one requirement, relaxed for one scope (an app, or an
onboarded endpoint for `endpoint_app_allowlist`). `enumerate_relaxations`
only ever *decides which relaxations exist* for a config snapshot and a set
of outcome rows -- it never applies anything. `Relaxation.apply` performs
the actual edit, but only when a caller invokes it explicitly, and always on
a deep copy: the input snapshot doc and rows are never mutated, and nothing
here re-simulates a row or scores a routing decision. That is Task 2's job
(`shadow_prices`), which calls `apply` per candidate and re-runs the P3a
replay engine.

**Scoping rule** (spec §3): every relaxation except `endpoint_app_allowlist`
is scoped to one app -- even the two relaxations that edit deployment-wide
data (`raw_sensitive`'s endpoint/provider/model-pool declarations,
`risk_floor`'s global `risk_floors` map). For those, the *doc* edit is still
global, but only the scoped app's rows are considered in scope by `apply`;
every other app's rows are left out, so the price is attributable to the one
app being priced. `endpoint_app_allowlist` is the one endpoint-scoped
relaxation: it scopes to (and re-simulates) every row, because any app's
requests could route to that endpoint.
"""
from __future__ import annotations

import copy
from dataclasses import dataclass
from datetime import datetime

from collections import Counter

from app.config.schema import PolicyCfg, Settings
from app.eval.outcomes import _fmt, _round6
from app.eval.scenario import app_rules, build_scenario, close_scenario
from app.eval.simulate import (
    RowResult,
    _before_since,
    _bucket_stats,
    _moves,
    _newly_rejected,
    _newly_served,
    _parse_ts,
    _route_changed,
    _window,
    observed_latencies,
    select_rows,
    simulate_row,
)
from app.eval.uncertainty import (UncertaintyPlan, insufficient_text, leaf, level_text,
                                  run_bootstrap, with_range)

from app.policy.core import (
    REQ_ALLOWED_MODELS,
    REQ_ALLOWED_PROVIDERS,
    REQ_ALLOWED_REGIONS,
    REQ_ALLOWED_TIERS,
    REQ_ENDPOINT_APP_ALLOWLIST,
    REQ_MAX_COST_USD_PER_REQUEST,
    REQ_RAW_SENSITIVE,
    REQ_REQUESTED_MODEL,
    REQ_REQUIRE_NO_RETENTION,
    REQ_RESIDENCY,
    REQ_RISK_FLOOR,
    REQUIREMENT_ORDER,
    SECURITY_IDS,
)
from app.policy.context import match_sensitivity_rule

__all__ = ["NEWLY_REJECTED_NOTE", "Relaxation", "SHADOW_CAVEAT", "ShadowInvariantError", "enumerate_relaxations",
           "render_shadow_section", "shadow_prices"]


@dataclass(frozen=True)
class Relaxation:
    requirement: str            # a REQUIREMENT_ORDER id
    app: str | None             # scoped app; None for endpoint_app_allowlist
    endpoint: str | None        # slug for endpoint_app_allowlist, else None
    relaxed_to: str             # human text for the report, e.g. "null", "any (escalation off)", "1e12", "cheap"

    @property
    def security(self) -> bool:
        return self.requirement in SECURITY_IDS

    def scope_key(self) -> str:
        if self.requirement == REQ_ENDPOINT_APP_ALLOWLIST:
            return f"endpoint:{self.endpoint}"
        return f"app:{self.app}"

    def apply(self, doc: dict, rows: list[dict]) -> tuple[dict, list[dict]]:
        """Return `(relaxed deep-copied doc, the in-scope rows, transformed
        where the relaxation is a row transform)`. Never mutates `doc` or
        `rows`."""
        relaxed = copy.deepcopy(doc)
        rows = copy.deepcopy(rows)

        _APPLIERS[self.requirement](self, relaxed)

        if self.requirement == REQ_ENDPOINT_APP_ALLOWLIST:
            scoped = rows
        else:
            scoped = [row for row in rows if row.get("application_id") == self.app]

        if self.requirement == REQ_REQUESTED_MODEL:
            for row in scoped:
                row["requested_model"] = None

        return relaxed, scoped


# --- doc edits (spec §3 table) -----------------------------------------------


def _find_app(doc: dict, app_id: str | None) -> dict:
    for entry in doc["apps"]:
        if entry["application_id"] == app_id:
            return entry
    raise KeyError(f"no app {app_id!r} in snapshot")


def _find_onboarded(doc: dict, slug: str | None) -> dict:
    for row in doc["onboarded"]:
        if row["slug"] == slug:
            return row
    raise KeyError(f"no onboarded endpoint {slug!r} in snapshot")


def _apply_residency(rel: Relaxation, doc: dict) -> None:
    policy = _find_app(doc, rel.app)["policy"]
    policy["residency"] = "any"
    policy["escalate_residency_on_detection"] = False


def _apply_null_field(field: str):
    def _apply(rel: Relaxation, doc: dict) -> None:
        _find_app(doc, rel.app)["policy"][field] = None
    return _apply


def _apply_require_no_retention(rel: Relaxation, doc: dict) -> None:
    _find_app(doc, rel.app)["policy"]["require_no_retention"] = False


def _is_declared_false(value: object) -> bool:
    """`accepts_raw_sensitive` is a bool on onboarded rows (the DB column)
    but a `"true" | "false" | "unknown"` string on `settings.providers.*`
    and `settings.routing.model_pool[*]` (the YAML declaration shape,
    P1b) -- `false` covers both. Strings are normalised the way
    `app/policy/feasible.py::_declared` reads them (stripped, lowercased),
    so `"False"` and `" FALSE "` count: serving enforces them as false."""
    if isinstance(value, bool):
        return value is False
    return value is not None and str(value).strip().lower() == "false"


def _flipped(value: object) -> object:
    """The true value in the declaration's own shape: bool `True` for a bool
    field, the string `"true"` for a string field."""
    if not _is_declared_false(value):
        return value
    return True if isinstance(value, bool) else "true"


def _apply_raw_sensitive(rel: Relaxation, doc: dict) -> None:
    for row in doc["onboarded"]:
        if _is_declared_false(row.get("accepts_raw_sensitive")):
            row["accepts_raw_sensitive"] = _flipped(row["accepts_raw_sensitive"])
    for provider in doc["settings"]["providers"].values():
        if _is_declared_false(provider.get("accepts_raw_sensitive")):
            provider["accepts_raw_sensitive"] = _flipped(provider["accepts_raw_sensitive"])
    for entry in doc["settings"]["routing"]["model_pool"]:
        if _is_declared_false(entry.get("accepts_raw_sensitive")):
            entry["accepts_raw_sensitive"] = _flipped(entry["accepts_raw_sensitive"])


def _apply_endpoint_app_allowlist(rel: Relaxation, doc: dict) -> None:
    _find_onboarded(doc, rel.endpoint)["allowed_app_ids"] = None


def _apply_requested_model(rel: Relaxation, doc: dict) -> None:
    pass  # row-only transform; handled in Relaxation.apply


def _apply_max_cost(rel: Relaxation, doc: dict) -> None:
    _find_app(doc, rel.app)["policy"]["max_cost_usd_per_request"] = 1e12


def _apply_risk_floor(rel: Relaxation, doc: dict) -> None:
    risk_floors = doc["settings"]["routing"]["risk_floors"]
    for key in risk_floors:
        risk_floors[key] = "cheap"


_APPLIERS = {
    REQ_RESIDENCY: _apply_residency,
    REQ_ALLOWED_PROVIDERS: _apply_null_field("allowed_providers"),
    REQ_ALLOWED_REGIONS: _apply_null_field("allowed_regions"),
    REQ_REQUIRE_NO_RETENTION: _apply_require_no_retention,
    REQ_RAW_SENSITIVE: _apply_raw_sensitive,
    REQ_ALLOWED_TIERS: _apply_null_field("allowed_tiers"),
    REQ_ALLOWED_MODELS: _apply_null_field("allowed_models"),
    REQ_ENDPOINT_APP_ALLOWLIST: _apply_endpoint_app_allowlist,
    REQ_REQUESTED_MODEL: _apply_requested_model,
    REQ_MAX_COST_USD_PER_REQUEST: _apply_max_cost,
    REQ_RISK_FLOOR: _apply_risk_floor,
}

_RELAXED_TO = {
    REQ_RESIDENCY: "any (escalation off)",
    REQ_ALLOWED_PROVIDERS: "null",
    REQ_ALLOWED_REGIONS: "null",
    REQ_REQUIRE_NO_RETENTION: "false",
    REQ_RAW_SENSITIVE: "true",
    REQ_ALLOWED_TIERS: "null",
    REQ_ALLOWED_MODELS: "null",
    REQ_ENDPOINT_APP_ALLOWLIST: "null",
    REQ_REQUESTED_MODEL: "unpinned",
    REQ_MAX_COST_USD_PER_REQUEST: "1e12",
    REQ_RISK_FLOOR: "cheap",
}


# --- enumeration (spec §3 "Candidates") --------------------------------------


def _any_raw_sensitive_false(doc: dict) -> bool:
    """`raw_sensitive` applies when any declaration anywhere -- onboarded
    rows, `settings.providers.*`, `settings.routing.model_pool[*]` -- is
    `false` (bool or the string `"false"`, per `_is_declared_false`)."""
    if any(_is_declared_false(row.get("accepts_raw_sensitive")) for row in doc["onboarded"]):
        return True
    if any(_is_declared_false(p.get("accepts_raw_sensitive"))
           for p in doc["settings"]["providers"].values()):
        return True
    return any(_is_declared_false(e.get("accepts_raw_sensitive"))
               for e in doc["settings"]["routing"]["model_pool"])


def _applies_residency(doc: dict, entry: dict, app_rows: list[dict]) -> bool:
    policy = entry.get("policy", {})
    residency = policy.get("residency")
    if residency is not None and residency != "any":
        return True
    escalate = policy.get("escalate_residency_on_detection")
    if escalate is False:
        return False
    return any(row.get("detected_entity_types") for row in app_rows)


def _applies_null_field(field: str):
    def _applies(doc: dict, entry: dict, app_rows: list[dict]) -> bool:
        return entry.get("policy", {}).get(field) is not None
    return _applies


def _applies_require_no_retention(doc: dict, entry: dict, app_rows: list[dict]) -> bool:
    return entry.get("policy", {}).get("require_no_retention") is True


def _applies_raw_sensitive(doc: dict, entry: dict, app_rows: list[dict]) -> bool:
    return _any_raw_sensitive_false(doc)


def _applies_requested_model(doc: dict, entry: dict, app_rows: list[dict]) -> bool:
    return any(row.get("requested_model") for row in app_rows)


def _applies_max_cost(doc: dict, entry: dict, app_rows: list[dict]) -> bool:
    return True


def _replay_risk_class(doc: dict, entry: dict, row: dict) -> str:
    """The risk class replay gives `row` -- recomputed from its
    `detected_entity_types` against the app's resolved sensitivity rules in
    THIS doc (`scenario.app_rules` + `match_sensitivity_rule`, exactly what
    `simulate_row` -> `build_context` uses), never the recorded
    `risk_class`, which reflects the rules at record time and ignores a
    proposal's edit. Only `settings.policy` is needed, so it is validated
    alone rather than building (and linting) a full `Settings`."""
    settings = Settings.model_construct(
        policy=PolicyCfg.model_validate(doc["settings"]["policy"]))
    rules = app_rules(entry, settings)
    etypes = set(row.get("detected_entity_types") or [])
    return match_sensitivity_rule(settings, rules, etypes).risk_class


def _applies_risk_floor(doc: dict, entry: dict, app_rows: list[dict]) -> bool:
    risk_floors = doc["settings"]["routing"]["risk_floors"]
    return any(risk_floors.get(_replay_risk_class(doc, entry, row), "cheap") != "cheap"
               for row in app_rows)


# Per-app checks, in `REQUIREMENT_ORDER` with `endpoint_app_allowlist`
# excluded -- it is not app-scoped and is enumerated separately below.
_APPLIES = {
    REQ_RESIDENCY: _applies_residency,
    REQ_ALLOWED_PROVIDERS: _applies_null_field("allowed_providers"),
    REQ_ALLOWED_REGIONS: _applies_null_field("allowed_regions"),
    REQ_REQUIRE_NO_RETENTION: _applies_require_no_retention,
    REQ_RAW_SENSITIVE: _applies_raw_sensitive,
    REQ_ALLOWED_TIERS: _applies_null_field("allowed_tiers"),
    REQ_ALLOWED_MODELS: _applies_null_field("allowed_models"),
    REQ_REQUESTED_MODEL: _applies_requested_model,
    REQ_MAX_COST_USD_PER_REQUEST: _applies_max_cost,
    REQ_RISK_FLOOR: _applies_risk_floor,
}

_APP_RULE_ORDER = tuple(r for r in REQUIREMENT_ORDER if r != REQ_ENDPOINT_APP_ALLOWLIST)


def enumerate_relaxations(doc: dict, rows: list[dict], *, cap: int = 50
                          ) -> tuple[list[Relaxation], int]:
    """The deterministic candidate list (spec §3): ordered by app id, then
    `REQUIREMENT_ORDER`, then endpoint slug -- built as two phases (every
    app's requirements, then every endpoint's) since `endpoint_app_allowlist`
    is not app-scoped. Only apps with at least one row in `rows` are
    considered. Truncated to `cap`; the second return value is how many
    candidates were cut."""
    rows_by_app: dict[str, list[dict]] = {}
    for row in rows:
        rows_by_app.setdefault(row.get("application_id"), []).append(row)

    candidates: list[Relaxation] = []
    for entry in sorted(doc["apps"], key=lambda a: a["application_id"]):
        app_id = entry["application_id"]
        app_rows = rows_by_app.get(app_id)
        if not app_rows:
            continue
        for req_id in _APP_RULE_ORDER:
            if _APPLIES[req_id](doc, entry, app_rows):
                candidates.append(Relaxation(req_id, app_id, None, _RELAXED_TO[req_id]))

    for row in sorted(doc["onboarded"], key=lambda o: o["slug"]):
        if row.get("allowed_app_ids") is not None:
            candidates.append(Relaxation(
                REQ_ENDPOINT_APP_ALLOWLIST, None, row["slug"],
                _RELAXED_TO[REQ_ENDPOINT_APP_ALLOWLIST]))

    skipped = max(0, len(candidates) - cap)
    return candidates[:cap], skipped


# --- evaluation (spec §3 invariant, §4 metrics, §5 block) ---------------------


class ShadowInvariantError(RuntimeError):
    """A relaxation narrowed a row's eligible set. Every relaxation only ever
    widens what `feasible_set` admits (spec §3), so this is an
    internal-invariant failure, never a reportable result: the CLI exits 2
    on it.

    It is the ONLY hard invariant. A relaxation may still newly REJECT a
    baseline-routed row -- the router is not monotone in the feasible set
    (rule 8 clamps the target tier to the best allowed tier, a wider allowed
    set can lift it to the risk floor, and the budget fallback never goes
    below the floor) -- so that is reported as `newly_rejected`, not raised
    (spec §3, as-built amendment 2026-09-26)."""


def _check_eligible_superset(rel: Relaxation, request_id: str, base, relaxed) -> None:
    missing = sorted(set(base.eligible) - set(relaxed.eligible))
    if missing:
        raise ShadowInvariantError(
            f"{rel.requirement}@{rel.scope_key()}: row {request_id} lost eligible "
            f"candidate(s) {missing} under the relaxation")


def _newly_rejected_reasons(results: list[RowResult]) -> dict[str, int]:
    """`{reject code: rows}` over the rows routed at baseline and rejected
    under the relaxation, keyed by the relaxed arm's `reject_codes` (a row
    with several codes counts once under each), sorted by code."""
    counts: Counter = Counter()
    for r in results:
        if _newly_rejected(r):
            for code in r.proposed.reject_codes or ("unspecified",):
                counts[code] += 1
    return dict(sorted(counts.items()))


def _renamed(delta: dict) -> dict:
    """P3a's `{current, proposed, delta}` in the shadow block's words."""
    return {"baseline": delta["current"], "relaxed": delta["proposed"], "delta": delta["delta"]}


def _window_days(results: list[RowResult]) -> float | None:
    window = _window(results)
    if window is None:
        return None
    days = (_parse_ts(window["end"]) - _parse_ts(window["start"])).total_seconds() / 86400
    return days if days >= 1 else None


def _entry(rel: Relaxation, results: list[RowResult], stats: dict,
           days: float | None) -> dict:
    """`days` is the EXPORT window (every selected row, spec §4), the same
    for every relaxation -- not the window of this relaxation's scoped rows."""
    both = _renamed(stats["cost_usd_both_routed"])
    # Like-for-like (plan ruling): only rows routed in BOTH arms count, so a
    # row the relaxation newly serves is not read as added cost.
    per_day = -both["delta"] / days if days is not None and both["delta"] is not None else None
    scope = {"endpoint": rel.endpoint} if rel.requirement == REQ_ENDPOINT_APP_ALLOWLIST \
        else {"app": rel.app}
    changed = [r for r in results
               if _route_changed(r) or _newly_served(r) or _newly_rejected(r)]
    return {
        "requirement": rel.requirement,
        "scope": scope,
        "security": rel.security,
        "relaxed_to": rel.relaxed_to,
        "rows_in_scope": stats["requests"],
        "route_changed": stats["route_changed"],
        "rejections_avoided": stats["newly_served"],
        "newly_rejected": stats["newly_rejected"],
        "newly_rejected_reasons": _newly_rejected_reasons(results),
        "cost_usd": _renamed(stats["cost_usd"]),
        "cost_usd_both_routed": both,
        "cost_saved_per_day": per_day,
        "quality_mean_delta": stats["quality_mean"]["delta"],
        "latency_p50_delta": stats["latency_ms"]["p50"]["delta"],
        "onprem_share": {"baseline": stats["onprem_share"]["current"],
                         "relaxed": stats["onprem_share"]["proposed"]},
        "affected_apps": sorted({r.row["application_id"] for r in changed}),
        "moves": _moves(results)[:5],
    }


def _entry_ranges(entry: dict, results: list[RowResult], plan: UncertaintyPlan,
                   days: float | None) -> dict:
    """P3c-3: the ranges of one binding relaxation's figures (spec §5.3),
    bootstrapped over the rows it re-simulated: `route_changed`,
    `rejections_avoided`, `newly_rejected`, the like-for-like cost delta
    and, with a window of at least a day, `cost_saved_per_day`. Each leaf's
    `point` is the entry's own figure. Fewer than `plan.min_rows` rows in
    scope: `insufficient_data` and no range.

    The draws are the ones every other part of the run uses for those rows,
    so a relaxation's range and the proposal's are over the same resamples:
    one application's rows are that application's stratum, and an
    endpoint-scoped relaxation, which re-simulates every row, is resampled
    in the export's own strata (small applications pooled). `totals_strata`
    says which, as `uncertainty.results_block` does."""
    head = {"requirement": entry["requirement"], "scope": entry["scope"],
            "security": entry["security"], "n": entry["rows_in_scope"]}
    # Only the totals of the rows in scope are read: no per-app figures.
    boot = run_bootstrap(results, plan, totals_only=True)
    if boot.totals is None:
        return {**head, "insufficient_data": True}
    both = boot.totals["cost_usd_both_routed.delta"]
    return {
        **head,
        "totals_strata": boot.strata,
        "route_changed": leaf(entry["route_changed"], boot.totals["route_changed"], plan),
        "rejections_avoided": leaf(entry["rejections_avoided"],
                                   boot.totals["newly_served"], plan),
        "newly_rejected": leaf(entry["newly_rejected"], boot.totals["newly_rejected"], plan),
        "cost_usd_both_routed_delta": leaf(entry["cost_usd_both_routed"]["delta"], both, plan),
        # The entry's own arithmetic: minus the like-for-like delta, per day.
        "cost_saved_per_day": (leaf(entry["cost_saved_per_day"], [-v / days for v in both], plan)
                               if days is not None else None),
    }


def _rank_key(entry: dict) -> tuple:
    saved = entry["cost_saved_per_day"]
    if saved is None:
        saved = -(entry["cost_usd_both_routed"]["delta"] or 0.0)
    scope = entry["scope"]
    scope_key = f"endpoint:{scope['endpoint']}" if "endpoint" in scope else f"app:{scope['app']}"
    return (-saved, -entry["rejections_avoided"], entry["requirement"], scope_key)


async def shadow_prices(records: list[dict], baseline_doc: dict, *, base: Settings,
                        cap: int = 50, baseline_label: str = "snapshot",
                        since: datetime | None = None,
                        uncertainty: UncertaintyPlan | None = None) -> dict:
    """The spec §5 `shadow_prices` block for `baseline_doc` (the snapshot,
    or the snapshot with a proposal already applied -- the caller decides and
    says which in `baseline_label`).

    The baseline scenario is built and simulated once. Each relaxation then
    builds its own scenario and re-simulates only its in-scope rows (spec §3
    scoping rule); every re-simulated row is checked against the
    eligible-superset invariant, and a breach raises `ShadowInvariantError`.
    A baseline-routed row the relaxation rejects is reported
    (`newly_rejected`, `newly_rejected_reasons`), not raised. A relaxation
    is binding when it changes a route, avoids a rejection or newly rejects
    a row; the rest are counted as `non_binding`, not listed. `since` cuts
    the export exactly as `simulate.run_rows` does (P3c-2).

    `uncertainty` (P3c-3) adds ONE key to the block, `uncertainty`:
    `{"interval", "constraints"}`, the second a list parallel to
    `constraints` (same order) of `_entry_ranges`. Without a plan the block
    is exactly what it was."""
    observed = observed_latencies([r for r in records if not _before_since(r, since)])
    baseline = await build_scenario(baseline_doc, base=base)
    try:
        rows, _excluded = select_rows(records, set(baseline.apps), since=since)
        base_arm = {row["request_id"]: simulate_row(baseline, row, observed) for row in rows}
        # The export window spans every selected row, whichever app sent it
        # (spec §4); one value serves every relaxation's per-day figure.
        days = _window_days([RowResult(row, base_arm[row["request_id"]],
                                       base_arm[row["request_id"]]) for row in rows])
        rels, skipped = enumerate_relaxations(baseline_doc, rows, cap=cap)

        constraints: list[dict] = []
        ranges: dict[int, dict] = {}        # id(entry) -> its ranges
        non_binding = 0
        for rel in rels:
            doc_r, scoped = rel.apply(baseline_doc, rows)
            relaxed = await build_scenario(doc_r, base=base)
            try:
                results: list[RowResult] = []
                for row in scoped:
                    request_id = row["request_id"]
                    ra = simulate_row(relaxed, row, observed)
                    _check_eligible_superset(rel, request_id, base_arm[request_id], ra)
                    results.append(RowResult(row, base_arm[request_id], ra))
            finally:
                await close_scenario(relaxed)

            stats = _bucket_stats(results)
            if stats["route_changed"] + stats["newly_served"] + stats["newly_rejected"] > 0:
                entry = _round6(_entry(rel, results, stats, days))
                constraints.append(entry)
                if uncertainty is not None:
                    ranges[id(entry)] = _entry_ranges(entry, results, uncertainty, days)
            else:
                non_binding += 1

        constraints.sort(key=_rank_key)
        block = {"baseline": baseline_label, "evaluated": len(rels),
                 "skipped_by_cap": skipped, "non_binding": non_binding,
                 "constraints": constraints}
        if uncertainty is not None:
            # Looked up by identity AFTER the sort, so the two lists stay
            # parallel whatever the order is.
            block["uncertainty"] = {"interval": uncertainty.interval,
                                    "constraints": [ranges[id(e)] for e in constraints]}
        return _round6(block)
    finally:
        await close_scenario(baseline)


# --- render (spec §5 "report.md section") -------------------------------------

#: The shadow-prices section's opening blockquote, verbatim from spec §5, one
#: string per source line (without the leading "> " -- `render_shadow_section`
#: adds that on every line, the same convention as `simulate.REPORT_CAVEAT`).
SHADOW_CAVEAT = (
    "**What each constraint costs.** Each row relaxes ONE constraint for one app "
    "(or endpoint) and\n"
    "replays the same recorded traffic. Nothing is applied. Security constraints "
    "are shown as the\n"
    "cost of a protection — they are never a recommendation to relax them."
)

#: Printed under the shadow "Ranges" table (P3c-3 spec §10.3).
RANGE_STRATA_NOTE = (
    "A relaxation scoped to one application is resampled as that application's rows; an "
    "endpoint-scoped one over every row, with small applications resampled together "
    "(`totals_strata` on each entry of `shadow_prices.uncertainty.constraints`). Resampling "
    "within strata leaves a total about the `variance_factor` share of its variance "
    "(analytic, equal variance assumed, not measured); ranges are not corrected for it."
)

#: The sentence under the two tables (final-fix ruling C1, verbatim).
NEWLY_REJECTED_NOTE = (
    "Relaxing a constraint can newly reject a row: a wider allowed set can lift "
    "the target to the risk floor, and the budget fallback never goes below the floor."
)

_PROTECTION_LABEL = "price of a protection — not a recommendation"

_SHADOW_COLUMNS = ("requirement", "scope", "rows", "route changed", "rejections avoided",
                   "newly rejected",
                   "cost saved/day (or Δcost both-routed)", "Δquality",
                   "Δp50 ms", "on-prem share (baseline→relaxed)")


def _scope_str(scope: dict) -> str:
    key, value = next(iter(scope.items()))
    return f"{key}:{value}"


def _cost_cell(entry: dict) -> str:
    per_day = entry["cost_saved_per_day"]
    if per_day is not None:
        return f"{_fmt(per_day)}/day"
    return f"{_fmt(entry['cost_usd_both_routed']['delta'])} (both-routed)"


def _shadow_row(entry: dict, *, security: bool) -> str:
    onprem = entry["onprem_share"]
    cells = [
        entry["requirement"],
        _scope_str(entry["scope"]),
        str(entry["rows_in_scope"]),
        str(entry["route_changed"]),
        str(entry["rejections_avoided"]),
        str(entry["newly_rejected"]),
        _cost_cell(entry),
        _fmt(entry["quality_mean_delta"]),
        _fmt(entry["latency_p50_delta"]),
        f"{_fmt(onprem['baseline'])}→{_fmt(onprem['relaxed'])}",
    ]
    if security:
        cells.append(_PROTECTION_LABEL)
    return "| " + " | ".join(cells) + " |"


def _shadow_table(entries: list[dict], *, security: bool) -> list[str]:
    if not entries:
        return ["None."]
    columns = list(_SHADOW_COLUMNS) + (["label"] if security else [])
    lines = ["| " + " | ".join(columns) + " |",
             "|" + "---|" * len(columns)]
    lines += [_shadow_row(e, security=security) for e in entries]
    return lines


_RANGE_COLUMNS = ("requirement", "scope", "rows", "cost saved/day (or Δcost both-routed)",
                  "rejections avoided", "newly rejected", "route changed", "label")


def _range_row(entry: dict) -> str:
    """One relaxation's ranges. A security requirement keeps its label here
    too: a range on the cost of a protection is still not a recommendation."""
    cells = [entry["requirement"], _scope_str(entry["scope"]), str(entry["n"])]
    if entry.get("insufficient_data"):
        cells += [insufficient_text(entry), "", "", ""]
    else:
        per_day = entry["cost_saved_per_day"]
        if per_day is not None:
            cost = with_range(f"{_fmt(per_day['point'])}/day", per_day)
        else:
            both = entry["cost_usd_both_routed_delta"]
            cost = with_range(f"{_fmt(both['point'])} (both-routed)", both)
        cells += [cost] + [with_range(str(entry[key]["point"]), entry[key])
                           for key in ("rejections_avoided", "newly_rejected", "route_changed")]
    cells.append(_PROTECTION_LABEL if entry["security"] else "")
    return "| " + " | ".join(cells) + " |"


def _range_lines(ranges: dict) -> list[str]:
    """The "Ranges" subsection, in the block's own order."""
    lines = ["### Ranges", "",
             f"`[lo, hi]` is the {level_text(ranges['interval'])} range of the figure before "
             "it: sampling variability only (see \"Uncertainty ranges\").", ""]
    if not ranges["constraints"]:
        return lines + ["None.", ""]
    lines += ["| " + " | ".join(_RANGE_COLUMNS) + " |", "|" + "---|" * len(_RANGE_COLUMNS)]
    lines += [_range_row(entry) for entry in ranges["constraints"]]
    lines += ["", RANGE_STRATA_NOTE, ""]
    return lines


def render_shadow_section(block: dict) -> str:
    """The `report.md` "Shadow prices" section (spec §5): the caveat, then
    the operational-constraints table, then the cost-of-protection table
    (every row labelled, never a recommendation), then the newly-rejected
    note, then (P3c-3, only when the block carries ranges) the ranges, then
    a non-binding/skipped footer."""
    lines: list[str] = ["## Shadow prices", ""]
    lines += ["> " + line for line in SHADOW_CAVEAT.split("\n")]
    lines.append("")

    constraints = block["constraints"]
    operational = [c for c in constraints if not c["security"]]
    protection = [c for c in constraints if c["security"]]

    lines.append("### Operational constraints")
    lines.append("")
    lines += _shadow_table(operational, security=False)
    lines.append("")

    lines.append("### Cost of protection")
    lines.append("")
    lines += _shadow_table(protection, security=True)
    lines.append("")

    lines.append(NEWLY_REJECTED_NOTE)
    lines.append("")

    if block.get("uncertainty") is not None:
        lines += _range_lines(block["uncertainty"])

    lines.append(f"Non-binding: {block['non_binding']} · "
                f"skipped by cap: {block['skipped_by_cap']}")

    return "\n".join(lines) + "\n"
