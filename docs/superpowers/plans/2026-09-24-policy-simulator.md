# P3a Policy Counterfactual Simulator Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Predict, offline and without activating anything, how a proposed policy, endpoint or
price change would re-route an operator's real recorded traffic, and what it would do to cost,
latency, quality and rejections.

**Architecture:**
- A read-only admin endpoint exports a content-free config snapshot.
- An operator-written proposal is applied to that snapshot as a pure dict transform, which
  yields a second snapshot.
- Each snapshot is built into a **scenario**: `Settings`, a scratch `ModelRegistry`, a
  `QualityResolver`, frozen signals and compiled per-app policies.
- Every P2b-2 outcome row is replayed through the **real** `build_context` → `feasible_set` →
  `Router.select` under both scenarios. The two simulated arms are then diffed.

**Tech Stack:** Python 3.12, FastAPI, Pydantic v2, SQLAlchemy async + aiosqlite (scratch
registry), Hypothesis (property test), pytest.

**Spec:** `docs/superpowers/specs/2026-09-24-policy-simulator-design.md`

## Global Constraints

- **Import direction:** serving code never imports `app.eval`, which `tests/eval/test_isolation.py`
  enforces. `app/eval/*` may import serving code.
- **The simulator activates nothing:**
  - no network;
  - no write to any deployment database (the scratch registry is always `sqlite+aiosqlite://`);
  - no admin write.
- **The snapshot is content-free and secret-free.** It never contains:
  - endpoint URLs, `api_key_env`, or anything from `serving.observability`
    (Langfuse keys/hosts);
  - `pii_exempt_patterns`;
  - custom recognizer definitions;
  - per-app Langfuse/credential columns.
- **Schema strings, verbatim:**
  - `routeforge.config-snapshot/v1`
  - `routeforge.policy-proposal/v1`
  - `routeforge.simulation/v1`
- **The headline diff is always simulated-current vs simulated-proposed,** never logged vs
  simulated.
- **Simulated statuses:** `ok`, `backend_failed`, `verify_failed`, `no_eligible`,
  `context_overflow`.
- **Exclusion reasons:** `cache_hit`, `unknown_status`, `unknown_app`, `missing_features`.
- **Determinism:** `simulation.json` is byte-identical for identical inputs.
  - Floats are rounded to 6 places.
  - `json.dumps(..., sort_keys=True, indent=2)`.
  - Rows are processed in `(ts, request_id)` order.
- **CLI exit codes:**
  - 0: written.
  - 2: an input failed to load or validate.
  - 3: new rejections when `--fail-on-new-rejections` is set.
- **Baseline warning threshold:** agreement < 0.90.
- **Rules of the project (CLAUDE.md):**
  - `onprem_only` is absolute (rule 3).
  - Residency beats the risk floor (rule 8).
  - A simulated `onprem_only` pick must never be a cloud provider.
- **Tests:** run with
  `LITELLM_LOCAL_MODEL_COST_MAP=True /home/manimal/projects/routeForge/.venv/bin/python -m pytest …`
  from the worktree root (`/home/manimal/projects/routeForge/.worktrees/policy-simulator`).
  - The worktree has no venv of its own.
  - Without the env var, pytest can hang on litellm's network cost-map fetch.
  - There is no pytest-asyncio. Async code in tests goes through `asyncio.run(...)`, or through
    `client.portal.call(...)` on a `TestClient`.
- Match the surrounding code: module docstrings that state the contract, type hints everywhere,
  and boring readable code.

## File map

| File | Status | Responsibility |
|---|---|---|
| `app/apps/snapshot.py` | create | `build_config_snapshot(...)`: serving-side, read-only, content-free document |
| `app/api/admin_routes.py` | modify | `GET /admin/config-snapshot` |
| `tests/test_config_snapshot.py` | create | Endpoint auth, shape, secret-freedom |
| `app/eval/scenario.py` | create | `load_snapshot`, `build_scenario`, `Scenario`, `FrozenSignals`, `SnapshotError` |
| `tests/eval/sim_fixtures.py` | create | `make_snapshot(...)`, `make_row(...)`: shared test builders |
| `tests/eval/test_scenario.py` | create | Scenario construction + round trip vs live `feasible_set` |
| `app/eval/proposal.py` | create | `load_proposal`, `apply_proposal`, `ProposalError` |
| `tests/eval/test_proposal.py` | create | Every edit kind + every load error |
| `app/eval/simulate.py` | create | Row selection, per-arm replay, aggregation, `render_report` |
| `tests/eval/test_simulate.py` | create | Invariants §9.1–§9.6, §9.8 |
| `scripts/policy_simulate.py` | create | CLI |
| `tests/eval/test_policy_simulate_cli.py` | create | Exit codes, files written, end-to-end baseline (§9.7) |
| `docs/reference/policy-simulator.md` | create | Operator guide |
| `docs/research/feasible-route-bench.md` | modify | §15 |
| `docs/backlog/bugs.md` | modify | B55 |
| `tests/eval/test_isolation.py` | unchanged | Already scans every `app/` module; it keeps passing |

---

### Task 1: Config snapshot builder + `GET /admin/config-snapshot`

**Files:**
- Create: `app/apps/snapshot.py`
- Modify: `app/api/admin_routes.py` (add the route beside `export_outcomes`, ~line 1255)
- Test: `tests/test_config_snapshot.py`

**Interfaces:**
- Consumes (existing):
  - `store.list_onboarded(session)`, which returns `OnboardedModel` rows, disabled ones
    included;
  - `store.list_applications(session)`;
  - `store.get_application_with_policy(session, app_id)`;
  - `policy_view_from(app)` (`app/apps/policy.py:126`);
  - the `AppSensitivityRule` rows (`app/apps/models.py:195`, ordered by `position`);
  - `QualityResolver.override_maps()`, which returns `(customer, calibrated)` dicts
    `{(model, workload|None): QualityScore(score, source, set_id)}`;
  - `SignalStore.snapshot()`;
  - `ModelRegistry.registry_version()`;
  - `settings.routing_policy_version`.
- Produces:
  - `async def build_config_snapshot(session, *, settings: Settings, registry: ModelRegistry, quality: QualityResolver, signals: SignalStore, now: datetime) -> dict`
  - the document shape in the spec §4. Later tasks rely on the exact keys below.

**Document keys (exact):**
- Top level: `schema`, `generated_at` (ISO-8601 UTC with `Z`), `routing_policy_version`,
  `registry_version`, `settings`, `onboarded`, `apps`, `quality`, `signals`.
- `settings`:
  - `routing`: `settings.routing.model_dump(mode="json")`;
  - `pricing`: `settings.pricing.model_dump(mode="json")`;
  - `policy`: `settings.policy.model_dump(mode="json")`;
  - `providers`: `{name: {"enabled", "region", "prompt_retention", "accepts_raw_sensitive"}}`.
- `onboarded[]`: `slug`, `display_name`, `provider`, `tier`, `quality`, `price_input`,
  `price_output`, `price_cached_input`, `enabled`, `allowed_app_ids` (a list or null), `region`,
  `prompt_retention`, `accepts_raw_sensitive`, `context_window`. Sorted by `slug`.
- `apps[]`: `application_id`, `name`, `policy`, `sensitivity_rules`. Sorted by
  `application_id`.
  - `policy` is every `AppPolicyView` dataclass field except `pii_exempt_patterns`. Frozensets
    become sorted lists.
  - `sensitivity_rules` is a list of `{"entities": sorted list, "sensitivity", "risk_class",
    "residency", "require_verification"}` in `position` order. It is `[]` when the app has none,
    in which case the global `policy.sensitivity_rules` apply.
- `quality`: `{"customer": [...], "calibrated": [...]}`.
  - Each entry is `{"model", "workload", "score", "set_id"}`.
  - Entries are sorted by `(model, workload or "")`.
- `signals`: `SignalStore.snapshot()` verbatim, with keys sorted by `json.dumps(sort_keys=True)`
  at the response.

- [ ] **Step 1: Write the failing tests** in `tests/test_config_snapshot.py`:

