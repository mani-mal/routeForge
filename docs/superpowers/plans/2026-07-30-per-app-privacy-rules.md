# Per-Application Privacy Rules Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let each application define its own PII/PHI sensitivity rules and custom regex recognizers, editable from the admin console, so classification is per-app instead of one global `policy.yaml` list.

**Architecture:** New DB tables (`app_recognizers`, `app_sensitivity_rules`) hold a per-app privacy profile, resolved once per request beside the existing `AppPolicyView`. The gate builds an *effective detector* = the shared global Presidio engine + the app's custom recognizers (a cheap regex second pass), and classifies with the app's rule set (falling back to the global rules when the app has none). The same effective detector is used for step-9 egress verification. Full per-app control; the app `default_residency` is the only backstop via the existing `_stricter()`.

**Tech Stack:** Python 3.12, FastAPI, Pydantic v2, SQLAlchemy async, Presidio, the `regex` module (per-match timeout), React + Vite + Tailwind, pytest.

## Global Constraints

- Sensitivity enum is exactly `public | internal | confidential | restricted` (from `policy.yaml`). Risk class is `low | medium | high`. Residency is `any | private_cloud_ok | onprem_only`. Tiers are `cheap | mid | frontier`. Provider name is `onprem` (one word). Copy these values verbatim; never introduce a fourth spelling.
- **Fail closed (CLAUDE.md rule 5):** any error resolving a profile or running a custom regex → reject the request; never fall through to allow.
- **Redact on egress (rule 2):** custom entity types must be masked on egress, same as built-ins.
- **`onprem_only` is absolute (rule 3):** never weaken the `_stricter(rule.residency, app.default_residency)` combination.
- **Nothing external before the gate (rule 1):** custom recognizers run inside the gate only.
- Display names change; app **ids and tokens stay** (`acme`/`acme-key`, `hospital`/`hospital-key`, `default`/`demo-key`).
- Entity **types** only in any UI/telemetry; never raw values, never CCR contents.
- Commit after every task. Do not run `git add -A`; stage named files.
- Run tests with `.venv/bin/python -m pytest`. Presidio-dependent tests use `pytest.importorskip("presidio_analyzer")` (the lean dev venv has no presidio; the image/DGX do).

## File structure

**Create**
- `app/policy/enums.py` — canonical `SENSITIVITY`, `RISK_CLASS`, `RESIDENCY` frozensets (single source).
- `app/policy/recognizers.py` — `CustomRecognizer`, regex validation + ReDoS screen, runtime-timeout matcher.
- `app/policy/effective.py` — `EffectiveDetector` (built-ins + custom overlay, merge/dedup).
- `app/apps/privacy.py` — `AppPrivacyProfile`, `SensitivityRuleView`, resolver + fallback + terminal catch-all, in-memory cache.
- Test files mirroring each of the above under `tests/`.

**Modify**
- `app/apps/models.py` — `AppRecognizer`, `AppSensitivityRule` ORM tables + relationships.
- `app/apps/store.py` — `get_privacy_profile`, `replace_privacy_profile`.
- `app/policy/context.py` — `build_context(..., app_rules=None)`; `_match_rule` over app rules.
- `app/policy/gate.py` — `Gate.evaluate(..., detector=None, rules=None)`.
- `app/pipeline.py` — resolve profile, build effective detector, thread to gate + verify.
- `app/api/routes.py` — resolve `AppPrivacyProfile`, pass to `pipeline.run`.
- `app/api/admin_routes.py` — `GET`/`PUT /admin/applications/{id}/privacy`.
- `app/config/lint.py` — use `app/policy/enums.py`.
- `app/apps/bootstrap.py` — rename display names, seed profiles.
- `pyproject.toml` — add `regex` dependency.
- `ui/src/api.ts`, `ui/src/console/AppConfig.tsx` (or the settings component) — privacy panel + types.

---

### Task 1: Canonical enum module

**Files:**
- Create: `app/policy/enums.py`
- Modify: `app/config/lint.py:44` (reuse the set)
- Test: `tests/test_policy_enums.py`

**Interfaces:**
- Produces: `SENSITIVITY: frozenset[str]`, `RISK_CLASS: frozenset[str]`, `RESIDENCY: frozenset[str]` in `app/policy/enums.py`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_policy_enums.py
from app.policy.enums import SENSITIVITY, RISK_CLASS, RESIDENCY

def test_canonical_sets_exact():
    assert SENSITIVITY == frozenset({"public", "internal", "confidential", "restricted"})
    assert RISK_CLASS == frozenset({"low", "medium", "high"})
    assert RESIDENCY == frozenset({"any", "private_cloud_ok", "onprem_only"})
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_policy_enums.py -v`
Expected: FAIL with "No module named 'app.policy.enums'".

- [ ] **Step 3: Write minimal implementation**

```python
# app/policy/enums.py
"""Canonical policy vocabularies — the single source for the sensitivity,
risk-class, and residency values used by the boot config-lint AND the
write-time per-app privacy-rule validation. Never spell these anywhere else."""
from __future__ import annotations

SENSITIVITY = frozenset({"public", "internal", "confidential", "restricted"})
RISK_CLASS = frozenset({"low", "medium", "high"})
RESIDENCY = frozenset({"any", "private_cloud_ok", "onprem_only"})
```

- [ ] **Step 4: Point the lint at it.** In `app/config/lint.py`, replace the inline
`valid_residency = {"any", "private_cloud_ok", "onprem_only"}` (line ~44) with:

```python
from app.policy.enums import RESIDENCY as valid_residency
```

(Move the import to the top of the file.)

- [ ] **Step 5: Run the enum test and the existing lint tests**

Run: `.venv/bin/python -m pytest tests/test_policy_enums.py tests/test_config_lint.py -v`
Expected: PASS (all).

- [ ] **Step 6: Commit**

```bash
git add app/policy/enums.py app/config/lint.py tests/test_policy_enums.py
git commit -m "feat(policy): single-source canonical sensitivity/risk/residency enums"
```

---

### Task 2: ORM tables for recognizers and rules

**Files:**
- Modify: `app/apps/models.py` (add two classes near `AppPolicy`)
- Test: `tests/test_privacy_models.py`

**Interfaces:**
- Produces: `AppRecognizer(application_id, name, regex, score, enabled)` and
  `AppSensitivityRule(application_id, position, entities, sensitivity, risk_class, residency, require_verification)` ORM models; `Application.recognizers` and `Application.sensitivity_rules` relationships (list, cascade delete).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_privacy_models.py
import pytest
from app.apps.db import Database
from app.apps.models import Application, AppRecognizer, AppSensitivityRule

@pytest.mark.asyncio
async def test_recognizer_and_rule_persist_and_cascade():
    db = Database(seed=None)  # in-memory sqlite, no seeding
    async with db.session() as s:
        s.add(Application(id="t1", name="T1", default_residency="any"))
        s.add(AppRecognizer(application_id="t1", name="MRN", regex=r"\bMRN-\d{7}\b", score=0.9))
        s.add(AppSensitivityRule(
            application_id="t1", position=0, entities=["MRN"],
            sensitivity="restricted", risk_class="high",
            residency="onprem_only", require_verification=True))
        await s.commit()
    async with db.session() as s:
        app = await s.get(Application, "t1")
        # deleting the app cascades to its recognizers/rules
        await s.delete(app)
        await s.commit()
    async with db.session() as s:
        assert await s.get(AppRecognizer, ("t1", "MRN")) is None
```

