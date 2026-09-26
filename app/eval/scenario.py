"""offline, operator-run; builds ONE scenario (settings + scratch registry +
quality + frozen signals + compiled apps) from a config snapshot; never
touches a deployment database (spec §4, §6).

A scenario is everything a routing decision reads. Two are built for the
policy simulator: `current` (from the live snapshot) and `proposed` (from the
snapshot plus a proposal) -- both share exactly one `FrozenSignals` reading so
a signal difference can never masquerade as a policy effect (spec §6.4).
"""
from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import dataclass, fields as dc_fields

from pydantic import ValidationError

from app.apps.db import Database
from app.apps.policy import AppPolicyView
from app.apps.privacy import AppPrivacyProfile, SensitivityRuleView, effective_rules
from app.apps.registry import ModelRegistry
from app.apps.snapshot import SERVING_POLICY_FIELDS
from app.apps.store import upsert_onboarded
from app.config.lint import lint_settings
from app.config.schema import PolicyCfg, PricingCfg, RoutingCfg, Settings, SignalsCfg
from app.policy.compile import EffectivePolicy, compile_policy
from app.policy.enums import PII_MODES, POLICY_MODES
from app.routing.quality import QualityResolver, QualityScore
from app.routing.router import Router
from app.routing.signals import SignalValue

SCHEMA_PREFIX = "routeforge.config-snapshot/"
SCHEMA_MAJOR = "v1"

_REQUIRED_TOP_KEYS = (
    "schema", "generated_at", "routing_policy_version", "registry_version",
    "settings", "onboarded", "apps", "quality", "signals",
)

# AppPolicyView fields whose value is `frozenset[str] | None` -- the snapshot
# spells these as JSON lists (see `app/apps/snapshot.py::_jsonable`).
_SET_FIELDS = frozenset({
    "allowed_tiers", "allowed_providers", "allowed_models",
    "pii_entity_denylist", "allowed_regions",
})
_APP_POLICY_FIELDS = frozenset(f.name for f in dc_fields(AppPolicyView))


class SnapshotError(ValueError):
    """A config-snapshot load/build error. Always names the offending
    location so a caller doesn't have to re-read the document to act on it."""

    def __init__(self, where: str, message: str) -> None:
        self.where = where
        super().__init__(f"{where}: {message}")


def load_snapshot(path: str) -> tuple[dict, str]:
    """Read a config-snapshot JSON file. Returns `(doc, sha256 hex)` -- the
    hash is over the raw file bytes, so it changes with any byte, including
    whitespace. Raises `SnapshotError` on invalid JSON or a failed
    `validate_snapshot`."""
    with open(path, "rb") as f:
        raw = f.read()
    digest = hashlib.sha256(raw).hexdigest()
    try:
        doc = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise SnapshotError("file", f"invalid JSON: {exc}") from exc
    validate_snapshot(doc)
    return doc, digest


def validate_snapshot(doc: dict) -> None:
    """Cheap, structural checks only -- schema string, required top-level
    keys, the type and required keys of every nested container the builders
    index (so a missing or mistyped key is a `SnapshotError` naming its
    path, never a `KeyError`/`TypeError` traceback), unique app ids. Deeper
    checks (settings validation, registry load, unknown policy fields) happen
    in `build_scenario`, which is where they need live objects (a base
    `Settings`, a scratch DB) to check against."""
    if not isinstance(doc, dict):
        raise SnapshotError("<root>", "must be an object")
    schema = doc.get("schema")
    if not isinstance(schema, str) or not schema.startswith(SCHEMA_PREFIX):
        raise SnapshotError(
            "schema", f"must start with {SCHEMA_PREFIX!r}; got {schema!r}")
    major = schema[len(SCHEMA_PREFIX):].split(".", 1)[0]
    if major != SCHEMA_MAJOR:
        raise SnapshotError(
            "schema", f"major version must be {SCHEMA_MAJOR!r}; got {major!r}")

    for key in _REQUIRED_TOP_KEYS:
        if key not in doc:
            raise SnapshotError(key, "missing")

    _validate_structure(doc)

    app_ids = [a["application_id"] for a in doc["apps"]]
    if len(app_ids) != len(set(app_ids)):
        dupes = sorted({a for a in app_ids if app_ids.count(a) > 1})
        raise SnapshotError("apps", f"duplicate application_id(s): {dupes}")


# The keys each nested snapshot container must carry -- exactly what
# `build_settings`/`build_scenario`/`FrozenSignals` index.
_PROVIDER_KEYS = ("enabled", "region", "prompt_retention", "accepts_raw_sensitive")
_ONBOARDED_KEYS = ("slug", "display_name", "provider", "tier", "quality",
                   "price_input", "price_output", "price_cached_input",
                   "enabled", "allowed_app_ids", "region", "prompt_retention",
                   "accepts_raw_sensitive", "context_window")
