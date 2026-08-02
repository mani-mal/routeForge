# Infra Management Config + "Switchyard" UI Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give an admin a runtime, DB-backed, console-editable declaration of the on-prem serving fleet (Run:ai license, per-model engine + enabled + topology) that the router consumes to gate candidates and annotate decision traces, and ship it inside a cohesive "Switchyard" design-system pass across the console.

**Architecture:** Two new SQLAlchemy tables (`infra_settings` singleton + `infra_models`) written through an admin API and read via a process-global `InfraRoster` snapshot injected into the `Router`. The router excludes admin-disabled on-prem models (reason "disabled by admin") and annotates the chosen model's `serving_engine` on the `DecisionRecord`. The UI gains a token system (new palette + self-hosted fonts + a rail/junction signature), a reskin of the existing console/admin surfaces, and a new "Infrastructure" admin section. Docs: the `docs/dynamo.txt` ownership boundary is promoted into the `inference-backends` skill.

**Tech Stack:** Python 3.12, FastAPI, Pydantic v2, SQLAlchemy async (`app.apps.db.Base` + `create_all`), `uv`, pytest. UI: React 18 + Vite 5 + TypeScript + Tailwind 3.4 + Recharts, self-hosted `@fontsource` fonts.

**Spec:** `docs/superpowers/specs/2026-08-01-infra-management-config-design.md` (approved). §5 data model, §6 gate+annotate, §7 API+UI, §11 tests, §12 UI design language.

## Global Constraints

Every task's requirements implicitly include this section.

- **Branch:** `feat/infra-management-config` (already checked out; all work lands here).
- **CLAUDE.md hard rules — non-negotiable:**
  - **Rule 3 — `onprem_only` is absolute.** Disabling on-prem models must NEVER route an `onprem_only` request to cloud. Empty candidate pool → REJECT via the existing `ValueError` path in `Router.select`. This is the load-bearing test (Task A2).
  - **Rule 4 — no auto-deploy.** routeForge describes + consumes the fleet; it deploys/renders nothing. No manifest authoring, no Run:ai/Dynamo API calls.
  - **Rule 5 — fail closed** in the gate; the `InfraRoster` fail-*safe* (last-known-good → permissive) governs only ops availability and can never breach residency (the gate enforces residency independently).
- **No migration for new tables.** `infra_settings` + `infra_models` are NEW TABLES → `Base.metadata.create_all` (in `app/apps/db.py:ensure_ready`) creates them automatically once they are classes on `Base` imported via `app/apps/models.py`. Do NOT add columns to any existing table (that would need a manual ALTER on deployed DBs — out of scope here).
- **One owner per concern.** `infra_models` owns ONLY serving metadata (engine, enabled, descriptive topology). It NEVER restates tier, provider, quality, or cost — those stay in `routing.yaml`/`pricing.yaml`. The boot config lint (`app/config/lint.py`) is untouched; this layer is runtime/DB.
- **Cloud models are not fleet infra.** `azure`/`bedrock` are external APIs; they are never `infra_models` rows. On-prem only.
- **Privacy/honesty.** No prompt/answer/PII is stored or displayed in this layer. `serving_engine` is a model-name/engine string (e.g. `"vllm"`), never request content. Telemetry/`capture_io` behavior is unchanged.
- **Backend tests:** `uv run pytest` (from repo root). Scope during dev with `uv run pytest tests/<file>.py -v`. NEVER run bare `uv sync`.
- **UI verification:** no JS test runner exists in the repo. Each UI task gates on `cd ui && npm run typecheck` and `cd ui && npm run build` (must produce `ui/dist/index.html`) plus an explicit acceptance checklist. Do not add a test runner (scope creep).
- **UI fonts self-hosted** via `@fontsource/*` npm packages — NO external CDN (`fonts.googleapis.com` etc.); the product runs on air-gapped/on-prem clusters.
- **Color is semantic.** Consolidate the three hardcoded tier-hex copies (`api.ts` `TIER_COLOR`, `BrandLogo.tsx` SVG, `RequestDrilldown.tsx` verdict colors) into ONE source (`ui/src/tokens.ts`). Reserve `alert` red strictly for breaches/health-fail, never decoration.
- **a11y floor (unannounced):** visible keyboard focus, `prefers-reduced-motion` respected, WCAG-AA text contrast, responsive to mobile. The existing `theme.ts` dark/light system stays; define tokens for both themes.
- **Git staging by path.** Stage only the files each task names. NEVER `git add -A` / `git add .` on the repo root. NEVER stage the intentionally-untracked `deploy/hpe/`, `docs/marketing/`, `docs/dashboard-metrics.txt`, `docs/demo-dashboard-notes.md`.
- **Commit trailer** on every commit:
  ```
  Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
  ```

---

## File Structure

**Backend (new):**
- `app/apps/models.py` (modify) — add `InfraSettings`, `InfraModel` tables.
- `app/apps/store.py` (modify) — add `get/upsert_infra_settings`, `list/get/upsert_infra_model`.
- `app/apps/infra.py` (create) — `InfraRoster` cache + `InfraSnapshot`/`InfraModelRow`/`InfraClusterRow` dataclasses.
- `app/routing/router.py` (modify) — `infra_roster` param; disable-gate in `select`; `serving_engine` on `RoutingDecision`.
- `app/models.py` (modify) — `serving_engine: str | None` on `DecisionRecord`.
- `app/gateway/gateway.py` (modify) — copy `decision.serving_engine` onto the router `DecisionRecord`.
- `app/api/routes.py` (modify) — `await infra_roster.ensure_loaded(db)` before `pipeline.run`.
- `app/api/admin_routes.py` (modify) — `/admin/infra` GET/PUT, `/admin/infra/models` GET, `/admin/infra/models/{slug}` PUT.
- `app/main.py` (modify) — construct `app.state.infra_roster`, inject into `Router`.
- `tests/test_infra_store.py`, `tests/test_infra_router.py`, `tests/test_admin_infra_api.py` (create).

**Frontend (new/modify):**
- `ui/package.json` (modify) — add `@fontsource/archivo`, `@fontsource/ibm-plex-sans`, `@fontsource/ibm-plex-mono`.
- `ui/tailwind.config.js` (modify) — new palette tokens + `fontFamily` + rail keyframes.
- `ui/src/index.css` (modify) — CSS var updates (both themes) + reduced-motion.
- `ui/src/main.tsx` (modify) — import font weights.
- `ui/src/tokens.ts` (create) — single source for `TIER_HEX`, `SIGNAL_HEX`.
- `ui/src/api.ts` (modify) — `infraApi`; `TIER_COLOR` reads `tokens.ts`.
- `ui/src/components/{BrandLogo,common,HeadroomToggle,RequestList}.tsx` (modify) — reskin + read `tokens.ts`.
- `ui/src/App.tsx`, `ui/src/console/ConsoleShell.tsx` (modify) — rail shell + "Infrastructure" nav.
- `ui/src/console/{FleetOverview,RequestDrilldown,AppPage}.tsx`, `ui/src/admin/{ApplicationsPanel,PrivacyRulesEditor}.tsx` (modify) — reskin.
- `ui/src/admin/InfrastructurePanel.tsx` (create) — the Infrastructure section.

**Docs:**
- `.claude/skills/inference-backends/SKILL.md` (modify) — ownership-boundary section.
- `docs/dynamo.txt` (delete or one-line pointer).

---

## Task A1: Infra tables + store CRUD

**Files:**
- Modify: `app/apps/models.py` (add two classes near `ModelQuality` ~line 184)
- Modify: `app/apps/store.py` (add functions near `upsert_model_quality` ~line 293)
- Test: `tests/test_infra_store.py` (create)

