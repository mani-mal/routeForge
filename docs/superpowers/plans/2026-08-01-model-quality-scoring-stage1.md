# Model Quality Scoring — Stage 1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give routeForge a per-model quality score with transparent provenance (customer > calibrated > published > tier_default), wire it into the router in place of the hard-coded tier constant, and unlock the Token/Cost Intelligence Density dashboard metrics.

**Architecture:** A pure `QualityResolver` (settings + an injectable DB-override snapshot) returns `(score, source)` for any model, always succeeding and never blocking a custom model. The `Router` builds a default resolver from `Settings` and consults it inside `score()`. The resolved score + source flow onto the `RoutingDecision` → `DecisionRecord` → pipeline response → `HistoryStore`, where two ratios (TID, CID) are computed. A new `model_quality` table is created (empty in Stage 1) so Stage 2 calibration can write to it; the resolver already reads it via the override snapshot.

**Tech Stack:** Python 3.12, FastAPI, Pydantic v2, SQLAlchemy async (aiosqlite/Postgres), `uv` for deps, pytest. Frontend: React + Vite + TypeScript + Recharts.

**Spec:** `docs/superpowers/specs/2026-08-01-model-quality-scoring-design.md` (Stages 1–2; this plan is Stage 1 only). Stage 2 (calibration harness, customer-declared endpoint, live DB→router refresh, CLAUDE.md §10 amendment) is a **separate follow-up plan**.

## Global Constraints

- **One owner per concern.** Quality is a *routing* input → it lives in `config/routing.yaml` only, **never** `config/pricing.yaml` (pricing owns USD). The config lint enforces no duplication.
- **`QualitySource` values are exactly** `customer | calibrated | published | tier_default` — verbatim strings, no fifth spelling. Defined once as `QUALITY_SOURCES` in `app/routing/quality.py`.
- **Canonical tiers are exactly** `cheap | mid | frontier`. There is no `strong` tier.
- **The resolver never raises and never returns empty.** An unknown model with no tier mapping falls back to a neutral `tier_default` floor (0.5). A custom model is never blocked from routing.
- **Scalar quality per model now.** `resolve()` accepts `task_class` for forward-compatibility but ignores it — no domain classifier is built (YAGNI).
- **Router stays unit-testable without a DB.** The resolver is injected as an optional keyword arg; when omitted the router builds a settings-only resolver (empty overrides). All existing `Router(settings)` call sites keep working unchanged.
- **`model_quality` is a NEW table** → `Base.metadata.create_all` handles it, no migration. But the **new COLUMNS on `usage_records`** (Task 5) need a manual `ALTER TABLE` on any persistent deployed DB (DGX uses the shared Langfuse Postgres — see `routeforge-schema-no-migrations`, `routeforge-dgx-deploy`). Fresh SQLite/test DBs are created by `create_all` and need no ALTER.
- **The shipped `config/routing.yaml` must remain a behavioral regression.** `tier_quality` is seeded with the exact old `_Q` values and no model_pool entry gets a published `quality`, so the tier-default path reproduces today's routing exactly. Populating published values is an onboarding activity (spec §13), exercised in unit tests here with synthetic values.
- **`app/config/lint.py` must lint clean against the shipped YAMLs.** `tests/test_config_lint.py::test_current_config_is_clean` must stay green.
- **Honesty/privacy (unchanged):** quality provenance is metadata, never PII; `DecisionRecord.detected_entities` stays types-only; nothing in this plan exports raw request content.
- **Tooling:** run everything through `uv run` (never bare `uv sync`). Tests are offline (conftest monkeypatches the backend). Stage git by explicit path — never `git add -A`.
- **Commit trailer (every commit):**
  ```
  Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
  ```

---

### Task 1: Config schema, `routing.yaml` values, and quality lint rules

**Files:**
- Modify: `app/config/schema.py` (`PoolCandidate` ~lines 4-15, `RoutingCfg` ~lines 18-35)
- Modify: `config/routing.yaml` (add `tier_quality`, ~after line 13)
- Modify: `app/config/lint.py` (`lint_settings`, add rules ~after line 36)
- Test: `tests/test_config_loader.py` (new test fn), `tests/test_config_lint.py` (new test fns)

**Interfaces:**
- Produces: `RoutingCfg.tier_quality: dict[str, float]` (required field); `PoolCandidate.quality: float | None = None`.
- Produces lint violations: `TIER_QUALITY_MISSING <tier>`, `TIER_QUALITY_OUT_OF_RANGE <tier>=<v>`, `POOL_QUALITY_OUT_OF_RANGE <name>=<v>`.
- Consumed by: Task 3 (`QualityResolver` reads both), Task 4 (router).

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_config_loader.py`:
```python
def test_routing_config_exposes_quality_fields():
    from app.config.loader import load_settings
    s = load_settings()
    # tier_quality covers every canonical tier, promoted from the old hardcoded _Q
    assert set(s.routing.tier_quality) == set(s.routing.tiers)
    assert s.routing.tier_quality["cheap"] == 0.5
    assert s.routing.tier_quality["frontier"] == 1.0
    # per-model published quality is optional; shipped config leaves it unset
    # (tier-default regression — populating it is an onboarding step)
    assert all(c.quality is None for c in s.routing.model_pool)
