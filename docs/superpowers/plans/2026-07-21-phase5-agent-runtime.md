# Phase 5 — Agent Runtime (NeMo Agent Toolkit, multi-step) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Multi-step requests run a real NeMo Agent Toolkit (`nvidia-nat`) agent whose every LLM call re-enters our router — proving per-call routing and cost accounting across agent hops.

**Architecture (verified by the Phase-5 spike — see `docs/superpowers/specs/2026-07-21-phase5-nat-integration-spike.md`):** A `tool_calling_agent` NAT workflow uses the `openai` LLM provider with `base_url` pointed at an **internal** OpenAI-compatible endpoint we host. Each agent hop is a plain HTTP POST to that endpoint; the endpoint runs `router.select` + gateway for that call (one `DecisionRecord` per hop), and returns an OpenAI **SSE** stream (LangChain `ChatOpenAI` sends `stream:true`). Per-run correlation is carried in the endpoint's URL path (`/internal/v1/{run_id}/chat/completions`) since NAT sends no correlation header. `app/agent/runtime.py` invokes the workflow programmatically via `nat.runtime.loader.load_workflow`.

**Tech Stack:** `nvidia-nat[langchain]` 1.8 (installed, `[agent]` extra), FastAPI SSE, our existing router/gateway/mock. All local, no GPU.

## Global Constraints

- **Per-call routing is load-bearing.** Every LLM call the agent makes re-enters `router.select` (step 6). The router prices **post-compression** tokens per hop; multi-step cost is the sum over hops, never a single-shot estimate. (CLAUDE.md pitfalls.)
- **The internal endpoint is separate from the public API.** The public `/v1/chat/completions` still **rejects inbound `stream:true`** (output-controls) and verifies the *complete* response. The internal `/internal/v1/{run_id}/chat/completions` endpoint DOES stream SSE — it carries per-hop model calls, not the customer-facing verified answer. Never conflate them. The internal endpoint must not be reachable as a public route (localhost/loopback only; no auth-key gating needed but it must not appear in the public OpenAPI surface used by customers).
- **`onprem_only` still absolute per hop.** Each hop's routing obeys the request's `ExecutionPolicyContext` (residency, allowed_providers). A multi-step `onprem_only` request keeps every hop on-prem.
- **Fail closed.** If the agent runtime errors or exceeds caps (`max_iterations` from `policy.agent_budgets`), the request fails cleanly (5xx/handled), never silently returns a partial/unrouted answer.
- **Verification runs on the final answer.** Step 9 verifies the assembled final agent response (complete, non-streamed), redacting egress as before.
- **Determinism.** The mock backend drives the agent deterministically (given the conversation, it emits either a scripted tool_call or a final answer) so the demo/tests are reproducible.
- **Telemetry unchanged.** Phase 4's OTel→Langfuse export still runs; multi-step traces now carry multiple hop spans. `opentelemetry` stays 1.44.0 (both extras coexist — verified).

---

### Task 1: Agent-aware mock + OpenAI SSE serialization

**Files:**
- Create: `app/agent/openai_sse.py` (SSE chunk serialization)
- Create: `app/agent/mock_agent.py` (deterministic tool_call/final decision from an OpenAI-format message list)
- Test: `tests/test_agent_mock_sse.py`

**Interfaces:**
- Produces:
  - `sse_tool_call(*, model, call_id, tool_name, arguments_json) -> Iterator[str]` — yields OpenAI `chat.completion.chunk` SSE lines ending with `data: [DONE]`, delta carrying `tool_calls`, finish_reason `tool_calls`.
  - `sse_final(*, model, content) -> Iterator[str]` — yields SSE chunks with `delta.content` and finish_reason `stop`, then `data: [DONE]`.
  - `decide_agent_step(messages: list[dict], tools: list[dict]) -> dict` — deterministic: if any message has `role == "tool"`, return `{"kind": "final", "content": ...}` (a final answer that references the last tool result); else return `{"kind": "tool_call", "tool_name": <first tool's name>, "arguments": {...}}` deriving arguments from the user question (e.g. extract an arithmetic expression for the calculator, else a fixed query string).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_agent_mock_sse.py
