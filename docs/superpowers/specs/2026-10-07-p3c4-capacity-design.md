# P3c-4 — a capacity model for the policy simulator

**Date:** 2026-10-07 · **Status:** design for review
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
   loaded it would be: peak and mean utilisation of each stated limit, how much of the time
   and of the traffic is above 100%, and when the peak is.
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
| `--capacity-min-buckets K` | `10` | integer `≥ 1` | fewer buckets with traffic → "window too short" (§6.3) |

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
- a flag value out of range.

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

**What it does.** Every routed request of an application counts `volume_scale` times in the
bucket it arrived in: requests, tokens and offered concurrency alike. Utilisation is
therefore exactly linear in it.

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
`token_scale` moves neither the request rate nor the replayed latency, so it does not move
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

using the row's own recorded latency (a missing, negative or non-finite one subtracts
nothing). That is a property of the request, so it is the same in both arms.

Buckets are `--capacity-bucket` seconds long and aligned to the Unix epoch: a row's bucket
is `floor(arrival / bucket)`, in integer milliseconds. It does not depend on which other
rows the export holds, and with the default 60 s a bucket is a UTC minute. A naive timestamp
is read as UTC, as `--since` reads it; offsets are honoured, so daylight-saving changes do
not move a row.

A simulated row with **no `ts`** cannot be placed. It is counted (`rows.without_ts`) and is
in no bucket.

**Why 60 s.** Provider quotas are per minute, and it is long against most request
latencies. It is short against a multi-minute generation, which §5.4 flags.

### 5.3 The four demands

For endpoint `e`, arm `a`, bucket `b` of length `T` seconds, over the rows `r` that `a`
routes to `e` and that arrived in `b`, with `v(r)` the volume scale of the row's
application:

```
requests/min        = Σ v(r)                                      × 60 / T
tokens/min          = Σ v(r) × (prompt_tokens(r) + completion_tokens(r)) × 60 / T
output tokens/min   = Σ v(r) × completion_tokens(r)               × 60 / T
offered concurrency = Σ v(r) × latency(a, e)                      / (T × 1000)
```

**Which token count, and why.**

- `prompt_tokens` is the count replay **priced** the call at: the backend's own billed count
  when the row has one, scaled by `token_scale`; RouteForge's `routing_tokens` only when no
  backend answered. An endpoint's limit is about what the endpoint processes, in its own
  tokenisation; `routing_tokens` is RouteForge's estimate of that and ran 1.5× high on the
  one run that compared them (B64). It is used only where nothing better exists, and those
  rows are counted (`rows.estimated_tokens`).
- `completion_tokens` is the recorded completion count — what was generated — not the
  client's `max_tokens`. For a row with no backend usage it is the router's output estimate
  (`max_tokens`, else `default_estimated_output_tokens`), the same value the row is costed
  at.
- **`tokens_per_minute` is prompt + completion**, because that is how a provider's TPM quota
  counts a call. A provider that reserves `max_tokens` at admission counts more than this;
  demand against such a quota is understated.
- **`output_tokens_per_minute` is completion only**, for a generation-throughput limit.
- Cached prompt tokens are not subtracted. `workload.cached_share` moves cost, not load.
- A request's tokens are all counted in the bucket it **arrived** in.

**Offered concurrency** is the Erlang offered load of the bucket: service time arriving per
unit time. It is the mean number of requests that would be in service if none ever waited.

### 5.4 Latency: one per endpoint, not one per request

`latency(a, e)` is the arm's **replayed** latency for the endpoint. Replay's ladder is a
function of the endpoint alone: the proposal's asserted `latency_ms`, else the median
observed latency of the export's single-attempt served rows, else the snapshot's signal,
else the tier default. Consequences, all stated in the output:

- Offered concurrency is the request rate times one constant. It carries no information
  about which requests are long.
- **A row moved to an endpoint it never ran on gets that endpoint's latency**, not its own
  recorded one. That is the right default for "what would this endpoint be doing", and it
  is an assumption.
- An endpoint whose latency is a **tier default** has no measurement behind its
  concurrency at all. The block carries `latency_source` per endpoint per arm and the report
  prints a note for `tier_default`.
- When the replayed latency is longer than a bucket, a request's whole service time is
  counted in the bucket it arrived in, which overstates that bucket and understates the
  next. The block carries `latency_exceeds_bucket` and the report says to use a longer
  bucket.

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

### 6.1 Buckets that hold traffic

