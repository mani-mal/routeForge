# P2a Routing Signal Measurement Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the router's latency, availability and cost terms measure what their names claim, and put every scoring term's value *and* provenance into the decision record.

**Architecture:** A new in-process `SignalStore` (`app/routing/signals.py`) holds per-model time-decayed EWMAs for latency and availability, seeded at boot from `usage_records` and updated by the gateway. It is injected into the step-6 *strategy* — never into eligibility. One shared `price_call` replaces three copies of the cost formula and prices cached prompt tokens. The scoring formula is rewritten so that, with an empty store, it produces numerically identical scores to today's.

**Tech Stack:** Python 3.12, Pydantic v2, SQLAlchemy 2 async, FastAPI, pytest, Hypothesis.

**Spec:** `docs/superpowers/specs/2026-09-19-routing-signal-measurement-p2a-design.md`

## Global Constraints

Copied verbatim from the spec. Every task's requirements implicitly include these.

- **Eligibility never consults a signal.** `feasible_set` (`app/policy/feasible.py`) remains the one authority on admissibility. A `RoutingStrategy` ranks; it never filters in or out. `ordered` must contain exactly the members of `eligible`, reordered.
- **CLAUDE.md rule 3 (`onprem_only` absolute) and rule 8 (residency beats the risk floor) are untouched.** Both are enforced before the strategy is called; no task may move filtering into the strategy.
- **No PII.** Signal values are model ids, floats and enum strings. The scoring record is emitted regardless of `capture_io`, exactly like `sp.candidates`, and runs through the same defensive egress redaction at emit time.
- **The identity property.** With an empty `SignalStore`, the new scoring formula must produce **numerically identical scores** — not merely identical ordering — to the current one. Defaults are chosen for this: `tier_latency_ms = {cheap: 200, mid: 500, frontier: 1000}`, `latency_scale_ms = 1000`, unmeasured availability `1.0`.
- **Latency normalizes against a fixed `latency_scale_ms`, never the candidate-set max.** Cost keeps set-max normalization. Do not "unify" them.
- **RouteForge never invents a discount.** `PriceCfg.cached_input is None` ⇒ cached tokens bill at the full `input` rate.
- **The router's cost estimate assumes zero cached tokens.** Estimate high, record true.
- **Availability is a ranking preference, never admission control.** It never removes a candidate. Failover remains the only recovery mechanism.
- **Every new DB column is nullable with no default**, matching `scripts/migrate_app_policies.py`'s stated invariant. Readers coerce `NULL` to `0` / "undeclared".
- Run tests with `LITELLM_LOCAL_MODEL_COST_MAP=True`. `pyproject` sets `addopts="-q"`; add `-o addopts=""` when you need counts. One pre-existing failure (`presidio_analyzer` not installed) is expected and unrelated.
- The `rtk` hook mangles some shell: use `/usr/bin/grep`, `/usr/bin/npx`, and `rtk proxy git ...`.

---

## File Structure

| File | Responsibility | Task |
|---|---|---|
| `app/routing/pricing.py` *(new)* | `price_call` — the one cost formula | 1 |
| `app/config/schema.py` | `PriceCfg.cached_input`; `SignalsCfg`; `weights` alias | 1, 5 |
| `app/routing/router.py` | `_est_cost` via `price_call`; thread `RankResult` | 1, 7 |
| `app/gateway/backends/litellm_backend.py` | `price_call`; capture `cached_tokens` | 1, 2 |
| `app/models.py` | `GenerationResult.cached_prompt_tokens`; `DecisionRecord.scoring` | 2, 8 |
| `app/apps/models.py` | `UsageRecord.cached_prompt_tokens`; `OnboardedModel.price_cached_input` | 2, 3 |
| `app/apps/store.py` | `record_usage` kwarg; onboard field list | 2, 3 |
| `app/apps/registry.py` | carry `price_cached_input` into `PriceCfg` | 3 |
| `app/api/admin_routes.py` | onboard/edit round-trip for `price_cached_input` | 3 |
| `scripts/migrate_app_policies.py` | two new columns, third table block | 2, 3 |
| `app/routing/signals.py` *(new)* | `SignalStore`, `SignalValue`, EWMA + decay | 4 |
| `app/config/lint.py` | `tier_latency_ms` coverage; `availability` weight | 5 |
| `app/routing/strategy.py` | `TermValue`/`ScoredCandidate`/`RankResult`; new formula | 6 |
| `app/gateway/gateway.py` | observe success/error; failure classification | 7 |
| `app/main.py` | boot seeding | 7 |
| `app/obs/export.py` | `sp.scoring` per router hop | 8 |
| `app/pipeline.py` | chosen-candidate terms on the response certificate | 8 |
| `docs/research/routing-signal-provenance.md` *(new)* | research artifact | 9 |

---

## Task 1: The pricing seam

**Files:**
- Create: `app/routing/pricing.py`
- Modify: `app/config/schema.py` (`PriceCfg`, ~line 108)
- Modify: `app/routing/router.py:97` (`Router._est_cost`)
- Modify: `app/gateway/backends/litellm_backend.py:140` and `:202`
- Test: `tests/routing/test_pricing.py` (new)

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces: `app.routing.pricing.price_call(price: PriceCfg, *, prompt_tokens: int, completion_tokens: int, cached_prompt_tokens: int = 0) -> float`; `PriceCfg.cached_input: float | None`.

- [ ] **Step 1: Write the failing test**

Create `tests/routing/test_pricing.py`:

```python
import pytest

from app.config.schema import PriceCfg
from app.routing.pricing import price_call


def _p(**kw) -> PriceCfg:
    base = {"input": 3.0, "output": 15.0, "tier": "frontier", "provider": "bedrock"}
    base.update(kw)
    return PriceCfg(**base)


def _legacy(price, tokens_in, tokens_out):
    """The formula as it existed in three places before this task."""
    return (tokens_in * price.input + tokens_out * price.output) / 1_000_000


def test_no_cached_rate_declared_matches_the_legacy_formula_exactly():
    price = _p()
    assert price_call(price, prompt_tokens=1000, completion_tokens=200) == \
        _legacy(price, 1000, 200)


def test_cached_tokens_bill_at_full_input_rate_when_no_rate_declared():
    """RouteForge never invents a discount."""
    price = _p()
    assert price_call(price, prompt_tokens=1000, completion_tokens=200,
                      cached_prompt_tokens=800) == _legacy(price, 1000, 200)


def test_declared_cached_rate_applies_to_the_cached_portion_only():
    price = _p(cached_input=0.30)
    got = price_call(price, prompt_tokens=1000, completion_tokens=200,
                     cached_prompt_tokens=800)
    expected = (200 * 3.0 + 800 * 0.30 + 200 * 15.0) / 1_000_000
    assert got == pytest.approx(expected)


def test_cached_equal_to_input_rate_is_the_onprem_declaration_and_changes_nothing():
    price = _p(cached_input=3.0)
    assert price_call(price, prompt_tokens=1000, completion_tokens=200,
                      cached_prompt_tokens=800) == pytest.approx(_legacy(price, 1000, 200))


def test_cached_tokens_are_clamped_to_the_prompt_total():
    """A provider that over-reports must never produce negative uncached tokens."""
    price = _p(cached_input=0.0)
    got = price_call(price, prompt_tokens=100, completion_tokens=0,
                     cached_prompt_tokens=999)
    assert got == pytest.approx(0.0)
    assert got >= 0.0


def test_negative_cached_is_treated_as_zero():
    price = _p(cached_input=0.0)
    assert price_call(price, prompt_tokens=100, completion_tokens=0,
                      cached_prompt_tokens=-5) == pytest.approx(_legacy(price, 100, 0))
```

- [ ] **Step 2: Run test to verify it fails**

Run: `LITELLM_LOCAL_MODEL_COST_MAP=True uv run pytest tests/routing/test_pricing.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.routing.pricing'`

- [ ] **Step 3: Add `cached_input` to `PriceCfg`**

In `app/config/schema.py`, extend `PriceCfg`:

```python
class PriceCfg(BaseModel):
    input: float
    output: float
    tier: str
    provider: str
    # USD/1M for prompt tokens the provider served from ITS OWN prompt cache
    # (`usage.prompt_tokens_details.cached_tokens`). None = undeclared, and an
    # undeclared rate bills cached tokens at the full `input` rate --
    # RouteForge never invents a discount an operator did not declare. On-prem
    # endpoints normally declare this EQUAL to `input`: a vLLM prefix-cache hit
    # saves latency, not GPU amortization.
    cached_input: float | None = None
```

- [ ] **Step 4: Write the implementation**

Create `app/routing/pricing.py`:

```python
"""The one place a call's USD cost is computed.

This formula used to exist in three copies -- `Router._est_cost` and both the
streaming and non-streaming paths of `LiteLLMBackend` -- which is how a fourth
pricing axis (cached prompt tokens) would have silently applied in two places
out of three. Every caller now funnels through `price_call`.
"""
from __future__ import annotations

from app.config.schema import PriceCfg


def price_call(price: PriceCfg, *, prompt_tokens: int, completion_tokens: int,
               cached_prompt_tokens: int = 0) -> float:
    """USD for one call.

    `cached_prompt_tokens` are billed at `price.cached_input` when the operator
    declared one, and at the full `price.input` rate otherwise. The cached count
    is clamped into `[0, prompt_tokens]`: providers occasionally over-report it,
    and an unclamped value would produce negative uncached tokens and a cost
    below zero.
    """
    cached = max(0, min(int(cached_prompt_tokens), int(prompt_tokens)))
    uncached = int(prompt_tokens) - cached
    cached_rate = price.input if price.cached_input is None else price.cached_input
    return (uncached * price.input
            + cached * cached_rate
            + int(completion_tokens) * price.output) / 1_000_000
```

- [ ] **Step 5: Run test to verify it passes**

Run: `LITELLM_LOCAL_MODEL_COST_MAP=True uv run pytest tests/routing/test_pricing.py -v`
Expected: PASS (6 tests)

- [ ] **Step 6: Replace the three duplicated call sites**

In `app/routing/router.py`, add the import and rewrite `_est_cost`:

```python
from app.routing.pricing import price_call
```

```python
    def _est_cost(self, cand: PoolCandidate, tokens_after: int, output_tokens: int) -> float:
        """Pre-call estimate. Deliberately assumes ZERO cached prompt tokens:
        the router cannot know the hit rate before the call, and guessing one
        biases routing toward whichever model the guess was generous about.
        Estimate high, record true (see the actual cost on the DecisionRecord)."""
        p = (self._reg.price_for(cand.name) if self._reg
             else self._s.pricing.models[cand.name])
        return price_call(p, prompt_tokens=tokens_after, completion_tokens=output_tokens)
```

