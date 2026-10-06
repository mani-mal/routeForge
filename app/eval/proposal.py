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
from app.eval.scenario import ALL_APPS, SnapshotError, _app_policy_view, build_settings
from app.policy.compile import (_OPERATOR_NAME, PolicyViolation, compile_policy,
                                resolve_residency)
from app.policy.enums import PROMPT_RETENTION_VALUES

SCHEMA = "routeforge.policy-proposal/v1"
_SCHEMA_PREFIX = "routeforge.policy-proposal/"
_SCHEMA_MAJOR = "v1"

_TOP_KEYS = frozenset({"schema", "title", "apps", "providers", "endpoints", "prices",
                       "workload"})
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
    "latency_ms",
})
_ENDPOINT_UPDATE_KEYS = frozenset({
    "tier", "region", "prompt_retention", "accepts_raw_sensitive",
    "context_window", "allowed_app_ids", "latency_ms",
})
_PRICE_KEYS = frozenset({"input", "output", "cached_input"})
# P3c-2 (spec §4.1): assumptions about the traffic itself, global and per app.
_WORKLOAD_KEYS = frozenset({"token_scale", "cached_share", "apps"})
_WORKLOAD_APP_KEYS = frozenset({"token_scale", "cached_share"})
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


def _opt_dict(obj: dict, key: str, path: str) -> dict:
    """`obj[key]` as a dict, `{}` when absent/null; any other type is a
    `ProposalError` at `path` (never an AttributeError traceback)."""
    value = obj.get(key)
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ProposalError(path, "must be an object")
    return value


def _opt_list(obj: dict, key: str, path: str) -> list:
    value = obj.get(key)
    if value is None:
        return []
    if not isinstance(value, list):
        raise ProposalError(path, "must be a list")
    return value


def _validate_keys(doc: dict) -> None:
    """Structural-only pass: every key, at every level, is one of the
    documented ones (spec §5), and every container is the documented type
    (object / list / list of slug strings) -- so a malformed document is a
    `ProposalError` naming its path, never a traceback. Value validity
    (types, ranges, references into the snapshot) happens later in
    `apply_proposal`, which has a snapshot to check against; this pass does
    not need one."""
    _check_keys(doc, _TOP_KEYS, "")

    for app_id, edit in _opt_dict(doc, "apps", "apps").items():
        _check_keys(edit, _APP_EDIT_KEYS, f"apps.{app_id}")
        rules_path = f"apps.{app_id}.sensitivity_rules"
        for i, rule in enumerate(_opt_list(edit, "sensitivity_rules", rules_path)):
            _check_keys(rule, _RULE_KEYS, f"{rules_path}[{i}]")

    for name, fields in _opt_dict(doc, "providers", "providers").items():
        _check_keys(fields, _PROVIDER_EDIT_KEYS, f"providers.{name}")

    endpoints = _opt_dict(doc, "endpoints", "endpoints")
    _check_keys(endpoints, _ENDPOINTS_KEYS, "endpoints")
    for i, entry in enumerate(_opt_list(endpoints, "add", "endpoints.add")):
        if not isinstance(entry, dict):
            raise ProposalError(f"endpoints.add[{i}]", "must be an object")
        slug = entry.get("slug")
        if not isinstance(slug, str) or not slug:
            raise ProposalError(f"endpoints.add[{i}].slug", "must be a non-empty string")
        loc = f"endpoints.add.{slug}"
        _check_keys(entry, _ENDPOINT_ADD_KEYS, loc)
        if isinstance(entry.get("price"), dict):
            _check_keys(entry["price"], _PRICE_KEYS, f"{loc}.price")
    for op in ("remove", "disable", "enable"):
        for i, slug in enumerate(_opt_list(endpoints, op, f"endpoints.{op}")):
            if not isinstance(slug, str) or not slug:
                raise ProposalError(f"endpoints.{op}[{i}]", "must be a non-empty slug string")
    for slug, fields in _opt_dict(endpoints, "update", "endpoints.update").items():
        _check_keys(fields, _ENDPOINT_UPDATE_KEYS, f"endpoints.update.{slug}")

    for slug, fields in _opt_dict(doc, "prices", "prices").items():
        _check_keys(fields, _PRICE_KEYS, f"prices.{slug}")

    workload = _opt_dict(doc, "workload", "workload")
    _check_keys(workload, _WORKLOAD_KEYS, "workload")
    for app_id, fields in _opt_dict(workload, "apps", "workload.apps").items():
        _check_keys(fields, _WORKLOAD_APP_KEYS, f"workload.apps.{app_id}")


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


