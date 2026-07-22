# Phase 5b — Conditional Context Optimization (Headroom) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build step 5 (Headroom compression) behind the `context_optimization` flag so the router prices post-compression tokens, the demo can show a before/after token drop, and the cost dashboard can split savings into two levers (routing vs compression) — with CCR staying local and every agent hop re-entering compression.

**Architecture:** A `Compressor` protocol with a deterministic offline default (`DeterministicCompressor`, used by tests and the laptop demo) and a real `HeadroomCompressor` behind an opt-in `[compression]` extra — mirroring the regex/presidio and mock/NAT sibling pattern. `compress()` orchestrates bypass rules, compressor dispatch, a local TTL-bounded CCR store, failure fallback, and emits a `CompressionRecord`. It sits after the gate and before the router in both the direct path and — the load-bearing fix — inside the per-hop internal endpoint so every agent LLM call re-enters compression.

**Tech Stack:** Python 3.12, Pydantic v2, FastAPI. Real compressor: `headroom-ai` (opt-in extra, lazy import). No new hard deps; the default path is offline and deterministic.

## Global Constraints

- **Compression runs after the gate, never before** — it only ever sees sanitized content (CLAUDE.md rule 7). Both the direct path and the per-hop agent endpoint already receive gate-sanitized messages; do not change that ordering.
- **The router prices `tokens_after`, never `tokens_before`** (CLAUDE.md pitfall). Every routing/pricing call takes the post-compression count.
- **CCR is local-only, TTL-enforced, never exported to telemetry** (CLAUDE.md rule 6). `ccr.residency` must be `local_only`; `ccr.export_to_telemetry` must be `false`. CCR *contents* (originals) never appear in a `CompressionRecord`, a `DecisionRecord`, or any exported span.
- **`ccr_retrievals_to_cloud` must be `0` for `onprem_only`** — the honesty counter. Assert it.
- **Two savings levers tracked separately** (CLAUDE.md demo step 5 / "Don't attribute all savings to routing"): `compression_saved_usd` and `routing_saved_usd` are distinct fields; total savings = compression + routing.
- **Default `context_optimization.enabled: false`** — the demo toggles it on to show before/after. Disabled ⇒ pass-through (`applied=False`, tokens unchanged), which is today's behavior and must stay green.
- **Compression failure falls back to uncompressed; the request still succeeds** (telemetry/optimization is never a hard gate). Fail *open* here — compression is an optimization, not a policy control. (Contrast the gate, which fails closed.)
- **Real `headroom-ai` is opt-in behind the `[compression]` extra, lazily imported.** `app.compression` must import and run with the extra absent (default DeterministicCompressor). Tests must pass offline with no model download.
- **Every agent hop re-enters compression** (CLAUDE.md: "every LLM call re-enters compression (5) and the router (6) — per-call, not per-request").
- Provider/tier/sensitivity canonical values unchanged: providers `onprem|azure|bedrock`; tiers `cheap|mid|frontier`; residency `onprem_only`.
- `count_tokens` is the single tokenizer, from `app.gateway.backends.base` (canonical-tokenizer approximation — do not add a second tokenizer).

---

### Task 1: Config surface — full `context_optimization` block + lint

**Files:**
- Modify: `app/config/schema.py` (add `CCRCfg`, `BypassCfg`, `ContextOptimizationCfg`; replace `context_optimization_enabled: bool` on `ServingCfg`)
- Modify: `app/config/loader.py:50` (build the nested cfg instead of a bare bool)
- Modify: `app/config/lint.py` (two new named checks)
- Modify: any current reader of `settings.serving.context_optimization_enabled` (grep first: `app/main.py`, `app/pipeline.py`)
- Test: `tests/test_config_lint.py`, `tests/test_compression_config.py` (new)

**Interfaces:**
- Consumes: existing `serving.yaml` `context_optimization:` block (enabled, mode, min_tokens, bypass_on{high_risk,restricted_sensitivity,user_prompt}, ccr{enabled,residency,ttl_hours,export_to_telemetry}, cache_alignment).
- Produces:
  - `ContextOptimizationCfg(enabled: bool, mode: str, min_tokens: int, bypass_on: BypassCfg, ccr: CCRCfg, cache_alignment: bool)`
  - `BypassCfg(high_risk: bool, restricted_sensitivity: bool, user_prompt: bool)`
  - `CCRCfg(enabled: bool, residency: str, ttl_hours: int, export_to_telemetry: bool)`
  - `ServingCfg.context_optimization: ContextOptimizationCfg` (the bare bool field is removed; callers read `.context_optimization.enabled`).

- [ ] **Step 1: Write the failing test** — `tests/test_compression_config.py`:

```python
from app.config.loader import load_settings


def test_context_optimization_cfg_surfaced(tmp_path, monkeypatch):
    s = load_settings()  # loads config/*.yaml
    co = s.serving.context_optimization
    assert co.enabled is False           # demo default
    assert co.min_tokens == 2000
    assert co.bypass_on.high_risk is True
    assert co.bypass_on.restricted_sensitivity is True
    assert co.bypass_on.user_prompt is True
    assert co.ccr.residency == "local_only"
    assert co.ccr.export_to_telemetry is False
    assert co.ccr.ttl_hours == 24
    assert co.cache_alignment is True
```

(If `load_settings` needs args, match the existing signature used in `tests/conftest.py` — grep it first and mirror that call.)

- [ ] **Step 2: Run it, verify it fails** — `uv run pytest tests/test_compression_config.py -v` → FAIL (`context_optimization` attribute missing).

- [ ] **Step 3: Add the models to `app/config/schema.py`** (place above `ServingCfg`):

