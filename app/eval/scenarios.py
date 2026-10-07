"""P3c-2: alternative scenarios, compared side by side (offline, operator-run).

An administrator writes several candidate policy changes into one
`routeforge.policy-scenarios/v1` file. Each scenario is a named
`routeforge.policy-proposal/v1` BODY; every one is replayed over the same
recorded traffic against the same `current` arm (`app/eval/simulate.py`).

RouteForge never picks, ranks or recommends a scenario: they are reported in
file order, and nothing is applied (handoff §28).

With a `CapacityPlan` (P3c-4) every scenario also gets a capacity block
(`app/eval/capacity.py`) under one `capacity` key, and the table one more
column: the proposed arm's most loaded stated limit. That is a figure, not
an ordering; the table stays in file order.

With an `UncertaintyPlan` (P3c-3) every scenario also gets ranges
(`app/eval/uncertainty.py`), under one `uncertainty` key. Every scenario is
resampled with the same draws, so the ranges of two scenarios are over the
same resampled traffic. A range is not a ranking either.

Never imported by serving code (`tests/eval/test_isolation.py`).
"""
from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from app.config.schema import Settings
from app.eval.capacity import (PEAK_COLUMN_NOTE, CapacityPlan, capacity_block, intro_lines,
                               peak_text, scenario_lines)
from app.eval.outcomes import _fmt, _round6
from app.eval.proposal import (SCHEMA as PROPOSAL_SCHEMA, ProposalError, _validate_keys,
                               _validate_schema, apply_proposal, assumptions_block,
                               normalized_edits)
from app.eval.scenario import build_scenario, close_scenario
from app.eval.simulate import (CACHED_SHARE_NOTE, REPORT_CAVEAT, _assumption_items,
                               _excluded_lines, _fidelity_notes, _metrics_table,
                               _move_lines, _new_rejection_lines, _per_day, arm_results,
                               run_rows, summarize)
from app.eval.uncertainty import (OUTSIDE_FOOTNOTE, ZERO_RANGE_NOTE, UncertaintyPlan,
                                  _count_outside, app_sizes, bucket_lines, caveat_lines,
                                  cost_line, insufficient_text, method_line, min_max_line,
                                  outside_line, plan_block, quality_line, results_block,
                                  strata_line, table_ranges, token_estimate_line, window_days,
                                  with_range)

SCHEMA = "routeforge.policy-scenarios/v1"
COMPARISON_SCHEMA = "routeforge.scenario-comparison/v1"
_SCHEMA_PREFIX = "routeforge.policy-scenarios/"
_SCHEMA_MAJOR = "v1"

_TOP_KEYS = frozenset({"schema", "title", "scenarios"})
_SCENARIO_KEYS = frozenset({"name", "proposal"})
MAX_SCENARIOS = 20
MAX_NAME_LEN = 64
# Matched with `fullmatch`, never `match` + `$`: `$` also matches before a
# trailing newline, which would let "name\n" through.
_NAME = re.compile(r"[A-Za-z0-9._+-]+")


class ScenarioError(ValueError):
    """A scenarios-file load/validate error. Always names the offending
    location -- `scenarios[<i>](<name>).proposal.<dotted path>` for an error
    inside a scenario's proposal -- mirroring `ProposalError`."""

    def __init__(self, where: str, message: str) -> None:
        self.where = where
        self.message = message
        super().__init__(f"{where}: {message}")


@dataclass(frozen=True)
class AppliedScenario:
    name: str
    proposal: dict      # the scenario's proposal body as a full v1 proposal
    doc: dict           # the snapshot with that proposal applied


def _scenario_where(index: int, name: str) -> str:
    return f"scenarios[{index}]({name})"