def _is_str_list(value) -> bool:
    return isinstance(value, list) and all(isinstance(v, str) for v in value)


def _validate_region(loc: str, value) -> None:
    """Admin parity (`OnboardIn.region: str | None`): a string or null.
    The token itself is customer vocabulary -- not checked against a list."""
    if value is not None and not isinstance(value, str):
        raise ProposalError(loc, "must be a string or null")


def _validate_allowed_app_ids(loc: str, value, app_ids: set[str]) -> None:
    if value is None:
        return
    if not _is_str_list(value):
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


def _validate_latency_ms(loc: str, value) -> None:
    """P3c-2 spec §4.2: an operator-asserted latency. `null` means "no
    override", so it is legal and writes nothing."""
    if value is not None and not _finite_positive(value):
        raise ProposalError(loc, "must be a finite number > 0, or null")


def _validate_tier(loc: str, value, canonical_tiers: set[str]) -> None:
    if value not in canonical_tiers:
        raise ProposalError(
            loc, f"unknown tier {value!r}; legal tiers: {sorted(canonical_tiers)}")


# --- app edit validation -------------------------------------------------------

def _validate_rule(app_id: str, i: int, rule: dict, risk_classes: set[str],
                   sensitivity_levels: set[str]) -> None:
    loc = f"apps.{app_id}.sensitivity_rules[{i}]"
    if not isinstance(rule, dict):
        raise ProposalError(loc, "must be an object")
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
                       sensitivity_levels: set[str], declared_providers: set[str],
                       known_models: set[str]) -> None:
    if "residency" in edit:
        _, parsed = resolve_residency(edit["residency"])
        if not parsed:
            raise ProposalError(
                f"apps.{app_id}.residency",
                f"unparseable residency {edit['residency']!r}")

    for field in _SET_FIELDS:
        if field in edit and edit[field] is not None and not _is_str_list(edit[field]):
            raise ProposalError(f"apps.{app_id}.{field}", "must be a list of strings or null")

    # Mirrors `app/api/admin_routes.py::_validate_policy_values`: the admin
    # write path checks only values a write CHANGES (a stored value from
    # before a check existed must not block an unrelated edit), refuses an
    # empty allowlist (the store reads [] as "no restriction", the opposite
    # of what clearing a list means), and refuses unknown tiers, providers
    # and models. `allowed_regions` is refused by the compiler below
    # (ALLOWED_REGIONS_INVALID), exactly as on the admin path.
    def changed(field: str) -> bool:
        value = edit.get(field)
        if value is None:
            return False
        stored = entry["policy"].get(field)
        return stored is None or sorted(value) != sorted(stored)

    for field in ("allowed_tiers", "allowed_providers", "allowed_models"):
        if changed(field) and len(edit[field]) == 0:
            raise ProposalError(
                f"apps.{app_id}.{field}",
                "is an empty list, which RouteForge reads as 'no restriction'; "
                "use null to clear it")
    for field, legal, what in (("allowed_tiers", canonical_tiers, "tier"),
                               ("allowed_providers", declared_providers, "provider")):
        if changed(field):
            bad = sorted(set(edit[field]) - legal)
            if bad:
                raise ProposalError(
                    f"apps.{app_id}.{field}",
                    f"unknown {what}(s) {bad}; legal {what}s: {sorted(legal)}")
    if changed("allowed_models"):
        bad = sorted(set(edit["allowed_models"]) - known_models)
        if bad:
            raise ProposalError(
                f"apps.{app_id}.allowed_models",
                f"unknown model(s) {bad}: not in the YAML pool, not onboarded and "
                "not added by this proposal")

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
        eff = compile_policy(view, settings)
    except (PolicyViolation, ValueError) as exc:
        raise ProposalError(f"apps.{app_id}", str(exc)) from exc
    if eff.rejected:
        # An unparseable value `compile_policy` failed closed on: the admin
        # write path refuses it (`validate_effective`, e.g.
        # ALLOWED_REGIONS_INVALID), so a proposal must too.
        names = [_OPERATOR_NAME.get(axis, axis) for axis, _value in eff.rejected]
        detail = ", ".join(f"{_OPERATOR_NAME.get(axis, axis)}={value!r}"
                           for axis, value in eff.rejected)
        raise ProposalError(f"apps.{app_id}.{names[0]}",
                            f"refused by compile_policy (unparseable value): {detail}")


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
    _validate_region(f"{loc}.region", entry.get("region"))
    _validate_prompt_retention(f"{loc}.prompt_retention", entry.get("prompt_retention"))
    _validate_accepts_raw_sensitive(
        f"{loc}.accepts_raw_sensitive", entry.get("accepts_raw_sensitive"))
    _validate_context_window(f"{loc}.context_window", entry.get("context_window"))
    quality = entry.get("quality")
    if quality is not None and not (_finite_number(quality) and 0 <= quality <= 1):
        raise ProposalError(f"{loc}.quality", "must be null or a finite value in [0, 1]")
    _validate_allowed_app_ids(f"{loc}.allowed_app_ids", entry.get("allowed_app_ids"), app_ids)
    _validate_latency_ms(f"{loc}.latency_ms", entry.get("latency_ms"))