Note: confirm `Database(seed=None)` yields an isolated in-memory engine; if the existing test suite uses a different fixture (see `tests/conftest.py`), use that fixture instead and keep the assertions.

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_privacy_models.py -v`
Expected: FAIL with "cannot import name 'AppRecognizer'".

- [ ] **Step 3: Write minimal implementation** — add to `app/apps/models.py`:

```python
class AppRecognizer(Base):
    """A customer-defined entity recognizer: a named regex whose matches are
    tagged with `name` (a custom entity type) and fed into classification."""
    __tablename__ = "app_recognizers"
    application_id: Mapped[str] = mapped_column(
        ForeignKey("applications.id", ondelete="CASCADE"), primary_key=True)
    name: Mapped[str] = mapped_column(String(32), primary_key=True)
    regex: Mapped[str] = mapped_column(Text)
    score: Mapped[float] = mapped_column(Float, default=0.85)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)


class AppSensitivityRule(Base):
    """Per-app classification rule. `position` orders rules most-severe-first,
    mirroring config/policy.yaml. Overrides the global sensitivity_rules for
    this application when any row exists."""
    __tablename__ = "app_sensitivity_rules"
    application_id: Mapped[str] = mapped_column(
        ForeignKey("applications.id", ondelete="CASCADE"), primary_key=True)
    position: Mapped[int] = mapped_column(Integer, primary_key=True)
    entities: Mapped[list] = mapped_column(JSON)
    sensitivity: Mapped[str] = mapped_column(String(16))
    risk_class: Mapped[str] = mapped_column(String(8))
    residency: Mapped[str] = mapped_column(String(24))
    require_verification: Mapped[bool] = mapped_column(Boolean, default=False)
```

Add to `Application` (near the existing `policy` relationship):

```python
    recognizers: Mapped[list["AppRecognizer"]] = relationship(
        cascade="all, delete-orphan")
    sensitivity_rules: Mapped[list["AppSensitivityRule"]] = relationship(
        cascade="all, delete-orphan")
```

Ensure `Text`, `Float`, `Boolean`, `Integer`, `JSON`, `String`, `ForeignKey`, `Mapped`, `mapped_column`, `relationship` are imported (most already are — check the top of the file).

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_privacy_models.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/apps/models.py tests/test_privacy_models.py
git commit -m "feat(apps): app_recognizers + app_sensitivity_rules tables"
```

---

### Task 3: Custom recognizer validation + ReDoS screen + runtime-timeout matcher

**Files:**
- Modify: `pyproject.toml` (add `regex` to `[project.dependencies]`, then `uv lock`)
- Create: `app/policy/recognizers.py`
- Test: `tests/test_recognizers.py`

**Interfaces:**
- Produces:
  - `NAME_RE` pattern; `validate_recognizer(name: str, pattern: str) -> None` (raises `ValueError` on bad name/regex/ReDoS).
  - `PRESIDIO_BUILTINS: frozenset[str]` — reserved built-in names.
  - `CustomRecognizer(name: str, regex: str, score: float)` with `find(text: str) -> list[Entity]` (uses `regex` module `timeout=0.1`; on timeout raises `RecognizerTimeout`).
  - `RecognizerTimeout(Exception)`.

- [ ] **Step 1: Add the dependency**

Edit `pyproject.toml` → `[project.dependencies]`, add `"regex>=2024.0.0"`. Then:

Run: `uv lock` (regenerates `uv.lock`).

- [ ] **Step 2: Write the failing test**

```python
# tests/test_recognizers.py
import pytest
from app.policy.recognizers import (
    validate_recognizer, CustomRecognizer, RecognizerTimeout, PRESIDIO_BUILTINS)

def test_valid_recognizer_accepts_good_regex():
    validate_recognizer("BADGE_ID", r"\bEMP-\d{6}\b")  # no raise

def test_rejects_lowercase_name():
    with pytest.raises(ValueError):
        validate_recognizer("badge", r"\d+")

def test_rejects_builtin_name_collision():
    assert "US_SSN" in PRESIDIO_BUILTINS
    with pytest.raises(ValueError):
        validate_recognizer("US_SSN", r"\d{9}")

def test_rejects_uncompilable_regex():
    with pytest.raises(ValueError):
        validate_recognizer("X", r"(unterminated")

def test_rejects_catastrophic_regex():
    with pytest.raises(ValueError):
        validate_recognizer("X", r"(a+)+$")   # classic ReDoS shape

def test_custom_recognizer_finds_entities():
    r = CustomRecognizer("BADGE_ID", r"\bEMP-\d{6}\b", 0.9)
    ents = r.find("hello EMP-123456 world")
    assert [(e.type, e.value) for e in ents] == [("BADGE_ID", "EMP-123456")]
```

- [ ] **Step 3: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_recognizers.py -v`
Expected: FAIL (module missing).

- [ ] **Step 4: Write minimal implementation**

```python
# app/policy/recognizers.py
"""Customer-defined regex recognizers: validation (name, compile, ReDoS screen)
and a runtime matcher with a per-match timeout. Kept separate from the built-in
Presidio path so it works under both pii_detection: presidio and regex."""
from __future__ import annotations

import re as _re
import regex  # the `regex` module: supports per-match timeout=

from app.models import Entity

NAME_RE = _re.compile(r"^[A-Z][A-Z0-9_]{1,31}$")
MAX_PATTERN_LEN = 512
_MATCH_TIMEOUT_S = 0.1

# Presidio's common built-in entity types — reserved so a custom name can't
# shadow one and cause confusing double-detection.
PRESIDIO_BUILTINS = frozenset({
    "US_SSN", "CREDIT_CARD", "EMAIL_ADDRESS", "PHONE_NUMBER", "US_BANK_NUMBER",
    "PERSON", "LOCATION", "DATE_TIME", "IP_ADDRESS", "URL", "MEDICAL_LICENSE",
    "US_DRIVER_LICENSE", "US_PASSPORT", "IBAN_CODE", "NRP", "US_ITIN",
})

# Adversarial inputs used to smoke out catastrophic backtracking at write time.
_REDOS_PROBES = ["a" * 40 + "!", "EMP-" + "1" * 40 + "x", "(" * 30]


class RecognizerTimeout(Exception):
    pass