def validate_scenarios(doc: object) -> None:
    """Shape-only checks (no snapshot needed): the schema, the closed key
    sets, 1..20 scenarios, legal unique names, and every proposal body's own
    keys. Values are checked against the snapshot by `apply_scenarios`."""
    if not isinstance(doc, dict):
        raise ScenarioError("<root>", "must be an object")
    schema = doc.get("schema")
    if not isinstance(schema, str) or not schema.startswith(_SCHEMA_PREFIX):
        raise ScenarioError("schema", f"must start with {_SCHEMA_PREFIX!r}; got {schema!r}")
    major = schema[len(_SCHEMA_PREFIX):].split(".", 1)[0]
    if major != _SCHEMA_MAJOR:
        raise ScenarioError("schema", f"major version must be {_SCHEMA_MAJOR!r}; got {major!r}")
    for key in doc:
        if key not in _TOP_KEYS:
            raise ScenarioError(key, "unknown key")
    title = doc.get("title")
    if title is not None and not isinstance(title, str):
        raise ScenarioError("title", "must be a string")

    scenarios = doc.get("scenarios")
    if not isinstance(scenarios, list):
        raise ScenarioError("scenarios", "must be a list")
    if not 1 <= len(scenarios) <= MAX_SCENARIOS:
        raise ScenarioError(
            "scenarios", f"must list 1 to {MAX_SCENARIOS} scenarios; got {len(scenarios)}")

    seen: set[str] = set()
    for i, entry in enumerate(scenarios):
        if not isinstance(entry, dict):
            raise ScenarioError(f"scenarios[{i}]", "must be an object")
        for key in entry:
            if key not in _SCENARIO_KEYS:
                raise ScenarioError(f"scenarios[{i}].{key}", "unknown key")
        name = entry.get("name")
        if (not isinstance(name, str) or not _NAME.fullmatch(name)
                or len(name) > MAX_NAME_LEN):
            raise ScenarioError(
                f"scenarios[{i}].name",
                f"must be 1 to {MAX_NAME_LEN} characters of [A-Za-z0-9._+-]; got {name!r}")
        if name in seen:
            raise ScenarioError(f"scenarios[{i}].name", f"duplicate scenario name {name!r}")
        seen.add(name)

        where = f"{_scenario_where(i, name)}.proposal"
        body = entry.get("proposal")
        if not isinstance(body, dict):
            raise ScenarioError(where, "must be an object")
        try:
            # `schema` is optional on a body; when present it must be a v1
            # proposal schema, checked exactly as a proposal file's is.
            if "schema" in body:
                _validate_schema(body)
            _validate_keys(body)
        except ProposalError as exc:
            raise ScenarioError(f"{where}.{exc.where}", exc.message) from exc


def load_scenarios(path: str) -> tuple[dict, str]:
    """Read a scenarios JSON file. Returns `(doc, sha256 hex)` -- the hash is
    over the raw file bytes. Raises `ScenarioError` on invalid JSON, a bad
    schema, an unknown key anywhere, or a bad scenario name."""
    with open(path, "rb") as f:
        raw = f.read()
    digest = hashlib.sha256(raw).hexdigest()
    try:
        doc = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ScenarioError("file", f"invalid JSON: {exc}") from exc
    validate_scenarios(doc)
    return doc, digest


def apply_scenarios(snapshot: dict, doc: dict, *, base: Settings) -> list[AppliedScenario]:
    """Validate EVERY scenario's proposal against `snapshot` and apply it,
    in file order. All-or-nothing: the first invalid scenario raises
    `ScenarioError` naming its index, name and dotted path, before any
    replay starts. Never mutates `snapshot` or `doc`."""
    validate_scenarios(doc)
    applied: list[AppliedScenario] = []
    for i, entry in enumerate(doc["scenarios"]):
        proposal = {"schema": PROPOSAL_SCHEMA, **entry["proposal"]}
        try:
            applied_doc = apply_proposal(snapshot, proposal, base=base)
        except ProposalError as exc:
            raise ScenarioError(
                f"{_scenario_where(i, entry['name'])}.proposal.{exc.where}",
                exc.message) from exc
        applied.append(AppliedScenario(entry["name"], proposal, applied_doc))
    return applied


# --- engine (spec §7) ----------------------------------------------------------