```python
class BypassCfg(BaseModel):
    high_risk: bool
    restricted_sensitivity: bool
    user_prompt: bool


class CCRCfg(BaseModel):
    enabled: bool
    residency: str
    ttl_hours: int
    export_to_telemetry: bool


class ContextOptimizationCfg(BaseModel):
    enabled: bool
    mode: str
    min_tokens: int
    bypass_on: BypassCfg
    ccr: CCRCfg
    cache_alignment: bool
```

Then on `ServingCfg` replace `context_optimization_enabled: bool` with:

```python
    context_optimization: ContextOptimizationCfg
```

- [ ] **Step 4: Build the nested cfg in `app/config/loader.py`** — replace the line at `:50` (`context_optimization_enabled=serving_raw["context_optimization"]["enabled"],`) with:

```python
            context_optimization=ContextOptimizationCfg(
                enabled=co_raw["enabled"],
                mode=co_raw["mode"],
                min_tokens=co_raw["min_tokens"],
                bypass_on=BypassCfg(**co_raw["bypass_on"]),
                ccr=CCRCfg(**co_raw["ccr"]),
                cache_alignment=co_raw["cache_alignment"],
            ),
```

Add `co_raw = serving_raw["context_optimization"]` just above the `ServingCfg(...)` construction, and import the new classes at the top of `loader.py` (`from app.config.schema import ... ContextOptimizationCfg, BypassCfg, CCRCfg`).

- [ ] **Step 5: Update readers** — `grep -rn context_optimization_enabled app/` and replace each read with `...context_optimization.enabled`. (Expected: `app/main.py` and/or `app/pipeline.py`. If a caller only needs the bool today, `.context_optimization.enabled` is the drop-in.)

- [ ] **Step 6: Run the config test** — `uv run pytest tests/test_compression_config.py -v` → PASS.

- [ ] **Step 7: Add the lint checks** — in `app/config/lint.py`, after the existing checks, add a named CCR-honesty check:

```python
    co = serving_raw.get("context_optimization", {})
    ccr = co.get("ccr", {})
    if ccr.get("enabled") and ccr.get("residency") != "local_only":
        raise ConfigLintError(
            "context_optimization.ccr.residency must be 'local_only' "
            f"(CCR is a local cache of originals; got {ccr.get('residency')!r})")
    if ccr.get("enabled") and ccr.get("export_to_telemetry") is not False:
        raise ConfigLintError(
            "context_optimization.ccr.export_to_telemetry must be false "
            "(CCR contents are originals and must never reach telemetry)")
```

(Match how `lint.py` already accesses the raw YAML dicts — grep for `serving_raw` / how other checks read raw config, and mirror it. If lint works on typed Settings rather than raw dicts, read the equivalent typed fields instead.)

- [ ] **Step 8: Add a failing→passing lint test** — in `tests/test_config_lint.py`, add:

```python
def test_lint_rejects_ccr_telemetry_export(base_config_dicts):
    cfg = base_config_dicts()
    cfg["serving"]["context_optimization"]["ccr"]["export_to_telemetry"] = True
    with pytest.raises(ConfigLintError, match="export_to_telemetry"):
        run_lint(cfg)  # match the helper the other lint tests use
```

Mirror the existing lint tests' fixture/helper names exactly (grep `test_config_lint.py` for how a drifted-config case is currently built and asserted; reuse that machinery — do not invent a new one).

- [ ] **Step 9: Run the full suite** — `uv run pytest -q` → all green (was 76). Fix any reader you missed.

- [ ] **Step 10: Commit** — `git add -A && git commit -m "feat(compression): full context_optimization config surface + CCR honesty lint"`

---

### Task 2: `CompressionRecord` model

**Files:**
- Modify: `app/models.py` (add `CompressionRecord`)
- Test: `tests/test_compression_record.py` (new)

**Interfaces:**
- Produces: `CompressionRecord` with exactly the skill's fields:
  `step: str = "compression"`, `enabled: bool`, `applied: bool`, `bypass_reason: str | None`, `tokens_before: int`, `tokens_after: int`, `reduction_pct: float`, `compressor: str | None`, `ccr_stored: bool`, `ccr_retrievals: int`, `ccr_retrievals_to_cloud: int`, `latency_ms: float`.

- [ ] **Step 1: Write the failing test** — `tests/test_compression_record.py`:

```python
from app.models import CompressionRecord


def test_compression_record_defaults_and_dump():
    r = CompressionRecord(enabled=True, applied=True, bypass_reason=None,
                          tokens_before=1000, tokens_after=300,
                          reduction_pct=70.0, compressor="deterministic",
                          ccr_stored=True, ccr_retrievals=0,
                          ccr_retrievals_to_cloud=0, latency_ms=1.2)
    d = r.model_dump()
    assert d["step"] == "compression"
    assert d["tokens_before"] == 1000 and d["tokens_after"] == 300
    assert d["reduction_pct"] == 70.0
    assert "value" not in d and "original" not in d  # no CCR contents ever
```

- [ ] **Step 2: Run it, verify it fails** — `uv run pytest tests/test_compression_record.py -v` → FAIL (import error).

- [ ] **Step 3: Add the model** to `app/models.py` (after `VerificationRecord`):

```python
class CompressionRecord(BaseModel):
    step: str = "compression"
    enabled: bool
    applied: bool
    bypass_reason: str | None = None
    tokens_before: int
    tokens_after: int
    reduction_pct: float = 0.0
    compressor: str | None = None
    ccr_stored: bool = False
    ccr_retrievals: int = 0
    ccr_retrievals_to_cloud: int = 0  # must be 0 for onprem_only — honesty counter
    latency_ms: float = 0.0
```

