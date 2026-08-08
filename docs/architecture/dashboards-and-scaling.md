# Dashboards durability and the API-scaling boundary

Cluster D (2026-08-08) made the console dashboards durable and DB-backed (B3),
bounded concurrent multi-step agent runs (B2), and fixed an in-memory-sqlite
session-serialization bug the B3 rewrite exposed (B18). This note is the
durable record of what changed, what's still per-pod by design, and where the
API-scaling story stops and the operator's serving-plane job starts. See
`docs/bugs-backlog.md` (B2, B3, B18) for the issue history this doc closes out.

## Durable, replica-coherent dashboards

Before Cluster D, `/api/requests`, `/api/requests/{id}`, `/api/stats`, and
`/api/fleet` were all served from `app/api/history.py`'s `HistoryStore` — an
in-process Python ring buffer. That has two consequences once you run more
than one API replica: the buffer resets on every restart, and each pod only
ever sees the fraction of traffic load-balanced to it, so two browsers hitting
two different pods see two different dashboards. That was B3.

The fix: the four dashboard reads now go through `app/apps/history_store.py`
against a durable `request_history` DB table (`app/apps/models.py`,
`RequestHistory`). The row is written in the **same DB session and
transaction** as the per-application usage row, inside `app/api/routes.py`'s
request handler — one commit, two inserts, so a request's usage ledger entry
and its dashboard row can't diverge. `retention_rows` (default 5000,
`serving.history.retention_rows` in `config/serving.yaml`) bounds the table:
oldest rows are pruned on write, so it doesn't grow without limit on a
long-running deployment. History now survives a restart and reads identically
from any replica, because every replica reads the same table.

`UsageRecord` (`app/apps/models.py`) is a **separate table and stays
unbounded** — it's the metering/budget ledger (`admin/usage`, per-app spend
enforcement), not a dashboard cache, and CLAUDE.md's Definition of Done
("usage is metered durably") depends on it never being pruned. Don't conflate
the two: `request_history` is a bounded, replaceable dashboard convenience;
`UsageRecord` is the record of what a tenant owes.

`app/api/history.py` (the old in-memory `HistoryStore`) is deleted;
`app.state.history` no longer exists.

## The live SSE animation stays per-pod — deliberately

The flow-visualizer's live stream (`/api/stream`, backed by `app/api/bus.py`'s
`DecisionBus`) is unchanged by Cluster D and remains **in-process**. With more
than one API replica, each pod's `DecisionBus` only ever publishes the
requests that pod itself served — a browser connected to pod A's SSE stream
will not see a request that landed on pod B. This is a known, accepted gap,
not an oversight: the live stream is a best-effort animation feed for the
demo/console experience, and `/api/requests` (now DB-backed, see above) is the
authoritative backfill any dashboard falls back to on load or refresh. Nothing
about correctness depends on the live feed seeing every request.

Making the live feed cross-replica-coherent needs a shared pub/sub layer
(Redis or NATS, replicas publish to a shared channel instead of an in-process
`asyncio.Queue` fan-out) and is **deferred**, deliberately: it's a new
mandatory infra dependency, and RouteForge targets on-prem/air-gapped
deployments where every additional required service is a real cost to the
operator. This is not a technical stopgap waiting on schedule — it's a
boundary call. Note the Redis already present in `deploy/compose/langfuse.yml`
belongs to Langfuse's own stack; it is not a general-purpose message bus
RouteForge's own code is wired to, and reusing it would couple RouteForge's
console to Langfuse's deployment lifecycle for no good reason.

If a customer deployment genuinely needs cross-replica live streaming, treat
it as its own scoped feature: pick a pub/sub dependency, make it as optional
as `providers.azure`/`providers.bedrock` are today, and keep the in-process
fallback for single-replica and air-gapped installs that don't want the extra
service.

## API-tier throughput is gated on operator serving capacity (B2 ↔ B3)

B3 fixes dashboard *correctness* under more than one API replica — every pod
now reports the same numbers. It does not, by itself, make the system handle
more concurrent traffic. Scaling the API tier (more uvicorn/FastAPI replicas)
only increases how many requests RouteForge can accept and route concurrently;
it does nothing to the thing that actually takes 16–24 seconds per LLM call —
the on-prem inference backend(s) the operator has onboarded. Per CLAUDE.md
rule 4, RouteForge is a pure router: it never stands up, scales, or
load-balances inference itself. If an operator scales the API tier without
also scaling their vLLM (or other onboarded backend) to match, they've made
more front doors into the same single queue — B2's failure mode (a burst of
long-lived multi-step requests overlapping and blowing past
`vllm.timeout_s`), not a fix for it.

So: B3 removes a correctness blocker to running >1 API replica. Whether doing
so actually helps throughput is entirely a function of how much serving
capacity the operator has behind it. That capacity planning is the operator's
job, not RouteForge's.

## B2 mitigations: admission control, not inference scaling

RouteForge's own lever for the multi-step burst-saturation failure
(`docs/bugs-backlog.md` B2) is `agent_budgets.max_concurrent_runs`
(`config/policy.yaml`, default 8). `app/main.py` builds an
`asyncio.Semaphore(max_concurrent_runs)` at startup
(`app.state.agent_run_gate`) and threads it through `Pipeline` into
`app/agent/runtime.py::run`, which holds the semaphore for the duration of
the NAT workflow execution — the expensive, long-lived part, not the
bookkeeping around it. This is **admission control**: it caps how many
multi-step agent runs execute concurrently inside RouteForge, so a traffic
burst can't pile an unbounded number of long-lived inferences onto whatever
backend is behind the pool. Runs beyond the cap queue for the semaphore rather
than firing immediately; nothing is rejected outright by this mechanism.

That is a mitigation, not a capacity increase. The levers that actually raise
throughput are operator-owned and out of RouteForge's scope to implement:

- **Scale/replicate the onboarded backend** (e.g. run more `vllm-local-8b`
  replicas behind the endpoint the operator onboarded) — the real fix for
  saturation, and squarely the operator's serving-plane job (rule 4).
- **Raise `agent_budgets.per_call_timeout_s`** — buys individual hops more
  time before they're treated as failed, at the cost of a burst taking longer
  to drain; doesn't add capacity.
- **Trim the `multi_step` persona share** in `tests/personas.yaml` — this is
  test-traffic tuning for a clean burst demo, not a production mitigation.

`max_concurrent_runs` keeps RouteForge from being the thing that makes a
backend-capacity problem worse; it can't manufacture backend capacity that
isn't there.
