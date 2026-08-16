# Cluster C — Serving correctness & honest failures Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the catch-all `503 "policy evaluation unavailable"` with an honest per-failure error taxonomy (B1), make the demo-default Presidio detector catch the textbook SSN `123-45-6789` on both gates (B4), and close out the already-fixed multi-step latency bug with test hardening (B5).

**Architecture:** Introduce typed exceptions (`app/errors.py`) that the route maps to distinct HTTP statuses; extract the redaction-safe entity merge into a leaf module (`app/policy/merge.py`) so `PresidioDetector` can supplement Presidio with the existing regex `US_SSN` pattern without an import cycle. No routing/policy behaviour change — only failure labels, wider SSN detection, and tests.

**Tech Stack:** Python 3.12, FastAPI, Pydantic v2, pytest, `uv`. Presidio (optional extra) for the demo-default detector. `nat` (optional extra) for the agent runtime — must never be required to import `app.agent.runtime` at module top level.

**Spec:** `docs/superpowers/specs/2026-08-06-cluster-c-serving-correctness-design.md`

## Global Constraints

Every task's requirements implicitly include these:

- **Fail closed (CLAUDE.md rule 5).** Every B1 error branch must still *reject* the request (4xx/5xx). Never turn a failure into a served response. The gate/policy stage keeps its **503**. The residual `except Exception` maps to **500**, which still rejects.
- **`onprem_only` absolute (rule 3).** B4 must make detection strictly *wider* (catch more SSNs), never narrower. B1's `NoEligibleCandidate → 422` is the onprem_only-with-no-healthy-onprem rejection — it stays a rejection.
- **Telemetry redaction (rule 2) unchanged.** Do NOT touch `app/obs/redact.py` / `app/obs/export.py` — the telemetry path already uses `RegexDetector` and already catches this SSN.
- **No behaviour change beyond the above.** Chosen model, cost math (the two-lever additive identity), and the decision trace shape are untouched.
- **Exception subclassing (load-bearing, minimizes test churn):** `NoEligibleCandidate(ValueError)` and `BackendUnavailable(RuntimeError)` — existing `pytest.raises(ValueError, match="no candidates after residency filter")` (`tests/test_derived_onprem_reject.py:50`, `tests/test_router.py:152`) and `pytest.raises(RuntimeError)` (`tests/test_litellm_egress.py:62`) must keep passing. `PolicyGateError(Exception)`.
- **`nat` optional.** Anything importing `app.agent.runtime` must only import module-level symbols (e.g. `AgentCapExceeded`, `run`) that do not require `nat` at import time. `nat` itself is imported lazily inside `run()`.
- **Test run command (WSL2):** `uv run pytest -q -p no:cacheprovider -W ignore` (the `-W ignore` suppresses the known aiosqlite event-loop-teardown flake in `test_langfuse_provision` / `test_traffic_*`, which is unrelated to this cluster). Avoid `2>&1` and `npm run` (rtk hook mangles them); no frontend build is needed for this cluster.

---

### Task 1: Extract the redaction-safe merge into a leaf module (B4 prep)

Pure move so `PresidioDetector` (Task 2) can reuse `EffectiveDetector`'s battle-tested overlap merge without the `effective.py ↔ detectors.py` import cycle. Behaviour identical; keeps the suite green as a foundation.

**Files:**
- Create: `app/policy/merge.py`
- Modify: `app/policy/effective.py:6,19,29-70`
- Test: `tests/test_policy_merge.py` (new)

**Interfaces:**
- Produces: `app.policy.merge.dedupe(ents: list[Entity]) -> list[Entity]` (public rename of the current `_dedupe`), and module-private `_score(e: Entity) -> float`. Same transitive-overlap cluster-merge semantics as today.
- Consumes: `app.models.Entity`.

- [ ] **Step 1: Write the failing test**