- [ ] **Step 4: Run the test** → PASS.

- [ ] **Step 5: Commit** — `git commit -am "feat(compression): CompressionRecord model (skill fields, CCR-contents-free)"`

---

### Task 3: `Compressor` protocol + `DeterministicCompressor`

**Files:**
- Create: `app/compression/compressors.py`
- Test: `tests/test_deterministic_compressor.py` (new)

**Interfaces:**
- Consumes: `app.models.Message`, `app.gateway.backends.base.count_tokens`.
- Produces:
  - `class Compressor(Protocol)`: `name: str`; `compress_text(text: str) -> str`.
  - `class DeterministicCompressor`: `name = "deterministic"`; `compress_text` does genuine, deterministic, offline reduction of verbose content (collapse runs of blank lines and repeated whitespace, drop consecutive duplicate lines, and truncate a long repeated-token tail). Idempotent. Never raises on normal text.

**Design note (honesty):** the deterministic compressor performs *real* reduction — it is a simpler algorithm than Headroom's ML compressor, not a faked number. On already-terse text it returns the text ~unchanged (near-0% reduction), which is correct. Verbose tool-output / log / repeated-line content is where it wins, matching the skill's "compress tool outputs and history."

- [ ] **Step 1: Write the failing test** — `tests/test_deterministic_compressor.py`:

```python
from app.compression.compressors import DeterministicCompressor
from app.gateway.backends.base import count_tokens


def test_reduces_verbose_repeated_content():
    c = DeterministicCompressor()
    verbose = ("INFO log line here\n" * 200) + "\n\n\n\n" + ("  padded   spaces  \n" * 50)
    out = c.compress_text(verbose)
    assert count_tokens(out) < count_tokens(verbose)


def test_idempotent():
    c = DeterministicCompressor()
    verbose = "a\n\n\n\nb\nb\nb\n" + ("row\t\tvalue\n" * 30)
    once = c.compress_text(verbose)
    assert c.compress_text(once) == once


def test_terse_text_barely_changes():
    c = DeterministicCompressor()
    terse = "What is 12 times 7?"
    out = c.compress_text(terse)
    assert count_tokens(out) <= count_tokens(terse)
    assert "12" in out and "7" in out  # never drops semantic content of a short prompt
```

- [ ] **Step 2: Run it, verify it fails** — `uv run pytest tests/test_deterministic_compressor.py -v` → FAIL (module missing).

- [ ] **Step 3: Implement** `app/compression/compressors.py`:

```python
"""Compressor implementations. The DeterministicCompressor is the offline,
zero-download default (tests + laptop demo); HeadroomCompressor (Task 5) is the
real ML sibling behind the [compression] extra. Both satisfy `Compressor`."""
from __future__ import annotations

import re
from typing import Protocol


class Compressor(Protocol):
    name: str

    def compress_text(self, text: str) -> str: ...


_MULTI_BLANK = re.compile(r"\n{3,}")
_TRAILING_WS = re.compile(r"[ \t]+(\n)")
_MULTI_SPACE = re.compile(r"[ \t]{2,}")


class DeterministicCompressor:
    """Deterministic, offline reduction of verbose content: collapse blank-line
    runs, strip redundant intra-line whitespace, and drop consecutive duplicate
    lines. Real reduction, no model, no network, idempotent."""

    name = "deterministic"

    def compress_text(self, text: str) -> str:
        text = _MULTI_BLANK.sub("\n\n", text)
        text = _TRAILING_WS.sub(r"\1", text)
        text = _MULTI_SPACE.sub(" ", text)
        out_lines: list[str] = []
        prev: str | None = None
        for line in text.split("\n"):
            if line == prev and line.strip() != "":
                continue  # drop consecutive duplicate non-blank lines
            out_lines.append(line)
            prev = line
        return "\n".join(out_lines)
```

- [ ] **Step 4: Run the tests** → PASS. If `test_idempotent` fails, the regexes aren't fixed-point — adjust so a second pass is a no-op (they are, since collapsed forms don't re-match).

- [ ] **Step 5: Commit** — `git commit -am "feat(compression): Compressor protocol + offline DeterministicCompressor"`

---

### Task 4: CCR local TTL store

**Files:**
- Create: `app/compression/ccr.py`
- Test: `tests/test_ccr_store.py` (new)

**Interfaces:**
- Produces:
  - `class CCRStore`: constructed `CCRStore(ttl_seconds: int, clock: Callable[[], float] = time.monotonic)` (inject clock for deterministic TTL tests).
    - `store(original: str) -> str` — returns an opaque `ccr_id`, caches the original locally, stamps insert time. Increments nothing.
    - `retrieve(ccr_id: str, *, to_cloud: bool) -> str | None` — returns the original if present and unexpired, else `None` (expired entries are evicted). Increments `retrievals`; increments `retrievals_to_cloud` iff `to_cloud`.
    - properties `retrievals: int`, `retrievals_to_cloud: int`, `size: int`.
- **Invariant:** the store never serializes its contents into any record; callers pass only counters/booleans into `CompressionRecord`.

- [ ] **Step 1: Write the failing test** — `tests/test_ccr_store.py`:

```python
from app.compression.ccr import CCRStore


def test_store_and_retrieve_local():
    t = [1000.0]
    s = CCRStore(ttl_seconds=60, clock=lambda: t[0])
    cid = s.store("original secret-free but full text")
    assert s.retrieve(cid, to_cloud=False) == "original secret-free but full text"
    assert s.retrievals == 1
    assert s.retrievals_to_cloud == 0


def test_ttl_expiry_evicts():
    t = [0.0]
    s = CCRStore(ttl_seconds=10, clock=lambda: t[0])
    cid = s.store("x")
    t[0] = 11.0
    assert s.retrieve(cid, to_cloud=False) is None
    assert s.size == 0  # expired entry evicted


def test_to_cloud_counter():
    s = CCRStore(ttl_seconds=60, clock=lambda: 0.0)
    cid = s.store("x")
    s.retrieve(cid, to_cloud=True)
    assert s.retrievals == 1 and s.retrievals_to_cloud == 1
```