# The parts of a P3a `simulation.json` body that are NOT per scenario: the
# first three belong to the document as a whole, `rows` and `baseline` are
# the shared `current` arm's and are lifted to the top level, and `edits` /
# `assumptions` sit beside each scenario's `summary`.
_LIFTED = ("schema", "simulation_id", "inputs", "rows", "baseline", "edits", "assumptions")


class ComparisonInvariantError(RuntimeError):
    """Two scenarios disagreed about something they share by construction --
    the selected rows, the excluded counts or the `current` arm's baseline.
    Never a reportable result: it means a bug, and the CLI exits 2."""


@dataclass(frozen=True)
class Comparison:
    rows: dict             # {"simulated": N, "excluded": {...}} -- the same for every scenario
    baseline: dict         # the `current` arm's fidelity block
    window: dict | None    # {"start", "end"} over the simulated rows' `ts`
    scenarios: list[dict]  # file order: {"name", "edits", "assumptions", "summary"}
    # P3c-3: `plan_block` plus `scenarios`, one `{"name", ...results_block,
    # "table"}` per scenario in file order. None without a plan.
    uncertainty: dict | None = None
    # P3c-4: `{"scenarios": [{"name", ...capacity_block}]}` in file order.
    # None without a plan.
    capacity: dict | None = None


async def compare(records: list[dict], snapshot: dict, scenarios_doc: dict, *,
                  base: Settings, since: datetime | None = None,
                  uncertainty: UncertaintyPlan | None = None,
                  notify: Callable[[str], None] | None = None,
                  capacity: CapacityPlan | None = None) -> Comparison:
    """Replay every scenario against ONE shared `current` arm (spec §7).

    Every proposal is validated and applied before anything is replayed
    (`apply_scenarios`, all-or-nothing). The `current` scenario is built and
    simulated once; each proposed scenario is then built, replayed and
    closed in file order, so at most one proposed scenario is open at a
    time. Nothing is ranked, and nothing is applied to a deployment.

    `uncertainty` (P3c-3) adds each scenario's ranges, bootstrapped from the
    rows it was just replayed over -- no row is simulated a second time.
    `notify` is told, once, before the first bootstrap, what all of them
    will cost (`uncertainty.cost_line`).

    `capacity` (P3c-4) adds each scenario's capacity block, computed from
    the same replayed rows against the plan's base limits and the
    scenario's own `capacity` / `workload.volume_scale`."""
    applied = apply_scenarios(snapshot, scenarios_doc, base=base)
    current = await build_scenario(snapshot, base=base)
    try:
        current_results = arm_results(records, current, since=since)
        shared: tuple[dict, dict, dict | None] | None = None
        entries: list[dict] = []
        ranges: list[dict] = []
        loads: list[dict] = []
        for item in applied:
            proposed = await build_scenario(item.doc, base=base)
            try:
                # A proposal only edits apps in place (spec §7.4), so every
                # scenario simulates exactly the snapshot's apps.
                if set(proposed.apps) != set(current.apps):
                    raise ComparisonInvariantError(
                        f"scenario {item.name!r} changed the set of applications")
                results, excluded = run_rows(records, current, proposed, since=since,
                                             current_results=current_results)
            finally:
                await close_scenario(proposed)
            sim = summarize(results, excluded, inputs={},
                            edits=normalized_edits(item.proposal),
                            assumptions=assumptions_block(item.proposal))
            head = (sim["rows"], sim["baseline"], sim["inputs"]["window"])
            if shared is None:
                shared = head
            elif head != shared:
                raise ComparisonInvariantError(
                    f"scenario {item.name!r} saw different rows or a different baseline")
            entries.append({
                "name": item.name,
                "edits": sim["edits"],
                "assumptions": sim["assumptions"],
                "summary": {k: v for k, v in sim.items() if k not in _LIFTED},
            })
            if uncertainty is not None:
                if notify is not None and not ranges:
                    # Every scenario replays the same rows: one pass each.
                    notify(cost_line(uncertainty, [app_sizes(results)] * len(applied)))
                block = results_block(results, uncertainty,
                                      days=window_days(sim["inputs"]["window"]))
                ranges.append({"name": item.name, **block, "table": table_ranges(block)})
            if capacity is not None:
                loads.append({"name": item.name,
                              **capacity_block(results, capacity, item.proposal,
                                               excluded=excluded)})
    finally:
        await close_scenario(current)
    rows, baseline, window = shared      # a file always holds at least one scenario
    return Comparison(
        rows=rows, baseline=baseline, window=window, scenarios=entries,
        uncertainty=(None if uncertainty is None
                     else {**plan_block(uncertainty), "scenarios": ranges}),
        capacity=None if capacity is None else {"scenarios": loads})


