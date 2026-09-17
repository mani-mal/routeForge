# RouteForge — Sample Request Flows (worked examples)

A companion to [`logical-and-request-flow.md`](./logical-and-request-flow.md)
and the [`diagrams/routeforge-request-flow.drawio`](diagrams/routeforge-request-flow.drawio).

That doc tells you *what* each of the ten steps is. **This doc walks real requests through
them** — three sample apps, each with a real config, so you can mentally trace how a request
moves and where each open-source library actually gets touched. It exists to answer the
questions the box diagrams don't:

1. How do **`app/compression/`, `app/cache/`, `app/memory/`** work *per call*?
2. What does an **application-driven agent loop** look like from RouteForge's side?

> **Changed 2026-09-17.** This doc used to carry a "Flow C" showing RouteForge's own
> internal NeMo Agent Toolkit runtime fanning one request into several hops, plus an
> optional LLM task-mode classifier that chose `direct` vs `multi_step`. Both were
> removed: step 4 always decides `direct`, and agent loops belong to the calling
> application. Flow C below is rewritten as the client-driven shape that replaced it.

> Cost/latency/token numbers below are **illustrative** — they show the shape of a
> `DecisionRecord`, not a benchmark. Everything else (field names, config keys, order of
> operations, defaults) is taken from the code.

---

## 0. The mental model — one spine, two branch points

Every request runs the same spine (steps 1→10) and answers with **one** model call.
There is only **one** place it branches:

- **Branch point — step 5, three optional features.** Semantic cache, persistent memory,
  and compression. **All three ship OFF** (`config/serving.yaml`). When off, they add nothing
  to the request — it passes through byte-for-byte.

Step 4 (`app/routing/task_mode.py`) always returns `"direct"` and emits no
`DecisionRecord`. RouteForge runs no agent loop of its own; an application's loop sends
each of its model calls as a separate request, so every hop of that loop re-enters this
whole spine independently — which is the point (see Flow C).

### The exact per-call order

This is the order in `Pipeline.run()` (`app/pipeline.py`). Memorize this — it's the thing the
diagrams flatten into one "Step 5" box:

| # | Stage | Module | Can it short-circuit? |
|---|-------|--------|------------------------|
| 1 | Authenticate + **sanitize** (PII scan) | `app/policy/gate.py` | Rejects on gate error (fail closed) |
| 2 | Build immutable **policy context** | `app/policy/context.py` | — |
| 3 | Decide **task mode** (constant `direct`) | `app/routing/task_mode.py` | — |
| 4 | **Memory recall** → *prepend* recalled context | `app/memory/` | No (just adds messages) |
| 5 | **Semantic cache** lookup | `app/cache/` | **Yes** — a verified hit returns immediately, skipping the model call |
| 6 | **Compression** (prices post-compression tokens) | `app/compression/` | No (fails *open*) |
| 7 | **Route + gateway** — pick model, call it | `app/routing/router.py`, `app/gateway/` | — |
| 8 | **Verify** output (leakage, `require_verification`) | `app/output/verify.py` | Rejects on failure |
| 9 | **Memory write** (success path only) | `app/memory/` | — |
| 10 | **Cache store** (on a genuine miss only) | `app/cache/` | — |

Note that **memory recall (4) runs *before* the cache check (5)** on purpose: recalled context
is then priced and compressed like any other tokens. Note also that the cache runs in
`Pipeline.run()` only — `run_stream()` deliberately skips it.

---

## 1. Three sample apps

An operator registers an app (`POST /admin/applications`), which returns a one-time plaintext
token `rf_live_…`, then sets policy separately (`PUT …/policy`, which **merges** — only the
fields you send change). Per-app policy can only ever *narrow* the global rules, never widen
them; `null` on any field means "inherit / no restriction."

### App A — `internal-chatbot` (low sensitivity, cloud allowed)

```jsonc
// POST /admin/applications
{ "id": "internal-chatbot", "name": "Internal Chatbot",
  "owner_email": "cybelink.webmaster@gmail.com", "default_residency": "any" }

// PUT /admin/applications/internal-chatbot/policy   (merge)
{ "allowed_tiers": ["cheap", "mid"],
  "allowed_providers": ["azure"],
  "max_spend_usd_per_day": 5.0,
  "max_rpm": 120,
  "pii_detection": "regex",
  "capture_io": "off",
  "stream_mode": "verified_then_stream" }
```

