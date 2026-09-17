# Remove the NeMo agent runtime and the multi-step path

**Status:** Approved design, 2026-09-17. Not yet implemented.
**Precedes:** Priority 1 slice P1a (policy compiler + noninterference harness), which
will no longer have to model an internal agent path.

## 1. Why

RouteForge is a pure router. Tools belong to the calling application: on the
OpenAI-compatible path RouteForge returns `tool_calls` and never executes them
(`app/pipeline.py`, `finish_reason="tool_calls"`). Agent loops are therefore
client-driven — each model call in the loop is a separate request that is gated,
compressed, routed, and verified on its own.

The internal NeMo Agent Toolkit (NAT) path contradicts that:

- **It hijacks ordinary requests.** `app/routing/task_mode.py` sends any request
  whose last user message contains `"step by step"` or `"look up"` to the internal
  agent. A customer asking "explain step by step" is answered by a NAT
  `tool_calling_agent` instead of the model they routed to.
- **Its tools are demo code.** `calculator`, `doc_search`, and `crm_lookup`
  (`nat_tools/src/nat_tools/tools.py`); the last two return canned data.
- **Its hops are not gated.** `app/agent/internal_endpoint.py` compresses and routes
  each hop but runs no detector over hop messages, tool arguments, or tool results.
  Only the final text is verified.

## 2. Scope

### 2.1 Removed

| Area | Items |
|---|---|
| Agent runtime | `app/agent/` (`runtime.py`, `internal_endpoint.py`, `openai_sse.py`, `__init__.py`); `internal_router` registration in `app/main.py`; `AgentCapExceeded` handling in `app/api/routes.py` |
| NAT tools | `nat_tools/` package; `config/nat_workflow.yaml`; `nvidia-nat[langchain]` and `nat_tools` in `pyproject.toml` (plus the `[tool.uv.sources]` entry and any NAT-only pins/filters); `COPY nat_tools/` in `Dockerfile` |
| Pipeline | `Pipeline._run_multi_step`; every `multi_step` branch in `run` / `run_stream`; the `multi_step` reason for `StreamFallback` (streaming floors become `onprem_only` and `require_verification` only); the per-hop port/stash wiring in `Pipeline.__init__` and `main.py` |
| Step-4 LLM classifier | `app/routing/task_mode_llm.py`; `Pipeline._decide_task_mode`'s classifier branch; `config/prompts/task_mode_classifier/` |
| Stage signals | `app/routing/signals.py`; `StageSignalsCfg` and `RoutingCfg.stage_signals` (`app/config/schema.py`); its lint check (`app/config/lint.py`); the `stage_signals:` block in `config/routing.yaml` |
| Budgets / infra config | `PolicyCfg.agent_budgets` and `agent_budgets:` in `config/policy.yaml`; the `agent:` block in `config/serving.yaml`; `servingPlane.agent.runtime` in `deploy/helm/serving-plane` and `deploy/hpe/routeforge` (values + configmap templates) |
| Test tooling | `multi_step` persona and its large-tool-output overlay (`tests/traffic/personas.py`, `tests/personas.yaml`); agent/NAT/multi-step tests |
| Repo skill | `.claude/skills/nemo-agent-toolkit/` |

### 2.2 Kept

- **Step 4 remains, as an explicit constant decision.** `task_mode.decide()` returns
  `"direct"` unconditionally. The "steps 1–10" numbering and the console drill-down
  step keep their names. **Note:** step 4 emits no `DecisionRecord` today (verified
  2026-09-17: nothing in `app/` constructs one with `step="task_mode"`, and the console
  derives the step body from the router/hop records). This change does not add one —
  recording the compiled task mode belongs to P1a's decision certificate. The console caption changes from "Direct answer vs. multi-step agent"
  to "Direct — agent loops run in the client" (`ui/src/api.ts`).
- **`mock_multistep` is silently ignored.** `ChatRequest` uses `extra="allow"` and
  never declared the field (`task_mode.py` read it via `getattr`), so removing the
  read is sufficient. A client that sends it gets a normal direct-path 200.
- **Prompt-optimization framework** (`app/config` prompt store, `routing.prompt_opt`,
  `sp.prompt_version`) stays; DSPy remains in scope per the 2026-08 scope expansion.
  Its only production artifact was `task_mode_classifier`, so after removal
  `active_prompts` is empty in production. Its tests move to a test-only fixture
  artifact so the framework stays exercised.
- **Headroom compression** on the direct path, CCR, the semantic cache, memory, the
  tool-conversation path (client-driven tool calling), and the separate routing vs.
  compression savings accounting are unchanged.
- **Langfuse export** is unchanged: RouteForge exports through its own OTel exporter
  (`app/obs/`); NAT's native exporter was already disabled.

## 3. Compatibility and deployment

- **No database change.** No table or column references the agent path.
- **Stale config keys must not break boot.** Deployed override files (Spark, DGX,
  Helm values) may still contain `stage_signals`, `agent_budgets`, or `agent:`.
  Pydantic config models ignore unknown keys by default; the plan must verify that
  neither the loader nor the cross-file lint rejects these keys. If either does, the
  stale keys are accepted and reported as a **lint warning naming the key**, not a
  boot failure. A test boots with all three stale keys present.