**Interfaces:**
- Consumes: `Base`, `utcnow` from `app/apps/models.py`; `Database` from `app/apps/db.py`.
- Produces:
  - `InfraSettings` (table `infra_settings`, PK `id="cluster"`), `InfraModel` (table `infra_models`, PK `model_name`).
  - `async def get_infra_settings(session) -> InfraSettings | None`
  - `async def upsert_infra_settings(session, **fields) -> InfraSettings`
  - `async def list_infra_models(session) -> list[InfraModel]`
  - `async def get_infra_model(session, model_name: str) -> InfraModel | None`
  - `async def upsert_infra_model(session, model_name: str, **fields) -> InfraModel`

- [ ] **Step 1: Write the failing test**

Create `tests/test_infra_store.py`:

```python
import asyncio

from app.apps import store
from app.apps.db import Database


def _run(coro):
    return asyncio.run(coro)


def test_infra_settings_singleton_roundtrip():
    db = Database(url="sqlite+aiosqlite:///:memory:")

    async def go():
        async with db.session() as s:
            await store.upsert_infra_settings(
                s, runai_licensed=True, runai_project="runai-routeforge",
                default_engine="trtllm")
            await s.commit()
        async with db.session() as s:
            row = await store.get_infra_settings(s)
            assert row is not None
            assert row.id == "cluster"
            assert row.runai_licensed is True
            assert row.runai_project == "runai-routeforge"
            assert row.default_engine == "trtllm"
        # second upsert updates the same singleton row
        async with db.session() as s:
            await store.upsert_infra_settings(s, runai_licensed=False)
            await s.commit()
        async with db.session() as s:
            row = await store.get_infra_settings(s)
            assert row.runai_licensed is False
            assert row.default_engine == "trtllm"  # untouched field preserved

    _run(go())


def test_infra_model_upsert_and_list():
    db = Database(url="sqlite+aiosqlite:///:memory:")

    async def go():
        async with db.session() as s:
            await store.upsert_infra_model(
                s, "local-8b", engine="vllm", enabled=True, gpu_type="NVIDIA-H200",
                replicas=2, tensor_parallel=1)
            await store.upsert_infra_model(s, "local-32b", engine="trtllm", enabled=False)
            await s.commit()
        async with db.session() as s:
            rows = await store.list_infra_models(s)
            by_name = {r.model_name: r for r in rows}
            assert by_name["local-8b"].engine == "vllm"
            assert by_name["local-8b"].enabled is True
            assert by_name["local-8b"].replicas == 2
            assert by_name["local-32b"].enabled is False
        # upsert existing patches only given fields
        async with db.session() as s:
            await store.upsert_infra_model(s, "local-32b", enabled=True)
            await s.commit()
        async with db.session() as s:
            row = await store.get_infra_model(s, "local-32b")
            assert row.enabled is True
            assert row.engine == "trtllm"  # preserved

    _run(go())
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_infra_store.py -v`
Expected: FAIL — `AttributeError: module 'app.apps.store' has no attribute 'upsert_infra_settings'` (and no `InfraSettings`/`InfraModel` tables).

- [ ] **Step 3: Add the tables**

In `app/apps/models.py`, after the `ModelQuality` class (~line 202), add. (`Boolean`, `Integer`, `String`, `DateTime`, `Mapped`, `mapped_column`, `utcnow` are already imported at the top of this file.)

```python
class InfraSettings(Base):
    """Cluster-level serving-fleet facts (singleton, id='cluster'): whether
    GPUs are Run:ai-scheduled and the default engine for a model with no
    explicit engine. Runtime/DB-backed — distinct from serving.yaml's boot-time
    `inference_backend`. NEW table → create_all handles it, no migration. No
    request content / PII. See infra-management-config-design.md §5."""

    __tablename__ = "infra_settings"

    id: Mapped[str] = mapped_column(String(16), primary_key=True, default="cluster")
    runai_licensed: Mapped[bool] = mapped_column(Boolean, default=False)
    runai_project: Mapped[str | None] = mapped_column(String(128), nullable=True)
    default_engine: Mapped[str] = mapped_column(String(16), default="vllm")
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class InfraModel(Base):
    """Per on-prem model serving metadata: engine (vllm|trtllm), the admin
    availability toggle that gates routing, and descriptive topology (replicas,
    TP, GPU type, endpoint). Additive to routing.yaml's model_pool keyed by the
    same slug — NEVER restates tier/provider/quality/cost. On-prem only (cloud
    models are provider APIs, not fleet infra). NEW table → create_all, no
    migration. See infra-management-config-design.md §5."""

    __tablename__ = "infra_models"

    model_name: Mapped[str] = mapped_column(String(100), primary_key=True)
    engine: Mapped[str] = mapped_column(String(16), default="vllm")  # vllm | trtllm
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    replicas: Mapped[int | None] = mapped_column(Integer, nullable=True)
    min_replicas: Mapped[int | None] = mapped_column(Integer, nullable=True)
    max_replicas: Mapped[int | None] = mapped_column(Integer, nullable=True)
    tensor_parallel: Mapped[int | None] = mapped_column(Integer, nullable=True)
    gpu_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    endpoint: Mapped[str | None] = mapped_column(String(255), nullable=True)
    runai_scheduled: Mapped[bool] = mapped_column(Boolean, default=False)
    notes: Mapped[str | None] = mapped_column(String(255), nullable=True)
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow)
```

- [ ] **Step 4: Add the store functions**

In `app/apps/store.py`, after `upsert_model_quality` (~line 319). `select`, `AsyncSession`, `utcnow` and the model classes are imported in this module (confirm `InfraSettings, InfraModel` are added to the existing `from app.apps.models import (...)` block).

```python
_INFRA_SETTINGS_FIELDS = {"runai_licensed", "runai_project", "default_engine"}
_INFRA_MODEL_FIELDS = {
    "engine", "enabled", "replicas", "min_replicas", "max_replicas",
    "tensor_parallel", "gpu_type", "endpoint", "runai_scheduled", "notes"}


async def get_infra_settings(session: AsyncSession) -> InfraSettings | None:
    return await session.get(InfraSettings, "cluster")


async def upsert_infra_settings(session: AsyncSession, **fields) -> InfraSettings:
    """Create or update the cluster singleton (caller commits)."""
    row = await session.get(InfraSettings, "cluster")
    if row is None:
        row = InfraSettings(id="cluster")
        session.add(row)
    for k, v in fields.items():
        if k in _INFRA_SETTINGS_FIELDS:
            setattr(row, k, v)
    return row


async def list_infra_models(session: AsyncSession) -> list[InfraModel]:
    return list((await session.execute(select(InfraModel))).scalars().all())


async def get_infra_model(session: AsyncSession, model_name: str) -> InfraModel | None:
    return await session.get(InfraModel, model_name)


async def upsert_infra_model(session: AsyncSession, model_name: str, **fields) -> InfraModel:
    """Create or update one on-prem model's serving metadata (caller commits)."""
    row = await session.get(InfraModel, model_name)
    if row is None:
        row = InfraModel(model_name=model_name)
        session.add(row)
    for k, v in fields.items():
        if k in _INFRA_MODEL_FIELDS:
            setattr(row, k, v)
    return row
```

- [ ] **Step 5: Run test to verify it passes**

Run: `uv run pytest tests/test_infra_store.py -v`
Expected: PASS (both tests).

- [ ] **Step 6: Run the full suite to confirm no regression**

Run: `uv run pytest`
Expected: PASS (new tables are created by `create_all` and touch nothing existing).

- [ ] **Step 7: Commit**

