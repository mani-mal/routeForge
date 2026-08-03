# Model Registry & Onboarding Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make routeForge a pure router over externally-deployed OpenAI-compatible endpoints: on-prem models are onboarded at runtime (DB-backed, hot-reloaded, per-model endpoint, probe-gated) instead of hand-edited into YAML, and the `inference_backend`/vLLM/Dynamo axis plus the built-in Qwen are removed.

**Architecture:** A new `onboarded_models` DB table is the single source for on-prem models. A `ModelRegistry` (replacing `InfraRoster`) merges the static YAML pool (cloud built-ins only) with enabled onboarded rows into one *effective pool*, which the router, gateway, pricing, and per-app policy all read through. Onboarding goes through a probe-gated admin API. routeForge boots with an empty pool; the operator onboards their own externally-deployed model (no bundled demo model — the user drives real traffic from their own application).

**Tech Stack:** Python 3.12, FastAPI, Pydantic v2, SQLAlchemy async (aiosqlite/postgres), LiteLLM, `uv`; React + Vite + TypeScript UI. Tests: `uv run pytest`.

## Global Constraints

- **Design spec:** `docs/superpowers/specs/2026-08-03-model-registry-onboarding-design.md` — authoritative.
- **routeForge deploys nothing** (CLAUDE.md Rule 4). Onboarding registers + probes an endpoint; it never deploys, scales, or load-balances. No engine/replicas/TP/GPU/Run:ai concepts survive.
- **Fail closed** (CLAUDE.md Rule 5): probe failure ⇒ model saved disabled, never routable. No healthy on-prem model ⇒ `onprem_only` request rejected, never routed to cloud (CLAUDE.md `none`-rule, now derived).
- **Canonical values:** provider `onprem` (one word); tiers exactly `cheap | mid | frontier` (no `strong`); sensitivity enum untouched.
- **One owner per concern:** cost lives in pricing/registry, tier/quality in routing/registry — never restated.
- **No auto-migrations:** `create_all` never `ALTER`s (memory: *No DB migrations*). New table is additive; deployed DBs get a documented `CREATE TABLE IF NOT EXISTS` + orphan-drop.
- **Test command:** `uv run pytest` (or `scripts/test.sh -k <expr>`). Tests set `SP_SKIP_BACKEND_HEALTHCHECK=1`, `SP_PII_DETECTION=regex`, `DATABASE_URL=sqlite+aiosqlite://` in `tests/conftest.py`.
- **v1 scope:** on-prem onboarding only. Cloud models stay YAML built-ins gated by `providers.{azure,bedrock}.enabled`. No cloud onboarding UI.
- **Commit** after each task's tests pass. End commit messages with `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>`.

---

## File map

**Create**
- `app/apps/registry.py` — `ModelRegistry` (replaces `InfraRoster`)
- `tests/test_onboarded_store.py`, `tests/test_model_registry.py`, `tests/test_onboard_api.py`, `tests/test_derived_onprem_reject.py`
- `docs/onboarding-models.md` — operator onboarding guide

**Modify**
- `app/apps/models.py` — add `OnboardedModel`; remove `InfraModel`, `InfraSettings`
- `app/apps/store.py` — onboarded CRUD; remove infra-settings/infra-model fns
- `app/apps/infra.py` — deleted (logic moves to `registry.py`)
- `app/routing/router.py` — read effective pool/pricing/endpoint via registry
- `app/gateway/backends/litellm_backend.py` — per-model endpoint in `_map`; drop vllm/dynamo
- `app/gateway/backends/factory.py` — drop boot health-check-exits; construct with registry
- `app/gateway/gateway.py` — price via `registry.price_for`
- `app/policy/context.py` — onprem availability via registry; app-scope compose
- `app/config/schema.py` — drop `inference_backend`/`vllm`/`dynamo`; drop `VllmCfg`/`DynamoCfg`
- `app/config/loader.py` — stop loading removed fields
- `app/config/lint.py` — relax provider/tier invariants to YAML-subset; drop backend enum
- `app/main.py` — build `ModelRegistry`, inject everywhere; wiring
- `app/api/admin_routes.py` — onboard/edit/delete/list endpoints + probe; remove old infra endpoints
- `config/serving.yaml`, `config/routing.yaml`, `config/pricing.yaml` — purge axis + Qwen
- `ui/src/api.ts`, `ui/src/admin/InfrastructurePanel.tsx` — onboard form + router-only fleet
- `deploy/compose/docker-compose.yml` — drop the dead `SP_INFERENCE_BACKEND` env; drop/comment the `vllm` GPU-profile service (onboarding replaces it)
- `CLAUDE.md`, `.claude/skills/inference-backends/SKILL.md`, `.claude/skills/routing-and-gateway/SKILL.md`
- Existing tests referencing `InfraRoster`/`local-8b`/`inference_backend`: `tests/test_infra_router.py`, `tests/test_infra_store.py`, `tests/test_admin_infra_api.py`, `tests/test_context.py`, `tests/_litellm_fake.py`, `tests/conftest.py`

---

## Milestone 1 — Backend core (onboarding works via API + pytest)

### Task 1: `onboarded_models` table + store CRUD

**Files:**
- Modify: `app/apps/models.py` (add `OnboardedModel`; **keep** `InfraModel`/`InfraSettings` — they still have live importers removed in the Task 3 cutover)
- Modify: `app/apps/store.py` (add onboarded CRUD **alongside** the existing infra fns — those are removed in Task 3)
- Test: `tests/test_onboarded_store.py`