# --- output (spec §8) ----------------------------------------------------------

#: Printed under the comparison table, verbatim (spec §8, invariant S8).
NO_RANKING_NOTE = "Listed in file order. RouteForge does not rank or recommend scenarios."

#: Why the table's two cost columns can be read across scenarios.
LIKE_FOR_LIKE_NOTE = (
    "Cost columns compare only the rows routed in both arms. A row a scenario newly rejects "
    "is counted under \"newly rejected\", never as a saving."
)


def _table_row(entry: dict, window: dict | None, capacity: dict | None = None) -> dict:
    """One comparison-table row. Exactly these keys -- there is no rank,
    score or recommendation field, by design (S8). With a capacity block
    (P3c-4) three more: the proposed arm's most loaded stated limit, all
    None when no limit is stated or the window is too short."""
    row = _plain_table_row(entry, window)
    if capacity is not None:
        peak = capacity["peak"]["proposed"] or {}
        row["peak_utilisation"] = peak.get("utilisation")
        row["peak_endpoint"] = peak.get("endpoint")
        row["peak_limit"] = peak.get("limit")
    return row


def _plain_table_row(entry: dict, window: dict | None) -> dict:
    summary = entry["summary"]
    totals = summary["totals"]
    like_for_like = totals["cost_usd_both_routed"]
    per_day = _per_day(window, like_for_like)
    return {
        "name": entry["name"],
        "cost_per_day_delta_usd": per_day["cost_usd"]["delta"] if per_day else None,
        "cost_delta_usd": like_for_like["delta"],
        "route_changed": totals["route_changed"],
        "newly_rejected": totals["newly_rejected"],
        "newly_served": totals["newly_served"],
        "latency_p50_delta_ms": totals["latency_ms"]["p50"]["delta"],
        "latency_p95_delta_ms": totals["latency_ms"]["p95"]["delta"],
        "quality_mean_delta": totals["quality_mean"]["delta"],
        "onprem_share_current": totals["onprem_share"]["current"],
        "onprem_share_proposed": totals["onprem_share"]["proposed"],
        "overflow_hops": totals["overflow_hops"]["proposed"],
        "affected_apps": summary["affected_apps"],
    }


def comparison_doc(comparison: Comparison, *, inputs: dict) -> dict:
    """The deterministic `comparison.json` document (spec §8). `inputs` is
    the caller's partial input record; `window` is added here, before
    hashing, exactly as `simulate.summarize` does for `simulation_id`."""
    full_inputs = dict(inputs)
    full_inputs["window"] = comparison.window
    comparison_id = hashlib.sha256(
        json.dumps(full_inputs, sort_keys=True).encode("utf-8")).hexdigest()[:16]
    blocks = (comparison.capacity["scenarios"] if comparison.capacity is not None
              else [None] * len(comparison.scenarios))
    doc = {
        "schema": COMPARISON_SCHEMA,
        "comparison_id": comparison_id,
        "inputs": full_inputs,
        "rows": comparison.rows,
        "baseline": comparison.baseline,
        "scenarios": comparison.scenarios,
        "table": [_table_row(entry, comparison.window, block)
                  for entry, block in zip(comparison.scenarios, blocks)],
    }
    if comparison.capacity is not None:
        # The ONE top-level key a capacity plan adds (the table rows gain
        # their three keys above); every other key is unchanged.
        doc["capacity"] = comparison.capacity
    if comparison.uncertainty is not None:
        # The ONE key `--uncertainty` adds; every key above is unchanged.
        doc["uncertainty"] = comparison.uncertainty
    return _round6(doc)