```bash
git add app/apps/models.py app/apps/store.py tests/test_infra_store.py
git commit -m "feat(infra): infra_settings + infra_models tables and store CRUD

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task A2: InfraRoster cache + Router disable-gate

**Files:**
- Create: `app/apps/infra.py`
- Modify: `app/routing/router.py` (constructor ~line 23; residency loop ~line 47-57; `RoutingDecision` dataclass ~line 13; return ~line 108)
- Test: `tests/test_infra_router.py` (create)

**Interfaces:**
- Consumes: `InfraSettings`, `InfraModel` from `app/apps/models.py`; `Database` from `app/apps/db.py`; `Settings` from `app/config/schema.py`.
- Produces:
  - `InfraRoster(settings)` with `async ensure_loaded(db)`, `async refresh(db)`, `current() -> InfraSnapshot | None`, `is_enabled(model_name) -> bool`, `engine_for(model_name) -> str | None`.
  - `InfraSnapshot(cluster: InfraClusterRow, models: dict[str, InfraModelRow])`, `InfraClusterRow`, `InfraModelRow` (frozen dataclasses).
  - `Router.__init__(settings, quality_resolver=None, infra_roster=None)`; `RoutingDecision.serving_engine: str | None`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_infra_router.py`. This models the existing `tests/test_router.py` (module-level `Settings`, `_ctx` helper) and uses a hand-built `InfraRoster` snapshot (no DB) via a small helper.

```python
import pytest

from app.apps.infra import InfraRoster, InfraSnapshot, InfraClusterRow, InfraModelRow
from app.config.loader import load_settings
from app.models import ExecutionPolicyContext
from app.routing.router import Router

S = load_settings()


def _ctx(providers, risk="low", onprem=False):
    return ExecutionPolicyContext(
        request_id="r", tenant_id="t", sensitivity="public", onprem_only=onprem,
        allowed_providers=frozenset(providers), allowed_models=frozenset(),
        max_cost_usd=0.05, max_latency_ms=15000, risk_class=risk,
        require_verification=False, detected_entities=(), redaction_map_id=None)


def _roster(models: dict[str, InfraModelRow], *, default_engine="vllm"):
    r = InfraRoster(S)
    r._snap = InfraSnapshot(
        cluster=InfraClusterRow(default_engine=default_engine), models=models)
    return r


def _model(name, *, engine="vllm", enabled=True):
    return InfraModelRow(model_name=name, engine=engine, enabled=enabled)


def _onprem_names():
    return [c.name for c in S.routing.model_pool if c.provider == "onprem"]


def test_disabled_onprem_model_excluded_with_reason():
    names = _onprem_names()
    assert len(names) >= 2, "test needs >=2 on-prem pool members"
    disabled = names[0]
    roster = _roster({disabled: _model(disabled, enabled=False)})
    r = Router(S, infra_roster=roster)
    d = r.select(_ctx({"onprem"}, onprem=True), tokens_after=20, complexity=0.9)
    assert d.chosen.name != disabled
    assert any(rc.name == disabled and "disabled by admin" in rc.reason
               for rc in d.rejected)


def test_all_onprem_disabled_onprem_only_rejects_never_cloud():
    roster = _roster({n: _model(n, enabled=False) for n in _onprem_names()})
    r = Router(S, infra_roster=roster)
    with pytest.raises(ValueError):
        r.select(_ctx({"onprem"}, onprem=True), tokens_after=20, complexity=0.1)


def test_all_enabled_roster_is_regression_noop():
    roster = _roster({n: _model(n, enabled=True) for n in _onprem_names()})
    with_roster = Router(S, infra_roster=roster).select(
        _ctx({"onprem", "azure", "bedrock"}), tokens_after=20, complexity=0.1)
    without = Router(S).select(
        _ctx({"onprem", "azure", "bedrock"}), tokens_after=20, complexity=0.1)
    assert with_roster.chosen.name == without.chosen.name


def test_serving_engine_annotated_for_onprem_choice():
    roster = _roster({n: _model(n, enabled=True) for n in _onprem_names()})
    d = Router(S, infra_roster=roster).select(
        _ctx({"onprem"}, onprem=True), tokens_after=20, complexity=0.1)
    assert d.chosen.provider == "onprem"
    assert d.serving_engine == "vllm"  # S.serving.inference_backend is "vllm" in tests
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_infra_router.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.apps.infra'`.

- [ ] **Step 3: Create the InfraRoster module**

Create `app/apps/infra.py`:

```python
from __future__ import annotations

import logging
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.apps.models import InfraModel, InfraSettings

_log = logging.getLogger("serving.infra")


@dataclass(frozen=True)
class InfraModelRow:
    model_name: str
    engine: str
    enabled: bool
    replicas: int | None = None
    min_replicas: int | None = None
    max_replicas: int | None = None
    tensor_parallel: int | None = None
    gpu_type: str | None = None
    endpoint: str | None = None
    runai_scheduled: bool = False
    notes: str | None = None


@dataclass(frozen=True)
class InfraClusterRow:
    runai_licensed: bool = False
    runai_project: str | None = None
    default_engine: str = "vllm"


@dataclass(frozen=True)
class InfraSnapshot:
    cluster: InfraClusterRow
    models: dict[str, InfraModelRow]


class InfraRoster:
    """Process-global, lazily-loaded snapshot of the DB-declared on-prem fleet.
    Injected into Router (sync reads on the request path); refreshed on every
    admin write. On a DB error it keeps last-known-good; a cold cache is
    PERMISSIVE (all on-prem enabled) so a DB blip degrades ops visibility, never
    residency — the gate enforces onprem_only -> never-cloud independently.
    See infra-management-config-design.md §6."""

    def __init__(self, settings) -> None:
        self._s = settings
        self._snap: InfraSnapshot | None = None

    async def ensure_loaded(self, db) -> None:
        if self._snap is None:
            await self.refresh(db)

    async def refresh(self, db) -> None:
        try:
            async with db.session() as session:
                self._snap = await self._load(session)
        except Exception:
            # keep last-known-good; cold cache stays None -> permissive current()
            _log.exception("infra roster refresh failed; keeping last-known-good")

    @staticmethod
    async def _load(session: AsyncSession) -> InfraSnapshot:
        cs = await session.get(InfraSettings, "cluster")
        cluster = InfraClusterRow(
            runai_licensed=bool(cs.runai_licensed) if cs else False,
            runai_project=cs.runai_project if cs else None,
            default_engine=cs.default_engine if cs else "vllm")
        rows = (await session.execute(select(InfraModel))).scalars().all()
        models = {
            r.model_name: InfraModelRow(
                model_name=r.model_name, engine=r.engine, enabled=bool(r.enabled),
                replicas=r.replicas, min_replicas=r.min_replicas,
                max_replicas=r.max_replicas, tensor_parallel=r.tensor_parallel,
                gpu_type=r.gpu_type, endpoint=r.endpoint,
                runai_scheduled=bool(r.runai_scheduled), notes=r.notes)
            for r in rows}
        return InfraSnapshot(cluster=cluster, models=models)

    def current(self) -> InfraSnapshot | None:
        return self._snap

    def is_enabled(self, model_name: str) -> bool:
        """Fail toward serving: unknown model or cold cache -> enabled."""
        snap = self._snap
        if snap is None:
            return True
        row = snap.models.get(model_name)
        return True if row is None else row.enabled

    def engine_for(self, model_name: str) -> str | None:
        """Serving engine for an on-prem model. Under global `vllm` it is 'vllm'
        by construction; under `dynamo` it is the per-model engine (or the
        cluster default). Only meaningful for on-prem choices; callers pass an
        on-prem model_name."""
        ib = self._s.serving.inference_backend
        if ib == "vllm":
            return "vllm"
        if ib == "none":
            return None
        # dynamo: heterogeneous workers
        snap = self._snap
        if snap is not None:
            row = snap.models.get(model_name)
            if row is not None and row.engine:
                return row.engine
            return snap.cluster.default_engine
        return None
```

- [ ] **Step 4: Wire the gate + annotation into the Router**

In `app/routing/router.py`:

(a) Add `serving_engine` to `RoutingDecision` (~line 13-20):
```python
@dataclass
class RoutingDecision:
    chosen: PoolCandidate
    est_cost_usd: float
    reason: str
    rejected: list[RejectedCandidate] = field(default_factory=list)
    quality_score: float = 0.0
    quality_source: str = "tier_default"
    serving_engine: str | None = None
```

