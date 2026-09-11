# RouteForge positioning — value-per-token optimization

Captured from collected marketing/positioning notes. Companion to
`routeforge-demo-talking-points.md` (this is the "why it matters" framing; that is the
demo script).

> **Status check 2026-09-10.** Verified against `app/routing/router.py`,
> `app/routing/strategy.py`, and `config/routing.yaml`. The previous revision of this
> document claimed RouteForge evaluates "model capability, quantization, inference
> engine, endpoint capacity, GPU availability" per request. Of those, the router keys
> on none directly except a coarse per-model quality score and a `capacity` scoring
> weight that is currently a constant (see the dimension-by-dimension note under "Core
> message" below) — quantization, inference engine choice, and GPU availability are not
> router inputs at all; RouteForge doesn't run the inference stack that would know
> them. The claims below are corrected to what the router actually does. Current
> authority: `docs/architecture/routing-tier-selection.md`.

## One-liner

**RouteForge — maximize the value of every token through privacy-aware, per-call
routing.**

RouteForge is a self-hosted request router and policy gateway that sits in front of
your model endpoints. For every call it filters out any endpoint that residency or
tenant policy rules out, picks a cost tier from the request's length, risk
classification, and any escalation from a low-confidence prior answer, then scores the
remaining candidates in that tier on cost, quality, and latency — and records why in a
full decision trace. It connects to endpoints you deploy and operate yourself
(vLLM, TensorRT-LLM, NVIDIA Dynamo, or any other OpenAI-compatible server); it never
runs, schedules, or tunes the inference stack behind them. Integrated Langfuse
observability captures every decision — model chosen, alternatives rejected and why,
cost, latency — with PII redacted by default.

## Why intelligent routing is more than choosing a model

Every request has a cost, latency, and privacy profile. RouteForge's job is the
request-level decision — which endpoint is even eligible, which tier it needs, and
which candidate in that tier wins — captured as a **decision record**, not the
infrastructure-level decisions (which quantized weights to load, how to batch, which
GPU to place a worker on) that belong to the serving stack the operator runs.

## What RouteForge decides, and what it hands off

| Layer | Who decides it | What actually happens |
|---|---|---|
| Routing & governance | **RouteForge** | Filters candidates by residency and per-app policy, picks a tier from `max(length-based complexity, risk floor, escalation floor)` clamped to the best residency-permitted tier, then scores the surviving candidates on cost, quality, and latency (a `capacity` scoring weight also exists in config but is not yet fed a live signal — see "Core message" below before quoting it). Every decision is captured in a trace, chosen and rejected candidates alike. |
| Model selection within a tier | **RouteForge** | Chooses *which onboarded model* answers, from the models the operator has registered in that tier — on cost, a quality score (published at onboarding or a tier-default), and latency. |
| Quantization, batching, KV-cache strategy | **The operator's serving stack** | Fixed by whatever the operator deployed behind the endpoint they onboarded (vLLM / TensorRT-LLM / NVIDIA Dynamo / any OpenAI-compatible server). RouteForge has no quantization axis and no view into batching or cache internals — see `docs/architecture/decisions/inference-layer-boundary.md`. |
| GPU pool, region, cluster placement | **The operator's platform** | Kubernetes / Dynamo's job, not RouteForge's. RouteForge doesn't observe GPU availability and doesn't route to a region or cluster. |
| Hardware generation | **The operator's infrastructure** | RouteForge never sees which GPU generation served a request. |

## Core message

RouteForge turns every inference request into a **value-per-token decision**. Per call,
it evaluates:
- Which endpoints are even eligible, given residency and per-app policy (`onprem_only`,
  `allowed_providers` narrow the candidate pool *before* anything is scored)
- What tier (cheap / mid / frontier) the request needs, from its length, its risk
  classification, and any escalation from a prior low-confidence answer
- Among the endpoints in that tier, which one wins on cost, quality, latency, and
  capacity — the router's declared scoring inputs (`config/routing.yaml: weights`).
  *(Capacity carries a configured weight today but is not yet fed a live per-endpoint
  signal — see the `routing-and-gateway` skill before quoting it as a live differentiator.)*
- Whether the request's privacy posture forces on-prem-only serving and, if so, the
  best *allowed* tier — residency always wins over the risk floor, never the reverse

What it does **not** evaluate, because these live one layer below the router, on
infrastructure the customer runs: model quantization, which inference engine served the
request, live GPU availability, or hardware generation.

## Compounding effect

**Residency filter → tier selection → within-tier scoring.** Each stage narrows the
field the next one works on: residency and policy decide who's even eligible, tier
selection decides which price band the request should sit in, and within-tier scoring
picks the specific model. Get the residency filter wrong and a sensitive request can
leak regardless of how well the rest scores; get tier selection wrong and every
downstream saving is measured against the wrong baseline.

## RouteForge-specific positioning

RouteForge does **not** need to deploy or manage every model directly. Model operators
independently deploy models (vLLM, TensorRT-LLM, Dynamo, or another platform) and
**onboard** them into RouteForge (`POST /admin/infra/models`) with a tier, a price, and
an optional published quality score. RouteForge **connects** to those endpoints and
makes routing decisions using that onboarding metadata, probed when the endpoint is
onboarded or edited, plus a same-request fallback to another model in the same tier if
the chosen one fails mid-call — there is no continuous background health poller or live
capacity feed today, so avoid claiming one in front of a technical buyer. (Consistent
with the inference-layer boundary — see `docs/architecture/decisions/inference-layer-boundary.md`.)