def validate_recognizer(name: str, pattern: str) -> None:
    if not NAME_RE.match(name):
        raise ValueError(f"invalid recognizer name {name!r} (want ^[A-Z][A-Z0-9_]{{1,31}}$)")
    if name in PRESIDIO_BUILTINS:
        raise ValueError(f"{name!r} collides with a built-in entity type")
    if len(pattern) > MAX_PATTERN_LEN:
        raise ValueError("pattern too long")
    try:
        compiled = regex.compile(pattern)
    except regex.error as e:
        raise ValueError(f"regex does not compile: {e}") from e
    # ReDoS screen: run against adversarial inputs under a tight timeout.
    for probe in _REDOS_PROBES:
        try:
            compiled.search(probe, timeout=_MATCH_TIMEOUT_S)
        except TimeoutError as e:
            raise ValueError("pattern too slow (possible ReDoS)") from e


class CustomRecognizer:
    def __init__(self, name: str, pattern: str, score: float = 0.85) -> None:
        self.name = name
        self.score = score
        self._rx = regex.compile(pattern)

    def find(self, text: str) -> list[Entity]:
        out: list[Entity] = []
        try:
            for m in self._rx.finditer(text, timeout=_MATCH_TIMEOUT_S):
                out.append(Entity(type=self.name, start=m.start(), end=m.end(),
                                  value=m.group()))
        except TimeoutError as e:
            raise RecognizerTimeout(self.name) from e
        return out
```

(Note: the `regex` module raises `TimeoutError` on timeout.)

- [ ] **Step 5: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_recognizers.py -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add pyproject.toml uv.lock app/policy/recognizers.py tests/test_recognizers.py
git commit -m "feat(policy): custom regex recognizers with ReDoS screen + match timeout"
```

---

### Task 4: EffectiveDetector (built-ins + custom overlay)

**Files:**
- Create: `app/policy/effective.py`
- Test: `tests/test_effective_detector.py`

**Interfaces:**
- Consumes: `Detector` protocol (`app/policy/detectors.py`), `CustomRecognizer` (Task 3).
- Produces: `EffectiveDetector(base: Detector, recognizers: list[CustomRecognizer])` implementing the `Detector` protocol (`analyze`, `anonymize`, `warmup`). Merges base + custom entities, de-duped by span (longest, then highest score wins).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_effective_detector.py
from app.policy.detectors import RegexDetector
from app.policy.recognizers import CustomRecognizer
from app.policy.effective import EffectiveDetector

def test_custom_entity_added_on_top_of_builtins():
    base = RegexDetector()
    det = EffectiveDetector(base, [CustomRecognizer("BADGE_ID", r"\bEMP-\d{6}\b", 0.9)])
    ents = det.analyze("ssn 482-73-1946 badge EMP-123456")
    types = {e.type for e in ents}
    assert "US_SSN" in types and "BADGE_ID" in types

def test_overlapping_custom_wins_by_score_then_length():
    base = RegexDetector()  # US_BANK_NUMBER matches long digit runs
    det = EffectiveDetector(base, [CustomRecognizer("ACCT", r"\b\d{10}\b", 0.99)])
    ents = det.analyze("acct 1234567890 end")
    spans = [(e.type, e.start, e.end) for e in ents]
    assert ("ACCT", 5, 15) in spans
    # no other entity occupies the same span
    assert sum(1 for _t, s, e in spans if (s, e) == (5, 15)) == 1

def test_anonymize_reuses_placeholder_scheme_for_custom():
    det = EffectiveDetector(RegexDetector(), [CustomRecognizer("BADGE_ID", r"\bEMP-\d{6}\b", 0.9)])
    text = "badge EMP-123456"
    ents = det.analyze(text)
    red, mapping = det.anonymize(text, ents)
    assert "EMP-123456" not in red and "<BADGE_ID_1>" in red
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_effective_detector.py -v`
Expected: FAIL (module missing).

- [ ] **Step 3: Write minimal implementation**

```python
# app/policy/effective.py
"""Detector that layers per-app custom recognizers on top of a base detector
(Presidio or regex). Built once per app and cached; see app/apps/privacy.py."""
from __future__ import annotations

from app.models import Entity
from app.policy.detectors import Detector, RegexDetector
from app.policy.recognizers import CustomRecognizer


class EffectiveDetector:
    def __init__(self, base: Detector, recognizers: list[CustomRecognizer]) -> None:
        self._base = base
        self._recognizers = recognizers

    def analyze(self, text: str) -> list[Entity]:
        ents = list(self._base.analyze(text))
        for r in self._recognizers:
            ents.extend(r.find(text))
        return _dedupe(ents)

    def anonymize(self, text: str, entities: list[Entity]):
        # reuse the deterministic placeholder scheme (map symmetry with the gate)
        return RegexDetector().anonymize(text, entities)

    def warmup(self) -> None:
        self._base.warmup()


def _dedupe(ents: list[Entity]) -> list[Entity]:
    # Prefer longer spans, then higher-confidence: sort so the winner for any
    # overlapping region comes first, then greedily drop overlaps.
    def _score(e: Entity) -> float:
        return getattr(e, "score", 0.0) or 0.0
    ents = sorted(ents, key=lambda e: (e.start, -(e.end - e.start), -_score(e)))
    kept: list[Entity] = []
    last_end = -1
    for e in ents:
        if e.start >= last_end:
            kept.append(e)
            last_end = e.end
    return kept
```

Note: if `Entity` has no `score` field, `_score` returns 0.0 for all (built-ins), which still gives longest-span-wins; the custom overlap test passes because `ACCT` (len 10) beats nothing longer. If tie-breaking by score is needed, add an optional `score: float | None = None` field to `Entity` in `app/models.py:42` and have `CustomRecognizer.find` set it — do this only if `test_overlapping_custom_wins_by_score_then_length` fails without it.

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_effective_detector.py -v`
Expected: PASS. (If the score tie-break test fails, apply the `Entity.score` note above and re-run.)

- [ ] **Step 5: Commit**

```bash
git add app/policy/effective.py tests/test_effective_detector.py app/models.py
git commit -m "feat(policy): EffectiveDetector layers custom recognizers on built-ins"
```

---

### Task 5: AppPrivacyProfile value object, resolver, fallback, cache

**Files:**
- Create: `app/apps/privacy.py`
- Modify: `app/apps/store.py` (add `get_privacy_profile`, `replace_privacy_profile`)
- Test: `tests/test_privacy_profile.py`