```python
"""P3a Task 1: `GET /admin/config-snapshot` -- admin-only, read-only,
content-free and secret-free (spec §4)."""
from __future__ import annotations

import json

from fastapi.testclient import TestClient

from app.main import create_app
from tests.test_admin_api import _login

FORBIDDEN_KEYS = {"endpoint", "api_key_env", "observability", "pii_exempt_patterns",
                  "recognizers", "langfuse_public_key", "langfuse_secret_key",
                  "langfuse_secret_enc", "public_key", "secret_key"}


def _all_keys(obj) -> set[str]:
    keys: set[str] = set()
    if isinstance(obj, dict):
        for k, v in obj.items():
            keys.add(k)
            keys |= _all_keys(v)
    elif isinstance(obj, list):
        for v in obj:
            keys |= _all_keys(v)
    return keys


def test_snapshot_requires_admin():
    with TestClient(create_app()) as c:
        assert c.get("/admin/config-snapshot").status_code == 401


def test_snapshot_shape_and_versions():
    with TestClient(create_app()) as c:
        _login(c)
        r = c.get("/admin/config-snapshot")
        assert r.status_code == 200
        doc = r.json()
        assert doc["schema"] == "routeforge.config-snapshot/v1"
        assert doc["routing_policy_version"] == c.app.state.settings.routing_policy_version
        assert doc["registry_version"] == c.app.state.registry.registry_version()
        assert set(doc["settings"]) == {"routing", "pricing", "policy", "providers"}
        for p in doc["settings"]["providers"].values():
            assert set(p) == {"enabled", "region", "prompt_retention", "accepts_raw_sensitive"}
        slugs = [o["slug"] for o in doc["onboarded"]]
        assert slugs == sorted(slugs) and "local-8b" in slugs   # SP_SEED_TEST_MODELS
        ids = [a["application_id"] for a in doc["apps"]]
        assert ids == sorted(ids) and "demo" in ids
        demo = next(a for a in doc["apps"] if a["application_id"] == "demo")
        assert "pii_exempt_patterns" not in demo["policy"]
        assert "residency" in demo["policy"]
        assert set(doc["quality"]) == {"customer", "calibrated"}


def test_snapshot_has_no_forbidden_keys_or_secret_values(monkeypatch):
    monkeypatch.setenv("SP_ONPREM_API_KEY", "sk-SEEDED-SECRET-VALUE-123")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "lf-SEEDED-SECRET-456")
    with TestClient(create_app()) as c:
        _login(c)
        doc = c.get("/admin/config-snapshot").json()
    assert _all_keys(doc) & FORBIDDEN_KEYS == set()
    text = json.dumps(doc)
    assert "sk-SEEDED-SECRET-VALUE-123" not in text
    assert "lf-SEEDED-SECRET-456" not in text
    assert "http://" not in text and "https://" not in text   # no endpoint URLs


def test_snapshot_includes_disabled_onboarded_rows():
    with TestClient(create_app()) as c:
        _login(c)
        # disable one seeded model through the real admin path
        slug = c.get("/admin/config-snapshot").json()["onboarded"][0]["slug"]
        r = c.patch(f"/admin/infra/models/{slug}", json={"enabled": False})
        assert r.status_code in (200, 204), r.text
        doc = c.get("/admin/config-snapshot").json()
        row = next(o for o in doc["onboarded"] if o["slug"] == slug)
        assert row["enabled"] is False
```

Before relying on the disable call, confirm the real admin route and method for disabling an
onboarded model with `grep -n "infra/models" app/api/admin_routes.py`. If it is not
`PATCH /admin/infra/models/{slug}` with `{"enabled": false}`, use whatever that grep shows. The
assertion stays the same.

Check `_login`'s signature in `tests/test_admin_api.py:11`. `test_admin_api` may rely on a
module-level `client` fixture; `_login(client)` only needs a `TestClient`.

If `"http://"` also appears legitimately in `settings.routing` or `settings.policy` (for
example a documentation URL in a config value), narrow the URL assertion: collect every
`onboarded` endpoint from `store.list_onboarded` and assert none of *those* strings appears.
Keep the stronger form if it passes.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `LITELLM_LOCAL_MODEL_COST_MAP=True /home/manimal/projects/routeForge/.venv/bin/python -m pytest tests/test_config_snapshot.py -q`

Expected: FAIL (404 on `/admin/config-snapshot`).

- [ ] **Step 3: Implement `app/apps/snapshot.py`**

```python
"""P3a: the content-free config snapshot `GET /admin/config-snapshot` serves
(spec §4) -- everything a routing decision reads, and nothing else.

Read-only. Built from the live settings, registry rows, app policies, quality
overrides and signals so the offline simulator (`app/eval/scenario.py`) can
rebuild the SAME eligibility and routing inputs without touching the
deployment. Content-free and secret-free BY CONSTRUCTION: every field is
listed explicitly below -- nothing is dumped wholesale except the routing,
pricing and policy config sections, which hold no secrets. Excluded: endpoint
URLs, `api_key_env`, `serving.observability` (Langfuse), `pii_exempt_patterns`
(operator regexes), custom recognizers, and per-app credential columns.
"""
from __future__ import annotations

import dataclasses
import datetime as dt

from sqlalchemy import select

from app.apps import store
from app.apps.models import AppSensitivityRule
from app.apps.policy import AppPolicyView, policy_view_from

SCHEMA = "routeforge.config-snapshot/v1"

_ONBOARDED_FIELDS = ("slug", "display_name", "provider", "tier", "quality",
                     "price_input", "price_output", "price_cached_input",
                     "enabled", "allowed_app_ids", "region", "prompt_retention",
                     "accepts_raw_sensitive", "context_window")
_PROVIDER_FIELDS = ("enabled", "region", "prompt_retention", "accepts_raw_sensitive")
_EXCLUDED_POLICY_FIELDS = frozenset({"pii_exempt_patterns"})


def _jsonable(value):
    if isinstance(value, (set, frozenset)):
        return sorted(value)
    return value


def policy_dict(view: AppPolicyView) -> dict:
    """Every `AppPolicyView` field except the excluded ones; sets become
    sorted lists. Shared with the simulator's tests as the one spelling."""
    return {f.name: _jsonable(getattr(view, f.name))
            for f in dataclasses.fields(view)
            if f.name not in _EXCLUDED_POLICY_FIELDS}


def _quality_entries(overrides: dict) -> list[dict]:
    rows = [{"model": model, "workload": workload, "score": q.score, "set_id": q.set_id}
            for (model, workload), q in overrides.items()]
    return sorted(rows, key=lambda r: (r["model"], r["workload"] or ""))


async def _rules(session, app_id: str) -> list[dict]:
    stmt = (select(AppSensitivityRule)
            .where(AppSensitivityRule.application_id == app_id)
            .order_by(AppSensitivityRule.position))
    return [{"entities": sorted(r.entities), "sensitivity": r.sensitivity,
             "risk_class": r.risk_class, "residency": r.residency,
             "require_verification": bool(r.require_verification)}
            for r in (await session.execute(stmt)).scalars()]


async def build_config_snapshot(session, *, settings, registry, quality, signals,
                                now: dt.datetime) -> dict:
    onboarded = []
    for r in await store.list_onboarded(session):
        row = {k: getattr(r, k) for k in _ONBOARDED_FIELDS}
        row["enabled"] = bool(row["enabled"])
        row["allowed_app_ids"] = (list(row["allowed_app_ids"])
                                  if row["allowed_app_ids"] else None)
        onboarded.append(row)
    apps = []
    for a in await store.list_applications(session):
        full = await store.get_application_with_policy(session, a.id)
        apps.append({"application_id": a.id, "name": a.name,
                     "policy": policy_dict(policy_view_from(full)),
                     "sensitivity_rules": await _rules(session, a.id)})
    customer, calibrated = quality.override_maps()
    providers = {name: {k: getattr(cfg, k) for k in _PROVIDER_FIELDS}
                 for name, cfg in settings.serving.providers.items()}
    return {
        "schema": SCHEMA,
        "generated_at": now.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "routing_policy_version": settings.routing_policy_version,
        "registry_version": registry.registry_version(),
        "settings": {
            "routing": settings.routing.model_dump(mode="json"),
            "pricing": settings.pricing.model_dump(mode="json"),
            "policy": settings.policy.model_dump(mode="json"),
            "providers": providers,
        },
        "onboarded": sorted(onboarded, key=lambda o: o["slug"]),
        "apps": sorted(apps, key=lambda x: x["application_id"]),
        "quality": {"customer": _quality_entries(customer),
                    "calibrated": _quality_entries(calibrated)},
        "signals": signals.snapshot(),
    }
```

Before writing this, verify two things:
- The column names on `OnboardedModel` match `_ONBOARDED_FIELDS` (`app/apps/models.py`).
- The `ProviderCfg` field names match `_PROVIDER_FIELDS` (`app/config/schema.py` ~line 239).

If a name differs, use the real name as the source and map it to the spec key.

- [ ] **Step 4: Add the route** in `app/api/admin_routes.py`, directly after `export_outcomes`:

```python
# --- policy simulator snapshot (P3a) -----------------------------------------

@admin_router.get("/config-snapshot")
async def config_snapshot(request: Request, _: str = Depends(require_admin)) -> dict:
    """Content-free, secret-free snapshot of everything a routing decision
    reads (P3a spec §4), for the offline policy simulator
    (`scripts/policy_simulate.py`). Read-only: it changes nothing."""
    state = request.app.state
    async with state.db.session() as session:
        return await build_config_snapshot(
            session, settings=state.settings, registry=state.registry,
            quality=state.quality, signals=state.signals,
            now=dt.datetime.now(dt.timezone.utc))
```

Add `from app.apps.snapshot import build_config_snapshot` to the imports. Confirm that the
router object is named `admin_router` and that its prefix makes the path
`/admin/config-snapshot`, the same way `/admin/outcomes/export` is declared.

- [ ] **Step 5: Run the tests** (same command). Expected: PASS. Then run the neighbouring
  suites: `tests/test_outcome_export.py tests/test_admin_api.py -q`, expecting PASS.

- [ ] **Step 6: Commit**

```bash
git add app/apps/snapshot.py app/api/admin_routes.py tests/test_config_snapshot.py
git commit -m "feat(admin): content-free config snapshot for the policy simulator (P3a)"
```

---