`tests/test_policy_merge.py`:
```python
from app.models import Entity
from app.policy.merge import dedupe


def test_non_overlapping_entities_pass_through():
    ents = [Entity(type="US_SSN", start=0, end=11, value="482-73-1946"),
            Entity(type="EMAIL_ADDRESS", start=20, end=27, value="a@b.co")]
    out = dedupe(ents)
    assert len(out) == 2


def test_overlapping_entities_merge_to_one_covering_span():
    # Two partially overlapping spans must collapse to ONE placeholder span
    # covering [min(start), max(end)) so no raw tail leaks (see the module
    # docstring / effective.py rationale).
    ents = [Entity(type="US_SSN", start=0, end=11, value="482-73-1946"),
            Entity(type="US_BANK_NUMBER", start=8, end=15, value="1946123")]
    out = dedupe(ents)
    assert len(out) == 1
    assert out[0].start == 0 and out[0].end == 15


def test_empty_is_empty():
    assert dedupe([]) == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_policy_merge.py -q -p no:cacheprovider -W ignore`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.policy.merge'`.

- [ ] **Step 3: Create `app/policy/merge.py`**

Move `_score` and `_dedupe` (renamed `dedupe`) out of `effective.py` verbatim:
```python
"""Redaction-safe entity merge, shared by EffectiveDetector (custom recognizers
over a base detector) and PresidioDetector (regex US_SSN supplement). A leaf
module so both app/policy/effective.py and app/policy/detectors.py import it
without a cycle (effective.py already imports from detectors.py)."""
from __future__ import annotations

from app.models import Entity


def _score(e: Entity) -> float:
    return getattr(e, "score", 0.0) or 0.0


def dedupe(ents: list[Entity]) -> list[Entity]:
    # Redaction-safe merge: entities can PARTIALLY overlap (e.g. a custom
    # recognizer matching a substring of a longer built-in match, or vice
    # versa). anonymize() splices one placeholder per entity span, so two
    # overlapping entities can never both be redacted independently --
    # dropping the "loser" of an overlap (as a naive greedy scan would)
    # always risks leaving the loser's non-overlapping tail as raw PII in
    # the output. Instead, group entities that overlap (transitively) into
    # a single cluster and emit ONE entity spanning the whole cluster
    # [min(start), max(end)) -- this guarantees the entire sensitive region
    # is covered by exactly one placeholder. Non-overlapping entities pass
    # through unchanged.
    if not ents:
        return []
    ordered = sorted(ents, key=lambda e: e.start)
    clusters: list[list[Entity]] = [[ordered[0]]]
    cluster_end = ordered[0].end
    for e in ordered[1:]:
        if e.start < cluster_end:
            clusters[-1].append(e)
            cluster_end = max(cluster_end, e.end)
        else:
            clusters.append([e])
            cluster_end = e.end

    merged: list[Entity] = []
    for cluster in clusters:
        if len(cluster) == 1:
            merged.append(cluster[0])
            continue
        start = min(e.start for e in cluster)
        end = max(e.end for e in cluster)
        # "Best" member decides type/score/value: highest score (None -> 0.0),
        # then longest span, as the tiebreak.
        best = max(cluster, key=lambda e: (_score(e), e.end - e.start))
        merged.append(Entity(type=best.type, start=start, end=end,
                             value=best.value, score=best.score))
    return merged
```

- [ ] **Step 4: Re-point `app/policy/effective.py` at the moved function**

