# Semantic-cache residency + correctness spec

Feature #6 (semantic cache), Task 1 design gate. This is the binding contract for the
CCR-class semantic-cache substrate built in `app/cache/store.py`. Task 2 wires the
check/store calls into the pipeline; this doc is what that wiring must honor.

**Status:** Task-2 pipeline wiring **shipped** (commit `a7f19da`); the feature **remains
default OFF** (`semantic_cache.enabled: false`, `config/serving.yaml:59`). `app/main.py`
constructs the `DeterministicSemanticCache` and hands it to the pipeline, which does a
cache CHECK/STORE on the direct path (`app/pipeline.py`: `_serve_from_cache`,
`self._cache.lookup`, `self._cache.store`). With the flag off, serving behavior is
byte-for-byte unchanged; the caller contracts below (C1, C4) are now implemented at those
call sites.

## Why this needs a spec before code

A cache hit skips the model entirely. That is exactly the kind of shortcut that breaks
this product's core guarantees if it isn't residency- and correctness-aware from the
start: a cached response is a **second copy of a previously-generated answer**, and if
that answer was produced by a cloud model, serving it back to an `onprem_only` request
is a Rule-3 breach as real as routing the live call to the cloud would be. The cache
must therefore be CCR-class (CLAUDE.md rule 6 — local, TTL-enforced, never exported) AND
additionally residency-partitioned in a way the CCR store never needed, because CCR
never crosses tenants or providers — it just retrieves an opaque original back to the
same request that stored it.

## C1 — sanitized-only

The cache stores **only** the sanitized query (as its lookup key) and the post-verify,
redacted response text (as its value). It never stores raw input, `result.text`, or
`raw_completion`. This is a **caller contract**, not something the store itself can
enforce — the store has no way to know if a string it's handed is sanitized. Task 2
must only ever call `store()` with the same sanitized query used to drive the pipeline,
and the same redacted text that step 9 verification passed and step 10 returned to the
client. See CLAUDE.md rule 7 (compression runs after the gate; the same ordering
argument applies here — the cache must only ever see sanitized content).

**Enforcement point:** Task 2 call sites (caller contract). Not enforceable inside the
store.

## C2 — tenant isolation

The cache key is **partitioned** by `tenant_id`; a lookup for tenant B can never return
tenant A's entry, even for an identical query string. `tenant_id` is mandatory —
`DeterministicSemanticCache` raises on a falsy `tenant_id` rather than silently
defaulting to a shared partition.

**Enforcement point:** structural — `tenant_id` is the first element of the partition
tuple every `store`/`lookup` call computes; there is no code path that looks up across
partitions.

## C3 — residency partition (load-bearing, design gate)

This is the guarantee this task exists to get right. The partition includes not just
`tenant_id` but `onprem_only` and the `allowed_providers` set, **and** every stored
entry self-describes the provider that produced it (`CacheEntry.provider`). The rule:

> An `onprem_only` lookup must NEVER return an entry whose producing provider is not in
> the local set (`_LOCAL_PROVIDERS = {"onprem", "mock", "?", "-", ""}`, imported from
> `app/api/history.py` — the same set the request-history honesty counter uses).

This is enforced **structurally**, in two places, not as a post-hoc filter bolted onto
a generic cache:

1. **Write side:** `store()` refuses to persist an entry at all when
   `onprem_only=True` and `provider` is not local. A residency-inconsistent entry
   can never enter an `onprem_only` partition through the public API.
2. **Read side:** `lookup()` independently re-checks the same rule against the
   entry it is about to return, before counting it as a hit. This is defense in
   depth: even if an entry somehow ended up in an `onprem_only` partition (a
   future bug, a manual `_data` mutation, a different backend implementation),
   `lookup` still refuses to serve it. The check lives inside the method that
   returns data to the caller — it is not a wrapper the caller could bypass.

The honesty counter `blocked_cloud_entries` mirrors CCR's `retrievals_to_cloud`: it
exists for parity and observability, and the rule above makes it structurally
unreachable via the public API, so it stays 0 for `onprem_only` traffic. If it is ever
nonzero, that is itself evidence of a Rule-3 breach worth alarming on.

**Enforcement point:** `DeterministicSemanticCache.lookup` (structural, load-bearing) +
`DeterministicSemanticCache.store` (defense in depth on write).

## C4 — verification carryover (design gate)

A cache hit skips the model call, but it must **never** skip verification. Task 2 MUST
re-run `verify(det, ctx, cached_text)` with the **current** request's per-app detector
before returning a cached response — this is free (no model call) but not optional.