def _validate_endpoint_update(slug: str, fields: dict, canonical_tiers: set[str],
                              app_ids: set[str]) -> None:
    loc = f"endpoints.update.{slug}"
    if "tier" in fields:
        _validate_tier(f"{loc}.tier", fields["tier"], canonical_tiers)
    if "region" in fields:
        _validate_region(f"{loc}.region", fields["region"])
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
    if "latency_ms" in fields:
        _validate_latency_ms(f"{loc}.latency_ms", fields["latency_ms"])


# --- workload validation (P3c-2 spec §4.1) -------------------------------------

def _validate_workload_fields(loc: str, fields: dict) -> None:
    """`token_scale` in (0, 10] and `cached_share` in [0, 1], each only when
    present. A bool, a string, NaN and +/-inf are all refused: an assumption
    that silently reads as "no change" is worse than a load error."""
    if "token_scale" in fields:
        value = fields["token_scale"]
        if not (_finite_number(value) and 0 < value <= 10):
            raise ProposalError(f"{loc}.token_scale",
                                "must be a finite number > 0 and <= 10")
    if "cached_share" in fields:
        value = fields["cached_share"]
        if not (_finite_number(value) and 0 <= value <= 1):
            raise ProposalError(f"{loc}.cached_share",
                                "must be a finite number in [0, 1]")


# --- application -----------------------------------------------------------

def normalized_edits(proposal: dict) -> dict:
    """`proposal` minus `schema`/`title`, keys sorted -- the exact form
    echoed into `simulation.json` so the record shows what was asked for
    without the schema/title noise."""
    return {k: proposal[k] for k in sorted(proposal) if k not in ("schema", "title")}