**Interfaces:**
- Produces:
  - `SensitivityRuleView(entities: frozenset[str], sensitivity, risk_class, residency, require_verification)` (frozen dataclass).
  - `AppPrivacyProfile(recognizers: tuple[CustomRecognizer, ...], rules: tuple[SensitivityRuleView, ...])` (frozen). Empty `rules` means "use global".
  - `async get_privacy_profile(session, app_id) -> AppPrivacyProfile` in `store.py`.
  - `async replace_privacy_profile(session, app_id, recognizers: list[dict], rules: list[dict]) -> None` in `store.py` (deletes existing rows, inserts new; caller validates first).
  - `ProfileCache` with `get(app_id) -> AppPrivacyProfile | None`, `put(app_id, profile)`, `clear(app_id)`.
  - `effective_rules(profile, settings) -> list[SensitivityRuleView]` — the app's rules, or the global `settings.policy.sensitivity_rules` mapped to views, always ending in a terminal `public/low/any` catch-all.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_privacy_profile.py
import pytest
from app.apps.db import Database
from app.apps.models import Application
from app.apps import store
from app.apps.privacy import (
    AppPrivacyProfile, SensitivityRuleView, ProfileCache, effective_rules)
from app.config.loader import load_settings

@pytest.mark.asyncio
async def test_replace_and_get_roundtrip():
    db = Database(seed=None)
    async with db.session() as s:
        s.add(Application(id="t1", name="T1", default_residency="any"))
        await s.commit()
    async with db.session() as s:
        await store.replace_privacy_profile(
            s, "t1",
            recognizers=[{"name": "MRN", "regex": r"\bMRN-\d{7}\b", "score": 0.9}],
            rules=[{"entities": ["MRN"], "sensitivity": "restricted",
                    "risk_class": "high", "residency": "onprem_only",
                    "require_verification": True}])
        await s.commit()
    async with db.session() as s:
        prof = await store.get_privacy_profile(s, "t1")
    assert [r.name for r in prof.recognizers] == ["MRN"]
    assert prof.rules[0].sensitivity == "restricted"

@pytest.mark.asyncio
async def test_empty_profile_falls_back_to_global_rules():
    prof = AppPrivacyProfile(recognizers=(), rules=())
    settings = load_settings()
    rules = effective_rules(prof, settings)
    # global rules present, and a terminal public catch-all guaranteed
    assert any(r.sensitivity == "restricted" for r in rules)
    assert rules[-1].entities == frozenset() and rules[-1].sensitivity == "public"

def test_cache_put_get_clear():
    c = ProfileCache()
    p = AppPrivacyProfile(recognizers=(), rules=())
    assert c.get("a") is None
    c.put("a", p); assert c.get("a") is p
    c.clear("a"); assert c.get("a") is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_privacy_profile.py -v`
Expected: FAIL (module missing).

- [ ] **Step 3: Write minimal implementation** — `app/apps/privacy.py`:

```python
"""Per-app privacy profile: the resolved recognizers + classification rules the
gate uses for one application. Empty rules => fall back to the global policy.yaml
sensitivity_rules. See docs/superpowers/specs/2026-07-30-per-app-privacy-rules-design.md."""
from __future__ import annotations

from dataclasses import dataclass

from app.config.schema import Settings
from app.policy.recognizers import CustomRecognizer

_TERMINAL = None  # built lazily below


@dataclass(frozen=True)
class SensitivityRuleView:
    entities: frozenset[str]
    sensitivity: str
    risk_class: str
    residency: str
    require_verification: bool


@dataclass(frozen=True)
class AppPrivacyProfile:
    recognizers: tuple[CustomRecognizer, ...]
    rules: tuple[SensitivityRuleView, ...]


_PUBLIC_CATCH_ALL = SensitivityRuleView(
    entities=frozenset(), sensitivity="public", risk_class="low",
    residency="any", require_verification=False)


def effective_rules(profile: AppPrivacyProfile, settings: Settings) -> list[SensitivityRuleView]:
    if profile.rules:
        rules = list(profile.rules)
    else:
        rules = [SensitivityRuleView(
            entities=frozenset(r.entities), sensitivity=r.sensitivity,
            risk_class=r.risk_class, residency=r.residency,
            require_verification=r.require_verification)
            for r in settings.policy.sensitivity_rules]
    if not rules or rules[-1].entities != frozenset():
        rules.append(_PUBLIC_CATCH_ALL)
    return rules


class ProfileCache:
    """Single-node in-memory cache, cleared on admin writes (like RateLimiter)."""
    def __init__(self) -> None:
        self._d: dict[str, AppPrivacyProfile] = {}

    def get(self, app_id: str):
        return self._d.get(app_id)

    def put(self, app_id: str, profile: AppPrivacyProfile) -> None:
        self._d[app_id] = profile

    def clear(self, app_id: str) -> None:
        self._d.pop(app_id, None)
```

Add to `app/apps/store.py`:

```python
from app.apps.models import AppRecognizer, AppSensitivityRule
from app.apps.privacy import AppPrivacyProfile, SensitivityRuleView
from app.policy.recognizers import CustomRecognizer
from sqlalchemy import delete, select

async def get_privacy_profile(session, app_id: str) -> AppPrivacyProfile:
    recs = (await session.execute(
        select(AppRecognizer).where(
            AppRecognizer.application_id == app_id,
            AppRecognizer.enabled.is_(True)))).scalars().all()
    rules = (await session.execute(
        select(AppSensitivityRule).where(
            AppSensitivityRule.application_id == app_id)
        .order_by(AppSensitivityRule.position))).scalars().all()
    return AppPrivacyProfile(
        recognizers=tuple(CustomRecognizer(r.name, r.regex, r.score) for r in recs),
        rules=tuple(SensitivityRuleView(
            frozenset(r.entities), r.sensitivity, r.risk_class,
            r.residency, r.require_verification) for r in rules))

async def replace_privacy_profile(session, app_id: str, recognizers: list[dict],
                                  rules: list[dict]) -> None:
    await session.execute(delete(AppRecognizer).where(AppRecognizer.application_id == app_id))
    await session.execute(delete(AppSensitivityRule).where(AppSensitivityRule.application_id == app_id))
    for r in recognizers:
        session.add(AppRecognizer(application_id=app_id, name=r["name"],
                                  regex=r["regex"], score=r.get("score", 0.85),
                                  enabled=r.get("enabled", True)))
    for i, r in enumerate(rules):
        session.add(AppSensitivityRule(
            application_id=app_id, position=i, entities=r["entities"],
            sensitivity=r["sensitivity"], risk_class=r["risk_class"],
            residency=r["residency"],
            require_verification=r.get("require_verification", False)))
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_privacy_profile.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/apps/privacy.py app/apps/store.py tests/test_privacy_profile.py
git commit -m "feat(apps): AppPrivacyProfile resolver, store CRUD, fallback + cache"
```

---

### Task 6: build_context honors per-app rules

**Files:**
- Modify: `app/policy/context.py` (`_match_rule`, `build_context` signature)
- Test: `tests/test_context_app_rules.py`

**Interfaces:**
- Consumes: `SensitivityRuleView` (Task 5).
- Produces: `build_context(settings, request_id, tenant_id, entities, redaction_map_id, app_policy=None, app_rules: list[SensitivityRuleView] | None = None)`. When `app_rules` is given, classification matches over them; else over `settings.policy.sensitivity_rules` (today's behavior). Residency still `_stricter(rule.residency, app_residency)`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_context_app_rules.py
from app.apps.privacy import SensitivityRuleView
from app.config.loader import load_settings
from app.models import Entity
from app.policy.context import build_context

S = load_settings()

def test_app_rules_demote_location():
    rules = [SensitivityRuleView(frozenset({"LOCATION"}), "internal", "low", "any", False),
             SensitivityRuleView(frozenset(), "public", "low", "any", False)]
    ctx = build_context(S, "r1", "acme", [Entity(type="LOCATION", start=0, end=6, value="France")],
                        None, app_rules=rules)
    assert ctx.sensitivity == "internal" and ctx.risk_class == "low"

def test_onprem_tenant_downgrade_still_floored():
    # even if a rule says residency=any, an onprem_only tenant stays onprem_only
    rules = [SensitivityRuleView(frozenset({"US_SSN"}), "restricted", "high", "any", True),
             SensitivityRuleView(frozenset(), "public", "low", "any", False)]
    ctx = build_context(S, "r2", "hospital", [Entity(type="US_SSN", start=0, end=11, value="482-73-1946")],
                        None, app_rules=rules)
    # tenant default (hospital) is onprem_only in policy.yaml -> backstop holds
    assert ctx.onprem_only is True
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_context_app_rules.py -v`
Expected: FAIL ("unexpected keyword argument 'app_rules'").