_TABLE_COLUMNS = ("scenario", "Δcost/day USD", "Δcost USD", "route changed", "newly rejected",
                  "newly served", "Δp50 ms", "Δp95 ms", "Δquality",
                  "on-prem share (current→proposed)", "overflow hops", "affected apps")


def _pct(value: float) -> str:
    return f"{value * 100:.1f}%"


def _agreement_lines(doc: dict) -> list[str]:
    """The agreement banner: model agreement (overall, per app, a warning
    under 90%), outcome agreement (the same, B60) with a note for every
    recorded outcome replay does not reproduce, and the `--since` cut when
    one was applied."""
    baseline = doc["baseline"]
    lines = ["## Baseline agreement", ""]
    if baseline["agreement"] is None:
        lines.append("**Baseline agreement: n/a** -- no served row with a recorded first "
                     "attempt to compare. Nothing vouches for this replay.")
    else:
        lines.append(f"**Baseline agreement: {_pct(baseline['agreement'])}** over "
                     f"{baseline['rows_compared']} compared rows.")
        if baseline["warning"]:
            lines.append("")
            lines.append("⚠ **Warning:** below the 90% threshold. Every scenario row below "
                         "rests on a replay that disagrees with the record this often.")
        if baseline["by_app"]:
            lines.append("")
            lines.append("Per app: " + " · ".join(
                f"{app_id} {_pct(rate)}" + (" ⚠" if rate < 0.90 else "")
                for app_id, rate in sorted(baseline["by_app"].items())))
        if baseline["overflow_contradicted"]:
            lines.append("")
            lines.append(
                f"**Note:** {baseline['overflow_contradicted']} row(s) served by an endpoint "
                "replay predicts would overflow -- check that endpoint's declared "
                "context_window.")
    # B60: outcome agreement is over EVERY simulated row, so it is shown even
    # when no served row makes model agreement computable.
    lines.append("")
    if baseline["outcome_agreement"] is None:
        lines.append("**Outcome agreement: n/a** -- no simulated row.")
    else:
        lines.append(f"**Outcome agreement: {_pct(baseline['outcome_agreement'])}** over "
                     f"{baseline['outcome_rows_compared']} simulated rows (routed or "
                     "rejected, against each row's recorded status).")
        if baseline["outcome_warning"]:
            lines.append("")
            lines.append("⚠ **Warning:** outcome agreement is below the 90% threshold. "
                         "Replay serves or rejects requests differently from the record "
                         "this often.")
        if baseline["outcome_by_app"]:
            lines.append("")
            lines.append("Per app (outcome): " + " · ".join(
                f"{app_id} {_pct(stats['agreement'])}" + (" ⚠" if stats["agreement"] < 0.90 else "")
                for app_id, stats in sorted(baseline["outcome_by_app"].items())))
    for note in _fidelity_notes(baseline, doc["rows"]):
        lines.append("")
        lines.append(f"**Note:** {note}")
    since = doc["inputs"].get("since")
    if since is not None:
        lines.append("")
        lines.append(f"Rows before `--since {since}` excluded: "
                     f"{doc['rows']['excluded'].get('before_since', 0)}.")
    lines.append("")
    return lines