Why: the store persists text that was verified once, against the detector config in
effect *at store time*. Between then and a later hit, the per-app policy may have
changed (a tenant's `pii_detection` mode, `require_verification`, or detector rules),
or the store's entry may have come from a different tenant/app-config context within
the same partition. Re-verifying against the *current* request's detector is what
makes serving the cached text correct — the store's job is only to keep verified text
around; the pipeline's job is to prove it is still verifiable under today's rules
before it leaves the process.

Backend scope: **deterministic exact/normalized-query match is the only backend built
now.** A fuzzy/embedding cache is deliberately deferred (see C8) — a paraphrase match
is a different query from the one that was verified, and "the cached answer addresses
a semantically similar question" does not imply "the cached answer's verification
carries over to the new phrasing." That needs a local embedder and its own re-
verification story; it is out of scope until a concrete need justifies the air-gap
cost of shipping an embedding model.

**Enforcement point:** Task 2 pipeline wiring (caller contract, spec'd here so Task 2
cannot skip it by omission).

## C5 — enforced TTL

Mirrors CCR (`app/compression/ccr.py::CCRStore`): sweep-on-write + evict-on-read.
`store()` opportunistically sweeps every expired entry across all partitions before
writing, so the cache stays bounded even in the steady state where nothing ever reads
(the same argument CCR's docstring makes — without this, "enforced TTL" would be a
claim the code doesn't back). `lookup()` evicts a stale entry it encounters rather than
returning it. Config: `ttl_hours > 0` is enforced by the boot lint (`ttl_hours <= 0` is
a `ConfigLintError`) and by `residency: local_only`.

**Enforcement point:** `DeterministicSemanticCache._sweep_expired` (write path) +
`DeterministicSemanticCache.lookup` (read path) + `app/config/lint.py::enforce`.

## C6 — never exported

Cache contents (`response_text`, `model`, `provider`) live only in-process, for the
process lifetime bounded by TTL. Only a **counts-only** `CacheRecord`
(`app/models.py`) reaches any trace or telemetry sink — `hit`, `tokens_saved`,
`cache_saved_usd`, `entries_size`, `blocked_cloud_entries`, `latency_ms`, and
`bypass_reason`. It has no query or response field, matching the
`CompressionRecord` precedent (which carries token counts, never CCR contents).
`semantic_cache.export_to_telemetry` must be `false`; the boot lint rejects `true`
exactly like `context_optimization.ccr.export_to_telemetry`.

**Enforcement point:** `CacheRecord`'s field set (structural — there is no content
field to accidentally export) + `app/config/lint.py::enforce`. If a content field is
ever added to `CacheRecord`, it MUST also be added to `app/obs/otel.py::REDACT_KEYS`.

## C7 — default OFF / one owner

`semantic_cache:` is a single block in `config/serving.yaml`, mirroring
`context_optimization`'s placement (an infrastructure switch, not a policy/routing/
pricing concern — no other YAML file may restate `enabled`, `ttl_hours`, `residency`,
or `export_to_telemetry`, or the cross-file config lint's one-owner rule catches it the
same way it catches every other duplicated concern). Default `enabled: false`. An
`SP_SEMANTIC_CACHE` env override (`on`/`off`/`true`/`false`/`1`/`0`/`yes`/`no`) mirrors
`SP_CONTEXT_OPT`'s pattern for the same reason: operators can flip it per-deploy without
editing YAML, e.g. to A/B a demo the way Headroom's before/after toggle works.

With `enabled: false` (the shipped default), the pipeline never calls into the cache — no
`lookup`/`store` happens on any request path, so the cache stays empty and serving is
byte-for-byte unchanged. The instance itself is constructed unconditionally in
`app/main.py` (mirroring `_ccr`), so the flag gates *use*, not *existence* — the same
always-construct/conditionally-use idiom CCR uses.

**Enforcement point:** `config/serving.yaml` (shipped default) + `app/config/loader.py`
(`SP_SEMANTIC_CACHE` override) + Task 2 (the actual construction/use, gated on
`cfg.enabled`).

## C8 — embedding seam (deferred)

`SemanticCacheBackend` (a `Protocol`: `lookup`, `store`, `size`, `hits`,
`blocked_cloud_entries`) is the seam a future fuzzy/embedding backend implements.
`DeterministicSemanticCache` is the only implementation shipped now. The
`SemanticCacheCfg.embedding` field is a placeholder (`"off"` today, inert — nothing
reads it to construct anything) for a future value naming an embedder.

When an embedding backend is eventually added, it must run **locally** (air-gap
concern — RouteForge deploys no compute per CLAUDE.md rule 4, and a customer's
air-gapped deployment cannot reach a cloud embedding API even for a cache lookup). The
lint guard for that future addition mirrors the existing
`observability.langfuse.capture_io == "full"` onprem-tenant reject: a cloud/remote
embedder configured while any tenant is `onprem_only` is a boot-fail, not a runtime
surprise, for the same "residency beats convenience" reason (CLAUDE.md rule 6). This
guard is **not implemented yet** — there is no embedding backend to guard against — but
is specified here so the seam is built with the right shape from day one instead of
needing a residency retrofit later.

