# P3c-4 — a capacity model for the policy simulator

**Date:** 2026-10-07 · **Status:** built; the model was revised after an independent review —
the body describes the model **as built**, and §15 "Amendments" lists every change from the
first design with the measurement behind it
**Priority:** 3, slice P3c-4 (`docs/roadmap/priorities-1-3-status.md` §2.1; handoff §22 "Will
on-premises GPU capacity be sufficient?", §25 and §31 "on-premises demand reaches 230% of
current capacity", §26 why traffic weighting matters).
**Follows:** P3a simulator (`app/eval/simulate.py`), P3c-2 scenarios and workload assumptions
(`app/eval/scenarios.py`, `app/eval/proposal.py`), P3c-3 uncertainty ranges
(`app/eval/uncertainty.py`), B60 replayable rejections.
**Paper:** feeds `docs/research/arxiv.md` §4.3 and experiment E10.
**Scope:** offline only — `app/eval/`, `scripts/policy_simulate.py`, `tests/eval/`, docs. No
serving code changes, no database change, no onboarding-form change, no new dependency.

## 1. The problem

The simulator predicts routes, cost, latency and rejections under a policy change. It says
nothing about whether the endpoints that traffic lands on can carry it.

The handoff's own example (§25): "all healthcare traffic on-prem" moves most requests onto
one local endpoint. A report that shows the cost and the route changes but not "on-prem
demand reaches 230% of capacity" leaves out the consequence that matters most to the person
who owns the hardware. Today every figure the simulator prints treats an endpoint as having
unlimited room (`docs/reference/policy-simulator.md` §8.7, §9.6).

Two product boundaries decide what an answer may look like:

- **RouteForge is a pure router.** It deploys, scales and load-balances no inference. A
  capacity model here cannot be about RouteForge managing capacity.
- **RouteForge records what an operator declared and cannot verify it.** It has no
  measurement of how many requests an endpoint can serve at once.

So the answer is a comparison of two things: **capacity the operator states**, and **demand
the replay predicts**.

## 2. Goals and non-goals

**Goals.**

1. An operator can state limits for an endpoint and read, per endpoint and per arm, how
   loaded it would be: peak utilisation of each stated limit, always with the average over
   the whole window beside it, how much of the time and of the traffic is above 100%, and
   when the peak is.
2. With no capacity stated anywhere, every output file is byte-identical to today's.
3. Demand comes from the rows replay already routed. Nothing is simulated a second time and
   no new input is recorded.
4. The scenario comparison shows one more figure per scenario: the proposed arm's most
   loaded stated limit, and the endpoint it is on.
5. An operator can ask the same question at another traffic volume.
6. A short export gets an explicit "window too short" state, not a number.
7. Every capacity section says what the figures are not.

**Non-goals.**

- **Predicted rejections, queueing delay, or any overload behaviour.** §9.2.
- **Ranges on capacity figures.** §9.3.
- **A recommendation.** No "add N GPUs", no ordering of scenarios by load, and never a
  protection named as the thing to change because capacity is short. §9.4.
- **Capacity declared on the onboarded endpoint** (an admin field, a snapshot key, a form).
  A follow-up, named in §13; this slice changes no serving code.
- **Measured capacity** from the endpoint observation log. A follow-up.
- **Admission and rate limiting** (B59), autoscaling, any console surface.
- **Capacity with `--shadow-prices` alone.** A relaxation is still priced as if every
  endpoint had unlimited room.

## 3. Inputs

Capacity is an **assumption**, carried exactly like `workload.token_scale` and an endpoint's
`latency_ms`: the operator states it, the report echoes it, nothing verifies it.

### 3.1 The deployment as it is: `--capacity FILE`

```json
{
  "schema": "routeforge.capacity-assumptions/v1",
  "title": "Lab endpoints, as load-tested 2026-10",
  "endpoints": {
    "local-llama": {"max_concurrency": 8, "requests_per_minute": 600,
                    "tokens_per_minute": 400000, "output_tokens_per_minute": 90000}
  },
  "workload": {"volume_scale": 10, "apps": {"claims": {"volume_scale": 25}}}
}
```

- Top-level keys: `schema`, `title` (optional string), `endpoints`, `workload`. Any other key
  is a load error naming it.
- `endpoints.<slug>`: an object with any of the four limits (§3.3). Every limit is optional.
  The slug must be an onboarded row of the snapshot (enabled or not) or a priced YAML pool
  model. An unknown slug is a load error: a typo must never read as "no limit stated".
- `workload.volume_scale` and `workload.apps.<application_id>.volume_scale`: §4. The
  application must be in the snapshot; `*` is reserved.
- What the file states holds in **both arms**. It describes the deployment, not a change.

**Why a file and a flag, not a block in the scenarios file.** Capacity as it is today is a
fact about the deployment, the same for every proposal and every scenarios file an operator
writes this quarter; it belongs in one file they keep. A flag works identically in
`--proposal` and `--scenarios` mode, so the scenarios schema keeps its closed key set and
there is one mechanism to test. The file's hash goes into `inputs` (§3.5).

### 3.2 What a change would make it: the proposal's `capacity` key

A proposal — and so every scenario body — gains one top-level key and one workload key:

```json
{
  "schema": "routeforge.policy-proposal/v1",
  "apps": {"claims": {"residency": "onprem_only"}},
  "capacity": {"local-llama": {"max_concurrency": 16}},
  "workload": {"volume_scale": 1.5, "apps": {"claims": {"volume_scale": 2}}}
}
```

- `capacity.<slug>` takes the same four limits. It applies to the **proposed arm only** and
  overrides the file's value for that endpoint **limit by limit**: a limit the proposal does
  not name keeps the file's value.
- The slug must exist in the proposed arm: an onboarded row this proposal does not remove, a
  priced YAML pool model, or an endpoint this proposal adds. Anything else is a load error
  at `capacity.<slug>`.
- A top-level map, not a field of `endpoints.add[]` / `endpoints.update`, because a limit is
  just as natural on a cloud pool model (a provider's tokens-per-minute quota) and
  `endpoints.update` accepts only `latency_ms` on one; and because one map reads the same as
  the file's.
- `apply_proposal` validates both keys and writes **neither** into the applied snapshot
  document. That document is what both arms are built from, and `assumptions_block` is
  echoed into every `simulation.json`; adding a key to either would change default output.
  The capacity model reads the proposal itself, after replay.

### 3.3 The four limits

| Limit | Compared with (per bucket) | Typical source |
|---|---|---|
| `max_concurrency` | offered concurrency (§5.3) | the serving engine's batch or sequence limit; a load test |
| `requests_per_minute` | requests arriving per minute | a provider quota; a gateway limit |
| `tokens_per_minute` | prompt + completion tokens per minute | a provider TPM quota |
| `output_tokens_per_minute` | completion tokens per minute | a load test's generation throughput |

Each is a finite number `> 0`. `0`, a negative, a string, a bool, NaN and ±inf are refused,
naming the location. **A limit of 0 is not "no capacity"**: leave the key out for "not
stated". A fraction is legal (`max_concurrency: 0.5` is half of one slot).

The brief suggested three limits. `output_tokens_per_minute` is added because generation
throughput is how an on-prem serving engine's capacity is normally measured; prompt tokens
are far cheaper to process than completion tokens, and a single total-token limit cannot
express that.

### 3.4 Flags

| Flag | Default | Valid | Meaning |
|---|---|---|---|
| `--capacity FILE` | none | a `routeforge.capacity-assumptions/v1` file | §3.1 |
| `--capacity-bucket SECONDS` | `60` | integer, `1 ≤ s ≤ 86400` | bucket length (§5.2) |
| `--capacity-min-buckets K` | `10` | integer `≥ 1` | fewer buckets with an arrival → "window too short" (§6.3) |

**When capacity is on.** The capacity model runs when `--capacity` is given **or** the
proposal (or any scenario) states at least one limit. Otherwise no capacity code runs and no
output changes.

**Nothing stated may silently do nothing** (each is exit `2`, nothing written, the reason on
stderr prefixed `error: capacity:`):

- `--capacity-bucket` or `--capacity-min-buckets` with no capacity stated;
- `workload.volume_scale` in a proposal or scenario with no capacity stated anywhere — it
  changes nothing but the capacity section;
- `--capacity` with `--shadow-prices` and no `--proposal`: there is no proposed arm;
- a capacity file that is missing, is not JSON, has the wrong schema, an unknown key, a bad
  value, or names an endpoint or application the snapshot does not hold;
- a flag value out of range;
- a latency a proposal asserts for an endpoint of more than 24 hours, when a row lands on
  that endpoint (§5.4): after replay, `error: capacity: endpoints.<slug>.latency_ms: …`.

A bad `capacity` or `volume_scale` inside a proposal is a `ProposalError` like any other
(exit `2`; in a scenarios file, named `scenarios[<i>](<name>).proposal.capacity.<slug>…`).
Exit codes do not change: `0`, `2`, `3`, `4`. `--min-agreement` still refuses first.

### 3.5 `inputs`

With capacity on, each document's `inputs` gains one key:

```json
"capacity": {"file_sha256": "<hex, or null with no --capacity file>",
             "bucket_seconds": 60, "min_buckets": 10}
```

`simulation_id` / `comparison_id` hash `inputs`, so two runs that differ in the file, the
bucket or the minimum have different ids. Without capacity the key is **absent**, so
`inputs` and the id are exactly what they are today. A proposal's own `capacity` and
`volume_scale` are already inside `proposal_sha256` / `scenarios_sha256`.

## 4. Volume scale

An export is a record of some period at some volume. A capacity question is often about
another volume: the real one when the export is a slice of traffic, or a future one.

`volume_scale` is finite, `> 0` and `≤ 1,000,000`; default `1`. It can be set in two places:

| Where | Applies to | Reads as |
|---|---|---|
| the `--capacity` file's `workload` | **both arms** | "real traffic is this many times the export" |
| a proposal's `workload` | **the proposed arm** | "…and suppose it grew by this much" |

Each has a global value and per-application overrides (an application's own value, else the
global one, else `1`). The proposed arm's factor for an application is the **product** of
the two. The current arm never sees a proposal's value, as with every other workload
assumption.

**What it does.** Every routed request of an application counts `volume_scale` times:
its requests and tokens in the bucket it arrived in, its service time in the buckets it
runs through (§5.3). Utilisation is therefore linear in it. **A stated limit is never
scaled**: it is printed exactly as the operator stated it.

**What it does not do.**

- It changes **no route and no cost figure**. Routing, costs, counts, latency and quality in
  the rest of the report are for the recorded volume. The capacity section says which scale
  it used.
- It does **not rescale burstiness**. Ten times the volume is the same arrivals, each
  counted ten times. A real tenfold stream is smoother within a bucket and no smoother across
  buckets; neither effect is modelled.
- It does **not make a small export a large one**. Twenty-four rows at `volume_scale: 1000`
  are twenty-four arrival times, not a realistic arrival process. The minimum-bucket rule
  (§6.3) counts buckets, not scaled requests.
- A per-application scale changes the **mix** on a shared endpoint. That is its purpose; the
  cost and quality figures elsewhere in the report do not follow it.

**With `token_scale`.** `workload.token_scale` scales the prompt tokens replay prices
(P3c-2). Token demand uses those scaled tokens, then multiplies by `volume_scale`.
`token_scale` moves neither the request rate nor any service time, so it does not move
`requests_per_minute` or offered concurrency. An operator who believes smaller prompts are
also faster states that with `endpoints.update.<slug>.latency_ms`.

## 5. Demand

### 5.1 Which rows

For each arm, every simulated row the arm **routed** adds demand to the endpoint replay
served it from (`ArmResult.model`, the endpoint after any predicted context-window
re-selection). A row the arm rejected adds nothing. An excluded row adds nothing (§5.5).

`ArmResult` gains two fields, set by `simulate_row` and serialised nowhere: `prompt_tokens`
and `completion_tokens`, the pair the arm handed to `price_call`. Demand reads them, so the
tokens counted are exactly the tokens costed.

### 5.2 When: estimated arrival, in epoch-aligned buckets

`routing_outcomes.ts` is **when the row was written — the end of the request**, not when it
arrived (`docs/reference/routing-outcomes.md` §3.2). Offered load is about arrivals. So a
row's arrival is estimated as

```
arrival = ts − recorded latency_ms
```

using the row's own recorded latency. That is a property of the request, so it is the same
in both arms, whichever service time an arm gives the row (§5.4).

Everything is **integer milliseconds**: `ts` is floored to the millisecond with exact integer
arithmetic (`(ts − epoch) // 1 ms`), and the latency is rounded to the nearest millisecond.
No float decides which bucket a row is in.

A recorded latency is **unusable** when it is missing, not a number, negative, non-finite or
**above 24 hours** (`MAX_SERVICE_MS = 86,400,000`). An unusable latency subtracts nothing —
the row is placed at `ts` — and the row is counted (`rows.unusable_latency`).

Buckets are `--capacity-bucket` seconds long and aligned to the Unix epoch: a row's bucket
is `floor(arrival / bucket)`. It does not depend on which other rows the export holds, and
with the default 60 s a bucket is a UTC minute. A naive timestamp is read as UTC, as
`--since` reads it; offsets are honoured, so daylight-saving changes do not move a row.

A simulated row with **no `ts`** cannot be placed. It is counted (`rows.without_ts`) and is
in no bucket.

**Why 60 s.** Provider quotas are per minute, and it is long against most request
latencies. A request longer than a bucket is spread over the buckets it runs through (§5.3),
so the choice no longer distorts concurrency; it still sets how fine a "peak" is (§6.2).

### 5.3 The four demands

A row `r` that arm `a` routes to endpoint `e` is **in service** on `e` over the interval

```
I_a(r) = [ arrival(r), arrival(r) + S_a(r) )
```

where `S_a(r)` is its service time in that arm (§5.4), in integer milliseconds. For bucket
`b` of length `T` seconds, with `v(r)` the volume scale of the row's application:

```
requests/min        = Σ_{r arriving in b} v(r)                                        × 60 / T
tokens/min          = Σ_{r arriving in b} v(r) × (prompt_tokens(r) + completion_tokens(r)) × 60 / T
output tokens/min   = Σ_{r arriving in b} v(r) × completion_tokens(r)                 × 60 / T
offered concurrency = Σ_{r}               v(r) × |I_a(r) ∩ b|                         / (T × 1000)
```

- **Requests and tokens are counted in the bucket of ARRIVAL.** A rate limit counts
  admissions.
- **Service time is spread over every bucket the request runs through**, by the milliseconds
  of overlap `|I_a(r) ∩ b|`. A request contributes at most `T` to any one bucket, so **one
  request is never more than 1.0 of concurrency in any bucket**, however long it runs and
  however short the bucket. A bucket a request only runs through (no arrival) has
  concurrency and no requests.
- Service time is conserved: the overlaps of one row sum to `S_a(r)`.

**Which token count, and why.**

- `prompt_tokens` is the count replay **priced** the call at: the backend's own billed count
  when the row has one, scaled by `token_scale`; RouteForge's `routing_tokens` only when no
  backend answered. An endpoint's limit is about what the endpoint processes, in its own
  tokenisation; `routing_tokens` is RouteForge's estimate of that and ran 1.5× high on the
  one run that compared them (B64). It is used only where nothing better exists, and the
  rows routed to an endpoint that are sized that way are counted per endpoint per arm
  (`estimated_token_rows`).
- `completion_tokens` is the recorded completion count — what was generated — not the
  client's `max_tokens`. For a row with no backend usage it is the router's output estimate
  (`max_tokens`, else `default_estimated_output_tokens`), the same value the row is costed
  at.
- **`tokens_per_minute` is prompt + completion**, because that is how a provider's TPM quota
  counts a call. A provider that reserves `max_tokens` at admission counts more than this;
  demand against such a quota is understated.
- **`output_tokens_per_minute` is completion only**, for a generation-throughput limit.
- Cached prompt tokens are not subtracted. `workload.cached_share` moves cost, not load.
- A request's tokens are all counted in the bucket it **arrived** in, although the endpoint
  produces a completion over the whole service time. Token demand is an admission rate.

**Offered concurrency** of a bucket is the mean number of requests that would be in service
during it if none ever waited (the time-average of the number in service in an
infinite-server system fed the same arrivals). Summed over the window and divided by its
length it is the arrival rate times the mean service time, `λ · E[S]`.

**Exact and order-independent.** Per endpoint, bucket and application the engine keeps four
exact integers (requests, tokens, output tokens, service milliseconds). An application's
volume scale is applied to those integers once, applications are taken in sorted order and
summed with `math.fsum`. No figure depends on the order rows come in, and a tie for the peak
bucket goes to the earliest bucket.

### 5.4 Service time: per row, per arm

`S_a(r)` is the first of these that applies:

| Source | When | Value |
|---|---|---|
| `asserted` | the proposal asserts a `latency_ms` for the arm's endpoint (proposed arm only) | that latency. It is the operator's claim about the endpoint under the scenario, so it replaces every other source |
| `recorded` | the arm serves the row on the endpoint that **served it alone**: recorded `status == "ok"`, exactly one recorded attempt, `chosen ==` the arm's endpoint, and a usable latency (§5.2) | the row's **own** recorded `latency_ms` |
| `endpoint_mean` | otherwise, when any simulated row was served alone by the arm's endpoint | the **mean** recorded latency of those rows (rounded to the millisecond) |
| `replay` | otherwise | replay's latency for the endpoint: the snapshot's signal, else the tier default |

Consequences, all stated in the output:

- **A row that stays where it was recorded keeps its own service time.** Offered concurrency
  then carries the real distribution of service times, not the request rate times a constant.
- **The mean, not the median.** Replay's reported latency for an observed endpoint is the
  median of its rows (`simulate.observed_latencies`); that stays the latency figure of §4–§10.
  Offered load is rate × **mean** service time, and service times are right-skewed, so the
  capacity model uses the mean. (Lognormal service times with σ = 1: rate × median understates
  the time-average concurrency by 38% — Amendments, A3.)
- **A row moved to an endpoint it never ran on gets that endpoint's mean**, not a prediction
  of its own. That is an assumption (`not_modelled: moved_request_latency`).
- **A row that failed over never lends its latency.** Its recorded latency includes the
  failed attempts and cannot be divided between endpoints, so it is neither that row's
  service time nor part of any endpoint's mean; the row gets `endpoint_mean` or `replay`.
- **A recorded latency is not a backend service time.** It includes the time RouteForge
  spent before and after the backend call. The caveat says so.
- **The mean is over the SIMULATED rows** (after `--since` and the exclusions of §5.5), so a
  cache hit's latency never enters it. Replay's own median is over every record in the
  window; an endpoint whose only served rows are excluded ones therefore falls to `replay`
  with `replay_source: observed`.
- The block carries, per endpoint per arm, `service_time_sources` (rows under each source),
  `service_time_ms` (`mean`, `max`), and `replay_share` / `replay_source`: the share of the
  rows' service time that came from `replay`, and which rung it was. The report prints a
  sentence when that share is a tenth or more, and says that a tier default is not a
  measurement.
- **An asserted or replayed latency above 24 hours is refused** (`CapacityError` at
  `endpoints.<slug>.latency_ms`, exit 2) when a row lands on that endpoint: it cannot be
  placed, and a stated value is never dropped in silence (C15).
- There is no `latency_exceeds_bucket` flag. It marked a distortion — a long request counted
  whole in its arrival bucket — that spreading removed. `service_time_ms.max` remains as
  information.

### 5.5 What is not in the demand

- **Cache hits** — excluded from simulation, and they load no endpoint.
- **Rows before `--since`.**
- **Other excluded rows** (`unknown_status`, `unknown_app`, `missing_features`,
  `missing_routing_tokens`) may have loaded an endpoint. Their count is
  `rows.excluded_not_counted`, and the report prints a note when it is not zero.
- **Failover attempts.** Replay is first-decision. A request that failed over live loaded
  two endpoints; here it loads one. A predicted context-window overflow hop adds no load
  (the refusal is immediate).
- **Traffic that does not pass through RouteForge**, including other tenants of the same
  endpoint.
- **Requests in flight at the window's start** that arrived before it.

## 6. Utilisation

### 6.1 The window, and which buckets count

**The window** runs from the bucket of the first estimated arrival of any simulated row to
the last bucket in which any **recorded** request was still in service (the bucket of the
last millisecond before its `ts`). It is computed from the recorded rows alone — routed or
rejected, by any application — so it is the same for both arms, every endpoint and every
scenario. `window.buckets` is every bucket from the first to the last, empty ones included.

`window.arrival_buckets` is the number of those buckets in which a simulated row
**arrived**. It is what §6.3 compares with the minimum.

Per endpoint and arm, `active_buckets` is the number of window buckets in which that
endpoint had an arrival **or a request in service**.

**Service time past the window.** A `recorded` service interval ends at its row's `ts`, so
it is inside the window by construction. An `asserted`, `endpoint_mean` or `replay` service
time can run past the window's last bucket. That part is put in **no bucket** — every
bucket figure is about the window — and is reported per endpoint per arm as
`service_ms_after_window`, so service time is conserved (C3).

### 6.2 Figures, per endpoint, per arm, per stated limit

With `d_b` the demand in bucket `b` (zero where the endpoint has none), `W` the window's
buckets, `A` the endpoint's active buckets and `L` the limit:

| Field | Definition |
|---|---|
| `limit` | `L`, exactly as stated (a volume scale never moves it) |
| `peak` | `max_{b ∈ W} d_b / L` |
| `peak_bucket` | start of the earliest bucket at the peak (ISO-8601 UTC); null with no demand |
| `mean_window` | `(Σ_{b ∈ W} d_b / |W|) / L` — over **every** bucket of the window: the time-average |
| `mean_active` | `(Σ_{b ∈ W} d_b / |A|) / L` — over the endpoint's own active buckets; `0` with none |
| `buckets_over` | number of buckets with `d_b > L` |
| `buckets_over_share` | `buckets_over / |W|` — the share of the **window** above the limit |
| `requests_over_share` | this endpoint's requests **arriving** in those buckets ÷ all its requests; null with no demand |

Demand of exactly `L` is 100%, which is not above 100% (a `1e-9` relative tolerance keeps
float noise from deciding).

**A peak is never reported alone.** A peak is a maximum over buckets. For a perfectly steady
stream it is well above the mean, and it grows with a longer window and with a shorter
bucket (Amendments, A4). So every object that carries a `peak` carries `mean_window` beside
it, and every sentence and table cell that prints a peak utilisation prints the window
average with it (C20).

**Two means, each with its denominator named.** `mean_window` answers "how loaded is this
endpoint over the period"; for a sparse export it is honestly near zero. `mean_active`
answers "how loaded is it while it is doing anything". The first design reported one `mean`
over the buckets in which any row arrived and named no denominator; on a low-rate stream
that read 0.85–0.87 where the time-average was 0.6.

Demand itself — `peak`, `mean_window` and `mean_active` of each of the four demands — is
reported for every endpoint that has routed rows in either arm, **whether or not a limit is
stated for it**. Traffic that lands on an endpoint nobody stated a capacity for must not be
invisible; it gets no utilisation.

`peak` for an arm (the comparison-table figure) is the largest `peak` over that arm's stated
limits; ties go to the first in (slug, limit) order; null when the arm has no stated limit.
It carries its own context: `endpoint`, `limit`, `utilisation`, `bucket`, `mean_window`,
`buckets_over_share`, `bucket_seconds` and `buckets` (the window's).

### 6.3 Window too short

If `window.arrival_buckets < --capacity-min-buckets` (default 10), the block says
`window_too_short: true` and carries **no endpoint figure at all**: `endpoints` is empty and
`peak` is null in both arms. The comparison-table cell reads
`window too short (k of the 10 buckets needed)` and the section prints one sentence.

The rule counts buckets with an **arrival**, not window buckets and not active buckets:
nine ten-minute requests keep an endpoint busy across eighteen buckets and are still nine
arrivals. It is the conservative count — a long service time cannot make a short export
look long.

Ten is a floor against reporting a "peak" of three buckets, not a claim that eleven buckets
describe a deployment. The section always prints both counts.

## 7. Output shape

### 7.1 `simulation.json` (`--proposal`)

One added top-level key, `capacity`, and `inputs.capacity` (§3.5). No other key or value
changes.

```json
"capacity": {
  "method": "bucketed_offered_load",
  "bucket_seconds": 60,
  "min_buckets": 10,
  "window": {"first_bucket": "2026-09-20T12:00:00+00:00",
             "last_bucket": "2026-09-20T12:29:00+00:00",
             "buckets": 30, "arrival_buckets": 30},
  "rows": {"simulated": 160, "without_ts": 0, "unusable_latency": 0,
           "excluded_not_counted": 0},
  "volume_scale": {"base": {}, "proposal": {"*": 2.0}},
  "not_modelled": ["queueing", "batching", "autoscaling", "engine_concurrency",
                   "external_load", "sub_bucket_bursts", "failover_load",
                   "moved_request_latency", "overload_behaviour"],
  "window_too_short": false,
  "endpoints": {
    "local-cheap": {
      "current": {
        "requests": 160.0,
        "active_buckets": 30,
        "service_time_sources": {"recorded": 160, "endpoint_mean": 0, "replay": 0,
                                 "asserted": 0},
        "service_time_ms": {"mean": 6000.0, "max": 6000},
        "replay_share": 0.0, "replay_source": null,
        "service_ms_after_window": 0.0,
        "estimated_token_rows": 0,
        "demand": {"concurrency": {"peak": 1.2, "mean_window": 0.533333,
                                   "mean_active": 0.533333},
                   "requests_per_minute": {"peak": 12.0, "mean_window": 5.333333,
                                           "mean_active": 5.333333},
                   "tokens_per_minute": {"peak": 12000.0, "mean_window": 5333.333333,
                                         "mean_active": 5333.333333},
                   "output_tokens_per_minute": {"peak": 1200.0, "mean_window": 533.333333,
                                                "mean_active": 533.333333}},
        "limits": {"max_concurrency": {"limit": 1.0, "peak": 1.2,
                                       "peak_bucket": "2026-09-20T12:20:00+00:00",
                                       "mean_window": 0.533333, "mean_active": 0.533333,
                                       "buckets_over": 10, "buckets_over_share": 0.333333,
                                       "requests_over_share": 0.75}}},
      "proposed": {"…": "the same shape, at twice the volume"}
    }
  },
  "peak": {"current": {"endpoint": "local-cheap", "limit": "max_concurrency",
                       "utilisation": 1.2, "bucket": "2026-09-20T12:20:00+00:00",
                       "mean_window": 0.533333, "buckets_over_share": 0.333333,
                       "bucket_seconds": 60, "buckets": 30},
           "proposed": {"endpoint": "local-cheap", "limit": "max_concurrency",
                        "utilisation": 2.4, "bucket": "2026-09-20T12:20:00+00:00",
                        "mean_window": 1.066667, "buckets_over_share": 0.333333,
                        "bucket_seconds": 60, "buckets": 30}}
}
```

(The block the tree produces for the hand-computed export of §11, one stated limit,
`volume_scale: 2` proposed.)

`endpoints` lists every slug with a stated limit in either arm or a routed row in either
arm, sorted. `limits` holds only the limits stated for that arm. `requests` is the
volume-scaled count; `service_time_sources`, `service_time_ms`, `active_buckets` and
`estimated_token_rows` are about recorded rows and buckets and are not scaled. With no row
routed to an endpoint in an arm, `service_time_ms` is `{"mean": null, "max": null}` and
`replay_share` is null. Floats are rounded to 6 places like the rest of the document.

### 7.2 `comparison.json` (`--scenarios`)

- One added top-level key: `capacity: {"scenarios": [{"name": …, …the §7.1 block}]}`, in
  file order. Each entry is exactly the block `--proposal` gives for that body. The
  `current` arm is repeated in every entry; it is identical in all of them by construction
  (the window, the service-time sources and the endpoint means all come from the recorded
  rows alone).
- Each `table` row gains four keys: `peak_utilisation`, `peak_mean_window`, `peak_endpoint`,
  `peak_limit` — the proposed arm's `peak` (§6.2). All four are null when no limit is stated
  in that arm or the window is too short.
- `inputs.capacity`.

### 7.3 Shadow prices

`shadow_prices` is unchanged. With `--proposal --shadow-prices` and capacity on, the
proposal's capacity section is added and the shadow block is not touched.

## 8. Reports

### 8.1 Single proposal

A `## Capacity` section is appended after the P3a report and before "Uncertainty ranges" and
"Shadow prices":

```
## Capacity

> **Stated capacity against replayed demand.** …the caveat, §9.1…

This section reports load against stated limits and proposes no remedy: not more capacity,
and not a change to any policy or protection.

Buckets of 60 s. The window is 30 bucket(s), from the first estimated arrival to the end of
the last recorded request (2026-09-20T12:00:00+00:00 to 2026-09-20T12:29:00+00:00); a
request arrived in 30 of them; 160 simulated row(s).

`avg (window)` is over every bucket of the window, empty ones included: the time-average.
`avg (active)` is over the buckets in which the endpoint had an arrival or a request in
service; that count is printed with it.

Volume scale assumed by the proposal (proposed arm only, multiplying any stated for both
arms): *=2.

Peak load: current peak 120.0% / avg 53.3% (local-cheap, max_concurrency, 60 s buckets);
proposed peak 240.0% / avg 106.7% (local-cheap, max_concurrency, 60 s buckets).

### local-cheap

Requests: current 160, proposed 320. Active buckets (an arrival or a request in service):
current 30 of 30, proposed 30 of 30.

Service time: current mean 6000 ms, longest 6000 ms over 160 row(s) (160 recorded); proposed
mean 6000 ms, longest 6000 ms over 160 row(s) (160 recorded).

| limit | arm | stated | peak | avg (window) | avg (active) | buckets over 100% | requests arriving in those buckets | peak bucket |
|---|---|---|---|---|---|---|---|---|
| max_concurrency | current | 1 | 120.0% | 53.3% | 53.3% | 10 of 30 | 75.0% | 2026-09-20T12:20:00+00:00 |
| max_concurrency | proposed | 1 | 240.0% | 106.7% | 106.7% | 10 of 30 | 75.0% | 2026-09-20T12:20:00+00:00 |

| demand | arm | peak | avg (window) | avg (active) |
|---|---|---|---|---|
| concurrency | current | 1.2 | 0.533333 | 0.533333 |
| concurrency | proposed | 2.4 | 1.06667 | 1.06667 |
| …
```

(Each paragraph is one line in the file; wrapped here.)

Notes are printed only when they apply: rows without a timestamp; rows with no usable
recorded latency; excluded rows not in the demand; and, per endpoint per arm, a tenth or more
of the service time coming from replay's latency for the endpoint (with a second sentence
when that latency is a tier default), service time that falls after the window's last
bucket, and rows whose token count is RouteForge's estimate. A limit stated in one arm only
prints `not stated` in the other. An endpoint with no stated limit prints "No limit is
stated for this endpoint; demand only."

### 8.2 Scenario comparison

- The comparison table gains one column, last: **`peak / avg load (proposed)`**, e.g.
  `peak 100.0% / avg 2.1% (local-llama, max_concurrency, 60 s buckets)`, `not stated`, or
  the window-too-short state. The table stays in file order. Under it: "Peak load is the
  proposed arm's most loaded stated limit and the endpoint it is on: its largest bucket,
  then its average over the whole window. A peak is a maximum over buckets, so read it with
  the average. It is listed in file order like every other column: a lower figure does not
  make a scenario the one to choose."
- One `## Capacity` section: the caveat, the no-remedy sentence, the shared window, and
  "Current arm peak load (the same for every scenario): …".
- Each scenario's section gains `### Capacity`: its volume scale, both arms' peak load and
  the per-endpoint tables.

**Every sentence, cell and table row that prints a peak utilisation prints the average over
the window beside it** (C20). There is no way to read "230%" off a report without its
average.

## 9. What the figures are, and are not

### 9.1 The caveat

Every capacity section opens with this, verbatim (a test pins it):

> **Stated capacity against replayed demand.** Every limit below is a figure the operator
> stated; RouteForge has not measured it and does not manage capacity. Demand is the replayed
> route of each recorded request. A request arrives at its estimated arrival time (the time
> the record was written minus its recorded latency) and is in service from then for its
> service time: requests and tokens are counted in the bucket of arrival, service time in
> every bucket it runs through. Service time is the request's own recorded latency where
> replay serves it on the endpoint that served it alone; otherwise that endpoint's mean
> recorded latency; otherwise replay's latency for the endpoint. A latency the proposal
> asserts replaces all three. A recorded latency includes the time RouteForge spent before
> and after the backend call, and for a request that failed over it cannot be attributed to
> one endpoint, so it is not used. A peak is a maximum over buckets: it grows with a longer
> window and with a shorter bucket even for a steady stream, so every peak is printed with
> the average over the whole window. Nothing here models queueing, batching, autoscaling, the
> serving engine's real concurrency behaviour, load that reaches an endpoint without passing
> through RouteForge, bursts shorter than a bucket, or the load of failover attempts.
> RouteForge does not know what an endpoint does above a stated limit (queue, shed or slow
> down), so no rejection and no delay is predicted. An export of a few buckets says little.

`capacity.not_modelled` carries the exclusions as ids: `queueing`, `batching`,
`autoscaling`, `engine_concurrency`, `external_load`, `sub_bucket_bursts`, `failover_load`,
`moved_request_latency`, `overload_behaviour`. (`moved_request_latency`: a request replay
moves to an endpoint it never ran on gets that endpoint's mean service time, §5.4.)

### 9.2 No predicted rejection, and no `on_overload` switch

**Decision: not built.** The brief allowed an operator-chosen `on_overload: "reject"` that
would count requests beyond a limit as "over-capacity requests", if it was simple and
honest. It is simple. It is not honest here, for three reasons:

1. **RouteForge's own gateway would not lose those requests.** A refused request fails over
   to the next candidate and the circuit breaker opens. Replay models neither. Counting the
   excess as lost requests contradicts what the product does; following the failover means
   the excess overloads the next endpoint, a cascade a first-decision replay cannot follow.
2. **Shedding is not a fluid.** With a hard concurrency limit and no queue, the blocked share
   is Erlang-B, which is above zero well below 100% utilisation and depends on arrival
   variability the model does not have (arrivals and service time are kept only as bucket
   totals). "Requests beyond capacity in a bucket" is a lower bound that reads like an
   estimate.
3. **The figures already reported carry the exposure without asserting a behaviour.**
   `buckets_over_share` and `requests_over_share` say how much of the time and of the
   traffic is above a stated limit. What happens to it is the endpoint owner's knowledge.

The roadmap sketched "turn overload into a predicted-rejection figure" and the handoff's
example shows a rejection rate beside the utilisation. Neither is produced. `newly_rejected`
remains a policy figure only, and nothing in the capacity block has "reject" in its name.

### 9.3 No range under `--uncertainty`

**Decision: capacity figures are points.** The P3c-3 bootstrap resamples rows with
replacement within an application. That destroys exactly what capacity depends on: a
resample keeps each row's timestamp but changes how many rows fall in each bucket at random,
so a "range" on a peak would measure the bootstrap's own reshuffling of arrivals, and a peak
is a maximum, for which the percentile bootstrap is known to be poor.

With `--uncertainty` and capacity together:

- the `capacity` block is identical to the one without `--uncertainty`;
- the `uncertainty` block is identical to the one without capacity (`volume_scale` moves no
  row, so no range moves, including when small applications are pooled);
- the capacity section prints: "Capacity figures are points and carry no range.
  `--uncertainty` resamples rows as if their order did not matter; capacity depends on when
  requests arrive, so a resample of rows says nothing about it. Resampling whole blocks of
  time is not built."
- the comparison table's peak-load cell carries no bracket.

A block (time-bucket) bootstrap is the right tool and is a named follow-up (§13).

### 9.4 Never a recommendation

- No output names a remedy. There is no "capacity needed", "GPUs to add" or "shortfall"
  field; the section says so in a fixed sentence.
- Scenarios stay in file order. No key or sentence orders them by load.
- A security requirement is never presented as the thing to change because capacity is
  short. The capacity section does not mention a requirement at all.
- No output key or sentence contains `rank`, `recommend`, `best`, `winner`, `prefer`,
  `score`, `should`, `headroom`, `relax`, `gpu`, `buy` or `upgrade` (a test scans for them).

## 10. Invariants (each pinned by a named test)

Tests are in `tests/eval/`; `cap` = `test_capacity.py`, `rep` = `test_capacity_report.py`,
`scn` = `test_capacity_scenarios.py`, `cli` = `test_capacity_cli.py`, `prop` =
`test_capacity_proposal.py`.

| # | Invariant | Test |
|---|---|---|
| C1 | With no capacity stated anywhere, every output of every mode is byte-identical to the tree before this slice (the P3c-3 golden files, not regenerated). With capacity, only `capacity`, `inputs.capacity` and the four table keys are added; a proposal's `capacity` and `volume_scale` never reach the applied document or the `assumptions` block. | `test_uncertainty_golden.py::test_u1_output_without_the_flag_is_byte_identical`; `prop::test_c1_capacity_and_volume_never_reach_the_applied_document`; `scn::test_c1_without_a_plan_nothing_is_added`, `scn::test_c1_a_plan_adds_one_key_and_four_table_keys_and_changes_nothing_else`; `cli::test_c1_a_capacity_file_adds_keys_and_changes_no_existing_value`, `cli::test_c1_the_report_is_the_old_report_plus_one_section` |
| C2 | Deterministic: the same inputs give the same bytes; the block does not depend on row order (volume scales 0.1 / 0.7 / 1.3, shuffled), on the order of equal buckets (a tie goes to the earliest), or on hash randomisation. | `cap::test_c2_the_block_is_byte_identical_whatever_the_row_order`, `cap::test_c2_many_applications_in_one_bucket_sum_the_same_in_any_order`, `cap::test_c2_a_tie_for_the_peak_goes_to_the_earliest_bucket_whatever_the_order`, `cap::test_c2_the_block_does_not_depend_on_hash_randomisation`; `cli::test_c2_the_run_is_byte_identical_twice` |
| C3 | Conservation, exact, in integers, at bucket lengths 1, 60 and 3600 s: per arm, Σ over endpoints and buckets of requests = the rows the arm routed (that have a `ts`); the same for tokens and output tokens; service milliseconds in buckets plus those after the window = Σ of the rows' service times. | `cap::test_c3_requests_tokens_and_service_time_are_conserved`, `cap::test_c3_service_time_after_the_window_is_kept_and_reported` |
| C4 | Utilisation and demand are linear in `volume_scale` and utilisation is inversely linear in a stated limit; a limit moves no demand and is printed as stated; `volume_scale` moves no route, no cost and no service time. | `cap::test_c4_utilisation_is_linear_in_volume_scale`, `cap::test_c4_utilisation_is_inversely_linear_in_a_stated_limit`, `cap::test_c4_volume_changes_no_route_and_no_cost` |
| C5 | A no-op proposal gives identical demand and utilisation in both arms. | `cap::test_c5_a_noop_proposal_gives_identical_demand_in_both_arms`; `scn::test_c5_the_empty_scenario_has_the_same_load_in_both_arms` |
| C6 | Removing an endpoint moves its demand to the endpoints replay routes those rows to: per-endpoint demand equals the replayed picks, and the gains equal the summary's own `moves`. | `cap::test_c6_a_removed_endpoints_demand_goes_where_replay_routes_it` |
| C7 | A rejected row adds no demand, and is in no endpoint's count of estimated rows. | `cap::test_c7_a_rejected_row_adds_no_demand`, `cap::test_a_row_no_arm_routes_is_not_counted_as_estimated_demand` |
| C8 | Fewer than `--capacity-min-buckets` buckets with an **arrival** gives `window_too_short` and no utilisation figure in the JSON, the table or the report; the threshold is inclusive; buckets a request only runs through do not count. | `cap::test_c8_fewer_buckets_than_the_minimum_is_a_state_not_a_number`, `cap::test_c8_the_minimum_counts_buckets_with_an_arrival_not_the_window`, `cap::test_c8_the_minimum_is_inclusive_and_can_be_lowered`, `cap::test_c8_no_rows_at_all`; `rep::test_c8_a_short_window_prints_the_state_and_no_figure`; `scn::test_c8_a_short_window_fills_the_cell_with_the_state`; `cli::test_c8_a_short_export_says_so_in_the_json_the_table_and_the_report` |
| C9 | `--uncertainty` leaves capacity as points: the capacity block and the uncertainty block are each unchanged by the other, and the report says why. | `rep::test_c9_with_ranges_in_the_same_report_capacity_says_why_it_has_none`; `scn::test_c9_ranges_and_capacity_together_leave_capacity_as_points`, `scn::test_c9_volume_scale_does_not_move_a_range_even_with_small_apps_pooled`; `cli::test_c9_uncertainty_leaves_capacity_as_points` |
| C10 | No ranking, recommendation or remedy vocabulary in any key or sentence; file order kept. | `cap::test_c10_no_key_ranks_recommends_or_prescribes`; `rep::test_c10_no_sentence_ranks_recommends_or_prescribes`; `scn::test_c10_the_table_stays_in_file_order_and_nothing_ranks` |
| C11 | No serving module imports `app.eval`. | `test_isolation.py::test_no_serving_module_imports_app_eval` |
| C12 | The caveat is this spec's text (§9.1) and opens every capacity section; `not_modelled` has the nine ids. | `rep::test_c12_the_caveat_is_the_spec_text`, `rep::test_c12_the_section_opens_with_the_caveat_and_the_no_remedy_sentence`; `scn::test_the_report_has_one_capacity_section_and_one_per_scenario` |
| C13 | The plan is in `inputs.capacity`; ids tell plans apart. | `cli::test_c13_the_plan_is_in_inputs_and_the_id_tells_plans_apart` |
| C14 | On a synthetic export with a known arrival pattern, every figure equals its closed form — for requests that end inside their arrival bucket and for requests that cross buckets; demand of exactly the limit is not "over". | `cap::test_c14_utilisation_matches_the_closed_form`, `cap::test_c14_a_request_that_crosses_buckets_is_spread_over_them`, `cap::test_c14_exactly_at_the_limit_is_not_over_it`; `cli::test_c14_the_cli_reports_the_hand_computed_utilisation` |
| C15 | Nothing stated silently does nothing: an unknown slug, an unknown key, a limit `≤ 0`, a bad `volume_scale`, a tuning flag or a `volume_scale` with no capacity stated, `--capacity` with no proposed arm, and an asserted latency too long to place are each exit 2 naming the location. | `cap::test_c15_a_bad_capacity_file_is_refused_naming_the_location`, `cap::test_c15_a_slug_or_app_the_snapshot_does_not_hold_is_refused`, `cap::test_c15_a_plan_value_out_of_range_is_refused`, `cap::test_c15_an_asserted_latency_too_long_to_place_is_refused`; `prop::test_c15_a_bad_limit_is_refused_naming_it`, `prop::test_c15_a_bad_volume_scale_is_refused`, `prop::test_c15_a_capacity_for_an_endpoint_that_is_not_there_is_refused`; `scn::test_c15_a_latency_too_long_to_place_names_the_scenario`; `cli::test_c15_a_tuning_flag_with_no_capacity_stated_is_refused`, `cli::test_c15_a_volume_scale_with_no_capacity_stated_is_refused`, `cli::test_c15_a_bad_capacity_file_is_refused`, `cli::test_c15_a_flag_out_of_range_is_refused`, `cli::test_c15_an_asserted_latency_too_long_to_place_is_refused`, `cli::test_c1_shadow_only_output_is_untouched_and_takes_no_capacity` |
| C16 | A row is placed by estimated arrival (`ts` − recorded latency) in an epoch-aligned bucket, in exact integer milliseconds, whatever the timestamp's offset; an unusable latency (including one above 24 hours) subtracts nothing and is counted; a row without `ts` is counted and in no bucket. | `cap::test_c16_a_row_is_placed_by_its_estimated_arrival`, `cap::test_c16_an_unusable_latency_subtracts_nothing`, `cap::test_c16_a_latency_of_exactly_24_hours_is_the_longest_usable_one`, `cap::test_c16_buckets_are_epoch_aligned_whatever_the_offset`, `cap::test_c16_a_timestamp_is_floored_to_the_millisecond_in_exact_integers`, `cap::test_c16_rows_without_a_timestamp_are_counted_and_in_no_bucket`, `cap::test_rows_with_an_unusable_latency_are_counted_and_placed_at_ts` |
| C17 | In a comparison each scenario's block is the single-proposal block for that body, and the `current` arm is the same in every scenario. | `scn::test_c17_each_scenario_is_the_single_proposal_block_in_file_order`, `scn::test_c17_the_current_arm_is_the_same_in_every_scenario` |
| C18 | A single request never contributes more than 1.0 of concurrency to any bucket. Twelve requests two hours apart, each 138 s, against `max_concurrency: 1`: peak exactly 100%, no bucket over, `mean_window` = 12 × 138 / (1323 × 60) = 0.0209. | `cap::test_c18_twelve_requests_two_hours_apart_never_exceed_one_in_service`, `cap::test_c18_one_lone_five_minute_request_is_one_request`, `cap::test_c18_a_single_request_never_fills_more_than_a_bucket`; `rep::test_a_sparse_export_reads_as_sparse` |
| C19 | On a Poisson stream with lognormal service times (σ = 1), rows served where they were recorded, `mean_window` concurrency equals the true time-average number in service (to 0.1%) and `λ · E[S]` (within 5%); on a steady stream it does not move with the window or the bucket, and the peak does. | `cap::test_c19_the_window_mean_is_the_time_average_under_skewed_service_times`, `cap::test_c19_the_window_mean_is_steady_and_the_peak_depends_on_window_and_bucket` |
| C20 | No output prints a peak utilisation without `mean_window` beside it: not the block, not the comparison table, not a report sentence. | `cap::test_c20_no_peak_is_reported_without_the_window_mean_beside_it`; `rep::test_c20_no_sentence_prints_a_peak_without_the_average`; `scn::test_c20_no_cell_or_sentence_prints_a_peak_without_the_average`; `cli::test_c20_the_written_report_never_prints_a_peak_alone` |

Service time per row per arm (§5.4) is pinned by
`cap::test_a_row_served_where_it_was_recorded_keeps_its_own_service_time`,
`cap::test_a_moved_row_gets_the_endpoints_mean_recorded_latency_not_the_median`,
`cap::test_an_endpoint_no_row_was_recorded_on_falls_back_to_replays_latency`,
`cap::test_an_endpoint_only_excluded_rows_ran_on_says_its_latency_is_replays`,
`cap::test_a_failed_over_row_never_lends_its_latency` and
`cap::test_an_asserted_latency_replaces_every_other_source_in_the_proposed_arm`.

## 11. Testing

- **The golden pin.** `tests/eval/test_uncertainty_golden.py` (two cases × four modes, files
  written before P3c-3) must pass untouched. No golden file is regenerated.
- **Two hand-computed exports (C14).**
  - *Requests that end inside their bucket.* One endpoint, 20 minutes at 2 requests a minute
    then 10 at 12; each request 6 s, arriving no later than second 49, 900 prompt + 100
    completion tokens; limits 1 concurrent, 10 req/min, 10,000 tok/min, 500 output tok/min.
    By hand: a quiet minute is 20% / 20% / 20% / 40%, a busy one 120% / 120% / 120% / 240%;
    the window is 30 buckets, all with an arrival, so both means are 53.33% (106.67% for
    output); 10 of 30 buckets and 75% of requests are over. At `volume_scale: 5` a quiet
    minute is exactly 100% and is not over. Checked in the engine and through the whole CLI.
  - *Requests that cross buckets.* One request a minute for 12 minutes, each arriving at
    second 30 and taking 90 s. By hand, service seconds per minute: 30, then 90 for eleven
    minutes, then 60 in a 13th minute with no arrival → 0.5, 1.5 × 11, 1.0 in service. The
    window is 13 buckets, 12 with an arrival, 13 active; the time-average is
    12 × 90 / (13 × 60) = 1.3846; against `max_concurrency: 1`, 11 of 13 buckets are over,
    holding 11 of the 12 arrivals.
- **Every demand figure comes from the real replay** (`simulate()` → `feasible_set` →
  `Router`), never from a guess at where a fixture routes. C6 compares against the replayed
  picks and the summary's `moves`. The exception is stated where it is made: C18 and C19 are
  arithmetic on a known arrival process and build their rows by hand
  (`test_capacity.py::synthetic`), each recorded as served alone by one endpoint.
- **Statistical checks use a truth computed outside the engine**: an event sweep over the
  raw (arrival, service) pairs gives the largest number in service at one instant and the
  request-seconds in service; nothing in it is bucketed.
- **Input classes likely to bite:** identical timestamps; no timestamp; mixed UTC offsets
  for one instant; a timestamp 400 µs before a bucket boundary; a one-bucket export; a
  two-session export a week apart; an endpoint with a limit and no rows; an endpoint with
  rows and no limit; a limit of 0; a slug not in the snapshot; a limit on a removed endpoint;
  a response longer than the bucket; a lone request; a latency of 0, of exactly 24 hours, of
  1e12 and of 1e300 ms; a row that failed over; an endpoint no row was recorded on; an
  asserted latency that runs past the window; non-dyadic volume scales with shuffled rows;
  an exact tie for the peak; `volume_scale` with small applications pooled under
  `--uncertainty`.
- `tests/eval/test_isolation.py` unchanged.

## 12. Docs

- `docs/reference/policy-simulator.md`: a new §11 (inputs, the four limits, flags, the
  method, how to read a peak with its averages, the caveat, output shape, reports, limits); the `capacity` and `volume_scale` keys
  in §2; the flags and exit-2 cases in §3; the keys in §4.1 and §9.4; §6, §8.7, §9.6 and
  §10.10 no longer say capacity is out of scope.
- `docs/research/feasible-route-bench.md`: a new §21 — the model, what it is not, the
  invariants and their tests, results (synthetic verification only, including the
  before/after of §15; every measurement on recorded traffic **TO MEASURE**, labelled),
  limits.
- `docs/research/arxiv.md`: §4.3 — the claim, the method, what it does and does not model,
  and the handoff's healthcare example as experiment E10; a threat to validity.
- `docs/roadmap/priorities-1-3-status.md`: the slice in Built; §2.1; §3.
- `docs/backlog/bugs.md`: B66, the follow-ups of §13.

## 13. Follow-ups (not built)

1. **Declared capacity on the onboarded endpoint.** An optional capacity declaration on
   `POST /admin/infra/models`, exported in the config snapshot, so the `--capacity` file is
   generated instead of hand-written. It is an admin and schema change (columns, an `ALTER`,
   a snapshot key), and by the product's rule it stays a declaration RouteForge records and
   cannot verify.
2. **A time-block bootstrap** for ranges on capacity figures (§9.3).
3. **A service-time model for requests that move.** Rows that stay on their recorded
   endpoint now use their own recorded latency (§5.4, §15 A3). A row moved to another
   endpoint gets that endpoint's mean; a latency-versus-tokens model per endpoint would need
   its own design and validation. So would separating backend time from the time RouteForge
   spends around the call, which the outcome record does not split.
4. **An overflow assumption** (§9.2), if failover is ever replayed.
5. **Capacity for shadow prices.**

## 14. Where this design departs from the brief

Each follows the code as it is and keeps the brief's intent.

1. **`ts` is not an arrival time.** The brief's caveat said timestamps are request-arrival
   times at RouteForge. They are the time the row was written, at the end of the request
   (`app/apps/models.py`: `default=utcnow` at insert). Rows are bucketed by `ts` minus the
   recorded latency, and the caveat says that instead.
2. **Four limits, not three.** `output_tokens_per_minute` is added (§3.3).
3. **The current arm's capacity comes from a `--capacity` file**, not from a block in the
   scenarios file. The brief offered both (§3.1).
4. **`volume_scale` exists in two places** — the file (both arms) and the proposal (proposed
   arm), multiplied. The brief named only `workload.volume_scale`. A proposal-only scale
   would compare a current arm at the recorded volume with a proposed arm at another, and
   the difference would read as the policy's effect.
5. **`volume_scale` and `capacity` are not in the `assumptions` block.** That block is
   printed in every `simulation.json` with a fixed key set; a fourth key would break
   byte-identity. They are echoed in `edits` and in the capacity block.
6. **`volume_scale` with no capacity stated is an error**, not a silent no-op.
7. **Means and shares are over buckets that hold traffic**, not over the whole window.
   *Superseded (§15 A4):* both means are reported, each with its denominator, and shares are
   of the window.
8. **`on_overload` is not built** (§9.2). The roadmap's "predicted-rejection figure" is not
   produced.
9. **`ArmResult` gains `prompt_tokens` and `completion_tokens`**, defaulted and serialised
   nowhere, so demand counts the tokens that were costed.
10. **Demand is reported for endpoints with no stated limit**, without a utilisation.
11. **`latency_source` and `latency_exceeds_bucket` are reported per endpoint per arm.** Not
    in the brief; each marks a concurrency figure that rests on less than it appears to.
    *Superseded (§15 A2, A3):* `service_time_sources`, `replay_share` and `replay_source`
    say what each endpoint's service time rests on; the bucket flag is removed.
12. **Capacity with `--shadow-prices` alone is refused**, not ignored.

## 15. Amendments (as built)

The first design (§1–§14 as approved on 2026-10-07) was built faithfully and then reviewed
independently before any figure was quoted. The review found that the **model** it specified
printed numbers that were unsafe to quote. The model was revised; the body above describes
the revised model, and this section records each change with the measurement behind it.

Every measurement here is **synthetic**: Poisson arrivals from Python's `random.Random(seed)`
(`expovariate`), arrival times rounded to the millisecond, each row recorded as served alone
by one endpoint with its own service time. "Before" is the engine at commit `422d234`,
"after" the engine as built; both were run on the same streams on 2026-10-06 (Linux x86_64,
Python 3.12.3). "True" figures were computed from the raw (arrival, service) pairs outside
the engine. Nothing here is a measurement of real traffic or of a real endpoint. The full
tables and the method are in `docs/research/feasible-route-bench.md` §21.5.

### A1. Demand is accumulated exactly, and does not depend on row order (C2)

*First design:* demand was a float per (endpoint, bucket), incremented row by row with the
row's volume scale already multiplied in. *Finding:* with per-application volume scales 0.1 /
0.7 / 1.3, 60 shuffles of the same 3,000 rows gave **29 distinct blocks** (before rounding to
six places; the review counted 2 distinct blocks after rounding over 200 shuffles), and two
buckets holding the same requests summed to 5.5 and 5.500000000000001 depending on the
order, which moved `peak_bucket`. *As built (§5.3):* exact integers per (endpoint, bucket,
application); one multiplication per application; `math.fsum` in sorted-application order.
The same 60 shuffles give one block, and the tie goes to the earlier bucket in every order.

### A2. Service time is spread over the buckets a request runs through (C14, C18)

*First design:* a request's whole service time was counted in the bucket it arrived in, with
a flag (`latency_exceeds_bucket`) when the endpoint's latency was longer than a bucket.
*Finding:* Poisson arrivals at a true mean offered load of 2.0 for 24 hours, 60 s buckets,
constant service time `S`; the largest number in service at one instant was 12 in every
case:

| S (s) | requests | peak before | peak after | true per-bucket peak | before ÷ true | flag before |
|---|---|---|---|---|---|---|
| 4 | 43,278 | 3.667 | 3.671 | 3.671 | 1.00 | no |
| 30 | 5,854 | 6.500 | 6.155 | 6.155 | 1.06 | no |
| 60 | 2,965 | 8.000 | 9.016 | 9.016 | 0.89 | no |
| 90 | 1,967 | 10.500 | 9.313 | 9.313 | 1.13 | yes |
| 300 | 596 | 15.000 | 10.782 | 10.782 | 1.39 | yes |
| 900 | 204 | 45.000 | 12.000 | 12.000 | 3.75 | yes |

The flag missed the 6% and 11% errors at `S ≤ T`. The case that would have been quoted:
twelve requests, one every two hours, each 138 s, against `max_concurrency: 1` — never more
than one in service — read **peak 230%, mean 230%, 12 of 12 buckets over, every request
over**. As built it reads peak 100%, `mean_window` 2.1% (0.0209), `mean_active` 76.7%, no
bucket over. One lone five-minute request read 5.0 concurrent; it reads 1.0.

*As built (§5.3):* the overlap of the service interval with each bucket. The flag is
removed (§5.4): what it warned of no longer happens.

### A3. Service time is per row, and an endpoint's fallback is its mean (C19)

*First design (§5.4 then):* one latency per endpoint per arm — replay's, which for an
observed endpoint is the **median** of its rows. *Finding:* Poisson arrivals at 1/3 per
second for 6 hours (7,269 requests), lognormal service times with median 6.0 s and σ = 1.0
(sample mean 9.73 s): mean concurrency **2.018 before** against a true time-average of
**3.265** — a 38.2% understatement. **After: 3.265.** *As built (§5.4):* the row's own
recorded latency where the arm serves it on the endpoint that served it alone; else that
endpoint's mean; else replay's latency; an asserted latency replaces all three.

This is follow-up 3 of §13 in the first design, built for rows that stay on their recorded
endpoint. A latency model for rows that move is still not built.

### A4. A peak is never reported without the average over the window (C20)

*First design:* `peak` and one `mean`, the mean over the buckets in which any row arrived,
with no key naming that denominator; the headline and the comparison cell printed the peak
alone. *Finding:* on a steady stream the peak is a maximum over buckets and says little by
itself, and the conditional mean overstated a sparse stream. Stationary Poisson, 0.5/s,
`S` = 4 s (true mean 2.0), peak ÷ mean:

| window | T (s) | before | after (peak ÷ `mean_window`) |
|---|---|---|---|
| 1 h | 60 | 1.64 | 1.59 |
| 24 h | 60 | 1.70 | 1.64 |
| 7 d | 60 | 1.70 | 1.68 |
| 1 h | 10 | 2.74 | 2.97 |
| 24 h | 10 | 3.18 | 3.02 |
| 7 d | 10 | 3.38 | 3.21 |

Spreading does not change this — it is what a maximum does — so the fix is in what is
printed. At a low rate (1.2 requests a minute, `S` = 30 s, true time-average 0.59 over
7 days) the first design reported a mean of **0.852** and a peak of 4.0; as built,
`mean_window` is **0.593** (the true figure), `mean_active` 0.715 and the peak 3.552 (the
true per-bucket peak).

*As built (§6.2, §8):* `mean_window` (every bucket of the window), `mean_active` (the
endpoint's active buckets, a count that is printed), `buckets_over_share` as a share of the
window; the headline `peak` object carries `mean_window`, `buckets_over_share`,
`bucket_seconds` and `buckets`; the comparison table has `peak_mean_window`; and every
printed peak reads `peak X% / avg Y% (endpoint, limit, T s buckets)`.

### A5. Smaller corrections

- **`rows.estimated_tokens` counted rows that are in no demand** (rejected in both arms).
  Replaced by `estimated_token_rows` per endpoint per arm: the rows routed there whose token
  count is an estimate. A block-level count of "rows in some arm's demand" would differ
  between scenarios and break C17 for the shared `rows` object.
- **Placement used float seconds rounded to the nearest millisecond**, so a row written
  400 µs before a minute boundary was placed in the next minute. Now an exact integer floor.
- **An absurd recorded latency was unbounded**: 1e12 ms placed a row in 1995 and 1e15 ms
  raised. A latency above 24 hours is now unusable, like a negative one, and counted
  (`rows.unusable_latency`).
- **`latency_exceeds_bucket`** was strictly `> T` and is removed (A2).
- A test named for a burst being averaged away asserted, in its 600 s case, that the peak
  was unchanged. It is now
  `test_the_peak_depends_on_the_bucket_and_the_window_mean_does_not`, and says what each
  bucket length does.

### A6. Decisions the revision forced

- **The window ends at the last recorded request's end**, not at the last arrival bucket
  (§6.1). With spreading, a request is in service after the bucket it arrived in; a window
  that stopped at the last arrival would cut the last requests short.
- **Service time past the window is reported, not placed** (`service_ms_after_window`). It
  arises only for an asserted, mean or replayed service time on a request near the end.
  Extending the window to cover it would make the window — and so the current arm's means —
  depend on the proposal, breaking C17.
- **The window-too-short rule counts buckets with an arrival** (§6.3), the conservative
  count. `window.active_buckets` is renamed `window.arrival_buckets`, because "active" now
  means something else per endpoint.
- **`buckets_over_share` is a share of the window**, to read with `mean_window`.
- **An asserted (or replayed) latency above 24 hours is refused** when a row lands on the
  endpoint (§5.4). The proposal schema allows any positive latency; a service time that long
  cannot be placed, and ignoring it would break C15.
- **`not_modelled`: `per_request_latency` → `moved_request_latency`.** The first id would
  now be false: per-request latency is modelled for rows that stay.
- **The endpoint mean is over the simulated rows**, not every record after `--since`
  (§5.4): the capacity engine sees the rows replay returned, and a cache hit's latency is not
  a service time.
- **The caveat (§9.1) is rewritten**, once, with `CAPACITY_CAVEAT` and its pin test: it
  describes spreading and the service-time rule, says what a recorded latency includes, and
  says what a peak is. The sentence "Replay gives each endpoint one latency…" is gone
  because it is no longer true.
- **C18's worked figure.** The revision brief gave `mean_window ≈ 0.019` for the
  twelve-request example; that is 12 × 138 s over a 24-hour day. Over the window as defined
  (1,323 one-minute buckets) it is 0.0209, which is what the test pins.

### A7. What is still true of the first design

Inputs (§3), volume scale (§4), which rows and which tokens (§5.1, §5.3), what is not in the
demand (§5.5), no predicted rejection (§9.2), no range (§9.3), never a recommendation
(§9.4), `inputs.capacity` (§3.5), the flags and their defaults, the exit codes, and the
byte-identity of every output when no capacity is stated.
