# Persistent memory — residency spec (Feature 5, Task 1)

**Status:** design-gate doc, 2026-08-22. Authored as part of Task 1 (substrate + config + lint
guards, no pipeline wiring, default OFF). Task 2 wires read/write into the serving pipeline and
must satisfy every guarantee below; the reviewer checks Task 2 against this doc. Sibling reads:
`docs/architecture/self-improving-agent-feature-fit.md` (§ persistent memory is one of the four
cost-reduction levers), `.claude/skills/headroom-compression/` (CCR is the precedent this spec
mirrors).

## Why this exists

Persistent memory (mem0-style: durable, cross-request recall of prior interaction summaries) is a
**second copy of potentially-sensitive data**, same as Headroom's CCR cache (CLAUDE.md rule 6).
Anything the pipeline remembers across requests must obey the same residency posture as anything
it caches: local-only, TTL-bounded, and never exported. This doc is the binding contract; the code
in `app/memory/store.py`, `app/config/schema.py::MemoryCfg`, and `app/config/lint.py::enforce`
implements it.

## R1 — sanitized-only

Memory is populated **only** with post-gate sanitized content: never raw input, never
`result.text`, never `raw_completion`. Compression runs after the gate and before the router
(CLAUDE.md rule 7); memory follows the identical ordering constraint — it is a step-5-class
component and must only ever see what the gate has already redacted.

**Enforcement point:** the caller's contract. `DeterministicMemoryStore.write()` stores whatever
text it is given — it cannot itself verify sanitization, exactly as `CCRStore.store()` trusts the
compressor to pass sanitized text today. Task 2 is responsible for calling `write()` only with
text derived from the sanitized message pipeline (never `ExecutionPolicyContext`-pre entities,
never a raw backend completion). This doc states the contract; Task 2's review checks the call
site.

## R2 — tenant isolation

A recall for one tenant can **never** return another tenant's memory. This is structural, not a
filter applied after a broader scan: `DeterministicMemoryStore` partitions its internal dict by a
key derived from `(tenant_id, application_id, session_id)`, with `tenant_id` **mandatory** and
always the leading component. `recall()` reads only the requesting partition's list — there is no
code path that scans across partitions.

**Enforcement point:** `app/memory/store.py::_partition_key` (raises if `tenant_id` is falsy) +
`DeterministicMemoryStore.recall`'s exclusive per-key lookup. Tested by
`tests/test_memory_store.py::test_tenant_isolation_structural` and
`::test_partition_key_includes_tenant_id`.

## R3 — residency boundary

