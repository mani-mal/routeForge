# LiteLLM Gateway Swap + Real Multi-Step Egress — Design

**Date:** 2026-07-30
**Status:** Approved (design); pending plan.
**Supersedes design intent in:** `2026-07-26-routeforge-per-application-console-design.md` §9
("LiteLLM — gateway swap"), which is executed here. Memory:
`routeforge-gateway-router-decisions.md`.

## 1. Goal

Replace the hand-rolled backend adapter layer (`app/gateway/backends/{mock,vllm,dynamo,
openai_compat}.py`) with a **single LiteLLM egress path** that can reach multiple
providers per call (**onprem vLLM/Dynamo + Azure + Bedrock**), and wire the
**multi-step/agent path to call real models** so traffic-generator traffic — single-shot
*and* agent — actually hits real backends. No demo/laptop concessions: the mock backend
is removed entirely.

## 2. Motivation

Today `inference_backend` selects **one** backend for the whole deployment; Azure/Bedrock
are config-present but never actually reachable, and the multi-step agent path *fakes*
its completions (`app/agent/mock_agent.py` scripts the tool-calls; the internal endpoint
returns canned SSE and never calls a backend). LiteLLM's value here is **per-call
multi-provider egress**: one deployment routing cheap→local vLLM and frontier→Azure/
Bedrock in the same request stream, with the agent driven by real models.

## 3. Locked decisions (from the brainstorm)

1. **Full replacement.** One `LiteLLMBackend`; the four adapter files are removed. `base.py`
   shared helpers (`count_tokens`, `seeded_latency_ms`/`TIER_LATENCY_MS`, the
   `InferenceBackend` Protocol, `BackendHealth`) stay — they have consumers beyond the
   adapters (`app/pipeline.py`, `app/compression/`, `app/agent/internal_endpoint.py`).
2. **Providers in v1:** onprem + Azure + Bedrock, all wired; live cloud calls need creds
   in `.env` (paths offline-tested).
3. **Mock dropped entirely.** No mock runtime mode. Offline test isolation is by
   monkeypatching `litellm.acompletion`.
4. **Real multi-step egress included.** The internal agent endpoint calls the real backend
   per hop; the scripted `decide_agent_step` logic moves into test fixtures.

### Decided by project rules (not re-litigated)

- **Cost stays `pricing.yaml`.** CLAUDE.md makes pricing.yaml the single owner of per-model
  USD and the config lint forbids a second cost source, so we do **not** use LiteLLM's
  `completion_cost`. LiteLLM's `usage` supplies token counts; our formula
  (`(tokens_in*price.input + tokens_out*price.output)/1e6`) supplies USD.
- **Confidence** reuses the existing mean-logprob→temperature-softened logic
  (`routing.confidence_logprob_temperature`); providers that don't return logprobs (often
  Bedrock) fall to `DEFAULT_CONFIDENCE = 0.9` — never escalate on a missing signal.
- **LiteLLM's own Langfuse callback stays OFF** (`litellm.success_callback`/
  `failure_callback` untouched). Same trap as NAT's native exporter (rule 2, redact on
  egress). We keep exporting redacted traces via `app/obs/export.py`.
- **`litellm` becomes a direct, pinned dependency** in `pyproject.toml` (currently present
  only transitively via `nvidia-nat`/`langchain-litellm`, so no new download weight).

## 4. The three guardrails (CLAUDE.md hard rules)

1. **Behind the gate (rule 1).** `LiteLLMBackend` is only ever called inside `Gateway.run`
   / the internal hop endpoint, both downstream of the step-2 gate. FastAPI stays the
   front door; LiteLLM is never the ingress proxy.
2. **Pinned, not self-routing.** We call `litellm.acompletion()` against the **single**
   model the router already chose, with `num_retries=0` and **no `litellm.Router`**. Our
   `Gateway.run` loop remains the only escalation/fallback authority. Two routers never
   fight.
3. **`onprem_only` = cloud stripped upstream (rule 3).** The gate already emptied cloud
   from `ctx.allowed_providers`, so the router only ever hands the backend an allowed
   candidate. Defense in depth: a **startup assertion** that the provider→LiteLLM map only
   contains *enabled* providers, plus the existing `RecordingBackend` "PII never reaches a
   cloud provider" test re-pointed at `LiteLLMBackend`.

## 5. Architecture

### 5.1 The backend seam is preserved

`Gateway.run` (`app/gateway/gateway.py`), the router, the escalation/fallback loop, and
`DecisionRecord` are **unchanged**. The Protocol seam
`generate(model, messages, price, tier, hint, tools=None) → GenerationResult` is what
`Gateway.run` and the traffic-honesty `RecordingBackend` observe, so the privacy guard
keeps working byte-for-byte.