import json
from app.agent.openai_sse import sse_tool_call, sse_final
from app.agent.mock_agent import decide_agent_step

def _parse(chunks):
    events = []
    for line in "".join(chunks).splitlines():
        line = line.strip()
        if not line.startswith("data:"):
            continue
        body = line[len("data:"):].strip()
        events.append(body if body == "[DONE]" else json.loads(body))
    return events

def test_sse_tool_call_shape():
    ev = _parse(sse_tool_call(model="local-8b", call_id="call_1",
                              tool_name="calculator", arguments_json='{"expression": "12 * 7"}'))
    assert ev[-1] == "[DONE]"
    first = ev[0]
    assert first["object"] == "chat.completion.chunk"
    tc = first["choices"][0]["delta"]["tool_calls"][0]
    assert tc["function"]["name"] == "calculator"
    assert json.loads(tc["function"]["arguments"]) == {"expression": "12 * 7"}
    assert any(e != "[DONE]" and e["choices"][0].get("finish_reason") == "tool_calls" for e in ev)

def test_sse_final_shape():
    ev = _parse(sse_final(model="local-8b", content="The answer is 84."))
    assert ev[-1] == "[DONE]"
    assert "".join(e["choices"][0]["delta"].get("content", "")
                   for e in ev if e != "[DONE]") == "The answer is 84."
    assert any(e != "[DONE]" and e["choices"][0].get("finish_reason") == "stop" for e in ev)

def test_decide_tool_call_then_final():
    tools = [{"type": "function", "function": {"name": "calculator",
              "parameters": {"properties": {"expression": {"type": "string"}}}}}]
    step1 = decide_agent_step([{"role": "user", "content": "What is 12 times 7?"}], tools)
    assert step1["kind"] == "tool_call" and step1["tool_name"] == "calculator"
    step2 = decide_agent_step(
        [{"role": "user", "content": "What is 12 times 7?"},
         {"role": "assistant", "tool_calls": [{"id": "c1"}]},
         {"role": "tool", "content": "Result: 84.0", "tool_call_id": "c1"}], tools)
    assert step2["kind"] == "final" and "84" in step2["content"]