- [ ] **Step 3: Write minimal implementation** — in `app/policy/context.py`:

Change `_match_rule` to accept a rule list:

```python
def _match_rule(rules, etypes: set[str]):
    for rule in rules:
        if rule.entities and etypes & set(rule.entities):
            return rule
    return rules[-1]
```

In `build_context`, add `app_rules=None` param and pick the rule source:

```python
    etypes = {e.type for e in entities}
    if app_rules is not None:
        rule = _match_rule(app_rules, etypes)
    else:
        rule = _match_rule(settings.policy.sensitivity_rules, etypes)
```

Everything downstream (`rule.sensitivity`, `rule.residency`, `rule.risk_class`, `rule.require_verification`) works unchanged because `SensitivityRuleView` and the config `SensitivityRule` share those attribute names.

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_context_app_rules.py tests/test_policy_gate.py -v`
Expected: PASS (new tests + existing gate tests unaffected).

- [ ] **Step 5: Commit**

```bash
git add app/policy/context.py tests/test_context_app_rules.py
git commit -m "feat(policy): build_context classifies with per-app rules when provided"
```

---

### Task 7: Gate + pipeline thread the effective detector and rules

**Files:**
- Modify: `app/policy/gate.py`, `app/pipeline.py`, `app/api/routes.py`
- Test: `tests/test_gate_effective.py`

**Interfaces:**
- Consumes: `EffectiveDetector` (Task 4), `AppPrivacyProfile` + `effective_rules` (Task 5).
- Produces:
  - `Gate.evaluate(request_id, tenant_id, request, app_policy=None, detector=None, rules=None)` — uses `detector or self._detector` and passes `rules` to `build_context(app_rules=rules)`.
  - `Pipeline.run(request_id, tenant_id, request, app_policy=None, profile: AppPrivacyProfile | None = None)` — builds the effective detector from `profile` (or uses `self._detector` when `profile is None`), passes it to the gate and to `verify(...)`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_gate_effective.py
from app.config.loader import load_settings
from app.models import ChatRequest, Message
from app.policy.detectors import RegexDetector
from app.policy.effective import EffectiveDetector
from app.policy.gate import Gate
from app.policy.recognizers import CustomRecognizer
from app.apps.privacy import SensitivityRuleView

def test_gate_uses_supplied_detector_and_rules():
    S = load_settings()
    gate = Gate(S, RegexDetector())
    det = EffectiveDetector(RegexDetector(), [CustomRecognizer("MRN", r"\bMRN-\d{7}\b", 0.9)])
    rules = [SensitivityRuleView(frozenset({"MRN"}), "restricted", "high", "onprem_only", True),
             SensitivityRuleView(frozenset(), "public", "low", "any", False)]
    req = ChatRequest(model="auto", messages=[Message(role="user", content="patient MRN-1234567")])
    ctx, _red, _map = gate.evaluate("r1", "acme", req, detector=det, rules=rules)
    assert "MRN" in ctx.detected_entities and ctx.sensitivity == "restricted"
    assert ctx.onprem_only is True
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_gate_effective.py -v`
Expected: FAIL ("unexpected keyword argument 'detector'").

- [ ] **Step 3: Implement Gate change** — `app/policy/gate.py`:

```python
    def evaluate(self, request_id, tenant_id, request, app_policy=None,
                 detector=None, rules=None):
        det = detector or self._detector
        text = "\n".join(m.content for m in request.messages)
        entities = det.analyze(text)
        redacted, mapping = det.anonymize(text, entities)
        map_id = f"map-{uuid.uuid4().hex[:8]}" if mapping else None
        ctx = build_context(self._settings, request_id, tenant_id, entities, map_id,
                            app_policy=app_policy, app_rules=rules)
        return ctx, redacted, mapping
```

- [ ] **Step 4: Implement Pipeline change** — `app/pipeline.py` `run(...)`:

Add `profile=None` param. At the top of `run`, build the per-request detector + rules and use them for the gate and verify:

```python
    async def run(self, request_id, tenant_id, request, app_policy=None, profile=None):
        if profile is not None:
            from app.policy.effective import EffectiveDetector
            from app.apps.privacy import effective_rules
            det = EffectiveDetector(self._detector, list(profile.recognizers))
            rules = effective_rules(profile, self._s)
        else:
            det, rules = self._detector, None
        ctx, sanitized, _map = self._gate.evaluate(
            request_id, tenant_id, request, app_policy=app_policy,
            detector=det, rules=rules)
        ...
        # in BOTH verify() call sites in this method, replace self._detector with det:
        text, vrec = verify(det, ctx, result.text)
```

Confirm `self._s` is the Settings attribute name on Pipeline (see `__init__`); if it's `self._settings`, use that. Replace **both** `verify(self._detector, ...)` calls (direct + the multi-step path) with `verify(det, ...)`. For the multi-step branch, thread `det` into `_run_multi_step` and use it there too.

- [ ] **Step 5: Implement routes.py change** — where `pipeline.run(...)` is called (`app/api/routes.py:81`), resolve the profile and pass it. Near the existing app_policy resolution:

```python
    async with request.app.state.db.session() as session:
        profile = await store.get_privacy_profile(session, tenant)
    resp = await request.app.state.pipeline.run(
        request_id, tenant, chat_req, app_policy=app_policy, profile=profile)
```

Prefer the profile cache: `cache = request.app.state.profile_cache` (added in Task 9); `profile = cache.get(tenant)`; on miss, load from DB and `cache.put`. If Task 9 isn't done yet, the direct DB load above is correct and Task 9 will swap in the cache.