Let `A` be the set of buckets in which **any simulated row** arrived — routed or rejected,
by any application. `A` is the same for both arms, every endpoint and every scenario,
because they all replay the same rows.

Means and shares are over `A`, not over the whole window. An export of two test sessions a
week apart spans ten thousand one-minute buckets, of which twenty hold traffic; a mean over
ten thousand would be noise. The block carries both counts (`window.buckets`,
`window.active_buckets`) so a sparse export is visible.

### 6.2 Figures, per endpoint, per arm, per stated limit

With `d_b` the demand in bucket `b` (zero where the endpoint has none) and `L` the limit:

| Field | Definition |
|---|---|
| `limit` | `L`, as stated |
| `peak` | `max_b d_b / L` |
| `mean` | `(Σ_b d_b / |A|) / L` |
| `peak_bucket` | start of the earliest bucket at the peak (ISO-8601 UTC); null with no demand |
| `buckets_over` | number of buckets with `d_b > L` |
| `buckets_over_share` | `buckets_over / |A|` |
| `requests_over_share` | this endpoint's requests arriving in those buckets ÷ all its requests; null with no demand |

Demand of exactly `L` is 100%, which is not above 100% (a `1e-9` relative tolerance keeps
float noise from deciding).

Demand itself — `peak` and `mean` of each of the four demands — is reported for every
endpoint that has routed rows in either arm, **whether or not a limit is stated for it**.
Traffic that lands on an endpoint nobody stated a capacity for must not be invisible; it
gets no utilisation.

`peak` for an arm (the comparison-table figure) is the largest `peak` over that arm's stated
limits, with its endpoint, limit and bucket; ties go to the first in (slug, limit) order;
null when the arm has no stated limit.

### 6.3 Window too short

If `|A| < --capacity-min-buckets` (default 10), the block says `window_too_short: true` and
carries **no endpoint figure at all**: `endpoints` is empty and `peak` is null in both arms.
The comparison-table cell reads `window too short (k of the 10 buckets needed)` and the
section prints one sentence.

Ten is a floor against reporting a "peak" of three buckets, not a claim that eleven buckets
describe a deployment. The section always prints how many buckets hold traffic.

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
             "buckets": 30, "active_buckets": 30},
  "rows": {"simulated": 160, "without_ts": 0, "estimated_tokens": 0,
           "excluded_not_counted": 0},
  "volume_scale": {"base": {}, "proposal": {"*": 2.0}},
  "not_modelled": ["queueing", "batching", "autoscaling", "engine_concurrency",
                   "external_load", "sub_bucket_bursts", "failover_load",
                   "per_request_latency", "overload_behaviour"],
  "window_too_short": false,
  "endpoints": {
    "local-cheap": {
      "current": {
        "requests": 160.0,
        "latency_ms": 6000.0, "latency_source": "observed",
        "latency_exceeds_bucket": false,
        "demand": {"concurrency": {"peak": 1.2, "mean": 0.533333},
                   "requests_per_minute": {"peak": 12.0, "mean": 5.333333},
                   "tokens_per_minute": {"peak": 12000.0, "mean": 5333.333333},
                   "output_tokens_per_minute": {"peak": 1200.0, "mean": 533.333333}},
        "limits": {"max_concurrency": {"limit": 1.0, "peak": 1.2, "mean": 0.533333,
                                       "peak_bucket": "2026-09-20T12:20:00+00:00",
                                       "buckets_over": 10, "buckets_over_share": 0.333333,
                                       "requests_over_share": 0.75}}},
      "proposed": {"…": "the same shape, at twice the volume"}
    }
  },
  "peak": {"current": {"endpoint": "local-cheap", "limit": "max_concurrency",
                       "utilisation": 1.2, "bucket": "2026-09-20T12:20:00+00:00"},
           "proposed": {"endpoint": "local-cheap", "limit": "max_concurrency",
                        "utilisation": 2.4, "bucket": "2026-09-20T12:20:00+00:00"}}
}
```

`endpoints` lists every slug with a stated limit in either arm or a routed row in either
arm, sorted. `limits` holds only the limits stated for that arm. `requests` is the
volume-scaled count. Floats are rounded to 6 places like the rest of the document.

### 7.2 `comparison.json` (`--scenarios`)

- One added top-level key: `capacity: {"scenarios": [{"name": …, …the §7.1 block}]}`, in
  file order. Each entry is exactly the block `--proposal` gives for that body. The
  `current` arm is repeated in every entry; it is identical in all of them by construction.
- Each `table` row gains three keys: `peak_utilisation`, `peak_endpoint`, `peak_limit` — the
  proposed arm's `peak` (§6.2). All three are null when no limit is stated in that arm or the
  window is too short.
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

Buckets of 60 s by estimated arrival time. 30 of 30 bucket(s) in the window hold traffic
(2026-09-20T12:00:00+00:00 to 2026-09-20T12:29:00+00:00); 160 simulated row(s). Means are
over the buckets that hold traffic.

Volume scale assumed by the proposal (proposed arm only, multiplying any stated for both
arms): *=2.

Peak load: current 120.0% (local-cheap, max_concurrency); proposed 240.0% (local-cheap,
max_concurrency).

### local-cheap

Requests: current 160, proposed 320. Replayed latency: current 6000 ms (observed), proposed
6000 ms (observed).

| limit | arm | stated | peak | mean | buckets over 100% | requests in those buckets | peak bucket |
|---|---|---|---|---|---|---|---|
| max_concurrency | current | 1 | 120.0% | 53.3% | 10 of 30 | 75.0% | 2026-09-20T12:20:00+00:00 |
| max_concurrency | proposed | 1 | 240.0% | 106.7% | 10 of 30 | 75.0% | 2026-09-20T12:20:00+00:00 |

| demand | current peak | current mean | proposed peak | proposed mean |
|---|---|---|---|---|
| concurrency | 1.2 | 0.533333 | 2.4 | 1.06667 |
| …
```

