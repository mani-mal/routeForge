# Architecture diagram — reading notes

`llm orchestration loop.drawio` is the canonical picture of the **Serving Plane**.
Two things the current diagram does not yet draw explicitly; treat these notes as
authoritative until the `.drawio` is redrawn to match.

## Canonical step count: 1–10

The Serving Plane is **steps 1–10** (1 Incoming Request → 10 Governed Response). All
prose uses this count — not "1–8" or "1–9". The Improvement Plane (training,
evaluation, promotion, registry) is out of scope and not in this diagram.

## Serving paths: add `mock` as a fourth, first-class box

Step 8 shows three serving paths — **Dynamo + TensorRT-LLM**, **vLLM + Semantic
Router**, and **managed cloud** (Azure/Bedrock). It is missing the path the demo
actually runs on:

- **`mock` (demo/dev)** — deterministic, seeded latency/cost, no GPU, no network. This
  is the default `inference_backend` and what every live customer demo uses.

So the honest "what's real vs simulated?" answer is: `mock` = simulated on-prem path;
`vllm`/`dynamo` = real on-prem paths; `managed_cloud` = real cloud path. There is a
single serving-path axis — the on-prem path is `inference_backend`
(`mock | vllm | dynamo | none`); the managed-cloud path's availability is
`providers.{azure,bedrock}.enabled`. There is **no** separate `serving_paths` config
axis. See the `inference-backends` skill.

## Redaction map return path

The gate (step 2) replaces entities with placeholders (`<SSN_1>`) and keeps the map
internal. Whether step 10 detokenizes is a **config decision**, not an omission:
demo mode returns placeholders deliberately (it shows redaction working); detokenize
mode re-inserts values inside the gate's trust boundary, after step 9. See the
`privacy-gate` and `output-controls` skills.