- [ ] **Step 6: Run tests**

Run: `.venv/bin/python -m pytest tests/test_gate_effective.py tests/test_pipeline*.py -v`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add app/policy/gate.py app/pipeline.py app/api/routes.py tests/test_gate_effective.py
git commit -m "feat(policy): thread per-app effective detector + rules through gate, pipeline, verify"
```

---

### Task 8: Egress redaction covers custom entity types

**Files:**
- Test: `tests/test_egress_custom_redaction.py`
- Modify: `app/obs/redact.py` and/or `app/output/verify.py` only if the test fails.

**Interfaces:**
- Consumes: the effective detector path (Task 7).

- [ ] **Step 1: Write the failing test** — assert a custom entity type is masked on egress in a captured trace.

```python
# tests/test_egress_custom_redaction.py
from app.obs.redact import redact  # confirm the actual redactor entry point

def test_custom_entity_type_is_redacted_on_egress():
    # A completion echoing a custom-typed value must not appear raw in telemetry.
    # If redact() keys off detected entity TYPES, feed it the custom type.
    masked = redact("patient EMP-123456 discharged", entity_types=["BADGE_ID"],
                    spans=[(8, 18)])
    assert "EMP-123456" not in masked
```

Adapt the call to the real `redact()` signature (read `app/obs/redact.py` first). The key assertion: given a custom entity type + span, egress output masks the value.

- [ ] **Step 2: Run test to verify current behavior**

Run: `.venv/bin/python -m pytest tests/test_egress_custom_redaction.py -v`
Expected: If it PASSES already (redaction is span/type-driven and type-agnostic), no code change is needed — keep the test as a guard and skip to commit. If it FAILS because redaction hardcodes built-in types, proceed to Step 3.

- [ ] **Step 3: Fix only if needed** — make the exporter/verify redaction operate on the entity types the effective detector reports, not a hardcoded built-in list. (Since Task 7 already routes step-9 `verify()` through the effective detector, custom types are detected on egress; ensure the exporter's redaction is likewise type-agnostic.)

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_egress_custom_redaction.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add tests/test_egress_custom_redaction.py app/obs/redact.py app/output/verify.py
git commit -m "test(obs): guarantee custom entity types are redacted on egress"
```

---

### Task 9: Profile cache wired into app state + invalidation

**Files:**
- Modify: `app/main.py` (add `app.state.profile_cache`), `app/api/routes.py` (use cache)
- Test: `tests/test_profile_cache_state.py`

**Interfaces:**
- Consumes: `ProfileCache` (Task 5).
- Produces: `app.state.profile_cache: ProfileCache`, populated on read miss in `routes.py`, cleared by the admin PUT (Task 10).

- [ ] **Step 1: Write the failing test** — boot `create_app()` and assert the cache exists and is a `ProfileCache`.

```python
# tests/test_profile_cache_state.py
import os
def test_app_has_profile_cache(monkeypatch):
    monkeypatch.setenv("SP_PII_DETECTION", "regex")
    monkeypatch.setenv("SP_INFERENCE_BACKEND", "mock")
    from app.main import create_app
    from app.apps.privacy import ProfileCache
    app = create_app()
    assert isinstance(app.state.profile_cache, ProfileCache)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_profile_cache_state.py -v`
Expected: FAIL (`profile_cache` missing).

- [ ] **Step 3: Implement** — in `app/main.py` `create_app()`, after `app.state.rate_limiter = RateLimiter()`:

```python
    from app.apps.privacy import ProfileCache
    app.state.profile_cache = ProfileCache()
```

In `app/api/routes.py`, replace the direct DB load from Task 7 with a cache-aware load:

```python
    cache = request.app.state.profile_cache
    profile = cache.get(tenant)
    if profile is None:
        async with request.app.state.db.session() as session:
            profile = await store.get_privacy_profile(session, tenant)
        cache.put(tenant, profile)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_profile_cache_state.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/main.py app/api/routes.py tests/test_profile_cache_state.py
git commit -m "feat(api): per-app privacy-profile cache on app state"
```

---

### Task 10: Admin API — GET/PUT /admin/applications/{id}/privacy

**Files:**
- Modify: `app/api/admin_routes.py`
- Test: `tests/test_admin_privacy_api.py`

**Interfaces:**
- Consumes: `validate_recognizer` (Task 3), `store.get_privacy_profile`/`replace_privacy_profile` (Task 5), enums (Task 1), `ProfileCache.clear`.
- Produces: `GET /admin/applications/{id}/privacy` → `{recognizers: [...], rules: [...]}`; `PUT` with the same shape (whole-profile, atomic), validated; on success clears the cache entry. 422 on any validation error.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_admin_privacy_api.py
# Use the existing admin test client fixture (see tests/test_admin_*.py for the
# logged-in client pattern). Pseudocode assertions:
#  - PUT valid profile (MRN recognizer + rule) -> 200; GET returns it.
#  - PUT recognizer with lowercase name -> 422.
#  - PUT rule with sensitivity "secret" -> 422.
#  - PUT rule referencing unknown entity "NOPE" -> 422.
#  - PUT catastrophic regex "(a+)+$" -> 422.
```

Write these as real tests against the admin client fixture used by the other `tests/test_admin_*.py` files (reuse that fixture verbatim — do not invent a new one).

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_admin_privacy_api.py -v`
Expected: FAIL (routes missing).

- [ ] **Step 3: Implement** — add Pydantic models + endpoints in `app/api/admin_routes.py`:

```python
from app.policy.enums import SENSITIVITY, RISK_CLASS, RESIDENCY
from app.policy.recognizers import validate_recognizer, PRESIDIO_BUILTINS

class RecognizerIn(BaseModel):
    name: str
    regex: str
    score: float = 0.85
    enabled: bool = True

class RuleIn(BaseModel):
    entities: list[str]
    sensitivity: str
    risk_class: str
    residency: str
    require_verification: bool = False

class PrivacyProfileIn(BaseModel):
    recognizers: list[RecognizerIn] = []
    rules: list[RuleIn] = []

def _validate_profile(body: PrivacyProfileIn) -> None:
    names = set()
    for r in body.recognizers:
        validate_recognizer(r.name, r.regex)   # raises ValueError
        if r.name in names:
            raise ValueError(f"duplicate recognizer {r.name!r}")
        names.add(r.name)
    known = PRESIDIO_BUILTINS | names
    for rule in body.rules:
        if rule.sensitivity not in SENSITIVITY:
            raise ValueError(f"bad sensitivity {rule.sensitivity!r}")
        if rule.risk_class not in RISK_CLASS:
            raise ValueError(f"bad risk_class {rule.risk_class!r}")
        if rule.residency not in RESIDENCY:
            raise ValueError(f"bad residency {rule.residency!r}")
        for e in rule.entities:
            if e not in known:
                raise ValueError(f"unknown entity {e!r}")

@admin_router.get("/applications/{app_id}/privacy")
async def get_privacy(app_id: str, request: Request, _: str = Depends(require_admin)):
    async with request.app.state.db.session() as session:
        if await store.get_application(session, app_id) is None:
            raise HTTPException(404, "application not found")
        prof = await store.get_privacy_profile(session, app_id)
    return {
        "recognizers": [{"name": r.name, "regex": r._rx.pattern, "score": r.score}
                        for r in prof.recognizers],
        "rules": [{"entities": sorted(r.entities), "sensitivity": r.sensitivity,
                   "risk_class": r.risk_class, "residency": r.residency,
                   "require_verification": r.require_verification}
                  for r in prof.rules],
    }

@admin_router.put("/applications/{app_id}/privacy")
async def put_privacy(app_id: str, body: PrivacyProfileIn, request: Request,
                      _: str = Depends(require_admin)):
    try:
        _validate_profile(body)
    except ValueError as e:
        raise HTTPException(422, str(e))
    async with request.app.state.db.session() as session:
        if await store.get_application(session, app_id) is None:
            raise HTTPException(404, "application not found")
        await store.replace_privacy_profile(
            session, app_id,
            recognizers=[r.model_dump() for r in body.recognizers],
            rules=[r.model_dump() for r in body.rules])
        await session.commit()
    request.app.state.profile_cache.clear(app_id)
    return {"ok": True}
```

(If exposing `r._rx.pattern` feels wrong, store the original pattern on `CustomRecognizer` as `self.pattern = pattern` in Task 3 and return `r.pattern`.)

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_admin_privacy_api.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/api/admin_routes.py tests/test_admin_privacy_api.py app/policy/recognizers.py
git commit -m "feat(admin): GET/PUT per-app privacy profile with validation + cache bust"
```

---

### Task 11: Demo seeding — rename display names + seed profiles

**Files:**
- Modify: `app/apps/bootstrap.py`
- Test: `tests/test_seed_privacy.py`

**Interfaces:**
- Consumes: `store.replace_privacy_profile` (Task 5).
- Produces: `seed_all` also seeds each demo app's privacy profile (idempotent). Display names: `acme`→"RAG Agent", `hospital`→"Hospital Agent", `default`→"Support Agent". Ids/tokens unchanged.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_seed_privacy.py
import pytest
from app.apps.db import Database
from app.apps import store

@pytest.mark.asyncio
async def test_seed_demotes_location_and_adds_mrn():
    db = Database(seed=None)
    from app.apps.bootstrap import seed_all
    async with db.session() as s:
        await seed_all(s); await s.commit()
    async with db.session() as s:
        acme = await store.get_application(s, "acme")
        assert acme.name == "RAG Agent"
        prof_acme = await store.get_privacy_profile(s, "acme")
        loc = [r for r in prof_acme.rules if "LOCATION" in r.entities][0]
        assert loc.sensitivity == "internal" and loc.risk_class == "low"
        prof_h = await store.get_privacy_profile(s, "hospital")
        assert any(r.name == "MRN" for r in prof_h.recognizers)

@pytest.mark.asyncio
async def test_seed_is_idempotent():
    db = Database(seed=None)
    from app.apps.bootstrap import seed_all
    async with db.session() as s:
        await seed_all(s); await seed_all(s); await s.commit()
    async with db.session() as s:
        prof = await store.get_privacy_profile(s, "hospital")
        assert sum(1 for r in prof.recognizers if r.name == "MRN") == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_seed_privacy.py -v`
Expected: FAIL (names still old / no profiles).

- [ ] **Step 3: Implement** — in `app/apps/bootstrap.py`:

Update `SEED_APPS` display names:

```python
SEED_APPS = [
    ("acme", "RAG Agent", "private_cloud_ok", "acme-key"),
    ("hospital", "Hospital Agent", "onprem_only", "hospital-key"),
    ("default", "Support Agent", "any", "demo-key"),
]
```