(b) Accept the roster in `__init__` (~line 23-27):
```python
class Router:
    def __init__(self, settings: Settings,
                 quality_resolver: QualityResolver | None = None,
                 infra_roster: "InfraRoster | None" = None) -> None:
        self._s = settings
        self._quality = quality_resolver or QualityResolver(settings)
        self._infra = infra_roster
```
Add the import at the top of the module: `from app.apps.infra import InfraRoster` (used only for typing; safe — `app.apps.infra` imports only models, no router cycle). If a circular-import surfaces, make it a `TYPE_CHECKING`-guarded import and quote the annotation.

(c) In the residency-filter loop (~line 47-57), reject admin-disabled on-prem candidates. Replace the loop body so a candidate that passes residency is ALSO checked against the roster:
```python
    allowed = []
    for c in pool:
        if c.provider not in ctx.allowed_providers:
            rejected.append(RejectedCandidate(
                name=c.name, provider=c.provider, tier=c.tier,
                display_name=c.display_name,
                reason="excluded by policy (residency)"))
            continue
        if (c.provider == "onprem" and self._infra is not None
                and not self._infra.is_enabled(c.name)):
            rejected.append(RejectedCandidate(
                name=c.name, provider=c.provider, tier=c.tier,
                display_name=c.display_name, reason="disabled by admin"))
            continue
        allowed.append(c)
    if not allowed:
        raise ValueError("no candidates after residency filter")
```
(The `onprem_only` + all-disabled case flows straight into this `raise` — cloud was already excluded by residency, so the pool cannot be re-populated with a cloud model. This is Rule 3.)

(d) At the return (~line 108-113), set `serving_engine` for an on-prem choice:
```python
    chosen_q = self._quality.resolve(chosen.name)
    serving_engine = (
        self._infra.engine_for(chosen.name)
        if self._infra is not None and chosen.provider == "onprem" else None)
    return RoutingDecision(
        chosen=chosen, est_cost_usd=est,
        reason=f"tier {chosen.tier} (target {target})", rejected=rejected,
        quality_score=chosen_q.score, quality_source=chosen_q.source,
        serving_engine=serving_engine)
```

- [ ] **Step 5: Run the test to verify it passes**

Run: `uv run pytest tests/test_infra_router.py -v`
Expected: PASS (all four tests).

- [ ] **Step 6: Run the existing router suite for regression**

Run: `uv run pytest tests/test_router.py -v`
Expected: PASS (no roster injected → `self._infra is None` → identical behavior).

- [ ] **Step 7: Commit**

```bash
git add app/apps/infra.py app/routing/router.py tests/test_infra_router.py
git commit -m "feat(infra): InfraRoster + router disable-gate; onprem_only+all-disabled rejects

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task A3: serving_engine on DecisionRecord (gateway annotation)

**Files:**
- Modify: `app/models.py` (`DecisionRecord` ~line 82-94)
- Modify: `app/gateway/gateway.py` (router record ~line 55-62)
- Test: `tests/test_infra_router.py` (add an assertion) or `tests/test_gateway.py` if present

**Interfaces:**
- Consumes: `RoutingDecision.serving_engine` (Task A2).
- Produces: `DecisionRecord.serving_engine: str | None`, populated on the `step="router"` record.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_infra_router.py` a gateway-level check IF a gateway test harness exists; otherwise assert at the model level here. Minimal model-level guard (always valid):

```python
def test_decision_record_has_serving_engine_field():
    from app.models import DecisionRecord
    rec = DecisionRecord(step="router", chosen="local-8b", reason="x",
                         serving_engine="vllm")
    assert rec.serving_engine == "vllm"
    # default is None (cloud / not annotated)
    assert DecisionRecord(step="router", chosen="x", reason="y").serving_engine is None
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_infra_router.py::test_decision_record_has_serving_engine_field -v`
Expected: FAIL — `TypeError: ... unexpected keyword argument 'serving_engine'`.

- [ ] **Step 3: Add the field**

In `app/models.py`, in `DecisionRecord` (after `quality_source`, ~line 94):
```python
    serving_engine: str | None = None  # on-prem engine (vllm|trtllm); None for cloud
```

- [ ] **Step 4: Populate it in the gateway**

In `app/gateway/gateway.py`, the `step="router"` `DecisionRecord(...)` (~line 55-62), add `serving_engine=decision.serving_engine`:
```python
    records.append(DecisionRecord(
        step="router", chosen=cand.name,
        chosen_display_name=cand.display_name, reason=decision.reason,
        rejected=decision.rejected, est_cost_usd=decision.est_cost_usd,
        actual_cost_usd=result.cost_usd, latency_ms=result.latency_ms,
        hop_index=len(records),
        quality_score=decision.quality_score,
        quality_source=decision.quality_source,
        serving_engine=decision.serving_engine))
```
Leave the failure-path record (`chosen=None`, ~line 43-47) unchanged — no engine when nothing was served.

- [ ] **Step 5: Run to verify it passes + full suite**

Run: `uv run pytest tests/test_infra_router.py -v && uv run pytest`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add app/models.py app/gateway/gateway.py tests/test_infra_router.py
git commit -m "feat(infra): annotate serving_engine on the routing DecisionRecord

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task A4: Admin infra API + app wiring

**Files:**
- Modify: `app/main.py` (construct roster ~line 100-103; call `ensure_loaded` is done in routes — here just build + inject)
- Modify: `app/api/routes.py` (`await infra_roster.ensure_loaded(db)` before `pipeline.run`)
- Modify: `app/api/admin_routes.py` (new routes + In-models + serializers + validation)
- Test: `tests/test_admin_infra_api.py` (create)

**Interfaces:**
- Consumes: store functions (A1), `InfraRoster` (A2), `require_admin`, `store`, `request.app.state.{db,settings,backend,infra_roster}`.
- Produces routes: `GET /admin/infra`, `PUT /admin/infra`, `GET /admin/infra/models`, `PUT /admin/infra/models/{slug}`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_admin_infra_api.py` (models `tests/test_admin_privacy_api.py`: `client` fixture, `_login`):

```python
_ADMIN = {"email": "admin@routeforge.local", "password": "changeme"}


def _login(client):
    r = client.post("/admin/login", json=_ADMIN)
    assert r.status_code == 200


def test_infra_requires_admin(client):
    assert client.get("/admin/infra").status_code in (401, 403)


def test_get_infra_defaults_and_backend_readback(client):
    _login(client)
    r = client.get("/admin/infra")
    assert r.status_code == 200
    body = r.json()
    # boot serving-layer echoed read-only (conftest sets SP_INFERENCE_BACKEND=vllm)
    assert body["inference_backend"] == "vllm"
    # cluster defaults when no row yet
    assert body["runai_licensed"] is False
    assert body["default_engine"] == "vllm"


def test_put_infra_then_get_roundtrip(client):
    _login(client)
    put = client.put("/admin/infra", json={
        "runai_licensed": True, "runai_project": "runai-routeforge",
        "default_engine": "trtllm"})
    assert put.status_code == 200
    got = client.get("/admin/infra").json()
    assert got["runai_licensed"] is True
    assert got["runai_project"] == "runai-routeforge"
    assert got["default_engine"] == "trtllm"


def test_put_infra_bad_engine_422(client):
    _login(client)
    r = client.put("/admin/infra", json={"default_engine": "sglang"})
    assert r.status_code == 422


def test_get_infra_models_lists_onprem_pool(client):
    _login(client)
    rows = client.get("/admin/infra/models").json()
    names = {r["model_name"] for r in rows}
    # on-prem pool members from routing.yaml appear; cloud models do not
    assert "local-8b" in names
    assert "azure-gpt-mid" not in names
    # unmanaged members are flagged
    assert all("managed" in r and "enabled" in r for r in rows)