In `app/gateway/backends/litellm_backend.py`, add `from app.routing.pricing import price_call` and replace **both** cost lines (the non-streaming one at ~140 and the streaming one at ~202):

```python
        cost_usd = price_call(price, prompt_tokens=tokens_in,
                              completion_tokens=tokens_out)
```

(Task 2 threads `cached_prompt_tokens=` into both.)

- [ ] **Step 7: Verify nothing else computes a price**

Run: `/usr/bin/grep -rn "1_000_000" --include=*.py app/`
Expected: the only remaining hit is inside `app/routing/pricing.py`. If any other site appears, route it through `price_call` too and note it in your report.

- [ ] **Step 8: Run the full suite**

Run: `LITELLM_LOCAL_MODEL_COST_MAP=True uv run pytest`
Expected: PASS except the known `presidio_analyzer` failure. Cost values are unchanged because `cached_input` defaults to `None` everywhere.

- [ ] **Step 9: Commit**

```bash
git add app/routing/pricing.py app/config/schema.py app/routing/router.py \
        app/gateway/backends/litellm_backend.py tests/routing/test_pricing.py
git commit -m "feat(p2a): one price_call, with an optional cached-input rate"
```

---

## Task 2: Capture cached prompt tokens

**Files:**
- Modify: `app/gateway/backends/litellm_backend.py` (`_usage_dict` ~line 39; non-streaming ~137; streaming ~200)
- Modify: `app/models.py` (`GenerationResult`, ~line 276)
- Modify: `app/apps/models.py` (`UsageRecord`, ~line 227)
- Modify: `app/apps/store.py` (`record_usage`, ~line 51)
- Modify: `app/api/routes.py` (the three `record_usage` call sites: ~281, ~329, ~787)
- Modify: `scripts/migrate_app_policies.py`
- Test: `tests/routing/test_cached_tokens.py` (new)

**Interfaces:**
- Consumes: `price_call` from Task 1.
- Produces: `GenerationResult.cached_prompt_tokens: int` (default 0); `UsageRecord.cached_prompt_tokens: int | None`; `store.record_usage(..., cached_prompt_tokens: int = 0)`.

- [ ] **Step 1: Write the failing test**

Create `tests/routing/test_cached_tokens.py`:

```python
from types import SimpleNamespace

from app.gateway.backends.litellm_backend import _usage_dict


def test_usage_dict_reads_cached_tokens_from_a_dict_shape():
    u = {"prompt_tokens": 1000, "completion_tokens": 50,
         "prompt_tokens_details": {"cached_tokens": 768}}
    assert _usage_dict(u) == {"prompt_tokens": 1000, "completion_tokens": 50,
                              "cached_tokens": 768}


def test_usage_dict_reads_cached_tokens_from_an_object_shape():
    u = SimpleNamespace(prompt_tokens=1000, completion_tokens=50,
                        prompt_tokens_details=SimpleNamespace(cached_tokens=768))
    assert _usage_dict(u)["cached_tokens"] == 768


def test_a_provider_that_reports_no_details_yields_zero():
    """Indistinguishable from a genuine cache miss -- documented, not guessed."""
    u = {"prompt_tokens": 1000, "completion_tokens": 50}
    assert _usage_dict(u)["cached_tokens"] == 0


def test_a_null_details_block_yields_zero():
    u = {"prompt_tokens": 10, "completion_tokens": 1, "prompt_tokens_details": None}
    assert _usage_dict(u)["cached_tokens"] == 0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `LITELLM_LOCAL_MODEL_COST_MAP=True uv run pytest tests/routing/test_cached_tokens.py -v`
Expected: FAIL — `KeyError: 'cached_tokens'`

- [ ] **Step 3: Extend `_usage_dict`**

In `app/gateway/backends/litellm_backend.py`:

```python
def _cached_tokens(u) -> int:
    """`usage.prompt_tokens_details.cached_tokens`, across the dict, pydantic
    and SimpleNamespace shapes litellm hands back. A provider that does not
    report the field yields 0 -- indistinguishable from a real cache miss, which
    is a documented limitation, not an inference."""
    det = (u.get("prompt_tokens_details") if isinstance(u, dict)
           else getattr(u, "prompt_tokens_details", None))
    if det is None:
        return 0
    raw = det.get("cached_tokens") if isinstance(det, dict) else getattr(det, "cached_tokens", 0)
    try:
        return max(0, int(raw or 0))
    except (TypeError, ValueError):
        return 0


def _usage_dict(u) -> dict:
    """Normalize a litellm usage object (pydantic-ish), a plain dict, or a
    SimpleNamespace-style object into a `{prompt_tokens, completion_tokens,
    cached_tokens}` dict -- streaming's terminal usage chunk shape varies by
    provider."""
    if isinstance(u, dict):
        return {"prompt_tokens": u.get("prompt_tokens", 0),
                "completion_tokens": u.get("completion_tokens", 0),
                "cached_tokens": _cached_tokens(u)}
    return {"prompt_tokens": getattr(u, "prompt_tokens", 0),
            "completion_tokens": getattr(u, "completion_tokens", 0),
            "cached_tokens": _cached_tokens(u)}
```

Keep whatever the existing body does for the first two keys; only the third is new. Read the current function before editing — do not assume the snippet above matches it line for line.

- [ ] **Step 4: Run test to verify it passes**

Run: `LITELLM_LOCAL_MODEL_COST_MAP=True uv run pytest tests/routing/test_cached_tokens.py -v`
Expected: PASS (4 tests)

- [ ] **Step 5: Carry it onto `GenerationResult` and into both cost computations**

In `app/models.py`, add to `GenerationResult`:

```python
    # Prompt tokens the PROVIDER served from its own prompt cache
    # (`usage.prompt_tokens_details.cached_tokens`). 0 when the provider does
    # not report the field, which is indistinguishable from a genuine miss.
    cached_prompt_tokens: int = 0
```

In `litellm_backend.py`, both paths read the cached count and pass it to `price_call` and onto the result:

```python
        cached_in = usage.get("cached_tokens", 0)
        cost_usd = price_call(price, prompt_tokens=tokens_in,
                              completion_tokens=tokens_out,
                              cached_prompt_tokens=cached_in)
```

and add `cached_prompt_tokens=cached_in` to each `GenerationResult(...)` construction.

For the **non-streaming** path (~line 137), the usage block comes from `data.get("usage") or {}`; call `_cached_tokens(usage)` on it rather than assuming `_usage_dict` ran.

- [ ] **Step 6: Persist it**

`app/apps/models.py`, in `UsageRecord`, immediately after `total_tokens`:

```python
    # Nullable with no default: this column is added to deployed databases by
    # scripts/migrate_app_policies.py, whose stated invariant is that every
    # column it adds is nullable with no default. Readers coerce NULL to 0.
    cached_prompt_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
```

`app/apps/store.py`, `record_usage`: add the keyword-only parameter `cached_prompt_tokens: int = 0` alongside the other token counts and pass it into the `UsageRecord(...)` construction.

`app/api/routes.py`: at each of the three `record_usage` call sites, pass `cached_prompt_tokens=<result>.cached_prompt_tokens` where a `GenerationResult` is in scope, and leave it at the default `0` for rejected/error rows that have no result. Read each call site before editing; do not guess which ones have a result.

- [ ] **Step 7: Extend the migration script**

In `scripts/migrate_app_policies.py`, add a third table block below `MODEL_COLUMNS`:

```python
# 2026-09-19 P2a cached-token accounting. Nullable with no default: an existing
# row reads NULL, which every reader coerces to 0 ("no cached tokens recorded"),
# so this migration changes no cost figure already written.
USAGE_TABLE = "usage_records"

USAGE_COLUMNS: list[tuple[str, str, str]] = [
    ("cached_prompt_tokens", "INTEGER", "INTEGER"),
]
```

and extend the loop tuple:

```python
        for table, columns in ((TABLE, COLUMNS),
                               (MODELS_TABLE, MODEL_COLUMNS),
                               (USAGE_TABLE, USAGE_COLUMNS)):
```

- [ ] **Step 8: Test the migration is idempotent on a fresh SQLite DB**

Run: `DATABASE_URL=sqlite+aiosqlite:///./_p2a_tmp.db uv run python scripts/migrate_app_policies.py && DATABASE_URL=sqlite+aiosqlite:///./_p2a_tmp.db uv run python scripts/migrate_app_policies.py; rm -f _p2a_tmp.db`
Expected: both runs exit 0; the second reports `already present` (or `table absent, skipped` for tables `create_all` has not built).

- [ ] **Step 9: Run the full suite and commit**

Run: `LITELLM_LOCAL_MODEL_COST_MAP=True uv run pytest`
Expected: PASS except the known `presidio_analyzer` failure.

```bash
git add -A
git commit -m "feat(p2a): capture and persist provider cached prompt tokens"
```

---

## Task 3: `price_cached_input` on onboarded endpoints

**Files:**
- Modify: `app/apps/models.py` (`OnboardedModel`, ~line 307)
- Modify: `app/apps/registry.py` (`_Row` ~line 21, `effective_pool` ~line 58, `price_for` ~line 187)
- Modify: `app/apps/store.py` (the onboard field list, ~line 492)
- Modify: `app/api/admin_routes.py` (`OnboardIn` ~line 253, `_onboarded_dict` ~line 69, onboard ~line 952, edit ~line 1006, validation ~line 860)
- Modify: `scripts/migrate_app_policies.py` (`MODEL_COLUMNS`)
- Test: `tests/onboarding/test_cached_price_roundtrip.py` (new)

**Interfaces:**
- Consumes: `PriceCfg.cached_input` from Task 1.
- Produces: `OnboardedModel.price_cached_input: float | None`; `ModelRegistry.price_for(slug).cached_input` populated from it.

> **Why this task exists as its own task.** P1b's plan had exactly this hole: declarations existed on the model and in the registry but nothing wired the admin API, so the console form was a no-op and the value could only be set by hand-editing the database. Do not repeat it — the round-trip test below is the point of the task.

- [ ] **Step 1: Write the failing test**

Create `tests/onboarding/test_cached_price_roundtrip.py`. Follow the existing onboarding tests' client/fixture style — read `tests/onboarding/` first and match it.

