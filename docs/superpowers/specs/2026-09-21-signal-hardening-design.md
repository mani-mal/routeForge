# Signal hardening — observation log, replay seeding, error metering, stream availability (P2a follow-up)

**Status:** approved design, 2026-09-21. Precedes P2b (evaluation plane).
**Closes:** backlog B38, B39, B40, B41, and the "latent latency/samples inversion" recorded
under B38. Also closes the open "seed query has no row-count cap" item from P2a.
**Builds on:** `docs/superpowers/specs/2026-09-19-routing-signal-measurement-p2a-design.md`
(P2a, PR #57), `docs/research/routing-signal-provenance.md`.

---

## 1. Problem

P2a made two routing terms measured: latency and availability, held per model in an
in-process `SignalStore` (`app/routing/signals.py`), fed live by the gateway, and **seeded at
boot** from `usage_records` via `app/apps/store.py::aggregate_model_signals`.

The live half is sound. The seed half is not, and every open signal defect traces back to
one cause: **the seed re-derives the signal from the billing table, which records a
different quantity than the live signal observes.**

| Defect | Mechanism |
|---|---|
| B39 | `usage_records.latency_ms` is the **whole-trace** sum (escalation hops + compression + cache lookup), attributed to the final model. Live observation is **per-hop, backend-only**. Two quantities, one EWMA, both labelled `measured`. A semantic-cache hit seeds a ~3 ms "measurement" for a model that was never called. |
| B38 (availability) | Nothing writes `status="error"`, so availability always seeds at 1.0. A failover hop inside a request that later succeeded has no row at all. |
| Samples inversion | `samples = count(ok)+count(error)` is reported for **both** signals; an all-error window would seed latency as `measured 0.0 ms` with full sample weight — the strongest possible preference for a dead endpoint. Latent only because B38 writes no error rows. |
| **Stale-as-fresh (found during this design)** | `aggregate_model_signals` stamps every seed row `last_ts = time.monotonic()` — *now*. A 23-hour-old latency average is therefore reported as fresh (the `staleness_s` cliff never fires on seeded state), and seeded availability has experienced zero decay. |
| Unbounded seed | The seed query has no row cap. |

Independently:

- **B38 (meter).** A request that fails outright writes **no** `usage_records` row. An
  application whose endpoint is down looks, in the console, like one that sent no traffic.
- **B41.** `Gateway.run_stream` observes nothing. **Correction to the backlog entry:** B41
  claimed the Spark's Qwen Code / `kit-gtm` clients contribute nothing. That is wrong. Every
  Qwen Code request carries `tools`, and a tool conversation raises `StreamFallback`
  (`app/pipeline.py`, `_is_tool_conversation`) before `run_stream` is reached, so it is served
  by `Gateway.run()` and **is** observed (live: `qwen3.8-27b` showed `availability n=98
  measured`). What is blind is plain-chat streaming: no tools, not `onprem_only`, not
  `require_verification`, `stream_mode` in `stream_unverified | incremental_verify`.
- **B40.** `RoutingStrategy.rank` reads `time.monotonic()` directly, so staleness and decay
  cannot be tested through the strategy.

## 2. Decisions (user answers, 2026-09-21)

1. **Failed requests are metered: one `error` row per failed request.** (Not per failed hop —
   a per-request table must stay per-request; per-hop outcomes go to the observation log.)
2. **Approach B: a separate observation log.** Seeding replays it. `usage_records` is no
   longer read for seeding.
3. **Streams feed availability, and log time-to-first-token.** Stream latency does not enter
   the latency EWMA. TTFT is recorded for P2b and not used in scoring.

## 3. The observation log

### 3.1 Table `endpoint_observations`

| Column | Type | Meaning |
|---|---|---|
| `id` | int, PK, autoincrement | |
| `ts` | timestamptz, indexed | wall-clock UTC of the observation |
| `model` | str(100), indexed | model slug attempted |
| `outcome` | str(8) | `ok` \| `error` |
| `latency_ms` | float, nullable | backend-only latency; set only for a **non-streamed** `ok` |
| `streamed` | bool | the attempt was a stream |
| `ttft_ms` | float, nullable | time to first token; set only for a **streamed** `ok`; never scored |

**No application id, request id, tenant, or content.** It records endpoint health only — the
same bounded-label stance as `GET /metrics`. It cannot carry PII, so it needs no redaction
and no residency treatment.

It is a **new table**: `create_all` creates it at boot. No `ALTER`, no migration script
change.

### 3.2 One writer: the store's sink

`SignalStore.__init__(cfg, *, sink: ObservationSink | None = None, wall_offset: float | None = None)`.

- `wall_offset` defaults to `time.time() - time.monotonic()` captured at construction; it maps
  the store's monotonic clock to wall time (`wall = mono + wall_offset`). Injectable for tests.
- Every observation method, **after** applying its update, calls
  `sink.put(Observation(ts_wall, model, outcome, latency_ms, streamed, ttft_ms))`
  when a sink is set. Exactly one `put` per applied observation.
- Consequence: the log can never disagree with what the live signal applied. There is no
  second place that decides what counts as an observation.

`ObservationSink` is a small class in a new module `app/routing/observations.py`:

- `put(obs)` — appends to a bounded in-memory `deque(maxlen=signals.max_buffer)`; on
  overflow the oldest entry is dropped and a `dropped` counter increments (logged at flush).
  Never raises, never blocks, no I/O — it is called on the serving path.
- `drain() -> list[Observation]` — atomically takes everything buffered.
- An async `flush(db)` writes a drained batch in one transaction. Any DB exception is logged
  at warning and the batch dropped — **never** raised into serving.

### 3.3 The flusher

A background task started in the app's lifespan (the same startup event that seeds today,
`app/main.py`) — never in `create_app`'s throwaway loop:

- every `signals.flush_interval_s`, `flush` the drained batch;
- once per hour, delete rows with `ts < now - seed_window_hours` (prune);
- on shutdown, one final flush.

### 3.4 Seeding is replay

At boot (replacing the `aggregate_model_signals` call):

1. Load, per model, the most recent `signals.max_seed_obs_per_model` rows with
   `ts >= now - seed_window_hours`, then order all loaded rows by `ts` ascending.
2. Replay each through the store's own observation methods **with the sink detached**
   (replay must not re-log what it reads), at monotonic time
   `mono_i = row.ts_wall - wall_offset` (the booting process's offset).
   - `ok`, not streamed → `observe_success(model, latency_ms, now=mono_i)`
   - `ok`, streamed → `observe_stream_success(model, ttft_ms, now=mono_i)`
   - `error` → `observe_error(model, streamed=..., now=mono_i)`
3. Seed failure keeps P2a's behaviour: log a warning, start empty, never block serving.

`SignalStore.seed(rows)` and `SeedRow` are removed; `aggregate_model_signals` is removed.
Their tests are replaced, not kept alongside.

Because each row is replayed at its **true age**, the staleness cliff and availability decay
apply to seeded state exactly as to live state. That closes the stale-as-fresh defect.

### 3.5 The property: replay identity

> For any sequence of observations, the state reached by replaying the logged sequence equals
> the state reached by observing it live, **up to a constant shift of the monotonic clock**.
> Every read (`latency()`, `availability()` — value, source and sample count) at the same
> age returns the same result.

It holds because every update depends on time only through differences (`now - ts`), and the
replay clock preserves differences exactly.

**Across replicas.** The log holds the union of all replicas' observations, interleaved by
`ts`. The seeded state is the replay of that union — which no single replica held live. That
is intended: the *seed* becomes cross-replica while the live store stays per-replica (P2a's
stance, unchanged).

**The cap is principled, not arbitrary.** With `alpha_floor = 0.1`, once `n >= 9` each update
multiplies prior influence by 0.9. Replaying only the last `K` observations therefore differs
from replaying the full history by at most `0.9^(K-9)` times the range of observed values.
For `K = 200` that factor is ~2×10⁻⁹. The test asserts this bound, computed from `K` and
`alpha_floor`, not a hardcoded tolerance.

## 4. Error metering (B38, meter half)

- `BackendUnavailable` gains `attempted_models: tuple[str, ...]` (default `()`), set by the
  gateway's `_exhausted` from the models tried, in order. A new keyword with a default, so
  existing `raise`/`pytest.raises` sites are unaffected.
- The chat route writes **one** `usage_records` row with `status="error"` when:
  - a non-streamed request ends in `BackendUnavailable` (the 502 path), or
  - a streamed request's backend raises before or during the stream.
- Row contents: `model` = last attempted (`attempted_models[-1]`, or the streamed candidate),
  its `tier` / `provider` from the registry; `prompt_tokens = completion_tokens =
  total_tokens = 0`; `cost_usd = baseline_usd = 0`; `latency_ms` = wall time to failure;
  `sensitivity` / `onprem_only` from the gate's context where the route holds it, else `"-"` /
  `False`, as `_record_cancelled` does.
- **No `error` row** for: `ContextWindowExceeded` (400 — the client's request, not an outage),
  `NoEligibleCandidate` (a policy/routing outcome), a client disconnect (already `cancelled`),
  or any policy rejection.
- A metering write failure is swallowed and logged, like every other `record_usage` call.
- `usage_summary` and `usage_by_application` gain `error_count`. The console usage view shows
  it. **Every spend, token, latency percentile and budget figure stays computed over `ok`
  rows only** — they already filter on `status == "ok"` (`app/apps/store.py`,
  `app/apps/policy.py::day_spend_usd`); this is asserted by test, not assumed.

## 5. Streams feed availability (B41)

Inside `Gateway.run_stream`'s generator:

- On the first chunk carrying content or a tool delta, record TTFT (`time.perf_counter`
  measured from the `generate_stream` call).
- On the terminal chunk (`chunk.final is not None`), call
  `signals.observe_stream_success(model, ttft_ms)`: **availability-only** update (same
  decayed-EWMA rule as `observe_success`), latency state untouched, logged `streamed=True`.
- On an exception from the backend: if `counts_as_endpoint_unhealth(exc)`, call
  `signals.observe_error(model, streamed=True)`; re-raise as today. Before or after the first
  token makes no difference.
- **No observation** for a client disconnect (`GeneratorExit` / `CancelledError`) or for a
  stream that ends without a terminal chunk. Neither is attributable to the endpoint.

This answers both reasons the P2a ruling gave for excluding streams: there is now a real
failure path, and stream timing never enters the latency EWMA.

`observe_error` gains `streamed: bool = False`, used only for the logged row.

## 6. Clock seam (B40)

`RoutingStrategy.rank` (Protocol) and `DefaultRoutingStrategy.rank` gain
`now: float | None = None`, forwarded to `signals.latency(..., now=now)` and
`signals.availability(..., now=now)`. `Router.select` passes nothing (production reads the
real clock). Every implementation and every test fake changes in the same commit — a
Protocol mismatch has previously been swallowed as `NoEligibleCandidate`.

## 7. Config

Three new `SignalsCfg` fields (owned by `routing.yaml`, like the rest of `signals`):

| Field | Default | Lint |
|---|---|---|
| `flush_interval_s` | `5.0` | `SIGNALS_FLUSH_INTERVAL_NOT_POSITIVE` if `<= 0` |
| `max_buffer` | `10000` | `SIGNALS_MAX_BUFFER_NOT_POSITIVE` if `< 1` |
| `max_seed_obs_per_model` | `200` | `SIGNALS_MAX_SEED_OBS_NOT_POSITIVE` if `< 1` |

Rules added in `app/config/lint.py` beside `LATENCY_SCALE_NOT_POSITIVE`.

## 8. Tests

Load-bearing (each must be shown to fail under a named mutation):

1. **Replay identity** (Hypothesis): random interleavings of success / error / stream-success
   observations with increasing timestamps; replayed store vs live store compared on every
   read at several later ages. **Mutations that must turn it red:** stamping replayed rows at
   `now` (today's seed behaviour); omitting `wall_offset` from the replay clock; replaying
   with the sink attached (doubles the log).
2. **Log equals live**: one `put` per applied observation, contents matching what was
   applied; replay produces zero `put`s.
3. **Cap bound**: last-`K` replay vs full replay within `0.9^(K-9) × range`.
4. **Stream observations**: success moves availability only (latency state byte-identical);
   pre-first-token and mid-stream endpoint errors count; `CancelledError` and a stream
   without a terminal chunk record nothing; a 4xx that `counts_as_endpoint_unhealth` excludes
   records nothing.
5. **Error rows**: 502 → exactly one `error` row with the last attempted model; streamed
   backend failure → one `error` row; context overflow 400, `NoEligibleCandidate`, policy
   reject and disconnect → no `error` row; `error_count` present; spend, tokens, budgets and
   latency percentiles unchanged by the presence of error rows.
6. **Clock seam**: staleness cliff and availability decay exercised through `rank(now=...)`.
7. **Writer resilience**: flush DB failure swallowed and logged; overflow drops oldest and
   counts; shutdown flushes; prune removes only rows past the window.
8. **Seed failure**: a failing load leaves an empty store and the app serving.

## 9. Deploy

- New table created at boot. **No `ALTER`.** Error rows use existing columns.
- **First boot after deploy seeds nothing** (the log is empty). Every model reports
  `tier_default` / `neutral` until it has `min_samples` (5) live observations — honest
  behaviour, and a numerically identical pre-P2a ranking (P2a's identity property) in the
  meantime. Stated in the deploy notes so nobody reads it as a regression.
- Spark: api image rebuild only; roll with all three compose files (base, `langfuse.yml`,
  Spark Langfuse overlay), in that order.

## 10. Documentation

- `docs/research/routing-signal-provenance.md`: a new section — the replay-identity property,
  the stale-as-fresh counterexample it closes (with the commit that closes it), the cap
  bound, and the cross-replica consequence. Add to "what is NOT demonstrated": TTFT is
  logged, not validated as a routing signal.
- `docs/backlog/bugs.md`: B38, B39, B40, B41 resolved with the commit; B41's claim about
  Qwen Code corrected in place with a visible dated correction.
- `docs/reference/routing-observability.md`: describe `endpoint_observations`, what it holds
  and does not hold, and replay seeding.
- `CLAUDE.md` routing-observability paragraph: one sentence noting seeding replays the
  observation log.

## 11. Out of scope

- Using TTFT in scoring (P2b decides whether it earns a place).
- Tuning `weights.availability` (B45 — must be measured, not guessed).
- Sharing the **live** store across replicas.
- A console view of `endpoint_observations`.
- Metering per failed hop in `usage_records` (per-hop outcomes live in the observation log).