```

- [ ] **Step 2: Run to verify it fails** — `uv run --extra agent pytest tests/test_agent_mock_sse.py -v` → FAIL (modules missing).

- [ ] **Step 3: Implement** `app/agent/openai_sse.py` and `app/agent/mock_agent.py`. SSE lines are `f"data: {json.dumps(chunk)}\n\n"`; terminal `"data: [DONE]\n\n"`. `decide_agent_step` uses a regex to pull an arithmetic expression (`\d+\s*[-+*/]\s*\d+`) for the calculator arguments; falls back to `{"query": <trimmed user text>}` for a non-calculator first tool. The final content echoes the last `role=="tool"` message content ("The tool reported: <...>").

- [ ] **Step 4: Run to green** — `uv run --extra agent pytest tests/test_agent_mock_sse.py -v`.

- [ ] **Step 5: Commit** — `feat(agent): deterministic agent-step decision + OpenAI SSE serialization`.

---

### Task 2: Internal router-backed OpenAI endpoint (per-hop DecisionRecords)

**Files:**
- Create: `app/agent/internal_endpoint.py` (an `APIRouter` with the internal route + a run-keyed hop collector)
- Modify: `app/main.py` (mount the internal router)
- Test: `tests/test_agent_internal_endpoint.py`

**Interfaces:**
- Produces:
  - `HopCollector` — process-global, maps `run_id -> list[DecisionRecord]`; `start(run_id)`, `record(run_id, rec)`, `pop(run_id) -> list[DecisionRecord]`.
  - Route `POST /internal/v1/{run_id}/chat/completions` — accepts an OpenAI request body (`messages`, `tools`, `stream`), builds the routing decision via `router.select` for the request's active `ExecutionPolicyContext` (looked up from the collector's run context — see below), appends a `DecisionRecord` (step `"agent_hop"`, incrementing `hop_index`) to `HopCollector[run_id]`, then streams `decide_agent_step(...)` via `openai_sse.*` as a `StreamingResponse(media_type="text/event-stream")`.
- Consumes: `router.select`, `decide_agent_step`, `sse_tool_call`/`sse_final`, the per-run `ExecutionPolicyContext` (the agent runtime stashes it in the collector at `start(run_id, ctx)`).

**Design notes for the implementer:**
- The endpoint must NOT be part of the customer-facing OpenAPI/tags; mark `include_in_schema=False`.
- The routing for a hop: use the stashed `ctx` (residency/allowed_providers/tiers) so `onprem_only` holds per hop; compute `tokens_after` from the incoming messages (reuse `app.gateway.backends.base.count_tokens`); `complexity = min(1.0, tokens_after/2000)`. `router.select(ctx, tokens_after, complexity, None)` → the chosen candidate → a `DecisionRecord(step="agent_hop", chosen=<model>, hop_index=<n>, est_cost_usd=..., ...)`.
- `hop_index` increments per call within a run (track on the collector).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_agent_internal_endpoint.py
import json
import pytest
from fastapi.testclient import TestClient

@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("SP_PII_DETECTION", "regex")
    from app.main import create_app
    return TestClient(create_app())

def _ctx():
    from app.models import ExecutionPolicyContext
    return ExecutionPolicyContext(
        request_id="req-x", tenant_id="acme", sensitivity="internal",
        onprem_only=False, allowed_providers=frozenset({"onprem", "azure", "bedrock"}),
        allowed_models=frozenset(), max_cost_usd=1.0, max_latency_ms=10000,
        risk_class="low", require_verification=False, detected_entities=(),
        redaction_map_id=None)

def test_hop_routes_records_and_streams(client):
    from app.agent.internal_endpoint import HOPS
    run_id = "run-abc"
    HOPS.start(run_id, _ctx())
    body = {"model": "auto", "stream": True,
            "messages": [{"role": "user", "content": "What is 12 times 7?"}],
            "tools": [{"type": "function", "function": {"name": "calculator",
                       "parameters": {"properties": {"expression": {"type": "string"}}}}}]}
    r = client.post(f"/internal/v1/{run_id}/chat/completions", json=body)
    assert r.status_code == 200
    assert "text/event-stream" in r.headers["content-type"]
    assert "data: [DONE]" in r.text
    # a tool_call was streamed (no tool result yet) and a hop was recorded
    assert "tool_calls" in r.text
    recs = HOPS.pop(run_id)
    assert len(recs) == 1 and recs[0].step == "agent_hop" and recs[0].chosen is not None

def test_onprem_only_hop_stays_onprem(client):
    from app.agent.internal_endpoint import HOPS
    from app.models import ExecutionPolicyContext
    run_id = "run-hosp"
    HOPS.start(run_id, ExecutionPolicyContext(
        request_id="r", tenant_id="hospital", sensitivity="restricted", onprem_only=True,
        allowed_providers=frozenset({"onprem"}), allowed_models=frozenset(),
        max_cost_usd=1.0, max_latency_ms=10000, risk_class="high",
        require_verification=True, detected_entities=("US_SSN",), redaction_map_id=None))
    body = {"model": "auto", "stream": True,
            "messages": [{"role": "user", "content": "diagnose please"}], "tools": []}
    client.post(f"/internal/v1/{run_id}/chat/completions", json=body)
    rec = HOPS.pop(run_id)[0]
    # chosen model must be an on-prem-tier model (provider onprem)
    from app.config.loader import load_settings
    pool = {c.name: c.provider for c in load_settings().routing.model_pool}
    assert pool[rec.chosen] == "onprem"
```

