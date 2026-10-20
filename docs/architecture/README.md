# Architecture

How RouteForge works today. Claims in this directory are meant to be checkable
against the code; last reviewed 2026-10-20 against `main` (component tables in
`logical-and-request-flow.md` re-checked, line numbers replaced with symbols). Doc
status stamps that name an `api:` version describe what was verified at that version.

## `architecture/` vs `decisions/` vs `specs/`

Three subdirectories, three different kinds of trust:

- **`architecture/`** (this directory) — descriptive. What RouteForge does
  today, checkable against the current code and diagrams.
- **`architecture/decisions/`** — a dated scope call: why RouteForge does or
  doesn't do X, and when that call was made. A decision doesn't expire just
  because time passes; if a later decision supersedes an earlier one, the
  superseded doc carries a banner saying so rather than being deleted.
- **`architecture/specs/`** — a binding contract for a shipped feature, cited
  by path from the application code that implements it. Treat these as
  load-bearing: a change to the code that violates one should update the spec
  in the same change.

## Files in `architecture/`

| Doc | Summary |
|---|---|
| [`logical-and-request-flow.md`](logical-and-request-flow.md) | Companion notes to the draw.io diagrams — the ten Serving-Plane steps and the end-to-end request/response flow. |
| [`sample-request-flows.md`](sample-request-flows.md) | Worked examples: real requests walked through the ten steps, branch by branch. |
| [`routing-tier-selection.md`](routing-tier-selection.md) | How the step-6 router actually picks a tier and model (length-based complexity, not semantic difficulty) and what it can't key on (no modality/capability axis). |
| [`api-load-balancing.md`](api-load-balancing.md) | How the `api` deployment scales horizontally — L4/L7 balancing layers and why per-pod in-memory state was a bug. |
| [`multidimensional-policy-model.md`](multidimensional-policy-model.md) | The conceptual policy model: why one strict/private/cheap switch is not enough, the placement and protection axes, worked policy profiles, and compile → feasible set → optimise → enforce. Status note at the top says which axes are built (tool access `G` is not). |

## Files in `architecture/specs/`

| Doc | Summary |
|---|---|
| [`semantic-cache-residency-spec.md`](specs/semantic-cache-residency-spec.md) | Binding residency + correctness contract for the semantic cache (`app/cache/store.py`) — a cached answer is a second copy that must stay CCR-class and residency-partitioned. |
| [`persistent-memory-residency-spec.md`](specs/persistent-memory-residency-spec.md) | Binding residency contract for persistent memory (mem0-style, step-5 extension) — the CCR-class guarantees a cross-request memory store must honor. |

## Diagrams

See [`diagrams/README.md`](diagrams/README.md) for the three draw.io diagrams
(logical architecture, physical architecture, request flow) and how to
regenerate them from their JSON specs.

`llm orchestration loop.drawio` (this directory) is the original diagram,
superseded by the three above. **Historical; contains removed concepts (NAT agent
runtime, multi-step branch, `strong` tier) — do not use as a reference.** It is retained only for its "Prompt / Instructions — DSPy / NAT optimizer" node as evidence that
prompt optimization was in the design from day one; that node has no
counterpart in any of the three current diagrams, so it is the one thing the
old file still shows that the new ones don't.

## Canonical step count: 1–10

The Serving Plane is **steps 1–10** (1 Incoming Request → 10 Governed Response). All
prose uses this count — not "1–8" or "1–9". The Improvement Plane (training,
evaluation, promotion, registry) runs on external compute; RouteForge only captures
substrate, triggers jobs and onboards results (CLAUDE.md rule 4). It is not in this diagram.

## Serving paths: on-prem models are onboarded at runtime

Step 8 shows two kinds of serving path — operator-run **on-prem endpoints**
(vLLM / Dynamo / NIM, any OpenAI-compatible server) and **managed cloud** (Azure/Bedrock). There is no deterministic
canned-response backend: the old `mock` backend was **removed** in the LiteLLM gateway
swap (2026-07-30), when LiteLLM became the single egress path for every provider. Tests
that need determinism fake the LiteLLM SDK call itself (`tests/_litellm_fake.py`), not a
mock backend class.

The `inference_backend: vllm | dynamo | none` axis was **removed** in the pure-router <!-- purged-ok -->
pivot (2026-08-03). routeForge no longer picks an on-prem backend in YAML — on-prem models
are **onboarded at runtime** (each with its own OpenAI-compatible `endpoint`) and live in
the `ModelRegistry`; see `docs/guides/onboarding-models.md`. The on-prem pool boots empty. The
managed-cloud path's availability is still `providers.{azure,bedrock}.enabled`, and there
is **no** separate `serving_paths` config axis. <!-- purged-ok -->

So the honest "what's real vs simulated?" answer is: onboarded vLLM/Dynamo endpoints =
real on-prem paths; `managed_cloud` = real cloud path; **nothing here is a demo-safe
simulation.** If no on-prem model is enabled, `onprem_only` requests are rejected
(derived — `has_healthy_onprem()` is false), never a silent cloud fallback. See the
`inference-backends` skill.

## Redaction map return path — placeholders, and nothing else

The gate (step 2) replaces entities with placeholders (`<SSN_1>`) and keeps the map
internal. **There is no detokenization.** The map is built during `anonymize()` and then
discarded; nothing re-inserts an original value, there is no broker, and there is no
config switch that turns one on. Placeholders are the deliverable, deliberately — they
show redaction working — and `model_input: "tokenize"` /
`client_output: "authorized_rehydrate"` are rejected with a 422
(`AXIS_VALUE_NOT_IMPLEMENTED`) rather than faked.

The consequence worth stating: **a redacted value cannot be a lookup key.** An app whose
tool must find a record by SSN needs `model_input: "raw"` — the axis exists for exactly
that — not a placeholder. See the `privacy-gate` and `output-controls` skills, and
`docs/reference/per-app-config.md` for the axis.