- **Clients.** Requests that previously went multi-step (trigger phrases or
  `mock_multistep`) now go direct. Response shape is unchanged. The `trace` no longer
  contains hop records for such requests.
- **Image size** drops with `nvidia-nat[langchain]` gone; the plan checks that no
  surviving code imported a package that was only present transitively through it
  (the `pyproject.toml` comment at line ~23 names one such pin).

## 4. Documentation

- **CLAUDE.md** (owner-approved amendment):
  - Architecture table: step 4 described as "always `direct`; agent loops are
    client-driven"; remove the "Agent Runtime / Orchestrator" row.
  - Replace "Multi-step (step 4 → agent runtime) uses NeMo Agent Toolkit … per-call"
    with: every model call a client's agent loop makes is its own request and
    re-enters steps 2–9.
  - "The step-4 classifier is a routed call" paragraph: remove.
  - Routing scopes: remove the NAT Router Agent bullet (keep step-4 note).
  - Routing observability: remove item (2) `routing.stage_signals`.
  - Config table: `policy.yaml` owns budgets (no `agent_budgets`).
  - Tech stack: remove NeMo Agent Toolkit; Langfuse export is RouteForge's own OTel
    exporter.
  - Skills list: remove `nemo-agent-toolkit`.
  - Build order: layer 5 "Agent runtime" marked removed (2026-09-17).
  - Sales walkthrough: remove step 3; step 4 (Headroom) re-runs a long
    conversation instead of a multi-step task.
- **Reference/guides:** `docs/reference/routing-observability.md` (remove stage
  signals), `per-app-config.md`, `api-reference.md`, `README.md`;
  `docs/guides/getting-started.md`, `agentic-app-guide.md`,
  `creating-a-sample-application.md`, `reading-the-dashboard-and-trace.md`,
  `integrating-an-app.md`, `ui-guide.md`, `exporting-eval-datasets.md`,
  `guides/README.md`.
- **Architecture:** `docs/architecture/logical-and-request-flow.md`,
  `sample-request-flows.md`, `routing-tier-selection.md`, and the request-flow and
  logical-architecture diagrams (regenerate from `diagrams/specs/*.json` via the
  drawio skill). Decision records get a dated "superseded by 2026-09-17 removal" note
  rather than rewrites.
- **Skills:** update `routing-and-gateway`, `traffic-and-tests`, `demo-ui` for the
  removed branch.
- **Research:** `docs/roadmap/ip-and-research-opportunities.md` — IP-1 / R3
  ("monotone privacy context across agent hops") re-targets client-driven agent
  loops, where each hop is a separate request and the ratchet is per conversation;
  this needs a conversation/session identifier, which RouteForge does not receive
  today. Note that `signals.py`'s stage heuristics were removed with the agent and
  could be revived for client-driven loops under that work.
- `docs/archive/` and past `docs/superpowers/` specs and plans are historical and
  are not edited.

## 5. Testing

**Deleted or rewritten** (agent, NAT, multi-step, stage signals, classifier):
`test_agent_hop_compression.py`, `test_agent_internal_endpoint.py`,
`test_agent_runtime.py`, `test_agent_pipeline_e2e.py`, `test_agent_mock_sse.py`,
`test_agent_telemetry_redaction.py`, `test_litellm_multistep.py`,
`test_nat_workflow_config.py`, `test_routing_signals.py`, `test_signal_wiring.py`,
`test_task_mode_classifier.py`, plus the multi-step cases in
`test_compression_and_mode.py`, `test_error_taxonomy.py`, `test_traffic_honesty.py`,
`test_traffic_personas.py`, `test_run_stream_pipeline.py`, `test_stream_keepalive.py`,
`test_stream_unverified.py`, `test_semantic_cache_config.py`, `test_config_lint.py`,
`test_prompt_optimizer.py`, `test_prompt_store.py`, and `tests/conftest.py`'s `HOPS`
fixture. The plan enumerates each file from a fresh grep rather than trusting this
list.

**New tests:**

1. `"explain this step by step"` and `"look up the order status"` route `direct`
   through `/v1/chat/completions`; the trace has a `task_mode` record with
   `branch == "direct"` and no hop records.
2. A request with `mock_multistep: true` returns 200 via the direct path.
3. The app boots and the lint passes (warning allowed) with stale `stage_signals`,
   `agent_budgets`, and `agent:` keys in the config files.
4. No module under `app/` imports `nat` or `app.agent` (static check).
5. A streaming request on a non-`onprem_only`, non-`require_verification` app with
   `stream_mode: stream_unverified` still streams; the removed `multi_step` fallback
   reason does not appear.

**Gates:** full `pytest` (with `LITELLM_LOCAL_MODEL_COST_MAP=True`), the
`tests/onboarding` conformance suite, `ui` type-check/build, and `helm template` for
both charts.

## 6. Out of scope

- Priority 1 (P1a/P1b) policy compiler, decision certificate, and harness.
- Any change to protection semantics, `policy_mode`, `capture_io`, or streaming modes
  beyond removing the `multi_step` fallback reason.
- Conversation-level privacy ratchet for client-driven loops (future IP-1 work).
- Deleting `docs/new/` handoffs (they remain until the P1a spec supersedes them).