> `pii_detection: regex` pairs with any `capture_io`, including `"redacted"` — the regex
> detector still redacts (SSNs, cards, emails, custom recognizers), just at lower
> fidelity than `presidio` (it misses entities without a pattern, e.g. names). Only
> `pii_detection: "off"` is refused in combination with `capture_io: "redacted"`
> (`app/api/admin_routes.py`, the `eff_pii == "off"` + `eff_capture == "redacted"` clamp
> — a 422 on write). See
> [`../guides/getting-started.md`](../guides/getting-started.md)'s identical Internal-chatbot recipe.

### App B — `hospital-phi` (onprem_only, Presidio, high sensitivity)

```jsonc
// POST /admin/applications
{ "id": "hospital-phi", "name": "General Health PHI Assistant",
  "owner_email": "cybelink.webmaster@gmail.com", "default_residency": "onprem_only" }

// PUT /admin/applications/hospital-phi/policy
{ "residency": "onprem_only",
  "allowed_providers": ["onprem"],
  "allowed_tiers": ["mid", "frontier"],
  "max_spend_usd_per_day": 50.0,
  "pii_detection": "presidio",
  "capture_io": "off",
  "stream_mode": "verified_then_stream" }

// PUT /admin/applications/hospital-phi/privacy   (optional: a custom entity + rules)
{ "recognizers": [ { "name": "MRN", "regex": "\\bMRN-\\d{7}\\b", "score": 0.9, "enabled": true } ],
  "rules": [
    { "entities": ["US_SSN", "MEDICAL_LICENSE", "MRN"], "sensitivity": "restricted",
      "risk_class": "high", "residency": "onprem_only", "require_verification": true },
    { "entities": [], "sensitivity": "public",
      "risk_class": "low", "residency": "onprem_only", "require_verification": false } ] }
```

> For an `onprem_only` app in the default `enforced` posture the write-time floors reject
> `capture_io: full` and `stream_mode: stream_unverified` — those guarantees can't be waived
> per-app. `pii_detection: off|regex` **is** permitted at `onprem_only` (detection and
> residency are independent axes), but only alongside `capture_io: "off"`, so there is no
> egress path for an unsanitized prompt. An app that genuinely has nothing to protect
> declares `policy_mode: "passthrough"` instead — which unlocks `capture_io: full` into its
> **own** Langfuse project while leaving `onprem_only` absolute. See
> [`../reference/per-app-config.md`](../reference/per-app-config.md).

### App C — `research-agent` (a client-driven agent loop, cloud + frontier)

```jsonc
// POST /admin/applications
{ "id": "research-agent", "name": "Research Agent",
  "owner_email": "cybelink.webmaster@gmail.com", "default_residency": "private_cloud_ok" }

// PUT /admin/applications/research-agent/policy
{ "residency": "private_cloud_ok",
  "allowed_tiers": ["mid", "frontier"],
  "allowed_providers": ["azure", "bedrock"],
  "max_spend_usd_per_day": 25.0,
  "max_rpm": 30,
  "compression_enabled": true,
  "pii_detection": "presidio",
  "capture_io": "redacted",
  "stream_mode": "off" }
```

**Vocab recap** (valid values): providers `onprem | azure | bedrock`; tiers `cheap | mid | frontier`;
residency `any | private_cloud_ok | onprem_only`; sensitivity `public | internal | confidential | restricted`;
risk_class `low | medium | high`; `pii_detection` `off | regex | presidio`;
`stream_mode` `off | verified_then_stream | stream_unverified | incremental_verify`.

There is **no `sensitivity` field on an app** — sensitivity is computed *per request* from the
entities the gate detects (see Flow B). There are no agent-loop caps either: the loop is the
application's, so its step limit is the application's to enforce. RouteForge's per-app limits
(`max_rpm`, `max_spend_usd_per_day`, `max_cost_usd_per_request`) apply to each call the loop
makes.

---

## Flow A — a simple question (direct path, everything default-OFF)

**App:** `internal-chatbot`. **Request:**

```http
POST /v1/chat/completions
Authorization: Bearer rf_live_x7Kp…
{ "model": "auto", "messages": [ {"role":"user","content":"What is the capital of France?"} ] }
```

This is the baseline: cache/memory/compression all OFF. Trace:

| Step | What happens | DecisionRecord fields (illustrative) |
|------|--------------|--------------------------------------|
| 1 · API | `routes.py` receives the call; token → `Application` (+ policy) is resolved fail-closed. | `request_id=req_a1` |
| 2 · Gate | `pii_detection: regex` runs. No entities found. Text unchanged. | `step=policy_gate, entities=[]` |
| 3 · Context | No entity rule matched → the `[]` catch-all → **`sensitivity=public, residency=any, risk=low`**. App narrows providers to `{azure}`, tiers to `{cheap, mid}`. | `allowed_providers=[azure], allowed_tiers=[cheap,mid]` |
| 4 · Task mode | `task_mode.decide()` → **`"direct"`**, always. Emits no record. | — |
| 4a · Memory recall | `memory.enabled=false` → bypass `"disabled"`. Nothing added. | (no record) |
| 5a · Cache lookup | `semantic_cache.enabled=false` → skipped entirely. | (no record) |
| 5b · Compression | `context_optimization.enabled=false` → bypass `"disabled"`. Tokens unchanged. | `bypass_reason=disabled` |
| 6 · Route | Router scores the `{azure}` × `{cheap,mid}` candidates on ~8 tokens, `complexity≈0.004` → target tier `cheap`. `azure-gpt-mid` is the only `azure` candidate in the static cloud pool (it is **mid**-tier, not cheap — `config/routing.yaml` has no cheap-tier `azure` entry), so the router escalates and picks it. | `step=route, chosen=azure-gpt-mid, chosen_tier=mid, reason="tier mid (target cheap)", est_cost=$0.0000xx` |
| 7 · Gateway | LiteLLM calls Azure. Returns "Paris." | `actual_cost=$0.0000xx, latency_ms=180, tokens_out=3` |
| 8 · Verify | Leakage checks pass; `require_verification=false`. | `step=verify, ok=true` |
| 9 · Memory write | `memory.enabled=false` → skipped. | — |
| 10 · Response | Assemble governed response + full trace. Return `200`. Because `capture_io=off`, Langfuse gets the decision trace **with no I/O** — no raw prompt, no completion. | — |

**Takeaway:** with the three step-5 features off, "Step 5" is a no-op and the request is
essentially *gate → route → verify*. This is the default posture.

---

## Flow A′ — the SAME app with step-5 features turned ON

Now the operator flips the three flags (in `serving.yaml`, or live per-app for compression via
`POST /api/controls`):

```yaml
# config/serving.yaml
context_optimization: { enabled: true, mode: library, min_tokens: 2000, ccr: { enabled: true } }
memory:               { enabled: true, max_recall: 3, ttl_hours: 24 }
semantic_cache:       { enabled: true, ttl_hours: 24 }
```

This is the section to read if `app/compression`, `app/cache`, `app/memory` are the confusing
part. Watch three requests in one session (`x-routeforge-session: sess-42`).

### Request ①  — "Summarize this 6-page incident report: …" (first time, ~3,000 tokens)

| Stage | What the module does |
|-------|----------------------|
| **Memory recall** (`app/memory/`) | Partition key = `(tenant, application_id, session_id)`. Session `sess-42` is new → nothing to recall. |
| **Cache lookup** (`app/cache/`) | Key = the *normalized query string* (lowercase, whitespace-collapsed). Partition = `(tenant, onprem_only, allowed_providers)`. **Miss** — first time. |
| **Compression** (`app/compression/`) | `tokens_before=3000 ≥ min_tokens (2000)` and not high-risk/restricted → **runs**. `select_compressor()` sees `mode: library`, tries `HeadroomCompressor` (`from headroom import compress`). If the `[compression]` extra isn't installed, it silently falls back to the in-house `DeterministicCompressor` (collapse blank lines, dedupe repeated lines). Result: `tokens_after=2100`. The **last user message is protected** (never compressed) so the live question stays verbatim. Because `ccr.enabled=true`, the *original* pre-compression text is stashed in the **CCRStore** — a local, TTL-bounded cache of originals that **never leaves the residency boundary and is never exported to telemetry**. |
| **Route** | The router prices **`tokens_after=2100`, not 3000** — this is the whole point of running compression *before* the router. `compression_saved_usd` is booked from the 900-token drop. |
| **Gateway → Verify → Respond** | Normal. |
| **Memory write** | On success, stores a bounded summary `"user: <sanitized>\nassistant: <verified output>"` (truncated to 2000 chars) under `sess-42`. **Sanitized input + post-verify output only — never raw text.** |
| **Cache store** | On this genuine miss, stores the verified response under the normalized-query key. |

### Request ②  — the *exact same* summarize request again