```python
import pytest


@pytest.mark.asyncio
async def test_onboard_accepts_and_returns_price_cached_input(admin_client, onboard_body):
    body = {**onboard_body, "price_cached_input": 0.30}
    r = await admin_client.post("/admin/infra/models", json=body)
    assert r.status_code == 200, r.text
    assert r.json()["model"]["price_cached_input"] == 0.30


@pytest.mark.asyncio
async def test_registry_price_for_carries_the_cached_rate(admin_client, onboard_body, registry):
    await admin_client.post("/admin/infra/models",
                            json={**onboard_body, "price_cached_input": 0.30})
    await registry.refresh()
    assert registry.price_for(onboard_body["name"]).cached_input == 0.30


@pytest.mark.asyncio
async def test_omitting_the_field_on_edit_preserves_it(admin_client, onboard_body):
    """Same rule as P1b's accepts_raw_sensitive: omission preserves, explicit
    null clears. A price silently reverting to 'no discount' on an unrelated
    edit would overstate spend with no operator action."""
    name = onboard_body["name"]
    await admin_client.post("/admin/infra/models",
                            json={**onboard_body, "price_cached_input": 0.30})
    r = await admin_client.put(f"/admin/infra/models/{name}",
                               json={**onboard_body, "quality": 0.7})
    assert r.status_code == 200, r.text
    assert r.json()["model"]["price_cached_input"] == 0.30


@pytest.mark.asyncio
async def test_explicit_null_on_edit_clears_it(admin_client, onboard_body):
    name = onboard_body["name"]
    await admin_client.post("/admin/infra/models",
                            json={**onboard_body, "price_cached_input": 0.30})
    r = await admin_client.put(f"/admin/infra/models/{name}",
                               json={**onboard_body, "price_cached_input": None})
    assert r.status_code == 200, r.text
    assert r.json()["model"]["price_cached_input"] is None


@pytest.mark.asyncio
async def test_a_negative_cached_price_is_rejected(admin_client, onboard_body):
    r = await admin_client.post("/admin/infra/models",
                                json={**onboard_body, "price_cached_input": -1.0})
    assert r.status_code == 422
```

- [ ] **Step 2: Run test to verify it fails**

Run: `LITELLM_LOCAL_MODEL_COST_MAP=True uv run pytest tests/onboarding/test_cached_price_roundtrip.py -v`
Expected: FAIL — the field is unknown / not returned.

- [ ] **Step 3: Add the column and carry it through the registry**

`app/apps/models.py`, in `OnboardedModel` beside `price_output`:

```python
    # USD/1M for prompt tokens this endpoint served from its own prompt cache.
    # NULL = undeclared -> cached tokens bill at `price_input` (no invented
    # discount). On-prem endpoints normally declare this EQUAL to price_input:
    # a prefix-cache hit saves latency, not GPU amortization.
    price_cached_input: Mapped[float | None] = mapped_column(Float, nullable=True)
```

`app/apps/registry.py`: add `price_cached_input: float | None = None` to the `_Row` dataclass, populate it in `effective_pool`'s row construction, and return it from `price_for`:

```python
            return PriceCfg(input=r.price_input, output=r.price_output,
                            cached_input=r.price_cached_input,
                            tier=r.tier, provider=r.provider)
```

`app/apps/store.py`: add `"price_cached_input"` to the onboard field tuple at ~line 492.

- [ ] **Step 4: Wire the admin API**

`app/api/admin_routes.py`:

- `OnboardIn`: add `price_cached_input: float | None = None`.
- The validation block at ~line 860: extend to reject a negative value —
  ```python
      if body.price_cached_input is not None and body.price_cached_input < 0:
          raise HTTPException(422, "price_cached_input must be >= 0")
  ```
- `_onboarded_dict` (~line 69): add `"price_cached_input": m.price_cached_input`.
- The onboard call (~line 952): pass `price_cached_input=body.price_cached_input`.
- The edit call (~line 1006): use the **`model_fields_set` pattern P1b established** so omission preserves and explicit null clears. Read how `_resolved_accepts_raw_sensitive(body, existing)` does it and write the direct analogue:
  ```python
  def _resolved_price_cached_input(body: OnboardIn, existing) -> float | None:
      """Omission preserves, explicit null clears -- the P1b rule. A price
      quietly reverting to 'no discount' on an unrelated edit would overstate
      spend with no operator action."""
      if "price_cached_input" in body.model_fields_set:
          return body.price_cached_input
      return existing.price_cached_input
  ```

- [ ] **Step 5: Run test to verify it passes**

Run: `LITELLM_LOCAL_MODEL_COST_MAP=True uv run pytest tests/onboarding/test_cached_price_roundtrip.py -v`
Expected: PASS (5 tests)

- [ ] **Step 6: Extend the migration script**

Append to `MODEL_COLUMNS` in `scripts/migrate_app_policies.py`:

```python
    # 2026-09-19 P2a — the endpoint's declared cached-input rate. NULL =
    # undeclared -> bill cached tokens at price_input, i.e. today's behaviour.
    ("price_cached_input", "DOUBLE PRECISION", "FLOAT"),
```

- [ ] **Step 7: Run the full suite and commit**

Run: `LITELLM_LOCAL_MODEL_COST_MAP=True uv run pytest`
Expected: PASS except the known `presidio_analyzer` failure.

```bash
git add -A
git commit -m "feat(p2a): declare a cached-input rate per onboarded endpoint"
```

---

## Task 4: The signal store

**Files:**
- Create: `app/routing/signals.py`
- Test: `tests/routing/test_signals.py` (new)

**Interfaces:**
- Consumes: `SignalsCfg` — **defined in this task** (Task 5 moves it into `RoutingCfg` and the YAML; define it here so this task stands alone).
- Produces: `SignalValue`, `SeedRow`, `SignalStore` with `observe_success`, `observe_error`, `latency`, `availability`, `seed`, `snapshot`.

- [ ] **Step 1: Write the failing test**

Create `tests/routing/test_signals.py`:

```python
import pytest

from app.config.schema import SignalsCfg
from app.routing.signals import SeedRow, SignalStore


def _store(**kw) -> SignalStore:
    cfg = SignalsCfg(min_samples=3, half_life_s=100.0, staleness_s=1000.0, **kw)
    return SignalStore(cfg)


def test_an_unmeasured_model_reports_the_tier_default_not_a_guess():
    s = _store()
    v = s.latency("m", "cheap", now=0.0)
    assert (v.value, v.source, v.samples) == (200.0, "tier_default", 0)


def test_an_unmeasured_model_reports_neutral_availability():
    """1.0 is today's hardcoded constant -- the identity property depends on it."""
    s = _store()
    v = s.availability("m", now=0.0)
    assert (v.value, v.source, v.samples) == (1.0, "neutral", 0)


def test_below_min_samples_still_reports_the_true_sample_count():
    s = _store()
    s.observe_success("m", 500.0, now=0.0)
    v = s.latency("m", "cheap", now=1.0)
    assert v.source == "tier_default"
    assert v.samples == 1        # honest about how little it knows


def test_latency_becomes_measured_at_min_samples():
    s = _store()
    for i in range(3):
        s.observe_success("m", 400.0, now=float(i))
    v = s.latency("m", "cheap", now=3.0)
    assert v.source == "measured"
    assert v.value == pytest.approx(400.0)
    assert v.samples == 3


def test_a_stale_latency_falls_back_to_the_tier_default():
    """A measurement from two days ago is not a measurement."""
    s = _store()
    for i in range(3):
        s.observe_success("m", 400.0, now=float(i))
    v = s.latency("m", "cheap", now=10_000.0)
    assert v.source == "tier_default"
    assert v.value == 200.0


def test_availability_drops_on_errors():
    s = _store()
    for i in range(6):
        s.observe_error("m", now=float(i))
    v = s.availability("m", now=6.0)
    assert v.source == "measured"
    assert v.value < 0.5


def test_availability_decays_back_toward_neutral_on_WALL_CLOCK_with_no_traffic():
    """The starvation trap: down-weighting plus failover starves a model of the
    traffic it would need to prove it recovered. Time decay, not sample decay,
    is what lets it earn a retry."""
    s = _store()
    for i in range(6):
        s.observe_error("m", now=float(i))
    sick = s.availability("m", now=6.0).value
    later = s.availability("m", now=6.0 + 300.0).value   # 3 half-lives
    assert later > sick
    assert later == pytest.approx(1.0 - (1.0 - sick) * 0.125, rel=1e-6)


def test_availability_never_exceeds_neutral():
    s = _store()
    for i in range(10):
        s.observe_success("m", 100.0, now=float(i))
    assert s.availability("m", now=10_000.0).value <= 1.0


def test_disabled_reports_defaults_and_says_so():
    s = SignalStore(SignalsCfg(enabled=False, min_samples=1))
    for i in range(5):
        s.observe_success("m", 999.0, now=float(i))
    assert s.latency("m", "frontier", now=5.0) == pytest.approx(1000.0), "tier default"
    assert s.latency("m", "frontier", now=5.0).source == "disabled"
    assert s.availability("m", now=5.0).source == "disabled"
    assert s.availability("m", now=5.0).value == 1.0


def test_seeding_makes_a_model_measured_without_live_traffic():
    s = _store()
    s.seed([SeedRow(model="m", avg_latency_ms=350.0, error_rate=0.1, samples=42,
                    last_ts=0.0)])
    lat = s.latency("m", "cheap", now=1.0)
    avail = s.availability("m", now=1.0)
    assert (lat.source, lat.value, lat.samples) == ("measured", 350.0, 42)
    assert avail.source == "measured"
    assert avail.value == pytest.approx(0.9)


def test_an_unknown_tier_falls_back_without_raising():
    s = _store()
    assert s.latency("m", "no-such-tier", now=0.0).source == "tier_default"


def test_snapshot_exposes_per_model_state():
    s = _store()
    s.observe_success("m", 100.0, now=0.0)
    snap = s.snapshot()
    assert "m" in snap and snap["m"]["latency_n"] == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `LITELLM_LOCAL_MODEL_COST_MAP=True uv run pytest tests/routing/test_signals.py -v`
Expected: FAIL — `ImportError: cannot import name 'SignalsCfg'`

- [ ] **Step 3: Define `SignalsCfg`**

Add to `app/config/schema.py`, above `RoutingCfg`:

```python
class SignalsCfg(BaseModel):
    """Measured routing signals (P2a). Owned by routing.yaml only."""
    enabled: bool = True
    # Latency used for a model with no usable measurement, per canonical tier.
    # These defaults are NOT arbitrary: with `latency_scale_ms` 1000 they make
    # the normalized latency term evaluate to 0.2/0.5/1.0 -- byte-identical to
    # the pre-P2a hardcoded `_L` table -- so an empty SignalStore reproduces
    # today's scores exactly. Changing either without the other breaks that
    # identity property (spec §6.2).
    tier_latency_ms: dict[str, float] = Field(
        default_factory=lambda: {"cheap": 200.0, "mid": 500.0, "frontier": 1000.0})
    latency_scale_ms: float = 1000.0
    # Ceiling on the normalized latency term, so one pathologically slow
    # endpoint cannot swamp quality and cost entirely.
    max_latency_norm: float = 3.0
    # Wall-clock half-life for availability's decay back toward 1.0.
    half_life_s: float = 900.0
    # Observations required before a signal is reported as `measured`.
    min_samples: int = 5
    # A latency observation older than this is no longer a measurement.
    staleness_s: float = 3600.0
    seed_window_hours: int = 24
    # EWMA smoothing floor. Early on, alpha = 1/(n+1) behaves as a running
    # mean; once n exceeds 1/alpha_floor it becomes a fixed-width EWMA.
    alpha_floor: float = 0.1