Delete the `_score` and `_dedupe` definitions (lines 29-70) from `effective.py`. Update the import (line 6 area) and the call (line 19):
```python
from app.policy.detectors import Detector, RegexDetector
from app.policy.merge import dedupe
```
```python
    def analyze(self, text: str) -> list[Entity]:
        ents = list(self._base.analyze(text))
        for r in self._recognizers:
            ents.extend(r.find(text))
        return dedupe(ents)
```
(Leave `EffectiveDetector.anonymize` and `warmup` unchanged.)

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/test_policy_merge.py "tests/test_recognizers.py" "tests/test_egress_custom_redaction.py" -q -p no:cacheprovider -W ignore`
Expected: PASS (the new merge tests + the existing EffectiveDetector consumers, unchanged in behaviour).

- [ ] **Step 6: Commit**

```bash
git add app/policy/merge.py app/policy/effective.py tests/test_policy_merge.py
git commit -m "refactor(policy): extract redaction-safe entity merge into app/policy/merge (B4 prep)"
```

---

### Task 2: Layer regex US_SSN over Presidio (B4 fix)

Make the composite `PresidioDetector` catch the textbook SSN `123-45-6789` that Presidio's `UsSsnRecognizer` deliberately blocklists, on both ingress (gate) and egress (verify). Only the `US_SSN` pattern is layered — Presidio's own EMAIL/PHONE/CREDIT_CARD recognizers are better and stay untouched.

**Files:**
- Modify: `app/policy/detectors.py:77-99` (`PresidioDetector`), add import near line 6
- Modify: `tests/test_detectors.py:68-80` (rework the pinned blocklist test)
- Test: `tests/test_output_controls.py` (add a Presidio egress test)

**Interfaces:**
- Consumes: `app.policy.merge.dedupe` (Task 1), the existing `_PATTERNS["US_SSN"]` regex in `detectors.py`.
- Produces: no signature change — `PresidioDetector.analyze` now returns Presidio entities ∪ regex `US_SSN` matches, deduped.

- [ ] **Step 1: Write the failing tests**

Rework `tests/test_detectors.py`. Replace `test_presidio_blocklists_sample_ssns` (lines 68-80) with two tests — keep the upstream pin at the RAW Presidio layer, and assert the composite now catches it:
```python
def test_presidio_raw_still_blocklists_sample_ssn():
    """Pin the upstream fact we design around: raw Presidio (no supplement)
    still drops the sample SSN. If a future Presidio release stops blocklisting
    sample SSNs this fails loudly -- the signal to revisit
    tests/traffic/personas.fake_ssn's constraints."""
    pytest.importorskip("presidio_analyzer")
    from presidio_analyzer import AnalyzerEngine
    results = AnalyzerEngine().analyze(
        text=f"my ssn is {PRESIDIO_BLOCKLISTED_SSN} ok", language="en")
    assert not any(r.entity_type == "US_SSN" for r in results), (
        "Presidio now flags sample SSNs at the raw layer; fake_ssn's constraints "
        "can be relaxed"
    )


def test_presidio_composite_catches_blocklisted_ssn():
    """B4: the composite PresidioDetector supplements Presidio with the regex
    US_SSN pattern, so the textbook 123-45-6789 IS flagged -- closing the gap
    where an SSN slipped past both gates under the demo-default config."""
    pytest.importorskip("presidio_analyzer")
    d = make_detector("presidio")
    ents = d.analyze(f"my ssn is {PRESIDIO_BLOCKLISTED_SSN} ok")
    assert any(e.type == "US_SSN" for e in ents), (
        "composite PresidioDetector must catch the sample SSN via the regex "
        "US_SSN supplement (B4)"
    )
```

Add a Presidio egress test to `tests/test_output_controls.py` (mirrors `test_pii_in_completion_is_redacted` but with the demo-default detector and the blocklisted SSN):
```python
import pytest
from app.policy.detectors import make_detector