_APP_KEYS = ("application_id", "name", "policy", "sensitivity_rules")
_RULE_KEYS = ("entities", "sensitivity", "risk_class", "residency", "require_verification")
_QUALITY_KEYS = ("model", "workload", "score", "set_id")
_SIGNAL_KEYS = ("latency_ms", "latency_n", "availability", "availability_n")


def _need_dict(value, where: str) -> dict:
    if not isinstance(value, dict):
        raise SnapshotError(where, "must be an object")
    return value


def _need_list(value, where: str) -> list:
    if not isinstance(value, list):
        raise SnapshotError(where, "must be a list")
    return value


def _need_keys(obj: dict, keys, where: str) -> None:
    for key in keys:
        if key not in obj:
            raise SnapshotError(f"{where}.{key}", "missing")


def _is_str_list(value) -> bool:
    return isinstance(value, list) and all(isinstance(v, str) for v in value)


def _is_number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _validate_serving_policy(block) -> None:
    """The four deployment posture values `compile_policy` reads (P3a final
    fix, Important 1). A snapshot without them predates the fix: its
    posture is unknown, so it is refused rather than silently taking the
    CLI machine's local config."""
    where = "settings.serving_policy"
    _need_dict(block, where)
    _need_keys(block, SERVING_POLICY_FIELDS, where)
    if block["pii_detection"] not in PII_MODES:
        raise SnapshotError(f"{where}.pii_detection",
                            f"must be one of {sorted(PII_MODES)}; got {block['pii_detection']!r}")
    if block["default_policy_mode"] not in POLICY_MODES:
        raise SnapshotError(
            f"{where}.default_policy_mode",
            f"must be one of {sorted(POLICY_MODES)}; got {block['default_policy_mode']!r}")
    for key in ("allow_app_pii_downgrade", "allow_app_passthrough"):
        if not isinstance(block[key], bool):
            raise SnapshotError(f"{where}.{key}", "must be a bool")


def _validate_structure(doc: dict) -> None:
    settings = _need_dict(doc["settings"], "settings")
    for section in ("routing", "pricing", "policy", "providers", "serving_policy"):
        if section not in settings:
            raise SnapshotError(f"settings.{section}", "missing")
    for name, fields in _need_dict(settings["providers"], "settings.providers").items():
        where = f"settings.providers.{name}"
        _need_keys(_need_dict(fields, where), _PROVIDER_KEYS, where)
    _validate_serving_policy(settings["serving_policy"])

    for i, row in enumerate(_need_list(doc["onboarded"], "onboarded")):
        row = _need_dict(row, f"onboarded[{i}]")
        slug = row.get("slug")
        if not isinstance(slug, str) or not slug:
            raise SnapshotError(f"onboarded[{i}].slug", "must be a non-empty string")
        where = f"onboarded.{slug}"
        _need_keys(row, _ONBOARDED_KEYS, where)
        if not isinstance(row["enabled"], bool):
            raise SnapshotError(f"{where}.enabled", "must be a bool")
        if not isinstance(row["tier"], str):
            raise SnapshotError(f"{where}.tier", "must be a string")
        if row["allowed_app_ids"] is not None and not _is_str_list(row["allowed_app_ids"]):
            raise SnapshotError(f"{where}.allowed_app_ids", "must be null or a list of strings")

    for i, entry in enumerate(_need_list(doc["apps"], "apps")):
        entry = _need_dict(entry, f"apps[{i}]")
        app_id = entry.get("application_id")
        if not isinstance(app_id, str) or not app_id:
            raise SnapshotError(f"apps[{i}].application_id", "must be a non-empty string")
        where = f"apps.{app_id}"
        _need_keys(entry, _APP_KEYS, where)
        policy = _need_dict(entry["policy"], f"{where}.policy")
        for field in _SET_FIELDS:
            value = policy.get(field)
            if value is not None and not _is_str_list(value):
                raise SnapshotError(f"{where}.policy.{field}", "must be null or a list of strings")
        rules = _need_list(entry["sensitivity_rules"], f"{where}.sensitivity_rules")
        for j, rule in enumerate(rules):
            rwhere = f"{where}.sensitivity_rules[{j}]"
            _need_dict(rule, rwhere)
            missing = [k for k in _RULE_KEYS if k not in rule]
            if missing:
                raise SnapshotError(rwhere, f"missing key(s) {missing}")
            if not _is_str_list(rule["entities"]):
                raise SnapshotError(f"{rwhere}.entities", "must be a list of strings")

    quality = _need_dict(doc["quality"], "quality")
    for bucket in ("customer", "calibrated"):
        if bucket not in quality:
            raise SnapshotError(f"quality.{bucket}", "missing")
        for i, q in enumerate(_need_list(quality[bucket], f"quality.{bucket}")):
            qwhere = f"quality.{bucket}[{i}]"
            _need_dict(q, qwhere)
            missing = [k for k in _QUALITY_KEYS if k not in q]
            if missing:
                raise SnapshotError(qwhere, f"missing key(s) {missing}")

    for model, row in _need_dict(doc["signals"], "signals").items():
        swhere = f"signals.{model}"
        _need_dict(row, swhere)
        bad = [k for k in _SIGNAL_KEYS if not _is_number(row.get(k))]
        if bad:
            raise SnapshotError(swhere, f"missing or non-numeric key(s) {bad}")