def _comparison_row(row: dict, ranges: dict | None = None,
                    capacity: dict | None = None) -> str:
    """One table row. `ranges` (P3c-3, `uncertainty.table_ranges`) puts
    `[lo, hi]` after each figure that has one; without it, or when the
    scenario has too few rows for a range, the cells are the points alone.
    `capacity` (P3c-4) is that scenario's capacity block: one more cell, a
    point with no range."""
    if ranges is None or ranges.get("insufficient_data"):
        ranges = {}

    def cell(key: str, text: str) -> str:
        return with_range(text, ranges.get(key))

    cells = [
        row["name"],
        cell("cost_per_day_delta_usd", _fmt(row["cost_per_day_delta_usd"])),
        cell("cost_delta_usd", _fmt(row["cost_delta_usd"])),
        cell("route_changed", str(row["route_changed"])),
        cell("newly_rejected", str(row["newly_rejected"])),
        cell("newly_served", str(row["newly_served"])),
        cell("latency_p50_delta_ms", _fmt(row["latency_p50_delta_ms"])),
        cell("latency_p95_delta_ms", _fmt(row["latency_p95_delta_ms"])),
        cell("quality_mean_delta", _fmt(row["quality_mean_delta"])),
        f"{_fmt(row['onprem_share_current'])}→{_fmt(row['onprem_share_proposed'])}",
        str(row["overflow_hops"]),
        ", ".join(row["affected_apps"]) or "none",
    ]
    if capacity is not None:
        cells.append(peak_text(capacity, "proposed"))
    return "| " + " | ".join(cells) + " |"


#: Printed with the ranges, verbatim (P3c-3 spec §10, invariant U11).
RANGE_NOT_RANKING_NOTE = (
    "In the table above, `[lo, hi]` after a figure is its range. A range is not a ranking: "
    "overlapping ranges do not make two scenarios equal, and separate ranges do not make "
    "one of them the one to choose."
)


def _uncertainty_lines(block: dict) -> list[str]:
    """The comparison report's "Uncertainty ranges" section: the caveat, how
    the ranges were made, the strata the totals were resampled in, then one
    line per scenario in FILE order -- its quality coverage, or that it has
    too few rows."""
    lines = ["## Uncertainty ranges", ""]
    lines += caveat_lines()
    lines += ["", method_line(block), ""]
    min_max = min_max_line(block)
    if min_max:
        lines += [min_max, ""]
    # Every scenario replays the same rows, so they all have the same strata
    # and the same rows with no backend token count.
    ranged = next((entry for entry in block["scenarios"]
                   if not entry["totals"].get("insufficient_data")), None)
    if ranged is not None:
        totals = ranged["totals"]
        lines += [token_estimate_line(totals["estimated_token_rows"], totals["n"]), "",
                  strata_line(ranged["totals_strata"], block["min_rows"]), ""]
    lines += [RANGE_NOT_RANKING_NOTE, ""]
    for entry in block["scenarios"]:
        if entry["totals"].get("insufficient_data"):
            lines.append(f"- **{entry['name']}** -- {insufficient_text(entry['totals'])}")
            continue
        text = quality_line(entry["quality_posterior"])
        outside = outside_line(entry["points_outside_interval"])
        lines.append(f"- **{entry['name']}** -- {text}" + (f" {outside}" if outside else ""))
    lines.append("")
    return lines


#: How many insufficient-data applications a scenario's section names.
_SMALL_APPS_SHOWN = 10