Add a profile seeder called from `seed_all` (after `seed_demo_apps`). Define the demoted-defaults rule set (built from policy.yaml's rules but LOCATION+DATE_TIME → internal/low/any) and the hospital MRN addition. Make it idempotent by replacing the profile each boot (`replace_privacy_profile` already deletes+inserts, so re-running yields the same single MRN):

```python
_DEMOTED = [
    {"entities": ["US_SSN", "MEDICAL_LICENSE", "US_BANK_NUMBER", "CREDIT_CARD", "EMPLOYEE_ID"],
     "sensitivity": "restricted", "risk_class": "high", "residency": "onprem_only",
     "require_verification": True},
    {"entities": ["PERSON", "EMAIL_ADDRESS", "PHONE_NUMBER"],
     "sensitivity": "confidential", "risk_class": "medium", "residency": "private_cloud_ok",
     "require_verification": False},
    {"entities": ["LOCATION", "DATE_TIME"],
     "sensitivity": "internal", "risk_class": "low", "residency": "any",
     "require_verification": False},
    {"entities": [], "sensitivity": "public", "risk_class": "low", "residency": "any",
     "require_verification": False},
]

async def seed_privacy_profiles(session) -> None:
    for app_id in ("acme", "default"):
        await store.replace_privacy_profile(session, app_id, recognizers=[], rules=_DEMOTED)
    hospital_rules = [{"entities": ["MRN"], "sensitivity": "restricted", "risk_class": "high",
                       "residency": "onprem_only", "require_verification": True}] + _DEMOTED
    await store.replace_privacy_profile(
        session, "hospital",
        recognizers=[{"name": "MRN", "regex": r"\bMRN-\d{7}\b", "score": 0.9}],
        rules=hospital_rules)
```

Call it from `seed_all`:

```python
async def seed_all(session) -> None:
    await seed_demo_apps(session)
    await seed_admin(session)
    await seed_privacy_profiles(session)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_seed_privacy.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/apps/bootstrap.py tests/test_seed_privacy.py
git commit -m "feat(apps): rename demo apps to *Agent + seed per-app privacy profiles"
```

---

### Task 12: 4.1 regression + backward-compat integration test

**Files:**
- Test: `tests/test_router_cheap_tier_regression.py`

**Interfaces:**
- Consumes: the whole stack (mock backend).

- [ ] **Step 1: Write the test** — a clean "capital of France" request on a seeded, LOCATION-demoted app targets the cheap tier; an app with no profile behaves like today's global rules.

```python
# tests/test_router_cheap_tier_regression.py
# Use the existing end-to-end/mock pipeline fixture (see tests/test_pipeline*.py
# or tests/test_router*.py). Assertions:
#  - With profile where LOCATION->internal/low: build_context on "France" gives
#    risk_class low, and Router.select(...) targets tier "cheap" (local-8b).
#  - With profile=None (global rules): same request still targets "mid"
#    (documents the old behavior; proves the fix is profile-driven, not global).
```

Implement concretely against `Router` from `app/routing/router.py` and `build_context`, mirroring the existing router tests' construction of `ExecutionPolicyContext`.

- [ ] **Step 2: Run it**

Run: `.venv/bin/python -m pytest tests/test_router_cheap_tier_regression.py -v`
Expected: PASS.

- [ ] **Step 3: Commit**

```bash
git add tests/test_router_cheap_tier_regression.py
git commit -m "test(routing): 4.1 regression — demoted LOCATION targets cheap tier"
```

---

### Task 13: UI — privacy rules panel in the app Settings tab

**Files:**
- Modify: `ui/src/api.ts` (types + fetchers), the Settings component (`ui/src/console/AppConfig.tsx` or wherever the Settings tab renders — grep for the Headroom tri-state to find it)
- Create: `ui/src/console/PrivacyRulesEditor.tsx`

**Interfaces:**
- Consumes: `GET`/`PUT /api/admin/applications/{id}/privacy` (proxied as `/admin/...` per the nginx config).
- Produces: a panel with a recognizers table, an ordered rules table, a downgrade-warning banner, a "test against sample text" box, and a "Reset to global defaults" button; saves the whole profile via PUT.

- [ ] **Step 1: Add API types + fetchers to `ui/src/api.ts`**

```ts
export interface Recognizer { name: string; regex: string; score: number; }
export interface SensitivityRule {
  entities: string[]; sensitivity: string; risk_class: string;
  residency: string; require_verification: boolean;
}
export interface PrivacyProfile { recognizers: Recognizer[]; rules: SensitivityRule[]; }

export async function getPrivacy(appId: string): Promise<PrivacyProfile> {
  const r = await fetch(`/admin/applications/${appId}/privacy`, {credentials: "include"});
  if (!r.ok) throw new Error(`getPrivacy ${r.status}`);
  return r.json();
}
export async function putPrivacy(appId: string, p: PrivacyProfile): Promise<void> {
  const r = await fetch(`/admin/applications/${appId}/privacy`, {
    method: "PUT", credentials: "include",
    headers: {"Content-Type": "application/json"}, body: JSON.stringify(p)});
  if (!r.ok) throw new Error((await r.json()).detail ?? `putPrivacy ${r.status}`);
}
```

- [ ] **Step 2: Build `PrivacyRulesEditor.tsx`** — controlled tables over `PrivacyProfile` state, load on mount, save via `putPrivacy`. Include:
  - Recognizers table: name / regex / score, add + delete rows.
  - Rules table: reorderable (up/down buttons), entities multi-select (built-ins constant list + custom recognizer names), sensitivity/risk_class/residency `<select>`s (option values are the canonical enums), require-verification checkbox.
  - Downgrade banner: when any rule has `residency === "any"` and its `entities` intersect a `SENSITIVE_LOOKING` set (`["US_SSN","CREDIT_CARD","US_BANK_NUMBER","MEDICAL_LICENSE","PERSON","EMAIL_ADDRESS","PHONE_NUMBER"]`), show an amber warning: "This routes potentially sensitive data off-prem."
  - "Test against sample text": a textarea; on input, call a small client-side highlighter that runs each recognizer regex (wrapped in try/catch) over the text and marks matches. (Client-side preview only; the server remains the source of truth.)
  - "Reset to global defaults": clears `rules` to `[]` and `recognizers` to `[]` and PUTs (server then falls back to global rules).
  - Colour tokens: reuse the amber `#f59e0b` warning colour already used in `RequestDrilldown.tsx`.

- [ ] **Step 3: Mount it** in the Settings tab, below the existing policy/Headroom controls (find the render site by grepping the Settings component for the Headroom tri-state control).

- [ ] **Step 4: Build the UI to verify it compiles**

Run: `cd ui && npm run build`
Expected: type-checks and builds (no TS errors).

- [ ] **Step 5: Commit**

```bash
git add ui/src/api.ts ui/src/console/PrivacyRulesEditor.tsx <the-settings-component>
git commit -m "feat(ui): per-app privacy rules editor (recognizers + rules + downgrade warning)"
```

---

### Task 14: Full-suite check + docs

**Files:**
- Modify: `docs/architecture-review.md` (mark 4.1 closed via this feature), `docs/help/ui-guide.md` (document the Privacy rules panel)

- [ ] **Step 1: Run the whole suite**

Run: `.venv/bin/python -m pytest -q`
Expected: all pass except the pre-existing `test_workflow_validates` env failure (`nat` binary not spawnable) — unrelated.

- [ ] **Step 2: Update docs** — in `docs/architecture-review.md` §4.1, note it is closed by per-app privacy rules (seeded LOCATION demotion). Add a short "Privacy rules" section to `docs/help/ui-guide.md` describing the editor and the downgrade warning.

- [ ] **Step 3: Commit**

```bash
git add docs/architecture-review.md docs/help/ui-guide.md
git commit -m "docs: close 4.1 via per-app privacy rules; document the editor"
```

---

## Self-review

**Spec coverage:**
- §2 guardrail model (full control + `_stricter` backstop) → Tasks 6, 7 (+ test `test_onprem_tenant_downgrade_still_floored`). ✓
- §3.1 tables → Task 2. §3.2 profile + fallback + catch-all → Task 5. §3.3 shared enums → Task 1. §3.4 cache → Tasks 5, 9. ✓
- §4 data flow (effective detector for gate **and** verify) → Task 7; egress custom redaction → Task 8. ✓
- §5 validation + runtime timeout → Tasks 3, 10. ✓
- §6 UI → Task 13. §7 seeding + renames → Task 11. §8 testing → distributed + Task 12. §9 hard rules → Tasks 6/7/8. ✓
- 4.1 origin closed → Tasks 11, 12, 14. ✓

**Placeholder scan:** UI task (13) and two integration tests (10, 12) describe behavior and point at the exact existing fixtures/APIs to reuse rather than inlining full React/fixtures — acceptable because they depend on existing patterns the implementer must read; every backend task has concrete code. No "TODO/handle edge cases" left.

**Type consistency:** `SensitivityRuleView` fields (`entities/sensitivity/risk_class/residency/require_verification`) match `build_context`/`_match_rule` usage (Task 6) and the config `SensitivityRule` attribute names. `EffectiveDetector` implements the `Detector` protocol (`analyze/anonymize/warmup`) used by gate + verify. `get_privacy_profile`/`replace_privacy_profile` signatures match their callers in Tasks 7, 9, 10, 11. `ProfileCache.get/put/clear` match Tasks 9, 10.

**Known adaptation points (call out during execution):** exact test fixtures for the admin client and the DB (`Database(seed=...)` vs conftest fixture) must be taken from the existing suite; `redact()` signature in Task 8 must be read before writing the test; the Settings component filename for Task 13 is found by grepping for the Headroom tri-state.
