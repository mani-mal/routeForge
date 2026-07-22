# Phase 7 — Traffic Generator + Branch Tests Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development. Steps use checkbox (`- [ ]`) syntax.

**Goal:** A seeded, weighted-persona traffic generator that fires ≥500 requests at the live API (filling the UI dashboard) and prints the demo's headline numbers, plus the highest-value honesty tests the existing suite doesn't yet cover — proving at the **gateway call level** (not just the decision record) that PII/onprem_only traffic never reaches a cloud provider.

**Architecture:** `tests/traffic/personas.py` defines the weighted persona mix and generates synthetic (never real) request bodies deterministically from a seed. `tests/traffic/generator.py` drives an async `httpx` client at a configurable RPS against the running API and aggregates a summary. `tests/traffic/__main__.py` is the CLI. A call-recording spy backend proves the load-bearing privacy assertion. An optional `/api/traffic` endpoint + UI button lets the demo fire the burst live.

**Tech Stack:** Python 3.12, asyncio, httpx, pytest. **No Faker** — synthetic PII is hand-rolled from `random.Random(seed)` (deterministic, offline, honors "synthetic only, never real customer data").

## Global Constraints

- **Synthetic PII only, never real data** — generate fake SSNs/emails/account numbers from a seeded RNG; the README must say so. The patterns must be ones the `regex` detector recognizes (so `pii_bearing` personas actually trip the gate): SSN `NNN-NN-NNNN`, email `x@y.z`, etc. (grep `app/policy/detectors.py` RegexDetector patterns and match them).
- **The generator hits the same API the UI watches** so the dashboard fills live (CLAUDE.md demo step 5). It talks only to the API over HTTP — no importing the pipeline directly.
- **Seeded + reproducible** — same `--seed` ⇒ same request stream ⇒ same numbers (a demo that changes each run is hard to narrate).
- **Offline / no GPU** — mock backend; tests run on a laptop. Do not add network-dependent deps.
- **The privacy assertion is load-bearing**: a `pii_bearing`/`onprem_only` request must never invoke a cloud provider — assert on the actual **gateway→backend call**, not only the DecisionRecord (per the traffic-and-tests skill's assertion #1).
- Tiers `cheap|mid|frontier`; providers `onprem|azure|bedrock`; personas map to the existing gate/router behavior — don't change routing to fit the generator.
- Weighted mix (traffic-and-tests skill): simple_qa 45, moderate 20, pii_bearing 15, multi_step 12, high_risk 5, low_confidence 3 (+ large_tool_output overlay on multi_step). Weights live in `tests/personas.yaml` (single source).

---

### Task 1: Personas (weighted mix + seeded synthetic PII)

**Files:**
- Create: `tests/personas.yaml` (weights + persona metadata)
- Create: `tests/traffic/personas.py`
- Create: `tests/traffic/README.md` (replace the stub; state synthetic-only)
- Test: `tests/test_traffic_personas.py`

**Interfaces:**
- Produces:
  - `class Persona(dataclass)`: `name: str`, `weight: int`, `api_key: str`, `expect_onprem: bool` (documentation of intent), plus a `build(rng: random.Random) -> dict` that returns a `/v1/chat/completions` request body.
  - `def load_personas(path="tests/personas.yaml") -> list[Persona]`
  - `def sample(personas, rng, n) -> list[Persona]` — weighted sampling (deterministic given rng).
  - Synthetic generators: `fake_ssn(rng) -> str` (`"NNN-NN-NNNN"`), `fake_email(rng)`, `fake_account(rng)` — match the RegexDetector patterns so they trip the gate.
- Persona bodies:
  - `simple_qa` → `{messages:[{role:user, content: "<trivial question>"}]}` (acme-key).
  - `moderate` → a somewhat longer question (acme-key).
  - `pii_bearing` → content embedding a `fake_ssn`/`fake_email` (hospital-key). expect_onprem=True.
  - `multi_step` → `{messages:[...], mock_multistep: true}` (acme-key). `large_tool_output` overlay adds a big newline-duplicated log block so Headroom has material (see Global Constraints — dup lines compress).
  - `high_risk` → content with a medical-license-like token (hospital-key). expect_onprem=True.
  - `low_confidence` → a normal body plus `mock_confidence: <low>` to trigger exactly one escalation (acme-key).

- [ ] **Step 1: Failing test** `tests/test_traffic_personas.py`:

```python
import random
from tests.traffic.personas import load_personas, sample, fake_ssn, fake_email

def test_weights_load():
    ps = {p.name: p for p in load_personas()}
    assert ps["simple_qa"].weight == 45
    assert ps["pii_bearing"].weight == 15
    assert ps["pii_bearing"].expect_onprem is True

def test_sampling_deterministic_and_weighted():
    ps = load_personas()
    a = [p.name for p in sample(ps, random.Random(42), 1000)]
    b = [p.name for p in sample(ps, random.Random(42), 1000)]
    assert a == b  # seeded → reproducible
    # simple_qa (weight 45) should dominate; pii_bearing present
    assert a.count("simple_qa") > a.count("pii_bearing") > 0

def test_synthetic_pii_matches_detector_patterns():
    from app.policy.detectors import RegexDetector
    d = RegexDetector()
    rng = random.Random(1)
    ssn = fake_ssn(rng)
    assert any(e.type == "US_SSN" for e in d.analyze(f"my ssn is {ssn}"))
    email = fake_email(rng)
    assert any(e.type == "EMAIL_ADDRESS" for e in d.analyze(f"reach me at {email}"))

def test_persona_build_shapes():
    rng = random.Random(7)
    for p in load_personas():
        body = p.build(rng)
        assert "messages" in body and body["messages"]
        if p.name == "multi_step":
            assert body.get("mock_multistep") is True
```

(Grep `app/policy/detectors.py` for the exact `US_SSN`/`EMAIL_ADDRESS` entity type strings and regex shapes; match them so these tests pass.)

- [ ] **Step 2: Run → FAIL.**
- [ ] **Step 3: Write `tests/personas.yaml`** (weights + api_key + expect_onprem per persona).
- [ ] **Step 4: Implement `tests/traffic/personas.py`** (dataclass, loaders, seeded generators, `build`).
- [ ] **Step 5: Run → PASS.**
- [ ] **Step 6: Write `tests/traffic/README.md`** — states synthetic-only, lists personas + weights, and the CLI usage. Commit.

---

### Task 2: Async traffic generator + CLI + summary

**Files:**
- Create: `tests/traffic/generator.py`
- Rewrite: `tests/traffic/__main__.py` (CLI)
- Test: `tests/test_traffic_generator.py`

**Interfaces:**
- Produces:
  - `async def run_traffic(base_url, *, n, rps, seed, personas=None, headroom=None, client=None) -> Summary`
  - `class Summary(dataclass/BaseModel)`: `count, tier_distribution: dict, total_usd, baseline_usd, pct_saved, routing_saved_usd, compression_saved_usd, onprem_count, p50_ms, p95_ms, errors`.
  - The generator: samples `n` personas (seeded), throttles to ~`rps` (simple `asyncio.sleep(1/rps)` pacing or a token bucket), POSTs each body to `{base_url}/v1/chat/completions` with the persona's `Authorization` key, records per-response `cost`/`policy`/latency, aggregates the Summary. If `headroom` is not None, POST `/api/controls {"headroom_enabled": headroom}` first.
  - `--compare-compression`: run the same seed twice (headroom off, then on) and print both summaries side by side (the A/B).
- CLI (`__main__.py`): `argparse` — `--requests` (default 500), `--rps` (default 10), `--seed` (default 42), `--base-url` (default http://127.0.0.1:8080), `--profile` (accepted, demo default), `--compare-compression`. On completion print the summary: tier distribution, total vs baseline, % saved, on-prem count, p50/p95. These are the numbers read aloud.

- [ ] **Step 1: Failing test** `tests/test_traffic_generator.py` — drive the generator against the `live_internal_server` fixture (real socket, full app) with a small N:

```python
import asyncio
from tests.traffic.generator import run_traffic

def test_generator_fills_summary(live_internal_server):
    base = f"http://127.0.0.1:{live_internal_server.port}"
    summary = asyncio.run(run_traffic(base, n=40, rps=50, seed=42))
    assert summary.count == 40
    assert sum(summary.tier_distribution.values()) == 40
    assert summary.onprem_count > 0          # pii/high_risk personas kept on-prem
    assert summary.baseline_usd >= summary.total_usd  # savings non-negative in aggregate
    assert "cheap" in summary.tier_distribution  # simple_qa dominates
```

(Confirm the `live_internal_server` fixture serves the full app incl. `/api/controls` and `/v1/chat/completions`, and that the suite already sets the regex detector so PII personas trip the gate offline. Grep conftest.)

- [ ] **Step 2: Run → FAIL.**
- [ ] **Step 3: Implement `generator.py`** (httpx.AsyncClient, seeded sampling reuse from Task 1, RPS pacing, aggregation). Handle per-request errors into `summary.errors` (a 422/503 must not crash the run).
- [ ] **Step 4: Implement `__main__.py`** CLI + pretty summary print.
- [ ] **Step 5: Run → PASS. Full suite green. Commit.**

---

### Task 3: Gateway-call-log honesty test (assertion #1) + coverage map

**Files:**
- Create: `tests/test_traffic_honesty.py`
- Modify: `tests/traffic/README.md` (map the skill's "assertions that matter most" to the tests that cover each — a checklist, so a reader can see the demo's promises are all tested).

**Interfaces:**
- A `RecordingBackend` (in the test file) implementing the `InferenceBackend` protocol, wrapping `MockBackend`, recording each `(model)` it's asked to generate for. Build a `Gateway(settings, RecordingBackend(...), Router(settings))` and a `Pipeline` (mirror `compression_on_pipeline`/e2e fixtures), run each `pii_bearing`/`high_risk`/`multi_step`-with-PII persona through `pipeline.run`, and assert **no recorded model maps to a cloud provider** (`azure`/`bedrock`) for any `onprem_only` request — proving privacy at the call level, not just the decision record.

- [ ] **Step 1: Failing test** `tests/test_traffic_honesty.py`:

```python
import asyncio
from app.config.loader import load_settings
from app.gateway.gateway import Gateway
from app.gateway.backends.mock import MockBackend
from app.routing.router import Router
from app.policy.detectors import make_detector
from app.policy.gate import Gate
from app.pipeline import Pipeline
from app.compression.ccr import CCRStore
from app.models import ChatRequest, Message

class RecordingBackend:
    name = "recording"
    def __init__(self, inner): self.inner = inner; self.models_called = []
    async def generate(self, model, messages, price, tier, hint):
        self.models_called.append(model)
        return await self.inner.generate(model, messages, price, tier, hint)

def _pipeline(rec):
    s = load_settings()
    det = make_detector("regex")
    co = s.serving.context_optimization
    return Pipeline(s, Gate(s, det), det, Gateway(s, rec, Router(s)),
                    self_port=8080, co_cfg_provider=lambda: co, ccr=CCRStore(3600))

def _provider_of(model):
    return {c.name: c.provider for c in load_settings().routing.model_pool}.get(model, "?")

def test_pii_request_never_calls_a_cloud_provider_at_the_backend():
    rec = RecordingBackend(MockBackend(load_settings()))
    pipe = _pipeline(rec)
    req = ChatRequest(messages=[Message(role="user", content="My SSN is 123-45-6789, summarize.")])
    out = asyncio.run(pipe.run("r1", "hospital", req))
    assert out["policy"]["onprem_only"] is True
    assert rec.models_called  # a model WAS called
    assert all(_provider_of(m) == "onprem" for m in rec.models_called), rec.models_called
```

(Match `RecordingBackend.generate`'s signature to the real `InferenceBackend.generate` — grep `app/gateway/backends/base.py` and `mock.py` for the exact parameters.)

- [ ] **Step 2: Run → FAIL (or PASS immediately if privacy already holds — if it passes on first write, mutate the gate/router mentally to confirm the test is real: a test that can't fail is worthless. Add a sibling assertion that a CLEAN request on the `default` tenant CAN reach a non-onprem provider ONLY when cloud is enabled — or document that with cloud disabled by default all traffic is onprem, and enable bedrock in a second case to show the pii case still stays onprem while a clean case could escalate. Keep the core assertion: onprem_only ⇒ only onprem models called.)**
- [ ] **Step 3: If any gap, fix; else the test stands as a regression guard.** Add a `multi_step` + PII variant asserting every HOP stayed on-prem (drive via the pipeline multi-step path).
- [ ] **Step 4: Update `tests/traffic/README.md`** with the assertions-that-matter checklist mapping each to its test file (skill lists 11; most already exist — cite them). Commit.

---

### Task 4: `/api/traffic` control + UI "Run burst" button (demo live-fire)

**Files:**
- Modify: `app/api/ui_routes.py` (`POST /api/traffic`, `GET /api/traffic`)
- Modify: `app/main.py` (a small traffic-runner state on app.state)
- Modify: `ui/src/api.ts` + `ui/src/components/ControlPanel.tsx` (a "Run burst" button + running indicator)
- Test: `tests/test_traffic_endpoint.py`

**Interfaces:**
- `POST /api/traffic {requests?: int=100, rps?: int=20}` → starts an in-process asyncio task that runs `run_traffic` against `http://127.0.0.1:{self_port}` (self-call, like the agent) so the dashboard fills live; returns `{"status": "started", "requests": n}`. Guard: refuse (409) if a run is already in progress (`app.state.traffic_running`). `GET /api/traffic` → `{"running": bool, "last_summary": {...}|null}`.
- The self-call needs the server's own port — reuse the `_self_port()` main.py already computes for the agent, stashed on `app.state`.
- UI: a "Run 100 burst" button in ControlPanel that POSTs `/api/traffic`; disable while `running`; poll `GET /api/traffic` (or reuse the stats poll) to flip the label. Keep it simple.

- [ ] **Step 1: Failing test** `tests/test_traffic_endpoint.py` using `live_internal_server` (real socket — the endpoint self-calls over HTTP): POST `/api/traffic {"requests": 10, "rps": 50}` → 200 `{"status":"started"}`; poll `GET /api/traffic` until `running` is False (bounded ~15s); assert `last_summary.count == 10` and `GET /api/stats` `count >= 10`. A second immediate POST while running → 409 (best-effort; if timing makes this flaky, assert only the happy path + the 409 via a direct state check).

- [ ] **Step 2: Run → FAIL.**
- [ ] **Step 3: Implement** the endpoint (asyncio.create_task; set/clear `app.state.traffic_running`; store `last_summary`) + main.py state + self-port stash.
- [ ] **Step 4: UI** — `api.startTraffic()`/`api.trafficStatus()` in api.ts; a button in ControlPanel. `npm run build` clean.
- [ ] **Step 5: Run tests → PASS. Full suite green. Commit.**

---

## Self-Review notes (controller)
- The generator hits the API over HTTP (not the pipeline) so it exercises the real serving path incl. auth, history, stream — the dashboard fills exactly as in the demo.
- Assertion #1 (gateway call-level privacy) is the one genuinely new correctness guard; the rest of the skill's 11 are mapped to existing tests in the README (test_policy_gate, test_router, test_output_controls, test_obs_export, test_agent_*, test_two_lever_cost, test_compress_orchestrator, test_backends).
- No Faker (synthetic PII hand-rolled, seeded, offline). No real data. README says so.
- Deferred (YAGNI): true token-bucket RPS (simple pacing suffices for a demo); a stop button (the burst is short; a run completes quickly — GET status shows progress).