def test_presidio_egress_masks_blocklisted_ssn():
    """B4: a completion containing the textbook 123-45-6789 must be masked on
    egress under the demo-default presidio detector -- not left raw."""
    pytest.importorskip("presidio_analyzer")
    d = make_detector("presidio")
    text, rec = verify(d, _ctx(False), "the record shows 123-45-6789 on file")
    assert "123-45-6789" not in text
    assert "US_SSN" in rec.entities_found
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest "tests/test_detectors.py::test_presidio_composite_catches_blocklisted_ssn" "tests/test_output_controls.py::test_presidio_egress_masks_blocklisted_ssn" -q -p no:cacheprovider -W ignore`
Expected: FAIL — the composite detector does not yet supplement Presidio, so the SSN is neither flagged nor masked. (If the `presidio_analyzer` extra is not installed locally, these `importorskip` and show as skipped — in that case verify against the container/CI where the extra is present; note the skip in the task report.)

- [ ] **Step 3: Compose US_SSN into `PresidioDetector.analyze`**

In `app/policy/detectors.py`, add the import near the top (after the existing imports):
```python
from app.policy.merge import dedupe
```
Update `PresidioDetector`:
```python
class PresidioDetector:
    """Real Presidio path (demo default). Lazy import so tests never need it.

    Supplements Presidio with the regex US_SSN pattern (B4): Presidio's
    UsSsnRecognizer deliberately blocklists textbook sample SSNs (e.g.
    123-45-6789), which would otherwise slip past BOTH the gate and egress
    verification under the demo-default config. Only US_SSN is layered --
    Presidio's own EMAIL/PHONE/CREDIT_CARD recognizers are stronger and are
    left as-is. dedupe() ensures a valid SSN Presidio already matched is not
    double-covered."""

    _SSN = _PATTERNS["US_SSN"]

    def __init__(self) -> None:
        from presidio_analyzer import AnalyzerEngine
        self._analyzer = AnalyzerEngine()

    def analyze(self, text: str) -> list[Entity]:
        results = self._analyzer.analyze(text=text, language="en")
        ents = [Entity(type=r.entity_type, start=r.start, end=r.end,
                       value=text[r.start:r.end]) for r in results]
        ents += [Entity(type="US_SSN", start=m.start(), end=m.end(),
                        value=m.group()) for m in self._SSN.finditer(text)]
        return dedupe(ents)
```
(Leave `anonymize` and `warmup` unchanged — `anonymize` already delegates to `RegexDetector().anonymize`.)

- [ ] **Step 4: Run the B4 tests + the detector suite to verify pass**

Run: `uv run pytest tests/test_detectors.py tests/test_output_controls.py -q -p no:cacheprovider -W ignore`
Expected: PASS (composite catches + egress masks; `test_presidio_raw_still_blocklists_sample_ssn` and `test_every_detector_flags_the_demo_ssn` still pass). Presidio tests skip cleanly if the extra is absent.

- [ ] **Step 5: Commit**

```bash
git add app/policy/detectors.py tests/test_detectors.py tests/test_output_controls.py
git commit -m "fix(policy): layer regex US_SSN over Presidio so sample SSNs are caught on both gates (B4)"
```

---

### Task 3: Typed exceptions + producers (B1 part 1)

Introduce the error types and make the router/gateway raise them instead of bare `ValueError`/`RuntimeError`. No route change yet (Task 4) — this task is the producers + their unit tests.

**Files:**
- Create: `app/errors.py`
- Modify: `app/routing/router.py:94,128` (+ import)
- Modify: `app/gateway/gateway.py:42,49,58` (+ import)
- Test: `tests/test_error_types.py` (new)

**Interfaces:**
- Produces: `app.errors.PolicyGateError(Exception)`, `app.errors.NoEligibleCandidate(ValueError)`, `app.errors.BackendUnavailable(RuntimeError)`.
- Consumes (unchanged): `Router.select` raising on empty pool; `Gateway.run`'s tier-exhaustion path.

- [ ] **Step 1: Write the failing tests**

`tests/test_error_types.py`:
```python
import pytest

from app.errors import BackendUnavailable, NoEligibleCandidate, PolicyGateError


def test_subclass_relationships_hold():
    # Load-bearing: existing pytest.raises(ValueError/RuntimeError) callers must
    # keep passing (tests/test_derived_onprem_reject.py, test_router.py,
    # test_litellm_egress.py).
    assert issubclass(NoEligibleCandidate, ValueError)
    assert issubclass(BackendUnavailable, RuntimeError)
    assert issubclass(PolicyGateError, Exception)


def test_router_raises_no_eligible_candidate_on_empty_pool():
    from app.config.loader import load_settings
    from app.models import ExecutionPolicyContext
    from app.routing.router import Router
    s = load_settings()
    # onprem_only with NO permitted provider -> residency filter empties the pool
    ctx = ExecutionPolicyContext(
        request_id="r", tenant_id="t", sensitivity="restricted", onprem_only=True,
        allowed_providers=frozenset(), allowed_models=frozenset(),
        max_cost_usd=1.0, max_latency_ms=10000, risk_class="high",
        require_verification=True, detected_entities=(), redaction_map_id=None)
    with pytest.raises(NoEligibleCandidate, match="no candidates after residency filter"):
        Router(s).select(ctx, tokens_after=100, complexity=0.1)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_error_types.py -q -p no:cacheprovider -W ignore`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.errors'`.