| Stage | What happens |
|-------|--------------|
| Memory recall | Recalls request ①'s summary, prepends it as a `user` message. |
| **Cache lookup** | **HIT.** The normalized query matches. The cached response is **re-verified against the *current* request's detector/context** (a cached answer can't leak for the new caller). Re-verify passes → **the pipeline returns immediately** (`return hit_resp`): **no compression, no model call, no gateway.** `cache_saved_usd` = the full all-frontier baseline; routing/compression savings = 0. |

That short-circuit is why the cache sits *before* compression and the model call. A cache
hit is the cheapest possible outcome.

### Request ③  — a *follow-up* in the same session: "Now list just the action items."

| Stage | What happens |
|-------|--------------|
| **Memory recall** | Different query, so **cache misses** — but memory **recalls** the earlier summary for `sess-42` and prepends it, giving the model continuity ("the report" refers to request ①). This is what "persistent memory, per call" means: short-term, session-scoped, in-process, TTL-bounded recall — **not** mem0, not a vector DB. |
| Cache lookup | Miss (new query). |
| Compression | Runs on the combined (recalled + new) messages. |
| Route → … | Normal, then writes request ③ back into memory. |

**Three honesty rules to remember about step 5:**
- **Cache** keys on a *normalized string*, not embeddings — it's exact-match, and it
  **re-verifies every hit**. It's residency-partitioned: an `onprem_only` lookup can never
  return a cloud-produced entry.
- **Memory** stores only sanitized/verified summaries, always `to_cloud=False`, TTL-evicted.
- **Compression** fails *open* (on error, returns uncompressed text) — it must never block a
  request. CCR (the cache of originals) fails *closed* on residency: for `onprem_only`,
  originals never travel to cloud.

All three **bypass automatically** on `high_risk` or `restricted` sensitivity (configurable in
`bypass_on`), so the hospital persona below never puts sensitive originals in a cache.

---

## Flow B — a request with an SSN (privacy gate in action)

**App:** `hospital-phi`. **Request:** `"Does patient with SSN 123-45-6789 qualify for the trial?"`

> The `presidio` extra is not installed in the venv this doc was last verified from, so the
> "no healthy on-prem model → 422" mechanism below (rows 3-4) was executed with `pii_detection:
> "regex"` standing in for `"presidio"` — the mechanism under test
> (`ModelRegistry.has_healthy_onprem()` dropping `onprem` from `allowed_providers`) is
> detector-independent, and `regex` also matches `US_SSN` (`app/policy/detectors.py`). Presidio
> actually detecting `US_SSN` (row 2) is stable, well-tested third-party behavior and was not
> re-executed here.