- [ ] **Step 2: Run it, verify it fails** — → FAIL (module missing).

- [ ] **Step 3: Implement** `app/compression/ccr.py`:

```python
"""CCR (Conditional Context Retrieval) — a LOCAL, TTL-bounded cache of
compression originals. Hard rules (CLAUDE.md rule 6 / headroom skill):
local-only, TTL-enforced, contents NEVER exported to telemetry, and for
`onprem_only` requests `retrievals_to_cloud` must always stay 0 (enforced by
the caller, which never retrieves to_cloud for onprem_only)."""
from __future__ import annotations

import time
import uuid
from typing import Callable


class CCRStore:
    def __init__(self, ttl_seconds: int,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self._ttl = ttl_seconds
        self._clock = clock
        self._data: dict[str, tuple[str, float]] = {}
        self._retrievals = 0
        self._retrievals_to_cloud = 0

    def store(self, original: str) -> str:
        cid = uuid.uuid4().hex
        self._data[cid] = (original, self._clock())
        return cid

    def retrieve(self, ccr_id: str, *, to_cloud: bool) -> str | None:
        entry = self._data.get(ccr_id)
        if entry is None:
            return None
        original, stamped = entry
        if self._clock() - stamped > self._ttl:
            del self._data[ccr_id]  # enforce TTL: evict on access
            return None
        self._retrievals += 1
        if to_cloud:
            self._retrievals_to_cloud += 1
        return original

    @property
    def retrievals(self) -> int:
        return self._retrievals

    @property
    def retrievals_to_cloud(self) -> int:
        return self._retrievals_to_cloud

    @property
    def size(self) -> int:
        return len(self._data)
```

- [ ] **Step 4: Run the tests** → PASS.

- [ ] **Step 5: Commit** — `git commit -am "feat(compression): local TTL-bounded CCR store with honesty counters"`

---

### Task 5: `compress()` orchestrator + `HeadroomCompressor` (opt-in extra)

**Files:**
- Rewrite: `app/compression/__init__.py`
- Modify: `pyproject.toml` (add `[compression]` extra: `headroom-ai`)
- Test: `tests/test_compress_orchestrator.py` (new); update any existing test that imports `compress`/`CompressionResult`.

**Interfaces:**
- Consumes: `ContextOptimizationCfg` (Task 1), `Message`, `ExecutionPolicyContext`, `CompressionRecord` (Task 2), `DeterministicCompressor` (Task 3), `CCRStore` (Task 4), `count_tokens`.
- Produces:
  - `@dataclass class CompressionResult: messages: list[Message]; tokens_before: int; tokens_after: int; record: CompressionRecord` (record is now a typed model; callers that did `{"step":"compression", **comp.record}` change to `comp.record.model_dump()` — update pipeline in Task 6).
  - `def compress(messages, ctx, cfg: ContextOptimizationCfg, *, ccr: CCRStore | None = None) -> CompressionResult`
  - `def select_compressor(cfg) -> Compressor` — returns `HeadroomCompressor()` when `cfg.mode == "library"` **and** headroom importable, else `DeterministicCompressor()`. Lazy import of headroom; ImportError → DeterministicCompressor (never crash).
- **Bypass order (first match wins), recorded in `bypass_reason`:**
  1. `not cfg.enabled` → `"disabled"`
  2. `cfg.bypass_on.high_risk and ctx.risk_class == "high"` → `"high_risk"`
  3. `cfg.bypass_on.restricted_sensitivity and ctx.sensitivity == "restricted"` → `"restricted_sensitivity"` (unless CCR residency verified — for the prototype, treat restricted as always bypass; simpler and safe)
  4. `tokens_before < cfg.min_tokens` → `"below_min_tokens"`
  - `cfg.bypass_on.user_prompt`: the compressor only compresses non-terminal (tool/assistant/history) messages; the live user prompt (last message) is never compressed. Implement by compressing all messages *except* the last when `user_prompt` is true.
- **Failure fallback:** any exception from the compressor → return the uncompressed messages with `applied=False`, `bypass_reason="compressor_error"`, `tokens_after == tokens_before`, and log it. The request must still succeed.
- **CCR:** when `applied` and `cfg.ccr.enabled` and a `ccr` store is provided, store each compressed message's original locally, set `ccr_stored=True`. Retrieval-into-cloud is not exercised in the single-shot path (retrievals stay 0); the counter plumbing exists for the multi-step/cloud story. For `onprem_only`, never retrieve `to_cloud` (caller guarantees; `ccr_retrievals_to_cloud` stays 0).

- [ ] **Step 1: Write the failing tests** — `tests/test_compress_orchestrator.py`:

```python
import pytest
from app.compression import compress
from app.compression.ccr import CCRStore
from app.config.loader import load_settings
from app.models import ExecutionPolicyContext, Message


def _ctx(**over):
    base = dict(request_id="r", tenant_id="acme", sensitivity="internal",
                onprem_only=False, allowed_providers=frozenset({"onprem"}),
                allowed_models=frozenset(), max_cost_usd=1.0, max_latency_ms=10000,
                risk_class="low", require_verification=False,
                detected_entities=(), redaction_map_id=None)
    base.update(over)
    return ExecutionPolicyContext(**base)


def _cfg(**over):
    co = load_settings().serving.context_optimization
    return co.model_copy(update=over)


def _msgs(body):
    return [Message(role="user", content=body)]


def test_disabled_is_passthrough():
    cfg = _cfg(enabled=False)
    r = compress(_msgs("x" * 50), _ctx(), cfg)
    assert r.record.applied is False
    assert r.record.bypass_reason == "disabled"
    assert r.tokens_after == r.tokens_before


def test_below_min_tokens_bypassed():
    cfg = _cfg(enabled=True, min_tokens=100000)
    r = compress(_msgs("short"), _ctx(), cfg)
    assert r.record.applied is False
    assert r.record.bypass_reason == "below_min_tokens"


def test_high_risk_bypassed():
    cfg = _cfg(enabled=True, min_tokens=1)
    r = compress(_msgs("INFO line\n" * 500), _ctx(risk_class="high"), cfg)
    assert r.record.applied is False
    assert r.record.bypass_reason == "high_risk"


def test_applies_and_reduces_history():
    cfg = _cfg(enabled=True, min_tokens=1)
    cfg = cfg.model_copy(update={"bypass_on": cfg.bypass_on.model_copy(update={"user_prompt": False})})
    verbose = "INFO log line\n" * 800
    r = compress([Message(role="assistant", content=verbose)], _ctx(), cfg)
    assert r.record.applied is True
    assert r.tokens_after < r.tokens_before
    assert r.record.reduction_pct > 0


def test_onprem_only_ccr_never_to_cloud():
    cfg = _cfg(enabled=True, min_tokens=1)
    cfg = cfg.model_copy(update={"bypass_on": cfg.bypass_on.model_copy(update={"user_prompt": False})})
    ccr = CCRStore(ttl_seconds=60, clock=lambda: 0.0)
    r = compress([Message(role="assistant", content="INFO\n" * 800)],
                 _ctx(onprem_only=True), cfg, ccr=ccr)
    assert r.record.ccr_retrievals_to_cloud == 0
    assert ccr.retrievals_to_cloud == 0
```

- [ ] **Step 2: Run, verify failure** — → FAIL.

- [ ] **Step 3: Rewrite** `app/compression/__init__.py`:

```python
"""Step 5 — Conditional Context Optimization (Headroom). Runs AFTER the gate
(sees only sanitized content) and BEFORE the router (which prices tokens_after).
Default compressor is offline/deterministic; real headroom-ai is opt-in behind
the [compression] extra. Fails OPEN — compression is an optimization, so a
compressor error falls back to uncompressed and the request still succeeds."""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass

from app.compression.ccr import CCRStore
from app.compression.compressors import Compressor, DeterministicCompressor
from app.config.schema import ContextOptimizationCfg
from app.gateway.backends.base import count_tokens
from app.models import CompressionRecord, ExecutionPolicyContext, Message

log = logging.getLogger("serving.compression")


@dataclass
class CompressionResult:
    messages: list[Message]
    tokens_before: int
    tokens_after: int
    record: CompressionRecord


def select_compressor(cfg: ContextOptimizationCfg) -> Compressor:
    if cfg.mode == "library":
        try:
            from app.compression.headroom import HeadroomCompressor
            return HeadroomCompressor()
        except Exception:  # extra not installed / model unavailable → offline default
            return DeterministicCompressor()
    return DeterministicCompressor()


def _bypass(cfg, ctx, tokens_before) -> str | None:
    if not cfg.enabled:
        return "disabled"
    if cfg.bypass_on.high_risk and ctx.risk_class == "high":
        return "high_risk"
    if cfg.bypass_on.restricted_sensitivity and ctx.sensitivity == "restricted":
        return "restricted_sensitivity"
    if tokens_before < cfg.min_tokens:
        return "below_min_tokens"
    return None


def compress(messages: list[Message], ctx: ExecutionPolicyContext,
             cfg: ContextOptimizationCfg, *,
             ccr: CCRStore | None = None) -> CompressionResult:
    t0 = time.monotonic()
    tokens_before = sum(count_tokens(m.content) for m in messages)

    reason = _bypass(cfg, ctx, tokens_before)
    if reason is not None:
        rec = CompressionRecord(enabled=cfg.enabled, applied=False,
                                bypass_reason=reason, tokens_before=tokens_before,
                                tokens_after=tokens_before, reduction_pct=0.0,
                                latency_ms=(time.monotonic() - t0) * 1000)
        return CompressionResult(messages, tokens_before, tokens_before, rec)

    # Compress history/tool output; never the live user prompt (last message)
    # when user_prompt bypass is set.
    protect_last = cfg.bypass_on.user_prompt
    compressor = select_compressor(cfg)
    ccr_stored = False
    try:
        out: list[Message] = []
        for i, m in enumerate(messages):
            is_last = i == len(messages) - 1
            if protect_last and is_last:
                out.append(m)
                continue
            compressed = compressor.compress_text(m.content)
            if ccr is not None and cfg.ccr.enabled and compressed != m.content:
                ccr.store(m.content)  # local original, TTL-bounded, never telemetry
                ccr_stored = True
            out.append(Message(role=m.role, content=compressed))
    except Exception:
        log.exception("compression failed; falling back to uncompressed")
        rec = CompressionRecord(enabled=cfg.enabled, applied=False,
                                bypass_reason="compressor_error",
                                tokens_before=tokens_before, tokens_after=tokens_before,
                                compressor=None, latency_ms=(time.monotonic() - t0) * 1000)
        return CompressionResult(messages, tokens_before, tokens_before, rec)

    tokens_after = sum(count_tokens(m.content) for m in out)
    reduction = 0.0 if tokens_before == 0 else max(
        0.0, (tokens_before - tokens_after) / tokens_before * 100)
    applied = tokens_after < tokens_before
    rec = CompressionRecord(
        enabled=cfg.enabled, applied=applied,
        bypass_reason=None if applied else "no_reduction",
        tokens_before=tokens_before, tokens_after=tokens_after,
        reduction_pct=round(reduction, 2),
        compressor=compressor.name if applied else None,
        ccr_stored=ccr_stored, ccr_retrievals=0, ccr_retrievals_to_cloud=0,
        latency_ms=(time.monotonic() - t0) * 1000)
    return CompressionResult(out, tokens_before, tokens_after, rec)
```