### Task 2: Scenario construction (`app/eval/scenario.py`)

**Files:**
- Create: `app/eval/scenario.py`, `tests/eval/sim_fixtures.py`, `tests/eval/test_scenario.py`

**Interfaces:**
- Consumes: the Task 1 document shape.
- Produces:

```python
class SnapshotError(ValueError):
    def __init__(self, where: str, message: str): ...   # str(e) == f"{where}: {message}"

def load_snapshot(path: str) -> tuple[dict, str]           # (doc, sha256 hex of file bytes)
def validate_snapshot(doc: dict) -> None                   # raises SnapshotError

class FrozenSignals:
    def __init__(self, cfg: SignalsCfg, values: dict[str, dict]): ...
    def latency(self, model: str, tier: str, *, now: float | None = None) -> SignalValue
    def availability(self, model: str, *, now: float | None = None) -> SignalValue
    def snapshot_latency(self, model: str) -> float | None   # used by Task 4's latency ladder

@dataclass(frozen=True)
class AppScenario:
    application_id: str
    name: str
    view: AppPolicyView
    rules: list            # effective_rules(...) output: list[SensitivityRuleView]
    eff: EffectivePolicy

@dataclass
class Scenario:
    settings: Settings
    registry: ModelRegistry
    quality: QualityResolver
    signals: FrozenSignals
    apps: dict[str, AppScenario]
    router: Router
    db: Database            # scratch; closed by close_scenario

async def build_scenario(doc: dict, *, base: Settings) -> Scenario
async def close_scenario(scn: Scenario) -> None
```

The test helpers in `tests/eval/sim_fixtures.py` are used by Tasks 3–5:

```python
def make_snapshot(*, onboarded: list[dict] | None = None, apps: list[dict] | None = None,
                  providers: dict[str, dict] | None = None,
                  quality: dict | None = None, signals: dict | None = None) -> dict
def onboarded_row(slug: str, tier: str, **over) -> dict
def app_entry(app_id: str, **policy) -> dict
def make_row(request_id: str, app_id: str, **over) -> dict   # a P2b-2 outcome record
BASE_SETTINGS: Settings                                        # load_settings("config"), loaded once
```

**Rules (from the spec, §6):**
- **Settings:** start from a `copy.deepcopy(base)`. Then:
  - Replace `routing`, `pricing` and `policy` with `RoutingCfg.model_validate(...)`,
    `PricingCfg.model_validate(...)` and `PolicyCfg.model_validate(...)` of the snapshot's
    sections, via `settings.model_copy(update=...)`.
  - Set `routing_policy_version` from the doc.
  - Overlay each provider's four fields onto `settings.serving.providers[name]`. A provider
    name not in `base.serving.providers` raises `SnapshotError("settings.providers.<name>", "provider not declared in this deployment's serving.yaml")`.
  - Run `app.config.lint.lint_settings(settings)`. A non-empty list raises
    `SnapshotError("settings", "; ".join(violations))`.
  - A Pydantic `ValidationError` raises `SnapshotError("settings.<section>", str(exc))`.
- **Registry:** follows `app/eval/endpoints.py::build_registry`.
  - Use `Database(url="sqlite+aiosqlite://")`, then `await db.ensure_ready()`.
  - Write one `store.upsert_onboarded(session, slug, display_name=…, provider=…, tier=…, quality=…, price_input=…, price_output=…, price_cached_input=…, endpoint=f"sim://{slug}", api_key_env=None, enabled=row["enabled"], probe_status="healthy", allowed_app_ids=row["allowed_app_ids"], region=…, prompt_retention=…, accepts_raw_sensitive=…, context_window=…)` per row.
  - Then `ModelRegistry(settings)` and `await registry.refresh(db)`.
  - Every `enabled` slug must be in `{c.name for c in registry.effective_pool()}`. Otherwise
    raise `SnapshotError("onboarded.<slug>", "did not load into the scratch registry")`.
  - Any failure after the DB exists disposes it (`await db.dispose()`) before re-raising.
- **Quality:**
  - `QualityResolver(settings, registry=registry)`.
  - Then `set_overrides({(e["model"], e["workload"]): QualityScore(e["score"], source, e["set_id"]) ...})`
    over both lists.
  - A key present in both lists maps to `[customer_q, calibrated_q]`.
- **Signals:** `FrozenSignals(settings.routing.signals, doc["signals"])`. It has the same
  semantics as `SignalStore.latency` / `availability`, minus the clock:
  - `cfg.enabled` false: `SignalValue(default, "disabled", 0)` / `SignalValue(1.0, "disabled", 0)`.
  - The model is absent, or `n < cfg.min_samples`: `SignalValue(tier_default, "tier_default", n_or_0)`
    / `SignalValue(1.0, "neutral", n_or_0)`.
  - Otherwise `SignalValue(latency_ms, "measured", n)` / `SignalValue(clamp(availability), "measured", n)`.
  - The tier default is `cfg.tier_latency_ms.get(tier, cfg.latency_scale_ms)`.
  - Staleness and decay are not applied: the snapshot is the moment the operator froze. Say
    so in the class docstring.
  - `snapshot_latency(model)` returns the `latency_ms` when `latency_n >= min_samples` and
    signals are enabled, else `None`.
- **Apps:** for each `apps[]` entry:
  - Build the view as `AppPolicyView(**converted)`, converting every list-valued field in
    `{"allowed_tiers","allowed_providers","allowed_models","pii_entity_denylist","allowed_regions"}`
    to a `frozenset`.
  - An unknown field raises `SnapshotError("apps.<id>.policy.<field>", "unknown field")`.
  - `rules = effective_rules(AppPrivacyProfile(recognizers=(), rules=tuple(SensitivityRuleView(entities=frozenset(r["entities"]), sensitivity=…, risk_class=…, residency=…, require_verification=…) for r in entry["sensitivity_rules"])), settings)`.
  - `eff = compile_policy(view, settings)`.
- **Router:** `Router(settings, quality_resolver=quality, registry=registry, signals=frozen)`.
- **`validate_snapshot`:**
  - The schema must be a string starting `routeforge.config-snapshot/`, with major `v1`.
    Otherwise raise `SnapshotError("schema", ...)`.
  - The required top-level keys must be present, each missing one raising
    `SnapshotError("<key>", "missing")`.
  - Application ids must be unique.
- **`load_snapshot`:**
  - Read the bytes; the sha256 is over the raw bytes.
  - Run `json.loads`; a JSON error raises `SnapshotError("file", ...)`.
  - Call `validate_snapshot`.

- [ ] **Step 1: Write `tests/eval/sim_fixtures.py`**

```python
"""Shared builders for the P3a simulator tests: a minimal valid config
snapshot (Task 1's shape) and P2b-2 outcome records. Test-only."""
from __future__ import annotations

import copy

from app.config.loader import load_settings

BASE_SETTINGS = load_settings("config")


def onboarded_row(slug: str, tier: str, **over) -> dict:
    row = {"slug": slug, "display_name": slug, "provider": "onprem", "tier": tier,
           "quality": None, "price_input": {"cheap": 0.1, "mid": 0.5, "frontier": 2.0}[tier],
           "price_output": {"cheap": 0.2, "mid": 1.0, "frontier": 6.0}[tier],
           "price_cached_input": None, "enabled": True, "allowed_app_ids": None,
           "region": "onprem", "prompt_retention": "none",
           "accepts_raw_sensitive": True, "context_window": None}
    row.update(over)
    return row


def app_entry(app_id: str, *, sensitivity_rules: list | None = None, **policy) -> dict:
    base = {"residency": "any"}
    base.update(policy)
    return {"application_id": app_id, "name": app_id, "policy": base,
            "sensitivity_rules": sensitivity_rules or []}


def make_snapshot(*, onboarded=None, apps=None, providers=None, quality=None,
                  signals=None) -> dict:
    s = BASE_SETTINGS
    prov = {n: {"enabled": c.enabled, "region": c.region,
                "prompt_retention": c.prompt_retention,
                "accepts_raw_sensitive": c.accepts_raw_sensitive}
            for n, c in s.serving.providers.items()}
    for n, over in (providers or {}).items():
        prov[n] = {**prov[n], **over}
    return copy.deepcopy({
        "schema": "routeforge.config-snapshot/v1",
        "generated_at": "2026-09-24T00:00:00Z",
        "routing_policy_version": s.routing_policy_version,
        "registry_version": "test",
        "settings": {"routing": s.routing.model_dump(mode="json"),
                     "pricing": s.pricing.model_dump(mode="json"),
                     "policy": s.policy.model_dump(mode="json"),
                     "providers": prov},
        "onboarded": onboarded if onboarded is not None else [
            onboarded_row("local-cheap", "cheap"),
            onboarded_row("local-mid", "mid"),
            onboarded_row("local-big", "frontier")],
        "apps": apps if apps is not None else [app_entry("app-a")],
        "quality": quality or {"customer": [], "calibrated": []},
        "signals": signals or {},
    })


def make_row(request_id: str, app_id: str, **over) -> dict:
    row = {"request_id": request_id, "application_id": app_id, "status": "ok",
           "ts": "2026-09-20T12:00:00+00:00", "workload": None,
           "complexity": 0.1, "prompt_tokens": 200, "completion_tokens": 100,
           "sensitivity": "public", "risk_class": "low", "onprem_only": False,
           "detected_entity_types": [], "cache_hit": False,
           "attempts": ["local-cheap"], "chosen": "local-cheap",
           "latency_ms": 120.0, "cost_usd": 0.0, "feedback": []}
    row.update(over)
    return row
```