- [ ] **Step 2: Run to verify it fails.**
- [ ] **Step 3: Implement** the collector + route; mount `app.include_router(internal_router)` in `main.py`. When `tools` is empty, `decide_agent_step` returns a final answer directly (still records a hop).
- [ ] **Step 4: Run to green** — `uv run --extra agent pytest tests/test_agent_internal_endpoint.py -v`.
- [ ] **Step 5: Commit** — `feat(agent): internal router-backed OpenAI SSE endpoint with per-hop DecisionRecords`.

---

### Task 3: NAT tool package + workflow config

**Files:**
- Create: `nat_tools/pyproject.toml`, `nat_tools/src/nat_tools/{__init__,register,tools}.py` (installable package; entry point `nat.components`)
- Create: `config/nat_workflow.yaml`
- Modify: root `pyproject.toml` (add `nat_tools` as an editable workspace dep of the `[agent]` extra)
- Test: `tests/test_nat_workflow_config.py`

**Interfaces:**
- Produces three Python-function tools registered via `@register_function(..., framework_wrappers=[LLMFrameworkEnum.LANGCHAIN])`: `calculator(expression: str) -> str`, `doc_search(query: str) -> str` (fake, returns a canned snippet), `crm_lookup(account: str) -> str` (fake). Follow the spike's verified pattern in `scratch-nat-spike/nat_spike_calc/`.
- `config/nat_workflow.yaml`: `workflow._type: tool_calling_agent`, `llm_name: sp_router`, `tool_names: [calculator, doc_search, crm_lookup]`, `max_iterations: ${AGENT_MAX_ITERATIONS}` (default from `policy.agent_budgets.max_steps`); `llms.sp_router`: `_type: openai`, `model_name: auto`, `base_url` + `api_key` placeholders the runtime overrides per-run.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_nat_workflow_config.py
import subprocess, sys, os

def test_workflow_validates():
    # nat validate must accept the workflow (agents + tools discoverable)
    env = dict(os.environ)
    r = subprocess.run(
        ["uv", "run", "--extra", "agent", "--extra", "observability",
         "nat", "validate", "--config_file", "config/nat_workflow.yaml"],
        capture_output=True, text=True, env=env, timeout=300)
    assert r.returncode == 0, f"nat validate failed:\n{r.stdout}\n{r.stderr}"
