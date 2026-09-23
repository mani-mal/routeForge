# Routing outcome records (P2b-2)

One content-free row per routed request — what was eligible, what was tried,
what was served, what it cost, and how it verified — plus local feedback
scores, kept `retention_days` then pruned. This is the substrate a later
phase (P2b-3) learns from; this doc will grow as the writer, reader, and
`/admin/outcomes/export` endpoint land.

**Status: schema + store only (Task 1 of P2b-2).** The two tables below exist
and are covered by store-level tests. Nothing writes to `routing_outcomes` or
`outcome_feedback` yet — later P2b-2 tasks add the writer (on every routed
request) and the reader (`GET /admin/outcomes/export`).

## Config

`config/serving.yaml`:

```yaml
outcomes:
  enabled: true
  retention_days: 90
```

`enabled: false` writes nothing and changes nothing else. `retention_days`
must be `> 0` (boot fails with `OUTCOMES_RETENTION_NOT_POSITIVE` otherwise) —
it is the cutoff `prune_outcomes` uses to delete rows from both tables.

## Content-free by contract

Both tables hold **ids, enums, numbers, entity types, and version hashes
only** — never prompt/completion text, reason strings, upstream error text,
free-text comments, or corrected output. `app/apps/models.py`'s
`RoutingOutcome` and `OutcomeFeedback` docstrings restate this at the source;
`tests/test_outcome_store.py` asserts no column name on either table
contains `prompt_text`, `completion` (as free text — `completion_tokens` is a
count, not text), `content`, `message`, `comment`, `corrected`, `reason`, or
`error`.

## Tables

`routing_outcomes` (`app/apps/models.py::RoutingOutcome`) — one row per
routed request, keyed by `request_id`. Carries the version hashes stamped on
every trace (`app_policy_version`, `endpoint_registry_version`,
`routing_policy_version`), the classification/eligibility inputs
(`workload`, `tier_target`, `complexity`, `prompt_tokens_bucket`,
`sensitivity`, `risk_class`, `onprem_only`, `detected_entity_types`), the
candidate sets (`eligible`, `rejected`, `attempts`, `chosen`, `scoring`), and
the outcome (`cost_usd`, `latency_ms`, `prompt_tokens`, `completion_tokens`,
`verify_required`/`verify_performed`/`verify_passed`/`verify_mode`).

`outcome_feedback` (`app/apps/models.py::OutcomeFeedback`) — a score attached
to a `request_id`. This is a **logical** link, not a foreign key, so pruning
one table never blocks or orphans the other. `comment`/`corrected_output`
free text is never stored here — it continues to go only to the app's own
Langfuse project (`capture_io=full`), exactly as before this feature.

## Store functions

`app/apps/store.py`: `write_outcome`, `get_outcome`, `find_outcome`,
`write_feedback`, `feedback_for`, `prune_outcomes`, `iter_outcomes`. All take
an `AsyncSession`; the caller commits. `iter_outcomes` pages by keyset
`(ts, request_id)` rather than `OFFSET`, so pagination stays stable under
concurrent writes.

See `tests/test_outcome_store.py` for the full behavioral contract (app
scoping, pruning both tables together, keyset pagination with no gaps or
duplicates).