def test_put_model_then_get_reflects_and_gates(client):
    _login(client)
    put = client.put("/admin/infra/models/local-8b", json={
        "engine": "trtllm", "enabled": False, "gpu_type": "NVIDIA-H200"})
    assert put.status_code == 200
    rows = {r["model_name"]: r for r in client.get("/admin/infra/models").json()}
    assert rows["local-8b"]["enabled"] is False
    assert rows["local-8b"]["engine"] == "trtllm"
    assert rows["local-8b"]["managed"] is True
    # the roster cache was refreshed on write
    snap = client.app.state.infra_roster.current()
    assert snap is not None and snap.models["local-8b"].enabled is False


def test_put_model_bad_engine_422(client):
    _login(client)
    r = client.put("/admin/infra/models/local-8b", json={"engine": "nope"})
    assert r.status_code == 422


def test_put_unknown_model_404(client):
    _login(client)
    r = client.put("/admin/infra/models/not-a-real-model", json={"enabled": True})
    assert r.status_code == 404
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_admin_infra_api.py -v`
Expected: FAIL — routes 404 / `app.state.infra_roster` missing.

- [ ] **Step 3: Construct + inject the roster in `app/main.py`**

Before the `Router(settings)` line (~line 100-103), build the roster and inject it:
```python
    # Runtime on-prem fleet declaration (spec 2026-08-01): a process-global
    # snapshot of infra_settings/infra_models, lazily loaded on the first
    # request (routes.py) and refreshed on every admin write. Injected into the
    # Router so admin-disabled on-prem models are excluded from candidates.
    from app.apps.infra import InfraRoster
    app.state.infra_roster = InfraRoster(settings)
    model_router = Router(settings, infra_roster=app.state.infra_roster)
    app.state.model_router = model_router
```
(Do not load from the DB here — `create_app` runs before the serving event loop, per the existing comment at lines 74-77. The roster loads lazily in `routes.py`.)

- [ ] **Step 4: Load the roster on the request path in `app/api/routes.py`**

In the main `/v1/chat/completions` handler, alongside the existing `profile_cache` read-through (~line 91-98), before `pipeline.run`, add:
```python
    await request.app.state.infra_roster.ensure_loaded(request.app.state.db)
```
(One line; cheap after first load. The agent hops self-call the internal endpoint in the same process and read the already-loaded global snapshot.)

- [ ] **Step 5: Add the admin routes**

In `app/api/admin_routes.py`, add the In-models near the other `*In` classes (~line 78), a validation helper near `_validate_profile` (~line 112), and the routes near the other `/admin` routes. `select`/pool access uses `request.app.state.settings.routing.model_pool`.

```python
_ENGINES = {"vllm", "trtllm"}


class InfraSettingsIn(BaseModel):
    runai_licensed: bool | None = None
    runai_project: str | None = None
    default_engine: str | None = None


class InfraModelIn(BaseModel):
    engine: str | None = None
    enabled: bool | None = None
    replicas: int | None = None
    min_replicas: int | None = None
    max_replicas: int | None = None
    tensor_parallel: int | None = None
    gpu_type: str | None = None
    endpoint: str | None = None
    runai_scheduled: bool | None = None
    notes: str | None = None


def _infra_model_dict(pool_cand, row, health) -> dict:
    """One on-prem roster entry: routing.yaml identity + infra_models metadata
    (or defaults if unmanaged) + a live-health cross-check."""
    managed = row is not None
    engine = row.engine if managed else None
    enabled = row.enabled if managed else True  # unmanaged -> enabled (§6)
    present = health.ok and pool_cand.name in health.models
    if not health.ok:
        cross = "unreachable"
    elif present:
        cross = "serving"
    else:
        cross = "absent"
    return {
        "model_name": pool_cand.name,
        "display_name": pool_cand.display_name or pool_cand.name,
        "tier": pool_cand.tier,
        "provider": pool_cand.provider,
        "managed": managed,
        "engine": engine,
        "enabled": enabled,
        "replicas": row.replicas if managed else None,
        "min_replicas": row.min_replicas if managed else None,
        "max_replicas": row.max_replicas if managed else None,
        "tensor_parallel": row.tensor_parallel if managed else None,
        "gpu_type": row.gpu_type if managed else None,
        "endpoint": row.endpoint if managed else None,
        "runai_scheduled": row.runai_scheduled if managed else False,
        "notes": row.notes if managed else None,
        "cross_check": cross,  # serving | absent | unreachable
    }


@admin_router.get("/infra")
async def get_infra(request: Request, _: str = Depends(require_admin)) -> dict:
    s = request.app.state.settings
    async with request.app.state.db.session() as session:
        row = await store.get_infra_settings(session)
    return {
        "inference_backend": s.serving.inference_backend,  # read-only boot fact
        "runai_licensed": bool(row.runai_licensed) if row else False,
        "runai_project": row.runai_project if row else None,
        "default_engine": row.default_engine if row else "vllm",
    }


@admin_router.put("/infra")
async def put_infra(body: InfraSettingsIn, request: Request,
                    _: str = Depends(require_admin)) -> dict:
    if body.default_engine is not None and body.default_engine not in _ENGINES:
        raise HTTPException(422, f"bad default_engine {body.default_engine!r}")
    fields = {k: v for k, v in body.model_dump().items() if v is not None}
    async with request.app.state.db.session() as session:
        await store.upsert_infra_settings(session, **fields)
        await session.commit()
    await request.app.state.infra_roster.refresh(request.app.state.db)
    return {"ok": True}


@admin_router.get("/infra/models")
async def get_infra_models(request: Request, _: str = Depends(require_admin)) -> list:
    s = request.app.state.settings
    onprem = [c for c in s.routing.model_pool if c.provider == "onprem"]
    health = await request.app.state.backend.health()
    async with request.app.state.db.session() as session:
        rows = {r.model_name: r for r in await store.list_infra_models(session)}
    return [_infra_model_dict(c, rows.get(c.name), health) for c in onprem]


@admin_router.put("/infra/models/{slug}")
async def put_infra_model(slug: str, body: InfraModelIn, request: Request,
                          _: str = Depends(require_admin)) -> dict:
    if body.engine is not None and body.engine not in _ENGINES:
        raise HTTPException(422, f"bad engine {body.engine!r}")
    s = request.app.state.settings
    onprem_names = {c.name for c in s.routing.model_pool if c.provider == "onprem"}
    if slug not in onprem_names:
        raise HTTPException(404, "not an on-prem model in the pool")
    fields = {k: v for k, v in body.model_dump().items() if v is not None}
    async with request.app.state.db.session() as session:
        await store.upsert_infra_model(session, slug, **fields)
        await session.commit()
    await request.app.state.infra_roster.refresh(request.app.state.db)
    return {"ok": True}
```

Confirm `HTTPException`, `Depends`, `Request`, `BaseModel`, `store` are already imported in `admin_routes.py` (they are — used by existing routes).

- [ ] **Step 6: Run the test to verify it passes**

Run: `uv run pytest tests/test_admin_infra_api.py -v`
Expected: PASS (all tests). Note: conftest sets `SP_SKIP_BACKEND_HEALTHCHECK=1`; `backend.health()` on the vllm backend with an unreachable endpoint returns `ok=False` → `cross_check="unreachable"`, which the tests don't assert on, so they pass regardless.

- [ ] **Step 7: Run the full suite**

Run: `uv run pytest`
Expected: PASS.

- [ ] **Step 8: Commit**

```bash
git add app/main.py app/api/routes.py app/api/admin_routes.py tests/test_admin_infra_api.py
git commit -m "feat(infra): admin /admin/infra + /admin/infra/models API, roster wiring

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task B1: "Switchyard" design foundation (tokens, fonts, palette)

**Files:**
- Modify: `ui/package.json` (add font deps)
- Create: `ui/src/tokens.ts`
- Modify: `ui/tailwind.config.js`, `ui/src/index.css`, `ui/src/main.tsx`, `ui/src/api.ts`