Notes are printed only when they apply: rows without a timestamp, excluded rows not in the
demand, rows sized from an estimate, a latency that is a tier default, a latency longer than
a bucket. A limit stated in one arm only prints `not stated` in the other. An endpoint with
no stated limit prints "No limit is stated for this endpoint; demand only."

### 8.2 Scenario comparison

- The comparison table gains one column, last: **`peak load (proposed)`**, e.g.
  `230.0% (local-llama, max_concurrency)`, `not stated`, or the window-too-short state. The
  table stays in file order. Under it: "Peak load is the proposed arm's most loaded stated
  limit and the endpoint it is on. It is listed in file order like every other column: a
  lower figure does not make a scenario the one to choose."
- One `## Capacity` section: the caveat, the no-remedy sentence, the shared window, and
  "Current arm peak load (the same for every scenario): …".
- Each scenario's section gains `### Capacity`: its volume scale, both arms' peak load and
  the per-endpoint tables.

## 9. What the figures are, and are not

### 9.1 The caveat

Every capacity section opens with this, verbatim (a test pins it):

> **Stated capacity against replayed demand.** Every limit below is a figure the operator
> stated; RouteForge has not measured it and does not manage capacity. Demand is the replayed
> route of each recorded request, grouped into buckets by estimated arrival time (the time
> the record was written minus its recorded latency). Nothing here models queueing, batching,
> autoscaling, the serving engine's real concurrency behaviour, load that reaches an endpoint
> without passing through RouteForge, bursts shorter than a bucket, or the load of failover
> attempts. Replay gives each endpoint one latency, so offered concurrency is the request
> rate times that latency. RouteForge does not know what an endpoint does above a stated
> limit (queue, shed or slow down), so no rejection and no delay is predicted. An export of
> a few buckets says little.

`capacity.not_modelled` carries the same exclusions as ids: `queueing`, `batching`,
`autoscaling`, `engine_concurrency`, `external_load`, `sub_bucket_bursts`, `failover_load`,
`per_request_latency`, `overload_behaviour`.

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
   variability the model does not have (one latency per endpoint, arrivals known only per
   bucket). "Requests beyond capacity in a bucket" is a lower bound that reads like an
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

