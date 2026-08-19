# Cluster B — Decision-trace Honesty Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the request drilldown and cost numbers tell the truth — disabled providers stop appearing as "residency" spurs (B10), the PRIVACY headline reflects the effective candidate pool not `onprem_only` (B11), and the all-frontier baseline is priced on real input+output tokens so routing savings stop going negative (B12).

**Architecture:** B10 is a backend candidate-pool filter + reason split (plus a one-line UI reason). B11 is a UI headline taxonomy. B12 is a cost-model correction: `_baseline` prices input and output at their respective frontier rates, which requires a new per-hop `DecisionRecord.tokens_out` for the multi-step path.

**Tech Stack:** Python 3.12 / FastAPI / Pydantic v2 / pytest (backend); React + Vite + TypeScript + Tailwind (frontend). No UI unit-test runner — frontend verification is `tsc -b --noEmit` + `vite build`.

## Global Constraints

- No change to routing/scoring/escalation logic or privacy guarantees. B10 only removes **disabled** providers from the candidate pool (they were never a legal destination); the chosen model and every privacy invariant are unchanged.
- The additive cost identity `compression_saved + routing_saved == baseline_usd − total` MUST hold after B12, on both the direct and multi-step paths.
- `baseline_model` is the **most-expensive frontier** model (`config/pricing.yaml`, currently `bedrock-frontier`). After B12, `routing_saved ≥ 0` is the norm (nothing is pricier than the all-frontier baseline). Keep the no-clamp code defensively, but expect the natural negative-routing_saved scenario to disappear.
- Honesty rules: entity **types** only, no raw PII, no CCR contents. The PRIVACY card shows `detected_entities` (types) as chips.
- **Known test interactions (reconcile, do not ignore):**
  - B10: default config disables azure+bedrock, so `effective_pool()` will now exclude `azure-gpt-mid`/`bedrock-frontier` by default. Any existing test asserting those appear as candidates or as "excluded by policy (residency)" spurs must be updated. Run the full backend suite and reconcile.
  - B12: `tests/test_two_lever_cost.py::test_routing_saved_goes_negative_under_escalation_identity_holds` asserts the **buggy** mechanism (`total > baseline`, `routing_saved < 0`) — it escalates to `bedrock-frontier`, which **is** `baseline_model`, so post-fix `routing_saved == 0`. This test must be reworked (see Task 3).
- Env: rtk mangles `npm run` and `2>&1`. Frontend from `ui/`: `./node_modules/.bin/tsc -b --noEmit` and `./node_modules/.bin/vite build`. Backend: `uv run pytest <path> -q -p no:cacheprovider` (the full suite has a known aiosqlite teardown flake that surfaces 4 unrelated tests as failures under full-suite teardown; they pass in isolation and with `-W ignore` — do not chase them).

---

### Task 1: B10 — disabled providers stop being candidates + correct rejection reason (backend)

**Files:**
- Modify: `app/apps/registry.py:60-67` (`effective_pool`)
- Modify: `app/routing/router.py:60-66` (rejection reason)
- Test: `tests/test_model_registry.py`, `tests/test_router.py` (add cases; reconcile existing)

**Interfaces:**
- Consumes: `settings.serving.providers: dict[str, ProviderCfg]` where `ProviderCfg.enabled: bool`; `ExecutionPolicyContext.onprem_only: bool` (already present).
- Produces: `effective_pool()` returns only candidates whose provider is enabled; `Router` emits `"excluded · residency"` vs `"not permitted for application"` instead of the conflated `"excluded by policy (residency)"`.

- [ ] **Step 1: Write the failing registry test**