**Interfaces:**
- Produces: `ui/src/tokens.ts` exporting `TIER_HEX: Record<"cheap"|"mid"|"frontier", string>` and `SIGNAL_HEX: { privacy: string; alert: string }`; Tailwind color tokens `privacy`, `alert`, updated `cheap`/`mid`/`frontier`/`ink`/`panel`/`edge`; `fontFamily` `display`/`sans`/`mono`; keyframe `rail-pulse`.

- [ ] **Step 1: Add self-hosted font packages**

Run:
```bash
cd ui && npm install @fontsource/archivo@^5 @fontsource/ibm-plex-sans@^5 @fontsource/ibm-plex-mono@^5
```
Expected: `package.json` `dependencies` gains the three packages; `package-lock.json` updates. (Host npm; do not use `npm ci` — see WSL2 memory.)

- [ ] **Step 2: Import font weights in `ui/src/main.tsx`**

At the top of `ui/src/main.tsx`, add (before the CSS import):
```ts
import "@fontsource/archivo/500.css";
import "@fontsource/archivo/600.css";
import "@fontsource/archivo/700.css";
import "@fontsource/ibm-plex-sans/400.css";
import "@fontsource/ibm-plex-sans/500.css";
import "@fontsource/ibm-plex-sans/600.css";
import "@fontsource/ibm-plex-mono/400.css";
import "@fontsource/ibm-plex-mono/500.css";
```

- [ ] **Step 3: Create the single token source `ui/src/tokens.ts`**

```ts
// Single source of truth for the semantic signal palette. The three tier
// hexes were previously duplicated in api.ts (TIER_COLOR), BrandLogo.tsx and
// RequestDrilldown.tsx — everything now reads from here. Kept in sync by hand
// with the Tailwind tokens in tailwind.config.js (Tailwind classes vs. inline
// SVG/Recharts hex are two consumers of the same palette).
export const TIER_HEX = {
  cheap: "#35D6A0",
  mid: "#4C8DFF",
  frontier: "#F5A524",
} as const;

export const SIGNAL_HEX = {
  privacy: "#8B7BF7", // residency dimension (not a tier)
  alert: "#F2555A", // breaches / health-fail ONLY
} as const;

export type Tier = keyof typeof TIER_HEX;
```

- [ ] **Step 4: Update Tailwind tokens + fonts + keyframes in `ui/tailwind.config.js`**

Replace the `theme.extend` block:
```js
  theme: {
    extend: {
      colors: {
        // Semantic signal palette (mirror of ui/src/tokens.ts). Constant across
        // themes so charts/bars read instantly.
        cheap: "#35D6A0",
        mid: "#4C8DFF",
        frontier: "#F5A524",
        privacy: "#8B7BF7",
        alert: "#F2555A",
        // Structural neutrals resolve through CSS variables (src/index.css).
        ink: "rgb(var(--c-ink) / <alpha-value>)",
        panel: "rgb(var(--c-panel) / <alpha-value>)",
        edge: "rgb(var(--c-edge) / <alpha-value>)",
        accent: "rgb(var(--c-accent) / <alpha-value>)",
        gray: {
          100: "rgb(var(--c-gray-100) / <alpha-value>)",
          200: "rgb(var(--c-gray-200) / <alpha-value>)",
          300: "rgb(var(--c-gray-300) / <alpha-value>)",
          400: "rgb(var(--c-gray-400) / <alpha-value>)",
          500: "rgb(var(--c-gray-500) / <alpha-value>)",
          600: "rgb(var(--c-gray-600) / <alpha-value>)",
          700: "rgb(var(--c-gray-700) / <alpha-value>)",
        },
      },
      fontFamily: {
        display: ['Archivo', 'ui-sans-serif', 'system-ui', 'sans-serif'],
        sans: ['"IBM Plex Sans"', 'ui-sans-serif', 'system-ui', 'sans-serif'],
        mono: ['"IBM Plex Mono"', 'ui-monospace', 'SFMono-Regular', 'monospace'],
      },
      keyframes: {
        "rail-pulse": {
          "0%": { opacity: "0.15" },
          "50%": { opacity: "1" },
          "100%": { opacity: "0.15" },
        },
      },
      animation: {
        "rail-pulse": "rail-pulse 1.2s ease-in-out",
      },
    },
  },
```

- [ ] **Step 5: Update neutrals + base font + reduced-motion in `ui/src/index.css`**

Update the dark neutrals to the cooler control-room values, add a rail-cyan accent, set the body font to IBM Plex Sans, and add a reduced-motion guard. Change these lines in the `:root, :root.dark` block:
```css
  --c-ink: 11 14 20;      /* #0B0E14 cool blue-black */
  --c-panel: 19 26 38;    /* #131A26 */
  --c-edge: 33 44 61;     /* #212C3D */
  --c-accent: 79 209 224; /* #4FD1E0 rail cyan (selection/focus, the "track") */
  --c-gray-100: 231 237 246; /* #E7EDF6 primary text */
```
In the `:root.light` block set `--c-accent: 14 155 176;` (`#0E9BB0`, deeper for white). Leave the rest of the light ramp as-is.

Change the `body` `font-family` to:
```css
  font-family: "IBM Plex Sans", ui-sans-serif, system-ui, -apple-system, "Segoe UI", Roboto, sans-serif;
```

Append at the end of the file:
```css
@media (prefers-reduced-motion: reduce) {
  *, *::before, *::after {
    animation-duration: 0.001ms !important;
    animation-iteration-count: 1 !important;
    transition-duration: 0.001ms !important;
  }
}
```

- [ ] **Step 6: Point `TIER_COLOR` in `ui/src/api.ts` at the token source**

Replace the hardcoded `TIER_COLOR` map with an import-backed one:
```ts
import { TIER_HEX } from "./tokens";
// ...
export const TIER_COLOR: Record<string, string> = { ...TIER_HEX };
```
(Keep the export name/shape identical so existing consumers compile unchanged.)

- [ ] **Step 7: Typecheck + build**

Run: `cd ui && npm run typecheck && npm run build`
Expected: PASS; `ui/dist/index.html` exists.

**Acceptance checklist:**
- Fonts load from `@fontsource` (no `fonts.googleapis.com` in `dist/`).
- `tokens.ts` is the only literal definition of the three tier hexes besides Tailwind config (which mirrors it with a comment).
- Dark bg is the cooler `#0B0E14`.

- [ ] **Step 8: Commit**

