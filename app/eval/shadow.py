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

__all__ = ["Relaxation", "enumerate_relaxations"]


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
    P1b) -- `false` covers both spellings."""
    return value is False or value == "false"


def _flipped(value: object) -> object:
    if value is False:
        return True
    if value == "false":
        return "true"
    return value


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


def _applies_residency(doc: dict, policy: dict, app_rows: list[dict]) -> bool:
    residency = policy.get("residency")
    if residency is not None and residency != "any":
        return True
    escalate = policy.get("escalate_residency_on_detection")
    if escalate is False:
        return False
    return any(row.get("detected_entity_types") for row in app_rows)


def _applies_null_field(field: str):
    def _applies(doc: dict, policy: dict, app_rows: list[dict]) -> bool:
        return policy.get(field) is not None
    return _applies


def _applies_require_no_retention(doc: dict, policy: dict, app_rows: list[dict]) -> bool:
    return policy.get("require_no_retention") is True


def _applies_raw_sensitive(doc: dict, policy: dict, app_rows: list[dict]) -> bool:
    return _any_raw_sensitive_false(doc)


def _applies_requested_model(doc: dict, policy: dict, app_rows: list[dict]) -> bool:
    return any(row.get("requested_model") for row in app_rows)


def _applies_max_cost(doc: dict, policy: dict, app_rows: list[dict]) -> bool:
    return True


def _applies_risk_floor(doc: dict, policy: dict, app_rows: list[dict]) -> bool:
    risk_floors = doc["settings"]["routing"]["risk_floors"]
    return any(risk_floors.get(row.get("risk_class"), "cheap") != "cheap" for row in app_rows)


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
        policy = entry.get("policy", {})
        for req_id in _APP_RULE_ORDER:
            if _APPLIES[req_id](doc, policy, app_rows):
                candidates.append(Relaxation(req_id, app_id, None, _RELAXED_TO[req_id]))

    for row in sorted(doc["onboarded"], key=lambda o: o["slug"]):
        if row.get("allowed_app_ids") is not None:
            candidates.append(Relaxation(
                REQ_ENDPOINT_APP_ALLOWLIST, None, row["slug"],
                _RELAXED_TO[REQ_ENDPOINT_APP_ALLOWLIST]))

    skipped = max(0, len(candidates) - cap)
    return candidates[:cap], skipped
