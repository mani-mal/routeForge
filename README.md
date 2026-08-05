# RouteForge

RouteForge is a **self-hostable, OpenAI-compatible model router and policy gateway**.
Applications send normal chat-completion requests with an API token; for every call
RouteForge runs a privacy gate first (PII/PHI detection + redaction + a residency
decision), then routes the call to the cheapest model/backend allowed by that
application's policy, verifies the output, records usage, and returns a standard
`ChatCompletion` **plus a decision trace** describing what it did and why. Sensitive
requests never leave on-prem when policy says so.

It implements steps 1–10 of the Unified LLM Orchestration architecture — the **Serving
Plane** — and delivers three guarantees:

1. **Privacy-first** — sensitive requests never leave the customer's infrastructure.
2. **Cost reduction** — two independent levers: per-call routing to cheap/local models,
   and Headroom context compression (60–95% fewer tokens on tool outputs).
3. **Observable** — every decision is captured in Langfuse via NeMo Agent Toolkit.

**RouteForge is a pure router.** It points at OpenAI-compatible endpoints and deploys,
scales, or load-balances **no** inference itself — operators stand up their own serving
(vLLM/Dynamo/NIM/any OpenAI-compatible server) and onboard it at runtime. The Improvement
Plane (training, evaluation, promotion) is **out of scope** and stays external.

## Quick start (local, single node)

Regex PII detection and an in-container SQLite store make a local bring-up need no GPU,
no API keys, and no network.

```bash
docker compose -f deploy/compose/docker-compose.yml up --build
```

Brings up the API (`:8080`), the console UI (http://localhost:3001), and a self-hosted
Langfuse stack (http://localhost:3000, login `demo@example.com` / `demodemo`).

Then, because the on-prem pool **boots empty**:

1. Open the console, sign in to the admin dashboard (`admin@routeforge.local` /
   `changeme` — change these for anything real).
2. **Onboard a model**: Infrastructure → Onboard, or `POST /admin/infra/models`, pointing
   at your own OpenAI-compatible endpoint. RouteForge probes it and only enables it if it's
   reachable. See [`docs/onboarding-models.md`](docs/onboarding-models.md).
3. **Create an application** and issue its `rf_live_…` token, then call the OpenAI-compatible
   API:

```bash
curl http://localhost:8080/v1/chat/completions \
  -H "Authorization: Bearer rf_live_…" \
  -H "Content-Type: application/json" \
  -d '{"model": "auto", "messages": [{"role": "user", "content": "hello"}]}'
```

Optional load tooling (product tooling, not the product): synthetic-persona traffic for
exercising every branch —

```bash
python -m tests.traffic --requests 500 --rps 10 --seed 42
```

## Two auth domains (read this first)

RouteForge has **two completely separate credentials**; mixing them up is the most common
onboarding mistake.

| Credential | Who holds it | What it opens |
|---|---|---|
| **App token** `rf_live_…` (`Authorization: Bearer`) | Your application | `POST /v1/chat/completions`, `GET /health` |
| **Admin session cookie** `rf_admin_session` | A human admin (dashboard) | `/admin/*` management, `/api/*` console (history, stats, live decision SSE) |

An app token **cannot** call `/admin/*` or `/api/*`, and an admin cookie is **not** used
for inference. See [`docs/dev-references/`](docs/dev-references/) for the full admin +
developer onboarding path and the exact HTTP contract.

## Deployment

| Target | Artifact |
|---|---|
| Single node (local / small) | `deploy/compose/docker-compose.yml` |
| Kubernetes / HPE GreenLake | `deploy/helm/serving-plane` (`values-greenlake.yaml`) |
| HPE Private Cloud AI (Ezmeral) | `deploy/hpe/` packaged chart |

See the `greenlake-deploy` skill for storage class, ingress, registry, and air-gapped
considerations.

## Configuration

Four single-owner YAMLs under `config/`, env-var overridable, cross-validated at boot by a
config lint that fails closed on any drift (duplicated concern, unpriced model, unknown
tier, provider-name mismatch):

| File | Owns |
|---|---|
| `serving.yaml` | Infrastructure switches: compression infra, observability, provider on/off, server |
| `policy.yaml` | Budgets, the sensitivity enum + default, residency, tenants |
| `routing.yaml` | Scoring weights, thresholds, the canonical `tiers` list, cloud `model_pool`, escalation |
| `pricing.yaml` | Per-model USD/1M and the savings baseline |

On-prem models are **not** declared in YAML — they're onboarded at runtime and live in the
`ModelRegistry` (cloud YAML pool ∪ enabled onboarded rows). If no on-prem model is enabled,
`onprem_only` requests are **rejected**, never silently sent to cloud.

## Context compression (Headroom)

Off by default so you can measure before/after. Enable with
`context_optimization.enabled: true`. It compresses tool outputs, logs, and history before
the LLM call; the CCR cache keeps originals **locally** for retrieval. Two rules that
matter: compression runs *after* the policy gate (never before), and the CCR cache must
stay inside the residency boundary. See the `headroom-compression` skill.

## Working with Claude Code

`CLAUDE.md` is the anchor — read it first. Skills in `.claude/skills/` carry area-specific
constraints:

| Skill | Covers |
|---|---|
| `privacy-gate` | Steps 2–3: Presidio, redaction, fail-closed, redaction map |
| `headroom-compression` | Step 5: Headroom, CCR residency, bypass rules |
| `routing-and-gateway` | Steps 6–8 (+ step-4 task mode): scoring, adapters, confidence |
| `output-controls` | Step 9: leakage checks, `require_verification`, streaming policy |
| `nemo-agent-toolkit` | Agent runtime + Langfuse export |
| `inference-backends` | What an operator runs behind an onboarded on-prem endpoint (vLLM/Dynamo/TensorRT-LLM); owns the full Dynamo skills list |
| `demo-ui` | Console: flow visualizer, decision inspector, cost dashboard |
| `traffic-and-tests` | Synthetic personas, generator, test suite (product load tooling) |
| `greenlake-deploy` | Docker, Helm, HPE GreenLake |

NVIDIA also publishes Dynamo skills — install them rather than reinventing. The full list
lives in the `inference-backends` skill (single source); read it before any Dynamo work.

## Notes

- All PII in the synthetic personas is fake (Faker). Never load real customer data into them.
- Headroom is Apache 2.0 and runs locally; no data leaves the machine.
- No secrets in code or compose. `.env` only; `.env.example` is committed.