```

Ensure `Field` is imported in that module.

- [ ] **Step 4: Write the implementation**

Create `app/routing/signals.py`:

```python
"""Measured per-model routing signals (P2a).

Two signals, both in-process and both PREFERENCES:

* **latency** -- an EWMA of observed wall-clock latency, used in place of the
  pre-P2a per-tier constant.
* **availability** -- an EWMA of per-call success, replacing a `capacity` term
  that added the same constant to every candidate and therefore never changed
  a routing decision at all.

Neither can make a candidate eligible or ineligible. `app/policy/feasible.py`
is the one authority on eligibility; this store is injected into the step-6
*strategy*, which by protocol can only reorder the set it is handed.

The store is per-process. Each API replica learns from its own traffic, so two
replicas can hold slightly different estimates for the same model. That is
acceptable for a ranking preference -- it cannot affect eligibility, cost caps
or residency -- and boot seeding from `usage_records` bounds the divergence.

Every method takes an injectable `now` so decay is testable without sleeping.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, replace

from app.config.schema import SignalsCfg


@dataclass(frozen=True)
class SignalValue:
    value: float      # milliseconds for latency; [0,1] for availability
    source: str       # "measured" | "tier_default" | "neutral" | "disabled"
    samples: int      # observations behind `value`; reported even when unused


@dataclass(frozen=True)
class SeedRow:
    model: str
    avg_latency_ms: float
    error_rate: float
    samples: int
    last_ts: float


@dataclass(frozen=True)
class _ModelState:
    latency_ms: float = 0.0
    latency_n: int = 0
    latency_ts: float = 0.0
    availability: float = 1.0
    availability_n: int = 0
    availability_ts: float = 0.0


class SignalStore:
    def __init__(self, cfg: SignalsCfg) -> None:
        self._cfg = cfg
        self._state: dict[str, _ModelState] = {}

    # -- internals --------------------------------------------------------
    def _now(self, now: float | None) -> float:
        return time.monotonic() if now is None else now

    def _alpha(self, n: int) -> float:
        """Running mean while samples are few, fixed-width EWMA thereafter."""
        return max(1.0 / (n + 1), self._cfg.alpha_floor)

    def _decayed_availability(self, st: _ModelState, now: float) -> float:
        """Pull availability back toward 1.0 on WALL-CLOCK time.

        This is what closes the starvation trap: down-weighting a failing model
        plus failover means it receives less traffic, so a purely sample-driven
        EWMA would never see the successes it needs to recover. Time decay
        returns it to neutral and earns it a retry regardless of traffic.
        """
        dt = max(0.0, now - st.availability_ts)
        if dt == 0.0 or self._cfg.half_life_s <= 0:
            return st.availability
        return 1.0 - (1.0 - st.availability) * (0.5 ** (dt / self._cfg.half_life_s))

    # -- observation ------------------------------------------------------
    def observe_success(self, model: str, latency_ms: float, *,
                        now: float | None = None) -> None:
        t = self._now(now)
        st = self._state.get(model, _ModelState())
        a_lat = self._alpha(st.latency_n)
        new_lat = (latency_ms if st.latency_n == 0
                   else st.latency_ms + a_lat * (latency_ms - st.latency_ms))
        base = self._decayed_availability(st, t)
        a_av = self._alpha(st.availability_n)
        self._state[model] = replace(
            st,
            latency_ms=new_lat, latency_n=st.latency_n + 1, latency_ts=t,
            availability=min(1.0, base + a_av * (1.0 - base)),
            availability_n=st.availability_n + 1, availability_ts=t)

    def observe_error(self, model: str, *, now: float | None = None) -> None:
        """An endpoint-side failure. Latency is NOT updated -- the duration of a
        failed call says nothing about how fast the endpoint serves."""
        t = self._now(now)
        st = self._state.get(model, _ModelState())
        base = self._decayed_availability(st, t)
        a_av = self._alpha(st.availability_n)
        self._state[model] = replace(
            st,
            availability=max(0.0, base + a_av * (0.0 - base)),
            availability_n=st.availability_n + 1, availability_ts=t)

    # -- reads ------------------------------------------------------------
    def _tier_default(self, tier: str) -> float:
        d = self._cfg.tier_latency_ms
        return d.get(tier, self._cfg.latency_scale_ms)

    def latency(self, model: str, tier: str, *, now: float | None = None) -> SignalValue:
        default = self._tier_default(tier)
        if not self._cfg.enabled:
            return SignalValue(default, "disabled", 0)
        st = self._state.get(model)
        if st is None or st.latency_n < self._cfg.min_samples:
            return SignalValue(default, "tier_default", 0 if st is None else st.latency_n)
        if self._now(now) - st.latency_ts > self._cfg.staleness_s:
            # A measurement from two days ago is not a measurement. Fall back
            # OUTRIGHT rather than drifting toward the default -- a drifted
            # value is neither measured nor default and cannot be labelled
            # honestly.
            return SignalValue(default, "tier_default", st.latency_n)
        return SignalValue(st.latency_ms, "measured", st.latency_n)

    def availability(self, model: str, *, now: float | None = None) -> SignalValue:
        if not self._cfg.enabled:
            return SignalValue(1.0, "disabled", 0)
        st = self._state.get(model)
        if st is None or st.availability_n < self._cfg.min_samples:
            return SignalValue(1.0, "neutral", 0 if st is None else st.availability_n)
        v = self._decayed_availability(st, self._now(now))
        return SignalValue(min(1.0, max(0.0, v)), "measured", st.availability_n)

    # -- lifecycle --------------------------------------------------------
    def seed(self, rows) -> None:
        """Populate from aggregated `usage_records` at boot. Replaces state for
        each named model; models absent from `rows` are left untouched."""
        for r in rows:
            self._state[r.model] = _ModelState(
                latency_ms=r.avg_latency_ms, latency_n=r.samples, latency_ts=r.last_ts,
                availability=min(1.0, max(0.0, 1.0 - r.error_rate)),
                availability_n=r.samples, availability_ts=r.last_ts)

    def snapshot(self) -> dict[str, dict]:
        return {m: {"latency_ms": st.latency_ms, "latency_n": st.latency_n,
                    "availability": st.availability,
                    "availability_n": st.availability_n}
                for m, st in self._state.items()}
```

- [ ] **Step 5: Run test to verify it passes**

Run: `LITELLM_LOCAL_MODEL_COST_MAP=True uv run pytest tests/routing/test_signals.py -v`
Expected: PASS (12 tests)

- [ ] **Step 6: Commit**

```bash
git add app/routing/signals.py app/config/schema.py tests/routing/test_signals.py
git commit -m "feat(p2a): per-model latency and availability signals"
```

---

## Task 5: Configuration, the `capacity` rename, and the lint

**Files:**
- Modify: `app/config/schema.py` (`RoutingCfg`)
- Modify: `config/routing.yaml`
- Modify: `app/config/lint.py` (~line 64, beside the `tier_quality` rules)
- Test: `tests/config/test_signals_config.py` (new)

**Interfaces:**
- Consumes: `SignalsCfg` from Task 4.
- Produces: `settings.routing.signals: SignalsCfg`; `settings.routing.weights["availability"]`.

- [ ] **Step 1: Write the failing test**

Create `tests/config/test_signals_config.py`. Match the existing config tests' construction style — read `tests/config/` first.

```python
import pytest

from app.config.schema import RoutingCfg


def _routing(**kw) -> dict:
    base = {
        "tiers": ["cheap", "mid", "frontier"],
        "score_on": "post_compression",
        "weights": {"quality": 1.0, "cost": 0.6, "latency": 0.3, "availability": 0.2},
        "tier_quality": {"cheap": 0.5, "mid": 0.7, "frontier": 0.9},
        "complexity_thresholds": {"cheap_max": 0.3, "mid_max": 0.7},
        "confidence_escalation_threshold": 0.5,
        "max_escalations": 1,
        "risk_floors": {"low": "cheap", "medium": "mid", "high": "frontier"},
        "model_pool": [],
        "fallback": {},
    }
    base.update(kw)
    return base


def test_capacity_is_accepted_as_a_deprecated_alias_for_availability():
    """No deployed routing.yaml may fail to boot on this rename."""
    cfg = RoutingCfg(**_routing(
        weights={"quality": 1.0, "cost": 0.6, "latency": 0.3, "capacity": 0.2}))
    assert cfg.weights["availability"] == 0.2
    assert "capacity" not in cfg.weights


def test_an_explicit_availability_wins_over_a_stale_capacity_alias():
    cfg = RoutingCfg(**_routing(
        weights={"quality": 1.0, "cost": 0.6, "latency": 0.3,
                 "capacity": 0.9, "availability": 0.2}))
    assert cfg.weights["availability"] == 0.2


def test_signal_defaults_preserve_the_identity_property():
    """tier_latency_ms / latency_scale_ms must reproduce the old _L constants."""
    cfg = RoutingCfg(**_routing())
    s = cfg.signals
    assert s.latency_scale_ms == 1000.0
    assert [s.tier_latency_ms[t] / s.latency_scale_ms
            for t in ("cheap", "mid", "frontier")] == [0.2, 0.5, 1.0]
```

And a lint test in the same file:

```python
def test_lint_rejects_a_tier_missing_from_tier_latency_ms(settings_factory):
    s = settings_factory()
    s.routing.signals.tier_latency_ms.pop("frontier")
    from app.config.lint import lint
    assert any(e.startswith("TIER_LATENCY_MISSING") for e in lint(s))


def test_lint_rejects_weights_with_no_availability(settings_factory):
    s = settings_factory()
    s.routing.weights.pop("availability")
    from app.config.lint import lint
    assert any(e.startswith("WEIGHT_MISSING") for e in lint(s))
```

Read `app/config/lint.py` for the real entry-point name and signature and match it; `lint` above is a placeholder for whatever that module actually exports.

- [ ] **Step 2: Run test to verify it fails**

Run: `LITELLM_LOCAL_MODEL_COST_MAP=True uv run pytest tests/config/test_signals_config.py -v`
Expected: FAIL — `capacity` is not translated and `cfg.signals` does not exist.

- [ ] **Step 3: Add `signals` and the alias validator to `RoutingCfg`**

In `app/config/schema.py`, add to `RoutingCfg`:

```python
    # P2a measured routing signals. Owned here (routing input), never restated.
    signals: SignalsCfg = SignalsCfg()
```

and a validator on the same class:

```python
    @model_validator(mode="before")
    @classmethod
    def _rename_capacity_weight(cls, data):
        """`capacity` -> `availability` (P2a).

        The old term added the SAME constant to every candidate, so it cancelled
        out of the ordering and never influenced a routing decision. It is
        renamed because true capacity -- queue depth, admission pressure -- is
        unobservable across an OpenAI-compatible boundary; what RouteForge can
        observe is whether calls have been succeeding, which is availability.

        The alias is accepted so no deployed routing.yaml fails to boot.
        """
        if not isinstance(data, dict):
            return data
        weights = data.get("weights")
        if isinstance(weights, dict) and "capacity" in weights:
            weights = dict(weights)
            legacy = weights.pop("capacity")
            weights.setdefault("availability", legacy)
            data = {**data, "weights": weights}
            logging.getLogger(__name__).warning(
                "routing.yaml: weights.capacity is deprecated; rename it to "
                "weights.availability (the old term never affected ranking)")
        return data
```

Ensure `model_validator` and `logging` are imported.

- [ ] **Step 4: Update `config/routing.yaml`**

```yaml
weights: {quality: 1.0, cost: 0.6, latency: 0.3, availability: 0.2}

# P2a measured routing signals. `tier_latency_ms` and `latency_scale_ms` are
# paired: with the defaults below an UNMEASURED candidate's normalized latency
# term is 0.2/0.5/1.0 -- exactly the pre-P2a hardcoded constants -- so turning
# measurement on changes nothing until a measurement actually exists.
signals:
  enabled: true
  tier_latency_ms:   {cheap: 200, mid: 500, frontier: 1000}
  latency_scale_ms:  1000
  max_latency_norm:  3.0
  half_life_s:       900
  min_samples:       5
  staleness_s:       3600
  seed_window_hours: 24
```

- [ ] **Step 5: Add the two lint rules**

In `app/config/lint.py`, beside the existing `tier_quality` loop (~line 64):

```python
    for t in tiers:
        if t not in s.routing.signals.tier_latency_ms:
            errs.append(f"TIER_LATENCY_MISSING {t}")
    for t, ms in s.routing.signals.tier_latency_ms.items():
        if ms <= 0:
            errs.append(f"TIER_LATENCY_NOT_POSITIVE {t}={ms}")
    if "availability" not in s.routing.weights:
        errs.append("WEIGHT_MISSING availability (routing.yaml; "
                    "`capacity` is the deprecated alias)")
```

- [ ] **Step 6: Run tests and boot the config lint**

Run: `LITELLM_LOCAL_MODEL_COST_MAP=True uv run pytest tests/config/ -v`
Expected: PASS.

Then confirm the real configs still lint by running the app's existing config-lint test (find it under `tests/config/`); do not start a server.

- [ ] **Step 7: Commit**

```bash
git add app/config/schema.py app/config/lint.py config/routing.yaml tests/config/test_signals_config.py
git commit -m "feat(p2a): signals config, capacity->availability rename, lint rules"
```

---

## Task 6: The scoring formula and its provenance types

**Files:**
- Modify: `app/routing/strategy.py` (whole file)
- Test: `tests/routing/test_strategy_identity.py` (new)

**Interfaces:**
- Consumes: `SignalStore`/`SignalValue` (Task 4), `SignalsCfg` (Task 4/5).
- Produces: `TermValue`, `ScoredCandidate`, `RankResult`; `RoutingStrategy.rank(eligible, *, ctx, tokens_after, weights, quality_resolver, signals, signals_cfg) -> RankResult`.

> **Implementation hazard — read this before you start.** Changing a `Protocol`
> signature has bitten this codebase before: a `TypeError` raised by a stale
> implementation was swallowed by `app/gateway/gateway.py` and surfaced as
> `NoEligibleCandidate`, which looks like a policy rejection rather than a bug.
> Find **every** implementation and every test fake of `RoutingStrategy`
> (`/usr/bin/grep -rn "def rank" --include=*.py app/ tests/`) and update them all
> in this commit.

- [ ] **Step 1: Write the failing test**

Create `tests/routing/test_strategy_identity.py`:

```python
import pytest
from hypothesis import given, settings as hyp_settings, strategies as st

from app.config.schema import PoolCandidate, SignalsCfg
from app.routing.signals import SignalStore
from app.routing.strategy import DefaultRoutingStrategy

WEIGHTS = {"quality": 1.0, "cost": 0.6, "latency": 0.3, "availability": 0.2}
_L_LEGACY = {"cheap": 0.2, "mid": 0.5, "frontier": 1.0}


def _legacy_score(cand, est, max_cost, q, weights):
    """The pre-P2a formula, copied verbatim from app/routing/strategy.py."""
    norm = est / max_cost if max_cost > 0 else 0.0
    return (weights["quality"] * q - weights["cost"] * norm
            - weights["latency"] * _L_LEGACY[cand.tier]
            + weights["capacity"] * 1.0)


@hyp_settings(max_examples=200, deadline=None)
@given(st.lists(st.tuples(st.sampled_from(["cheap", "mid", "frontier"]),
                          st.floats(min_value=1e-9, max_value=1.0,
                                    allow_nan=False, allow_infinity=False),
                          st.floats(min_value=0.0, max_value=1.0)),
                min_size=1, max_size=6))
def test_empty_signal_store_reproduces_the_legacy_scores_exactly(rows, quality_resolver_factory):
    """THE identity property (spec §6.2). Equal SCORES, not merely equal order.

    If this ever fails, a refactor has broken the guarantee that turning
    measurement on changes nothing until a measurement exists -- and the
    successor slice loses its control arm."""
    eligible = [(PoolCandidate(name=f"m{i}", provider="onprem", tier=tier), est)
                for i, (tier, est, _q) in enumerate(rows)]
    qmap = {f"m{i}": q for i, (_t, _e, q) in enumerate(rows)}
    resolver = quality_resolver_factory(qmap)

    cfg = SignalsCfg()
    result = DefaultRoutingStrategy().rank(
        eligible, ctx=None, tokens_after=0, weights=WEIGHTS,
        quality_resolver=resolver, signals=SignalStore(cfg), signals_cfg=cfg)

    max_cost = max(est for _c, est in eligible)
    legacy_weights = {**WEIGHTS, "capacity": WEIGHTS["availability"]}
    for sc in result.ordered:
        expected = _legacy_score(sc.candidate, sc.est_cost_usd, max_cost,
                                 qmap[sc.candidate.name], legacy_weights)
        assert sc.score == pytest.approx(expected, rel=1e-12, abs=1e-12)


@hyp_settings(max_examples=100, deadline=None)
@given(st.integers(min_value=1, max_value=6), st.integers(min_value=0, max_value=50))
def test_ranking_never_changes_membership_whatever_the_signals_say(n, seed, quality_resolver_factory):
    """A strategy ranks; it never filters in or out. Signals are the first
    thing that could plausibly breach that invariant."""
    import random
    rnd = random.Random(seed)
    eligible = [(PoolCandidate(name=f"m{i}", provider="onprem", tier="cheap"),
                 0.001 * (i + 1)) for i in range(n)]
    cfg = SignalsCfg(min_samples=1)
    store = SignalStore(cfg)
    for i in range(n):
        for k in range(3):
            if rnd.random() < 0.5:
                store.observe_error(f"m{i}", now=float(k))
            else:
                store.observe_success(f"m{i}", rnd.uniform(1.0, 9_000.0), now=float(k))

    result = DefaultRoutingStrategy().rank(
        eligible, ctx=None, tokens_after=0, weights=WEIGHTS,
        quality_resolver=quality_resolver_factory({}), signals=store, signals_cfg=cfg)

    assert len(result.ordered) == len(eligible)
    assert {sc.candidate.name for sc in result.ordered} == {c.name for c, _ in eligible}


def test_a_measured_fast_model_outranks_a_measured_slow_peer(quality_resolver_factory):
    """The whole point: two same-tier, same-price models now differentiate."""
    eligible = [(PoolCandidate(name="slow", provider="onprem", tier="cheap"), 0.001),
                (PoolCandidate(name="fast", provider="onprem", tier="cheap"), 0.001)]
    cfg = SignalsCfg(min_samples=1)
    store = SignalStore(cfg)
    for k in range(5):
        store.observe_success("slow", 3_000.0, now=float(k))
        store.observe_success("fast", 120.0, now=float(k))

    result = DefaultRoutingStrategy().rank(
        eligible, ctx=None, tokens_after=0, weights=WEIGHTS,
        quality_resolver=quality_resolver_factory({}), signals=store, signals_cfg=cfg)

    assert result.ordered[0].candidate.name == "fast"
    assert result.ordered[0].latency.source == "measured"


def test_disabled_signals_restore_tier_defaults_and_say_so(quality_resolver_factory):
    eligible = [(PoolCandidate(name="slow", provider="onprem", tier="cheap"), 0.001)]
    cfg = SignalsCfg(enabled=False, min_samples=1)
    store = SignalStore(cfg)
    for k in range(5):
        store.observe_success("slow", 9_000.0, now=float(k))

    result = DefaultRoutingStrategy().rank(
        eligible, ctx=None, tokens_after=0, weights=WEIGHTS,
        quality_resolver=quality_resolver_factory({}), signals=store, signals_cfg=cfg)

    assert result.signals_enabled is False
    assert result.ordered[0].latency.source == "disabled"
    assert result.ordered[0].latency.value == 200.0


def test_one_pathologically_slow_endpoint_is_clamped(quality_resolver_factory):
    eligible = [(PoolCandidate(name="awful", provider="onprem", tier="cheap"), 0.001)]
    cfg = SignalsCfg(min_samples=1, max_latency_norm=3.0)
    store = SignalStore(cfg)
    for k in range(5):
        store.observe_success("awful", 500_000.0, now=float(k))

    result = DefaultRoutingStrategy().rank(
        eligible, ctx=None, tokens_after=0, weights=WEIGHTS,
        quality_resolver=quality_resolver_factory({}), signals=store, signals_cfg=cfg)

    # quality*q - cost*1.0 - latency*3.0 (clamped) + availability*1.0
    assert result.ordered[0].score == pytest.approx(
        WEIGHTS["quality"] * result.ordered[0].quality.value
        - WEIGHTS["cost"] * 1.0 - WEIGHTS["latency"] * 3.0
        + WEIGHTS["availability"] * 1.0)
```

Add a `quality_resolver_factory` fixture to `tests/routing/conftest.py` (create the file if absent):

```python
import pytest

from app.routing.quality import QualityScore


@pytest.fixture
def quality_resolver_factory():
    """A minimal QualityResolver stand-in: a dict of model -> score, defaulting
    to the historical tier_default floor for anything unnamed."""
    class _Fake:
        def __init__(self, scores: dict[str, float]) -> None:
            self._s = scores

        def resolve(self, name: str) -> QualityScore:
            if name in self._s:
                return QualityScore(score=self._s[name], source="published")
            return QualityScore(score=0.5, source="tier_default")

    return _Fake
```

- [ ] **Step 2: Run test to verify it fails**

Run: `LITELLM_LOCAL_MODEL_COST_MAP=True uv run pytest tests/routing/test_strategy_identity.py -v`
Expected: FAIL — `rank() got an unexpected keyword argument 'signals'`

- [ ] **Step 3: Rewrite `app/routing/strategy.py`**

Keep the existing module docstring (the residency-invariant explanation) and extend it. Replace the types and the default strategy:

```python
@dataclass(frozen=True)
class TermValue:
    """One scoring term's value AND where the value came from.

    The provenance is the point. A router that reports `latency: 342ms
    (measured, n=128)` and one that reports `latency: 200ms (tier_default,
    n=0)` have made very different claims, and before P2a they were
    indistinguishable on the wire.
    """
    value: float
    source: str
    samples: int = 0


@dataclass(frozen=True)
class ScoredCandidate:
    candidate: PoolCandidate
    est_cost_usd: float
    score: float
    quality: TermValue          # source from QUALITY_SOURCES
    latency: TermValue          # ms; measured | tier_default | disabled
    availability: TermValue     # [0,1]; measured | neutral | disabled
    cost_norm: float
    cost_cached_pricing: str    # "priced" | "unpriced"


@dataclass(frozen=True)
class RankResult:
    """Everything needed to RECOMPUTE the ranking offline from the record
    alone -- no SignalStore, no live config. See spec §7.3."""
    ordered: list[ScoredCandidate]      # best first
    weights: dict[str, float]
    signals_enabled: bool
    latency_scale_ms: float
    max_latency_norm: float
```

The protocol:

```python
class RoutingStrategy(Protocol):
    def rank(self, eligible: list[tuple[PoolCandidate, float]], *,
             ctx: "ExecutionPolicyContext", tokens_after: int,
             weights: dict[str, float],
             quality_resolver: "QualityResolver",
             signals: "SignalStore",
             signals_cfg: "SignalsCfg") -> RankResult:
        """Order `eligible` best-first and report how each score was reached.

        MUST return exactly the same members it was given, only reordered -- a
        strategy ranks, it never filters-in. `eligible` is already the
        post-residency/scope/tier/budget-filtered set (see module docstring);
        `signals` can reorder it but can never add to or remove from it.
        """
        ...
```

The default strategy:

```python
class DefaultRoutingStrategy:
    """Scores within the target tier: prefer high quality, low
    (tier-max-normalized) cost, low MEASURED latency, high availability;
    tie-break by raw est_cost ascending.

    **The identity property.** Latency normalizes against a FIXED
    `latency_scale_ms`, never the candidate-set max. With the shipped defaults
    (`tier_latency_ms` 200/500/1000, `latency_scale_ms` 1000) an unmeasured
    candidate's latency term is exactly 0.2/0.5/1.0 -- the pre-P2a `_L`
    constants -- and unmeasured availability is exactly 1.0, the pre-P2a
    hardcoded constant. So with an empty SignalStore this produces numerically
    identical scores to the pre-P2a formula, and every behaviour change is
    attributable to a measurement actually existing.

    Do NOT switch latency to set-max normalization "for consistency" with cost.
    Cost is comparative (dollars vary by orders of magnitude between models);
    latency is absolute (a millisecond means the same thing everywhere). Set-max
    on latency would rescale the term whenever the eligible set's tier mix
    changed, breaking the identity property for reasons unrelated to any
    measurement. `tests/routing/test_strategy_identity.py` pins this.
    """

    def rank(self, eligible, *, ctx, tokens_after, weights, quality_resolver,
             signals, signals_cfg) -> RankResult:
        max_cost = max((est for _, est in eligible), default=0.0)
        scored: list[ScoredCandidate] = []

        for cand, est in eligible:
            q = quality_resolver.resolve(cand.name)
            lat = signals.latency(cand.name, cand.tier)
            avail = signals.availability(cand.name)

            cost_norm = est / max_cost if max_cost > 0 else 0.0
            lat_norm = min(lat.value / signals_cfg.latency_scale_ms,
                           signals_cfg.max_latency_norm)
            score = (weights["quality"] * q.score
                     - weights["cost"] * cost_norm
                     - weights["latency"] * lat_norm
                     + weights["availability"] * avail.value)

            scored.append(ScoredCandidate(
                candidate=cand, est_cost_usd=est, score=score,
                quality=TermValue(q.score, q.source),
                latency=TermValue(lat.value, lat.source, lat.samples),
                availability=TermValue(avail.value, avail.source, avail.samples),
                cost_norm=cost_norm,
                cost_cached_pricing=_cached_pricing_label(cand, signals_cfg)))

        scored.sort(key=lambda sc: (-sc.score, sc.est_cost_usd))
        return RankResult(ordered=scored, weights=dict(weights),
                          signals_enabled=signals_cfg.enabled,
                          latency_scale_ms=signals_cfg.latency_scale_ms,
                          max_latency_norm=signals_cfg.max_latency_norm)
```

For `cost_cached_pricing`: the strategy does not hold a `PriceCfg`, so resolve
the label where the price is available. Simplest correct approach — pass it in.
Add an optional `cached_pricing: dict[str, str] | None = None` parameter to
`rank`, built by `Router.select` (Task 7) as `{name: "priced" if price.cached_input
is not None else "unpriced"}`, defaulting every candidate to `"unpriced"` when
absent. Replace `_cached_pricing_label(...)` above with
`(cached_pricing or {}).get(cand.name, "unpriced")` and add the parameter to the
Protocol signature and to every test call.

- [ ] **Step 4: Update every other implementation and fake**

Run: `/usr/bin/grep -rn "def rank\|\.rank(" --include=*.py app/ tests/`

Update every hit to the new signature and return type. `Router.select`'s call
site is Task 7's job, but it must at minimum compile and pass its tests here —
make the minimal change (`ordered = result.ordered`, then
`chosen, est = ordered[0].candidate, ordered[0].est_cost_usd`).

- [ ] **Step 5: Run test to verify it passes**

Run: `LITELLM_LOCAL_MODEL_COST_MAP=True uv run pytest tests/routing/ -v`
Expected: PASS.

- [ ] **Step 6: Run the FULL suite — this is where a Protocol change breaks things**

Run: `LITELLM_LOCAL_MODEL_COST_MAP=True uv run pytest`
Expected: PASS except the known `presidio_analyzer` failure. Pay particular attention to the streaming suites and the gateway suites: a `TypeError` from a stale `rank` implementation surfaces as `NoEligibleCandidate`, so a *policy-rejection* failure in an unrelated test is the signature of a missed call site.

- [ ] **Step 7: Commit**

```bash
git add app/routing/strategy.py tests/routing/
git commit -m "feat(p2a): score on measured signals, with per-term provenance"
```

---

## Task 7: Wiring — router, gateway observations, boot seeding

**Files:**
- Modify: `app/routing/router.py` (`RoutingDecision`, `Router.__init__`, `Router.select`)
- Modify: `app/gateway/gateway.py` (the failure branch ~line 135, the success branch ~line 156)
- Modify: `app/main.py` (lifespan: build and seed the store)
- Modify: `app/apps/store.py` (the seed aggregate query)
- Test: `tests/routing/test_signal_wiring.py` (new)

**Interfaces:**
- Consumes: `SignalStore`, `SeedRow` (Task 4); `RankResult` (Task 6).
- Produces: `RoutingDecision.scoring: RankResult | None`; `store.aggregate_model_signals(session, *, since_hours) -> list[SeedRow]`; `app.gateway.gateway.counts_as_endpoint_unhealth(exc) -> bool`.

- [ ] **Step 1: Write the failing test**

Create `tests/routing/test_signal_wiring.py`:

```python
import pytest

from app.errors import ContextWindowExceeded
from app.gateway.gateway import counts_as_endpoint_unhealth


class _Status(Exception):
    def __init__(self, code):
        super().__init__(f"http {code}")
        self.status_code = code


def test_a_context_window_overflow_is_the_REQUEST_being_wrong():
    """An oversized prompt must not make a healthy endpoint look sick. This is
    the exact shape of the Qwen Code retry storm (backlog B36)."""
    assert counts_as_endpoint_unhealth(ContextWindowExceeded("too long")) is False


@pytest.mark.parametrize("code", [400, 401, 403, 404, 422])
def test_client_errors_do_not_count_against_the_endpoint(code):
    assert counts_as_endpoint_unhealth(_Status(code)) is False


@pytest.mark.parametrize("code", [500, 502, 503, 504])
def test_server_errors_count(code):
    assert counts_as_endpoint_unhealth(_Status(code)) is True


def test_rate_limiting_counts_as_unhealth():
    """429 is the closest thing to admission pressure an OpenAI-compatible
    endpoint can express -- the one real capacity signal available."""
    assert counts_as_endpoint_unhealth(_Status(429)) is True


def test_a_connection_error_with_no_status_counts():
    assert counts_as_endpoint_unhealth(OSError("connection refused")) is True
```

Plus an end-to-end wiring test — follow the existing gateway test fixtures (read `tests/` for how a `Gateway` is assembled with the deterministic fake backend):

```python
@pytest.mark.asyncio
async def test_a_served_request_records_a_latency_observation(gateway_with_signals):
    gw, store = gateway_with_signals
    await gw.run(...)      # match the existing gateway tests' invocation
    assert store.snapshot()             # some model got an observation
```

- [ ] **Step 2: Run test to verify it fails**

Run: `LITELLM_LOCAL_MODEL_COST_MAP=True uv run pytest tests/routing/test_signal_wiring.py -v`
Expected: FAIL — `cannot import name 'counts_as_endpoint_unhealth'`

- [ ] **Step 3: Write the failure classifier**

In `app/gateway/gateway.py`:

```python
def counts_as_endpoint_unhealth(exc: Exception) -> bool:
    """Does this failure say something about the ENDPOINT, or about the request?

    A 4xx means the request was wrong -- an oversized prompt, a bad tool schema,
    a missing key. Counting it against availability would let one client's bad
    requests push traffic off a perfectly healthy endpoint, which is exactly the
    shape of the Qwen Code retry storm (B36).

    429 is the exception among 4xx: it is the endpoint refusing load, and it is
    the closest thing to admission pressure an OpenAI-compatible endpoint can
    express -- the one real capacity signal the protocol offers.

    Anything with no status at all (connection refused, TLS failure, timeout)
    is endpoint unhealth.
    """
    if isinstance(exc, ContextWindowExceeded):
        return False
    code = getattr(exc, "status_code", None)
    if code is None:
        return True
    try:
        code = int(code)
    except (TypeError, ValueError):
        return True
    if code == 429:
        return True
    return not (400 <= code < 500)
```

- [ ] **Step 4: Observe from the gateway's two branches**

`Gateway.__init__` takes `signals: SignalStore | None = None` and stores it.

In the **failure** branch (after `last_error = ...`):

```python
                if self._signals is not None and counts_as_endpoint_unhealth(exc):
                    # This hop's model failed -- observe THAT model, not the
                    # peer that succeeds on the next iteration.
                    self._signals.observe_error(cand.name)
```

In the **success** branch (immediately before the `records.append(DecisionRecord(step="router", ...))`):

```python
            if self._signals is not None:
                self._signals.observe_success(cand.name, result.latency_ms)
```

- [ ] **Step 5: Thread `RankResult` through the router**

`RoutingDecision` gains `scoring: "RankResult | None" = None`.

`Router.__init__` gains `signals: "SignalStore | None" = None`, defaulting to
`SignalStore(settings.routing.signals)` when not injected.

In `Router.select` step 5, build the cached-pricing map and pass the new arguments:

```python
        cached_pricing = {}
        for c, _e in eligible:
            try:
                p = (self._reg.price_for(c.name) if self._reg
                     else self._s.pricing.models[c.name])
            except KeyError:
                continue
            cached_pricing[c.name] = "priced" if p.cached_input is not None else "unpriced"

        result = self._strategy.rank(
            eligible, ctx=ctx, tokens_after=tokens_after,
            weights=self._s.routing.weights, quality_resolver=self._quality,
            signals=self._signals, signals_cfg=self._s.routing.signals,
            cached_pricing=cached_pricing)
        ordered = result.ordered
        chosen, est = ordered[0].candidate, ordered[0].est_cost_usd
        for sc in ordered[1:]:
            rejected.append(RejectedCandidate(
                name=sc.candidate.name, provider=sc.candidate.provider,
                tier=sc.candidate.tier, display_name=sc.candidate.display_name,
                reason="lower score"))
```

and add `scoring=result` to the returned `RoutingDecision`.

- [ ] **Step 6: Seed at boot**

Add to `app/apps/store.py`:

```python
async def aggregate_model_signals(session: AsyncSession, *,
                                  since_hours: int = 24) -> list[SeedRow]:
    """Per-model latency/error aggregates for SignalStore seeding.

    Counts only rows the endpoint actually answered or failed on: `status` in
    ('ok','error'). A 'rejected' row is the POLICY GATE doing its job, and
    counting it as endpoint unhealth would make a strict application look like
    broken infrastructure -- and would let a protection posture influence
    routing preference, which CLAUDE.md rule 2a forbids.
    """
```

Implement it with a single grouped `select` over `UsageRecord` filtered to
`ts >= utcnow() - timedelta(hours=since_hours)` and `status.in_(("ok", "error"))`,
returning `SeedRow(model, avg_latency_ms, error_rate, samples, last_ts)`.
Compute `avg_latency_ms` over `status == 'ok'` rows only (a failed call's
duration says nothing about serving speed) and `error_rate` over all counted
rows. Set `last_ts` to `time.monotonic()` — the store's clock is monotonic, so a
wall-clock timestamp would make every seeded row instantly stale.

In `app/main.py`'s lifespan, after the DB is ready and before serving:

```python
    signals = SignalStore(settings.routing.signals)
    try:
        async with session_factory() as session:
            rows = await store.aggregate_model_signals(
                session, since_hours=settings.routing.signals.seed_window_hours)
        signals.seed(rows)
        log.info("signal store seeded models=%d", len(rows))
    except Exception:
        # Fail to the boring side: an empty store means every model reports
        # tier_default / neutral, which is exactly pre-P2a behaviour.
        log.warning("signal store seeding failed; starting with an empty store",
                    exc_info=True)
```

Pass `signals` into the `Router` and the `Gateway` constructed there. Read how
they are currently constructed and match it.

- [ ] **Step 7: Run tests**

Run: `LITELLM_LOCAL_MODEL_COST_MAP=True uv run pytest tests/routing/test_signal_wiring.py -v`
Expected: PASS.

- [ ] **Step 8: Run the full suite and commit**

Run: `LITELLM_LOCAL_MODEL_COST_MAP=True uv run pytest`
Expected: PASS except the known `presidio_analyzer` failure.

```bash
git add -A
git commit -m "feat(p2a): observe latency and availability, seed them at boot"
```

---

## Task 8: Emit the scoring record, and prove it is replayable

**Files:**
- Modify: `app/models.py` (`DecisionRecord`)
- Modify: `app/routing/router.py` (serialize `RankResult` onto the record)
- Modify: `app/obs/export.py` (`sp.scoring` on router-hop spans, ~line 437)
- Modify: `app/pipeline.py` (chosen-candidate terms on the response certificate, `_certificate` ~line 258)
- Test: `tests/routing/test_scoring_record_replay.py` (new)

**Interfaces:**
- Consumes: `RankResult`, `ScoredCandidate`, `TermValue` (Task 6).
- Produces: `RankResult.to_record() -> dict`; `DecisionRecord.scoring: dict | None`; trace attribute `sp.scoring`.

- [ ] **Step 1: Write the failing test — the replay property**

Create `tests/routing/test_scoring_record_replay.py`:

```python
import pytest

from app.config.schema import PoolCandidate, SignalsCfg
from app.routing.signals import SignalStore
from app.routing.strategy import DefaultRoutingStrategy

WEIGHTS = {"quality": 1.0, "cost": 0.6, "latency": 0.3, "availability": 0.2}


def _replay(record: dict) -> list[str]:
    """Recompute the ranking from the emitted record ALONE -- no SignalStore,
    no live config. This function is deliberately a naive re-implementation:
    if it needs anything the record does not carry, the record is insufficient
    and the successor slice would be measuring a fiction."""
    w = record["weights"]
    scale = record["latency_scale_ms"]
    cap = record["max_latency_norm"]
    out = []
    for c in record["candidates"]:
        score = (w["quality"] * c["quality"]["v"]
                 - w["cost"] * c["cost"]["norm"]
                 - w["latency"] * min(c["latency"]["ms"] / scale, cap)
                 + w["availability"] * c["availability"]["v"])
        out.append((score, c["cost"]["est_usd"], c["name"]))
    out.sort(key=lambda t: (-t[0], t[1]))
    return [name for _s, _e, name in out]


@pytest.mark.parametrize("seed", range(20))
def test_the_record_alone_reproduces_the_router_order(seed, quality_resolver_factory):
    import random
    rnd = random.Random(seed)
    tiers = ["cheap", "mid", "frontier"]
    eligible = [(PoolCandidate(name=f"m{i}", provider="onprem",
                               tier=rnd.choice(tiers)), rnd.uniform(1e-6, 1e-3))
                for i in range(rnd.randint(1, 6))]
    cfg = SignalsCfg(min_samples=1)
    store = SignalStore(cfg)
    for c, _e in eligible:
        for k in range(rnd.randint(0, 6)):
            if rnd.random() < 0.3:
                store.observe_error(c.name, now=float(k))
            else:
                store.observe_success(c.name, rnd.uniform(50.0, 5_000.0), now=float(k))

    result = DefaultRoutingStrategy().rank(
        eligible, ctx=None, tokens_after=0, weights=WEIGHTS,
        quality_resolver=quality_resolver_factory({}), signals=store,
        signals_cfg=cfg, cached_pricing={})

    actual = [sc.candidate.name for sc in result.ordered]
    assert _replay(result.to_record()) == actual


def test_the_record_carries_provenance_for_every_term(quality_resolver_factory):
    eligible = [(PoolCandidate(name="m", provider="onprem", tier="cheap"), 0.001)]
    cfg = SignalsCfg()
    rec = DefaultRoutingStrategy().rank(
        eligible, ctx=None, tokens_after=0, weights=WEIGHTS,
        quality_resolver=quality_resolver_factory({}), signals=SignalStore(cfg),
        signals_cfg=cfg, cached_pricing={}).to_record()

    c = rec["candidates"][0]
    assert c["latency"]["src"] == "tier_default"
    assert c["availability"]["src"] == "neutral"
    assert c["quality"]["src"] == "tier_default"
    assert c["cost"]["cached"] == "unpriced"
    assert c["latency"]["n"] == 0


def test_the_record_contains_no_free_text(quality_resolver_factory):
    """Emitted regardless of capture_io, so it must carry ids/enums/numbers only."""
    eligible = [(PoolCandidate(name="m", provider="onprem", tier="cheap"), 0.001)]
    cfg = SignalsCfg()
    rec = DefaultRoutingStrategy().rank(
        eligible, ctx=None, tokens_after=0, weights=WEIGHTS,
        quality_resolver=quality_resolver_factory({}), signals=SignalStore(cfg),
        signals_cfg=cfg, cached_pricing={}).to_record()

    allowed_srcs = {"measured", "tier_default", "neutral", "disabled",
                    "customer", "calibrated", "published"}
    for c in rec["candidates"]:
        assert c["latency"]["src"] in allowed_srcs
        assert c["availability"]["src"] in allowed_srcs
        assert c["cost"]["cached"] in {"priced", "unpriced"}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `LITELLM_LOCAL_MODEL_COST_MAP=True uv run pytest tests/routing/test_scoring_record_replay.py -v`
Expected: FAIL — `'RankResult' object has no attribute 'to_record'`

- [ ] **Step 3: Add `to_record`**

In `app/routing/strategy.py`, on `RankResult`:

```python
    def to_record(self) -> dict:
        """The scoring record: enough to RECOMPUTE this ranking offline with no
        SignalStore and no live config (spec §7.3). Ids, enums and numbers
        only -- it is emitted regardless of `capture_io`, exactly like
        `sp.candidates`, so it must never carry free text."""
        return {
            "weights": self.weights,
            "signals_enabled": self.signals_enabled,
            "latency_scale_ms": self.latency_scale_ms,
            "max_latency_norm": self.max_latency_norm,
            "candidates": [
                {"name": sc.candidate.name,
                 "score": sc.score,
                 "quality": {"v": sc.quality.value, "src": sc.quality.source},
                 "latency": {"ms": sc.latency.value, "src": sc.latency.source,
                             "n": sc.latency.samples},
                 "availability": {"v": sc.availability.value,
                                  "src": sc.availability.source,
                                  "n": sc.availability.samples},
                 "cost": {"est_usd": sc.est_cost_usd, "norm": sc.cost_norm,
                          "cached": sc.cost_cached_pricing}}
                for sc in self.ordered],
        }
```

- [ ] **Step 4: Carry it onto the `DecisionRecord` and the trace**

`app/models.py`, on `DecisionRecord`:

```python
    # P2a scoring record: per-eligible-candidate term values and provenance.
    # Ids/enums/numbers only -- emitted regardless of capture_io, like
    # `candidates`. Rejected candidates are absent by design: they were never
    # scored, and inventing terms for them would be exactly the overstatement
    # P2a exists to remove.
    scoring: dict | None = None
```

In `Router.select`, set `scoring=result` on `RoutingDecision`; in
`app/gateway/gateway.py`'s `DecisionRecord(step="router", ...)` construction,
pass `scoring=decision.scoring.to_record() if decision.scoring else None`.

In `app/obs/export.py`, beside the existing `sp.candidates` emission (~line 437):

```python
                        if rec.get("scoring"):
                            sp.set_attribute("sp.scoring",
                                             redact_value(json.dumps(rec["scoring"])))
```

Match the exact serialization the neighbouring `sp.candidates` line uses — read it first rather than assuming `json.dumps`.

- [ ] **Step 5: Put the chosen candidate's terms on the response certificate**

In `app/pipeline.py`'s `_certificate`, add the chosen candidate's terms only.
The response is a client-facing surface on every chat completion and must stay
small; the trace carries the full eligible set.

```python
def _chosen_scoring(scoring: dict | None, chosen: str | None) -> dict | None:
    """The chosen candidate's terms, for the response body. The full eligible
    set goes to the trace only -- see spec §7.1."""
    if not scoring or not chosen:
        return None
    for c in scoring.get("candidates", []):
        if c.get("name") == chosen:
            return {"weights": scoring["weights"],
                    "signals_enabled": scoring["signals_enabled"], **c}
    return None
```

Wire it into the certificate dict as `sp.scoring` (or the key the neighbouring
certificate fields use — match their naming).

- [ ] **Step 6: Run tests**

Run: `LITELLM_LOCAL_MODEL_COST_MAP=True uv run pytest tests/routing/test_scoring_record_replay.py -v`
Expected: PASS.

- [ ] **Step 7: Run the full suite and commit**

Run: `LITELLM_LOCAL_MODEL_COST_MAP=True uv run pytest`
Expected: PASS except the known `presidio_analyzer` failure. The streaming suites are the ones most likely to break on a `DecisionRecord` field change — check them specifically.

```bash
git add -A
git commit -m "feat(p2a): emit a replayable scoring record per router hop"
```

---

## Task 9: Documentation, backlog, and the research artifact

**Files:**
- Modify: `docs/reference/routing-observability.md`
- Modify: `docs/reference/per-app-config.md`
- Modify: `docs/guides/onboarding-models.md`
- Modify: `docs/backlog/bugs.md` (B30, ~line 427)
- Modify: `CLAUDE.md` (the routing-observability paragraph)
- Create: `docs/research/routing-signal-provenance.md`

**Interfaces:**
- Consumes: everything above. No code changes.

- [ ] **Step 1: Document the scoring record**

In `docs/reference/routing-observability.md`, add a section covering: the
`sp.scoring` trace attribute and its shape; the trace-carries-all /
response-carries-chosen split and why; the four provenance values
(`measured`, `tier_default`, `neutral`, `disabled`); and the **per-replica
drift caveat** stated plainly — each API replica learns from its own traffic, so
two replicas can hold different latency estimates for the same model; this is
acceptable for a ranking preference because it cannot affect eligibility, cost
caps or residency, and boot seeding bounds the divergence.

- [ ] **Step 2: Document the two new columns and the running ALTER count**

In `docs/reference/per-app-config.md`'s manual-migration section, add
`usage_records.cached_prompt_tokens` and `onboarded_models.price_cached_input`,
and state the running total explicitly:

> A deployment still on pre-P1a code now needs **eleven** `ALTER`s before the
> image is rolled: P1a's 4 on `app_policies`, P1b's 5 (2 on `app_policies`,
> 3 on `onboarded_models`), and P2a's 2 (1 on `onboarded_models`, 1 on
> `usage_records`). `scripts/migrate_app_policies.py` applies all of them and is
> idempotent. It has been exercised against SQLite only — run it against a
> restored Postgres snapshot first; the Spark and DGX both run Postgres.

- [ ] **Step 3: Document `price_cached_input` for operators**

In `docs/guides/onboarding-models.md`, beside the existing price fields: what it
is, that leaving it unset bills cached tokens at the full input rate (RouteForge
invents no discount), and the on-prem guidance — set it **equal to**
`price_input`, because a vLLM prefix-cache hit saves latency, not GPU
amortization; the latency win shows up in the routing signal instead.

- [ ] **Step 4: Close B30 honestly**

In `docs/backlog/bugs.md`, mark B30 resolved, and record the correction rather
than quietly fixing it:

> **Resolved 2026-09-19 (P2a) — renamed and made real.** The original entry
> understated this. `capacity` was not merely hardcoded: it added the *same*
> constant to every candidate, so it cancelled out of the ordering and never
> influenced a single routing decision — changing `weights.capacity` from 0.2 to
> 20 would have changed nothing. It is now `availability`, computed from an
> EWMA over call outcomes. The rename is deliberate: true capacity (queue depth,
> admission pressure) is unobservable across an OpenAI-compatible boundary, so a
> term named `capacity` promised a measurement the protocol cannot deliver.
> `capacity` remains a deprecated alias in `routing.yaml`.

- [ ] **Step 5: Update CLAUDE.md**

Amend the "Routing observability & signals" paragraph to mention `sp.scoring`
alongside `sp.candidates` and `sp.routing_policy_version`, in the same style and
without inflating the claim.

- [ ] **Step 6: Write the research artifact**

Create `docs/research/routing-signal-provenance.md`, following the structure of
`docs/research/policy-compiler-noninterference.md`. Required sections:

1. **The problem** — three of four scoring terms were placeholders; the
   `capacity` term was algebraically inert. Include the table from spec §1.
2. **The model** — signals as ranking preferences strictly downstream of an
   eligibility authority; the formula; the fixed-scale normalization argument.
3. **The identity property** — statement, the defaults that produce it, and the
   test that pins it. Explain why it matters: it makes every behaviour change
   attributable to a measurement existing, and gives the successor slice a real
   control arm.
4. **Provenance** — the four values, and the claim that a decision is
   reconstructible from its own record *including the admission that a term was
   a constant*.
5. **Results** — the test suite as evidence. State sample sizes honestly.
6. **What is NOT demonstrated** — be explicit, on the P1a precedent:
   - No claim that routing quality improves. Nothing here measures it.
   - No claim that the signals are globally consistent; they are per-replica.
   - Cached-token capture depends on providers reporting the field; a
     non-reporting provider is indistinguishable from a cache miss.
   - `min_samples`, `half_life_s` and `alpha_floor` are chosen defaults, not
     tuned or validated against production traffic.
   - The identity property is proven against the *implementation* of the
     pre-P2a formula, not against any external notion of correct routing.
7. **Relationship to P1a/P1b** — eligibility provenance plus scoring provenance;
   the combined claim, stated no more strongly than §6 permits.

- [ ] **Step 7: Commit**

```bash
git add -A
git commit -m "docs(p2a): scoring record, cached pricing, B30 resolution, research notes"
```

---

## Self-Review

**Spec coverage:** §4 signal store → Task 4; §4.4 failure rulings → Task 7; §4.5 seeding → Task 7; §5 cached tokens → Tasks 1–3; §6 formula and rename → Tasks 5–6; §7 record → Task 8; §8 tests T1–T10 → T1 Task 8, T2/T7/T8 Task 6, T3/T4 Task 4, T5 Task 7, T6 Task 1, T9 Task 7, T10 Task 6 step 4/6; §9 config → Task 5; §10 schema → Tasks 2–3; §11 docs → Task 9; §12 research artifact → Task 9.

**Type consistency:** `SignalValue(value, source, samples)` is produced in Task 4 and consumed in Task 6 as `TermValue(value, source, samples)` — a deliberate rename at the boundary (the store speaks about signals; the strategy speaks about score terms), with field names identical so the mapping is mechanical. `RankResult` is produced in Task 6, consumed in Task 7 (`RoutingDecision.scoring`) and serialized in Task 8 (`to_record`). `SeedRow` is defined in Task 4 and produced by `aggregate_model_signals` in Task 7. `cached_pricing` is added to `rank` in Task 6 step 3 and populated in Task 7 step 5.

**Known plan risk, flagged rather than hidden:** Task 6 changes a `Protocol`
signature, and this codebase has swallowed a resulting `TypeError` as
`NoEligibleCandidate` before. Task 6 steps 4 and 6 exist specifically to catch
that, and the full-suite run in step 6 is not optional.