```bash
git add ui/package.json ui/package-lock.json ui/src/tokens.ts ui/tailwind.config.js ui/src/index.css ui/src/main.tsx ui/src/api.ts
git commit -m "feat(ui): Switchyard design foundation — palette, self-hosted fonts, token source

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task B2: Rail shell + shared primitives reskin

**Files:**
- Modify: `ui/src/App.tsx` (header band), `ui/src/console/ConsoleShell.tsx` (nav → rail), `ui/src/components/common.tsx` (`Panel`/`StatTile`/`TierDot`), `ui/src/components/BrandLogo.tsx` (mark/wordmark/CdwMark/ThemeToggle), `ui/src/components/HeadroomToggle.tsx`

**Interfaces:**
- Consumes: `tokens.ts`, new Tailwind tokens/fonts (B1).
- Produces: a left-rail `ConsoleShell` layout with "station" nav items and a `RailNav` element; reskinned shared primitives that cascade to every panel.

- [ ] **Step 1: Reskin shared primitives in `common.tsx`**

Update `Panel` to use `font-display` uppercase tracked titles and the new surfaces; `StatTile` values to `font-mono`; keep the exported signatures identical. Concretely:
- `Panel` title: `className="text-xs font-display uppercase tracking-[0.12em] text-gray-400"`.
- `StatTile` value: wrap the value in `font-mono` (all measured quantities are mono per the design language).
- `TierDot`: unchanged logic, but ensure the color comes from the tier Tailwind classes (`bg-cheap`/`bg-mid`/`bg-frontier`) which now resolve to the new hexes.

- [ ] **Step 2: Convert `ConsoleShell.tsx` to the rail**

Extend `View` to `"fleet" | "manage" | "app" | "infra"`. Replace the horizontal nav band with a left rail: a slim vertical column (`w-14 sm:w-48`) of "station" buttons (Overview/Fleet, Applications, Infrastructure) each rendered as a lit node when active (`text-accent` + a `bg-accent` dot; inactive `text-gray-500`), with a thin vertical `bg-edge` "track" line behind the nodes. Content area to the right. Keep the `view==="app"` full-screen short-circuit. Add the `"infra"` branch rendering `<InfrastructurePanel/>` (created in B5 — until then, a placeholder `<Panel title="Infrastructure"/>` so this task builds independently; B5 replaces the body).

ASCII target (matches spec §12):
```
┌──────┬───────────────────────────────┐
│ ▤ RF │  <content for active station> │
│ ●Over│                               │
│ │    │                               │
│ ○Apps│                               │
│ │    │                               │
│ ●Infra                               │
└──────┴───────────────────────────────┘
```

- [ ] **Step 3: Reskin the header in `App.tsx`**

Header uses `font-display` for the wordmark, the connection dot uses `bg-cheap`/`bg-gray-600`, keep `HeadroomToggle`/`ThemeToggle`/sign-out/`CdwMark`. No structural change beyond typography + the new tokens.

- [ ] **Step 4: Update `BrandLogo.tsx` to read `tokens.ts`**

The `RouteForgeMark` SVG tier dots must import `TIER_HEX` from `../tokens` instead of literal hex. Wordmark uses `font-display`. `CdwMark`/`ThemeToggle` restyled to the new edge/accent tokens.

- [ ] **Step 5: Reskin `HeadroomToggle.tsx`**

Pill-switch track uses `bg-cheap` (on) / `bg-gray-600` (off), border `border-edge` → `hover:border-accent`. Ensure visible focus ring (`focus-visible:ring-2 focus-visible:ring-accent`).

- [ ] **Step 6: Typecheck + build**

Run: `cd ui && npm run typecheck && npm run build`
Expected: PASS.

**Acceptance checklist:**
- Left rail renders "station" nav with a lit active node; keyboard-focusable with a visible ring.
- No literal tier hex remains in `BrandLogo.tsx` (imports `TIER_HEX`).
- Header wordmark uses Archivo (`font-display`).

- [ ] **Step 7: Commit**

```bash
git add ui/src/App.tsx ui/src/console/ConsoleShell.tsx ui/src/components/common.tsx ui/src/components/BrandLogo.tsx ui/src/components/HeadroomToggle.tsx
git commit -m "feat(ui): rail shell + shared-primitive reskin (Switchyard)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task B3: The junction signature (decision inspector)

**Files:**
- Modify: `ui/src/console/RequestDrilldown.tsx`

**Interfaces:**
- Consumes: `tokens.ts` (`TIER_HEX`, `SIGNAL_HEX`), the `DecisionRecord`/trace types in `api.ts`, new `serving_engine` field.
- Produces: a "junction" rendering of the routing step — chosen path lit in its tier color, rejected candidates as dimmed spurs each labelled with its reason; the chosen model shows "served by: <engine>".

- [ ] **Step 1: Consolidate the verdict hexes**

Replace the local `CLEAR/SHIELD/ALARM/NEUTRAL` literal hexes with imports: tier colors from `TIER_HEX`, `SHIELD`/privacy from `SIGNAL_HEX.privacy`, `ALARM` from `SIGNAL_HEX.alert`, `NEUTRAL` from a gray. No literal `#` tier/alert hex remains in this file.

- [ ] **Step 2: Render the router step as a junction**

In the router step's `StepBody`/`Chosen` rendering, present the chosen candidate as a lit node (tier-colored left border + a glowing dot) and each `rejected` candidate as a dimmed row (`opacity-60`, `bg-ink/40`) with its `reason` shown inline as a right-aligned label (e.g. `excluded · residency`, `above target tier`, `disabled by admin`, `lower score`). Map long reasons to compact labels where helpful but keep the raw reason visible.

- [ ] **Step 3: Show `serving_engine`**

Where the chosen model is displayed for the router step, if the record's `serving_engine` is set, render a small mono chip `served by · <engine>` (e.g. `served by · vLLM`). If null, render nothing. Add `serving_engine?: string | null` to the trace/`DecisionRecord` TS type in `api.ts` if not already present.

- [ ] **Step 4: Typecheck + build**

Run: `cd ui && npm run typecheck && npm run build`
Expected: PASS.

**Acceptance checklist:**
- The router step visibly distinguishes the chosen path (lit, tier-colored) from rejected spurs (dimmed + reason).
- "disabled by admin" reason (from Task A2) renders on a disabled on-prem candidate.
- `serving_engine` chip appears for on-prem choices, absent for cloud.
- Reduced-motion: no essential info conveyed by animation alone.

- [ ] **Step 5: Commit**

```bash
git add ui/src/console/RequestDrilldown.tsx ui/src/api.ts
git commit -m "feat(ui): junction decision inspector — lit chosen path, dimmed reasoned spurs

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task B4: Reskin remaining console/admin surfaces

**Files:**
- Modify: `ui/src/console/FleetOverview.tsx`, `ui/src/console/AppPage.tsx`, `ui/src/admin/ApplicationsPanel.tsx`, `ui/src/admin/PrivacyRulesEditor.tsx`, `ui/src/components/RequestList.tsx`

**Interfaces:**
- Consumes: reskinned `common.tsx` primitives, new tokens/fonts, `tokens.ts`.
- Produces: the same components, restyled to the Switchyard language (mono for all numbers/ids, `font-display` section headers, new tier/privacy/alert semantics, the amber downgrade banner recolored to `alert`-adjacent only where it is a genuine warning).

- [ ] **Step 1: FleetOverview**

Numbers (`StatTile` values, spend/latency cells) → `font-mono`. Table header row → `font-display uppercase tracking` small caps. The residency-breach warning icon → `text-alert` (reserved red). Row hover/active uses `border-accent`. Keep `TierMiniBar`/`TierDot` (now new hexes).

- [ ] **Step 2: AppPage tab bar**

Restyle tabs to the rail/station idiom used in `ConsoleShell` (active = `text-accent` + underline/dot), `font-display` labels. Per-app id/residency shown mono.

- [ ] **Step 3: ApplicationsPanel + PrivacyRulesEditor**

Reskin the `chip()` helper (`border-accent text-accent bg-accent/10` on / `border-edge text-gray-500` off) — keep the pattern, ensure focus-visible rings. Inputs/selects use the new `bg-ink border-edge` tokens. The amber downgrade-warning banner in `PrivacyRulesEditor`: recolor its inline `AMBER` to read from `SIGNAL_HEX` — use `frontier` (amber) for a *caution* framing, reserving `alert` red for hard breaches; import from `../tokens` (remove the literal `#f59e0b`).

- [ ] **Step 4: RequestList**

Selected row `border-cheap bg-cheap/10` → keep but with the new cheap hex; badges (`on-prem`, `N hops`) use `bg-mid/20 text-mid` / `bg-gray-700`. Cost/latency mono.

- [ ] **Step 5: Typecheck + build**

Run: `cd ui && npm run typecheck && npm run build`
Expected: PASS.

**Acceptance checklist:**
- All measured values (cost, latency, tokens, ids) render in `font-mono`.
- No literal tier/amber hex remains in these files (import from `tokens.ts`).
- `alert` red appears only on genuine breach/warning states.

- [ ] **Step 6: Commit**

