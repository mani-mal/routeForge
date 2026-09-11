# What belongs in the calling application vs. RouteForge

**Date:** 2026-09-06
**Status:** Framing / decision doc. No code changes. Describes shipped behaviour as of
`api:0.24.0`, with the gaps named as backlog.
**Answers the recurring question:** *"If an application wants to cut its own token cost —
a client-side compressor like [rtk](https://github.com/rtk-ai/rtk), its own answer cache,
a prompt-caching library — can it? Does that affect RouteForge? Isn't RouteForge just a
cost-cutting router?"*

**Siblings:** [`compression-in-scope.md`](./compression-in-scope.md) (why Headroom is in,
the long form) · [`caching-cost-feature-fit.md`](../../comparisons/caching-cost-feature-fit.md) (the four
caches + the F1–F5 cost backlog) · [`semantic-cache-residency-spec.md`](../specs/semantic-cache-residency-spec.md)
(the cache's binding contract) · [`inference-layer-boundary.md`](./inference-layer-boundary.md)
(the other boundary — where RouteForge stops and serving begins).

---

## 0. Two questions that keep getting conflated

Almost every round of this confusion comes from mixing these up:

1. **Does the capability belong *inside* RouteForge?** — a boundary question.
2. **Is it a reason a customer *buys* RouteForge?** — a positioning question.

Headroom and the semantic cache answer **yes** to (1) and **no** to (2). That is not a
contradiction, and noticing it is not a sign either feature is misplaced. Some things ship
in the box because they can only be done *safely* inside the boundary, while still not
being the pitch.

## 1. The placement test

> **You cannot move a boundary-enforcing component outside the boundary it enforces.**

That line is the load-bearing argument in `compression-in-scope.md`; it generalises to
every app-vs-RouteForge question. The operational form, for a capability X:

- Does X need the **policy context** (tenant, residency, sensitivity, allowed providers)
  to be *correct or safe*? → RouteForge.
- Does X **produce or consume the audit record** (DecisionRecord, meter, trace)? →
  RouteForge.
- Does X only affect the app's **own experience of its own data**? → the app. Do not
  absorb it.

RouteForge's moat is being the **only place that sees every call with policy attached**.
Every candidate feature gets asked: *does this need the chokepoint, or is it merely a
feature that happens to live at one?*

## 2. The three-bucket sort

| Bucket | What's in it | Sell it? |
|---|---|---|
| **Chokepoint — only RouteForge can do it** | Policy gate (step 2), residency-by-construction (rule 3), per-app policy, metering, `DecisionRecord`, step-9 verification | **Yes. This is the product.** |
| **In-boundary levers — in RouteForge *because of* the boundary and the agent loop** | Headroom compression (step 5), `DeterministicSemanticCache` | Demo them. Both default-OFF. Never the pitch. |
| **The app's own business** | rtk-style client compressors, the app's own agent harness, the app's own answer cache | **Never absorb.** |

## 3. Client-side token reduction (rtk and friends) — the app's business

An app may run rtk or any prompt-shrinking proxy before it calls RouteForge. RouteForge
sees a smaller request and prices what actually arrives. Nothing breaks.

Two consequences worth stating plainly:

- It runs **before the gate, on the app's side of the wire**. Fine — the app owns its own
  data — but its savings are invisible to RouteForge's accounting. They appear as "this
  app just sends fewer tokens", not as a tracked lever.
- RouteForge's in-boundary analogue is **Headroom (step 5)**, which compresses *after* the
  gate and is tracked separately as `compression_saved_usd`. The two stack; they do not
  know about each other.

## 4. Headroom — in RouteForge (settled 2026-08-20)

Full argument in [`compression-in-scope.md`](./compression-in-scope.md). Short form: CCR
caches *originals* under a residency boundary and a TTL (rule 6) and must only see
post-gate content (rule 7) — you cannot move that outside the boundary that enforces it;
on the **multi-step path there is no app-owned harness to hand it to**, because the tool
outputs and history are produced by RouteForge's own NAT runtime; and the router must
price **post-compression** tokens or the two-levers cost story is unmeasurable.

Where the "it could live in the app" instinct is right: on the **direct single-shot path**,
an app that pre-summarises its own history is doing the same job. That overlap is fine
precisely because Headroom is default-OFF and conditional.

## 5. `DeterministicSemanticCache` — in RouteForge, but a lever, not infrastructure

**Why it's in:** a cache hit is a routing decision in disguise. Serving a cached answer
that a *cloud* model produced to an `onprem_only` request is a rule-3 breach exactly as
real as routing the live call to cloud. Hence the partition key
`(tenant_id, onprem_only, allowed_providers)`, the per-entry producing-provider record,
refusal on both write and read, and step-9 re-verification on a hit. An app-side cache has
none of that. Binding contract: [`semantic-cache-residency-spec.md`](../specs/semantic-cache-residency-spec.md).

**Why it is *not* a selling point** — and the honest, concrete reason:

- It is a **per-process in-memory dict** (`app/cache/store.py:101`), TTL-bounded,
  **exact normalized-query match** (not embedding similarity — that seam is deferred),
  **off by default**, and consulted on the **direct path only**.
- With `replicaCount.api = N` behind the Service, hit rate degrades toward **1/N**. See
  [`api-load-balancing.md`](../api-load-balancing.md) — per-pod in-memory state is a known
  bug class here, not a design goal.

A customer who wants serious caching wants Redis or a vector store, and that is theirs to
run. RouteForge's cache exists to (a) prove the residency discipline generalises to a
second copy of an answer and (b) hand the operator a free lever. **Do not market it as a
caching product.**

## 6. The prompt-caching landscape — two families, only one is invisible

"Prompt caching" (e.g. the [`prompt-caching` GitHub topic](https://github.com/topics/prompt-caching))
covers two mechanisms that land on opposite sides of the boundary. Conflating them is the
single easiest way to get this wrong.

| | **Family A — caches above RouteForge** | **Family B — provider-side prompt caching** |
|---|---|---|
| Examples | GPTCache, LangChain LLM cache, any app-side answer/semantic cache | Anthropic `cache_control` breakpoints, OpenAI `prompt_cache_key`, Gemini context caching |
| What it is | The app answers without calling RouteForge | **Markers inside the request** the app sends *through* RouteForge |
| Passes through RouteForge? | **No** — the call never happens | **Yes, unavoidably.** RouteForge holds the wire format |
| Affects RouteForge? | No, technically | **Yes** — see below |

### 6a. Family A — yes, fine, and genuinely none of RouteForge's business

The only cost is governance, and it should be a knowing choice: **a call the app serves
from its own cache produces no DecisionRecord, no meter entry, no trace, no policy
verdict, no step-9 verification.** If the customer's reason for deploying RouteForge is
"every LLM interaction is accounted for", an app-side cache punches a hole in exactly that.

### 6b. Family B — the app can set it, and RouteForge currently drops it

Verified against `api:0.24.0`:

1. **The markers are stripped, for two different reasons.** A per-message `cache_control`
   block never survives request parsing at all: `Message` (`app/models.py`) declares no
   such field and silently drops unknown ones, so `_to_wire()`
   (`app/gateway/backends/litellm_backend.py:34`, used at `:97` and `:149`) — which
   serialises each message to `{role, content}` plus tool-calling fields only — never sees
   it either way. A top-level `prompt_cache_key` survives one step further: `ChatRequest`
   allows extra fields, so it lands in `request.model_extra` — but nothing downstream
   (`Pipeline.run`, the gateway call construction) ever reads `model_extra`, so it is never
   added to the `call` dict passed to `acompletion()`. `drop_params: True`
   (`litellm_backend.py:100`, `:153`) never gets the chance to act on it — the param is
   simply never forwarded, not dropped by that flag. Verified 2026-09-10:
   `Message(role=..., cache_control=...).model_dump()` omits the field, and
   `ChatRequest(..., prompt_cache_key=...).model_extra == {"prompt_cache_key": ...}` while
   grep confirms `model_extra` is read nowhere in `app/`. The app sets them; the provider
   never sees them.
2. **Even if they survived, nothing keeps a session on one route.** RouteForge re-selects a
   model from scratch on every call and every agent hop, so turn 2 can land elsewhere and
   cold-miss the prefix the app just paid to warm (`caching-cost-feature-fit.md` §2b).
3. **The cost math would still lie.** `PriceCfg` has no cached-input rate and the backend
   never reads `cache_read_input_tokens` / `cached_tokens`, so a real provider cache hit is
   still billed at 100% of input price — the invoice comes in under RouteForge's dashboard
   (§2a; backlog F1).

### 6c. `policy_mode` × byte-stable prefixes *(new since `caching-cost-feature-fit.md`; `policy_mode` shipped 2026-09-05)*

Provider prompt caching needs a **byte-stable prefix**. Whether RouteForge preserves one
depends on the app's **protection** posture — not its placement:

- **`policy_mode: passthrough`** → the gate composes the **null detector**
  (`app/pipeline.py:190-214`): nothing is detected, so nothing is redacted at intake and
  nothing on egress. The payload is passed through unmodified and a stable prefix holds.
- **`policy_mode: enforced`** → redaction rewrites message text whenever entities are
  found, and Headroom (if enabled) rewrites it again. A stable prefix is **not
  guaranteed**. It happens to survive a request where nothing is detected and compression
  is off — which is worse than a clean failure, because the app's cache hit rate becomes
  data-dependent and silently variable.

**This is a real reason a customer might choose `passthrough` that has nothing to do with
residency** — and a clean illustration of rule 2a: placement and protection are separate
axes. `onprem_only` + `passthrough` remains valid and honest.

## 7. The principle, and what would change it

> **RouteForge should not own a cache, but it must be a faithful conduit for the caches on
> either side of it.**

Today it is faithful in Family A and **lossy in Family B**. Not absorbing the customer's
cache is correct; silently deleting the customer's caching directives is a different thing
and is not correct.

The fix is already ranked in [`caching-cost-feature-fit.md`](../../comparisons/caching-cost-feature-fit.md)
§3 — **F1** count cached tokens and price them honestly → **F2** session affinity with a
switch-cost budget so a warm prefix survives the next turn → **F3** pass through / set the
provider markers. None of them make RouteForge *host* a cache; they make it stop breaking
the one the provider already runs. Rule 4 intact throughout.

## 8. The customer-facing sentence

> RouteForge doesn't make your calls cheaper by being clever with your prompts — do that
> yourself if you like. It makes them cheaper by sending most of them somewhere cheap, and
> it's the one place that can prove where every call went and that nothing sensitive left
> the building.

Cost is one of three guarantees (privacy, cost, observability), and it is the most legible
but not the load-bearing one. Stripped to "cheap router", RouteForge describes something a
customer could replace with bare LiteLLM. The reason to deploy it on-prem is that it is the
single place where *where the data may go* is decided and recorded.
