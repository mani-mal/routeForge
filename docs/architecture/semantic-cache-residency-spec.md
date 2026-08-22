# Semantic-cache residency + correctness spec

Feature #6 (semantic cache), Task 1 design gate. This is the binding contract for the
CCR-class semantic-cache substrate built in `app/cache/store.py`. Task 2 wires the
check/store calls into the pipeline; this doc is what that wiring must honor.

**Status:** substrate built, default OFF (`semantic_cache.enabled: false`). No pipeline
wiring in this task — serving behavior is byte-for-byte unchanged.

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

The honesty counter `served_from_cloud_entry` mirrors CCR's `retrievals_to_cloud`: it
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
`cache_saved_usd`, `entries_size`, `served_from_cloud_entry`, `latency_ms`, and
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

With `enabled: false` (the shipped default), nothing in the pipeline constructs or
touches a `DeterministicSemanticCache` instance — this task adds no pipeline wiring at
all, so the claim is stronger than "the cache does nothing": it doesn't exist as an
object anywhere in the running process.

**Enforcement point:** `config/serving.yaml` (shipped default) + `app/config/loader.py`
(`SP_SEMANTIC_CACHE` override) + Task 2 (the actual construction/use, gated on
`cfg.enabled`).

## C8 — embedding seam (deferred)

`SemanticCacheBackend` (a `Protocol`: `lookup`, `store`, `size`, `hits`,
`served_from_cloud_entry`) is the seam a future fuzzy/embedding backend implements.
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

## Summary table

| Guarantee | What | Enforced where | Status |
|---|---|---|---|
| C1 | sanitized-only | Task 2 caller contract | spec'd, not enforceable in-store |
| C2 | tenant isolation | partition tuple | built + tested |
| C3 | residency partition | `store` (write refuse) + `lookup` (structural, load-bearing) | built + tested |
| C4 | verification carryover | Task 2 caller contract | spec'd, deferred to Task 2 |
| C5 | enforced TTL | sweep-on-write + evict-on-read + lint | built + tested |
| C6 | never exported | `CacheRecord` counts-only + lint | built + tested |
| C7 | default OFF / one owner | `serving.yaml` + loader | built + tested |
| C8 | embedding seam | `SemanticCacheBackend` Protocol | typed seam only, no backend, no lint guard yet |