- [ ] **Step 3: Create `app/errors.py`**

```python
"""Typed serving errors mapped to honest HTTP statuses at the API route
(app/api/routes.py). Replaces the old catch-all 503 "policy evaluation
unavailable" (B1). Subclasses are chosen so existing pytest.raises(ValueError/
RuntimeError) callers keep passing."""
from __future__ import annotations


class PolicyGateError(Exception):
    """The identity/privacy/policy gate (or its detector) failed to evaluate.
    Fail-closed: the request is rejected. Route maps -> 503."""


class NoEligibleCandidate(ValueError):
    """No model survived the residency/budget/scope filter -- e.g. an
    onprem_only request with no healthy on-prem model (CLAUDE.md rule 3).
    Route maps -> 422. Subclasses ValueError for back-compat with existing
    pytest.raises(ValueError) router callers."""


class BackendUnavailable(RuntimeError):
    """Every reachable tier's backend failed (timeout/connection/provider
    unreachable). Route maps -> 502. Subclasses RuntimeError for back-compat
    with existing pytest.raises(RuntimeError) gateway callers."""
```

- [ ] **Step 4: Router raises `NoEligibleCandidate`**

`app/routing/router.py` — add `from app.errors import NoEligibleCandidate` (top), then replace both bare raises (same messages):
- Line 94: `raise NoEligibleCandidate("no candidates after residency filter")`
- Line 128: `raise NoEligibleCandidate("no candidates after budget filter")`

- [ ] **Step 5: Gateway raises `BackendUnavailable`**

`app/gateway/gateway.py` — add `from app.errors import BackendUnavailable` (top). Line 42 (the failed-tier loop guard):
```python
            if cand.tier in failed_tiers:
                raise BackendUnavailable(
                    "backend failed on all reachable tiers for this request")
```
And the backend-exception escalate block (lines 49-59) — capture the exception and wrap the final re-raise:
```python
            try:
                result = await self._backend.generate(
                    cand.name, messages, price, cand.tier, hint)
            except Exception as exc:
                records.append(DecisionRecord(
                    step="gateway", chosen=None,
                    reason=f"{cand.name} failed; escalating/fallback",
                    rejected=decision.rejected, est_cost_usd=decision.est_cost_usd,
                    latency_ms=0.0, hop_index=len(records)))
                failed_tiers.add(cand.tier)
                nxt = _NEXT[cand.tier]
                if nxt is None:
                    raise BackendUnavailable(
                        "backend failed on all reachable tiers for this request"
                    ) from exc
                min_tier = nxt
                continue
```

- [ ] **Step 6: Run tests to verify pass (incl. back-compat callers)**

Run: `uv run pytest tests/test_error_types.py tests/test_router.py tests/test_derived_onprem_reject.py tests/test_gateway.py tests/test_litellm_egress.py -q -p no:cacheprovider -W ignore`
Expected: PASS — new types raise as designed; the existing `pytest.raises(ValueError, match=...)` / `pytest.raises(RuntimeError)` callers still pass via the subclass relationship.

- [ ] **Step 7: Commit**

```bash
git add app/errors.py app/routing/router.py app/gateway/gateway.py tests/test_error_types.py
git commit -m "feat(errors): typed NoEligibleCandidate/BackendUnavailable/PolicyGateError raised by router+gateway (B1)"
```

---

### Task 4: Route error taxonomy + fail-closed gate wrap (B1 part 2)

Wire the typed exceptions to honest HTTP statuses at the route, wrap the gate/setup stages so genuine policy failures stay 503, and prove each mapping with a route test.

**Files:**
- Modify: `app/pipeline.py` (import + wrap gate stage lines 88-95)
- Modify: `app/api/routes.py` (imports + wrap setup lines 93-102 + taxonomy lines 105-114)
- Test: `tests/test_error_taxonomy.py` (new)