def assumptions_block(proposal: dict) -> dict | None:
    """The `assumptions` block a VALIDATED proposal sets (P3c-2 spec §4.3):
    `{"token_scale": {"*": x, "<app>": y}, "cached_share": {...},
    "latency_ms": {"<slug>": ms}}`, or None when it sets none. All three keys
    are always present in a block, so a reader never has to probe for one.
    `apply_proposal` writes exactly this into the applied document; the CLI
    and the scenario comparison echo it, like `normalized_edits`."""
    workload = proposal.get("workload") or {}
    block: dict[str, dict[str, float]] = {"token_scale": {}, "cached_share": {},
                                          "latency_ms": {}}
    for key in ("token_scale", "cached_share"):
        if key in workload:
            block[key][ALL_APPS] = float(workload[key])
        for app_id, fields in sorted((workload.get("apps") or {}).items()):
            if key in fields:
                block[key][app_id] = float(fields[key])
    # An asserted latency is an assumption about an endpoint, never a field
    # of its row: it lives here, and only here (spec §4.2). `null` = none.
    endpoints = proposal.get("endpoints") or {}
    for entry in endpoints.get("add") or []:
        if entry.get("latency_ms") is not None:
            block["latency_ms"][entry["slug"]] = float(entry["latency_ms"])
    for slug, fields in (endpoints.get("update") or {}).items():
        if fields.get("latency_ms") is not None:
            block["latency_ms"][slug] = float(fields["latency_ms"])
    return block if any(block.values()) else None


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
    # The block is only ever written below, so a document that already has
    # one is a snapshot a proposal was already applied to (or a hand edit):
    # applying a second proposal on top would silently merge two sets of
    # assumptions (P3c-2 spec §4.3).
    if "assumptions" in snapshot:
        raise SnapshotError(
            "assumptions", "is written only by apply_proposal; this snapshot already "
            "has a proposal applied")

    settings = build_settings(snapshot, base if base is not None else _load_base_settings())

    apps_by_id = {a["application_id"]: a for a in snapshot["apps"]}
    onboarded_by_slug = {o["slug"]: o for o in snapshot["onboarded"]}
    pricing_models = set(settings.pricing.models)
    canonical_tiers = set(settings.routing.tiers)
    risk_classes = set(settings.routing.risk_floors)
    sensitivity_levels = set(settings.policy.sensitivity_scale)
    declared_providers = set(snapshot["settings"]["providers"])
    app_ids = set(apps_by_id)

    # Every model an app's `allowed_models` may name: the YAML pool, the
    # snapshot's onboarded rows, and this proposal's own `endpoints.add`
    # (admin parity: `_validate_policy_values`'s known-model set, plus the
    # endpoint the same proposal onboards). `_validate_keys` above already
    # guaranteed every add entry is an object with a string slug.
    known_models = ({c.name for c in settings.routing.model_pool}
                    | set(onboarded_by_slug)
                    | {e["slug"] for e in (proposal.get("endpoints") or {}).get("add") or []})

    # -- apps --
    app_edits = proposal.get("apps") or {}
    for app_id, edit in app_edits.items():
        if app_id not in apps_by_id:
            raise ProposalError(f"apps.{app_id}", "unknown application")
        _validate_app_edit(app_id, edit, apps_by_id[app_id], settings,
                          canonical_tiers, risk_classes, sensitivity_levels,
                          declared_providers, known_models)

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
        if slug in onboarded_by_slug:
            _validate_endpoint_update(slug, fields, canonical_tiers, app_ids)
        elif slug in pricing_models:
            # A YAML pool model's tier, region and the rest are declared in
            # routing.yaml, not on an onboarded row -- there is nothing here
            # to update. An asserted latency is the one per-endpoint
            # assumption a proposal may set on it (spec §4.2).
            for field in fields:
                if field != "latency_ms":
                    raise ProposalError(
                        f"endpoints.update.{slug}.{field}",
                        "only latency_ms may be updated on a YAML pool model")
            _validate_endpoint_update(slug, fields, canonical_tiers, app_ids)
        else:
            raise ProposalError(
                f"endpoints.update.{slug}",
                "not an onboarded endpoint or a priced YAML pool model")

    # -- prices --
    removed = set(remove_list)
    price_edits = proposal.get("prices") or {}
    for slug, fields in price_edits.items():
        eligible = (slug in onboarded_by_slug and slug not in removed) or slug in pricing_models
        if not eligible:
            raise ProposalError(
                f"prices.{slug}", "not an onboarded endpoint or a priced model")
        _validate_price_fields(f"prices.{slug}", fields)

    # -- workload --
    workload = proposal.get("workload") or {}
    _validate_workload_fields("workload", workload)
    for app_id, fields in (workload.get("apps") or {}).items():
        if app_id not in apps_by_id:
            raise ProposalError(f"workload.apps.{app_id}", "unknown application")
        _validate_workload_fields(f"workload.apps.{app_id}", fields)

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
        row = onboarded_out.get(slug)     # None for a YAML pool model: latency_ms only
        for field, value in fields.items():
            if field == "latency_ms":
                continue                  # an assumption, not a row field: assumptions_block
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

    # Absent when the proposal sets none, so an empty proposal stays a true
    # identity.
    assumptions = assumptions_block(proposal)
    if assumptions is not None:
        out["assumptions"] = assumptions

    return out