| Step | What happens |
|------|--------------|
| 2 · Gate | `pii_detection: presidio` (Presidio + spaCy NER) detects **`US_SSN`**. The gate **redacts** it before anything downstream sees the text: `"… SSN <US_SSN> …"`. (Nothing external has seen the raw request — rule #1.) |
| 3 · Context | The detected entity `US_SSN` matches the first sensitivity rule → **`sensitivity=restricted, risk_class=high, residency=onprem_only, require_verification=true`**. The app's own residency is already `onprem_only`; `_stricter()` keeps the stricter of the two. **`onprem_only` ⇒ `allowed_providers = enabled ∩ {onprem}`** — Azure/Bedrock are *filtered out of the candidate pool now*, before the router scores anything (rule #3). |
| — | *If no healthy on-prem model is onboarded*, `has_healthy_onprem()` is false → `onprem` is dropped too → **empty candidate set → request rejected (422)**. This is the privacy guarantee: never silently fall through to cloud. |
| 4 · Task mode | No trigger phrase → `direct`. |
| 5 · Step-5 | `risk_class=high` → compression, cache, and memory **all bypass** (`bypass_on.high_risk`). Sensitive originals never enter any cache. |
| 6 · Route | Only on-prem candidates remain. `risk=high` + `onprem_only` floors to the best **allowed (on-prem)** tier — it is **never lifted to cloud** to satisfy the floor (rule #8). Picks an onboarded on-prem model, e.g. `onprem-llama-mid`. |
| 7 · Gateway | LiteLLM calls the on-prem endpoint. |
| 8 · Verify | `require_verification=true` → the stricter output check runs; leakage checks confirm no PII in the completion. |
| 10 · Response | `200`. Because `capture_io=off`, Langfuse gets the decision trace **with no I/O** — no raw prompt, no completion, no CCR contents. |

**Takeaway:** privacy is *structural*. The cloud providers are removed from the pool at step 3,
so no later bug can route this to Azure. Sensitivity is derived from detected entities, not
declared by the app.

---

## Flow C — an application's agent loop (several requests, one per hop)

**App:** `research-agent`. Its harness — LangChain, LlamaIndex, an agent framework, or
its own `while` loop — owns the tools and the control flow. RouteForge is the model
provider it points at.

```python
rf = OpenAI(base_url="http://<host>:<port>/v1", api_key="rf_live_...")
history = [{"role": "user",
            "content": "Look up Acme's latest revenue and draft a one-paragraph summary."}]
while not done:
    resp = rf.chat.completions.create(model="auto", messages=history, tools=TOOLS)
    # ... the app runs its own tools and appends the results to `history` ...
```

### What RouteForge sees

**Three requests, not one.** Each iteration of the loop is its own
`POST /v1/chat/completions`, so each one independently runs steps 1→10: its own gate
pass, its own `ExecutionPolicyContext`, its own compression, its own
`router.select()`, its own verification, its own `DecisionRecord` and its own trace.

| Loop turn | What the app sent | What RouteForge did |
|---|---|---|
| 1 | the task | gate → route → model returns a `tool_calls` turn (`finish_reason: "tool_calls"`) |
| 2 | task + assistant tool call + the app's tool results | gate re-scans the **tool output** (data the first gate pass never saw) → route → model returns another turn |
| 3 | the full history | gate → route → final answer, verified, returned |

**This is the load-bearing behaviour:** compression (5) and routing (6) are **per-call**,
so a cheap turn lands on the cheap tier and a hard turn floors higher, and a turn whose
*tool output* carries PII goes `onprem_only` on its own — even though the original task
did not. A three-turn loop makes three routing decisions and can land on three tiers.

### Consequences for the app

- **Correlation is the app's job.** Three requests means three trace ids. Send
  `x-routeforge-session: <run-id>` on every call to group them into one Langfuse Session.
- **Cost accounting is the app's job.** Each response carries its own `cost`; the task
  total is the sum the app keeps. RouteForge sums within one request, never across them.
- **Tool calls pass through.** `tools` / `tool_choice` in, `tool_calls` out. A request
  carrying `tools` is always served as one complete completion and **bypasses step-5
  compression** (Headroom would corrupt the tool_call/tool_result structure) and the
  semantic cache.
- **Never route tool results around RouteForge.** Feeding them back through the next call
  is what keeps the gate in front of them.

See [`../guides/agentic-app-guide.md`](../guides/agentic-app-guide.md) for the full
integration shape.

---

## Recap — which libraries each flow touches

| | Flow A (default) | Flow A′ (step-5 on) | Flow B (SSN) | Flow C (client loop) |
|---|---|---|---|---|
| Gate | `regex` | `regex` | **Presidio + spaCy** | Presidio, **once per turn** |
| Task mode | `direct` | `direct` | `direct` | `direct` (every turn) |
| Memory (`app/memory`) | off | **recall+write** | bypass (high-risk) | per turn |
| Cache (`app/cache`) | off | **hit/miss** | bypass (high-risk) | bypass (`tools` present) |
| Compression (`app/compression`) | off | **Headroom/deterministic + CCR** | bypass (high-risk) | bypass (`tools` present) |
| Agent runtime | — | — | — | **the application's, not RouteForge's** |
| Router + gateway | LiteLLM → Azure | LiteLLM → Azure | LiteLLM → **on-prem only** | LiteLLM, **per turn** |
| Verify | basic | basic | **`require_verification`** | per turn |
| Langfuse capture | off (no I/O) | off (no I/O) | **off (no I/O)** | redacted, one trace per turn |

Every row of every flow emits a `DecisionRecord`. The "why" in those records — which
candidates were rejected and why — is the product.

---

## See also

- [`logical-and-request-flow.md`](./logical-and-request-flow.md) — the ten steps and their libraries, with status markers.
- [`../reference/per-app-config.md`](../reference/per-app-config.md) — the `pii_detection` / `stream_mode` knobs and their write-time floors.
- [`../guides/onboarding-models.md`](../guides/onboarding-models.md) — how the on-prem pool is populated at runtime (Flow B needs a healthy on-prem model).
- [`../reference/routing-observability.md`](../reference/routing-observability.md) — `sp.candidates`, stage signals, `/metrics`.