**Interfaces:**
- Consumes: `app.errors.{PolicyGateError, NoEligibleCandidate, BackendUnavailable}` (Task 3), `app.agent.runtime.AgentCapExceeded`, `app.pipeline.PipelineRejected`.
- Produces: HTTP status contract — 503 gate / 422 no-candidate / 500 agent-cap / 502 backend / 500 unexpected.

- [ ] **Step 1: Write the failing route tests**

`tests/test_error_taxonomy.py` (uses the shared `client` fixture + `Bearer demo-key`, same as `tests/test_e2e_slice.py`; monkeypatches the nearest seam that raises each error):
```python
import pytest

_AUTH = {"Authorization": "Bearer demo-key"}
_MSG = {"messages": [{"role": "user", "content": "hello"}]}


def test_genuine_gate_failure_is_503(client, monkeypatch):
    # A detector/gate crash must stay the fail-closed 503 (CLAUDE.md rule 5).
    from app.policy.gate import Gate
    def boom(self, *a, **k):
        raise RuntimeError("presidio exploded")
    monkeypatch.setattr(Gate, "evaluate", boom)
    r = client.post("/v1/chat/completions", headers=_AUTH, json=_MSG)
    assert r.status_code == 503
    assert r.json()["detail"] == "policy evaluation unavailable"


def test_no_eligible_candidate_is_422(client, monkeypatch):
    from app.errors import NoEligibleCandidate
    from app.routing.router import Router
    def empty(self, *a, **k):
        raise NoEligibleCandidate("no candidates after residency filter")
    monkeypatch.setattr(Router, "select", empty)
    r = client.post("/v1/chat/completions", headers=_AUTH, json=_MSG)
    assert r.status_code == 422
    assert "no model is available" in r.json()["detail"]


def test_backend_failure_is_502(client, monkeypatch):
    # Backend raises on every tier -> gateway exhausts -> BackendUnavailable.
    from app.gateway.backends import litellm_backend
    async def always_fail(**k):
        raise RuntimeError("connection refused")
    monkeypatch.setattr(litellm_backend, "acompletion", always_fail)
    r = client.post("/v1/chat/completions", headers=_AUTH, json=_MSG)
    assert r.status_code == 502
    assert r.json()["detail"] == "no backend could serve this request"


def test_agent_non_convergence_is_500(client, monkeypatch):
    # Multi-step path; agent runtime raises AgentCapExceeded (no nat needed --
    # decide_mode returns multi_step on mock_multistep, and we replace the
    # runtime's run() before it touches nat).
    import app.agent.runtime as runtime
    async def cap(*a, **k):
        raise runtime.AgentCapExceeded("did not converge")
    monkeypatch.setattr(runtime, "run", cap)
    r = client.post("/v1/chat/completions", headers=_AUTH,
                    json={"mock_multistep": True, **_MSG})
    assert r.status_code == 500
    assert r.json()["detail"] == "agent did not converge within its step limit"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_error_taxonomy.py -q -p no:cacheprovider -W ignore`
Expected: FAIL — today all four collapse to `503 "policy evaluation unavailable"` (or 500), so the 422/500/502 assertions fail.

- [ ] **Step 3: Wrap the gate stage in `app/pipeline.py`**

Add `from app.errors import PolicyGateError` near the top imports. Wrap the detector-construction + gate-evaluate block (current lines 88-95) so an unexpected failure becomes `PolicyGateError`, while a deliberate `PipelineRejected` is untouched:
```python
        try:
            if profile is not None:
                det = EffectiveDetector(self._detector, list(profile.recognizers))
                rules = effective_rules(profile, self._s)
            else:
                det, rules = self._detector, None
            ctx, sanitized, _map = self._gate.evaluate(
                request_id, tenant_id, request, app_policy=app_policy,
                detector=det, rules=rules)
        except PolicyGateError:
            raise
        except Exception as e:
            raise PolicyGateError("policy evaluation failed") from e
```
(The requested-model check at lines 96-108 stays exactly as-is — it raises `PipelineRejected`, which must remain a 422.)