**Sequencing note:** Task 1 is purely additive so the suite stays green. `InfraModel`/`InfraSettings`, the store infra fns (`get/upsert_infra_settings`, `list/get/upsert_infra_model`), `InfraRoster`, and the old `/admin/infra*` endpoints are all removed together in the **Task 3 cutover** (before Task 4's config purge, since `InfraRoster.engine_for` reads `inference_backend`). Do **not** remove them here.

**Interfaces produced:**
- `OnboardedModel` columns: `slug:str PK`, `display_name:str`, `provider:str="onprem"`, `tier:str`, `quality:float|None`, `price_input:float`, `price_output:float`, `endpoint:str`, `api_key_env:str|None`, `context_window:int|None`, `enabled:bool=False`, `allowed_app_ids:list|None` (JSON; None/[] = all apps), `probe_status:str="unknown"`, `probe_error:str|None`, `notes:str|None`, `updated_at`.
- `store.list_onboarded(session) -> list[OnboardedModel]`
- `store.get_onboarded(session, slug) -> OnboardedModel | None`
- `store.upsert_onboarded(session, slug, **fields) -> OnboardedModel`
- `store.delete_onboarded(session, slug) -> bool`

- [ ] **Step 1: Write failing test** `tests/test_onboarded_store.py`

```python
import asyncio
from app.apps.db import Database
from app.apps import store

def _run(coro): return asyncio.get_event_loop().run_until_complete(coro)

async def _mk():
    db = Database(url="sqlite+aiosqlite://")
    await db.ensure_ready()
    return db

def test_upsert_get_list_delete_onboarded():
    async def go():
        db = await _mk()
        async with db.session() as s:
            await store.upsert_onboarded(
                s, "nemotron-mid", display_name="Nemotron", provider="onprem",
                tier="mid", price_input=0.2, price_output=0.2,
                endpoint="http://x/v1", api_key_env="NEM_KEY", enabled=True,
                allowed_app_ids=["acme"])
            await s.commit()
        async with db.session() as s:
            row = await store.get_onboarded(s, "nemotron-mid")
            assert row.tier == "mid" and row.enabled is True
            assert row.allowed_app_ids == ["acme"]
            rows = await store.list_onboarded(s)
            assert [r.slug for r in rows] == ["nemotron-mid"]
        async with db.session() as s:
            assert await store.delete_onboarded(s, "nemotron-mid") is True
            await s.commit()
        async with db.session() as s:
            assert await store.get_onboarded(s, "nemotron-mid") is None
    _run(go())
```

- [ ] **Step 2: Run — expect FAIL** (`AttributeError: module 'app.apps.store' has no attribute 'upsert_onboarded'`)

Run: `uv run pytest tests/test_onboarded_store.py -q`

- [ ] **Step 3: Add the model** in `app/apps/models.py` (ADD only — do **not** delete `InfraModel`/`InfraSettings` yet; live importers remain until Task 3):

```python
class OnboardedModel(Base):
    """A registered on-prem model routeForge routes to but never deploys.
    Self-sufficient: identity + routing + pricing + endpoint. Replaces the
    infra_models/infra_settings tables. NEW table -> create_all, no migration.
    See 2026-08-03-model-registry-onboarding-design.md."""

    __tablename__ = "onboarded_models"

    slug: Mapped[str] = mapped_column(String(100), primary_key=True)
    display_name: Mapped[str] = mapped_column(String(200))
    provider: Mapped[str] = mapped_column(String(32), default="onprem")
    tier: Mapped[str] = mapped_column(String(16))
    quality: Mapped[float | None] = mapped_column(Float, nullable=True)
    price_input: Mapped[float] = mapped_column(Float)
    price_output: Mapped[float] = mapped_column(Float)
    endpoint: Mapped[str] = mapped_column(String(255))
    api_key_env: Mapped[str | None] = mapped_column(String(64), nullable=True)
    context_window: Mapped[int | None] = mapped_column(Integer, nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    allowed_app_ids: Mapped[list | None] = mapped_column(JSON, nullable=True)
    probe_status: Mapped[str] = mapped_column(String(16), default="unknown")
    probe_error: Mapped[str | None] = mapped_column(String(500), nullable=True)
    notes: Mapped[str | None] = mapped_column(String(255), nullable=True)
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow)
```

Ensure `Float`, `JSON`, `Integer`, `Boolean`, `String`, `DateTime` are imported (they already are for the removed models).

- [ ] **Step 4: Add store CRUD** in `app/apps/store.py` (ADD alongside the existing infra fns — do **not** remove them here). Follow the existing whitelist idiom:

```python
_ONBOARDED_FIELDS = {
    "display_name", "provider", "tier", "quality", "price_input",
    "price_output", "endpoint", "api_key_env", "context_window", "enabled",
    "allowed_app_ids", "probe_status", "probe_error", "notes"}


async def list_onboarded(session: AsyncSession) -> list[OnboardedModel]:
    return list((await session.execute(select(OnboardedModel))).scalars().all())


async def get_onboarded(session: AsyncSession, slug: str) -> OnboardedModel | None:
    return await session.get(OnboardedModel, slug)


async def upsert_onboarded(session: AsyncSession, slug: str, **fields) -> OnboardedModel:
    row = await session.get(OnboardedModel, slug)
    clean = {k: v for k, v in fields.items() if k in _ONBOARDED_FIELDS}
    if row is None:
        row = OnboardedModel(slug=slug, **clean)
        session.add(row)
    else:
        for k, v in clean.items():
            setattr(row, k, v)
    return row


async def delete_onboarded(session: AsyncSession, slug: str) -> bool:
    row = await session.get(OnboardedModel, slug)
    if row is None:
        return False
    await session.delete(row)
    return True
```

Add the `OnboardedModel` import at the top of `store.py`; **keep** the `InfraModel`/`InfraSettings` imports (still used by the infra fns you're leaving in place).

- [ ] **Step 5: Run — expect PASS** (`uv run pytest tests/test_onboarded_store.py -q`), then the full suite `uv run pytest -q` to confirm nothing regressed (Task 1 is additive; `tests/test_infra_store.py` still passes and is left untouched until Task 3).

- [ ] **Step 6: Commit** `git add app/apps/models.py app/apps/store.py tests/test_onboarded_store.py && git commit -m "feat(apps): onboarded_models table + store CRUD (additive)"`

---

### Task 2: `ModelRegistry` (merged pool) replacing `InfraRoster`

**Files:**
- Create: `app/apps/registry.py`
- Delete: `app/apps/infra.py`
- Test: `tests/test_model_registry.py`

**Interfaces consumed:** `store.list_onboarded`, `OnboardedModel`, `PoolCandidate`, `PriceCfg`, `Settings`.

**Interfaces produced (`ModelRegistry`):**
- `__init__(self, settings: Settings)`
- `async refresh(self, db) -> None` / `async ensure_loaded(self, db) -> None` (same last-known-good behavior as `InfraRoster.refresh`)
- `effective_pool(self) -> list[PoolCandidate]` — YAML `settings.routing.model_pool` + enabled onboarded rows (as `PoolCandidate(name=slug, provider, tier, display_name, quality)`)
- `price_for(self, slug: str) -> PriceCfg` — YAML `settings.pricing.models` first, else onboarded row → `PriceCfg(input, output, tier, provider)`; raises `KeyError(slug)` if absent (caller catches)
- `endpoint_for(self, slug: str) -> tuple[str, str | None] | None` — `(endpoint, api_key_env)` for an onboarded model; `None` for YAML/cloud built-ins
- `is_enabled(self, slug: str) -> bool` — onboarded row enabled flag; unknown/cold ⇒ True (permissive, matches old `InfraRoster.is_enabled`)
- `is_allowed_for_app(self, slug: str, app_id: str | None) -> bool` — onboarded `allowed_app_ids` None/[] ⇒ all apps; else membership. Non-onboarded (cloud) ⇒ True
- `has_healthy_onprem(self) -> bool` — any enabled onboarded on-prem model exists in the snapshot

- [ ] **Step 1: Write failing test** `tests/test_model_registry.py`

```python
import asyncio
from app.config.loader import load_settings
from app.apps.db import Database
from app.apps import store
from app.apps.registry import ModelRegistry

def _run(c): return asyncio.get_event_loop().run_until_complete(c)
S = load_settings()

async def _reg_with(rows):
    db = Database(url="sqlite+aiosqlite://")
    await db.ensure_ready()
    async with db.session() as s:
        for slug, f in rows.items():
            await store.upsert_onboarded(s, slug, **f)
        await s.commit()
    reg = ModelRegistry(S)
    await reg.refresh(db)
    return reg

def test_effective_pool_merges_enabled_onboarded():
    reg = _run(_reg_with({
        "nemotron-mid": dict(display_name="Nemotron", provider="onprem",
            tier="mid", price_input=0.2, price_output=0.2,
            endpoint="http://nem/v1", api_key_env="NEM", enabled=True),
        "off-model": dict(display_name="Off", provider="onprem", tier="cheap",
            price_input=0.1, price_output=0.1, endpoint="http://o/v1",
            enabled=False)}))
    names = {c.name for c in reg.effective_pool()}
    assert "nemotron-mid" in names          # enabled onboarded merged in
    assert "off-model" not in names         # disabled excluded
    assert reg.price_for("nemotron-mid").input == 0.2
    assert reg.endpoint_for("nemotron-mid") == ("http://nem/v1", "NEM")
    assert reg.has_healthy_onprem() is True

def test_app_scope():
    reg = _run(_reg_with({
        "scoped": dict(display_name="S", provider="onprem", tier="mid",
            price_input=0.2, price_output=0.2, endpoint="http://s/v1",
            enabled=True, allowed_app_ids=["acme"])}))
    assert reg.is_allowed_for_app("scoped", "acme") is True
    assert reg.is_allowed_for_app("scoped", "hospital") is False

def test_empty_onboarded_has_no_healthy_onprem():
    reg = _run(_reg_with({}))
    assert reg.has_healthy_onprem() is False
```

- [ ] **Step 2: Run — expect FAIL** (`ModuleNotFoundError: app.apps.registry`).

- [ ] **Step 3: Implement `app/apps/registry.py`.** Mirror `InfraRoster`'s snapshot/last-known-good pattern (see old `app/apps/infra.py`), but snapshot onboarded rows:

```python
from __future__ import annotations
import logging
from dataclasses import dataclass
from app.config.schema import PoolCandidate, PriceCfg, Settings
from app.apps import store

_log = logging.getLogger("serving.registry")


@dataclass(frozen=True)
class _OnboardedRow:
    slug: str; display_name: str; provider: str; tier: str
    quality: float | None; price_input: float; price_output: float
    endpoint: str; api_key_env: str | None; enabled: bool
    allowed_app_ids: tuple[str, ...] | None


class ModelRegistry:
    """Merged model source: static YAML pool (cloud built-ins) + enabled
    onboarded on-prem rows. Replaces InfraRoster. Sync reads on the request
    path; refreshed on every admin write. Last-known-good on DB error; cold
    cache is empty-onboarded (YAML-only) so a DB blip degrades onboarding
    visibility, never residency."""

    def __init__(self, settings: Settings) -> None:
        self._s = settings
        self._rows: dict[str, _OnboardedRow] | None = None

    async def ensure_loaded(self, db) -> None:
        if self._rows is None:
            await self.refresh(db)

    async def refresh(self, db) -> None:
        try:
            async with db.session() as session:
                rows = await store.list_onboarded(session)
            self._rows = {
                r.slug: _OnboardedRow(
                    slug=r.slug, display_name=r.display_name, provider=r.provider,
                    tier=r.tier, quality=r.quality, price_input=r.price_input,
                    price_output=r.price_output, endpoint=r.endpoint,
                    api_key_env=r.api_key_env, enabled=bool(r.enabled),
                    allowed_app_ids=tuple(r.allowed_app_ids) if r.allowed_app_ids else None)
                for r in rows}
        except Exception:
            _log.exception("registry refresh failed; keeping last-known-good")

    def _onboarded(self) -> dict[str, _OnboardedRow]:
        return self._rows or {}

    def effective_pool(self) -> list[PoolCandidate]:
        pool = list(self._s.routing.model_pool)
        for r in self._onboarded().values():
            if r.enabled:
                pool.append(PoolCandidate(
                    name=r.slug, provider=r.provider, tier=r.tier,
                    display_name=r.display_name, quality=r.quality))
        return pool

    def price_for(self, slug: str) -> PriceCfg:
        p = self._s.pricing.models.get(slug)
        if p is not None:
            return p
        r = self._onboarded().get(slug)
        if r is not None:
            return PriceCfg(input=r.price_input, output=r.price_output,
                            tier=r.tier, provider=r.provider)
        raise KeyError(slug)

    def endpoint_for(self, slug: str) -> tuple[str, str | None] | None:
        r = self._onboarded().get(slug)
        return (r.endpoint, r.api_key_env) if r is not None else None

    def is_enabled(self, slug: str) -> bool:
        r = self._onboarded().get(slug)
        return True if r is None else r.enabled

    def is_allowed_for_app(self, slug: str, app_id: str | None) -> bool:
        r = self._onboarded().get(slug)
        if r is None or not r.allowed_app_ids:
            return True
        return app_id in r.allowed_app_ids

    def has_healthy_onprem(self) -> bool:
        return any(r.enabled and r.provider == "onprem"
                   for r in self._onboarded().values())
```

- [ ] **Step 4: Run — expect PASS** (`uv run pytest tests/test_model_registry.py -q`).

- [ ] **Step 5: Commit** `git add app/apps/registry.py tests/test_model_registry.py && git commit -m "feat(apps): ModelRegistry merged pool replacing InfraRoster"`

*(`app/apps/infra.py` is deleted in Task 3 once no importer remains.)*

---

### Task 3: Wire router, gateway, context, backend to the registry

**Files:**
- Modify: `app/routing/router.py`, `app/gateway/gateway.py`, `app/gateway/backends/litellm_backend.py`, `app/gateway/backends/factory.py`, `app/policy/context.py`, `app/policy/gate.py`, `app/api/routes.py`, `app/main.py`
- **Cutover — remove the old infra layer** (all together, since they are mutually coupled and `InfraRoster.engine_for` reads `inference_backend` which Task 4 deletes):
  - Delete: `app/apps/infra.py` (`InfraRoster`/`InfraSnapshot`/`InfraModelRow`/`InfraClusterRow`)
  - `app/apps/models.py`: remove `InfraModel` (222-244) + `InfraSettings` (205-219)
  - `app/apps/store.py`: remove `get/upsert_infra_settings`, `list/get/upsert_infra_model` + their whitelist consts (343-383)
  - `app/api/admin_routes.py`: remove `GET/PUT /admin/infra` (391-414), `GET /admin/infra/models` (417-424), `PUT /admin/infra/models/{slug}` (427-441), and `InfraSettingsIn`/`InfraModelIn`/`_ENGINES`/`_infra_model_dict` (339-388)
  - `app/main.py`: remove `InfraRoster` import + `app.state.infra_roster`; add `app.state.registry = ModelRegistry(settings)`
- Test: `tests/test_infra_router.py` (rewrite to use `ModelRegistry`), `tests/test_derived_onprem_reject.py` (new). **Delete** `tests/test_infra_store.py` and `tests/test_admin_infra_api.py` (they cover the removed old layer).

**Interfaces consumed:** all `ModelRegistry` methods from Task 2.

**Note:** After this cutover there is no admin endpoint to onboard a model until Task 5 adds the new ones — that is fine (no test depends on onboarding until Task 5). The suite must be green at the end of Task 3 with the old layer fully gone.

Key changes (each is behavior-preserving except the derived reject):

1. **`router.py`** — constructor param `infra_roster` → `registry: ModelRegistry | None`; store as `self._reg`.
   - Line 48: `pool = self._reg.effective_pool() if self._reg else self._s.routing.model_pool`.
   - `_est_cost` (42): `p = self._reg.price_for(cand.name) if self._reg else self._s.pricing.models[cand.name]`.
   - Disable-gate (60): `not self._reg.is_enabled(c.name)` (registry `is_enabled` covers onboarded rows; keep the `c.provider == "onprem"` guard).
   - Remove `serving_engine` entirely from `RoutingDecision` and the return (engine concept purged). Grep for `serving_engine` consumers (gateway record, DecisionRecord) and drop them (see gateway change below and DecisionRecord field removal note).
   - App-scope filter: add, alongside residency, `if self._reg and not self._reg.is_allowed_for_app(c.name, ctx.app_id): reject "not permitted for application"; continue`. (Requires `app_id` on `ExecutionPolicyContext` — it already carries `tenant_id`; use `tenant_id` as the app id since `Application.id` is the app slug and `ctx.tenant_id` is set to it. Confirm in context.py; if a distinct field is needed, add `app_id` to the context in the context.py change below.)

2. **`gateway.py`** — line 38: `price = self._reg.price_for(cand.name)` (inject registry into Gateway, or fetch via router). Simplest: pass `registry` into `Gateway.__init__` and use it; keep `self._s.pricing` only as fallback. Remove `serving_engine=decision.serving_engine` from the `DecisionRecord(...)` at 60-66.

3. **`litellm_backend.py` `_map`** (29-50) — replace the onprem `api_base` block:

```python
        litellm_model = cand.litellm_model or f"{pcfg.litellm_prefix}/{cand.name}"
        kwargs: dict = {}
        if cand.provider == "onprem":
            ep = self._reg.endpoint_for(cand.name) if self._reg else None
            if ep is None:
                raise RuntimeError(
                    f"onprem model {model!r} has no registered endpoint")
            base, key_env = ep
            kwargs["api_base"] = base
            if key_env:
                kwargs["api_key"] = os.environ.get(key_env, "sk-noauth")
        if pcfg.api_base_env:   # cloud, unchanged
            kwargs["api_base"] = os.environ.get(pcfg.api_base_env) or kwargs.get("api_base")
        if pcfg.api_key_env and "api_key" not in kwargs:
            kwargs["api_key"] = os.environ.get(pcfg.api_key_env, "sk-noauth")
        if pcfg.api_version_env and os.environ.get(pcfg.api_version_env):
            kwargs["api_version"] = os.environ.get(pcfg.api_version_env)
        return litellm_model, kwargs
```
   Inject the registry: `LiteLLMBackend.__init__(self, settings, *, registry=None, logprob_temperature=1.0)`; store `self._reg = registry`. `self._pool` must also see onboarded models — change to read `registry.effective_pool()` lazily inside `_map` (`cand = {c.name: c for c in (self._reg.effective_pool() if self._reg else settings.routing.model_pool)}[model]`). Remove the `onprem_disabled = ... inference_backend == "none"` line and the `health()` method's vllm/dynamo branch (replace `health()` body with `return BackendHealth(ok=True, detail="litellm")` — per-model reachability is the onboard probe now).

4. **`factory.py`** — `make_backend(settings, registry)` passes the registry through. `resolve_backend`: **remove the `sys.exit(1)` health-check**; return `make_backend(settings, registry)` directly (keep the `SP_SKIP_BACKEND_HEALTHCHECK` no-op for compatibility or delete it — reachability is per-model now). Update both call sites in `app/main.py`.

5. **`context.py`** — replace the `inference_backend == "none"` onprem-drop (60-61) with a registry check. `build_context` gains a `registry: ModelRegistry | None = None` param:

```python
    enabled = {p for p, c in settings.serving.providers.items() if c.enabled}
    # On-prem availability is derived: no healthy onboarded on-prem model ->
    # drop onprem so an onprem_only request resolves to empty allowed and the
    # router rejects it (fail-closed; replaces inference_backend: none).
    if registry is not None and not registry.has_healthy_onprem():
        enabled = enabled - {"onprem"}
    onprem_providers = {"onprem"}
```
   Thread `registry` from `app/policy/gate.py:34` and set `app_id` if the router needs it (or confirm `ctx.tenant_id` already equals the app slug).

6. **`main.py`** — build `registry = ModelRegistry(settings)`; `app.state.registry = registry`; pass to `Router(settings, registry=registry)`, `make_backend/resolve_backend(settings, registry)`, `Gateway(settings, backend, router, registry)`. Remove `InfraRoster` import/state. Request path: replace `infra_roster.ensure_loaded` (routes.py:100) with `registry.ensure_loaded`.

- [ ] **Step 1: Write failing test** `tests/test_derived_onprem_reject.py` — target the *mechanism* in `build_context` (independent of whether Qwen is still in the YAML pool): with a registry that has no enabled on-prem model, an `onprem_only` request drops `onprem` from `allowed_providers`.

```python
import asyncio
from app.config.loader import load_settings
from app.apps.db import Database
from app.apps.registry import ModelRegistry
from app.policy.context import build_context

def _run(c): return asyncio.get_event_loop().run_until_complete(c)
S = load_settings()

def test_no_healthy_onprem_drops_onprem_for_onprem_only():
    db = Database(url="sqlite+aiosqlite://"); _run(db.ensure_ready())
    reg = ModelRegistry(S); _run(reg.refresh(db))     # zero onboarded -> no healthy onprem
    # Construct an onprem_only request. Use whatever path yields onprem_only
    # under the CURRENT policy.yaml — confirm by reading it. If a tenant with
    # default_residency: onprem_only exists (e.g. a hospital tenant), pass its id
    # with entities=[]; else pass entities that a restricted rule maps to
    # onprem_only. The point of the assertion is the registry-driven drop.
    ctx = build_context(S, request_id="r", tenant_id="<onprem_only tenant>",
                        entities=[], redaction_map_id=None, registry=reg)
    assert ctx.onprem_only is True
    assert "onprem" not in ctx.allowed_providers    # dropped: no healthy onprem
```

- [ ] **Step 2: Run — expect FAIL** (`build_context` has no `registry` param yet). `uv run pytest tests/test_derived_onprem_reject.py -q`.

- [ ] **Step 3: Apply the six wiring changes** (router, gateway, litellm_backend, factory, context, main) from the numbered list above. Thread `registry` from `app/policy/gate.py` into `build_context`, and swap `infra_roster.ensure_loaded` → `registry.ensure_loaded` at `app/api/routes.py:100`.

- [ ] **Step 4: Cutover — remove the old infra layer** (see Files above): delete `app/apps/infra.py`; remove `InfraModel`/`InfraSettings` from `models.py`; remove the store infra fns; remove the old `/admin/infra*` endpoints + their Pydantic/helper symbols from `admin_routes.py`; remove `InfraRoster` wiring from `main.py`. Then `grep -rn "InfraRoster\|InfraModel\|InfraSettings\|infra_roster\|/admin/infra" app` returns nothing (only the new `app.state.registry`).

- [ ] **Step 5: Update tests** — rewrite `tests/test_infra_router.py` to build a `ModelRegistry` over an in-memory DB seeded with onboarded rows (helper like `test_model_registry._reg_with`) and construct `Router(S, registry=reg)`; keep the residency/disable/tier assertions. **Delete** `tests/test_infra_store.py` and `tests/test_admin_infra_api.py` (they cover the removed layer).

- [ ] **Step 6: Run the full suite** `uv run pytest -q`. Remaining failures should only be tests referencing `inference_backend`/`local-8b`/`serving_engine` still present in the YAML config (removed in Task 4) — if a test fails purely because Qwen is still a candidate, mark it `@pytest.mark.xfail(reason="Qwen removed in Task 4")` and note it in the report so Task 4 clears it. All *registry-wiring* tests must pass.

- [ ] **Step 7: Commit** `git add -A && git commit -m "refactor(routing): cutover to ModelRegistry; remove old infra layer (InfraRoster/InfraModel/InfraSettings/admin infra endpoints)"`

---

### Task 4: Config purge + Qwen removal + lint relaxation

**Files:**
- Modify: `app/config/schema.py`, `app/config/loader.py`, `app/config/lint.py`, `config/serving.yaml`, `config/routing.yaml`, `config/pricing.yaml`
- Test: `tests/test_config_lint.py` (existing — adjust), `tests/test_context.py` (adjust)

**Changes:**
1. **`schema.py`** — `ServingCfg`: remove `inference_backend`, `vllm`, `dynamo`. Delete `VllmCfg`/`DynamoCfg` classes. Keep `providers`.
2. **`loader.py`** — remove `SP_INFERENCE_BACKEND` override, `inference_backend=...`, `vllm=VllmCfg(...)`, `dynamo=DynamoCfg(...)`, and the `VllmCfg/DynamoCfg` imports.
3. **`serving.yaml`** — delete the `inference_backend:` line and the `vllm:`/`dynamo:` blocks. Add a comment: `# On-prem models are onboarded at runtime (see docs/onboarding-models.md); routeForge deploys nothing.`
4. **`routing.yaml`** — remove the `local-8b` and `local-32b` `model_pool` entries (keep `azure-gpt-mid`, `bedrock-frontier`).
5. **`pricing.yaml`** — remove the `local-8b`/`local-32b` rows (keep cloud rows + `baseline_model: bedrock-frontier`).
6. **`lint.py`:**
   - Remove `_VALID_BACKENDS` and the `inference_backend` enum check in `enforce`.
   - `PROVIDER_NAME_DRIFT`: change equality to **subset** — every provider *used* by the pool/pricing must be *declared* in serving, but serving may declare providers with no YAML member (their members come from onboarding):
     ```python
     if not (pool_prov <= serv_prov and price_prov <= serv_prov):
         errs.append(f"PROVIDER_UNDECLARED pool={sorted(pool_prov)} "
                     f"pricing={sorted(price_prov)} not subset of serving={sorted(serv_prov)}")
     ```
   - Remove `TIER_HAS_NO_MEMBER` (an empty YAML tier is legal now; membership can come from the DB).
   - Add **pool-slug ↔ pricing-slug** name cross-check (closes the KeyError gap for cloud built-ins):
     ```python
     price_names = set(s.pricing.models)
     for c in s.routing.model_pool:
         if c.name not in price_names:
             errs.append(f"POOL_MODEL_NO_PRICE {c.name}")
     ```
   - `PROVIDER_NO_LITELLM_MAP` (67-70): drop the `and not (pname == "onprem" and inference_backend == "none")` clause — just `enabled = pcfg.enabled`.

- [ ] **Step 1: Adjust `tests/test_config_lint.py`** — remove/replace cases asserting the old `inference_backend` enum and `TIER_HAS_NO_MEMBER`; add a case asserting a cloud pool member without a price yields `POOL_MODEL_NO_PRICE`, and that an empty on-prem tier boots clean.

- [ ] **Step 2: Run — expect FAIL** on the new assertions.

- [ ] **Step 3: Apply the schema/loader/yaml/lint changes above.**

- [ ] **Step 4: Run** `uv run pytest tests/test_config_lint.py tests/test_context.py -q`. Fix `test_context.py` cases that referenced `inference_backend == "none"` (now derived): seed/withhold an onboarded model via a `ModelRegistry` to exercise the onprem-drop instead. Also **extend `tests/test_derived_onprem_reject.py`** with a now-passing Router-level case (Qwen is gone): an `onprem_only` ctx + empty registry → `Router(S, registry=reg).select(...)` raises `ValueError("no candidates after residency filter")`; and clear any `xfail` markers added in Task 3.

- [ ] **Step 5: Boot-smoke** `uv run python -m app.config.lint` → prints `CONFIG_LINT: ok` with the purged config + empty on-prem tiers.

- [ ] **Step 6: Full suite** `uv run pytest -q`. Fix residual references to `local-8b`/`inference_backend` in other tests (`grep -rn "local-8b\|inference_backend\|SP_INFERENCE_BACKEND" tests`), including `tests/conftest.py` (drop `SP_INFERENCE_BACKEND` setdefault) and `tests/_litellm_fake.py` (its `_map`/health may reference removed fields).

- [ ] **Step 7: Commit** `git add -A && git commit -m "refactor(config): purge inference_backend/vllm/dynamo axis + built-in Qwen; relax lint to YAML-subset"`

---

### Task 5: Admin API — onboard / edit / delete / list + probe

**Files:**
- Modify: `app/api/admin_routes.py` (remove old infra endpoints 391-441 + `InfraSettingsIn`/`InfraModelIn`/`_ENGINES`/`_infra_model_dict`; add onboarding endpoints)
- Test: `tests/test_onboard_api.py` (new); remove/rewrite `tests/test_admin_infra_api.py`

**Interfaces produced:**
- `POST /admin/infra/models` — body `OnboardIn`; runs static-validate → merged-lint → probe → persist → `registry.refresh` → returns row dict incl. `probe_status`/`enabled`.
- `PUT /admin/infra/models/{slug}` — partial edit of an onboarded model; re-probe.
- `DELETE /admin/infra/models/{slug}` — remove onboarded model.
- `GET /admin/infra/models` — list onboarded rows with health/app-scope.
- `probe_endpoint(endpoint: str, slug: str, api_key_env: str | None) -> tuple[bool, str]` — helper (in `app/apps/probe.py` or inline): `GET {endpoint}/models`, assert `slug` in returned ids; on any exception return `(False, str(e))`.

Pydantic:
```python
class OnboardIn(BaseModel):
    slug: str
    display_name: str
    tier: str                       # cheap|mid|frontier
    endpoint: str
    api_key_env: str | None = None
    price_input: float
    price_output: float
    quality: float | None = None
    context_window: int | None = None
    allowed_app_ids: list[str] | None = None
    notes: str | None = None
```

Handler flow (verbatim skeleton):
```python
@admin_router.post("/infra/models")
async def onboard_model(body: OnboardIn, request: Request,
                        _: str = Depends(require_admin)) -> dict:
    s = request.app.state.settings
    reg = request.app.state.registry
    tiers = set(s.routing.tiers)
    yaml_names = {c.name for c in s.routing.model_pool}
    # static validation
    if body.tier not in tiers:
        raise HTTPException(422, f"bad tier {body.tier!r}")
    if not re.match(r"^https?://", body.endpoint):
        raise HTTPException(422, "endpoint must be an http(s) URL")
    if body.price_input < 0 or body.price_output < 0:
        raise HTTPException(422, "price must be >= 0")
    async with request.app.state.db.session() as session:
        if body.slug in yaml_names or await store.get_onboarded(session, body.slug):
            raise HTTPException(409, f"slug {body.slug!r} already exists")
        if body.allowed_app_ids:
            for aid in body.allowed_app_ids:
                if await store.get_application(session, aid) is None:
                    raise HTTPException(422, f"unknown application {aid!r}")
        ok, detail = await probe_endpoint(body.endpoint, body.slug, body.api_key_env)
        await store.upsert_onboarded(
            session, body.slug, display_name=body.display_name, provider="onprem",
            tier=body.tier, endpoint=body.endpoint, api_key_env=body.api_key_env,
            price_input=body.price_input, price_output=body.price_output,
            quality=body.quality, context_window=body.context_window,
            allowed_app_ids=body.allowed_app_ids, notes=body.notes,
            enabled=ok, probe_status=("healthy" if ok else "unreachable"),
            probe_error=(None if ok else detail))
        await session.commit()
    await reg.refresh(request.app.state.db)
    return {"slug": body.slug, "enabled": ok,
            "probe_status": "healthy" if ok else "unreachable",
            "probe_error": None if ok else detail}
```
`PUT` mirrors this on an existing onboarded slug (404 if absent, re-probe, update). `DELETE` calls `store.delete_onboarded` + `reg.refresh`, 404 if absent. `GET` returns `store.list_onboarded` mapped to dicts.

`probe_endpoint` (put in `app/apps/probe.py`):
```python
import httpx
async def probe_endpoint(endpoint: str, slug: str, api_key_env: str | None) -> tuple[bool, str]:
    import os
    headers = {}
    if api_key_env and os.environ.get(api_key_env):
        headers["Authorization"] = f"Bearer {os.environ[api_key_env]}"
    url = endpoint.rstrip("/") + "/models"
    try:
        async with httpx.AsyncClient(timeout=5) as c:
            resp = await c.get(url, headers=headers)
            resp.raise_for_status()
            ids = {m.get("id") for m in resp.json().get("data", [])}
        if slug not in ids:
            return False, f"served-model-name {slug!r} not in {sorted(ids)}"
        return True, "ok"
    except Exception as e:
        return False, str(e)
```

- [ ] **Step 1: Write failing test** `tests/test_onboard_api.py` — use the `client` `TestClient` fixture + `_login` idiom from `tests/test_admin_infra_api.py`; monkeypatch `app.apps.probe.probe_endpoint` (or the imported name in admin_routes) to return `(True, "ok")` / `(False, "boom")`:

```python
def test_onboard_probe_pass_enables(client, monkeypatch):
    monkeypatch.setattr("app.api.admin_routes.probe_endpoint",
                        lambda *a, **k: _ok())  # async helper returning (True,"ok")
    _login(client)
    r = client.post("/admin/infra/models", json={
        "slug": "nemotron-mid", "display_name": "Nemotron", "tier": "mid",
        "endpoint": "http://nem/v1", "price_input": 0.2, "price_output": 0.2})
    assert r.status_code == 200 and r.json()["enabled"] is True
    lst = client.get("/admin/infra/models").json()
    assert any(m["slug"] == "nemotron-mid" and m["enabled"] for m in lst)

def test_onboard_probe_fail_disables(client, monkeypatch): ...   # enabled False, probe_status unreachable
def test_onboard_bad_tier_422(client): ...
def test_onboard_duplicate_409(client, monkeypatch): ...
def test_delete_onboarded(client, monkeypatch): ...
```
(Provide the `_ok`/`_fail` async monkeypatch helpers and `_login` per the existing test module.)

- [ ] **Step 2: Run — expect FAIL** (404/handler missing).

- [ ] **Step 3: Implement** the endpoints + `probe_endpoint`; remove the old `/admin/infra` + `/admin/infra/models/{slug}` PUT (Task-0 versions) and the `InfraSettings*`/`_ENGINES`/`_infra_model_dict` symbols. Remove `store` infra-settings imports.

- [ ] **Step 4: Run — expect PASS**; then full suite; delete `tests/test_admin_infra_api.py` (old contract) — its coverage is replaced.

- [ ] **Step 5: Commit** `git add -A && git commit -m "feat(admin): probe-gated model onboarding API (POST/PUT/DELETE/GET /admin/infra/models)"`

---

### Task 6: Compose cleanup (no stub, no seed)

Per the user: they run their own application and drive real traffic, so there is **no
demo stub service and no first-boot seed**. routeForge boots with an empty pool; the
operator onboards their real model (e.g. Nemotron) via the admin API/UI. This task only
removes now-dead compose config.

**Files:** Modify `deploy/compose/docker-compose.yml`

- Under `api.environment`: remove `SP_INFERENCE_BACKEND: mock` (the loader no longer
  reads it after Task 4; leaving it is a dead, misleading knob).
- The `vllm` GPU-profile service: either delete it, or leave it with a comment
  reframing it as *an example on-prem endpoint an operator would deploy and then
  onboard* (routeForge does not manage it). Prefer deletion unless it reads as useful
  documentation.
- Confirm the stack still `config`-validates: `docker compose -f deploy/compose/docker-compose.yml config -q` exits 0.

- [ ] **Step 1: Apply the compose edits.**
- [ ] **Step 2: Verify** `docker compose -f deploy/compose/docker-compose.yml config -q` (exit 0). No test change (compose isn't covered by pytest).
- [ ] **Step 3: Commit** `git add deploy/compose/docker-compose.yml && git commit -m "chore(compose): drop dead SP_INFERENCE_BACKEND + vllm demo service"`

---

## Milestone 2 — UI

### Task 7: `api.ts` — onboarding types + `onboardApi`

**Files:** Modify `ui/src/api.ts` (replace `InfraSettings`/`InfraModel`/`infraApi` at 266-306)

```ts
export type OnboardedModel = {
  slug: string; display_name: string; tier: string; provider: string;
  endpoint: string; api_key_env: string | null;
  price_input: number; price_output: number;
  quality: number | null; context_window: number | null;
  enabled: boolean; allowed_app_ids: string[] | null;
  probe_status: string; probe_error: string | null; notes: string | null;
};
export type OnboardIn = {
  slug: string; display_name: string; tier: string; endpoint: string;
  api_key_env?: string | null; price_input: number; price_output: number;
  quality?: number | null; context_window?: number | null;
  allowed_app_ids?: string[] | null; notes?: string | null;
};
export const onboardApi = {
  list: () => fetch("/admin/infra/models", { credentials: CRED }).then(j<OnboardedModel[]>),
  create: (body: OnboardIn) =>
    fetch("/admin/infra/models", opts("POST", body)).then(j<OnboardedModel & {enabled: boolean}>),
  update: (slug: string, body: Partial<OnboardIn>) =>
    fetch(`/admin/infra/models/${encodeURIComponent(slug)}`, opts("PUT", body)).then(j<OnboardedModel>),
  remove: (slug: string) =>
    fetch(`/admin/infra/models/${encodeURIComponent(slug)}`, opts("DELETE")).then(j<{ ok: boolean }>),
};
```
Grep the UI for `infraApi`/`InfraModel`/`InfraSettings` usages and update/remove.

- [ ] Step 1: Apply the type/api changes. Step 2: `cd ui && npm run typecheck` — fix references. Step 3: Commit.

### Task 8: `InfrastructurePanel.tsx` — router-only fleet + onboard form

**Files:** Modify `ui/src/admin/InfrastructurePanel.tsx` (full rewrite)

Remove `ClusterCard`, `EngineSelect`, `ENGINES`, the Engine/Replicas/TP/GPU columns and their state. New structure:
- **Onboard form** (modal or inline `<form>`): inputs for display name, slug, tier `<select>` (`cheap|mid|frontier`), endpoint, api_key_env, price_input, price_output, optional quality/context_window, applications multi-select (fetch app list — reuse existing apps API if present, else a comma field of app ids), notes. Submit → `onboardApi.create` → show returned `probe_status`/`probe_error` inline (green enabled / red unreachable).
- **Fleet table** columns: **Model** (display_name + slug) · **Tier** (`TierDot`) · **Endpoint** · **Apps** (`allowed_app_ids ?? "all"`) · **Enabled** (`Toggle` → `onboardApi.update(slug,{enabled})`) · **Health** (`probe_status` badge; red on `unreachable` reusing the existing `text-alert` idiom) · actions (delete → `onboardApi.remove`). Empty state: "No models onboarded — add one above."
- Reuse `Panel`, `TierDot`, `Toggle` from the current file. Keep the dim-row idiom for disabled models.

- [ ] Step 1: Rewrite the component. Step 2: `npm run typecheck` + `npm run build`. Step 3: Manual smoke via `scripts/dev-frontend.sh` against `scripts/dev-backend.sh` — onboard a reachable OpenAI-compatible endpoint (probe passes → enabled), see it in the fleet, then delete it. (A wrong endpoint should save disabled with the probe error shown.) Step 4: Commit `feat(ui): model onboarding panel — router-only fleet + onboard form`.

---

## Milestone 3 — Docs & skills

### Task 9: Onboarding guide + CLAUDE.md + skills

**Files:** Create `docs/onboarding-models.md`; modify `CLAUDE.md`, `.claude/skills/inference-backends/SKILL.md`, `.claude/skills/routing-and-gateway/SKILL.md`

- **`docs/onboarding-models.md`:** (a) deploy your model externally (out of scope — vLLM/Dynamo/NIM/any OpenAI-compatible server) with `--served-model-name <slug>`; (b) onboard via UI (Infrastructure → Onboard) or `POST /admin/infra/models` with the field reference; (c) what the probe checks (`GET {endpoint}/models` contains the slug); (d) app scoping (`allowed_app_ids` empty = all); (e) privacy behavior (onboarded = on-prem; no healthy on-prem ⇒ `onprem_only` rejected); (f) migration note (`CREATE TABLE IF NOT EXISTS onboarded_models` on existing DBs; drop orphaned `infra_models`/`infra_settings`); (g) the pool boots empty — the operator onboards their own model; there is no bundled demo model.
- **`CLAUDE.md`:** remove the `inference_backend: mock|vllm|dynamo|none` config block/rules, the `serving_paths`/backend-rules section, and the "mock=default" lines; reframe the step-8 module row and the `serving.yaml` ownership row (no `inference_backend`); update the demo narrative (the operator onboards their own model; no bundled demo model) and build-order §9 (real backends → "operators onboard endpoints"). Keep all privacy hard rules (state the `none`-reject as derived).
- **`inference-backends` skill:** reframe intro to "routeForge points at OpenAI-compatible endpoints and deploys nothing; on-prem models are onboarded (see docs/onboarding-models.md)." Move vLLM/Dynamo/TensorRT content under "what the operator runs behind the endpoint — outside routeForge." Remove the `SP_INFERENCE_BACKEND` switching instructions.
- **`routing-and-gateway` skill:** update the serving-paths table/section — pool + endpoints come from the `ModelRegistry`; no `inference_backend` axis.

- [ ] Step 1: Write `docs/onboarding-models.md`. Step 2: Edit CLAUDE.md + both skills. Step 3: `grep -rn "inference_backend\|SP_INFERENCE_BACKEND\|local-8b\|serving_paths" CLAUDE.md .claude/skills docs` — no stale references remain (except historical spec/plan docs). Step 4: Commit `docs: onboarding guide; purge inference_backend axis from CLAUDE.md + skills`.

---

## Self-review notes (spec coverage)

- Spec §Core model → Task 1. Spec and plan agree: `infra_models`/`InfraSettings` are deleted and `onboarded_models` is the single model table (the built-in Qwen it would have overlaid is removed).
- Spec §Merged view → Task 2. §Router/gateway/backend → Task 3. §Config purge → Task 4. §Admin API + probe → Task 5. §No bundled demo model → Task 6 (compose cleanup). §UI → Tasks 7-8. §Docs/skills → Task 9. §Derived privacy-reject → Task 3 (context) + Task 3 test. §App-scoping → Tasks 2/3/5. §Migration → Task 1 (additive) + Task 9 (documented).
- Lint invariant move (spec §Config) → Task 4 (`PROVIDER_UNDECLARED` subset + drop `TIER_HAS_NO_MEMBER` + add `POOL_MODEL_NO_PRICE`).