```bash
git add ui/src/console/FleetOverview.tsx ui/src/console/AppPage.tsx ui/src/admin/ApplicationsPanel.tsx ui/src/admin/PrivacyRulesEditor.tsx ui/src/components/RequestList.tsx
git commit -m "feat(ui): reskin fleet/app/admin surfaces to Switchyard tokens

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task B5: Infrastructure section

**Files:**
- Create: `ui/src/admin/InfrastructurePanel.tsx`
- Modify: `ui/src/api.ts` (add `infraApi`), `ui/src/console/ConsoleShell.tsx` (render the real panel in the `"infra"` branch)

**Interfaces:**
- Consumes: `GET/PUT /admin/infra`, `GET /admin/infra/models`, `PUT /admin/infra/models/{slug}` (Task A4); `common.tsx` primitives; `tokens.ts`.
- Produces: `infraApi` client object; `InfrastructurePanel` React component.

- [ ] **Step 1: Add the `infraApi` client in `api.ts`**

Following the existing `adminApi` shape (`opts(method, body)`, `credentials: "same-origin"`, `j<T>`), add types and object:
```ts
export type InfraSettings = {
  inference_backend: string;
  runai_licensed: boolean;
  runai_project: string | null;
  default_engine: string;
};

export type InfraModel = {
  model_name: string;
  display_name: string;
  tier: string;
  provider: string;
  managed: boolean;
  engine: string | null;
  enabled: boolean;
  replicas: number | null;
  min_replicas: number | null;
  max_replicas: number | null;
  tensor_parallel: number | null;
  gpu_type: string | null;
  endpoint: string | null;
  runai_scheduled: boolean;
  notes: string | null;
  cross_check: "serving" | "absent" | "unreachable";
};

export const infraApi = {
  get: () => fetch("/admin/infra", { credentials: "same-origin" }).then(j<InfraSettings>),
  put: (body: Partial<Omit<InfraSettings, "inference_backend">>) =>
    fetch("/admin/infra", opts("PUT", body)).then(j<{ ok: boolean }>),
  models: () =>
    fetch("/admin/infra/models", { credentials: "same-origin" }).then(j<InfraModel[]>),
  putModel: (slug: string, body: Partial<InfraModel>) =>
    fetch(`/admin/infra/models/${encodeURIComponent(slug)}`, opts("PUT", body))
      .then(j<{ ok: boolean }>),
};
```

- [ ] **Step 2: Build `InfrastructurePanel.tsx`**

A component with two parts, in the Switchyard language:
- **Cluster card** (`<Panel title="Cluster">`): read-only serving-layer readout (`inference_backend`, in mono), a Run:ai-licensed toggle, a Run:ai project text field, a default-engine select (`vLLM`/`TensorRT-LLM`). Save via `infraApi.put`.
- **On-prem fleet table** (`<Panel title="On-prem fleet">`): one row per `infraApi.models()` entry. Columns: model (display_name + slug mono), tier (`TierDot`), engine dropdown (`vllm`/`trtllm`), enabled toggle (**uses the dim-node language — a disabled row dims like an excluded candidate in the junction**, per spec §12), replicas/TP/GPU (mono, editable), and a cross-check badge: `serving` → `text-cheap ● match`, `absent` → `text-frontier ▲ not serving`, `unreachable` → `text-alert ● unreachable`. Unmanaged rows (`managed=false`) show an "unmanaged / defaulted" tag. Per-row Save via `infraApi.putModel`.

Empty/normal states: if the on-prem pool is empty (backend `none`), show a clear message ("No on-prem models in the pool"). Numbers mono; section headers `font-display`.

- [ ] **Step 3: Wire it into `ConsoleShell.tsx`**

Replace the B2 placeholder in the `view === "infra"` branch with `<InfrastructurePanel/>`.

- [ ] **Step 4: Typecheck + build**

Run: `cd ui && npm run typecheck && npm run build`
Expected: PASS.

**Acceptance checklist:**
- Toggling a model's "enabled" off and saving persists (visible after reload) — verified manually against a running API, or at least typechecks + builds and calls the correct endpoints.
- A disabled model row uses the same dimmed visual as an excluded candidate (learned-once metaphor).
- Cloud models do not appear (on-prem only).
- Cross-check badge reflects `serving`/`absent`/`unreachable`; `alert` red only on `unreachable`.

- [ ] **Step 5: Commit**

```bash
git add ui/src/admin/InfrastructurePanel.tsx ui/src/api.ts ui/src/console/ConsoleShell.tsx
git commit -m "feat(ui): Infrastructure admin section — cluster card + on-prem fleet table

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task C1: Promote docs/dynamo.txt into the inference-backends skill

**Files:**
- Modify: `.claude/skills/inference-backends/SKILL.md`
- Delete: `docs/dynamo.txt` (replace with nothing, or a one-line pointer — see step)

**Interfaces:** none (docs only).

- [ ] **Step 1: Read the current skill**

Read `.claude/skills/inference-backends/SKILL.md` to find where to insert an "Ownership boundary" section (near the top, after the backend-axis description).

- [ ] **Step 2: Add the ownership-boundary section**

Insert a section titled "## Ownership boundary (routeForge vs Dynamo vs engine vs Run:ai)" containing the responsibility table from `docs/dynamo.txt` (lines 7-17), the "What Dynamo already provides / You still need to configure / What Dynamo does not replace" lists (lines 18-64), and a one-line note that the per-model `engine` attribute and the cluster `runai_licensed` fact are *consumed* facts (surfaced by the infra-management config, spec 2026-08-01), NOT new serving-path axes — the single `inference_backend` axis (`vllm | dynamo | none`) still stands. Do not restate the `dynamo/vllm/none` mechanics already in the skill.

- [ ] **Step 3: Remove the loose doc**

Delete `docs/dynamo.txt` (its content now lives in the skill, the single source). `docs/dynamo.txt` is NOT a protected untracked file — it is tracked; remove it with `git rm`.

- [ ] **Step 4: Commit**

```bash
git rm docs/dynamo.txt
git add .claude/skills/inference-backends/SKILL.md
git commit -m "docs(skill): promote Dynamo ownership boundary into inference-backends skill

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Self-Review

**Spec coverage** (against `2026-08-01-infra-management-config-design.md`):
- §5 tables + one-owner reconciliation → A1 (tables), A4 (join uses routing.yaml pool, never restates tier/quality/cost). ✅
- §6 gate + annotate; unmanaged → enabled; onprem_only + all-disabled → REJECT; fail-safe → A2 (gate, permissive cold cache, reject test) + A3 (annotate). ✅
- §7 API + Infrastructure section → A4 (four routes) + B5 (panel). ✅
- §8 Run:ai display/defaulting only → A4 (`runai_licensed`/`runai_project`/`default_engine` echoed, no scheduling) + B5 (cluster card). ✅
- §9 docs/dynamo.txt disposition → C1. ✅
- §10 hard rules → Global Constraints + A2 reject test + A4 no-deploy (describe/consume only). ✅
- §11 tests → A1 (store round-trip), A2 (gating, reject, regression), A3 (annotation), A4 (API validate/roundtrip/cache-refresh/cross-check present). ✅
- §12 UI design language → B1 (tokens/fonts/palette), B2 (rail shell), B3 (junction signature), B4 (reskin), B5 (Infra section to the language). ✅

**Placeholder scan:** UI tasks intentionally verify via typecheck/build + acceptance checklists (no JS test runner in-repo — documented in Global Constraints). B2 uses a deliberate temporary placeholder in the `"infra"` branch, replaced in B5 (called out explicitly). No "TBD"/"handle edge cases"/"similar to" left.

**Type consistency:** `InfraRoster` methods (`ensure_loaded`/`refresh`/`current`/`is_enabled`/`engine_for`) consistent across A2/A3/A4; `serving_engine` name identical on `RoutingDecision` (A2) and `DecisionRecord` (A3) and the gateway copy; `infraApi` field names match the `_infra_model_dict`/`get_infra` JSON keys (A4 ↔ B5).

**Cross-check limitation noted:** the live `/models` probe returns model ids, not engine type, so the cross-check badge is presence/health-based (`serving`/`absent`/`unreachable`), not a declared-engine-vs-actual-engine comparison. Engine-level mismatch detection is future work (documented in A4 step 6 note and B5).