```

- [ ] **Step 2: Run to verify it fails** (config/tools missing).
- [ ] **Step 3: Implement** the tool package (mirror `scratch-nat-spike/nat_spike_calc` structure exactly — it validated), add it as an editable dep, write `config/nat_workflow.yaml`. Run `uv sync --extra agent` so the editable package is installed and its `nat.components` entry point is discoverable.
- [ ] **Step 4: Run to green** — the `nat validate` test passes.
- [ ] **Step 5: Commit** — `feat(agent): nat_tools package (calculator/doc_search/crm_lookup) + workflow config`.

---

### Task 4: Agent runtime (`app/agent/runtime.py`)

**Files:**
- Create: `app/agent/runtime.py`
- Modify: `app/agent/__init__.py` (export `run`/`AgentResult`, drop the `decide_mode` stub)
- Test: `tests/test_agent_runtime.py`

**Interfaces:**
- Produces: `AgentResult` (Pydantic: `text: str`, `hops: list[DecisionRecord]`) and
  `async def run(ctx: ExecutionPolicyContext, request: ChatRequest, *, port: int) -> AgentResult`.
- Behavior: mint `run_id`; `HOPS.start(run_id, ctx)`; `load_config(config/nat_workflow.yaml)`, override `config.llms["sp_router"].base_url = f"http://127.0.0.1:{port}/internal/v1/{run_id}/v1"` and `api_key = "sk-internal"`; run the workflow programmatically (`WorkflowBuilder.from_config` → `SessionManager.create` → `session.run(user_text)` → `runner.result(to_type=str)` — the spike's verified pattern); `hops = HOPS.pop(run_id)`; return `AgentResult(text=result, hops=hops)`. Enforce `max_iterations` via the config (cap from `policy.agent_budgets`).

**Verify-against-installed (spike-verified API, re-confirm the import paths on the pinned build):**
`from nat.runtime.loader import load_config` and `load_workflow`; `from nat.builder.workflow_builder import WorkflowBuilder`; `from nat.runtime.session import SessionManager`. The exact override point for `base_url`/`api_key` on the loaded `Config` object must be confirmed (mutate the pydantic model field on `config.llms[name]`); if the field is frozen, write a per-run temp YAML instead. The spike used `load_workflow(config_file)` directly — for per-run base_url override, `load_config` + mutate + `WorkflowBuilder.from_config` is the path. Confirm and document what worked.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_agent_runtime.py
import pytest
from app.models import ChatRequest, Message, ExecutionPolicyContext

pytest.importorskip("nat")

def _ctx():
    return ExecutionPolicyContext(
        request_id="req-1", tenant_id="acme", sensitivity="internal", onprem_only=False,
        allowed_providers=frozenset({"onprem", "azure", "bedrock"}), allowed_models=frozenset(),
        max_cost_usd=1.0, max_latency_ms=10000, risk_class="low",
        require_verification=False, detected_entities=(), redaction_map_id=None)

@pytest.mark.anyio
async def test_multi_step_run_yields_hops(live_internal_server):  # fixture boots the app on a port
    from app.agent.runtime import run
    req = ChatRequest(model="auto", messages=[Message(role="user", content="What is 12 times 7?")])
    result = await run(_ctx(), req, port=live_internal_server.port)
    assert result.hops, "expected >=1 routed hop"
    assert all(h.step == "agent_hop" and h.chosen for h in result.hops)
    assert result.text  # a final answer came back
```

