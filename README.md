# Serving Plane Prototype

Demo-grade implementation of the **Serving Plane only** (steps 1–10) of the Unified
LLM Orchestration architecture. Built to prove three things to a customer:

1. **Privacy-first** — sensitive requests never leave their infrastructure
2. **Cost reduction** — two independent levers: per-call routing to cheap/local
   models, and Headroom context compression (60–95% fewer tokens on tool outputs)
3. **Observable** — every decision captured in Langfuse via NeMo Agent Toolkit

The Improvement Plane (training, evaluation, promotion) is **out of scope**.

## Quick start

```bash
cp .env.example .env
docker compose up
```

UI at `localhost:5173`, API at `localhost:8080`, Langfuse at `localhost:3000`.
Defaults to the mock backend — no GPU, no API keys, no network required.

```bash
python -m tests.traffic --requests 500 --rps 10 --seed 42
```

## Using this with Claude Code

`CLAUDE.md` is the anchor — read it first. Skills in `.claude/skills/` carry
area-specific constraints:

| Skill | Covers |
|---|---|
| `privacy-gate` | Steps 2–3: Presidio, redaction, fail-closed, redaction map |
| `headroom-compression` | Step 5: Headroom, CCR residency, bypass rules |
| `routing-and-gateway` | Steps 6–8 (+ step-4 task mode): scoring, adapters, confidence |
| `output-controls` | Step 9: leakage checks, `require_verification`, streaming policy |
| `nemo-agent-toolkit` | Agent runtime + Langfuse export |
| `inference-backends` | mock / vLLM / Dynamo / none (owns the full Dynamo skills list) |
| `demo-ui` | Flow visualizer, cost dashboard |
| `traffic-and-tests` | Personas, generator, assertions |
| `greenlake-deploy` | Docker, Helm, HPE GreenLake |

Also install NVIDIA's verified Dynamo skills rather than reinventing them — the full
`npx skills add …` list lives in the `inference-backends` skill (single source). Read
that skill before any Dynamo work.

Suggested first prompt:

> Read CLAUDE.md and the skills in .claude/skills/. Build phase 1 (skeleton) from
> the build order, then stop and show me the structure.

Build phase by phase. Don't ask for the whole thing in one go.

## Backends

| Mode | Use |
|---|---|
| `mock` | **Demo default.** Deterministic, no GPU, never fails live. |
| `vllm` | On-prem tier via vLLM OpenAI-compatible server. |
| `dynamo` | Multi-GPU TensorRT-LLM load distribution. K8s + GPUs. |
| `none` | No local inference; `onprem_only` requests are **rejected**. |

Switch in `config/serving.yaml` or via Helm values. No rebuild.

## Context compression (Headroom)

Off by default so the demo can show before/after. Enable with
`context_optimization.enabled: true`. Compresses tool outputs, logs and history
before the LLM call; CCR keeps originals **locally** for retrieval.

Two rules that matter: compression runs *after* the policy gate (never before), and
the CCR cache must stay inside the residency boundary. See the
`headroom-compression` skill.

## Deployment

`deploy/helm/serving-plane` with `values-greenlake.yaml` for HPE GreenLake.
See the `greenlake-deploy` skill for storage class, ingress, registry, and
air-gapped considerations.

## Notes

- All PII in test personas is synthetic (Faker). Never load real customer data.
- Rehearse offline; assume venue wifi fails.
- Estimated build effort with Claude Code: 2.5–4.5 days.
- Headroom is Apache 2.0 and runs locally; no data leaves the machine.