- [ ] **Step 4: Wrap setup + install the taxonomy in `app/api/routes.py`**

Add imports near the top:
```python
from app.errors import BackendUnavailable, NoEligibleCandidate, PolicyGateError
from app.agent.runtime import AgentCapExceeded
```
(`AgentCapExceeded` is defined at `app.agent.runtime` module level above its lazy `nat` import, so this top-level import does not pull in `nat`. If import-time evaluation ever changes to require `nat`, move this import inside the handler instead.)

Wrap the profile-load / registry-load setup (current lines 93-102) so its failure keeps the fail-closed-503 intent, then map the pipeline exceptions (replace current lines 105-114):
```python
    try:
        try:
            cache = request.app.state.profile_cache
            profile = cache.get(tenant)
            if profile is None:
                async with request.app.state.db.session() as session:
                    profile = await store.get_privacy_profile(session, tenant)
                cache.put(tenant, profile)
            await request.app.state.registry.ensure_loaded(request.app.state.db)
        except Exception as e:
            raise PolicyGateError("policy setup failed") from e
        resp = await request.app.state.pipeline.run(
            request_id, tenant, body, app_policy=app_policy, profile=profile)
    except PipelineRejected as e:
        message = str(e)
        prefix = ("model request rejected: "
                  if message.startswith("requested model")
                  else "response failed verification: ")
        raise HTTPException(422, f"{prefix}{message}")
    except NoEligibleCandidate as e:
        raise HTTPException(422, f"no model is available for this request's policy: {e}")
    except AgentCapExceeded:
        raise HTTPException(500, "agent did not converge within its step limit")
    except BackendUnavailable:
        raise HTTPException(502, "no backend could serve this request")
    except PolicyGateError:
        raise HTTPException(503, "policy evaluation unavailable")
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(500, "internal error")
```
Keep the original inline comment about the profile-load fail-closed rationale near the wrapped block. Note ordering: `NoEligibleCandidate`(ValueError) and `BackendUnavailable`(RuntimeError) are caught by their specific handlers before the generic `except Exception`; `AgentCapExceeded` and `BackendUnavailable` are siblings (both RuntimeError) so their relative order is free.

- [ ] **Step 5: Run the taxonomy tests + the full route/e2e suite**

Run: `uv run pytest tests/test_error_taxonomy.py tests/test_e2e_slice.py tests/test_token_auth.py tests/test_capture_io.py -q -p no:cacheprovider -W ignore`
Expected: PASS — each failure maps to its honest status; existing route behaviour (200 happy path, 401 auth, 422 verification/requested-model) unchanged.

- [ ] **Step 6: Commit**

```bash
git add app/pipeline.py app/api/routes.py tests/test_error_taxonomy.py
git commit -m "feat(api): honest error taxonomy replaces catch-all 503; fail-closed gate stays 503 (B1)"
```

---

### Task 5: B5 closeout — harden latency tests + fix stale comment + backlog

B5 is already fixed in code (per-hop latency flows end-to-end). Strengthen the two weak assertions so a regression to literal `0.0` is caught, correct the stale comment, and record B5 as resolved.

**Files:**
- Modify: `tests/test_litellm_multistep.py:32`
- Modify: `tests/test_agent_hop_compression.py:100`
- Modify: `app/gateway/backends/base.py:15-22`
- Modify: `docs/bugs-backlog.md` (B5 entry)

**Interfaces:** none (test + comment + docs only).

- [ ] **Step 1: Harden the two latency assertions**

`tests/test_litellm_multistep.py:32` — change to:
```python
        assert rec.latency_ms > 0  # measured (real) latency, not zero; never seeded
```
`tests/test_agent_hop_compression.py:100` — change to:
```python
    assert recs[0].latency_ms > 0  # real (measured) latency, never 0.0
```
(Both paths run under the deterministic fake, which seeds ≥200 ms per tier via `TIER_LATENCY_MS`, so `> 0` is safe and now guards against a regression to `0.0`.)