| # | Invariant |
|---|---|
| C1 | With no capacity stated anywhere, every output of every mode is byte-identical to the tree before this slice (the P3c-3 golden files, not regenerated). With capacity, only `capacity`, `inputs.capacity` and the three table keys are added; a proposal's `capacity` and `volume_scale` never reach the applied document or the `assumptions` block. |
| C2 | Deterministic: the same inputs give the same bytes; the block does not depend on row order. |
| C3 | Conservation: per arm, Σ over endpoints and buckets of requests = the rows the arm routed (that have a `ts`); the same for tokens, output tokens and service time; totals do not depend on the bucket length. |
| C4 | Utilisation and demand are linear in `volume_scale` and utilisation is inversely linear in a stated limit; a limit moves no demand; `volume_scale` moves no route and no cost. |
| C5 | A no-op proposal gives identical demand and utilisation in both arms. |
| C6 | Removing an endpoint moves its demand to the endpoints replay routes those rows to: per-endpoint demand equals the replayed picks, and the gains equal the summary's own `moves`. |
| C7 | A rejected row adds no demand. |
| C8 | Fewer than `--capacity-min-buckets` buckets with traffic gives `window_too_short` and no utilisation figure in the JSON, the table or the report; the threshold is inclusive. |
| C9 | `--uncertainty` leaves capacity as points: the capacity block and the uncertainty block are each unchanged by the other, and the report says why. |
| C10 | No ranking, recommendation or remedy vocabulary in any key or sentence; file order kept. |
| C11 | No serving module imports `app.eval`. |
| C12 | The caveat is the spec's text and opens every capacity section; `not_modelled` has the nine ids. |
| C13 | The plan is in `inputs.capacity`; ids tell plans apart. |
| C14 | On a synthetic export with a known arrival pattern, every figure equals its closed form; demand of exactly the limit is not "over". |
| C15 | Nothing stated silently does nothing: an unknown slug, an unknown key, a limit `≤ 0`, a bad `volume_scale`, a tuning flag or a `volume_scale` with no capacity stated, and `--capacity` with no proposed arm are each exit 2 naming the location. |
| C16 | A row is placed by estimated arrival (`ts` − recorded latency) in an epoch-aligned bucket, whatever the timestamp's offset; a row without `ts` is counted and in no bucket. |
| C17 | In a comparison each scenario's block is the single-proposal block for that body, and the `current` arm is the same in every scenario. |

## 11. Testing

- **The golden pin.** `tests/eval/test_uncertainty_golden.py` (two cases × four modes, files
  written before P3c-3) must pass untouched. No golden file is regenerated.
- **A hand-computed export (C14).** One endpoint, 20 minutes at 2 requests a minute then 10
  at 12; each request 6 s, 900 prompt + 100 completion tokens; limits 1 concurrent, 10
  req/min, 10,000 tok/min, 500 output tok/min. By hand: a quiet minute is 20% / 20% / 20% /
  40%, a busy one 120% / 120% / 120% / 240%; the mean is 53.33% (106.67% for output); 10 of
  30 buckets and 75% of requests are over. At `volume_scale: 5` a quiet minute is exactly
  100% and is not over. Checked in the engine and through the whole CLI.
- **Every demand figure comes from the real replay** (`simulate()` → `feasible_set` →
  `Router`), never from a guess at where a fixture routes. C6 compares against the replayed
  picks and the summary's `moves`.
- **Input classes likely to bite:** identical timestamps; no timestamp; mixed UTC offsets
  for one instant; a one-bucket export; a two-session export a week apart; an endpoint with
  a limit and no rows; an endpoint with rows and no limit; a limit of 0; a slug not in the
  snapshot; a limit on a removed endpoint; a latency longer than the bucket; a tier-default
  latency; `volume_scale` with small applications pooled under `--uncertainty`.
- `tests/eval/test_isolation.py` unchanged.

## 12. Docs

- `docs/reference/policy-simulator.md`: a new §11 (inputs, the four limits, flags, the
  method, output shape, reports, the caveat, limits); the `capacity` and `volume_scale` keys
  in §2; the flags and exit-2 cases in §3; the keys in §4.1 and §9.4; §6, §8.7, §9.6 and
  §10.10 no longer say capacity is out of scope.
- `docs/research/feasible-route-bench.md`: a new §21 — the model, what it is not, the
  invariants and their tests, results (synthetic verification only; every measurement on
  recorded traffic **TO MEASURE**, labelled), limits.
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
3. **Measured service time per request.** One latency per endpoint is the largest
   simplification here. The export already has a per-row latency for rows that stay on their
   recorded endpoint; using it, and a latency-versus-tokens model for rows that move, would
   need its own design and validation.
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
7. **Means and shares are over buckets that hold traffic**, not over the whole window
   (§6.1).
8. **`on_overload` is not built** (§9.2). The roadmap's "predicted-rejection figure" is not
   produced.
9. **`ArmResult` gains `prompt_tokens` and `completion_tokens`**, defaulted and serialised
   nowhere, so demand counts the tokens that were costed.
10. **Demand is reported for endpoints with no stated limit**, without a utilisation.
11. **`latency_source` and `latency_exceeds_bucket` are reported per endpoint per arm.** Not
    in the brief; each marks a concurrency figure that rests on less than it appears to.
12. **Capacity with `--shadow-prices` alone is refused**, not ignored.
