# Caching & Cost Feature Fit — the "four caches in LLM serving" vs. RouteForge

**Date:** 2026-08-29
**Status:** Analysis / recommendation + buildable-feature backlog (no code changes yet)
**Scope:** Map the four LLM-serving caches (KV · prefix · prompt · semantic; *Daily Dose of
DS* diagram) onto RouteForge's boundary, decide **what RouteForge should build vs. delegate**,
and fix the **cost aspect** — the caches change the real price of a call, and RouteForge's cost
math is currently blind to half of it.

All ✅/🟡/❌ statuses are confirmed against code (refs inline).

> **Boundary companion (2026-09-06):** for *"can a customer just use a prompt-caching
> library in their own application, and does that affect RouteForge?"* — and the
> app-vs-RouteForge placement test behind it — see
> [`app-vs-routeforge-boundary.md`](../architecture/decisions/app-vs-routeforge-boundary.md). It splits
> "prompt caching" into **Family A** (caches above RouteForge: invisible, fine) and
> **Family B** (provider-side `cache_control` / `prompt_cache_key` markers: they pass
> *through* RouteForge and are currently stripped — §2c below), and adds the
> `policy_mode` interaction this doc predates.

---

## 0. The one idea that sorts all four caches

**RouteForge is a router; it hosts no inference compute (rule #4).** It builds **none** of the
serving-layer cache *engines* — the serving engines (vLLM/Dynamo) and the cloud providers do.
But **routing decisions determine whether those caches hit**, and RouteForge's current
behavior — *re-select a model from scratch on every call and every agent hop*
(`app/routing/router.py:select`, `app/pipeline.py:337-346`) — is **actively hostile to the
warm caches beneath it.** A multi-turn conversation can bounce across models/backends and
**cold-miss every warm cache on every hop**, quietly erasing the savings a cheaper per-token
price was supposed to buy.

So the four caches sort by **ownership relative to RouteForge's boundary:**

| Cache | Where it lives | RouteForge's role | Build? |
|---|---|---|---|
| **KV cache** | Inside one model forward pass (prefill stores K/V → decode reuses) | None. Pure serving-engine internal. | **No** — Dynamo/vLLM own it (rule #4). *The prior decision is correct.* |
| **Prefix cache** | Serving engine (shared prefix → reused KV blocks across requests) | Doesn't implement it, but **routing decides if it hits** | No engine — but stop cold-missing it via affinity |
| **Prompt cache** | Provider-side, **billed** (Anthropic/OpenAI/Gemini cached input ≈ 10% of input price) | Must **cooperate** to earn hits + **account** for the discount | No store — but cooperate + fix the cost math |
| **Semantic cache** | Above the model, provider-agnostic | **RouteForge owns it — already shipped** | Already built (see §1) |

The through-line for the whole doc: **RouteForge doesn't build cache engines — it builds the
routing and accounting that lets the caches beneath it pay off, and it counts the savings
honestly.**

---

## 1. What RouteForge already has

### Semantic cache ✅ (narrower than the name — an exact-match response cache)

`app/cache/store.py:DeterministicSemanticCache` — a local, TTL-bounded, in-memory **response**
cache. Honest naming caveat: matching is **exact normalized-query** (`_normalize`:
lowercase/strip/collapse-whitespace), **not** embedding similarity — the `embedding: "off"`
seam (`SemanticCacheBackend` Protocol) is deferred. Strong points already in place:

- **Residency-partitioned by construction:** partition key is
  `(tenant_id, onprem_only, frozenset(allowed_providers))` (`store.py:_partition`). It
  **refuses cloud→onprem_only** on both write (`_is_residency_consistent`) and read (re-check in
  `lookup`, increments `blocked_cloud_entries`). This is the discipline every other cache
  feature below must reuse.
- **Wired in** on the direct path (`pipeline.py:355-392` check, `421-438` store); hits
  **re-verify** through `_serve_from_cache` before serving. **Off by default**
  (`config/serving.yaml semantic_cache.enabled: false`); the multi-step/stream paths don't
  consult it.

### Savings already tracked as separate levers ✅

RouteForge already refuses to lump savings together (the CLAUDE.md discipline). Three levers,
separated in `pipeline.py:440-490`:

1. `compression_saved_usd` — Headroom (step 5)
2. `routing_saved_usd` — cheap tier vs. the `bedrock-frontier` "all-frontier" baseline
   (`pipeline.py:_baseline`, `config/pricing.yaml baseline_model`)
3. `cache_saved_usd` — the **response cache avoiding the call entirely**
   (`pipeline.py:_serve_from_cache`: on a hit `total_usd=0`, full baseline booked as cache
   savings)

**This is the frame to extend.** The diagram's *prompt cache* is a **fourth** lever RouteForge
does not yet see — and its absence makes lever #2 subtly wrong (below).

---

## 2. The cost aspect — the gap that actually costs money

### 2a. RouteForge is blind to provider prompt-cache discounts ❌ *(the core finding)*

A provider prompt-cache hit bills cached input at **≈10%** of the normal input price. RouteForge
never sees it:

- **No cached-input price exists.** `PriceCfg` (`app/config/schema.py`) has only
  `input`/`output`/`tier`/`provider` — one input price per model. `config/pricing.yaml` rows
  likewise.
- **Usage parsing ignores cache fields.** `litellm_backend.py` reads only
  `prompt_tokens`/`completion_tokens`; the `cache_read_input_tokens` / `cached_tokens` the
  provider returns are **dropped**.
- **Consequence — RouteForge's own cost numbers *overstate* real spend.** When a provider serves
  a cached-input hit and charges 10%, RouteForge still bills the app (and the dashboard) at
  **100%** of input price (`litellm_backend.py` cost calc). Every "cost" and "savings" figure
  drifts from the invoice, invisibly — exactly the pitfall the CLAUDE.md warns about ("every
  cost figure becomes wrong… invisible until someone checks the math").
- **The router can't reason about it either.** `router.py:_est_cost = tokens/1e6 · (input+output)`
  prices every call as a cold call, so it can't prefer a route that would land a cheap cached
  hit.

### 2b. Nothing keeps a session on a cache-friendly route ❌

No model/backend affinity. `session_id` (from `x-routeforge-session`) reaches only the
memory-recall partition and the Langfuse trace attribute — it is **never** threaded into
`Router.select`, `Gateway.run`, or `_est_cost`. So even when a provider *would* cache, RouteForge
happily re-routes the next turn to a different model and throws the warm prefix/prompt cache away.

### 2c. Nothing earns provider prompt-cache hits ❌

The gateway forwards plain `{"role","content"}` dicts — **no** Anthropic `cache_control`
breakpoints, **no** `prompt_cache_key`, no stable-prefix construction, and `drop_params: True`
would strip such params anyway.

**Amendment 2026-09-06 — `policy_mode` decides whether a stable prefix is even possible.**
`policy_mode` (`enforced | passthrough`) shipped after this doc was written. Provider
prompt caching needs a **byte-stable prefix**, and only `passthrough` guarantees one: it
composes the null detector (`app/pipeline.py:190-214`), so the payload is unmodified.
Under `enforced`, redaction rewrites message text whenever entities are found and Headroom
rewrites it again — so an app's cache hit rate becomes **data-dependent and silently
variable** rather than cleanly absent. F3 must say which postures it can honestly promise a
hit under. See [`app-vs-routeforge-boundary.md`](../architecture/decisions/app-vs-routeforge-boundary.md) §6c.

### 2d. No downstream KV hint ❌

`LiteLLMBackend.generate` gets no session/prefix/`cache_salt` — so a **KV-aware downstream router
(Dynamo/vLLM)** receives nothing to route the request to the worker already holding its KV
blocks. RouteForge doesn't manage KV (correctly), but it currently withholds the one cheap hint
that would let the layer that *does* manage KV do its job.

---

## 3. Buildable features — ranked

Legend: all live in the **routing / gateway / accounting** plane — **none hosts cache compute**
(rule #4 intact). There's a dependency spine: **F1 unlocks correct F2/F3.**

| Rank | Feature | Cache served | Marketability / value | Fit | Effort | Depends on |
|---|---|---|---|---|---|---|
| **1** | **Cached-token accounting + cached-input pricing** | Prompt | **Correctness** (invoice-accurate cost) + a 4th savings lever | High | **Low–Med** | — |
| **2** | **Cache-aware routing (model/backend affinity) + switch-cost budget** | Prefix + Prompt | **Highest cost ROI** | High | Med | F1 |
| **3** | **Provider prompt-cache enablement (`cache_control` / `prompt_cache_key`)** | Prompt | Real per-turn $ once sessions stick | High | Med | F2 |
| **4** | **Downstream KV hint to Dynamo/vLLM** | KV (cooperate) | Dynamo synergy, cheap | High | **Low** | — |
| **5** | **True semantic (embedding) response cache** | Semantic | Catches paraphrases the exact-match cache misses | Med | Med | — |

### F1 — Cached-token accounting + cached-input pricing *(do first)*

Add an optional `cached_input` rate to `PriceCfg`/`pricing.yaml`; read
`cache_read_input_tokens`/`cached_tokens` from provider usage in `litellm_backend.py`; bill and
report cached tokens at the cached rate; add a **fourth savings lever `prompt_cache_saved_usd`**
alongside routing/compression/response-cache. **Why first:** it's the correctness fix (cost stops
lying), it's low-risk (accounting only, no routing change), and F2/F3 can't make a correct
switch-vs-stay decision until the router can price a cached hit.

### F2 — Cache-aware routing: model/backend affinity + switch-cost budget *(highest ROI)*

Thread `session_id` into the router; within a session **prefer the previously-chosen model when
it's still eligible after the residency + budget filters** (a tie-break/bias, **never** an
override — `onprem_only` still filters cloud out first, rule #3); add a **switch-cost budget**
(per-app config, default ~20%): only switch to a "better" model if the score gain outweighs the
estimated cache-rebuild cost (priced using F1). **Residency-partition affinity exactly like the
response cache already does** (`store.py:_partition`) so it can't leak across the residency
boundary. Emit the reasoning in the `DecisionRecord` ("stayed on model X for cache affinity;
switch cost 34% > budget 20%") — the *why* stays auditable.

> **The tradeoff to name explicitly:** affinity works *against* routing savings — pinning a
> session to keep caches warm can mean not dropping to a cheaper tier. The switch-cost budget is
> the arbitration, and it's only correct once F1 lets RouteForge price the cached hit. This is
> the exact same "advisory bias, residency-clamped, logged" pattern as `stage_signals` — see
> `docs/architecture/external-router-feature-analysis.md` §3.1 (F2/F3), which this converges
> with: the caching diagram and the Plano/Devin-Fusion comparison point at **one** build.

### F3 — Provider prompt-cache enablement

Once F2 keeps a session on one model, make the gateway adapter **earn** the provider hit: emit
Anthropic `cache_control` breakpoints on the stable prefix (system prompt, tool defs, pinned
context), set `prompt_cache_key` where supported, stop dropping cache params. Only pays off with
F2 in place — otherwise the next turn re-routes and the cache is cold anyway.

### F4 — Downstream KV hint *(cheap, respects the boundary)*

Pass a stable session/prefix key (`cache_salt` / session header) **downstream** so a Dynamo/vLLM
KV-aware router can route to the worker holding the KV blocks. RouteForge builds no KV cache and
manages no placement — it just **stops discarding the hint** the serving layer needs. Low effort,
direct Dynamo synergy (see the `inference-backends` skill for what runs behind the endpoint).

### F5 — True semantic (embedding) response cache *(the one cache RouteForge owns)*

Activate the deferred `embedding` seam so the response cache matches **paraphrases**, not just
exact strings. Constraints: keep the residency partition; **the embedding call is itself a routed
call** — it must run **post-gate** (rule #1) and on-prem for `onprem_only` (rule #3), never
phone-home. Lower priority than the cost-correctness items, but it's genuinely RouteForge's to
own because it sits *above* the model and is provider-agnostic.

---

## 4. Explicitly delegate / do NOT build

- **KV cache** — inside the forward pass; Dynamo/vLLM/TensorRT-LLM own it. Rule #4. **The prior
  "KV cache is part of Dynamo" decision is correct** — reaffirmed. RouteForge's only KV move is
  the *hint* in F4.
- **Prefix cache engine** — the serving engine's automatic prefix caching owns it. RouteForge's
  contribution is **making it hit** (F2), not implementing it.
- **Prompt cache store** — the provider owns the store and the billing. RouteForge **cooperates**
  (F3) and **accounts** (F1); it does not cache provider-side.
- **A second response cache** — one exists (`app/cache/store.py`); extend it (F5), don't add
  another.

---

## 5. Bottom line

Of the four caches, RouteForge legitimately **owns one** (the response/"semantic" cache, already
shipped and residency-safe) and correctly **delegates the KV cache to Dynamo**. The value it's
leaving on the table is entirely in the **middle two** — prefix and prompt caches it doesn't own
but whose hit-rate its routing controls — plus a **cost-reporting correctness bug**: it bills
provider-cached input at full price and can't see the discount.

Recommended order: **F1 (cached-token accounting — correctness first) → F2 (cache-aware affinity +
switch-cost budget — the ROI) → F3 (earn the provider hit) → F4 (KV hint, cheap) → F5 (embedding
cache, later).** F1→F2→F3 is one coherent thread; F4 and F5 are independent. All of it stays inside
the router/gateway/accounting plane — RouteForge builds no cache compute, and every cache-aware
decision stays residency-clamped and logged in the `DecisionRecord`.

---

*Related: `docs/architecture/external-router-feature-analysis.md` §3.1 (F2/F3 cache-aware routing
+ switch-cost budget — same build, reached from the routing angle), `docs/routing-observability.md`
(the `DecisionRecord` these decisions must log), the `inference-backends` skill (what runs behind
an onboarded endpoint — where KV/prefix caches actually live), `docs/onboarding-models.md`.*