- [ ] **Step 2: Run the two tests to verify they still pass**

Run: `uv run pytest tests/test_litellm_multistep.py tests/test_agent_hop_compression.py -q -p no:cacheprovider -W ignore`
Expected: PASS (latency is non-zero by construction).

- [ ] **Step 3: Fix the stale comment in `app/gateway/backends/base.py`**

Replace the comment block at lines 15-22 (which wrongly says "the internal endpoint fakes the completion (it never calls generate)"):
```python
# Per-tier modeled latency, used ONLY by the deterministic test fake
# (tests/_litellm_fake.py's _DeterministicLiteLLM.generate) so a given tier
# gets a stable, non-zero latency in tests. The real LiteLLMBackend.generate
# measures actual wall-clock latency, and the agent per-hop path
# (app/agent/internal_endpoint.py) now makes a REAL backend.generate call and
# records that measured latency -- so multi-step requests report real per-hop
# latency, not 0 ms (B5, resolved). This table is the single source the fake
# reads; production never uses it.
TIER_LATENCY_MS = {"cheap": 200.0, "mid": 500.0, "frontier": 2000.0}
```

- [ ] **Step 4: Mark B5 resolved in `docs/bugs-backlog.md`**

Update the B5 entry (lines 66-72) to record it fixed — replace the body with a resolved note, e.g.:
```markdown
## B5 — Multi-step latency reported as 0 ms  ✅ RESOLVED (2026-08-06, Cluster C)

**Severity:** medium (resolved)
Per-hop latency now flows end-to-end: each `agent_hop` DecisionRecord records
the real measured `latency_ms` (`app/agent/internal_endpoint.py`, from the
backend's wall-clock timing), and `app/api/history.py` + the DB usage row sum
it into the request total; the UI drilldown reads the summed value. Closed out
in Cluster C with hardened `> 0` latency assertions
(`tests/test_litellm_multistep.py`, `tests/test_agent_hop_compression.py`) and a
corrected `app/gateway/backends/base.py` comment. Cross-ref memory
`routeforge-multistep-latency-zero-bug` (now stale/resolved).
```

- [ ] **Step 5: Run the affected suites once more**

Run: `uv run pytest tests/test_litellm_multistep.py tests/test_agent_hop_compression.py tests/test_gateway.py -q -p no:cacheprovider -W ignore`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add tests/test_litellm_multistep.py tests/test_agent_hop_compression.py app/gateway/backends/base.py docs/bugs-backlog.md
git commit -m "test(latency): harden multi-step latency asserts to >0; fix stale comment; mark B5 resolved (B5)"
```

---

## Final verification (after all tasks)

- [ ] Run the full backend suite: `uv run pytest -q -p no:cacheprovider -W ignore`
  Expected: green (mod the known WSL2 aiosqlite teardown flake in `test_langfuse_provision` / `test_traffic_*`, which pass in isolation — confirm any failure there is only that flake, not a Cluster C regression).
- [ ] Confirm no frontend change was needed (no `ui/` files touched).
- [ ] Confirm the additive cost identity and routing behaviour are unchanged (no edits to `_baseline`, router scoring, or the trace shape).

## Self-review notes (author)

- **Spec coverage:** B1 (Tasks 3-4), B4 (Tasks 1-2), B5 (Task 5) — all spec sections mapped.
- **Type consistency:** `NoEligibleCandidate(ValueError)` / `BackendUnavailable(RuntimeError)` / `PolicyGateError(Exception)` used identically in `app/errors.py`, the producers (Task 3), and the route map (Task 4). `dedupe` name consistent across `merge.py`, `effective.py`, `detectors.py`.
- **Plan-vs-review conflict flagged for SDD pre-flight:** Task 2 Step 1 *intentionally rewrites* `tests/test_detectors.py::test_presidio_blocklists_sample_ssns`, whose current assertion the B4 fix falsifies. This is deliberate (the pin moves to the raw layer; the composite now catches it) — not a weakened test. The reviewer should treat the reworked pair as the intended behaviour change, not a regression.