If `ProviderCfg` has no `region` / `prompt_retention` / `accepts_raw_sensitive` attribute
under those names, use the real names, the same ones Task 1 used. The spec keys stay the
same.

- [ ] **Step 2: Write the failing tests** in `tests/eval/test_scenario.py`:

```python
"""P3a Task 2: building a scenario from a config snapshot (spec §6)."""
from __future__ import annotations

import asyncio
import json

import pytest

from app.eval.scenario import (SnapshotError, build_scenario, close_scenario,
                               load_snapshot, validate_snapshot)
from app.policy.feasible import feasible_set
from tests.eval.sim_fixtures import BASE_SETTINGS, app_entry, make_snapshot, onboarded_row


def _build(doc):
    async def go():
        scn = await build_scenario(doc, base=BASE_SETTINGS)
        try:
            return {
                "pool": sorted(c.name for c in scn.registry.effective_pool()),
                "apps": sorted(scn.apps),
                "price": scn.registry.price_for("local-mid").input,
                "lat": scn.signals.latency("local-mid", "mid"),
                "q": scn.quality.resolve("local-mid", None),
                "eff_residency": scn.apps["app-a"].eff.placement.residency,
            }
        finally:
            await close_scenario(scn)
    return asyncio.run(go())


def test_builds_pool_apps_prices():
    out = _build(make_snapshot())
    assert {"local-cheap", "local-mid", "local-big"} <= set(out["pool"])
    assert out["apps"] == ["app-a"]
    assert out["price"] == 0.5


def test_disabled_onboarded_row_is_not_in_pool():
    doc = make_snapshot(onboarded=[onboarded_row("local-cheap", "cheap"),
                                   onboarded_row("local-mid", "mid", enabled=False)])
    assert "local-mid" not in _build(doc)["pool"]


def test_frozen_signals_measured_and_default():
    doc = make_snapshot(signals={"local-mid": {"latency_ms": 321.0, "latency_n": 50,
                                               "availability": 0.9, "availability_n": 50}})
    lat = _build(doc)["lat"]
    assert (lat.value, lat.source) == (321.0, "measured")
    lat2 = _build(make_snapshot())["lat"]
    assert lat2.source in ("tier_default", "disabled")


def test_quality_overrides_loaded():
    doc = make_snapshot(quality={"customer": [], "calibrated": [
        {"model": "local-mid", "workload": None, "score": 0.91, "set_id": "set-1"}]})
    q = _build(doc)["q"]
    assert (q.score, q.source) == (0.91, "calibrated")


def test_app_policy_compiled():
    doc = make_snapshot(apps=[app_entry("app-a", residency="onprem_only")])
    assert _build(doc)["eff_residency"] == "onprem_only"


@pytest.mark.parametrize("mutate,where", [
    (lambda d: d.update(schema="routeforge.config-snapshot/v2"), "schema"),
    (lambda d: d.pop("apps"), "apps"),
    (lambda d: d["settings"]["providers"].update(nosuch={"enabled": True, "region": None,
        "prompt_retention": None, "accepts_raw_sensitive": None}), "settings.providers.nosuch"),
    (lambda d: d["apps"][0]["policy"].update(bogus=1), "apps.app-a.policy.bogus"),
])
def test_load_errors_name_the_location(mutate, where):
    doc = make_snapshot()
    mutate(doc)
    with pytest.raises(SnapshotError) as exc:
        validate_snapshot(doc)
        asyncio.run(build_scenario(doc, base=BASE_SETTINGS))
    assert str(exc.value).startswith(where)


def test_load_snapshot_hashes_file(tmp_path):
    p = tmp_path / "s.json"
    p.write_text(json.dumps(make_snapshot()))
    doc, sha = load_snapshot(str(p))
    assert doc["schema"] == "routeforge.config-snapshot/v1" and len(sha) == 64


def test_round_trip_matches_live_feasible_set():
    """Spec §9.10: a scenario built from a LIVE snapshot reproduces the live
    app's policy-side feasible set."""
    from fastapi.testclient import TestClient
    from app.main import create_app
    from tests.test_admin_api import _login
    with TestClient(create_app()) as c:
        _login(c)
        doc = c.get("/admin/config-snapshot").json()
        st = c.app.state
        async def live():
            from app.apps.policy import load_policy_view
            async with st.db.session() as s:
                view = await load_policy_view(s, "demo")
            return sorted(x.name for x in feasible_set(
                st.settings, st.registry, view, tenant_id="demo").eligible)
        live_names = c.portal.call(live)

    async def sim():
        scn = await build_scenario(doc, base=BASE_SETTINGS)
        try:
            return sorted(x.name for x in feasible_set(
                scn.settings, scn.registry, scn.apps["demo"].view,
                tenant_id="demo").eligible)
        finally:
            await close_scenario(scn)
    assert asyncio.run(sim()) == live_names
```

In `test_load_errors_name_the_location`, the `validate_snapshot` call raises for the first two
cases, and `build_scenario` raises for the last two. Write the body exactly as shown: the
`with` block stops at the first raise.

- [ ] **Step 3: Run the tests to verify they fail.** Expected: `ModuleNotFoundError: app.eval.scenario`.

- [ ] **Step 4: Implement `app/eval/scenario.py`** following the rules above. Module docstring:
  "offline, operator-run; builds ONE scenario (settings + scratch registry + quality + frozen
  signals + compiled apps) from a config snapshot; never touches a deployment database".
  Import:
  - `Database` from where `app/eval/endpoints.py` imports it;
  - `upsert_onboarded` from `app.apps.store`;
  - `effective_rules`, `AppPrivacyProfile` and `SensitivityRuleView` from `app.apps.privacy`;
  - `compile_policy` from `app.policy.compile`;
  - `lint_settings` from `app.config.lint`;
  - `RoutingCfg`, `PricingCfg`, `PolicyCfg` and `SignalsCfg` from `app.config.schema`;
  - `SignalValue` from `app.routing.signals`;
  - `QualityScore` and `QualityResolver` from `app.routing.quality`;
  - `Router` from `app.routing.router`.

- [ ] **Step 5: Run the tests to verify they pass.** Then run `tests/eval/ -q`, expecting all
  PASS; this includes `test_isolation.py`.

- [ ] **Step 6: Commit**

```bash
git add app/eval/scenario.py tests/eval/sim_fixtures.py tests/eval/test_scenario.py
git commit -m "feat(eval): build a routing scenario from a config snapshot (P3a)"
```

---

### Task 3: Proposal loading and application (`app/eval/proposal.py`)

**Files:**
- Create: `app/eval/proposal.py`, `tests/eval/test_proposal.py`

**Interfaces:**
- Consumes: the snapshot document (Task 1/2 shape).
- Produces:

```python
class ProposalError(ValueError):
    def __init__(self, where: str, message: str): ...   # str(e) == f"{where}: {message}"

SCHEMA = "routeforge.policy-proposal/v1"
def load_proposal(path: str) -> tuple[dict, str]       # (doc, sha256 of file bytes); validates schema + keys
def apply_proposal(snapshot: dict, proposal: dict) -> dict
    # returns a NEW snapshot doc (deep copy); never mutates either input
def normalized_edits(proposal: dict) -> dict            # proposal minus "schema"/"title", keys sorted; echoed in simulation.json
```

**Allowed keys (anything else raises `ProposalError("<dotted path>", "unknown key")`):**
- top level: `schema`, `title`, `apps`, `providers`, `endpoints`, `prices`;
- `apps.<id>`: `residency`, `allowed_regions`, `require_no_retention`, `allowed_tiers`,
  `allowed_providers`, `allowed_models`, `max_cost_usd_per_request`,
  `escalate_residency_on_detection`, `sensitivity_rules`;
- `providers.<name>`: `enabled`;
- `endpoints`: `add`, `remove`, `disable`, `enable`, `update`;
- `endpoints.add[i]`: `slug`, `provider`, `tier`, `price`, `region`, `prompt_retention`,
  `accepts_raw_sensitive`, `context_window`, `quality`, `allowed_app_ids`;
- `endpoints.add[i].price`: `input`, `output`, `cached_input`;
- `endpoints.update.<slug>`: `tier`, `region`, `prompt_retention`, `accepts_raw_sensitive`,
  `context_window`, `allowed_app_ids`;
- `prices.<slug>`: `input`, `output`, `cached_input`;
- `sensitivity_rules[i]`: `entities`, `sensitivity`, `risk_class`, `residency`,
  `require_verification`.

**Validation (all before any change; every error names its location):**
- **Schema:** a string with the prefix `routeforge.policy-proposal/` and major `v1`, else
  `ProposalError("schema", ...)`.