```

Add to `tests/test_config_lint.py`:
```python
import copy
import pytest
from app.config.loader import load_settings
from app.config.lint import lint_settings, ConfigLintError, enforce


def test_tier_quality_missing_tier_flags():
    s = copy.deepcopy(load_settings())
    s.routing.tier_quality.pop("frontier")
    assert any("TIER_QUALITY_MISSING" in e for e in lint_settings(s))


def test_tier_quality_out_of_range_flags():
    s = copy.deepcopy(load_settings())
    s.routing.tier_quality["mid"] = 1.5
    assert any("TIER_QUALITY_OUT_OF_RANGE" in e for e in lint_settings(s))


def test_pool_quality_out_of_range_flags():
    s = copy.deepcopy(load_settings())
    s.routing.model_pool[0].quality = 2.0
    assert any("POOL_QUALITY_OUT_OF_RANGE" in e for e in lint_settings(s))
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_config_loader.py::test_routing_config_exposes_quality_fields tests/test_config_lint.py -k "quality" -v`
Expected: FAIL — `tier_quality` is not a field yet (`ValidationError` or `AttributeError`).

- [ ] **Step 3: Add the schema fields**

In `app/config/schema.py`, add to `PoolCandidate` (after `litellm_model`, line 15):
```python
    # Published quality snapshot in [0,1] (e.g. normalized Artificial Analysis
    # Intelligence Index / 100). Optional: unset means the resolver falls to the
    # tier_default rung. Populating this is an onboarding activity (spec §9/§13);
    # the shipped demo leaves it unset so routing stays a tier-default regression.
    quality: float | None = None
```

In `app/config/schema.py`, add to `RoutingCfg` (e.g. after `weights`, line 21):
```python
    # Tier-default rung of the quality ladder — the fallback quality for any
    # model with no published/calibrated/customer score. Promoted from the old
    # hardcoded router `_Q`. One value per canonical tier (config lint enforces
    # full coverage and the [0,1] range). Owned here (routing input), never in
    # pricing.yaml.
    tier_quality: dict[str, float]
```

- [ ] **Step 4: Add the values to `config/routing.yaml`**

Insert after the `weights:` line (line 13):
```yaml

# Tier-default quality (the last rung of the quality ladder in app/routing/quality.py).
# Promoted from the router's old hardcoded _Q so it is single-sourced and lintable.
# There is NO per-model published `quality` on the model_pool below yet — the demo
# stays a tier-default regression. To add a published snapshot at onboarding, put e.g.
#   - {name: azure-gpt-mid, ..., quality: 0.70}   # AA Intelligence Index / 100, dated
# on a model_pool entry (see spec §9). Values must be in [0,1] (config lint).
tier_quality: {cheap: 0.5, mid: 0.75, frontier: 1.0}
```

- [ ] **Step 5: Add the lint rules**

In `app/config/lint.py`, inside `lint_settings`, add after the pricing-tier loop (after line 36, `tiers` is already in scope):
```python
    for t in tiers:
        if t not in s.routing.tier_quality:
            errs.append(f"TIER_QUALITY_MISSING {t}")
    for t, q in s.routing.tier_quality.items():
        if not (0.0 <= q <= 1.0):
            errs.append(f"TIER_QUALITY_OUT_OF_RANGE {t}={q}")
    for c in s.routing.model_pool:
        if c.quality is not None and not (0.0 <= c.quality <= 1.0):
            errs.append(f"POOL_QUALITY_OUT_OF_RANGE {c.name}={c.quality}")
```

- [ ] **Step 6: Run the tests to verify they pass, and the whole config suite is clean**

Run: `uv run pytest tests/test_config_loader.py tests/test_config_lint.py -v`
Expected: PASS, including `test_current_config_is_clean`.

- [ ] **Step 7: Commit**

```bash
git add app/config/schema.py config/routing.yaml app/config/lint.py tests/test_config_loader.py tests/test_config_lint.py
git commit -m "feat(config): tier_quality + per-model quality fields with lint rules

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 2: `model_quality` DB table + store accessors

**Files:**
- Modify: `app/apps/models.py` (add `ModelQuality`, after `AdminUser`)
- Modify: `app/apps/store.py` (add `upsert_model_quality`, `load_quality_overrides`)
- Test: `tests/test_model_quality_store.py` (new)

**Interfaces:**
- Produces: `ModelQuality` table `model_quality` (new table → `create_all`, no migration).
- Produces: `async upsert_model_quality(session, *, model_name, task_class=None, score, source, eval_run_id=None, notes=None) -> ModelQuality` (caller commits).
- Produces: `async load_quality_overrides(session) -> dict[str, tuple[float, str]]` — one winning `(score, source)` per model, **customer beating calibrated**. Consumed by Stage 2 to build the router's override snapshot; unit-tested here.

- [ ] **Step 1: Write the failing test**