Memory recall never crosses the residency boundary. For `onprem_only` requests (and any request
where local residency can't be guaranteed), memory must be bypassed or kept local-only — never
retrieved "to cloud" the way a compressed-and-uncompressed CCR original might theoretically be
handed to a cloud call. `DeterministicMemoryStore.recall(..., to_cloud=...)` carries the same
`retrievals_to_cloud` honesty counter `CCRStore` does: the deterministic store is always local, so
this counter stays pinned at 0 under a normal (`to_cloud=False`) recall. The counter exists for
parity with CCR and so a future backend that could genuinely leave the boundary has a place to be
honest about it — but the **caller must never pass `to_cloud=True` for an `onprem_only` request**;
Task 2 owns that call-site discipline, mirroring how the compression orchestrator never retrieves
CCR originals to_cloud for onprem_only today.

**Enforcement point:** `DeterministicMemoryStore.recall`'s `to_cloud` parameter + counter. Tested
by `tests/test_memory_store.py::test_retrievals_to_cloud_pinned_zero_on_normal_recall`.

## R4 — never exported

Memory contents live only in-process. The OTel/Langfuse exporter never receives them; the only
thing that ever reaches a trace is the counts-only `MemoryRecord` (`app/models.py`):
`recalled_count`, `tokens_recalled`, `recall_stayed_local`, `retrievals_to_cloud`, `latency_ms` —
no content field, ever. If a content field is ever added to `MemoryRecord` it **must** be added to
`app/obs/otel.py::REDACT_KEYS` in the same change, exactly as `CompressionRecord` never carries the
compressed/original text itself.

`export_to_telemetry: false` is enforced at boot: `memory.enabled` + `export_to_telemetry is not
False` is a `ConfigLintError` (fail-closed, CLAUDE.md rule 5).

**Enforcement point:** `app/config/lint.py::enforce` (memory guards) + `MemoryRecord`'s
counts-only shape. Tested by `tests/test_config_lint.py::test_lint_rejects_memory_telemetry_export`
and `tests/test_memory_store.py::test_memory_record_is_counts_only`.

## R5 — enforced TTL

Unbounded retention defeats the whole residency claim — a "local-only" cache that never expires is
still a permanent second copy of sensitive data. `DeterministicMemoryStore` enforces TTL two ways,
mirroring `CCRStore` exactly:

- **Sweep-on-write:** every `write()` first sweeps every partition's expired entries, so the store
  stays bounded even in the steady state where nothing ever recalls.
- **Evict-on-read:** every `recall()` filters the requested partition to live entries before
  returning, and drops the partition entirely from the dict if nothing survives.

`ttl_hours` must be `> 0`; `memory.enabled` + `ttl_hours <= 0` is a `ConfigLintError` at boot — this
guard is **new relative to CCR** (CCR's shipped config always sets a positive `ttl_hours` by
convention with no boot-time check; memory adds the explicit check because an unbounded memory
cache is a plausible operator mistake this feature specifically must not allow to ship silently).
Residency must additionally be `local_only`; `memory.enabled` + `residency != "local_only"` is also
a `ConfigLintError`.

**Enforcement point:** `app/memory/store.py::DeterministicMemoryStore._sweep_expired` +
`recall`'s eviction path; `app/config/lint.py::enforce` for the three boot-fail guards. Tested by
`tests/test_memory_store.py::test_ttl_swept_on_write` / `::test_ttl_evicted_on_read`, and
`tests/test_config_lint.py::test_lint_rejects_memory_nonpositive_ttl` /
`::test_lint_rejects_memory_residency_drift`.

## R6 — mem0 is a deferred opt-in

The **default and only backend built now** is `DeterministicMemoryStore` — an in-process,
recency-ranked store with no embeddings and no vector search. "Relevant" for this store means
recency within the tenant partition: most-recent-first, capped at `max_recall`. This is
deliberately boring and safe, exactly like `DeterministicCompressor` is the offline default ahead
of the real `HeadroomCompressor`.

A `MemoryStore` protocol (`app/memory/store.py`) is the seam a future `mem0`-backed store would
implement, behind a lazy import and a `[memory]` extra — the same pattern `HeadroomCompressor` uses
behind `[compression]`. **This backend is not built in Task 1 or Task 2's scope**, and `mem0` is
never imported anywhere in this codebase today.

A real mem0 backend would additionally require a **local embedder** and a **local vector store** to
satisfy R1–R5 (a cloud embedding API or a cloud vector DB would both be residency violations — raw
or sanitized text would leave the boundary to be embedded, or the vector index itself would become
an off-box copy of sensitive summaries). Sourcing and running those locally is an air-gap
deployment concern, out of scope for this task. When that backend is eventually built, it inherits
every guarantee in this doc unchanged — the protocol seam exists precisely so the residency
contract doesn't have to be re-derived per backend.

## R7 — default OFF / one-owner

The `memory:` block lives **only** in `config/serving.yaml` (an infrastructure switch, alongside
`context_optimization`) — no other YAML file restates any of its values, per CLAUDE.md's one-owner
config rule. Shipped default is `enabled: false`. The env override is `SP_MEMORY` (mirrors
`SP_CONTEXT_OPT`): unset follows the YAML value; a truthy spelling (`on`/`true`/`1`/`yes`, any
case) forces it on; anything else forces it off.

**Task 1 adds no pipeline wiring at all** — no code path constructs or calls a
`DeterministicMemoryStore` yet, regardless of `memory.enabled`. Flipping the flag on today has zero
runtime effect; it only surfaces the config value and satisfies the lint guards. Serving behavior
with the shipped config is byte-for-byte unchanged from before this feature. Task 2 is the first
code that constructs a store and reads/writes it per request.

**Enforcement point:** `config/serving.yaml` (single `memory:` block), `app/config/loader.py`
(`SP_MEMORY` override, mirrors `SP_CONTEXT_OPT`). Tested by
`tests/test_memory_config.py::test_memory_cfg_surfaced_and_default_off` and
`::test_sp_memory_env_override`.

## Summary table

| Guarantee | Enforcement | Test |
|---|---|---|
| R1 sanitized-only | caller contract (Task 2 call site) | reviewed at Task 2, not testable in Task 1 |
| R2 tenant isolation | structural partition key (`tenant_id` mandatory, leading) | `test_tenant_isolation_structural`, `test_partition_key_includes_tenant_id` |
| R3 residency boundary | `to_cloud` param + honesty counter, caller discipline | `test_retrievals_to_cloud_pinned_zero_on_normal_recall` |
| R4 never exported | counts-only `MemoryRecord` + boot lint | `test_lint_rejects_memory_telemetry_export`, `test_memory_record_is_counts_only` |
| R5 enforced TTL | sweep-on-write + evict-on-read + boot lint | `test_ttl_swept_on_write`, `test_ttl_evicted_on_read`, `test_lint_rejects_memory_nonpositive_ttl` |
| R6 mem0 deferred | `MemoryStore` protocol seam, no `mem0` import | code review (no test needed to prove an absence of an import) |
| R7 default OFF / one-owner | `serving.yaml` only, `SP_MEMORY` override | `test_memory_cfg_surfaced_and_default_off`, `test_sp_memory_env_override` |