def render_comparison(doc: dict) -> str:
    """`report.md` for a comparison (spec §8): the caveat, the agreement
    banner, ONE table in file order, the assumptions each scenario makes,
    then a short section per scenario, then the excluded rows. Built from
    the `comparison.json` document alone."""
    lines: list[str] = ["> " + line for line in REPORT_CAVEAT.split("\n")]
    lines.append("")
    title = doc["inputs"].get("title")
    lines.append(f"# Scenario comparison: {title}" if title else "# Scenario comparison")
    lines.append("")
    lines.append(f"{doc['rows']['simulated']} rows simulated, "
                 f"{len(doc['scenarios'])} scenario(s), one shared current arm.")
    lines.append("")

    lines += _agreement_lines(doc)

    lines.append("## Comparison")
    lines.append("")
    uncertainty = doc.get("uncertainty")
    ranges = {entry["name"]: entry for entry in uncertainty["scenarios"]} if uncertainty else {}
    capacity = doc.get("capacity")
    loads = {entry["name"]: entry for entry in capacity["scenarios"]} if capacity else {}
    columns = _TABLE_COLUMNS + (("peak load (proposed)",) if capacity else ())
    lines.append("| " + " | ".join(columns) + " |")
    lines.append("|" + "---|" * len(columns))
    lines += [_comparison_row(row, ranges[row["name"]]["table"] if ranges else None,
                              loads.get(row["name"]))
              for row in doc["table"]]
    lines.append("")
    lines.append(NO_RANKING_NOTE)
    lines.append("")
    lines.append(LIKE_FOR_LIKE_NOTE)
    lines.append("")
    if capacity:
        lines += [PEAK_COLUMN_NOTE, ""]
    if uncertainty:
        tables = [entry["table"] for entry in uncertainty["scenarios"]
                  if not entry["table"].get("insufficient_data")]
        if tables:
            # Only when the table above shows brackets at all.
            lines += [ZERO_RANGE_NOTE, ""]
            # The on-prem share cell prints no range, so a mark could not
            # appear there: count the cells that do.
            shown = [{k: v for k, v in table.items() if not k.startswith("onprem_share")}
                     for table in tables]
            if _count_outside(shown):
                lines += [OUTSIDE_FOOTNOTE, ""]
        lines += _uncertainty_lines(uncertainty)

    if capacity:
        # Every scenario replays the same rows against the same base plan,
        # so the window and the current arm's peak are the first one's.
        shared = capacity["scenarios"][0]
        lines += ["## Capacity", "", *intro_lines(shared, ranged=bool(uncertainty)),
                  f"Current arm peak load (the same for every scenario): "
                  f"{peak_text(shared, 'current')}.", ""]

    assuming = [(entry["name"], _assumption_items(entry["assumptions"]))
                for entry in doc["scenarios"]]
    assuming = [(name, items) for name, items in assuming if items]
    if assuming:
        lines.append("## Assumptions")
        lines.append("")
        for name, items in assuming:
            lines.append(f"- **{name}** -- " + "; ".join(items))
        if any(entry["assumptions"]["cached_share"] for entry in doc["scenarios"]):
            lines.append("")
            lines.append(CACHED_SHARE_NOTE)
        lines.append("")

    for entry in doc["scenarios"]:
        summary = entry["summary"]
        lines.append(f"## Scenario: {entry['name']}")
        lines.append("")
        lines += _metrics_table(summary["totals"])
        lines.append("")
        if uncertainty:
            lines.append("### Ranges")
            lines.append("")
            lines += bucket_lines(ranges[entry["name"]]["totals"], uncertainty["interval"])
            lines.append("")
            small = [f"{app_id} (n = {bucket['n']})"
                     for app_id, bucket in sorted(ranges[entry["name"]]["by_app"].items())
                     if bucket.get("insufficient_data")]
            if small:
                # An export can hold thousands of small applications: the
                # count, and at most ten names. The rest are in the JSON.
                shown = small[:_SMALL_APPS_SHOWN]
                head = (f"{len(small)} app(s), the first {len(shown)}: "
                        if len(small) > len(shown) else f"{len(small)} app(s): ")
                lines.append("Per-app ranges are in `comparison.json`. Insufficient data: "
                             + head + ", ".join(shown) + ".")
            else:
                lines.append("Per-app ranges are in `comparison.json`.")
            lines.append("")
        lines.append("### Moves")
        lines.append("")
        lines += _move_lines(summary["moves"])
        lines.append("")
        lines.append("### New rejections")
        lines.append("")
        lines += _new_rejection_lines(summary["new_rejections"])
        lines.append("")
        lines.append("Affected apps: " + (", ".join(summary["affected_apps"]) or "none"))
        lines.append("")
        if capacity:
            lines += ["### Capacity", "", *scenario_lines(loads[entry["name"]], depth=4)]

    lines.append("## Excluded rows")
    lines.append("")
    lines += _excluded_lines(doc["rows"]["excluded"])

    return "\n".join(lines) + "\n"