Create `tests/test_model_quality_store.py`:
```python
import pytest
from app.apps.db import Database
from app.apps import store


@pytest.mark.asyncio
async def test_upsert_and_load_quality_overrides():
    db = Database(url="sqlite+aiosqlite:///:memory:")
    async with db.session() as s:
        await store.upsert_model_quality(
            s, model_name="local-8b", score=0.62, source="calibrated",
            eval_run_id="run-1", notes="seed")
        await store.upsert_model_quality(
            s, model_name="local-8b", score=0.80, source="customer")
        await s.commit()

    async with db.session() as s:
        overrides = await store.load_quality_overrides(s)
    # customer beats calibrated for the same model
    assert overrides["local-8b"] == (0.80, "customer")


@pytest.mark.asyncio
async def test_load_quality_overrides_empty_when_no_rows():
    db = Database(url="sqlite+aiosqlite:///:memory:")
    async with db.session() as s:
        overrides = await store.load_quality_overrides(s)
    assert overrides == {}
```
> Note to implementer: use the DB construction/fixture style already in `tests/test_apps_store.py` (or the nearest existing store test) — `Database(url="sqlite+aiosqlite:///:memory:")` with StaticPool keeps one connection so the table created by `ensure_ready` survives across `session()` blocks. If that file uses a shared `db` fixture, prefer it. Confirm the `Database` constructor's `url=` kwarg name against `app/apps/db.py`.

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/test_model_quality_store.py -v`
Expected: FAIL — `ModelQuality` / `upsert_model_quality` do not exist.

- [ ] **Step 3: Add the ORM model**

In `app/apps/models.py`, after the `AdminUser` class, add (mirrors `UsageRecord`'s column style; `utcnow` already imported in the module):
```python
class ModelQuality(Base):
    """Per-model quality score with provenance, for the routing quality ladder.
    Written by the Stage 2 calibration harness (source=calibrated) or a
    customer-declared override (source=customer). A NEW table, so create_all
    handles it — no migration. This is model metadata, not a promotion/registry
    system (spec §5/§10). Not a copy of any request content — no PII."""

    __tablename__ = "model_quality"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    model_name: Mapped[str] = mapped_column(String(100), index=True)
    # Reserved for quality_by_class (Stage 3+); NULL = the scalar quality today.
    task_class: Mapped[str | None] = mapped_column(String(64), nullable=True)
    score: Mapped[float] = mapped_column(Float)
    source: Mapped[str] = mapped_column(String(20))  # customer | calibrated
    calibrated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow)
    eval_run_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    notes: Mapped[str | None] = mapped_column(String(255), nullable=True)
```
(If `Float`/`DateTime`/`dt` are not already imported at the top of `models.py`, they are — `UsageRecord` uses all three. Confirm the import line includes them.)

- [ ] **Step 4: Add the store accessors**

In `app/apps/store.py`, add `ModelQuality` to the model import block (lines 15-18), then append these functions:
```python
async def upsert_model_quality(
    session: AsyncSession,
    *,
    model_name: str,
    task_class: str | None = None,
    score: float,
    source: str,
    eval_run_id: str | None = None,
    notes: str | None = None,
) -> ModelQuality:
    """Insert-or-replace the (model_name, task_class, source) score (caller commits)."""
    stmt = select(ModelQuality).where(
        ModelQuality.model_name == model_name,
        ModelQuality.task_class.is_(task_class) if task_class is None
        else ModelQuality.task_class == task_class,
        ModelQuality.source == source,
    )
    row = (await session.execute(stmt)).scalar_one_or_none()
    if row is None:
        row = ModelQuality(id=uuid.uuid4().hex, model_name=model_name,
                           task_class=task_class, source=source, score=score)
        session.add(row)
    row.score = score
    row.calibrated_at = utcnow()
    row.eval_run_id = eval_run_id
    row.notes = notes
    return row


# Provenance precedence for load_quality_overrides: customer wins over calibrated.
_QUALITY_SOURCE_RANK = {"customer": 2, "calibrated": 1}


async def load_quality_overrides(session: AsyncSession) -> dict[str, tuple[float, str]]:
    """One winning (score, source) per model_name across the scalar (task_class
    IS NULL) rows, customer beating calibrated. Feeds the router's override
    snapshot; empty until the Stage 2 harness writes rows."""
    stmt = select(ModelQuality).where(ModelQuality.task_class.is_(None))
    rows = (await session.execute(stmt)).scalars().all()
    best: dict[str, tuple[int, float, str]] = {}
    for r in rows:
        rank = _QUALITY_SOURCE_RANK.get(r.source, 0)
        cur = best.get(r.model_name)
        if cur is None or rank > cur[0]:
            best[r.model_name] = (rank, r.score, r.source)
    return {name: (score, source) for name, (_r, score, source) in best.items()}
```
Also import `utcnow` — it is already imported at line 17.

- [ ] **Step 5: Run the test to verify it passes**

Run: `uv run pytest tests/test_model_quality_store.py -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add app/apps/models.py app/apps/store.py tests/test_model_quality_store.py
git commit -m "feat(db): model_quality table + quality-override accessors

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 3: The quality resolver

**Files:**
- Create: `app/routing/quality.py`
- Test: `tests/test_quality_resolver.py` (new)

**Interfaces:**
- Consumes: `Settings` (`routing.tier_quality`, `routing.model_pool[].quality`, `[].tier`); an optional `overrides: dict[str, QualityScore]` snapshot (built from `store.load_quality_overrides` in Stage 2 — customer/calibrated already resolved to one entry per model).
- Produces: `QUALITY_SOURCES` tuple; `QualityScore(score: float, source: str)` (frozen dataclass); `QualityResolver(settings, overrides=None)` with `.resolve(model, task_class=None) -> QualityScore`.
- Consumed by: Task 4 (router), Task 4/5 (provenance on DecisionRecord).