- [ ] **Step 4: Add the extra** to `pyproject.toml` (mirror the `[presidio]`/`[agent]` extras' style):

```toml
[project.optional-dependencies]
# ... existing extras ...
compression = ["headroom-ai>=0.1"]
```

(Do NOT add `headroom-ai` to the core deps — the default path is offline.)

- [ ] **Step 5: Run** `uv run pytest tests/test_compress_orchestrator.py -v` → PASS. Then `uv run pytest -q` — fix any existing test/caller that imported the old dict-record `compress` (the pipeline is updated in Task 6; if a test breaks now, it's because it read `comp.record["..."]` — leave the pipeline change for Task 6 but if a *unit* test of compress asserted the old dict shape, update it to the model).

- [ ] **Step 6: Commit** — `git commit -am "feat(compression): compress() orchestrator (bypass rules, CCR, fail-open) + [compression] extra"`

---

### Task 5b: `HeadroomCompressor` (real sibling behind the extra)

**Files:**
- Create: `app/compression/headroom.py`
- Test: `tests/test_headroom_compressor.py` (new, `importorskip`)

**Interfaces:**
- Produces: `class HeadroomCompressor` — `name = "headroom"`; `compress_text(text)` calls the real `headroom` library. Lazy import at construction; if `headroom` missing, `__init__` raises ImportError (so `select_compressor` falls back).

- [ ] **Step 1: Write the guarded test** — `tests/test_headroom_compressor.py`:

```python
import pytest

pytest.importorskip("headroom")  # skip when [compression] extra absent


def test_headroom_compressor_conforms():
    from app.compression.headroom import HeadroomCompressor
    c = HeadroomCompressor()
    assert c.name == "headroom"
    out = c.compress_text("INFO log line\n" * 500)
    assert isinstance(out, str)
```

- [ ] **Step 2: Run it** — with the extra absent → SKIPPED (not failed). `uv run pytest tests/test_headroom_compressor.py -v`.

- [ ] **Step 3: Implement** `app/compression/headroom.py`:

```python
"""Real Headroom ML compressor — opt-in behind the [compression] extra.
Lazy: __init__ raises ImportError when headroom-ai isn't installed, so
select_compressor() cleanly falls back to the deterministic default."""
from __future__ import annotations


class HeadroomCompressor:
    name = "headroom"

    def __init__(self) -> None:
        from headroom import compress as _hr_compress  # ImportError if absent
        self._compress = _hr_compress

    def compress_text(self, text: str) -> str:
        result = self._compress(text)
        # headroom returns a result object/string depending on version; normalize
        return getattr(result, "text", None) or getattr(result, "compressed", None) or str(result)
```

**Note for implementer:** confirm the real `headroom.compress` return shape against the installed version if the extra is present; the `getattr` chain is a defensive normalizer for the demo. If the extra is absent (default), you cannot verify the exact API — leave the defensive normalizer and the `importorskip` test; the real-API confirmation happens on the GPU laptop where the extra is installed.

- [ ] **Step 4: Run** `uv run pytest -q` → HeadroomCompressor test SKIPPED, rest green.

- [ ] **Step 5: Commit** — `git commit -am "feat(compression): real HeadroomCompressor behind [compression] extra (lazy, fallback-safe)"`

---

### Task 6: Pipeline two-lever cost accounting (direct path)

**Files:**
- Modify: `app/pipeline.py` (direct path in `run()`; construct/hold a `CCRStore`; pass `cfg`; two-lever cost)
- Modify: `app/main.py` (pass `settings.serving.context_optimization` into the pipeline; construct app-level `CCRStore` from `ccr.ttl_hours`)
- Test: `tests/test_two_lever_cost.py` (new); update `tests/test_e2e_slice.py` cost assertions if they read `cost` keys.

**Interfaces:**
- Consumes: `compress(messages, ctx, cfg, ccr=...)` now returns a typed `CompressionResult`; `comp.record` is a `CompressionRecord` (use `.model_dump()`).
- Produces: the response `cost` dict gains a `compression_saved_usd` field and keeps `routing_saved_usd`; `baseline_usd` = baseline priced on **tokens_before** (the true all-frontier baseline before either lever). Definitions:
  - `baseline_usd = self._baseline(comp.tokens_before)`
  - `baseline_after = self._baseline(comp.tokens_after)`
  - `compression_saved_usd = max(0.0, baseline_usd - baseline_after)`
  - `routing_saved_usd = max(0.0, baseline_after - total)`
  - (total savings = compression_saved + routing_saved = baseline_usd − total)

- [ ] **Step 1: Write the failing test** — `tests/test_two_lever_cost.py`:

```python
# Build a pipeline with compression enabled and a verbose message; assert the
# cost dict splits savings into the two levers and they sum to total savings.
# Mirror how tests/test_e2e_slice.py constructs a Pipeline + ChatRequest.
from app.models import ChatRequest, Message


async def _run(pipeline, content):
    return await pipeline.run("req-1", "acme",
                              ChatRequest(messages=[Message(role="user", content=content)]))


def test_cost_dict_has_two_levers(compression_on_pipeline):  # fixture: co.enabled=True, min_tokens=1, user_prompt=False
    import asyncio
    out = asyncio.run(_run(compression_on_pipeline, "INFO log line\n" * 800))
    cost = out["cost"]
    assert "compression_saved_usd" in cost and "routing_saved_usd" in cost
    total_saved = cost["baseline_usd"] - cost["total_usd"]
    assert abs((cost["compression_saved_usd"] + cost["routing_saved_usd"]) - total_saved) < 1e-9
    assert cost["compression_saved_usd"] > 0  # compression actually reduced tokens
```

Add a `compression_on_pipeline` fixture to `tests/conftest.py` that builds the same Pipeline the existing e2e fixture builds but with `context_optimization` overridden to `enabled=True, min_tokens=1, bypass_on.user_prompt=False`. Grep `conftest.py` for the existing pipeline/settings fixture and mirror it (use `settings.model_copy`/`serving.model_copy` to override just the cfg).

- [ ] **Step 2: Run, verify failure** — → FAIL (`compression_saved_usd` missing / fixture missing).

- [ ] **Step 3: Update `app/pipeline.py`** — inject cfg + CCR, use typed record, two-lever cost.
  - Change `__init__` to accept `cfg: ContextOptimizationCfg` and `ccr: CCRStore` (store as `self._co_cfg`, `self._ccr`).
  - In `run()`: `comp = compress(san_messages, ctx, self._co_cfg, ccr=self._ccr)`.
  - Replace `trace.append({"step": "compression", **comp.record})` with `trace.append(comp.record.model_dump())` (both direct and multi-step paths).
  - Replace the direct-path `cost` block with:

```python
        total = sum(r.actual_cost_usd or 0.0 for r in records)
        baseline_usd = self._baseline(comp.tokens_before)
        baseline_after = self._baseline(comp.tokens_after)
        compression_saved = max(0.0, baseline_usd - baseline_after)
        routing_saved = max(0.0, baseline_after - total)
        # ...
        "cost": {"total_usd": total, "baseline_usd": baseline_usd,
                 "routing_saved_usd": routing_saved,
                 "compression_saved_usd": compression_saved},
```

- [ ] **Step 4: Update `app/main.py`** — construct `ccr = CCRStore(ttl_seconds=settings.serving.context_optimization.ccr.ttl_hours * 3600)` and pass `cfg=settings.serving.context_optimization, ccr=ccr` into `Pipeline(...)`. Import `CCRStore` from `app.compression.ccr`.

- [ ] **Step 5: Run** `uv run pytest tests/test_two_lever_cost.py -v` → PASS.

- [ ] **Step 6: Run full suite** — `uv run pytest -q`. Update `tests/test_e2e_slice.py` if it asserted an exact `baseline_usd` that now prices tokens_before with compression off — **with compression off, tokens_before == tokens_after**, so `baseline_usd` is unchanged and `compression_saved_usd == 0`; existing assertions should still hold. Fix only genuine breaks.

- [ ] **Step 7: Commit** — `git commit -am "feat(compression): two-lever cost split (routing vs compression) in direct path"`

---

### Task 7: M3 — agent hops re-enter compression + wiring + smoke

**Files:**
- Modify: `app/agent/internal_endpoint.py` (compress each hop's messages before pricing/routing; store per-hop CompressionRecords)
- Modify: `app/main.py` (stash `context_optimization` cfg + CCR on `app.state` so the internal endpoint can reach them)
- Modify: `app/pipeline.py` `_run_multi_step` (aggregate per-hop compression into the trace + two-lever cost)
- Create: `scripts/smoke-compression.sh`
- Test: `tests/test_agent_hop_compression.py` (new, uses the `live_internal_server`/TestClient machinery from Task-5 Phase-5 tests)

**Interfaces:**
- Consumes: `compress()`, `ContextOptimizationCfg`, `CCRStore`, `HOPS`.
- The internal endpoint currently prices `tokens_after = sum(count_tokens(m["content"]) ...)` on the **raw** hop messages. Change it to run `compress()` first so `tokens_priced` is the **post-compression** count — matching the direct path and CLAUDE.md's "every LLM call re-enters compression (5)."
- Add a `CompressionRecord` per hop to the `HopCollector` run state so the multi-step trace shows compression per hop.

- [ ] **Step 1: Write the failing test** — `tests/test_agent_hop_compression.py`. Drive a multi-step request through the pipeline with compression enabled and a verbose injected tool/history message; assert at least one `agent_hop` record's `tokens_priced` is less than the raw hop token count, i.e. compression ran per hop. Mirror `tests/` Phase-5 multi-step test setup (grep for `mock_multistep` / how multi-step is triggered and how hops are asserted). Concretely:

```python
# Assert the internal endpoint prices post-compression tokens per hop.
from app.agent.internal_endpoint import HOPS
from app.models import ExecutionPolicyContext


def test_hop_prices_post_compression(internal_client_with_compression_on):
    client, run_id = internal_client_with_compression_on  # fixture: HOPS.start + cfg on app.state
    verbose = "INFO log line\n" * 800
    body = {"messages": [{"role": "assistant", "content": verbose},
                         {"role": "user", "content": "summarize"}],
            "tools": []}
    resp = client.post(f"/internal/v1/{run_id}/chat/completions", json=body)
    assert resp.status_code == 200
    recs = HOPS.pop(run_id)
    assert recs and recs[0].tokens_priced < 800  # far below the raw ~800+ token count
```

Add the `internal_client_with_compression_on` fixture to `conftest.py`: build the app (TestClient) with `context_optimization` enabled (min_tokens=1, user_prompt=False) on `app.state`, then `HOPS.start(run_id, ctx)` with a non-onprem ctx. Reuse the loopback-allowed `testclient` host.

- [ ] **Step 2: Run, verify failure** — → FAIL (hop prices raw tokens).

- [ ] **Step 3: Stash cfg on `app.state`** in `app/main.py`: `app.state.co_cfg = settings.serving.context_optimization` and `app.state.ccr = ccr` (reuse the Task-6 store).

- [ ] **Step 4: Update `app/agent/internal_endpoint.py`** — inside `internal_chat_completions`, after loading `ctx` and before pricing:

```python
    from app.compression import compress  # local import: avoids import cycle at module load

    co_cfg = request.app.state.co_cfg
    ccr = getattr(request.app.state, "ccr", None)
    hop_msgs = [Message(role=m.get("role", "user"), content=m["content"])
                for m in messages if m.get("content")]
    comp = compress(hop_msgs, ctx, co_cfg, ccr=ccr)
    tokens_after = comp.tokens_after
    complexity = min(1.0, tokens_after / 2000)
    decision = model_router.select(ctx, tokens_after, complexity, None)
```

(Import `Message` at the top of the module.) Keep the rest (record, SSE) the same; add `HOPS.record_compression(run_id, comp.record)` — see Step 5 — so per-hop compression is visible. The SSE step decision (`decide_agent_step`) still runs on the original `messages` (tool-call detection needs the raw role structure); only pricing/routing use the compressed count.

- [ ] **Step 5: Extend `HopCollector`** in `internal_endpoint.py` to also collect per-hop compression records:

```python
    # in _RunState:
    compressions: list[CompressionRecord] = field(default_factory=list)

    # on HopCollector:
    def record_compression(self, run_id: str, rec: CompressionRecord) -> None:
        self._runs[run_id].compressions.append(rec)

    def pop_compressions(self, run_id: str) -> list[CompressionRecord]:
        state = self._runs.get(run_id)
        return list(state.compressions) if state else []
```

Import `CompressionRecord`. Note `pop()` currently deletes the run; ensure the runtime drains compressions **before** `pop()` (or have `pop()` return both). Simplest: add `pop_all(run_id) -> tuple[list[DecisionRecord], list[CompressionRecord]]` and use it in the runtime; keep `pop()` for back-compat. Grep `app/agent/runtime.py` for where `HOPS.pop` is called and update it to also retrieve compressions, threading them onto `AgentResult` (add `compressions: list[CompressionRecord]` to `AgentResult`).

- [ ] **Step 6: Aggregate in `_run_multi_step`** (`app/pipeline.py`) — replace the single outer `comp` with the per-hop compressions from `result.compressions`:
  - Append each hop compression record to the trace: `for cr in result.compressions: trace.append(cr.model_dump())` (instead of the single outer `comp`).
  - Two-lever cost for multi-step: `baseline_usd = sum(self._baseline(h.tokens_priced) for h in result.hops)` stays as the post-compression baseline; add `baseline_before = sum(self._baseline(cr.tokens_before) for cr in result.compressions)` and set `compression_saved_usd = max(0.0, baseline_before - baseline_usd)`, `routing_saved_usd = max(0.0, baseline_usd - total)`, and report `baseline_usd = baseline_before` in the response cost (so the headline baseline is pre-both-levers, consistent with the direct path). Keep `total` as-is.
  - The outer `compress()` call in `run()` for the multi-step branch becomes unnecessary for pricing (each hop compresses itself); still pass `san_messages` to the agent. Remove the now-dead outer `comp` usage in `_run_multi_step` if it's only used for the trace/cost (replaced above). Verify no other reader depends on it.

- [ ] **Step 7: Run** `uv run pytest tests/test_agent_hop_compression.py -v` → PASS. Then `uv run pytest -q` → all green.

- [ ] **Step 8: Write `scripts/smoke-compression.sh`** — boot the server twice (or toggle via env `SP_CONTEXT_OPT=on/off`), send the same verbose multi-step request, and assert (with httpx/jq): compression OFF → `compression_saved_usd == 0`; compression ON → `tokens_after < tokens_before` in the compression trace record and `compression_saved_usd > 0`; and `ccr_retrievals_to_cloud == 0` for an onprem_only (SSN) request. Mirror `scripts/smoke-agent.sh` structure (env override, health-wait, assertions, `grep -v` any token). Make it executable (`chmod +x`).

- [ ] **Step 9: Commit** — `git add -A && git commit -m "feat(compression): agent hops re-enter compression (per-hop pricing + two-lever multi-step cost) + smoke"`

---

## Self-Review notes (controller)

- **Spec coverage** vs headroom skill "Tests" list: off→passthrough (T5/T6), large output→reduction (T3/T5), short→below_min_tokens (T5), high_risk→bypass (T5), router scores tokens_after (T6/T7), onprem_only CCR local + to_cloud==0 (T4/T5/T7), CCR absent from spans (structural — CompressionRecord has no contents field, T2 asserts; export.py already types-only), compression failure→fallback (T5). All covered.
- **CLAUDE.md two-lever** covered T6/T7. **Rule 7 ordering** unchanged (compress after gate). **M3** is T7.
- **Type consistency:** `CompressionResult.record` is `CompressionRecord` everywhere after T5; every `comp.record` consumer uses `.model_dump()`. `select_compressor`/`Compressor.name` consistent across T3/T5/T5b.
- **Deferred (YAGNI):** real CCR retrieval-into-cloud flow (counter plumbing exists, retrievals stay 0 in single-shot); CacheAligner metric surfacing (cfg present, UI shows it in Phase 6); sidecar `proxy` mode (cfg present, library mode is the built path).