- **Apps:** an app id not in the snapshot gives
  `ProposalError("apps.<id>", "unknown application")`.
  - `residency` goes through `app.policy.enums` / `resolve_residency`; an unparseable value
    gives `ProposalError("apps.<id>.residency", ...)`.
  - `allowed_tiers` values must be in the snapshot's `settings.routing.tiers`.
  - Set-valued fields must be a list or null.
  - `max_cost_usd_per_request` must be a finite number > 0, or null.
  - `require_no_retention` and `escalate_residency_on_detection` must be bool or null.
  - After the edit, `compile_policy(AppPolicyView(**...), settings)` must succeed. Build the
    view with the same frozenset conversion as Task 2, and use a Settings rebuilt as in Task 2,
    or call `app.eval.scenario`'s internal settings builder if Task 2 exposed one; exposing
    `build_settings(doc, base)` from Task 2 is preferred. A `PolicyViolation` or `ValueError`
    gives `ProposalError("apps.<id>", str(exc))`.
  - `sensitivity_rules[i]`:
    - `entities` is a list of strings;
    - `sensitivity` is in the snapshot's policy sensitivity enum;
    - `residency` is parseable;
    - `risk_class` is a key of `settings.routing.risk_floors`;
    - `require_verification` is a bool.
- **`providers.<name>`:** the name must be in `snapshot.settings.providers`, else
  `ProposalError("providers.<name>", "provider not declared in the snapshot")`. `enabled` must
  be a bool.
- **Endpoints:**
  - A slug may appear in at most one of `add` / `remove` / `disable` / `enable` / `update`,
    else `ProposalError("endpoints.<slug>", "appears in more than one endpoint operation")`.
  - `add`:
    - the slug must not already be onboarded, and must not be in `pricing.models`;
    - the provider must be declared;
    - the tier must be canonical;
    - `price.input` / `price.output` must be finite ≥ 0, and `cached_input` must be null or
      finite ≥ 0;
    - `prompt_retention` must be in `PROMPT_RETENTION_VALUES` or null;
    - `accepts_raw_sensitive` must be a bool or null;
    - `context_window` must be an int > 0 or null;
    - `quality` must be null or a finite value in [0, 1];
    - `allowed_app_ids` must be null or a list of snapshot app ids.
  - `remove` / `disable` / `enable` / `update`: the slug must be an onboarded row, else
    `ProposalError("endpoints.<op>.<slug>", "not an onboarded endpoint")`. Update values follow
    the same field rules as `add`.
- **`prices.<slug>`:** the slug must be onboarded (and not removed by this proposal), or a key
  of `settings.pricing.models`. The same price rules apply.

**Application (on a deep copy, in this order):**
1. **App fields:** replace the named `policy` fields, with lists written as sorted lists. A
   `sensitivity_rules` edit replaces the app's `sensitivity_rules` list.
2. **Provider `enabled`.**
3. **Endpoints:**
   - `add` appends a Task-1-shaped row: `display_name=slug`, `enabled=true`, and
     `price_input`/`price_output`/`price_cached_input` from `price`.
   - `remove` deletes the row, and also drops every `quality` entry for that slug.
   - `disable` / `enable` set `enabled`.
   - `update` replaces the fields.
4. **Prices:**
   - An onboarded slug gets `price_input` / `price_output` / `price_cached_input` set.
   - A `pricing.models` slug gets its `input` / `output` / `cached_input` set in
     `settings.pricing.models[slug]`.
5. Keep the list ordering invariants of Task 1: `onboarded` sorted by `slug`, `apps` by
   `application_id`.

- [ ] **Step 1: Write the failing tests** in `tests/eval/test_proposal.py`:

```python
"""P3a Task 3: proposal validation and application (spec §5)."""
from __future__ import annotations

import copy
import json

import pytest

from app.eval.proposal import ProposalError, apply_proposal, load_proposal
from tests.eval.sim_fixtures import app_entry, make_snapshot

P = "routeforge.policy-proposal/v1"


def test_empty_proposal_is_identity():
    snap = make_snapshot()
    assert apply_proposal(snap, {"schema": P}) == snap


def test_inputs_never_mutated():
    snap, prop = make_snapshot(), {"schema": P, "apps": {"app-a": {"residency": "onprem_only"}}}
    s0, p0 = copy.deepcopy(snap), copy.deepcopy(prop)
    apply_proposal(snap, prop)
    assert (snap, prop) == (s0, p0)


def test_app_edit_replaces_only_named_fields():
    snap = make_snapshot(apps=[app_entry("app-a", allowed_tiers=["cheap"])])
    out = apply_proposal(snap, {"schema": P, "apps": {"app-a": {
        "residency": "onprem_only", "allowed_regions": ["eu", "onprem"]}}})
    pol = out["apps"][0]["policy"]
    assert pol["residency"] == "onprem_only"
    assert pol["allowed_regions"] == ["eu", "onprem"]
    assert pol["allowed_tiers"] == ["cheap"]


def test_endpoint_ops_and_prices():
    out = apply_proposal(make_snapshot(), {"schema": P,
        "endpoints": {"add": [{"slug": "new-mid", "provider": "onprem", "tier": "mid",
                               "price": {"input": 0.3, "output": 0.6, "cached_input": None}}],
                      "remove": ["local-big"], "disable": ["local-cheap"]},
        "prices": {"local-mid": {"input": 9.0, "output": 9.0, "cached_input": None}}})
    by = {o["slug"]: o for o in out["onboarded"]}
    assert "local-big" not in by and by["new-mid"]["enabled"] is True
    assert by["local-cheap"]["enabled"] is False
    assert (by["local-mid"]["price_input"], by["local-mid"]["price_output"]) == (9.0, 9.0)
    assert [o["slug"] for o in out["onboarded"]] == sorted(by)


def test_remove_drops_quality_entries():
    snap = make_snapshot(quality={"customer": [], "calibrated": [
        {"model": "local-big", "workload": None, "score": 0.9, "set_id": "s"}]})
    out = apply_proposal(snap, {"schema": P, "endpoints": {"remove": ["local-big"]}})
    assert out["quality"]["calibrated"] == []


@pytest.mark.parametrize("prop,where", [
    ({"schema": "routeforge.policy-proposal/v2"}, "schema"),
    ({"schema": P, "bogus": 1}, "bogus"),
    ({"schema": P, "apps": {"nope": {"residency": "any"}}}, "apps.nope"),
    ({"schema": P, "apps": {"app-a": {"residencyy": "any"}}}, "apps.app-a.residencyy"),
    ({"schema": P, "apps": {"app-a": {"residency": "moon"}}}, "apps.app-a.residency"),
    ({"schema": P, "apps": {"app-a": {"allowed_tiers": ["strong"]}}}, "apps.app-a.allowed_tiers"),
    ({"schema": P, "providers": {"nosuch": {"enabled": True}}}, "providers.nosuch"),
    ({"schema": P, "endpoints": {"remove": ["ghost"]}}, "endpoints.remove.ghost"),
    ({"schema": P, "endpoints": {"remove": ["local-mid"], "disable": ["local-mid"]}},
     "endpoints.local-mid"),
    ({"schema": P, "endpoints": {"add": [{"slug": "local-mid", "provider": "onprem",
      "tier": "mid", "price": {"input": 1, "output": 1, "cached_input": None}}]}},
     "endpoints.add.local-mid"),
    ({"schema": P, "prices": {"local-mid": {"input": -1, "output": 1, "cached_input": None}}},
     "prices.local-mid"),
])
def test_errors_name_the_location(prop, where):
    with pytest.raises(ProposalError) as exc:
        apply_proposal(make_snapshot(), prop)
    assert str(exc.value).startswith(where)


def test_load_proposal_rejects_unknown_key(tmp_path):
    p = tmp_path / "p.json"
    p.write_text(json.dumps({"schema": P, "apss": {}}))
    with pytest.raises(ProposalError) as exc:
        load_proposal(str(p))
    assert str(exc.value).startswith("apss")
```

- [ ] **Step 2: Run to verify failure.** Expected: `ModuleNotFoundError`.
- [ ] **Step 3: Implement `app/eval/proposal.py`** per the rules above. If Task 2 did not
  expose `build_settings(doc, base)`, add it to `app/eval/scenario.py` now: it is the
  settings-building part of `build_scenario`, refactored out so both callers share one
  implementation. `apply_proposal` takes an optional `base: Settings | None = None`; when it is
  `None` it uses `load_settings("config")`, loaded once at module level lazily.
- [ ] **Step 4: Run** `tests/eval/test_proposal.py tests/eval/test_scenario.py -q`. Expected: PASS.
- [ ] **Step 5: Commit**

```bash
git add app/eval/proposal.py app/eval/scenario.py tests/eval/test_proposal.py
git commit -m "feat(eval): policy proposal validation and application (P3a)"
```

---

### Task 4: Replay engine (`app/eval/simulate.py`, core)

**Files:**
- Create: `app/eval/simulate.py`, `tests/eval/test_simulate.py`

**Interfaces:**
- Consumes: `Scenario`, `build_scenario` and `close_scenario` (Task 2); `apply_proposal`
  (Task 3); outcome records as returned by `app/eval/outcomes.py::load_export`.
- Produces:

```python
SIMULATED_STATUSES = frozenset({"ok", "backend_failed", "verify_failed",
                                "no_eligible", "context_overflow"})

@dataclass(frozen=True)
class ArmResult:
    outcome: str                  # "routed" | "rejected"
    model: str | None
    provider: str | None
    tier: str | None
    eligible: tuple[str, ...]     # policy-eligible names (feasible_set), sorted
    reject_codes: tuple[str, ...] # sorted, empty when routed
    cost_usd: float | None
    latency_ms: float | None
    latency_source: str | None    # "observed" | "signal" | "tier_default"
    quality: float | None
    quality_source: str | None

@dataclass(frozen=True)
class RowResult:
    row: dict
    current: ArmResult
    proposed: ArmResult

def select_rows(records: list[dict], app_ids: set[str]) -> tuple[list[dict], dict[str, int]]
def observed_latencies(records: list[dict]) -> dict[str, float]
def simulate_row(scn: Scenario, row: dict, observed: dict[str, float]) -> ArmResult
def run_rows(records: list[dict], current: Scenario, proposed: Scenario) -> tuple[list[RowResult], dict[str, int]]
async def simulate(records: list[dict], snapshot: dict, proposal: dict, *, base: Settings) -> tuple[list[RowResult], dict[str, int]]
    # builds both scenarios (current = snapshot, proposed = apply_proposal(snapshot, proposal)), runs, closes both
```

**Rules:**
- **`select_rows`** returns the rows sorted by `(ts or "", request_id)`, plus the excluded
  counts. Each row is checked in this order:
  1. `cache_hit` is true: `cache_hit`;
  2. the status is not in `SIMULATED_STATUSES`: `unknown_status`;
  3. `application_id` is not in `app_ids`: `unknown_app`;
  4. `complexity` or `prompt_tokens` is None: `missing_features`.

  All four keys always appear in the returned dict, even when their count is 0.
- **`observed_latencies`:** for rows with `status == "ok"`, `attempts` of length exactly 1, and
  `chosen == attempts[0]` with a numeric `latency_ms`, it returns the median of `latency_ms` per
  model. For an even count, the median is the mean of the two middle values.
- **`simulate_row`:**
  1. `app = scn.apps[row["application_id"]]`.
  2. `entities = [Entity(type=t, start=0, end=0, value="") for t in row["detected_entity_types"] or []]`.
  3. `ctx = build_context(scn.settings, row["request_id"], row["application_id"], entities, None, app_policy=app.view, app_rules=app.rules, registry=scn.registry, escalate_residency=app.eff.escalate_residency_on_detection, content_integrity=app.eff.protection.content_integrity, model_input=app.eff.protection.model_input, workload=row.get("workload"))`.
  4. `eligible = tuple(sorted(c.name for c in feasible_set(scn.settings, scn.registry, ctx, tenant_id=row["application_id"]).eligible))`.
  5. `decision = scn.router.select(ctx, tokens_after=int(row["prompt_tokens"]), complexity=float(row["complexity"]))`.
  6. On `NoEligibleCandidate` as `exc`, the result is `ArmResult("rejected", None, None, None, eligible, codes, None, None, None, None, None)`:
     - `codes` is `sorted({code or ("over_budget" if "budget filter" in str(exc) else "unspecified") for _name, code in exc.rejected})`;
     - it is `("empty_pool",)` when `exc.rejected` is empty.
  7. On `ContextWindowExceeded`: `rejected`, with codes `("context_window",)`.
  8. Otherwise the pick is `decision.chosen`, and:
     - `cost = price_call(scn.registry.price_for(pick.name), prompt_tokens=int(row["prompt_tokens"]), completion_tokens=int(row.get("completion_tokens") or 0))`;
     - `latency`: `observed[pick.name]` (`"observed"`), else `scn.signals.snapshot_latency(pick.name)` (`"signal"`), else `scn.signals.latency(pick.name, pick.tier).value` (`"tier_default"`);
     - `q = scn.quality.resolve(pick.name, row.get("workload"))`, giving `quality=q.score` and `quality_source=q.source`.
  9. Do not catch any other exception: a bug must surface, never be recorded as a rejection.

  Check the `price_call` argument names in `app/routing/pricing.py:13`, and use its extra
  keyword (for cached tokens) with 0 if it has one.
- **`run_rows`:**
  - `observed` is computed ONCE from all `records`, not just the selected ones, and shared by
    both arms.
  - `app_ids` is the intersection of both scenarios' app keys.

- [ ] **Step 1: Write the failing tests** in `tests/eval/test_simulate.py`:

```python
"""P3a Task 4: replay invariants (spec §9.1-§9.6)."""
from __future__ import annotations

import asyncio

from hypothesis import HealthCheck, given, settings as hsettings, strategies as st

from app.eval.simulate import observed_latencies, select_rows, simulate
from tests.eval.sim_fixtures import (BASE_SETTINGS, app_entry, make_row, make_snapshot,
                                     onboarded_row)

P = "routeforge.policy-proposal/v1"


def _run(records, snap, prop):
    return asyncio.run(simulate(records, snap, prop, base=BASE_SETTINGS))


def _rows(n=6, app="app-a", **over):
    return [make_row(f"r{i}", app, complexity=[0.05, 0.6, 1.5][i % 3],
                     ts=f"2026-09-20T12:00:{i:02d}+00:00", **over) for i in range(n)]


def test_empty_proposal_zero_change():                                    # §9.1
    results, _ = _run(_rows(), make_snapshot(), {"schema": P})
    assert results
    for r in results:
        assert r.current == r.proposed


def test_onprem_only_never_cloud_and_rejects_without_onprem():            # §9.3
    snap = make_snapshot(providers={"azure": {"enabled": True}})
    results, _ = _run(_rows(), snap, {"schema": P,
        "apps": {"app-a": {"residency": "onprem_only"}}})
    for r in results:
        assert r.proposed.provider in (None, "onprem")
    snap2 = make_snapshot(onboarded=[], providers={"azure": {"enabled": True}})
    results2, _ = _run(_rows(), snap2, {"schema": P,
        "apps": {"app-a": {"residency": "onprem_only"}}})
    assert all(r.proposed.outcome == "rejected" for r in results2)


def test_sole_model_removal_rejects_exactly_those_rows():                  # §9.4
    snap = make_snapshot(apps=[app_entry("app-a", allowed_models=["local-mid"]),
                               app_entry("app-b")])
    rows = _rows(3, "app-a") + [make_row(f"b{i}", "app-b", ts=f"2026-09-20T13:00:0{i}+00:00")
                                for i in range(3)]
    results, _ = _run(rows, snap, {"schema": P, "endpoints": {"remove": ["local-mid"]}})
    for r in results:
        if r.row["application_id"] == "app-a":
            assert (r.current.outcome, r.proposed.outcome) == ("routed", "rejected")
        else:
            assert r.proposed.outcome == "routed"


def test_price_change_moves_cost_exactly():                                # §9.5
    rows = _rows()
    results, _ = _run(rows, make_snapshot(), {"schema": P,
        "prices": {"local-cheap": {"input": 1.0, "output": 2.0, "cached_input": None}}})
    for r in results:
        assert r.current.model == r.proposed.model
        if r.current.model == "local-cheap":
            p, c = r.row["prompt_tokens"], r.row["completion_tokens"]
            expected = (p * 1.0 + c * 2.0) / 1_000_000
            assert abs(r.proposed.cost_usd - expected) < 1e-12
        else:
            assert r.proposed.cost_usd == r.current.cost_usd


def test_sensitivity_rule_replays_recorded_entity_types():                 # §9.6
    snap = make_snapshot(providers={"azure": {"enabled": True}})
    rows = [make_row("s1", "app-a", detected_entity_types=["US_SSN"]),
            make_row("s2", "app-a", detected_entity_types=[])]
    rule = {"entities": ["US_SSN"], "sensitivity": "restricted", "risk_class": "high",
            "residency": "onprem_only", "require_verification": True}
    results, _ = _run(rows, snap, {"schema": P,
        "apps": {"app-a": {"sensitivity_rules": [rule]}}})
    by = {r.row["request_id"]: r for r in results}
    assert by["s1"].proposed.provider in (None, "onprem")
    assert by["s2"].proposed == by["s2"].current


def test_select_rows_exclusions():
    recs = [make_row("a", "app-a"), make_row("b", "app-a", cache_hit=True),
            make_row("c", "app-a", status="weird"), make_row("d", "ghost"),
            make_row("e", "app-a", complexity=None)]
    rows, excluded = select_rows(recs, {"app-a"})
    assert [r["request_id"] for r in rows] == ["a"]
    assert excluded == {"cache_hit": 1, "unknown_status": 1, "unknown_app": 1,
                        "missing_features": 1}


def test_observed_latency_median_single_attempt_ok_only():
    recs = [make_row("1", "app-a", latency_ms=100.0), make_row("2", "app-a", latency_ms=300.0),
            make_row("3", "app-a", latency_ms=9999.0, attempts=["x", "local-cheap"]),
            make_row("4", "app-a", latency_ms=9999.0, status="backend_failed")]
    assert observed_latencies(recs) == {"local-cheap": 200.0}


_TIGHTEN = st.fixed_dictionaries({}, optional={
    "residency": st.just("onprem_only"),
    "allowed_tiers": st.lists(st.sampled_from(["cheap", "mid", "frontier"]),
                              min_size=1, unique=True),
    "allowed_regions": st.just(["onprem"]),
    "require_no_retention": st.just(True),
})


@hsettings(max_examples=30, deadline=None,
           suppress_health_check=[HealthCheck.too_slow])
@given(tighten=_TIGHTEN,
       disable=st.lists(st.sampled_from(["local-cheap", "local-mid", "local-big"]),
                        unique=True, max_size=2),
       etypes=st.lists(st.sampled_from(["US_SSN", "EMAIL_ADDRESS", "PERSON"]),
                       unique=True, max_size=2),
       complexity=st.floats(0.0, 3.0))
def test_stricter_never_widens(tighten, disable, etypes, complexity):      # §9.2
    snap = make_snapshot(providers={"azure": {"enabled": True}})
    prop = {"schema": P, "apps": {"app-a": tighten}}
    if disable:
        prop["endpoints"] = {"disable": disable}
    rows = [make_row("h", "app-a", detected_entity_types=etypes, complexity=complexity)]
    results, _ = _run(rows, snap, prop)
    (r,) = results
    assert set(r.proposed.eligible) <= set(r.current.eligible)
```