- [ ] **Step 1: Write the failing tests**

Create `tests/test_quality_resolver.py`:
```python
from app.config.loader import load_settings
from app.routing.quality import QualityResolver, QualityScore, QUALITY_SOURCES


def _settings():
    return load_settings()


def test_tier_default_rung_for_model_without_published_quality():
    s = _settings()  # shipped config: no per-model quality set
    r = QualityResolver(s)
    q = r.resolve("local-8b")  # cheap tier
    assert q == QualityScore(0.5, "tier_default")


def test_published_rung_when_pool_entry_has_quality():
    s = _settings()
    s.routing.model_pool[0].quality = 0.61  # local-8b published
    r = QualityResolver(s)
    q = r.resolve("local-8b")
    assert q == QualityScore(0.61, "published")


def test_override_rung_beats_published_and_tier_default():
    s = _settings()
    s.routing.model_pool[0].quality = 0.61
    r = QualityResolver(s, overrides={"local-8b": QualityScore(0.9, "customer")})
    assert r.resolve("local-8b") == QualityScore(0.9, "customer")


def test_calibrated_override_rung():
    s = _settings()
    r = QualityResolver(s, overrides={"local-8b": QualityScore(0.72, "calibrated")})
    assert r.resolve("local-8b") == QualityScore(0.72, "calibrated")


def test_unknown_model_never_raises_returns_tier_default_floor():
    s = _settings()
    r = QualityResolver(s)
    q = r.resolve("some-custom-model-not-in-pool")
    assert q.source == "tier_default"
    assert 0.0 <= q.score <= 1.0


def test_task_class_is_accepted_and_ignored():
    s = _settings()
    r = QualityResolver(s)
    assert r.resolve("local-8b", task_class="coding") == r.resolve("local-8b")


def test_quality_sources_are_the_canonical_four():
    assert QUALITY_SOURCES == ("customer", "calibrated", "published", "tier_default")
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_quality_resolver.py -v`
Expected: FAIL — module `app.routing.quality` does not exist.

- [ ] **Step 3: Write the resolver**

