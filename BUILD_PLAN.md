# Build Plan

Ten phases. Verify each before starting the next. Estimated 2.5–4.5 days with Claude
Code (compression adds roughly half a day).

## Phase 1 — Skeleton (2h)
FastAPI app, config loader with env override **and the cross-file config lint**
(provider names identical, every `risk_floors`/pool tier exists in the canonical
`tiers` list, no concern owned by two files, one sensitivity enum — exit non-zero with
a named violation), ten module stubs with typed interfaces, `/health`, structured
logging with `request_id`.
**Verify:** boots; `/health` returns active backend and model list; a deliberately
drifted config (e.g. `on_prem` in one file) fails the boot with a named error.

## Phase 2 — Policy gate (4h) — *skill: privacy-gate*
Presidio analyzer/anonymizer, `ExecutionPolicyContext` (frozen), tenant auth,
residency rules, `allowed_providers` computed here, fail-closed handler.
**Verify:** SSN → detected, redacted, `onprem_only`, cloud excluded. Gate exception
→ 503 with zero model calls.

## Phase 3 — Router + mock gateway (5h) — *skill: routing-and-gateway*
Scoring with configurable weights, model pool from config, mock backend with
per-tier latency/cost, `DecisionRecord` with rejected candidates, cost accumulator
plus all-frontier baseline.
*(The mock backend named here is superseded by the LiteLLM gateway swap,
2026-07-30 — mock is removed; egress is `LiteLLMBackend` for every provider.)*
**Verify:** clean → cheap tier; PII → on-prem only; high-risk (cloud allowed) →
frontier; high-risk + `onprem_only` → best on-prem tier + `require_verification`, never
cloud.

## Phase 3b — Output controls & verification (3h) — *skill: output-controls*
Step 9 on the response path: re-run Presidio on completions (egress redaction),
consume `require_verification` from `policy.yaml`, escalate-then-fail-closed, sampling
rate, reject inbound `stream: true`. `VerificationRecord` in the decision trace.
**Verify:** PII in a completion is redacted before return; a `require_verification`
request that can't be verified fails closed (never returns the unverified completion);
`onprem_only` verification escalation stays on-prem; `stream: true` → clear rejection.

## Phase 4 — Observability (3h) — *skill: nemo-agent-toolkit*
NAT install and config, Langfuse via compose, OTel export, **redacting span
processor**.
**Verify:** traces in Langfuse; known SSN absent from every span.

## Phase 5 — Agent runtime (4h) — *skill: nemo-agent-toolkit*
NAT tool-calling agent, gateway registered as its model endpoint, 2–3 fake tools,
step and spend caps.
**Verify:** 4-hop task → 4 DecisionRecords with distinct `hop_index`; caps enforced.

## Phase 5b — Conditional Context Optimization (4h) — *skill: headroom-compression*
Headroom behind `context_optimization.enabled`, ContentRouter/compressor selection,
bypass rules (min tokens, high risk, restricted, user prompt), CCR with local-only
residency and TTL, `CompressionRecord`.
**Verify:** large tool output compresses measurably; short content bypasses with a
reason; **router prices `tokens_after`**; CCR writes stay local; nothing from CCR
reaches Langfuse.

## Phase 6 — UI (6h) — *skill: demo-ui*
Flow visualizer with SSE/WebSocket streaming, decision inspector, cost dashboard with
**routing vs compression savings split**, control panel with the Headroom toggle.
(The cost-weight slider and provider toggles were **deliberately dropped** — this router
is complexity/risk-tiered, not cost-tiered, so a cost slider wouldn't honestly shift
tiers, and per-request `rejected[]` already greys excluded providers. See
`_DEFERRED_CONTROLS` in `app/api/ui_routes.py` and the demo-ui skill's Control panel.)
**Verify:** PII request visibly greys out cloud providers; multi-step visibly loops;
compression A/B is one click.

## Phase 7 — Traffic + tests (4h) — *skill: traffic-and-tests*
Personas with weights (including `large_tool_output` overlay), async generator with
`--seed` and `--compare-compression`, pytest suite, the critical assertions.
**Verify:** 500 requests populate the dashboard; full suite green on a laptop.

## Phase 8 — Packaging (4h) — *skill: greenlake-deploy*
Multi-stage Dockerfiles, compose with healthchecks, Helm chart, GreenLake values.
**Verify:** clean-machine `docker compose up` under 5 min; `helm lint` and dry-run.

## Phase 9 — Real backends & serving paths (4h) — *skill: inference-backends*
vLLM adapter (+ Semantic Router path), Dynamo + TensorRT-LLM adapter (use NVIDIA
skills), managed-cloud path, startup validation and fallback.
**Verify:** each backend satisfies the Protocol; `none` + `onprem_only` → rejected.
*(Superseded by the LiteLLM gateway swap, 2026-07-30 — the per-backend hand-rolled
adapters above were replaced by a single `LiteLLMBackend`; startup "fallback" is gone
too, unreachable onprem is now a fail-closed `sys.exit(1)`.)*

## Cut list

If time runs short, cut in this order: Dynamo adapter → vLLM adapter → Helm →
control panel → decision inspector. **Cut Headroom only if desperate** — it's a
second, independent savings story and toggling it live demos well.

**Never cut:** the policy gate, redaction on export, CCR residency enforcement, or
the critical assertions. Those are the demo's honesty. A demo that overstates
savings or leaks data is worse than a smaller demo.