### 5.2 `LiteLLMBackend` (new — `app/gateway/backends/litellm_backend.py`)

```python
class LiteLLMBackend:
    name = "litellm"

    def __init__(self, settings: Settings, *, logprob_temperature: float = 1.0) -> None: ...

    async def generate(self, model: str, messages: list[Message], price: PriceCfg,
                        tier: str, hint: dict,
                        tools: list[dict] | None = None) -> GenerationResult:
        litellm_model, kwargs = self._map(model)     # provider -> pinned string + conn
        resp = await litellm.acompletion(
            model=litellm_model, messages=[...], tools=tools,
            num_retries=0, timeout=..., drop_params=True, **kwargs)
        # cost from pricing.yaml; confidence from logprobs (temp) else 0.9;
        # returns GenerationResult(text, tool_calls, tokens_in/out, latency_ms, cost_usd, confidence)

    async def health(self) -> BackendHealth: ...      # probes the onprem endpoint only
```

`_map(model)` resolves the router-pool slug (e.g. `local-8b`) to `(litellm_model, kwargs)`
using the model's `provider` and the serving.yaml connection config:

| provider | litellm model | connection |
|---|---|---|
| onprem (vllm) | `openai/<litellm_model or name>` | `api_base=vllm.base_url`, `api_key=$SP_ONPREM_API_KEY` (dummy) |
| onprem (dynamo) | `openai/<…>` | `api_base=dynamo.endpoint` (+`/v1`), dummy key |
| azure | `azure/<deployment>` | `AZURE_API_BASE`/`AZURE_API_KEY`/`AZURE_API_VERSION` |
| bedrock | `bedrock/<model-id>` | AWS_* env (standard) |

`_map` raises if the chosen model's provider isn't enabled/mapped — the belt to the
router's braces (guardrail 3).

### 5.3 `GenerationResult` gains tool-calls

`app/models.py`: add `tool_calls: list[dict] | None = None`. The direct path never passes
`tools` and ignores it; the multi-step hop path uses it.

### 5.4 Factory + startup (`factory.py`)

- `make_backend(settings)` → single `LiteLLMBackend(settings, logprob_temperature=…)`.
- `resolve_backend`: for `vllm`/`dynamo`, health-check the onprem endpoint; unreachable →
  `sys.exit(1)`. **No fallback-to-mock** (removed). For `none`, skip the onprem probe
  (onprem is structurally disabled in `context.py`); cloud reachability is checked
  per-call. The throwaway-probe / fresh-serving-instance loop-safety pattern is retained.

### 5.5 Real multi-step egress (`internal_endpoint.py`)

After the existing per-hop routing + compression, replace the faked completion:

```python
# comp.messages = the COMPRESSED output (NOT raw hop_msgs) — the model must
# receive, and be billed on, post-compression content, matching the direct path
# and CLAUDE.md's post-compression pricing rule. (Corrected 2026-07-31 in review.)
det_result = await backend.generate(
    decision.chosen.name, comp.messages, price, decision.chosen.tier, hint, tools=tools)
HOPS.record(run_id, DecisionRecord(
    step="agent_hop", chosen=decision.chosen.name, ...,
    latency_ms=det_result.latency_ms,            # REAL, not seeded
    actual_cost_usd=det_result.cost_usd,         # REAL, populates the total
    tokens_priced=tokens_after, hop_index=hop_index))
gen = (sse_tool_call(...real args...) if det_result.tool_calls
       else sse_final(model=..., content=det_result.text))
```

- The backend is threaded via `request.app.state.backend` (already resolved in `main.py`).
- **Per-hop = one real call, no per-hop escalation/fallback** — matches today's multi-step
  behavior (multi-step never escalated); escalation stays a direct-path feature. Documented
  in code so it isn't read as an omission.
- The real model now decides tool-calls; NAT executes the real local tools
  (`calculator`, `doc_search`, `crm_lookup` from `config/nat_workflow.yaml`). `model_name:
  auto` is still ignored — our router picks the model per hop.
- Invariants preserved: hop `ctx` enforces `onprem_only`; egress redaction (`verify`) still
  runs on the final text; `max_iterations` cap + `AgentCapExceeded` unchanged (more relevant
  with real models); NAT-native telemetry stays OFF (`_configure_nat_telemetry` no-op).
- `app/agent/mock_agent.py` is **removed from production**; its scripted tool_call→final
  logic is reused as a **test fake** for `litellm.acompletion`. `app/agent/openai_sse.py`
  stays (now serializes real model output). `seeded_latency_ms`/`TIER_LATENCY_MS` may become
  production-unused after this change; retained only if a test still needs them (decided in
  the plan).

## 6. Configuration changes

