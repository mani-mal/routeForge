"""P3a Task 3: operator policy-proposal loading and application (spec §5).

A proposal is a small, strictly-validated JSON document an operator writes by
hand: "what would change if I applied this policy edit". `load_proposal`
reads and shape-validates it (schema + the closed key set -- a typo can never
silently mean "no change"). `apply_proposal` takes that shape-valid document
plus a config snapshot (Task 1/2's shape) and returns a NEW snapshot with the
edit applied -- deep-copied, never mutating either input. All validation runs
before any change: loading is all-or-nothing.

Kept out of `app.eval.scenario` on purpose (controller ruling): this module
is the proposal's own concern and must not be imported by serving code --
only `app/eval/*` and `scripts/*` ever touch it.
"""
from __future__ import annotations

import copy
import hashlib
import json
import math

from app.config.loader import load_settings
from app.config.schema import Settings
from app.eval.scenario import _app_policy_view, build_settings
from app.policy.compile import PolicyViolation, compile_policy, resolve_residency
from app.policy.enums import PROMPT_RETENTION_VALUES

SCHEMA = "routeforge.policy-proposal/v1"
_SCHEMA_PREFIX = "routeforge.policy-proposal/"
_SCHEMA_MAJOR = "v1"

_TOP_KEYS = frozenset({"schema", "title", "apps", "providers", "endpoints", "prices"})
_APP_EDIT_KEYS = frozenset({
    "residency", "allowed_regions", "require_no_retention", "allowed_tiers",
    "allowed_providers", "allowed_models", "max_cost_usd_per_request",
    "escalate_residency_on_detection", "sensitivity_rules",
})
# The subset of `_APP_EDIT_KEYS` that are actually `AppPolicyView` fields --
# `sensitivity_rules` lives beside `policy` on an app entry, not inside it,
# and is never merged into the view `compile_policy` checks.
_APP_POLICY_EDIT_FIELDS = _APP_EDIT_KEYS - {"sensitivity_rules"}
_SET_FIELDS = frozenset(
    {"allowed_regions", "allowed_tiers", "allowed_providers", "allowed_models"})
_PROVIDER_EDIT_KEYS = frozenset({"enabled"})
_ENDPOINTS_KEYS = frozenset({"add", "remove", "disable", "enable", "update"})
_ENDPOINT_ADD_KEYS = frozenset({
    "slug", "provider", "tier", "price", "region", "prompt_retention",
    "accepts_raw_sensitive", "context_window", "quality", "allowed_app_ids",
})
_ENDPOINT_UPDATE_KEYS = frozenset({
    "tier", "region", "prompt_retention", "accepts_raw_sensitive",
    "context_window", "allowed_app_ids",
})
_PRICE_KEYS = frozenset({"input", "output", "cached_input"})
_RULE_KEYS = frozenset(
    {"entities", "sensitivity", "risk_class", "residency", "require_verification"})

_BASE_SETTINGS: Settings | None = None


class ProposalError(ValueError):
    """A proposal load/validate/apply error. Always names the offending
    dotted location so a caller doesn't have to re-read the document to act
    on it -- mirrors `app.eval.scenario.SnapshotError`."""

    def __init__(self, where: str, message: str) -> None:
        self.where = where
        self.message = message
        super().__init__(f"{where}: {message}")


# --- shape validation (needs no snapshot) ------------------------------------

def _check_keys(obj, allowed: frozenset[str], path: str) -> None:
    if not isinstance(obj, dict):
        raise ProposalError(path or "<root>", "must be an object")
    for key in obj:
        if key not in allowed:
            raise ProposalError(f"{path}.{key}" if path else key, "unknown key")


def _validate_schema(doc: dict) -> None:
    schema = doc.get("schema") if isinstance(doc, dict) else None
    if not isinstance(schema, str) or not schema.startswith(_SCHEMA_PREFIX):
        raise ProposalError(
            "schema", f"must start with {_SCHEMA_PREFIX!r}; got {schema!r}")
    major = schema[len(_SCHEMA_PREFIX):].split(".", 1)[0]
    if major != _SCHEMA_MAJOR:
        raise ProposalError(
            "schema", f"major version must be {_SCHEMA_MAJOR!r}; got {major!r}")