**Enforcement point:** deferred; `SemanticCacheBackend` Protocol exists now so the seam
is typed, no lint guard exists yet (nothing to guard).

## C9 — eligibility carryover (added 2026-09-19, P1b Task 5)

C3 partitions on `(tenant_id, onprem_only, allowed_providers)`. That key is the whole
of what the *store* knows about placement, and it is strictly less than what a request
is allowed to reach: it knows nothing about the app's `allowed_models`/`allowed_tiers`,
nothing about the registry's per-model gates, and nothing about P1b's three endpoint
data-handling declarations (`region`, `prompt_retention`, `accepts_raw_sensitive`). So
two apps of one tenant share a partition while having different eligible sets, and an
app declaring `allowed_regions: [us]` could be served a completion produced by an `eu`
endpoint. The rule:

> An entry whose model is not in **this request's** `FeasibleSet.eligible`
> (`app/policy/feasible.py`) is a **MISS**. It is not served; the request routes
> normally.

A cache hit bypasses the router entirely, so this is the *only* point at which the
request's own eligible set is applied to a cached answer. Enforced in
`app/pipeline.py` at the lookup call site (so the trace can name the refusal:
`CacheRecord.bypass_reason = "entry_not_in_eligible_set"`) and again inside
`_serve_from_cache`, which is the function that actually hands cached text to a
client — the same two-place, defense-in-depth shape as C3.

**Why not widen the partition key instead.** A key can only encode the rules that
existed when it was written, and drifts silently the moment a rule is added — exactly
what happened here between the cache landing and P1b. Checking against the feasible
set closes the whole class in one place, including the pre-existing case where an
app's `allowed_models` changed after an entry was cached, and it cannot go stale
because it asks the one eligibility authority at the moment of use. It also survives
C8: a future fuzzy backend inherits this guarantee unchanged, because it lives in the
pipeline, not in the store.

**A refusal is not an eviction.** The entry stays. Two reasons, both about what the
check *is*:

1. **A read must not mutate shared state.** `lookup` is a read. Eviction would make the
   act of looking change what other readers of the same partition find — a side effect
   nothing in the calling code can see or reason about.
2. **The judgement is per-request, not about the entry.** "Not eligible" here means *not
   for this request*, which is a statement about an app's compiled policy at one moment,
   not about the cached text. The very next request through the same partition — a
   sibling app, or the same app after an operator widens `allowed_regions` — may be
   perfectly entitled to it. Deleting on a per-request judgement conflates "not for you"
   with "not for anyone", and C5's TTL already owns the entry's lifetime.

Keeping it is also cheap and safe: C6 means the entry never leaves the process, and the
guard above means it is never served, so an unservable entry costs only memory.

**What keeping does NOT buy, and why — do not read more into this than it gives.** It
would be natural to add that keeping the entry preserves the sibling app's later hit, or
that evicting would open a timing side channel on a sibling's policy. **Neither holds
today.** The refusing request goes on to route normally and then stores *its own* answer
under the same partition and the same query key (`app/pipeline.py`'s store-on-miss; the
refusal deliberately does not set `cache_bypass_reason`, which would also suppress that
store). So the sibling's entry is overwritten a moment later regardless of what the
refusal did, and the observable hit/miss pattern is the same either way. Two apps in one
partition with disjoint eligible sets therefore *mutually evict* each other by writing,
with the write amplification that implies — a real cost, tracked as backlog, and the fix
belongs to the cache's key (an eligibility fingerprint, rather than enumerating each new
rule into the partition tuple), not to an eviction policy. The narrow property the
decision actually rests on, and the one the test pins, is that **`lookup` does not
mutate** — asserted in the only window where it is observable, between the refused read
and that store.

**Enforcement point:** `app/pipeline.py` (lookup call site + `_serve_from_cache`),
asking `app/policy/feasible.py`.

## Summary table

| Guarantee | What | Enforced where | Status |
|---|---|---|---|
| C1 | sanitized-only | Task 2 caller contract (`pipeline.py` `store()` call site) | shipped (caller contract wired); not enforceable in-store |
| C2 | tenant isolation | partition tuple | built + tested |
| C3 | residency partition | `store` (write refuse) + `lookup` (structural, load-bearing) | built + tested |
| C4 | verification carryover | Task 2 caller contract (`pipeline.py` re-verify on hit) | shipped (verify carryover wired at the hit call site) |
| C5 | enforced TTL | sweep-on-write + evict-on-read + lint | built + tested |
| C6 | never exported | `CacheRecord` counts-only + lint | built + tested |
| C7 | default OFF / one owner | `serving.yaml` + loader | built + tested |
| C8 | embedding seam | `SemanticCacheBackend` Protocol | typed seam only, no backend, no lint guard yet |
| C9 | eligibility carryover | `pipeline.py` lookup guard + `_serve_from_cache` guard, via `policy/feasible.py` | built + tested |