### `config/serving.yaml`
- `inference_backend`: enum becomes `vllm | dynamo | none` (**`mock` removed**); default
  `vllm`.
- **Remove** `allow_backend_fallback` (its fallback target no longer exists).
- Reuse existing `vllm`/`dynamo` blocks for the onprem endpoint. Add per-provider LiteLLM
  connection detail (prefix + env-var names) — owner: serving.yaml (infra/transport).

### `config/routing.yaml`
- Optional `litellm_model` on each `model_pool` entry (owner: routing.yaml — "which models
  and how they're addressed downstream"). Defaults to `<prefix>/<name>`.

### `app/config/schema.py` + `app/config/lint.py`
- Schema: new provider-connection fields, optional `litellm_model`, drop
  `allow_backend_fallback`, `inference_backend` enum drops `mock`.
- Lint: every **enabled** provider (onprem per `inference_backend`, azure/bedrock per
  `providers.*.enabled`) has connection config; every enabled `model_pool` tier resolves to
  a LiteLLM model string; existing single-owner/tier checks retained.

### `.env.example`
- Add `SP_ONPREM_API_KEY` (dummy for vLLM), `AZURE_API_KEY`/`AZURE_API_BASE`/
  `AZURE_API_VERSION`, `AWS_ACCESS_KEY_ID`/`AWS_SECRET_ACCESS_KEY`/`AWS_REGION_NAME`
  placeholders. No secrets committed.

## 7. Test strategy

**Offline isolation:** a shared `conftest` helper monkeypatches `litellm.acompletion` with a
`fake_acompletion` returning a canned OpenAI-shaped response (content *or* tool_calls,
optional logprobs). This is the single mechanism replacing the removed `MockBackend`.

- **Rewrite** adapter tests (`test_backends`, `test_vllm_backend`, `test_dynamo_backend`,
  `test_backend_selection`, `test_confidence_calibration`) against `LiteLLMBackend`:
  provider→model-string mapping (onprem/azure/bedrock), cost from `pricing.yaml`, confidence
  from logprobs vs default, provider-not-enabled rejection, `resolve_backend` exit-on-
  unreachable.
- **Re-point** every direct `MockBackend(...)` construction (`conftest`, `test_gateway`,
  `test_two_lever_cost`, `test_display_name`, `test_controls`, `test_traffic_honesty`) at
  `LiteLLMBackend` + the fake.
- **Regression contract (must stay green):** `test_traffic_honesty` (privacy guard),
  `test_two_lever_cost`, escalation (`test_gateway`), circuit-breaker/fallback.
- **New:**
  - Azure and Bedrock egress: router picks a cloud model → backend calls
    `azure/…`/`bedrock/…` (assert the pinned model string), cost from pricing.
  - `pii → onprem_only` never calls a cloud provider through `LiteLLMBackend`
    (`RecordingBackend`-style assertion on the model string passed to `acompletion`).
  - Real multi-step: `fake_acompletion` drives a `tool_call → final` sequence; assert hops
    carry **real** `actual_cost_usd`/`latency_ms`, the total sums per hop, and an
    `onprem_only` multi-step run keeps every hop on an on-prem model.

## 8. Deploy delta (next image, `:0.7.0`)

- `SP_INFERENCE_BACKEND=vllm` (already set on DGX). `litellm` already baked (transitive).
- Azure/Bedrock creds only needed if those providers are enabled — via K8s secret, not
  committed. Config lint fails boot if an enabled provider lacks connection config.
- **This changes the DGX-validated vLLM transport** (now vLLM-via-LiteLLM `openai/`). Re-run
  a traffic burst on the cluster post-deploy and confirm `backend`/completions/cost/latency,
  redaction, and Langfuse population — same acceptance as the original vLLM validation.

## 9. Out of scope (documented, not re-litigated)

- **LiteLLM Router / virtual-key budgets** — needs the proxy; per-app quotas already work via
  our `RateLimiter`. Revisit if per-app spend enforcement must live in the gateway.
- **Semantic caching** — needs CCR-grade residency + step-9 verification on hits.
- **Live cloud-credential validation** — needs real Azure/Bedrock creds; the paths are wired
  and offline-tested.
- **Per-hop escalation/fallback in multi-step** — kept as direct-path-only, unchanged.

## 10. Risks

- **Full replacement touches the validated vLLM path.** Mitigation: the full existing suite
  is the regression contract; re-validate on the DGX at deploy.
- **Real multi-step with real models can loop / not converge.** Mitigation: the existing
  `max_iterations` cap + `AgentCapExceeded` fail-closed path already handles this and becomes
  load-bearing.
- **Tool residency for `onprem_only`.** The demo tools are local NAT functions (no external
  egress). If a future tool makes external calls, that is a rule-1/rule-6 concern to gate —
  flagged, not solved here.