def _validate_keys(doc: dict) -> None:
    """Structural-only pass: every key, at every level, is one of the
    documented ones (spec §5). Value validity (types, ranges, references into
    the snapshot) happens later in `apply_proposal`, which has a snapshot to
    check against; this pass does not need one."""
    _check_keys(doc, _TOP_KEYS, "")

    for app_id, edit in (doc.get("apps") or {}).items():
        _check_keys(edit, _APP_EDIT_KEYS, f"apps.{app_id}")
        for i, rule in enumerate(edit.get("sensitivity_rules") or []):
            _check_keys(rule, _RULE_KEYS, f"apps.{app_id}.sensitivity_rules[{i}]")

    for name, fields in (doc.get("providers") or {}).items():
        _check_keys(fields, _PROVIDER_EDIT_KEYS, f"providers.{name}")

    endpoints = doc.get("endpoints") or {}
    _check_keys(endpoints, _ENDPOINTS_KEYS, "endpoints")
    for i, entry in enumerate(endpoints.get("add") or []):
        slug = entry.get("slug") if isinstance(entry, dict) else None
        loc = f"endpoints.add.{slug}" if isinstance(slug, str) and slug else f"endpoints.add[{i}]"
        _check_keys(entry, _ENDPOINT_ADD_KEYS, loc)
        if isinstance(entry.get("price"), dict):
            _check_keys(entry["price"], _PRICE_KEYS, f"{loc}.price")
    for slug, fields in (endpoints.get("update") or {}).items():
        _check_keys(fields, _ENDPOINT_UPDATE_KEYS, f"endpoints.update.{slug}")

    for slug, fields in (doc.get("prices") or {}).items():
        _check_keys(fields, _PRICE_KEYS, f"prices.{slug}")


def load_proposal(path: str) -> tuple[dict, str]:
    """Read a policy-proposal JSON file. Returns `(doc, sha256 hex)` -- the
    hash is over the raw file bytes. Raises `ProposalError` on invalid JSON,
    a bad schema, or an unknown key anywhere in the document."""
    with open(path, "rb") as f:
        raw = f.read()
    digest = hashlib.sha256(raw).hexdigest()
    try:
        doc = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ProposalError("file", f"invalid JSON: {exc}") from exc
    _validate_schema(doc)
    _validate_keys(doc)
    return doc, digest


# --- value helpers ------------------------------------------------------------