def build_settings(doc: dict, base: Settings) -> Settings:
    """The settings half of `build_scenario` (controller ruling: public, so
    later tasks can build settings without the rest of a scenario).

    Starts from `copy.deepcopy(base)`, replaces `routing`/`pricing`/`policy`
    wholesale with the snapshot's sections, overlays each declared provider's
    four fields and the four `serving_policy` posture values, sets
    `routing_policy_version`, then runs the same
    cross-file lint a boot runs. A snapshot that fails validation or lint is
    a load error, named at its location."""
    validate_snapshot(doc)
    settings = copy.deepcopy(base)
    doc_settings = doc["settings"]

    update = {}
    for section, cls in (("routing", RoutingCfg), ("pricing", PricingCfg),
                         ("policy", PolicyCfg)):
        try:
            update[section] = cls.model_validate(doc_settings[section])
        except ValidationError as exc:
            raise SnapshotError(f"settings.{section}", str(exc)) from exc
    settings = settings.model_copy(update=update)

    settings.routing_policy_version = doc["routing_policy_version"]

    for name, fields in doc_settings["providers"].items():
        cfg = settings.serving.providers.get(name)
        if cfg is None:
            raise SnapshotError(
                f"settings.providers.{name}",
                "provider not declared in this deployment's serving.yaml")
        cfg.enabled = fields["enabled"]
        cfg.region = fields["region"]
        cfg.prompt_retention = fields["prompt_retention"]
        cfg.accepts_raw_sensitive = fields["accepts_raw_sensitive"]

    # The deployment's posture defaults and ceilings (P3a final fix,
    # Important 1): `compile_policy` reads these, so the SNAPSHOT's values
    # decide an app's compiled posture -- never the machine running the
    # simulator (its serving.yaml or SP_* env overrides).
    for key, value in doc_settings["serving_policy"].items():
        if key in SERVING_POLICY_FIELDS:
            setattr(settings.serving, key, value)

    violations = lint_settings(settings)
    if violations:
        raise SnapshotError("settings", "; ".join(violations))

    return settings


@dataclass(frozen=True)
class FrozenSignals:
    """A frozen replay of `SignalStore.latency`/`availability` (spec §6.4):
    same shapes and sources, but no clock -- no staleness check, no
    wall-clock decay. The snapshot IS the moment an operator froze it; a
    scenario built from it must not silently age past that moment while a
    proposal is being evaluated."""

    cfg: SignalsCfg
    values: dict[str, dict]

    def _tier_default(self, tier: str) -> float:
        return self.cfg.tier_latency_ms.get(tier, self.cfg.latency_scale_ms)

    def latency(self, model: str, tier: str, *, now: float | None = None) -> SignalValue:
        default = self._tier_default(tier)
        if not self.cfg.enabled:
            return SignalValue(default, "disabled", 0)
        row = self.values.get(model)
        n = int(row["latency_n"]) if row else 0
        if row is None or n < self.cfg.min_samples:
            return SignalValue(default, "tier_default", n)
        return SignalValue(float(row["latency_ms"]), "measured", n)

    def availability(self, model: str, *, now: float | None = None) -> SignalValue:
        if not self.cfg.enabled:
            return SignalValue(1.0, "disabled", 0)
        row = self.values.get(model)
        n = int(row["availability_n"]) if row else 0
        if row is None or n < self.cfg.min_samples:
            return SignalValue(1.0, "neutral", n)
        value = min(1.0, max(0.0, float(row["availability"])))
        return SignalValue(value, "measured", n)

    def snapshot_latency(self, model: str) -> float | None:
        """The frozen `latency_ms` for `model`, used by the latency ladder
        (Task 4) -- `None` when signals are disabled or there aren't enough
        samples to trust it."""
        if not self.cfg.enabled:
            return None
        row = self.values.get(model)
        if row is None or int(row["latency_n"]) < self.cfg.min_samples:
            return None
        return float(row["latency_ms"])