Check the tests against the real config before running them:
- **Price arithmetic:** check the actual `price_call` formula in `app/routing/pricing.py`. If
  prices are USD per 1M tokens, the `expected` formula above holds; otherwise adjust
  `expected` to that formula. The invariant is "cost equals `price_call` of the new price".
- **Cloud pool:** if the stock `config/routing.yaml` `model_pool` has no `azure` entries, the
  `providers={"azure": …}` in §9.3 and §9.6 still works; there is simply no cloud candidate.
  Keep the tests; they still assert the invariant.
- **`US_SSN`:** confirm that entity type name matches `config/policy.yaml`'s sensitivity rules
  vocabulary, and use the real name if it differs.

- [ ] **Step 2: Run to verify failure.**
- [ ] **Step 3: Implement** the functions above in `app/eval/simulate.py`, with a module
  docstring stating:
  - replay of the FIRST routing decision only;
  - both arms simulated;
  - no network, no deployment DB;
  - the deliberate non-goals (spec §2).
- [ ] **Step 4: Run** `tests/eval/ -q`. Expected: all PASS.
- [ ] **Step 5: Commit**

```bash
git add app/eval/simulate.py tests/eval/test_simulate.py
git commit -m "feat(eval): replay recorded traffic under current and proposed scenarios (P3a)"
```

---

### Task 5: Aggregation, report, CLI

**Files:**
- Modify: `app/eval/simulate.py` (add `summarize`, `render_report`, `REPORT_CAVEAT`)
- Create: `scripts/policy_simulate.py`, `tests/eval/test_policy_simulate_cli.py`
- Test: extend `tests/eval/test_simulate.py`

**Interfaces:**
- Consumes: `RowResult`, `ArmResult` and `simulate` (Task 4); `load_export`
  (`app/eval/outcomes.py`); `load_snapshot` (Task 2); `load_proposal` and `normalized_edits`
  (Task 3).
- Produces:

```python
REPORT_CAVEAT: str   # the exact blockquote text from spec §8 (without the leading "> ")
def summarize(results: list[RowResult], excluded: dict[str, int], *, inputs: dict,
              edits: dict) -> dict
def render_report(sim: dict) -> str
```

**`summarize` output (exact keys, spec §8):**

```text
schema: "routeforge.simulation/v1"
simulation_id: sha256(json.dumps(inputs, sort_keys=True)).hexdigest()[:16]
inputs: {export_sha256, snapshot_sha256, proposal_sha256, proposal_title,
         routing_policy_version, registry_version, snapshot_generated_at,
         window: {start, end}}         # min/max row ts over SIMULATED rows, or null
edits: normalized_edits(proposal)
rows: {simulated: int, excluded: {cache_hit, unknown_status, unknown_app, missing_features}}
baseline: {rows_compared, agreement, warning: bool, by_app: {app: agreement},
           top_disagreements: [{recorded, simulated, count}]  # top 10}
totals / by_app.<app> / by_workload.<wl or "(untagged)">:
  {requests, route_changed, newly_rejected, newly_served,
   cost_usd: {current, proposed, delta},
   latency_ms: {p50: {current, proposed, delta}, p95: {current, proposed, delta}},
   quality_mean: {current, proposed, delta},
   onprem_share: {current, proposed}}
moves: [{from, to, count}]      # top 20; "(rejected)" stands in for a None model
new_rejections: {by_app: {app: n}, by_code: {code: n}}
affected_apps: [sorted app ids]
per_day: {cost_usd: {current, proposed, delta}} | null   # null when window < 1 day
```

**Definitions:**
- `route_changed`: both arms `routed` and the models differ.
- `newly_rejected`: current `routed`, proposed `rejected`.
- `newly_served`: the reverse.
- `cost_usd`: the sum over `routed` rows of each arm. Rejected rows count 0.
- Latency percentiles use nearest-rank over routed rows (as in `app/eval/outcomes.py::_nearest_rank`);
  a value is `None` when there are no routed rows.
- `quality_mean`: the mean over routed rows, or `None`.
- `onprem_share`: routed rows with provider `onprem` divided by routed rows, or `None`.
- A `delta` is `None` if either side is `None`.
- **Baseline:** compared rows are simulated rows with `status == "ok"` and a non-empty
  `attempts`.
  - A row agrees when `current.model == attempts[0]`.
  - `agreement` is None when no rows were compared.
  - `warning = agreement is not None and agreement < 0.90`.
- `moves`: `(current.model or "(rejected)", proposed.model or "(rejected)")` for rows where
  those two differ, sorted by `(-count, from, to)`.
- Every float is rounded to 6 places, recursively, as in `app/eval/outcomes.py::_round6`.
- `per_day`: the window in days is `(end - start).total_seconds() / 86400`. `per_day` is null
  when that is under 1.

**`render_report(sim)`**, in this order, as Markdown:
1. `> **Simulation only.** …`: the caveat blockquote;
2. the baseline warning line when `baseline.warning` is set;
3. `## Headline` (the totals table: current / proposed / delta);
4. `## New rejections` (or "None.");
5. `## Moves`;
6. `## By app` and `## By workload`;
7. `## Latency and quality sources` (counts of `latency_source` / `quality_source` per arm, so
   `summarize` must also emit `sources: {current: {latency: {...}, quality: {...}}, proposed: {...}}`);
8. `## Excluded rows`.

**CLI `scripts/policy_simulate.py`**, in the same style as `scripts/quality_recommend.py` (read
it first):

```text
usage: policy_simulate.py --export FILE --snapshot FILE --proposal FILE --out DIR
                          [--config-dir config] [--fail-on-new-rejections]
```

- It loads the export (`load_export`), and prints each warning to stderr. It loads the snapshot
  and proposal.
  - `SnapshotError`, `ProposalError`, `ValueError` from `load_export`, `OSError` and
    `json.JSONDecodeError` each print `error: <message>` to stderr and exit 2.
  - `apply_proposal` errors are raised inside `simulate`; catch `ProposalError` around it too,
    also exiting 2.
- It calls `asyncio.run(simulate(...))`, then `summarize` with
  `inputs = {export_sha256: sha256 of the export file bytes, snapshot_sha256, proposal_sha256, proposal_title: proposal.get("title"), routing_policy_version: snap["routing_policy_version"], registry_version: snap["registry_version"], snapshot_generated_at: snap["generated_at"], window: …}`.
  The window is computed inside `summarize` from the results, so `inputs` passed in has no
  window; `summarize` adds it before hashing.
- It writes `simulation.json` with `json.dumps(sim, sort_keys=True, indent=2) + "\n"` and
  `report.md`, creating `--out` if needed, and prints the two paths.
- Exit 3 when `--fail-on-new-rejections` is set and `totals.newly_rejected > 0`; else exit 0.
- `sys.path` bootstrapping follows `scripts/quality_recommend.py`.

- [ ] **Step 1: Write the failing tests.** Append to `tests/eval/test_simulate.py`:

```python
from app.eval.simulate import REPORT_CAVEAT, render_report, summarize

_INPUTS = {"export_sha256": "e", "snapshot_sha256": "s", "proposal_sha256": "p",
           "proposal_title": "t", "routing_policy_version": "v",
           "registry_version": "r", "snapshot_generated_at": "2026-09-24T00:00:00Z"}


def test_summarize_empty_proposal_zero_deltas():
    results, excluded = _run(_rows(), make_snapshot(), {"schema": P})
    sim = summarize(results, excluded, inputs=_INPUTS, edits={})
    t = sim["totals"]
    assert t["route_changed"] == t["newly_rejected"] == t["newly_served"] == 0
    assert t["cost_usd"]["delta"] == 0
    assert sim["moves"] == [] and sim["affected_apps"] == []
    assert sim["schema"] == "routeforge.simulation/v1"


def test_summarize_counts_new_rejections_and_moves():
    snap = make_snapshot(apps=[app_entry("app-a", allowed_models=["local-mid"])])
    results, excluded = _run(_rows(3), snap, {"schema": P,
                                              "endpoints": {"remove": ["local-mid"]}})
    sim = summarize(results, excluded, inputs=_INPUTS, edits={})
    assert sim["totals"]["newly_rejected"] == 3
    assert sim["moves"] == [{"from": "local-mid", "to": "(rejected)", "count": 3}]
    assert sim["affected_apps"] == ["app-a"]
    assert sim["new_rejections"]["by_app"] == {"app-a": 3}


def test_baseline_agreement_and_warning():
    rows = _rows(4)                              # all recorded attempts[0] == local-cheap
    results, excluded = _run(rows, make_snapshot(), {"schema": P})
    sim = summarize(results, excluded, inputs=_INPUTS, edits={})
    assert sim["baseline"]["rows_compared"] == 4
    assert 0.0 <= sim["baseline"]["agreement"] <= 1.0
    assert sim["baseline"]["warning"] == (sim["baseline"]["agreement"] < 0.90)


def test_report_opens_with_caveat():
    results, excluded = _run(_rows(), make_snapshot(), {"schema": P})
    md = render_report(summarize(results, excluded, inputs=_INPUTS, edits={}))
    assert md.startswith("> " + REPORT_CAVEAT.split("\n")[0])
    assert "## New rejections" in md and "## Excluded rows" in md


def test_summarize_deterministic():                                        # §9.8
    a = summarize(*_run(_rows(), make_snapshot(), {"schema": P}), inputs=_INPUTS, edits={})
    b = summarize(*_run(_rows(), make_snapshot(), {"schema": P}), inputs=_INPUTS, edits={})
    import json
    assert json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)
```

Create `tests/eval/test_policy_simulate_cli.py`:

```python
"""P3a Task 5: the CLI end to end, including the live-app baseline (§9.7)."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from fastapi.testclient import TestClient

from app.main import create_app
from tests.eval.sim_fixtures import make_row, make_snapshot
from tests.test_admin_api import _login

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "policy_simulate.py"
P = "routeforge.policy-proposal/v1"


def _run_cli(tmp_path, export_lines, snapshot, proposal, *extra):
    exp, snap, prop = tmp_path / "e.ndjson", tmp_path / "s.json", tmp_path / "p.json"
    exp.write_text("".join(json.dumps(line) + "\n" for line in export_lines))
    snap.write_text(json.dumps(snapshot))
    prop.write_text(json.dumps(proposal))
    out = tmp_path / "out"
    proc = subprocess.run([sys.executable, str(SCRIPT), "--export", str(exp),
                           "--snapshot", str(snap), "--proposal", str(prop),
                           "--out", str(out), *extra],
                          capture_output=True, text=True, cwd=ROOT,
                          env={**__import__("os").environ,
                               "LITELLM_LOCAL_MODEL_COST_MAP": "True"})
    return proc, out


def _line(row):
    feedback = row.pop("feedback", [])
    return {"schema": "routeforge.outcome/v1", "outcome": row, "feedback": feedback}


def test_cli_writes_deterministic_outputs(tmp_path):
    lines = [_line(make_row(f"r{i}", "app-a")) for i in range(3)]
    proc, out = _run_cli(tmp_path, lines, make_snapshot(), {"schema": P})
    assert proc.returncode == 0, proc.stderr
    first = (out / "simulation.json").read_text()
    assert (out / "report.md").read_text().startswith("> **Simulation only.**")
    proc2, _ = _run_cli(tmp_path, lines, make_snapshot(), {"schema": P})
    assert proc2.returncode == 0 and (out / "simulation.json").read_text() == first


def test_cli_exit_2_on_bad_proposal(tmp_path):
    proc, _ = _run_cli(tmp_path, [], make_snapshot(), {"schema": P, "bogus": 1})
    assert proc.returncode == 2 and "bogus" in proc.stderr


def test_cli_exit_3_on_new_rejections_when_flagged(tmp_path):
    from tests.eval.sim_fixtures import app_entry
    snap = make_snapshot(apps=[app_entry("app-a", allowed_models=["local-mid"])])
    lines = [_line(make_row("r1", "app-a"))]
    prop = {"schema": P, "endpoints": {"remove": ["local-mid"]}}
    proc, _ = _run_cli(tmp_path, lines, snap, prop, "--fail-on-new-rejections")
    assert proc.returncode == 3
    proc2, _ = _run_cli(tmp_path, lines, snap, prop)
    assert proc2.returncode == 0


def test_live_round_trip_baseline_is_perfect(tmp_path):                   # §9.7
    with TestClient(create_app()) as c:
        for _ in range(4):
            r = c.post("/v1/chat/completions", headers={"Authorization": "Bearer demo-key"},
                       json={"messages": [{"role": "user", "content": "hello"}]})
            assert r.status_code == 200, r.text
        _login(c)
        snapshot = c.get("/admin/config-snapshot").json()
        export = [json.loads(x) for x in
                  c.get("/admin/outcomes/export").text.strip().splitlines()]
    proc, out = _run_cli(tmp_path, export, snapshot, {"schema": P})
    assert proc.returncode == 0, proc.stderr
    sim = json.loads((out / "simulation.json").read_text())
    assert sim["rows"]["simulated"] == 4
    assert sim["baseline"]["agreement"] == 1.0
```

For `test_live_round_trip_baseline_is_perfect`: if the live signals change between requests so
that the first-decision ranking can differ, the snapshot is taken *after* the requests, so the
signals reflect that state. Should the four `hello` rows then disagree with the snapshot's
ranking, **do not weaken the assertion**. Diagnose it: the most likely causes are that the
snapshot is missing a routing input, or that `FrozenSignals` semantics differ. Report it as a
concern (`DONE_WITH_CONCERNS`) with the evidence.

- [ ] **Step 2: Run to verify failure.**
- [ ] **Step 3: Implement** `summarize`, `render_report`, `REPORT_CAVEAT` and the CLI.
- [ ] **Step 4: Run** `tests/eval/ tests/test_config_snapshot.py -q`. Expected: PASS.
- [ ] **Step 5: Commit**

```bash
git add app/eval/simulate.py scripts/policy_simulate.py tests/eval/test_simulate.py tests/eval/test_policy_simulate_cli.py
git commit -m "feat(eval): simulation summary, report and policy_simulate CLI (P3a)"
```

---

### Task 6: Documentation + backlog

**Files:**
- Create: `docs/reference/policy-simulator.md`
- Modify: `docs/research/feasible-route-bench.md` (append §15), `docs/backlog/bugs.md` (add B55),
  `CLAUDE.md` (one sentence in the "Routing observability & signals" paragraph)

**Interfaces:**
- Consumes: the final CLI flags, the snapshot, proposal and simulation schemas, and the
  invariants as built. Read `app/eval/simulate.py`, `app/eval/proposal.py`,
  `app/apps/snapshot.py` and the spec before writing, and document what was built.

- [ ] **Step 1: Write `docs/reference/policy-simulator.md`**, in the same style and heading
  depth as `docs/reference/quality-calibration.md` (read it first). Sections:
  1. What it answers, and the fact that it never applies anything.
  2. The three inputs, and how to get each: the `curl` for `/admin/config-snapshot` and
     `/admin/outcomes/export` with the admin session cookie, as `routing-outcomes.md` shows
     for the export.
  3. The proposal schema: a full example plus a table of every allowed key and its
     validation.
  4. Running the CLI, with the exit codes.
  5. Reading `report.md` and `simulation.json`, with a field table.
  6. Fidelity: the baseline check and what lowers it.
  7. What is not simulated (spec §2), and B55.
  8. Security: the snapshot contents, and why it is secret-free.
- [ ] **Step 2: Append §15 "Policy counterfactual simulator (P3a)"** to
  `docs/research/feasible-route-bench.md`, in the style of §11–§14:
  - the model: a counterfactual over recorded traffic, re-running the real eligibility and
    router code;
  - the two-arm design, and why logged-vs-simulated is avoided;
  - invariants §9.1–§9.8, with the test names;
  - a "Results" subsection reading "Pending the Spark run after deploy — see §15.4" with an
    empty results table (`| proposal | simulated | agreement | route_changed | newly_rejected | Δcost/day |`).
    This is a deliberate, named slot the controller fills after deploy, not a placeholder in
    the code.
- [ ] **Step 3: Add B55** to `docs/backlog/bugs.md`, following the file's existing entry format:
  - **Title:** "Outcome rows don't record the pinned model or `max_tokens`".
  - **Impact:** the simulator replays pinned requests unpinned and budgets with the default
    output estimate, which lowers baseline agreement for pinned or large-`max_tokens` traffic.
  - **Fix:** two nullable `routing_outcomes` columns (`requested_model`, `max_tokens`), which
    need an ALTER on deployed DBs.
  - **Status:** open, deferred.
- [ ] **Step 4: Add one sentence to `CLAUDE.md`**, after the P2b-2 sentence in the "Routing
  observability & signals" paragraph:
  "Since P3a, `GET /admin/config-snapshot` exports a content-free config snapshot that the
  offline `scripts/policy_simulate.py` replays those records against, to predict a policy
  change's effect before anyone applies it — see `docs/reference/policy-simulator.md`."
- [ ] **Step 5: Commit**

```bash
git add docs/reference/policy-simulator.md docs/research/feasible-route-bench.md docs/backlog/bugs.md CLAUDE.md
git commit -m "docs: policy simulator reference, research §15, B55 (P3a)"
```