def _finite_number(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def _finite_nonneg(v) -> bool:
    return _finite_number(v) and v >= 0


def _finite_positive(v) -> bool:
    return _finite_number(v) and v > 0


def _validate_price_fields(loc: str, price) -> None:
    if not isinstance(price, dict):
        raise ProposalError(loc, "must be an object")
    if not _finite_nonneg(price.get("input")):
        raise ProposalError(loc, "input must be a finite number >= 0")
    if not _finite_nonneg(price.get("output")):
        raise ProposalError(loc, "output must be a finite number >= 0")
    cached = price.get("cached_input")
    if cached is not None and not _finite_nonneg(cached):
        raise ProposalError(loc, "cached_input must be null or a finite number >= 0")


def _validate_allowed_app_ids(loc: str, value, app_ids: set[str]) -> None:
    if value is None:
        return
    if not isinstance(value, list):
        raise ProposalError(loc, "must be null or a list of application ids")
    bad = sorted(set(value) - app_ids)
    if bad:
        raise ProposalError(loc, f"unknown application id(s) {bad}")


def _validate_prompt_retention(loc: str, value) -> None:
    if value is not None and value not in PROMPT_RETENTION_VALUES:
        raise ProposalError(
            loc, f"must be one of {sorted(PROMPT_RETENTION_VALUES)} or null")


def _validate_accepts_raw_sensitive(loc: str, value) -> None:
    if value is not None and not isinstance(value, bool):
        raise ProposalError(loc, "must be a bool or null")


def _validate_context_window(loc: str, value) -> None:
    if value is not None and (not isinstance(value, int) or isinstance(value, bool)
                              or value <= 0):
        raise ProposalError(loc, "must be an int > 0 or null")


def _validate_tier(loc: str, value, canonical_tiers: set[str]) -> None:
    if value not in canonical_tiers:
        raise ProposalError(
            loc, f"unknown tier {value!r}; legal tiers: {sorted(canonical_tiers)}")


# --- app edit validation -------------------------------------------------------

def _validate_rule(app_id: str, i: int, rule: dict, risk_classes: set[str],
                   sensitivity_levels: set[str]) -> None:
    loc = f"apps.{app_id}.sensitivity_rules[{i}]"
    entities = rule.get("entities")
    if not isinstance(entities, list) or not all(isinstance(e, str) for e in entities):
        raise ProposalError(f"{loc}.entities", "must be a list of strings")
    sensitivity = rule.get("sensitivity")
    if sensitivity not in sensitivity_levels:
        raise ProposalError(f"{loc}.sensitivity", f"unknown sensitivity {sensitivity!r}")
    _, parsed = resolve_residency(rule.get("residency"))
    if not parsed:
        raise ProposalError(
            f"{loc}.residency", f"unparseable residency {rule.get('residency')!r}")
    risk_class = rule.get("risk_class")
    if risk_class not in risk_classes:
        raise ProposalError(f"{loc}.risk_class", f"unknown risk_class {risk_class!r}")
    if not isinstance(rule.get("require_verification"), bool):
        raise ProposalError(f"{loc}.require_verification", "must be a bool")


def _validate_app_edit(app_id: str, edit: dict, entry: dict, settings: Settings,
                       canonical_tiers: set[str], risk_classes: set[str],
                       sensitivity_levels: set[str]) -> None:
    if "residency" in edit:
        _, parsed = resolve_residency(edit["residency"])
        if not parsed:
            raise ProposalError(
                f"apps.{app_id}.residency",
                f"unparseable residency {edit['residency']!r}")

    for field in _SET_FIELDS:
        if field in edit and edit[field] is not None and not isinstance(edit[field], list):
            raise ProposalError(f"apps.{app_id}.{field}", "must be a list or null")

    if "allowed_tiers" in edit and edit["allowed_tiers"] is not None:
        bad = sorted(set(edit["allowed_tiers"]) - canonical_tiers)
        if bad:
            raise ProposalError(
                f"apps.{app_id}.allowed_tiers",
                f"unknown tier(s) {bad}; legal tiers: {sorted(canonical_tiers)}")

    if "max_cost_usd_per_request" in edit and edit["max_cost_usd_per_request"] is not None:
        if not _finite_positive(edit["max_cost_usd_per_request"]):
            raise ProposalError(
                f"apps.{app_id}.max_cost_usd_per_request",
                "must be a finite number > 0, or null")

    for field in ("require_no_retention", "escalate_residency_on_detection"):
        if field in edit and edit[field] is not None and not isinstance(edit[field], bool):
            raise ProposalError(f"apps.{app_id}.{field}", "must be a bool or null")

    if "sensitivity_rules" in edit:
        rules = edit["sensitivity_rules"]
        if not isinstance(rules, list):
            raise ProposalError(f"apps.{app_id}.sensitivity_rules", "must be a list")
        for i, rule in enumerate(rules):
            _validate_rule(app_id, i, rule, risk_classes, sensitivity_levels)

    merged = dict(entry["policy"])
    for field in _APP_POLICY_EDIT_FIELDS:
        if field in edit:
            merged[field] = edit[field]
    try:
        view = _app_policy_view(merged, app_id)
        compile_policy(view, settings)
    except (PolicyViolation, ValueError) as exc:
        raise ProposalError(f"apps.{app_id}", str(exc)) from exc


# --- endpoint edit validation --------------------------------------------------

def _validate_endpoint_add(entry, onboarded_by_slug: dict, pricing_models: set[str],
                           declared_providers: set[str], canonical_tiers: set[str],
                           app_ids: set[str]) -> None:
    slug = entry.get("slug")
    if not isinstance(slug, str) or not slug:
        raise ProposalError("endpoints.add", "slug must be a non-empty string")
    loc = f"endpoints.add.{slug}"
    if slug in onboarded_by_slug or slug in pricing_models:
        raise ProposalError(loc, "already an onboarded endpoint or a priced model")
    if entry.get("provider") not in declared_providers:
        raise ProposalError(
            f"{loc}.provider", "provider not declared in the snapshot")
    _validate_tier(f"{loc}.tier", entry.get("tier"), canonical_tiers)
    _validate_price_fields(f"{loc}.price", entry.get("price"))
    _validate_prompt_retention(f"{loc}.prompt_retention", entry.get("prompt_retention"))
    _validate_accepts_raw_sensitive(
        f"{loc}.accepts_raw_sensitive", entry.get("accepts_raw_sensitive"))
    _validate_context_window(f"{loc}.context_window", entry.get("context_window"))
    quality = entry.get("quality")
    if quality is not None and not (_finite_number(quality) and 0 <= quality <= 1):
        raise ProposalError(f"{loc}.quality", "must be null or a finite value in [0, 1]")
    _validate_allowed_app_ids(f"{loc}.allowed_app_ids", entry.get("allowed_app_ids"), app_ids)


def _validate_endpoint_update(slug: str, fields: dict, canonical_tiers: set[str],
                              app_ids: set[str]) -> None:
    loc = f"endpoints.update.{slug}"
    if "tier" in fields:
        _validate_tier(f"{loc}.tier", fields["tier"], canonical_tiers)
    if "prompt_retention" in fields:
        _validate_prompt_retention(f"{loc}.prompt_retention", fields["prompt_retention"])
    if "accepts_raw_sensitive" in fields:
        _validate_accepts_raw_sensitive(
            f"{loc}.accepts_raw_sensitive", fields["accepts_raw_sensitive"])
    if "context_window" in fields:
        _validate_context_window(f"{loc}.context_window", fields["context_window"])
    if "allowed_app_ids" in fields:
        _validate_allowed_app_ids(
            f"{loc}.allowed_app_ids", fields["allowed_app_ids"], app_ids)


# --- application -----------------------------------------------------------

def normalized_edits(proposal: dict) -> dict:
    """`proposal` minus `schema`/`title`, keys sorted -- the exact form
    echoed into `simulation.json` so the record shows what was asked for
    without the schema/title noise."""
    return {k: proposal[k] for k in sorted(proposal) if k not in ("schema", "title")}


def _load_base_settings() -> Settings:
    global _BASE_SETTINGS
    if _BASE_SETTINGS is None:
        _BASE_SETTINGS = load_settings("config")
    return _BASE_SETTINGS


def apply_proposal(snapshot: dict, proposal: dict, *, base: Settings | None = None) -> dict:
    """Validate `proposal` against `snapshot` (all-or-nothing, every error
    naming its location) and return a NEW snapshot document with the edit
    applied. Never mutates `snapshot` or `proposal`."""
    _validate_schema(proposal)
    _validate_keys(proposal)

    settings = build_settings(snapshot, base if base is not None else _load_base_settings())

    apps_by_id = {a["application_id"]: a for a in snapshot["apps"]}
    onboarded_by_slug = {o["slug"]: o for o in snapshot["onboarded"]}
    pricing_models = set(settings.pricing.models)
    canonical_tiers = set(settings.routing.tiers)
    risk_classes = set(settings.routing.risk_floors)
    sensitivity_levels = set(settings.policy.sensitivity_scale)
    declared_providers = set(snapshot["settings"]["providers"])
    app_ids = set(apps_by_id)

    # -- apps --
    app_edits = proposal.get("apps") or {}
    for app_id, edit in app_edits.items():
        if app_id not in apps_by_id:
            raise ProposalError(f"apps.{app_id}", "unknown application")
        _validate_app_edit(app_id, edit, apps_by_id[app_id], settings,
                          canonical_tiers, risk_classes, sensitivity_levels)

    # -- providers --
    provider_edits = proposal.get("providers") or {}
    for name, fields in provider_edits.items():
        if name not in declared_providers:
            raise ProposalError(
                f"providers.{name}", "provider not declared in the snapshot")
        if not isinstance(fields.get("enabled"), bool):
            raise ProposalError(f"providers.{name}.enabled", "must be a bool")

    # -- endpoints --
    endpoints = proposal.get("endpoints") or {}
    add_list = endpoints.get("add") or []
    remove_list = endpoints.get("remove") or []
    disable_list = endpoints.get("disable") or []
    enable_list = endpoints.get("enable") or []
    update_map = endpoints.get("update") or {}

    add_slugs = [e.get("slug") for e in add_list]
    seen: dict[str, str] = {}
    for op_name, slugs in (("add", add_slugs), ("remove", remove_list),
                          ("disable", disable_list), ("enable", enable_list),
                          ("update", list(update_map))):
        for slug in slugs:
            if slug in seen:
                raise ProposalError(
                    f"endpoints.{slug}", "appears in more than one endpoint operation")
            seen[slug] = op_name

    for entry in add_list:
        _validate_endpoint_add(entry, onboarded_by_slug, pricing_models,
                              declared_providers, canonical_tiers, app_ids)
    for slug in remove_list:
        if slug not in onboarded_by_slug:
            raise ProposalError(f"endpoints.remove.{slug}", "not an onboarded endpoint")
    for slug in disable_list:
        if slug not in onboarded_by_slug:
            raise ProposalError(f"endpoints.disable.{slug}", "not an onboarded endpoint")
    for slug in enable_list:
        if slug not in onboarded_by_slug:
            raise ProposalError(f"endpoints.enable.{slug}", "not an onboarded endpoint")
    for slug, fields in update_map.items():
        if slug not in onboarded_by_slug:
            raise ProposalError(f"endpoints.update.{slug}", "not an onboarded endpoint")
        _validate_endpoint_update(slug, fields, canonical_tiers, app_ids)

    # -- prices --
    removed = set(remove_list)
    price_edits = proposal.get("prices") or {}
    for slug, fields in price_edits.items():
        eligible = (slug in onboarded_by_slug and slug not in removed) or slug in pricing_models
        if not eligible:
            raise ProposalError(
                f"prices.{slug}", "not an onboarded endpoint or a priced model")
        _validate_price_fields(f"prices.{slug}", fields)

    # -- everything validated; apply on a deep copy, in spec order --
    out = copy.deepcopy(snapshot)

    apps_out = {a["application_id"]: a for a in out["apps"]}
    for app_id, edit in app_edits.items():
        policy = apps_out[app_id]["policy"]
        for field, value in edit.items():
            if field == "sensitivity_rules":
                apps_out[app_id]["sensitivity_rules"] = copy.deepcopy(value)
            elif field in _SET_FIELDS and value is not None:
                policy[field] = sorted(value)
            else:
                policy[field] = value

    providers_out = out["settings"]["providers"]
    for name, fields in provider_edits.items():
        providers_out[name]["enabled"] = fields["enabled"]

    onboarded_out = {o["slug"]: o for o in out["onboarded"]}
    for entry in add_list:
        price = entry.get("price") or {}
        allowed_app_ids = entry.get("allowed_app_ids")
        onboarded_out[entry["slug"]] = {
            "slug": entry["slug"],
            "display_name": entry["slug"],
            "provider": entry["provider"],
            "tier": entry["tier"],
            "quality": entry.get("quality"),
            "price_input": price.get("input"),
            "price_output": price.get("output"),
            "price_cached_input": price.get("cached_input"),
            "enabled": True,
            "allowed_app_ids": sorted(allowed_app_ids) if allowed_app_ids else None,
            "region": entry.get("region"),
            "prompt_retention": entry.get("prompt_retention"),
            "accepts_raw_sensitive": entry.get("accepts_raw_sensitive"),
            "context_window": entry.get("context_window"),
        }
    for slug in remove_list:
        onboarded_out.pop(slug, None)
    for slug in disable_list:
        onboarded_out[slug]["enabled"] = False
    for slug in enable_list:
        onboarded_out[slug]["enabled"] = True
    for slug, fields in update_map.items():
        row = onboarded_out[slug]
        for field, value in fields.items():
            row[field] = sorted(value) if field == "allowed_app_ids" and value is not None else value

    if removed:
        for bucket in ("customer", "calibrated"):
            out["quality"][bucket] = [
                q for q in out["quality"][bucket] if q["model"] not in removed]

    for slug, fields in price_edits.items():
        if slug in onboarded_out:
            row = onboarded_out[slug]
            row["price_input"] = fields["input"]
            row["price_output"] = fields["output"]
            row["price_cached_input"] = fields.get("cached_input")
        else:
            model = out["settings"]["pricing"]["models"][slug]
            model["input"] = fields["input"]
            model["output"] = fields["output"]
            model["cached_input"] = fields.get("cached_input")

    # `onboarded_out`'s values are the SAME dict objects already living in
    # `out["onboarded"]` (a shallow slug->row index over the deep-copied
    # list), so disable/enable/update mutations already took effect in
    # place -- no rebuild needed. Only `add`/`remove` change which rows
    # exist, so the list is only rebuilt (and re-sorted, per the Task 1
    # ordering invariant) when membership actually changed; an edit that
    # never touches endpoints leaves the list's existing order untouched,
    # which is what makes an empty proposal a true identity. `apps` never
    # gains or loses entries (a proposal only edits an existing app's
    # fields in place), so its list is never rebuilt either.
    if add_list or remove_list:
        out["onboarded"] = sorted(onboarded_out.values(), key=lambda o: o["slug"])

    return out