@dataclass(frozen=True)
class AppScenario:
    application_id: str
    name: str
    view: AppPolicyView
    rules: list
    eff: EffectivePolicy


@dataclass
class Scenario:
    settings: Settings
    registry: ModelRegistry
    quality: QualityResolver
    signals: FrozenSignals
    apps: dict[str, AppScenario]
    router: Router
    db: Database


def _app_policy_view(policy: dict, app_id: str) -> AppPolicyView:
    converted: dict = {}
    for key, value in policy.items():
        if key not in _APP_POLICY_FIELDS:
            raise SnapshotError(f"apps.{app_id}.policy.{key}", "unknown field")
        if key in _SET_FIELDS and value is not None:
            converted[key] = frozenset(value)
        else:
            converted[key] = value
    return AppPolicyView(**converted)


def app_rules(entry: dict, settings: Settings) -> list[SensitivityRuleView]:
    """One snapshot app entry's resolved sensitivity rules -- its own
    `sensitivity_rules` if any, else `settings.policy`'s, always ending in
    the public catch-all (`effective_rules`). The replay's rule list for
    that app; public so offline callers classify rows exactly as replay
    does (P3c-1 `risk_floor` candidacy). Only `settings.policy` is read."""
    profile = AppPrivacyProfile(
        recognizers=(),
        rules=tuple(
            SensitivityRuleView(
                entities=frozenset(r["entities"]),
                sensitivity=r["sensitivity"],
                risk_class=r["risk_class"],
                residency=r["residency"],
                require_verification=r["require_verification"])
            for r in entry["sensitivity_rules"]))
    return effective_rules(profile, settings)


async def build_scenario(doc: dict, *, base: Settings) -> Scenario:
    settings = build_settings(doc, base)   # runs validate_snapshot first
    canonical_tiers = set(settings.routing.tiers)
    for row in doc["onboarded"]:
        if row["tier"] not in canonical_tiers:
            raise SnapshotError(
                f"onboarded.{row['slug']}.tier",
                f"unknown tier {row['tier']!r}; legal tiers: {sorted(canonical_tiers)}")

    db = Database(url="sqlite+aiosqlite://")
    await db.ensure_ready()
    try:
        async with db.session() as session:
            for row in doc["onboarded"]:
                await upsert_onboarded(
                    session, row["slug"],
                    display_name=row["display_name"],
                    provider=row["provider"],
                    tier=row["tier"],
                    quality=row["quality"],
                    price_input=row["price_input"],
                    price_output=row["price_output"],
                    price_cached_input=row["price_cached_input"],
                    endpoint=f"sim://{row['slug']}",
                    api_key_env=None,
                    enabled=row["enabled"],
                    probe_status="healthy",
                    allowed_app_ids=row["allowed_app_ids"],
                    region=row["region"],
                    prompt_retention=row["prompt_retention"],
                    accepts_raw_sensitive=row["accepts_raw_sensitive"],
                    context_window=row["context_window"],
                )
            await session.commit()

        registry = ModelRegistry(settings)
        await registry.refresh(db)

        loaded = {c.name for c in registry.effective_pool()}
        for row in doc["onboarded"]:
            if row["enabled"] and row["slug"] not in loaded:
                raise SnapshotError(
                    f"onboarded.{row['slug']}",
                    "did not load into the scratch registry")

        quality = QualityResolver(settings, registry=registry)
        overrides: dict = {}
        for entry in doc["quality"]["customer"]:
            overrides.setdefault((entry["model"], entry["workload"]), []).append(
                QualityScore(entry["score"], "customer", entry["set_id"]))
        for entry in doc["quality"]["calibrated"]:
            overrides.setdefault((entry["model"], entry["workload"]), []).append(
                QualityScore(entry["score"], "calibrated", entry["set_id"]))
        quality.set_overrides(overrides)

        signals = FrozenSignals(settings.routing.signals, doc["signals"])

        apps: dict[str, AppScenario] = {}
        for entry in doc["apps"]:
            app_id = entry["application_id"]
            view = _app_policy_view(entry["policy"], app_id)
            rules = app_rules(entry, settings)
            eff = compile_policy(view, settings)
            apps[app_id] = AppScenario(
                application_id=app_id, name=entry["name"], view=view, rules=rules, eff=eff)

        router = Router(settings, quality_resolver=quality, registry=registry, signals=signals)

        return Scenario(settings=settings, registry=registry, quality=quality,
                        signals=signals, apps=apps, router=router, db=db)
    except BaseException:
        await db.dispose()
        raise


async def close_scenario(scn: Scenario) -> None:
    await scn.db.dispose()