Create `app/routing/quality.py`:
```python
from __future__ import annotations

from dataclasses import dataclass

from app.config.schema import Settings

# Provenance of a resolved quality score, most authoritative first. These four
# strings are the ONLY valid sources (spec §4) — used verbatim on DecisionRecord
# and in the UI. Precedence between `customer` and `calibrated` is applied when
# BUILDING the overrides snapshot (store.load_quality_overrides), so the resolver
# sees one winning override per model.
QUALITY_SOURCES = ("customer", "calibrated", "published", "tier_default")

_TIER_DEFAULT_FLOOR = 0.5  # neutral last resort; == the historical cheap-tier _Q


@dataclass(frozen=True)
class QualityScore:
    score: float
    source: str  # one of QUALITY_SOURCES


class QualityResolver:
    """Resolves a per-model quality score via a fixed provenance ladder:
    customer > calibrated > published > tier_default. Always succeeds
    (tier_default covers every tier), never raises for an unknown model, never
    blocks a model from routing.

    `overrides` carries DB-sourced scores (Stage 2 calibration / customer
    declarations), keyed by model name -> QualityScore whose `source` is already
    `customer` or `calibrated`. It is a plain dict snapshot so the router stays
    synchronous and DB-free in unit tests; the caller refreshes it. Empty in
    Stage 1 (no calibration written yet)."""

    def __init__(self, settings: Settings,
                 overrides: dict[str, QualityScore] | None = None) -> None:
        self._tier_quality = dict(settings.routing.tier_quality)
        self._tier_of = {c.name: c.tier for c in settings.routing.model_pool}
        self._published = {c.name: c.quality
                           for c in settings.routing.model_pool
                           if c.quality is not None}
        self._overrides = overrides or {}

    def resolve(self, model: str, task_class: str | None = None) -> QualityScore:
        # task_class is accepted for forward-compat (quality_by_class, Stage 3+)
        # and ignored while a single scalar quality per model ships (YAGNI).
        ov = self._overrides.get(model)
        if ov is not None:
            return ov
        pub = self._published.get(model)
        if pub is not None:
            return QualityScore(pub, "published")
        tier = self._tier_of.get(model)
        if tier is not None and tier in self._tier_quality:
            return QualityScore(self._tier_quality[tier], "tier_default")
        # Unknown model with no tier mapping: neutral floor so the resolver never
        # raises and never blocks a custom model.
        return QualityScore(_TIER_DEFAULT_FLOOR, "tier_default")
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_quality_resolver.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/routing/quality.py tests/test_quality_resolver.py
git commit -m "feat(routing): quality resolver ladder (customer>calibrated>published>tier_default)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 4: Router integration — resolver in `score()`, quality on the decision

**Files:**
- Modify: `app/routing/router.py` (`RoutingDecision`, `Router.__init__`, `select`, remove `_Q`)
- Modify: `app/models.py` (`DecisionRecord`, add two fields)
- Modify: `app/gateway/gateway.py` (the `step="router"` record, lines 55-60)
- Modify: `app/agent/internal_endpoint.py` (the `step="agent_hop"` record, lines 162-168)
- Test: `tests/test_router.py` (add cases), `tests/test_router_cheap_tier_regression.py` (verify still green)

**Interfaces:**
- Consumes: `QualityResolver` (Task 3).
- Produces: `RoutingDecision.quality_score: float`, `RoutingDecision.quality_source: str`; `Router.__init__(self, settings, quality_resolver=None)`; `DecisionRecord.quality_score: float | None`, `DecisionRecord.quality_source: str | None`.
- Consumed by: Task 5 (metrics read `DecisionRecord.quality_*`).

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_router.py` (it already builds `R = Router(S)` and a ctx — reuse the module's existing `S`/ctx fixtures; adapt names to what the file uses):
```python
def test_higher_quality_wins_within_tier_when_cost_latency_equal():
    from app.config.loader import load_settings
    from app.routing.quality import QualityResolver, QualityScore
    from app.routing.router import Router
    s = load_settings()
    # Two mid-tier models: local-32b (onprem) and azure-gpt-mid. Give azure a
    # much higher override so quality dominates the (cheaper-onprem) tie-break.
    r = Router(s, quality_resolver=QualityResolver(
        s, overrides={"azure-gpt-mid": QualityScore(1.0, "customer"),
                      "local-32b": QualityScore(0.1, "customer")}))
    # ctx must allow azure + onprem and land on the mid tier; build it the same
    # way the existing tests in this file build ctx (allowed_providers includes
    # onprem and azure, risk medium). Assert azure-gpt-mid is chosen.
    ctx = _mid_tier_ctx_allowing_cloud()  # helper: see existing test patterns
    decision = r.select(ctx, tokens_after=200, complexity=0.5)
    assert decision.chosen.name == "azure-gpt-mid"
    assert decision.quality_source == "customer"
    assert decision.quality_score == 1.0


def test_default_resolver_reproduces_tier_default_quality():
    from app.config.loader import load_settings
    from app.routing.router import Router
    s = load_settings()
    r = Router(s)  # no resolver injected -> settings-only, empty overrides
    ctx = _cheap_ctx()  # existing helper style; onprem-only or clean
    decision = r.select(ctx, tokens_after=50, complexity=0.1)
    assert decision.quality_source == "tier_default"
    assert decision.quality_score == s.routing.tier_quality[decision.chosen.tier]
```
> Note to implementer: `tests/test_router.py` already constructs a ctx and `S`. Reuse its existing helpers/fixtures rather than the placeholder `_mid_tier_ctx_allowing_cloud()` / `_cheap_ctx()` names above — match the file's actual style. The behavioral assertions (higher-quality-wins; default==tier_default) are the requirement.

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/test_router.py -k "quality or tier_default" -v`
Expected: FAIL — `Router.__init__` takes no `quality_resolver`; `RoutingDecision` has no `quality_source`.

- [ ] **Step 3: Update `RoutingDecision` and `Router`**

In `app/routing/router.py`:

Remove the module-level `_Q` (line 9) — the resolver + `tier_quality` config now own tier quality. Keep `_ORDER` and `_L`.

Extend `RoutingDecision` (lines 13-18):
```python
@dataclass
class RoutingDecision:
    chosen: PoolCandidate
    est_cost_usd: float
    reason: str
    rejected: list[RejectedCandidate] = field(default_factory=list)
    quality_score: float = 0.0
    quality_source: str = "tier_default"
```

Update `Router.__init__` (lines 21-23) and add the resolver import:
```python
from app.routing.quality import QualityResolver
...
class Router:
    def __init__(self, settings: Settings, quality_resolver: QualityResolver | None = None) -> None:
        self._s = settings
        self._quality = quality_resolver or QualityResolver(settings)
```

In `select()`, replace the `score()` closure (lines 93-97) so quality comes from the resolver:
```python
        def score(item: tuple[PoolCandidate, float]) -> float:
            c, est = item
            norm = est / max_cost if max_cost > 0 else 0.0
            q = self._quality.resolve(c.name).score
            return (w["quality"] * q - w["cost"] * norm
                    - w["latency"] * _L[c.tier] + w["capacity"] * 1.0)
```

Then resolve the chosen model's provenance and attach it to the returned decision (replace the `return RoutingDecision(...)` at lines 105-107):
```python
        chosen_q = self._quality.resolve(chosen.name)
        return RoutingDecision(
            chosen=chosen, est_cost_usd=est,
            reason=f"tier {chosen.tier} (target {target})", rejected=rejected,
            quality_score=chosen_q.score, quality_source=chosen_q.source)
```

- [ ] **Step 4: Add the `DecisionRecord` fields**

In `app/models.py`, extend `DecisionRecord` (after `tokens_priced`, line 92):
```python
    quality_score: float | None = None
    quality_source: str | None = None  # customer|calibrated|published|tier_default
```

- [ ] **Step 5: Surface quality at the two DecisionRecord construction sites**

In `app/gateway/gateway.py`, the `step="router"` record (lines 55-60) — add the two kwargs:
```python
            records.append(DecisionRecord(
                step="router", chosen=cand.name,
                chosen_display_name=cand.display_name, reason=decision.reason,
                rejected=decision.rejected, est_cost_usd=decision.est_cost_usd,
                actual_cost_usd=result.cost_usd, latency_ms=result.latency_ms,
                hop_index=len(records),
                quality_score=decision.quality_score,
                quality_source=decision.quality_source))
```

In `app/agent/internal_endpoint.py`, the `step="agent_hop"` record (lines 162-168) — add the two kwargs:
```python
    HOPS.record(run_id, DecisionRecord(
        step="agent_hop", chosen=decision.chosen.name,
        chosen_display_name=decision.chosen.display_name, reason=decision.reason,
        rejected=decision.rejected, est_cost_usd=decision.est_cost_usd,
        actual_cost_usd=gen.cost_usd,
        latency_ms=gen.latency_ms,
        hop_index=hop_index, tokens_priced=tokens_after,
        quality_score=decision.quality_score,
        quality_source=decision.quality_source))
```

- [ ] **Step 6: Run the router tests + regression**

Run: `uv run pytest tests/test_router.py tests/test_router_cheap_tier_regression.py -v`
Expected: PASS — new quality cases pass; the cheap-tier regression is unchanged (tier_quality values equal the old `_Q`).

- [ ] **Step 7: Commit**

```bash
git add app/routing/router.py app/models.py app/gateway/gateway.py app/agent/internal_endpoint.py tests/test_router.py
git commit -m "feat(routing): score on resolved per-model quality; carry provenance on DecisionRecord

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 5: Metrics plumbing — quality onto the response, `RequestSummary`, and `usage_records`

**Files:**
- Modify: `app/pipeline.py` (add a `quality` block to both response dicts — direct ~lines 132-150, multi-step ~lines 215-236)
- Modify: `app/api/history.py` (`RequestSummary`, `record`)
- Modify: `app/apps/models.py` (`UsageRecord` — new columns)
- Modify: `app/apps/store.py` (`record_usage` signature) + `app/api/routes.py` (call site passes quality)
- Test: `tests/test_history_store.py` (add cases)

**Interfaces:**
- Consumes: `DecisionRecord.quality_score` / `quality_source` (Task 4).
- Produces: response `quality` block `{"sum": float, "chosen": float|None, "source": str|None}`; `RequestSummary.quality_sum: float`, `.total_tokens: int`, `.quality: float | None`, `.quality_source: str | None`; `UsageRecord.quality`, `.quality_source` columns; `record_usage(..., quality=0.0, quality_source="?")`.
- Consumed by: Task 6 (TID/CID from `RequestSummary.quality_sum` + `.total_tokens`).

- [ ] **Step 1: Write the failing test**

Add to `tests/test_history_store.py` (mirror the module's existing `resp` dict shape used by other tests):
```python
def test_record_captures_quality_sum_and_source_and_tokens():
    from app.api.history import HistoryStore
    store = HistoryStore()
    resp = {
        "id": "r1", "model": "local-8b", "model_display": "Qwen (local)",
        "usage": {"prompt_tokens": 100, "completion_tokens": 40, "total_tokens": 140},
        "cost": {"total_usd": 0.001, "baseline_usd": 0.02,
                 "routing_saved_usd": 0.019, "compression_saved_usd": 0.0},
        "quality": {"sum": 0.5, "chosen": 0.5, "source": "tier_default"},
        "trace": [{"step": "router", "latency_ms": 12.0}],
        "policy": {"sensitivity": "public", "onprem_only": False, "detected_entities": []},
    }
    s = store.record(resp, tenant_id="acme", tier="cheap", provider="onprem")
    assert s.quality_sum == 0.5
    assert s.total_tokens == 140
    assert s.quality == 0.5
    assert s.quality_source == "tier_default"
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_history_store.py -k quality -v`
Expected: FAIL — `RequestSummary` has no `quality_sum`.

- [ ] **Step 3: Add the `quality` block to both pipeline responses**

In `app/pipeline.py` direct path, compute the quality sum from the gateway records and add the block to the returned dict (insert alongside the `cost` key, ~line 144). After `trace = [...]` (line 129-130), add:
```python
        qrecs = [r for r in records if r.quality_score is not None]
        quality_sum = sum(r.quality_score for r in qrecs)
        chosen_q = qrecs[-1] if qrecs else None
```
and add to the returned dict (after the `"cost": {...}` entry, line 146):
```python
            "quality": {"sum": quality_sum,
                        "chosen": chosen_q.quality_score if chosen_q else None,
                        "source": chosen_q.quality_source if chosen_q else None},
```

In `app/pipeline.py` multi-step path, after `trace = [...]` (line 204-206), add:
```python
        qhops = [h for h in result.hops if h.quality_score is not None]
        quality_sum = sum(h.quality_score for h in qhops)
        chosen_q = qhops[-1] if qhops else None
```
and add to the returned dict (after the `"cost": {...}` entry, line 232):
```python
            "quality": {"sum": quality_sum,
                        "chosen": chosen_q.quality_score if chosen_q else None,
                        "source": chosen_q.quality_source if chosen_q else None},
```

- [ ] **Step 4: Extend `RequestSummary` and `record`**

In `app/api/history.py`, add to `RequestSummary` (after `detected_entities`, line 44):
```python
    total_tokens: int = 0
    quality_sum: float = 0.0            # Σ per-call quality — TID/CID numerator
    quality: float | None = None        # chosen model's score (display)
    quality_source: str | None = None   # provenance of `quality` (display)
```

In `record()` (after reading `cost`/`policy`, ~line 73), add:
```python
        usage = resp.get("usage", {})
        quality = resp.get("quality", {})
```
and pass into the `RequestSummary(...)` constructor (after `created_seq=self._seq`, line 101):
```python
            total_tokens=int(usage.get("total_tokens", 0)),
            quality_sum=float(quality.get("sum", 0.0)),
            quality=quality.get("chosen"),
            quality_source=quality.get("source"),
```

- [ ] **Step 5: Add durable columns + thread through `record_usage`**

In `app/apps/models.py`, add to `UsageRecord` (after `total_tokens`, line 159 — grouped with the metrics columns):
```python
    quality: Mapped[float] = mapped_column(Float, default=0.0)
    quality_source: Mapped[str] = mapped_column(String(20), default="?")
```

In `app/apps/store.py` `record_usage`, add two keyword params (with defaults, so no other caller breaks) and set them on the row:
```python
    quality: float = 0.0,
    quality_source: str = "?",
```
and in the `UsageRecord(...)` construction add `quality=quality, quality_source=quality_source,`.

In `app/api/routes.py`, at the `record_usage(...)` call site, pass the chosen model's quality from the response:
```python
        quality=(resp.get("quality") or {}).get("chosen") or 0.0,
        quality_source=(resp.get("quality") or {}).get("source") or "?",
```

- [ ] **Step 6: Run the history test + the app-store/route tests**

Run: `uv run pytest tests/test_history_store.py tests/test_apps_store.py tests/test_routes.py -v`
(Adjust the app-store/routes test file names to those present; the goal is: history captures quality, and `record_usage` + the route still pass.)
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add app/pipeline.py app/api/history.py app/apps/models.py app/apps/store.py app/api/routes.py tests/test_history_store.py
git commit -m "feat(metrics): thread per-request quality onto response, history, and usage_records

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

> **Deployment note (not a code step):** `usage_records` gains two columns. On the DGX/persistent Postgres, run BEFORE rolling the new image (per `routeforge-schema-no-migrations`):
> ```sql
> ALTER TABLE usage_records ADD COLUMN quality DOUBLE PRECISION DEFAULT 0.0;
> ALTER TABLE usage_records ADD COLUMN quality_source VARCHAR(20) DEFAULT '?';
> ```
> Fresh SQLite/test DBs need nothing (`create_all`).

---

### Task 6: Dashboard stats — Token & Cost Intelligence Density

**Files:**
- Modify: `app/api/history.py` (`stats`, and `fleet` for parity)
- Test: `tests/test_history_store.py` (add cases)

**Interfaces:**
- Consumes: `RequestSummary.quality_sum`, `.total_tokens`, `.total_usd` (Task 5).
- Produces: `stats()` keys `token_intelligence_density` (= Σquality_sum ÷ Σtotal_tokens × 1000) and `cost_intelligence_density` (= Σquality_sum ÷ Σtotal_usd); same keys per row in `fleet()`.
- Consumed by: Task 7 (frontend tiles read `/api/stats`).

- [ ] **Step 1: Write the failing test**

Add to `tests/test_history_store.py`:
```python
def _resp(rid, tokens, total_usd, qsum, chosen=0.5):
    return {
        "id": rid, "model": "local-8b", "model_display": "Qwen (local)",
        "usage": {"prompt_tokens": tokens, "completion_tokens": 0, "total_tokens": tokens},
        "cost": {"total_usd": total_usd, "baseline_usd": total_usd * 10,
                 "routing_saved_usd": 0.0, "compression_saved_usd": 0.0},
        "quality": {"sum": qsum, "chosen": chosen, "source": "tier_default"},
        "trace": [{"step": "router", "latency_ms": 5.0}],
        "policy": {"sensitivity": "public", "onprem_only": False, "detected_entities": []},
    }


def test_stats_reports_intelligence_densities():
    from app.api.history import HistoryStore
    store = HistoryStore()
    store.record(_resp("a", tokens=1000, total_usd=0.002, qsum=0.5), "acme", "cheap", "onprem")
    store.record(_resp("b", tokens=1000, total_usd=0.002, qsum=0.5), "acme", "cheap", "onprem")
    st = store.stats()
    # TID = Σqsum / Σtokens * 1000 = 1.0 / 2000 * 1000 = 0.5
    assert st["token_intelligence_density"] == pytest.approx(0.5)
    # CID = Σqsum / Σcost = 1.0 / 0.004 = 250.0
    assert st["cost_intelligence_density"] == pytest.approx(250.0)


def test_stats_densities_zero_on_empty():
    from app.api.history import HistoryStore
    st = HistoryStore().stats()
    assert st["token_intelligence_density"] == 0.0
    assert st["cost_intelligence_density"] == 0.0
```
(Ensure `import pytest` is present in the test file.)

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_history_store.py -k density -v`
Expected: FAIL — keys absent.

- [ ] **Step 3: Compute the densities in `stats()`**

In `app/api/history.py` `stats()`, add both keys to the `count == 0` early return (lines 167-177):
```python
                "token_intelligence_density": 0.0,
                "cost_intelligence_density": 0.0,
```
and in the main return (lines 191-201) compute and add:
```python
        q_sum = sum(s.quality_sum for s in summaries)
        tok_sum = sum(s.total_tokens for s in summaries)
        usd_sum = sum(s.total_usd for s in summaries)
```
```python
            "token_intelligence_density": (q_sum / tok_sum * 1000.0) if tok_sum > 0 else 0.0,
            "cost_intelligence_density": (q_sum / usd_sum) if usd_sum > 0 else 0.0,
```

In `fleet()` (per-row dict, lines 141-159), add for parity:
```python
                "token_intelligence_density": (
                    sum(s.quality_sum for s in items) / sum(s.total_tokens for s in items) * 1000.0
                    if sum(s.total_tokens for s in items) > 0 else 0.0),
                "cost_intelligence_density": (
                    sum(s.quality_sum for s in items) / sum(s.total_usd for s in items)
                    if sum(s.total_usd for s in items) > 0 else 0.0),
```

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest tests/test_history_store.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/api/history.py tests/test_history_store.py
git commit -m "feat(metrics): Token & Cost Intelligence Density in stats and fleet

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 7: Dashboard UI — density tiles + quality provenance

**Files:**
- Modify: `ui/src/console/aggregate.ts` (surface `token_intelligence_density` / `cost_intelligence_density` from `/api/stats`, or derive a client-side `densityAgg`)
- Modify: `ui/src/console/sections/CostSection.tsx` (render two `StatTile`s)
- Verify: `npm run build` (typecheck) — the console UI has no JS unit-test runner; the deliverable is a clean typecheck/build and the tiles rendering.

**Interfaces:**
- Consumes: `/api/stats` JSON `token_intelligence_density`, `cost_intelligence_density` (Task 6).
- Produces: two new stat tiles in the cost dashboard: **Token Intelligence Density** and **Cost Intelligence Density**.

- [ ] **Step 1: Read the current CostSection and aggregate**

Read `ui/src/console/sections/CostSection.tsx` and `ui/src/console/aggregate.ts` to match the existing `StatTile` prop shape and how `stats` is fetched/typed (the `/api/stats` response type).

- [ ] **Step 2: Wire the two values through the stats type/aggregate**

Add `token_intelligence_density: number` and `cost_intelligence_density: number` to the `/api/stats` response type used in `aggregate.ts` (mirror how `total_usd`/`baseline_usd` are typed). No new math is needed client-side — the server already computes them (Task 6); pass them through.

- [ ] **Step 3: Render the tiles in `CostSection.tsx`**

Add two `StatTile`s next to the existing savings/latency tiles (match the existing grid at ~lines 110-115). Label them "Token Intelligence Density" (format to 2-3 sig figs) and "Cost Intelligence Density" (quality per $). Add a one-line caption/tooltip: *"quality delivered per 1k tokens / per $ — needs a per-model quality score."* Keep the tier colour scheme; do not render any raw values or PII.

- [ ] **Step 4: Typecheck / build**

Run: `cd ui && npm run build`
Expected: build succeeds, no TypeScript errors.

- [ ] **Step 5: Commit**

```bash
git add ui/src/console/aggregate.ts ui/src/console/sections/CostSection.tsx
git commit -m "feat(ui): Token & Cost Intelligence Density tiles on the cost dashboard

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## After all tasks: full suite + branch finish

- [ ] **Run the whole backend suite:** `uv run pytest -q` — expect green.
- [ ] **Lint the shipped config standalone:** `uv run python -m app.config.lint` — expect `CONFIG_LINT: ok`.
- [ ] **Frontend build:** `cd ui && npm run build` — expect success.
- [ ] Then use **superpowers:finishing-a-development-branch** to integrate `feat/model-quality-scoring`.

## Out of scope for this plan (Stage 2 — separate plan)

- The calibration harness (graduate `tests/traffic/generator.py` into `app/`, eval set, LLM judge through the gateway, write `source=calibrated`).
- Customer-declared score endpoint (`source=customer`).
- Live DB→router refresh: loading `store.load_quality_overrides` into the `Router`'s override snapshot at startup + cache invalidation on write. **Stage 1 ships the seam (override param, loader, table) but wires the production override snapshot as empty** — published + tier_default are live; the DB rungs are exercised only in unit tests.
- The **CLAUDE.md §10 governance amendment** (permits Stage-2 evaluation) — Stage 2's first task; **not needed for Stage 1** (no evaluation here).
- Stage 3 production-posterior quality; `quality_by_class` + a domain classifier; savings-at-equivalent-quality baseline re-expression (spec §6) — deferred.

## Self-review notes (author)

- **Spec coverage (Stage 1 slice):** resolver ladder §4 → Task 3; config ownership + lint §5 → Task 1; `model_quality` table §5 → Task 2; router integration §6 → Task 4; TID/CID §8 → Tasks 5-6; AA-as-published-seed §9 → Task 1 (capability + convention, values deferred to onboarding). Stage 2 items §7/§10 and deferred §6-baseline/§13 explicitly parked above.
- **Type consistency:** `QualityScore(score, source)` and `QUALITY_SOURCES` used identically in Tasks 3-4; `RoutingDecision.quality_score/quality_source` → `DecisionRecord.quality_score/quality_source` → response `quality.{sum,chosen,source}` → `RequestSummary.quality_sum/quality/quality_source/total_tokens` → `stats()` densities. Names match across tasks.
- **No placeholders:** every code step has real code except the two test-helper names in Task 4 (`_mid_tier_ctx_allowing_cloud`/`_cheap_ctx`), explicitly flagged for the implementer to map onto `tests/test_router.py`'s existing ctx-building style — the behavioral assertions are concrete.