The `live_internal_server` fixture (add to `tests/conftest.py`) boots `create_app()` on an ephemeral port with `uvicorn` in a thread (the internal endpoint must be reachable over real HTTP because NAT's client makes real HTTP calls). Mark the test module to use the real event loop.

- [ ] **Step 2–4:** Run red, implement `runtime.py`, run green. This test makes a real in-process HTTP round-trip through NAT → internal endpoint → mock; expect ≥2 hops for a tool-using question.
- [ ] **Step 5: Commit** — `feat(agent): NAT tool_calling_agent runtime — per-hop routing via internal endpoint`.

---

### Task 5: Task-mode + pipeline integration

**Files:**
- Modify: `app/routing/task_mode.py` (rules-first `direct` vs `multi_step`)
- Modify: `app/pipeline.py` (route `multi_step` through the agent runtime; sum hop costs; verify final answer)
- Modify: `app/main.py` (pass the server port to the pipeline for the internal endpoint)
- Test: `tests/test_agent_pipeline_e2e.py`

**Interfaces:**
- `task_mode.decide(ctx, request) -> str` returns `"multi_step"` when the request opts in — rule: the request carries `mock_multistep: true` (extra field, like `mock_confidence`) OR the last user message contains a multi-step trigger phrase (e.g. "step by step"/"look up"); else `"direct"`. Keep it rules-only (classifier off), per §10.3.
- `Pipeline.run`: when mode is `multi_step`, call `await agent.run(ctx, request, port=self._port)`; assemble the response with `trace = [hop records...] + [verify record] + [compression record]`, `choices[0]` = the agent's final `text`, `cost.total_usd` = sum of hop `est_cost_usd`/`actual_cost_usd`, `baseline_usd` = all-frontier baseline over the hops. Verification (step 9) runs on the final `text`. `PipelineRejected` on verify fail as today.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_agent_pipeline_e2e.py
import pytest
from fastapi.testclient import TestClient

pytest.importorskip("nat")

@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("SP_PII_DETECTION", "regex")
    from app.main import create_app
    return TestClient(create_app())

def test_multistep_request_returns_multi_hop_trace(client):
    r = client.post("/v1/chat/completions",
                    headers={"authorization": "Bearer acme-key"},
                    json={"model": "auto", "mock_multistep": True,
                          "messages": [{"role": "user", "content": "Look up account 42 then compute 12*7"}]})
    assert r.status_code == 200
    body = r.json()
    hops = [t for t in body["trace"] if t.get("step") == "agent_hop"]
    assert len(hops) >= 2, "multi-step should route more than one hop"
    assert body["cost"]["total_usd"] == pytest.approx(sum(h.get("actual_cost_usd") or h.get("est_cost_usd", 0) for h in hops))
    assert body["choices"][0]["message"]["content"]
```

Note: this e2e requires the internal endpoint reachable over HTTP within `TestClient`. Since `TestClient` runs the app in-process but NAT makes real HTTP calls to `127.0.0.1:<port>`, the test must run the app under a real uvicorn server on a port (reuse the `live_internal_server` fixture) rather than bare `TestClient`. Adjust the fixture accordingly and point the request at the live base URL.

- [ ] **Step 2–4:** Red, implement, green.
- [ ] **Step 5: Commit** — `feat(agent): route multi-step requests through the NAT agent; per-hop cost + final-answer verification`.

---

### Task 6: Live acceptance + smoke

**Files:**
- Create: `scripts/smoke-agent.sh`
- Modify: `scripts/README.md`

**Interface:** boots the API (`--extra agent --extra observability`), sends a `mock_multistep` request, asserts the response trace has ≥2 `agent_hop` records with distinct/valid chosen models and a non-empty final answer; if Langfuse is up, notes the multi-span trace. This is the controller's phase-level acceptance (like `smoke-obs.sh`).

- [ ] **Step 1:** Write `scripts/smoke-agent.sh` with genuine assertions (≥2 hops, final answer present, exit non-zero on failure).
- [ ] **Step 2:** `bash -n` it; run it live; confirm PASS.
- [ ] **Step 3: Commit** — `feat(agent): multi-step agent smoke (>=2 routed hops)`.

---

## Self-Review

**Spec coverage:** §10.3 NAT agent runtime (gateway as NAT's model endpoint, per-call re-entry) → Tasks 2+4; multi-step task mode (step 4) → Task 5; caps from `policy.agent_budgets` → Tasks 3/4; telemetry hops → inherited from Phase 4 (each hop is a routed call already traced). Demo narrative #3 (multi-step, mixed tiers) → Task 5 e2e.

**Placeholder scan:** none — code is concrete where the spike verified it; the two NAT-API spots (programmatic `Config` base_url override; exact import paths on the pinned build) carry explicit verify-against-installed notes with the spike's working fallback (`load_workflow` / per-run temp YAML). Legitimate TDD, not placeholders.

**Type consistency:** `AgentResult.hops: list[DecisionRecord]` flows into `pipeline.trace`; `HOPS` collector keyed by `run_id`; `decide_agent_step` output shape (`kind`/`tool_name`/`arguments`/`content`) consumed identically in Tasks 1 and 2; `run(ctx, request, *, port)` signature matches the pipeline call in Task 5.

**Risks flagged for the reviewer:** (1) the internal endpoint must be loopback-only and `include_in_schema=False` — never a customer-reachable route; (2) the public API's stream-reject invariant must remain intact (Task 5 must not weaken it); (3) determinism depends on the mock's `decide_agent_step` — verify tests fail if it returns nondeterministic output; (4) `onprem_only` must hold on every hop (Task 2 test covers it).
