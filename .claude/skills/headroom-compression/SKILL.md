---
name: headroom-compression
description: Use when implementing step 5 Conditional Context Optimization - Headroom token compression of tool outputs, logs, JSON, and long history before the LLM call. Trigger on mentions of Headroom, compression, CCR, token reduction, context optimization, cache alignment, or prefix caching.
---

# Conditional Context Optimization — Headroom (Step 5)

Headroom (`headroomlabs-ai/headroom`, Apache 2.0) compresses everything the model
reads — tool outputs, logs, RAG chunks, files, long history — before it reaches the
LLM. Reported 60–95% token reduction on agent workloads, with accuracy held on GSM8K
and TruthfulQA. It runs **locally**; data does not leave the machine.

Enabled at startup via `context_optimization.enabled`. **Default `false`** for the
demo baseline — you want to show the before/after.

## Where it sits — and why the position is load-bearing

Step 5 runs **after** the policy gate (2–3) and task mode (4), and **before** the
router (6). Two consequences that drive the whole design:

1. **It only ever sees already-sanitized content.** The gate has run. Never move
   compression earlier — Headroom would then see raw PII, and CCR would cache it.
2. **The router prices post-compression tokens.** Compression changes the token count
   the router scores on. If the router uses pre-compression counts, every cost number
   in the demo is wrong. Pass `post_compression_tokens` into scoring explicitly.

**The 5↔6 tokenizer cycle — resolve it here.** Headroom compresses "for a target
model," but the target isn't chosen until the router (6) runs, and the router must
score post-compression counts (5). Taken literally that's a cycle. Break it: compress
**once against a canonical tokenizer** (cl100k / the cheap tier's), let the router
treat cost as an estimate, and record the actual count from the response into
`actual_cost_usd`. Do **not** loop compression↔routing for marginal per-model token
accuracy — per-model recompression is a documented approximation, not a bug.

## CCR and residency — the one real risk

CCR (Conditional Context Retrieval) keeps **originals cached locally** so the model
can retrieve them on demand. That cache is a new copy of potentially sensitive data.

Hard rules:

- The CCR store **must** live inside the approved residency boundary. In K8s, a PVC
  on in-cluster storage — never an external object store, never a cloud bucket.
- For `onprem_only` requests, CCR retrieval must be served locally or not at all.
- Set a TTL and enforce it. Originals are retrievable *within the configured TTL*;
  an unbounded cache is an unbounded data-retention liability.
- CCR contents are **never** exported to telemetry.

If residency cannot be guaranteed for a tenant, run compression with CCR disabled
(one-way) or bypass compression entirely for that tenant. Losing some token savings
is acceptable; leaking residency is not.

**CCR retrieval into cloud prompts — the flow to be honest about.** When compression
has summarized content and a **cloud** model later retrieves the original via CCR, that
original (sanitized, but full) is inserted into a cloud prompt. That is within policy
for `private_cloud_ok` tenants, but it is **forbidden** for `onprem_only` (the CCR
store is local and cloud models are already filtered out for those requests). Count it:
emit `ccr_retrievals_to_cloud` so the honest answer to "do the originals ever go to the
cloud?" is a number, not "mostly." For `onprem_only` this counter must always be zero —
assert it.

## Bypass rules — when NOT to compress

Compression is conditional. Skip it when:

- Content is already short (below `min_tokens`, default ~2000) — overhead exceeds gain
- `risk_class: high` — don't compress input to a high-stakes decision
- `sensitivity: restricted` unless CCR residency is verified for that tenant
- The content is the user's own prompt (compress tool outputs and history, not the
  live question) — the demo's `pass_through_short_or_high_risk` rule

Always log the bypass reason into the `DecisionRecord`. "Why didn't it compress?" is
a question customers ask.

## Integration

Two supported modes; pick per deployment.

**Library (default for the prototype)** — in-process, simplest, no extra container:

```python
from headroom import compress
result = compress(messages, model=target_model)
```

**Sidecar proxy** — `headroom proxy --port 8787`, OpenAI-compatible, zero code
changes. Use in K8s as a per-pod sidecar when you want compression shared across
processes or language-independent.

Headroom also ships a LiteLLM callback and LangChain/Agno adapters. Prefer the
library call here — explicit is better for a demo where you must show the numbers.

Install: `pip install "headroom-ai[all]"`, Python 3.10+. Pin the version.

## Cache alignment

CacheAligner stabilizes prompt prefixes so provider KV caches actually hit. This
compounds with routing savings and is worth surfacing separately in the UI — cache
hit rate is a metric customers recognize.

## What the DecisionRecord must carry

```python
class CompressionRecord(BaseModel):
    enabled: bool
    applied: bool
    bypass_reason: str | None      # "below_min_tokens" | "high_risk" | ...
    tokens_before: int
    tokens_after: int
    reduction_pct: float
    compressor: str | None         # SmartCrusher | CodeCompressor | Kompress-v2
    ccr_stored: bool
    ccr_retrievals: int
    ccr_retrievals_to_cloud: int   # must be 0 for onprem_only — the honesty counter
    latency_ms: float
```

`tokens_before` / `tokens_after` is the headline. The UI shows it per request and
aggregated; the cost dashboard needs both to separate compression savings from
routing savings.

## Air-gapped / offline

Headroom fetches two assets over TLS: the ONNX runtime and the Kompress model from
HuggingFace. In an air-gapped GreenLake install, pre-download the model and set
`HF_HUB_OFFLINE=1`, or bake it into the image. Running with compression disabled
needs neither asset — that's the safe fallback if the download is blocked.

Behind corporate SSL inspection, `HEADROOM_TLS_STRICT=0` narrows only the strict
X.509 flag; chain, expiry, and hostname checks stay on.

## Tests

- Compression off → `applied: false`, token counts unchanged, baseline intact
- Large tool output → measurable reduction, `tokens_after < tokens_before`
- Short content → bypassed with `below_min_tokens`
- `risk_class: high` → bypassed, reason recorded
- **Router scores on `tokens_after`**, not `tokens_before`
- `onprem_only` → CCR store path is local; assert no external write
- `onprem_only` → `ccr_retrievals_to_cloud == 0` (no original ever enters a cloud prompt)
- CCR contents absent from every exported Langfuse span
- Compression failure → falls back to uncompressed, request still succeeds