Add to `tests/test_model_registry.py` (mirror the file's existing fixture style for building a `ModelRegistry` from settings):

```python
def test_effective_pool_omits_disabled_provider_candidates():
    from app.config.loader import load_settings
    from app.apps.registry import ModelRegistry
    s = load_settings()  # default: azure + bedrock disabled
    reg = ModelRegistry(s)
    # cold cache => YAML-only pool; disabled cloud providers must not appear
    names = {c.name for c in reg.effective_pool()}
    assert "azure-gpt-mid" not in names      # azure disabled in serving.yaml
    assert "bedrock-frontier" not in names   # bedrock disabled in serving.yaml

def test_effective_pool_includes_enabled_provider_candidates():
    from app.config.loader import load_settings
    from app.apps.registry import ModelRegistry
    s = load_settings()
    prov = dict(s.serving.providers)
    prov["bedrock"] = prov["bedrock"].model_copy(update={"enabled": True})
    s = s.model_copy(update={"serving": s.serving.model_copy(update={"providers": prov})})
    reg = ModelRegistry(s)
    assert "bedrock-frontier" in {c.name for c in reg.effective_pool()}
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_model_registry.py::test_effective_pool_omits_disabled_provider_candidates -q -p no:cacheprovider`
Expected: FAIL (today `effective_pool` includes the disabled candidates).

- [ ] **Step 3: Filter `effective_pool` by provider availability**

In `app/apps/registry.py`, replace `effective_pool` (lines 60-67) with:

```python
    def effective_pool(self) -> list[PoolCandidate]:
        # A static (YAML) cloud candidate is only routable when its provider is
        # enabled in serving.yaml. A killed provider must never enter the pool —
        # otherwise it surfaces as a phantom "residency" spur on the router card
        # (B10). Onboarded on-prem rows are gated per-model by r.enabled; the
        # on-prem infra kill-switch is handled in policy/context via
        # has_healthy_onprem, so it is not re-applied here.
        enabled = {p for p, c in self._s.serving.providers.items() if c.enabled}
        pool = [c for c in self._s.routing.model_pool if c.provider in enabled]
        for r in self._onboarded().values():
            if r.enabled:
                pool.append(PoolCandidate(
                    name=r.slug, provider=r.provider, tier=r.tier,
                    display_name=r.display_name, quality=r.quality))
        return pool
```

- [ ] **Step 4: Split the router rejection reason**

In `app/routing/router.py`, replace the first rejection block (lines 61-66) with:

```python
            if c.provider not in ctx.allowed_providers:
                reason = ("excluded · residency" if ctx.onprem_only
                          else "not permitted for application")
                rejected.append(RejectedCandidate(
                    name=c.name, provider=c.provider, tier=c.tier,
                    display_name=c.display_name, reason=reason))
                continue
```

- [ ] **Step 5: Add a router reason test**

Add to `tests/test_router.py` (mirror its existing `Router` construction; build a context with `onprem_only=True` and a cloud candidate in the pool to force the residency rejection):

```python
def test_residency_rejection_is_labeled_residency_not_generic():
    from app.config.loader import load_settings
    from app.apps.registry import ModelRegistry
    from app.routing.router import Router
    from app.policy.context import build_context
    from app.models import ChatRequest, Message
    s = load_settings()
    prov = dict(s.serving.providers)
    prov["bedrock"] = prov["bedrock"].model_copy(update={"enabled": True})
    s = s.model_copy(update={"serving": s.serving.model_copy(update={"providers": prov})})
    reg = ModelRegistry(s)
    # hospital tenant is onprem_only in the default policy.yaml
    ctx = build_context(s, "req", "hospital", ["US_SSN"], registry=reg)
    assert ctx.onprem_only is True
    dec = Router(s, registry=reg).select(ctx, tokens_after=50, complexity=0.1)
    reasons = {r.name: r.reason for r in dec.rejected}
    # a cloud candidate dropped by residency reads "residency", never the app-policy label
    assert any(v == "excluded · residency" for v in reasons.values())
```

Note: adjust the `build_context` call to the module's actual signature (check `app/policy/context.py` — it may take the request/etypes differently). Keep the assertion: an onprem_only request labels cloud rejections `"excluded · residency"`.

- [ ] **Step 6: Run the new tests + full suite; reconcile fallout**

Run: `uv run pytest tests/test_model_registry.py tests/test_router.py tests/test_infra_router.py -q -p no:cacheprovider`
Then the full suite: `uv run pytest -q -p no:cacheprovider -W ignore`
Expected: new tests PASS. Any existing test that asserted a disabled provider appears as a candidate or as `"excluded by policy (residency)"` must be updated to the new behavior (no candidate / precise reason). Fix them and note each in your report.

- [ ] **Step 7: Commit**

```bash
git add app/apps/registry.py app/routing/router.py tests/test_model_registry.py tests/test_router.py
git commit -m "fix(routing): B10 — disabled providers leave the candidate pool; split rejection reason

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 2: B10 + B11 — RequestDrilldown trace honesty (UI)

**Files:**
- Modify: `ui/src/console/RequestDrilldown.tsx:315-316` (ProviderStrip reason — B10)
- Modify: `ui/src/console/RequestDrilldown.tsx:432-437` (PRIVACY headline — B11)

**Interfaces:**
- Consumes: `p.onprem_only: boolean`, `p.allowed_providers: string[]`, `p.detected_entities: string[]`. The `NEUTRAL` accent constant already exists (`RequestDrilldown.tsx:358`). `entities = p.detected_entities` is already in scope near the privacy block (line 422).
- Produces: honest ProviderStrip reasons and a 4-case PRIVACY headline.

- [ ] **Step 1: Fix the ProviderStrip reason (B10)**

In `ui/src/console/RequestDrilldown.tsx`, change the `ProviderStrip` reason (line 316) from:

```tsx
  const excludedReason = onpremOnly ? "excluded — residency" : "excluded by policy";
```

to:

```tsx
  const excludedReason = onpremOnly ? "excluded · residency" : "provider off";
```

- [ ] **Step 2: Replace the PRIVACY headline derivation (B11)**

In `ui/src/console/RequestDrilldown.tsx`, replace the privacy verdict block (lines 432-437) with the taxonomy:

```tsx
  // Privacy — derived from the EFFECTIVE candidate pool, not onprem_only alone.
  // onprem_only=false only means residency didn't force on-prem; it does NOT mean
  // cloud is reachable (the kill-switch may have disabled it). Read allowed_providers.
  const hasCloud = p.allowed_providers.some((x) => x !== "onprem" && x !== "mock");
  const hasEntities = p.detected_entities.length > 0;
  const entityList = p.detected_entities.join(", ");
  let privacyAccent: string;
  let privacyVerdict: string;
  let privacyWhy: string;
  if (p.onprem_only) {
    privacyAccent = SHIELD;
    privacyVerdict = "Kept on-prem";
    privacyWhy = hasEntities
      ? `Detected ${entityList} — residency requires on-prem serving; cloud was ineligible.`
      : "Residency policy requires on-prem serving; cloud was ineligible.";
  } else if (!hasCloud) {
    privacyAccent = NEUTRAL;
    privacyVerdict = "On-prem only";
    privacyWhy = "Cloud providers are disabled — this request was served on-prem.";
  } else {
    privacyAccent = CLEAR;
    privacyVerdict = "Cloud-eligible";
    privacyWhy = hasEntities
      ? `Detected ${entityList}, but none require on-prem residency — every provider was eligible.`
      : "No PII detected — every provider was eligible.";
  }
```

(The existing `entities.length > 0` chip block below at ~line 464 still renders the type chips using `privacyAccent` — leave it; it now inherits the correct accent per case.)

- [ ] **Step 3: Typecheck**

Run: `cd ui && ./node_modules/.bin/tsc -b --noEmit`
Expected: exit 0. (`SHIELD`, `CLEAR`, `NEUTRAL` are all already declared at lines 355-358.)

- [ ] **Step 4: Build**

Run: `cd ui && ./node_modules/.bin/vite build`
Expected: exit 0.

- [ ] **Step 5: Manual verification (four cases)**

Confirm on real drilldowns: (a) onprem_only PII request → "Kept on-prem" amber, names the entity types; (b) non-sensitive request with cloud disabled → "On-prem only" neutral gray, "Cloud providers are disabled"; (c) cloud-enabled request with entities → "Cloud-eligible" green, "Detected … but none require on-prem"; (d) cloud-enabled clean request → "Cloud-eligible" green, "No PII detected". No card ever says "no sensitive data found" while entity chips are shown. ProviderStrip shows "provider off" for a kill-switch-disabled provider, "excluded · residency" for an onprem_only request.

- [ ] **Step 6: Commit**

```bash
git add ui/src/console/RequestDrilldown.tsx
git commit -m "fix(ui): B10/B11 — honest ProviderStrip reason + PRIVACY headline from allowed_providers

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 3: B12 — price the all-frontier baseline on real input+output tokens (backend)

**Files:**
- Modify: `app/models.py:84-96` (add `DecisionRecord.tokens_out`)
- Modify: `app/agent/internal_endpoint.py:171-179` (populate `tokens_out`)
- Modify: `app/pipeline.py:58-60` (`_baseline` signature) and call sites `150-153` (direct) + `228-245` (multi-step)
- Test: `tests/test_two_lever_cost.py` (rework the negative test + add B12 tests)

**Interfaces:**
- Consumes: `GenerationResult.tokens_out` (direct: `result.tokens_out`, `pipeline.py:180`; per-hop: `gen.tokens_out` in `internal_endpoint.py`). `PriceCfg.input`/`.output` from `pricing.models[baseline_model]`.
- Produces: `_baseline(in_tokens, out_tokens) -> float`; a new `DecisionRecord.tokens_out: int = 0`.

- [ ] **Step 1: Write the failing B12 tests**

Replace `tests/test_two_lever_cost.py::test_routing_saved_goes_negative_under_escalation_identity_holds` with the corrected-behavior test, and add the B12 repro. The escalation now lands on `bedrock-frontier` which **is** `baseline_model`, so `routing_saved == 0`:

```python
def test_escalation_to_baseline_model_yields_zero_routing_saved_identity_holds():
    """After B12 the all-frontier baseline is priced on the SAME input+output
    tokens as `total`. Escalating to bedrock-frontier — which IS baseline_model —
    therefore yields routing_saved == 0 (not the old bug's negative value), and
    the additive identity still holds exactly. routing_saved is never negative
    here because baseline_model is the most expensive frontier tier."""
    import asyncio
    from app.compression.ccr import CCRStore
    from app.config.loader import load_settings
    from tests._litellm_fake import _DeterministicLiteLLM
    from app.gateway.gateway import Gateway
    from app.pipeline import Pipeline
    from app.policy.detectors import make_detector
    from app.policy.gate import Gate
    from app.routing.router import Router
    from app.models import ChatRequest, Message
    s = load_settings()
    prov = dict(s.serving.providers)
    prov["bedrock"] = prov["bedrock"].model_copy(update={"enabled": True})
    s = s.model_copy(update={"serving": s.serving.model_copy(update={"providers": prov})})
    det = make_detector("regex")
    co = s.serving.context_optimization
    pipe = Pipeline(s, Gate(s, det), det,
                    Gateway(s, _DeterministicLiteLLM(s), Router(s)),
                    self_port=8080, co_cfg_provider=lambda: co, ccr=CCRStore(3600))
    req = ChatRequest(messages=[Message(role="user", content="Email john.doe@example.com re: Q3")])
    object.__setattr__(req, "mock_confidence", 0.1)  # force escalation to frontier
    out = asyncio.run(pipe.run("rid", "acme", req))
    c = out["cost"]
    assert out["model"] == "bedrock-frontier"          # escalated onto the frontier tier
    assert abs(c["routing_saved_usd"]) < 1e-9          # chosen == baseline model => 0, not negative
    total_saved = c["baseline_usd"] - c["total_usd"]
    assert abs((c["compression_saved_usd"] + c["routing_saved_usd"]) - total_saved) < 1e-12


def test_cheaper_tier_request_has_positive_routing_saved():
    """A normal request routes to a cheap on-prem model; priced against the
    all-frontier (bedrock-frontier) baseline on the SAME in+out tokens, routing
    must SAVE money (positive), and the identity holds. This is the B12 repro
    (was negative before the fix)."""
    import asyncio
    from app.compression.ccr import CCRStore
    from app.config.loader import load_settings
    from tests._litellm_fake import _DeterministicLiteLLM
    from app.gateway.gateway import Gateway
    from app.pipeline import Pipeline
    from app.policy.detectors import make_detector
    from app.policy.gate import Gate
    from app.routing.router import Router
    from app.models import ChatRequest, Message
    s = load_settings()
    det = make_detector("regex")
    co = s.serving.context_optimization
    pipe = Pipeline(s, Gate(s, det), det,
                    Gateway(s, _DeterministicLiteLLM(s), Router(s)),
                    self_port=8080, co_cfg_provider=lambda: co, ccr=CCRStore(3600))
    req = ChatRequest(messages=[Message(role="user", content="What is the capital of France?")])
    out = asyncio.run(pipe.run("rid", "acme", req))
    c = out["cost"]
    assert c["total_usd"] < c["baseline_usd"]          # cheaper than all-frontier
    assert c["routing_saved_usd"] > 0                  # B12: positive, was negative
    total_saved = c["baseline_usd"] - c["total_usd"]
    assert abs((c["compression_saved_usd"] + c["routing_saved_usd"]) - total_saved) < 1e-12
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_two_lever_cost.py -q -p no:cacheprovider`
Expected: the two new tests FAIL (today baseline is priced on prompt tokens only, so `routing_saved` is wrong/negative).

- [ ] **Step 3: Add `tokens_out` to `DecisionRecord`**

In `app/models.py`, add a field to `DecisionRecord` (after `tokens_priced`, line 94):

```python
    tokens_priced: int = 0
    tokens_out: int = 0   # completion tokens for this hop (multi-step baseline pricing, B12)
```

- [ ] **Step 4: Populate `tokens_out` on each hop**

In `app/agent/internal_endpoint.py`, in the `HOPS.record(run_id, DecisionRecord(...))` call (lines 171-179), add `tokens_out=gen.tokens_out` alongside `tokens_priced=tokens_after`:

```python
        hop_index=hop_index, tokens_priced=tokens_after, tokens_out=gen.tokens_out,
```

- [ ] **Step 5: Change `_baseline` to price input+output at frontier rates**

In `app/pipeline.py`, replace `_baseline` (lines 58-60):

```python
    def _baseline(self, in_tokens: int, out_tokens: int) -> float:
        """All-frontier baseline: input and output priced at the baseline
        (most-expensive frontier) model's respective rates — the SAME token
        basis `total` uses, so routing_saved compares like with like (B12)."""
        p = self._s.pricing.models[self._s.pricing.baseline_model]
        return (in_tokens * p.input + out_tokens * p.output) / 1_000_000
```

- [ ] **Step 6: Fix the direct-path call site**

In `app/pipeline.py`, at the direct-path cost block (lines 150-153), pass the completion tokens (`result.tokens_out`, already used for `usage.completion_tokens` at line 180) to both baselines:

```python
        baseline_usd = self._baseline(comp.tokens_before, result.tokens_out)
        baseline_after = self._baseline(comp.tokens_after, result.tokens_out)
        compression_saved = max(0.0, baseline_usd - baseline_after)
        routing_saved = baseline_after - total
```

- [ ] **Step 7: Fix the multi-step call site**

In `app/pipeline.py`, at the multi-step cost block (lines 228-238), price each hop's baseline on `(input, output)` and pair compressions to hops by index:

```python
        baseline_after = sum(self._baseline(h.tokens_priced, h.tokens_out)
                             for h in result.hops)
        # baseline_before pairs each compression's pre-compression input with its
        # hop's output (compressions are 1:1 with hops; see comment above).
        if result.compressions:
            baseline_before = sum(
                self._baseline(cr.tokens_before, h.tokens_out)
                for cr, h in zip(result.compressions, result.hops))
        else:
            baseline_before = baseline_after
        compression_saved_usd = max(0.0, baseline_before - baseline_after)
```

- [ ] **Step 8: Run the B12 tests + full cost suite**

Run: `uv run pytest tests/test_two_lever_cost.py -q -p no:cacheprovider`
Expected: PASS (both new tests + the reworked one).

- [ ] **Step 9: Run the full backend suite; reconcile**

Run: `uv run pytest -q -p no:cacheprovider -W ignore`
Expected: green apart from the known aiosqlite teardown flake (4 tests that pass in isolation). Any other cost/baseline-dependent test that assumed the old prompt-only baseline must be updated — note each in your report. Check `tests/test_agent_pipeline_e2e.py` for multi-step cost assertions.

- [ ] **Step 10: Commit**

```bash
git add app/models.py app/agent/internal_endpoint.py app/pipeline.py tests/test_two_lever_cost.py
git commit -m "fix(cost): B12 — price all-frontier baseline on real in+out tokens; routing_saved no longer negative

Adds DecisionRecord.tokens_out for the multi-step baseline. Preserves the
additive identity compression_saved + routing_saved == baseline - total.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Self-Review

**Spec coverage:**
- B10 (effective_pool filter + reason split + ProviderStrip) → Task 1 + Task 2 Step 1. ✓
- B11 (headline taxonomy from allowed_providers + entities, NEUTRAL for cloud-off, no "no sensitive data" with entities) → Task 2 Step 2. ✓
- B12 (baseline in+out rate model, DecisionRecord.tokens_out, both call sites, identity, tests) → Task 3. ✓
- Additive identity preserved / no-clamp kept defensively → Global Constraints + Task 3 tests. ✓
- Known test interactions (B10 pool ripple, B12 negative-test rework) → Global Constraints + Task 1 Step 6 + Task 3 Step 1. ✓

**Placeholder scan:** Task 1 Step 5 notes the `build_context` signature may differ — the implementer must check `app/policy/context.py` and adapt while keeping the stated assertion; that is a real (grounded) instruction, not a placeholder. No TODO/TBD elsewhere.

**Type consistency:** `_baseline(in_tokens, out_tokens)` defined in Task 3 Step 5 and called with two args in Steps 6-7; `DecisionRecord.tokens_out` defined Step 3, populated Step 4, consumed Step 7. `effective_pool` returns `list[PoolCandidate]` unchanged in shape. `excludedReason` string swap only.

**Ordering:** Task 1 (backend) → Task 2 (UI, independent of Task 1 but B11 relies on `allowed_providers` already excluding disabled cloud, which is pre-existing) → Task 3 (independent cost-model change). Tasks touch disjoint files except both Task 1 and Task 3 add backend tests in different files. Any order works, but 1 → 2 → 3 is the natural review order.
