# RouteForge positioning — value-per-token optimization

Captured from collected marketing/positioning notes. Companion to
`routeforge-demo-talking-points.md` (this is the "why it matters" framing; that is the
demo script).

## One-liner

**RouteForge — maximize the value of every token through intelligent routing and
continuous evaluation.**

RouteForge is an intelligent inference routing and evaluation layer that helps
organizations maximize the value of every token. It evaluates each request across model
capability, quantization, inference engine, endpoint capacity, GPU availability,
latency, cost, privacy, and governance requirements. It connects to independently
deployed model endpoints (vLLM, TensorRT-LLM, Dynamo, …), then directs each request to
the most suitable model and hardware environment. Integrated Langfuse evaluation
provides observability into request performance, quality, cost, latency, and routing
decisions, enabling continuous improvement.

## Why intelligent routing is more than choosing a model

Every request has a cost, latency, and quality profile. RouteForge compounds value
across the entire inference stack by selecting the right **model, endpoint, execution
strategy, and hardware** for each request.

## The layer cake

| Layer | RouteForge interpretation |
|---|---|
| Routing & governance | Selects the right model + endpoint by task complexity, quality, latency, cost, privacy, policies, and workload capacity. |
| Model & quantization | Selects the model variant and quantization level — balancing accuracy, memory, throughput, and cost. |
| Inference stack | Chooses the endpoint running the most effective serving stack (vLLM, TensorRT-LLM, Dynamo) — batching, prefix caching, KV-cache efficiency, speculative decoding, parallelism. |
| Capacity & placement | Routes to the right GPU pool, region, cluster, or deployment by capacity, queue depth, latency, locality, and priority. |
| Silicon & hardware | Accounts for hardware (H200, H100, B200, GB200, GB10, RTX PRO 6000) — generation affects throughput, memory, energy efficiency. |

## Core message

RouteForge turns every inference request into a **value-per-token decision**. It
evaluates:
- Which model can complete the task successfully
- Which quantization gives the best quality-performance balance
- Which inference engine can serve the request efficiently
- Which endpoint has sufficient capacity
- Which hardware can deliver the required latency and throughput
- Which policies, privacy requirements, and governance rules apply

## Compounding effect

**Routing → Model → Inference Stack → Capacity → Hardware.** Each layer influences the
next. A poor decision at one layer limits the benefits available from the others. Miss
one layer, and you can't optimize your way out of the others.

## RouteForge-specific positioning

RouteForge does **not** need to deploy or manage every model directly. Model operators
independently deploy models (vLLM, TensorRT-LLM, Dynamo, or another platform).
RouteForge **connects** to those endpoints and makes intelligent routing decisions using
their metadata, health, performance, and capacity signals. (Consistent with the
inference-layer boundary — see `docs/architecture/inference-layer-boundary.md`.)
