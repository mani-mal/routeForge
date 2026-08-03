# Architecture diagram — reading notes

`llm orchestration loop.drawio` is the canonical picture of the **Serving Plane**.
Two things the current diagram does not yet draw explicitly; treat these notes as
authoritative until the `.drawio` is redrawn to match.

> **Where routeForge stops and the serving layer begins** — for the recurring
> "does routeForge do llm-d / DRA / Gateway API / prefill-decode?" question and the
> "why routeForge does not deploy Dynamo" decision, see
> [`inference-layer-boundary.md`](./inference-layer-boundary.md).

## Canonical step count: 1–10

The Serving Plane is **steps 1–10** (1 Incoming Request → 10 Governed Response). All
prose uses this count — not "1–8" or "1–9". The Improvement Plane (training,
evaluation, promotion, registry) is out of scope and not in this diagram.

## Serving paths: `mock` is gone — vLLM is the default

Step 8 shows three serving paths — **Dynamo + TensorRT-LLM**, **vLLM + Semantic
Router**, and **managed cloud** (Azure/Bedrock). The diagram is now correct as drawn:
the old `mock` backend was **removed** in the LiteLLM gateway swap (2026-07-30), when
LiteLLM became the single egress path for every provider. There is no
deterministic canned-response backend any more.

- The default `inference_backend` is **`vllm`**. Tests that need determinism fake the
  LiteLLM SDK call itself (`tests/_litellm_fake.py`), not a mock backend class.

So the honest "what's real vs simulated?" answer is: `vllm`/`dynamo` = real on-prem
paths; `managed_cloud` = real cloud path; **nothing here is a demo-safe simulation any
more.** There is a single serving-path axis — the on-prem path is `inference_backend`
(`vllm | dynamo | none`); the managed-cloud path's availability is
`providers.{azure,bedrock}.enabled`. There is **no** separate `serving_paths` config
axis. An unreachable on-prem endpoint at boot is a fail-closed `sys.exit(1)`, never a
silent fallback (`allow_backend_fallback` no longer exists). See the
`inference-backends` skill.

## Redaction map return path

The gate (step 2) replaces entities with placeholders (`<SSN_1>`) and keeps the map
internal. Whether step 10 detokenizes is a **config decision**, not an omission:
demo mode returns placeholders deliberately (it shows redaction working); detokenize
mode re-inserts values inside the gate's trust boundary, after step 9. See the
`privacy-gate` and `output-controls` skills.
