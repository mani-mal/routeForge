# Cluster C — Serving correctness & honest failures (B1, B4, B5)

**Date:** 2026-08-06
**Status:** design, ready for implementation plan
**Source items:** `docs/bugs-backlog.md` B1, B4, B5
**Base:** `feat/infra-management-config` @ current HEAD (Cluster A PR #6 + Cluster B PR #7 merged)

## Purpose

Three serving-layer defects from the DGX burst backlog. Two are real and get
fixed; one (B5) is already fixed in code and gets closed out honestly.

- **B1** — every downstream failure (agent non-convergence, backend timeout,
  no-eligible-candidate) collapses into one misleading `503 "policy evaluation
  unavailable"`, pointing debugging at the gate (step 2) when the real failure
  is in steps 5–8.
- **B4** — under the demo-default `pii_detection: presidio`, the textbook SSN
  `123-45-6789` is missed on **both** ingress and egress because Presidio's
  `UsSsnRecognizer` deliberately blocklists sample SSNs. (The backlog framed
  this as egress-only; grounding showed it is a detector-coverage gap affecting
  both gates.)
- **B5** — already fixed: per-hop latency now flows end-to-end. Close it out
  with test hardening + a stale-comment fix so the backlog entry is honest.

## Non-negotiables preserved

- **Fail closed (CLAUDE.md rule 5).** Every B1 error branch still *rejects* the
  request. The taxonomy only changes the status code + message, never turns a
  failure into a served response. The gate/policy stage keeps its 503.
- **`onprem_only` absolute (rule 3).** B4 makes the detector catch *more* PII,
  never less. B1's "no eligible candidate" branch is exactly the
  onprem_only-with-no-healthy-onprem rejection — it stays a rejection, now with
  an honest message.
- **Telemetry redacted on egress (rule 2).** B4 strengthens egress redaction;
  the telemetry path (`app/obs/redact.py`, already RegexDetector-based) already
  caught this SSN and is unchanged.
- No routing/policy **behaviour** change: B1 relabels failures, B4 widens SSN
  detection, B5 is test-only. The chosen model, cost math, and privacy
  invariants are untouched.

## Pre-work findings (grounded, current line numbers)

### B1
- `app/api/routes.py:113-114` — `except Exception: raise HTTPException(503,
  "policy evaluation unavailable")` wraps the whole `pipeline.run()` call
  (`routes.py:103-104`) plus the profile-load / registry-load setup
  (`routes.py:93-102`). `PipelineRejected → 422` (`routes.py:105-110`) and
  `HTTPException` passthrough (`routes.py:111-112`) are already separated.
- Distinct downstream failures currently swallowed as 503:
  - `AgentCapExceeded(RuntimeError)` — `app/agent/runtime.py:95`, raised at
    `runtime.py:184` when NAT's `tool_calling_agent` exhausts `max_iterations`.
    **The headline mislabeled case.** Deliberately not caught in the pipeline
    (`app/pipeline.py:211-213` comment) — propagates to the route.
  - `RuntimeError("backend failed on all reachable tiers for this request")` —
    `app/gateway/gateway.py:42`.
  - Underlying LiteLLM exceptions (timeout/connection/API) — raised inside
    `backend.generate`, caught by the gateway escalate loop
    (`gateway.py:49-60`), re-raised at the final tier (`gateway.py:58`).
  - `RuntimeError` provider-not-enabled / onprem-no-endpoint —
    `app/gateway/backends/litellm_backend.py:38` and `:48` (raised inside
    `_map`, so surfaces via the gateway loop as a backend failure).
  - `ValueError("no candidates after residency filter")` — `router.py:94`;
    `ValueError("no candidates after budget filter")` — `router.py:128`. Called
    from both the direct path (`gateway.py:36`) and the per-hop path
    (`internal_endpoint.py:152`).
- The gate stage inside `pipeline.run` is `pipeline.py:88-95` (detector
  construction + `self._gate.evaluate(...)`). The requested-model check
  (`pipeline.py:96-108`) already raises `PipelineRejected` (→ 422) and must stay
  that way. Everything from `decide_mode` (`pipeline.py:111`) downward is
  post-gate.
- No test asserts the string `"policy evaluation unavailable"` or
  `status_code == 503`, so relabeling breaks nothing — but there is also **no
  test guarding the fail-closed 503 on a genuine gate failure**; add one.

### B4
- `app/policy/detectors.py:8-14` — `RegexDetector`'s `_PATTERNS` includes
  `"US_SSN": re.compile(r"\b\d{3}-\d{2}-\d{4}\b")`, which matches `123-45-6789`.
- `app/policy/detectors.py:84-87` — `PresidioDetector.analyze` returns only
  Presidio's own results. Presidio's `UsSsnRecognizer.invalidate_result()`
  blocklists textbook sample SSNs, so `123-45-6789` yields **no** `US_SSN`
  entity → the gate and egress verify find nothing to mask.
- `app/output/verify.py` (egress) and `app/policy/gate.py` (ingress) both call
  the same detector's `analyze`/`anonymize` — symmetric. So a detector-layer
  fix closes both gates at once. `verify()` already *masks* (not merely
  detect-and-reject); it is correct and untouched.
- `app/policy/effective.py:29-70` — `_dedupe`/`_score` implement a
  redaction-safe, transitive-overlap cluster-merge. `EffectiveDetector.analyze`
  uses it to merge custom recognizers onto a base detector. **Reuse this** for
  the B4 merge instead of writing a second dedupe.
- Import-cycle note: `effective.py` imports from `detectors.py`
  (`from app.policy.detectors import Detector, RegexDetector`). So
  `detectors.py` cannot import `_dedupe` from `effective.py` directly. Extract
  `_dedupe`/`_score` into a new leaf module `app/policy/merge.py` (no intra-`policy`
  deps) and import it from both.
- `tests/test_detectors.py:68-80` — `test_presidio_blocklists_sample_ssns`
  **pins** the raw-Presidio blocklist (asserts `123-45-6789` yields no
  `US_SSN`). The fix intentionally makes the *composite* `PresidioDetector`
  catch it, so this test must be reworked (see below). `test_detectors.py:51-65`
  (`test_every_detector_flags_the_demo_ssn`, parametrized regex+presidio) uses
  the non-blocklisted `482-73-1946` and keeps passing.

### B5
- `app/agent/internal_endpoint.py:176` — hop `DecisionRecord` carries
  `latency_ms=gen.latency_ms`; value from real wall-clock timing in
  `litellm_backend.py:76-78`; the test fake seeds non-zero per-tier latency
  (`base.py:22`, `TIER_LATENCY_MS = {cheap:200, mid:500, frontier:2000}`).
- `app/api/history.py:80-82` and `app/api/routes.py:180-182` both sum
  `latency_ms` across trace records; `pipeline.py:255` puts hop records in the
  trace. Direct path sets latency at `gateway.py:66`. **Fully wired — B5 is
  fixed.**
- Residue: two tests assert `>= 0` not `> 0`, so a regression to literal `0.0`
  would slip through: `tests/test_litellm_multistep.py:32` and
  `tests/test_agent_hop_compression.py:100`. Stale comment
  `app/gateway/backends/base.py:15-22` still says "the internal endpoint fakes
  the completion (it never calls generate)" — no longer true.

## The three fixes

### B1 — Honest error taxonomy

Introduce typed exceptions so the route maps each failure to what actually
broke, instead of matching fragile `RuntimeError`/`ValueError` message strings.

**1. New leaf module `app/errors.py`** with three exceptions (docstrings state
which HTTP status the route maps them to):

```python
class PolicyGateError(Exception):
    """The identity/privacy/policy gate (or its detector) failed to evaluate.
    Fail-closed: the request is rejected. Route maps -> 503."""

class NoEligibleCandidate(Exception):
    """No model survived the residency/budget/scope filter — e.g. an
    onprem_only request with no healthy on-prem model (CLAUDE.md rule 3).
    Route maps -> 422 with an honest residency message."""

class BackendUnavailable(Exception):
    """Every reachable tier's backend failed (timeout/connection/provider
    unreachable). Route maps -> 502."""
```

`PipelineRejected` stays in `app/pipeline.py` (→ 422) unchanged;
`AgentCapExceeded` stays in `app/agent/runtime.py` (→ 500).

**2. Router** (`app/routing/router.py:94,128`): replace the two bare
`ValueError(...)` with `NoEligibleCandidate(...)`, keeping the same messages
(`"no candidates after residency filter"`, `"no candidates after budget
filter"`). Import from `app.errors`.

**3. Gateway** (`app/gateway/gateway.py`):
- Line 42: raise `BackendUnavailable("backend failed on all reachable tiers for
  this request")` instead of `RuntimeError`.
- Line 49/58: capture the caught exception (`except Exception as exc:`) and, at
  the final-tier re-raise (line 58, `nxt is None`), raise
  `BackendUnavailable("backend failed on all reachable tiers for this request")
  from exc` instead of a bare `raise`. (The provider-not-enabled / onprem-no-
  endpoint RuntimeErrors from `litellm_backend._map` are caught here and become
  `BackendUnavailable` too — acceptable; they are genuine "no backend could
  serve" cases.)
- `NoEligibleCandidate` from `router.select` (line 36) is **not** inside the
  `except Exception` (which wraps only `backend.generate`, line 47), so it
  propagates cleanly out of `Gateway.run`.

**4. Pipeline gate stage** (`app/pipeline.py:88-95`): wrap detector construction
+ `self._gate.evaluate(...)` so any unexpected failure becomes `PolicyGateError`
(fail-closed 503), while a deliberate `PipelineRejected` (from the requested-
model check further down) is untouched:

```python
from app.errors import PolicyGateError  # top of file
...
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

**5. Route setup** (`app/api/routes.py:93-102`): the profile-load / registry-
load block must keep its existing fail-closed-503 intent (comment at
`routes.py:88-92`). Wrap it to raise `PolicyGateError` on failure so it maps to
503 via the handler below, not the generic 500:

```python
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
```

**6. Route exception taxonomy** (`app/api/routes.py:105-114`): replace the
two-branch catch with the full map. Order matters (specific before generic):

```python
except PipelineRejected as e:
    message = str(e)
    prefix = ("model request rejected: " if message.startswith("requested model")
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

Import `NoEligibleCandidate, BackendUnavailable, PolicyGateError` from
`app.errors` and `AgentCapExceeded` from `app.agent.runtime` at the route.
(`AgentCapExceeded` import must be lazy/local or top-level guarded — `app.agent`
depends on the optional `nat` extra. Prefer importing the class object at
top-level from `app.agent.runtime`, which defines `AgentCapExceeded` **above**
its own lazy `nat` import, so importing the class does not require `nat`.
Verify this holds; if not, import inside the handler.)

**Status choices (grounded in the "full honest taxonomy" decision):**
- Gate/detector/profile failure → **503** (unchanged message; fail-closed).
- No eligible candidate (residency/budget/scope) → **422** (client's policy
  leaves nothing serveable; the operator must onboard/enable a model).
- Agent non-convergence → **500** (we tried, couldn't complete within the step
  budget; not the client's fault, not a gate fault).
- Backend failed on all tiers / timeout / provider unreachable → **502**.
- Truly unexpected → **500 "internal error"** (still rejects; fail-closed
  preserved, but no longer lies about the gate).

**Post-implementation note (scope of 502):** the **502** mapping is
**direct-path-only**. In the multi-step agent path, a *per-hop* backend failure
happens inside the internal endpoint (`app/agent/internal_endpoint.py`), which
does not go through `Gateway.run`'s tier-exhaustion logic and crosses an HTTP
boundary back to the agent client — so it surfaces at the route as the generic
**500 "internal error"**, not 502. Still fail-closed (the request is rejected).
Giving per-hop backend failures their own 502 is a possible follow-up, out of
scope for B1.

**Tests (`tests/`):** add a route-level test module (e.g. extend an existing
`tests/test_error_taxonomy.py` or add one) asserting, with the regex detector
config:
- A genuine gate failure (monkeypatch `Gate.evaluate` / the detector to raise) →
  **503** "policy evaluation unavailable" (guards fail-closed, currently
  untested).
- `AgentCapExceeded` surfaced from the multi-step path → **500** "agent did not
  converge…" (monkeypatch `app.agent.runtime.run` to raise `AgentCapExceeded`).
- `BackendUnavailable` (monkeypatch the backend to always raise, so the gateway
  exhausts tiers) → **502**.
- `NoEligibleCandidate` (a request whose policy filters the pool empty, e.g.
  onprem_only with no healthy on-prem) → **422**.
Use the existing app fixtures (`create_app()` + a valid app token) as other
route tests do; monkeypatch at the seam that most directly raises each error.

### B4 — Layer regex US_SSN over Presidio (symmetric)

**1. Extract the merge into a leaf module.** Create `app/policy/merge.py`
containing the current `_dedupe` and `_score` from `effective.py` verbatim
(same redaction-safe transitive-cluster semantics). Re-point
`app/policy/effective.py` to `from app.policy.merge import dedupe` (rename to a
public `dedupe`, or keep `_dedupe` and import it) — behaviour identical, this is
a pure move so `EffectiveDetector` is unchanged in effect.

**2. Compose US_SSN into `PresidioDetector.analyze`** (`app/policy/detectors.py`):

```python
from app.policy.merge import dedupe   # top of file

class PresidioDetector:
    # US_SSN only: Presidio's own recognizers for EMAIL/PHONE/CREDIT_CARD are
    # better than regex; the sole coverage gap is the deliberate sample-SSN
    # blocklist in UsSsnRecognizer (see tests/test_detectors.py). Supplement
    # ONLY that pattern, and dedupe so a valid SSN Presidio already found is not
    # double-masked.
    _SSN = _PATTERNS["US_SSN"]

    def analyze(self, text: str) -> list[Entity]:
        results = self._analyzer.analyze(text=text, language="en")
        ents = [Entity(type=r.entity_type, start=r.start, end=r.end,
                       value=text[r.start:r.end]) for r in results]
        ents += [Entity(type="US_SSN", start=m.start(), end=m.end(),
                        value=m.group()) for m in self._SSN.finditer(text)]
        return dedupe(ents)
```

`anonymize` is unchanged (already delegates to `RegexDetector().anonymize`). The
merge guarantees `123-45-6789` is flagged `US_SSN` on ingress (gate) and masked
on egress (`verify`) under the demo-default config. Errs toward redaction — the
safe direction for a privacy gateway, defensible in production too (textbook
SSNs are better masked than leaked).

**3. Rework the pinned test** (`tests/test_detectors.py:68-80`,
`test_presidio_blocklists_sample_ssns`). The composite detector now catches
`123-45-6789`, so the current assertion (`not any US_SSN`) is intentionally
falsified. Replace with two clearer assertions:
- **Keep the upstream pin at the raw layer:** raw Presidio (via
  `presidio_analyzer.AnalyzerEngine().analyze` directly, or a dedicated helper)
  still blocklists the sample SSN — the fact we design around. If a future
  Presidio release stops blocklisting, this fails loudly.
- **Assert the composite behaviour:** `make_detector("presidio").analyze(...)`
  **does** flag `123-45-6789` as `US_SSN` (the B4 fix). Rename to reflect intent,
  e.g. `test_presidio_composite_catches_blocklisted_ssn`.

**4. New egress coverage test** (the gap grounding named): under
`pii_detection: presidio`, a completion containing `123-45-6789` is masked by
`verify()` (no raw SSN in the returned text). Model it on
`tests/test_output_controls.py::test_pii_in_completion_is_redacted` but with the
Presidio detector and the blocklisted SSN. Guarded by
`pytest.importorskip("presidio_analyzer")` like the other Presidio tests, so
lean CI skips rather than fails.

### B5 — Close out (already fixed)

1. `tests/test_litellm_multistep.py:32` — change `assert rec.latency_ms >= 0` to
   `assert rec.latency_ms > 0` (the fake seeds ≥200 ms, so this is safe and now
   catches a regression to `0.0`). Update the inline comment accordingly.
2. `tests/test_agent_hop_compression.py:100` — same change,
   `assert recs[0].latency_ms > 0`.
3. `app/gateway/backends/base.py:15-22` — fix the stale comment: the internal
   endpoint **does** call `backend.generate` now (real per-hop latency);
   `TIER_LATENCY_MS`/`seeded_latency_ms` are used **only by the test fake**
   (`tests/_litellm_fake.py`), not by production. Rewrite the docstring to say
   so (keep the constant; it is still the single source the fake reads).
4. `docs/bugs-backlog.md` — mark **B5 resolved**: latency flows end-to-end
   (hop records → history/DB sum → UI); note the harden landed in Cluster C.

## What is explicitly NOT in scope

- B2 (vLLM capacity / replica scaling) and B3 (shared history+bus store) — those
  are Cluster D. B1 gives B2's timeout failures an honest 502 label, but does not
  add capacity.
- No new provider, detector engine, or routing/scoring change.
- No change to the egress **telemetry** path (`app/obs/redact.py` already uses
  RegexDetector and catches the SSN). B4 fixes the completion/gate detector only.
- No streaming, no policy-rule changes.

## Testing summary

- **Backend/unit:** B1 route taxonomy (503 gate / 500 agent-cap / 502 backend /
  422 no-candidate) + a fail-closed-503 guard; B4 composite-Presidio SSN
  (ingress detect + egress mask, blocklist pin retained at the raw layer); B5
  two hardened latency asserts. Reuse existing `create_app()` / registry /
  detector fixtures.
- **Full suite** must stay green (mod the known WSL2 aiosqlite teardown flake:
  `test_langfuse_provision`, `test_traffic_*` — pass in isolation and under
  `-W ignore`; not caused by this cluster).
- **No frontend change** — no `tsc`/`vite` run needed. (B1's status codes are
  consumed by the UI's error display, but the drilldown already renders whatever
  message the API returns; no schema change.)

## Build order (dependency-aware)

1. **B4 merge extraction** — `app/policy/merge.py` + re-point `effective.py`
   (pure move; keeps the suite green as a foundation).
2. **B4 detector compose** — `PresidioDetector.analyze` + rework the pinned test
   + new egress test.
3. **B1 errors module + producers** — `app/errors.py`, then router + gateway
   raise the typed exceptions.
4. **B1 route taxonomy** — pipeline gate wrap + route setup wrap + the exception
   map + route tests.
5. **B5 closeout** — two test asserts, `base.py` comment, backlog entry.

B4 (1–2) and B1 (3–4) are independent; B5 (5) is independent of both. Order
above is the recommended sequence, not a hard dependency between B4 and B1.
