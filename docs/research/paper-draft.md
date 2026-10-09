# Eligibility-Constrained LLM Routing: When the Candidate Pool Is a Per-Request Variable

**Draft manuscript, 2026-10-09.** Source of every number: [`arxiv.md`](arxiv.md) and the
research notes it points to (FB = [`feasible-route-bench.md`](feasible-route-bench.md), PC =
[`policy-compiler-noninterference.md`](policy-compiler-noninterference.md), SP =
[`routing-signal-provenance.md`](routing-signal-provenance.md)). Section references in
brackets are for the author and come out before submission.

> **Before this is posted.**
> 1. **Related work (§9) is not verified.** The papers listed there are leads from the
>    author's notes. None has been read against this draft, and some identifiers may not
>    exist. No sentence in this draft relies on what any of them says.
> 2. **Authors, affiliation, acknowledgements** are not filled in.

---

## Abstract

LLM routers choose a model for each request by trading cost, quality and latency over a
fixed pool of candidates. In a regulated, multi-tenant deployment the pool is not fixed:
where compute may run, whether a provider keeps prompts, which region it is in, what a
tenant has allowed and what a request may cost decide which endpoints may see a request at
all, before anything is scored. We describe RouteForge, a self-hosted, OpenAI-compatible
router that makes this *eligible set* a derived, per-request, fail-closed quantity, and we
study what follows from treating it that way. We contribute (i) a model that separates
placement from protection, with a noninterference property checked by a property-based
harness that found two fail-open defects; (ii) an evaluation harness whose oracle never
credits an endpoint a request was not allowed to use; and (iii) a simulator that replays
content-free routing records through the production eligibility and routing code to
predict the effect of a policy change, with the smallest set of conflicting requirements
for a rejected request, the price of each requirement, uncertainty ranges and a capacity
reading. On four live runs of about 900 requests each, with two on-premises models and
two cloud models, no request restricted to on-premises compute was attempted on a cloud
endpoint (0 of 987); replay reproduced every routing outcome, and every model choice when
endpoints were answering; a replayed prediction of 180 requests reaching the cloud (90%
range 163 to 199) was followed by a recorded 178; and keeping two applications
on-premises was priced at +$3.74 over 900 requests (3.20 to 4.30) at stated prices. We
also report what the system does not show: its router does not choose well among eligible
models (a fixed "cheapest eligible" rule passed more benchmark cases, twice), no saving
against a frontier-only baseline was measured, and relaxing a requirement can, by
construction, cause a new rejection. Finally, we show where request-level eligibility
stops: in a live three-turn conversation, a placement restriction raised by a tool result
did not survive the client compacting its history, and in one recorded agent session
94.5% of input tokens had been sent before. We state the trajectory-level problem these
two observations point to.

---

## 1. Introduction

A router that sends each request to the cheapest model good enough for it is a familiar
idea. The usual formulation takes a set of models as given and optimises over it. That is
a reasonable model of a single team calling public APIs. It is a poor model of the
deployments that most need a router: a hospital, a bank or a public agency running several
applications against a mix of their own GPUs and contracted cloud endpoints.

In those deployments the question that comes first is not *which model is best* but
*which models may see this request*. The answer depends on the application (it may be
confined to on-premises compute), on the request (a detected identifier may confine it
further), on what the operator has declared about each endpoint (its region, whether it
keeps prompts, whether it may receive unredacted sensitive text), and on a budget. It
changes from request to request. We call the result the **eligible set** and write
`E(request)`.

Treating `E` as a first-class quantity has consequences that this paper works through:

- **It must be computed before scoring and fail closed.** If eligibility is a penalty in
  a score, or a check made after a choice, an optimiser can trade it away or a refactor
  can skip it. If `E` is empty the request is rejected, never sent to the nearest
  alternative.
- **Evaluation must respect it.** An oracle that credits the globally best model
  overstates the regret of any router that obeyed the policy.
- **Policy becomes something an operator can ask questions about.** If the records of
  past routing decisions contain every input the router read, and no request content, a
  proposed policy can be replayed against real traffic before anyone applies it.

**Contributions.**

1. A model of policy-derived eligibility in which placement and protection are separate
   axes with a noninterference property, and a property-based harness for it (§3).
2. FeasibleRouteBench, an offline harness with a feasible oracle and regret metrics that
   never credit an ineligible endpoint (§4).
3. A counterfactual policy simulator over content-free records: scenario comparison,
   minimal conflicting cores, empirical prices of single requirements, uncertainty ranges
   and a capacity reading (§5).
4. Live measurements on a GPU lab with on-premises and cloud endpoints (§6), and a set of
   negative results that we consider as useful as the positive ones (§7).
5. A statement of *trajectory-level* feasible routing — eligibility carried along the
   sequence of calls an agent makes — motivated by a live counterexample to the
   request-level model and by the token profile of a real agent session (§8).

We do not claim a better routing algorithm. The scoring rule is simple and, as §6.5 and
§7 show, not good at choosing among eligible models. The contribution is the constraint
and the tooling built on it.

---

## 2. System

RouteForge sits between applications and OpenAI-compatible model endpoints. It deploys no
models: an operator onboards endpoints they run, and may declare cloud providers in
configuration. Each request passes, in order, through a policy gate, an immutable
execution context, optional context compression, the router, a gateway with failover and
circuit breakers, and output verification.

Three ordering rules carry the privacy claim:

- Nothing outside the deployment sees a request before the gate has run.
- An application or request confined to on-premises compute has cloud providers removed
  from its candidates **before** scoring.
- If the gate errors, or no endpoint is eligible, the request is rejected.

**What an operator declares.** Per application: placement (`any`, `private_cloud_ok`,
`onprem_only`), allowed regions, whether a provider may keep prompts, allowed tiers,
providers and models, a per-request cost cap, and a protection posture. Per endpoint:
tier, price, region, prompt retention, and whether it accepts unredacted sensitive text.
RouteForge enforces policy against these declarations. It cannot verify a provider's
contractual claim, and an undeclared attribute never satisfies a requirement.

**The router.** The target tier is a function of request length:
`complexity = min(1, tokens / 2000)`, with thresholds 0.35 and 0.70 separating cheap, mid
and frontier. It is not a semantic estimate. Among eligible endpoints of the target tier
the router maximises

`w_q · quality − w_c · (cost / max cost) − w_l · min(latency / scale, cap) + w_a · availability`

with shipped weights 1.0, 0.6, 0.3 and 0.2. Latency and availability are measured per
endpoint; quality is a published score if the operator supplied one and otherwise a
default for the tier (0.5, 0.75, 1.0). Each term is recorded with where its value came
from, so a ranking can be recomputed offline [SP §2].

**What is recorded.** Every routed request writes one content-free record: application,
detected entity *types*, placement, target tier, the eligible set, each rejected endpoint
with a reason code, the attempt chain, the chosen endpoint, token counts, cost, latency,
and the versions of the application policy, the endpoint registry and the routing
configuration. It contains no prompt or completion text.

---

## 3. Eligibility, placement and protection

**Two kinds of axis.** *Placement* governs where compute may run and is a single ordered
axis. *Protection* governs what the model is shown, what the client receives, what
telemetry stores, which detector runs, and whether lossy transformations and streaming are
allowed; it is a product of independent ordered axes. An application's effective policy
is a point in the product of the two. Each axis is resolved by the same precedence:
deployment ceiling, then the application's explicit value, then a preset, then the
deployment default. A value that cannot be parsed resolves to the protective end of its
axis.

**Noninterference.** Changing a protection setting never changes placement, and changing
placement never changes a protection value:

- **NI-1.** For any protection axis `a` and values `v`, `v′`, the placement component of
  the compiled policy is the same under `a = v` and `a = v′`.
- **NI-2.** Symmetrically for placement against every protection axis.

The one permitted interaction is declared and raise-only: a sensitivity rule may raise
placement when a detector finds a named entity type, and every rule that fires is named
on the response and the record.

**Eligibility.** Given the compiled policy and the endpoint registry, the eligible set is
computed in one place. It is antitone in placement: tightening a policy never enlarges
it. An empty set rejects the request.

**Checking it.** Eight invariants are stated as ten property functions over generated
policies and registries (200 examples by default, 2,000 in a deeper profile). Against 18
hand-written mutants of the compiler the harness catches 15 [PC §4.2]. On its first run
it found a fail-open defect: an empty placement string compiled to `any` through a
default-on-falsy idiom. A second defect came from two code paths each deciding placement:
the compiler said `onprem_only` while the request context still listed two cloud
providers; the fix was a single authority [PC §5]. We note one methodological finding: a
mutation score measured against generators that cannot reach the mutated line overstates
adequacy, and we report the score only after closing that gap [PC §8].

**Explaining a rejection.** When `E` is empty, each endpoint fails some set of
requirements. A *conflicting core* is a set of requirements that together exclude every
endpoint; the smallest such set is a minimum hitting set, found exactly by search over at
most eleven requirement identifiers. Endpoint *state* (disabled, breaker open) is reported
separately and is never part of a core, and no remediation hint proposes relaxing a
security requirement [FB §16].

---

## 4. Evaluating a router that has to obey a policy

For a case `c` with eligible set `E(c)`, quality `q(c, e)` and a quality floor `f(c)`:

- the best achievable quality is `q*(c) = max over e in E(c) of q(c, e)`;
- the passing set is `P(c) = { e in E(c) : q(c, e) ≥ f(c) }`;
- the cost and latency oracles are the minima over `P(c)`.

FeasibleRouteBench reports, for each routing strategy: the **violation rate** (a choice
outside `E(c)`, which fails the run), pass rate and quality regret, cost and latency
regret over cases a strategy passed, and correct handling of empty-set cases. The oracle
never credits an endpoint outside `E(c)`. Results are also reported per group of cases
(placement, protection, application, workload) with the case count beside every figure,
and no figure for a group below a minimum size.

On top of this sits an **offline version check**: a candidate routing configuration is
scored beside the current one on the same cases, the paired difference of each named
metric is bootstrapped over cases, and a metric is judged on its **whole range** — a
candidate is `regressed` only if the entire range lies beyond the tolerance,
`within tolerance` only if the entire range lies inside it, and `inconclusive` otherwise.
An earlier rule that judged the point estimate called a no-change candidate regressed in
66% to 97% of synthetic null draws; the whole-range rule does so in 2% to 31%
[FB §22.7–§22.8]. The check applies nothing; a routing change is still made by an
operator.

In simulation, where quality is an input, the harness is exercised with failure
injection. A closed form follows for when a dead endpoint loses the ranking: only if
`w_a · (1 − availability) > w_c · (1 − p)`, where `p` is its price relative to its peer.
At the shipped weights a dead endpoint cheaper than two thirds of its peer's price still
ranks first, which motivated a circuit breaker [FB §9.5, §12].

---

## 5. Predicting the effect of a policy change

**Replay.** The simulator reads an export of outcome records and a configuration
snapshot, and runs each record through the production context builder, eligibility
function and router twice: under the current configuration and under a proposal. The
reported effect is the difference between the two simulated arms. Agreement between the
current arm and what was recorded is a fidelity check, reported with every result and
usable as a gate.

Because the records were produced by a deterministic router, they contain no overlap
between policies, and off-policy estimators do not apply. Replay is possible only because
routing is a pure function of recorded inputs.

**Scenarios.** Several proposals are compared side by side against one shared current
arm: routes changed, requests newly rejected and newly served, change in cost, latency,
quality and on-premises share. The report lists scenarios in file order and never ranks
them. Cost differences are computed only over requests routed in both arms; a newly
rejected request is never counted as a saving.

**The price of a requirement.** For one application and one requirement, relax it, replay,
and difference. The result is an empirical price, not a dual variable. For a security
requirement it is labelled "price of a protection — not a recommendation".

**Uncertainty.** Each difference carries a percentile range from a paired bootstrap of the
replayed records, stratified by application. A count of zero is printed with a one-sided
bound on its rate, not as zero risk [FB §20].

**Capacity.** An operator states limits per endpoint (concurrency, requests and tokens per
minute). Replayed demand in each arm is bucketed in time and compared with the stated
limits, reporting peak, time-average and the share of buckets over the limit, always with
the bucket length [FB §21]. It reads stated limits; it does not predict queueing.

---

## 6. Experiments

### 6.1 Setup

All live runs are on one GPU lab running the production build.

| endpoint | provider | tier | stated price in/out, $ per 1M tokens | prompt retention |
|---|---|---|---|---|
| Qwen3.6-35B-A3B (FP8, one H200) | on-premises | cheap | 1 / 1 | none |
| GLM 5.3 Flash | on-premises | mid | 10 / 10 | none |
| Nemotron 3 Super 120B (free variant via an aggregator) | cloud | mid | 0.08 / 0.45 | retained |
| Nemotron 3 Ultra 550B (free variant via an aggregator) | cloud | frontier | 0.50 / 2.20 | retained |

On-premises prices are illustrative. Cloud prices are the paid list prices of the same
models; the free variants were billed nothing. **No absolute cost in this section is a
real bill.**

Three applications: *claims* (per-request cap $0.05), *dev*, and *clinic* (on-premises
only, one allowed region, no retention). Traffic is synthetic: each request is drawn
independently (application, kind of text among prose, code, logs and JSON, a heavy-tailed
length, and with some probability a planted SSN and e-mail address). Runs 1 and 2 used
one model; runs 3 and 4 used the four endpoints above.

| run | endpoints | rows | what it adds |
|---|---|---|---|
| 1 | one on-premises model | 912 | first independent rows; replay baseline; range behaviour |
| 2 | one model under three declared names | 900 | declarations, a designed 181.5 s outage |
| 3 | four models | 899 | cloud path; the cloud provider's quota ran out after 6 of 30 minutes |
| 4 | four models | 900 | cloud path working throughout |

### 6.2 Placement holds

In runs 3 and 4, 641 rows belonged to the on-premises-only application and 346 rows from
the other two applications were raised to on-premises placement because an entity was
detected. **None of the 987 was attempted on a cloud endpoint** [FB §25.3, §26.2]. A
request from the on-premises-only application that names a cloud model is refused with
the reason. In every benchmark run, simulated and live, no strategy chose an endpoint
outside a case's eligible set.

A pattern detector has a cost here that is easy to miss. In run 4, 83 of the 173
detection-raised requests carried no planted identifier: random digits in synthetic logs
and JSON matched credit-card and bank-number patterns. The error is in the safe
direction — those requests stayed on-premises — and it is paid for at the on-premises
price.

### 6.3 Replay fidelity

| run | endpoints during the run | outcome agreement | model agreement |
|---|---|---|---|
| 1 | answering | 1.0 over 912 | 1.0 |
| 2 | one endpoint down for 181.5 s | 1.0 over 900 | 0.926 over 880 |
| 3 | cloud provider down for 24 of 30 min | 1.0 over 899 | 0.919 over 878 |
| 4 | answering (one endpoint refusing a third of calls) | 1.0 over 900 | 1.0 over 878 |

Replay reproduced whether each request was served or rejected in all 3,611 rows. Model
agreement was complete when endpoints were answering. In run 2 all 65 disagreements were
requests the live router diverted because a breaker was open. In run 3 replay sent 180
requests to a provider that answered 31, and its baseline cost was 26% below the recorded
cost; in run 4 the two costs were within 1.2%. **Replay describes the deployment with its
endpoints healthy.** It does not model breaker or failure state, and its figures should be
read that way.

Fidelity also depends on recording every input the router reads. An early deployment did
not record a client's explicit model choice; replay agreement was 0.0206 over 194 rows,
behind a report that looked plausible. After recording it, agreement was 1.0 [FB §15.4].
Similarly, rejected requests once recorded a token count of zero and replayed as served
(7 of 7) until the router's own token count was recorded on every row [FB §19.4].

### 6.4 What a policy change costs

**Keeping two applications on-premises** (run 4, 900 rows, replayed baseline $7.02):

| proposal | routes changed | newly rejected | Δ cost, $ |
|---|---|---|---|
| on-premises only for *claims* and *dev* | 178 [160, 197] | 0 | +3.74 [3.20, 4.30] |
| "no prompt retention" for *claims* and *dev* | 178 [160, 197] | 0 | +3.74 [3.20, 4.30] |
| let *clinic* use the cloud | 63 [52, 75] | 0 | −0.89 [−1.19, −0.65] |
| raise the per-request cap tenfold | 15 [9, 22] | 0 (22 newly served) | +0.76 [0.46, 1.15] |

Ranges are 90%. Two different requirements — a placement and a retention rule — produce
the same 178 moves, because the cloud endpoints are declared as retaining prompts; the
report shows each under its own name. Run 3 gave +$3.12 [2.69, 3.54] for the first
proposal on a separate sample; the ranges overlap.

**A prediction followed by a measurement.** Run 3's replay was computed while the cloud
provider was refusing every call, and put 180 requests [163, 199] on the cloud under the
current policy. Run 4, an independent draw from the same generator with the provider
answering, recorded 178. This is one agreement, on synthetic traffic.

**A static check can call a tightening free when it is not.** In run 2, restricting two
applications to one region left another endpoint eligible for every application, so a
check of the configuration alone finds nothing lost. Replay found 5 requests [2, 9]
rejected, because the remaining endpoint was over those requests' budget; a retention
rule likewise rejected 4 [1, 7] [FB §24.5]. In runs 3 and 4, with an inexpensive
on-premises endpoint available, the same kind of tightening rejected none. The effect
depends on what else is eligible for the traffic that actually arrives, which is the
argument for replay over inspection.

**Capacity.** In run 4, moving everything on-premises takes the on-premises mid endpoint
from 50.0% to 90.9% of its stated limit in its busiest minute (time-average 30.3% to
41.1%), a consequence the cost table does not show [FB §26.6].

**Ranges.** On run 1's 912 independent rows the width of a range shrinks as `1/√n` from
`n = 200`. Splitting the export in half 100 times, the range from one half contained the
other half's point estimate 66% to 76% of the time, against a reference of 75.5% for a
calibrated 90% range of a difference between two independent halves [FB §23.7]. Coverage
has not been measured directly, nor under the dependence of agent-loop traffic.

### 6.5 Routing quality on the benchmark

22 cases, one call per case and eligible endpoint, a deterministic grader.

| | run 3 | run 4 |
|---|---|---|
| cheap on-premises model, cases passed | 18 of 22 | 19 of 22 |
| mid on-premises model, cases passed | 16 of 22 | 16 of 22 |
| "cheapest eligible" strategy, pass rate | 0.818 | 0.864 |
| RouteForge router, pass rate | 0.682 | 0.727 |
| violations, every strategy | 0 | 0 |

The cheap model passed more cases than the mid model both times, and a rule that always
picks the cheapest eligible endpoint beat the router on pass rate at about a tenth of the
cost. With no published quality for either model, the router's quality term is a default
for the tier, and for this pair the tier order is wrong. The suite is small and each case
is sampled once; we read it as evidence that a tier is not a quality measurement, not as
a ranking of two models.

Where two endpoints shared a tier and a quality default, the cost term decided: all 84
unrestricted mid-tier requests in run 4 went to the cloud endpoint.

**The version check on live endpoints.** Two candidates that changed scoring weights
altered no route on this suite (the tier is set by length, and each tier offered one
endpoint), and passed trivially. A candidate that removes the cheap tier changed 12 of 22
routes. Its mean chosen cost was worse by 0.00062 [0.00003, 0.00154] against a tolerance
of 0.0005, and the verdict was `inconclusive`: a real worsening that the check neither
passed nor called a regression on 22 cases [FB §26.7]. In simulation the check catches 15
of 15 single-rule mutations of its own detectors and flags planted regressions [FB §22.5].
A `regressed` verdict on live endpoints has not been observed.

### 6.6 Failures

| | run 2 | run 3 | run 4 |
|---|---|---|---|
| cause | one endpoint refused for 181.5 s | cloud quota exhausted for 24 min | one cloud endpoint refused 35 of 94 calls |
| requests failed | 2 of 900 | 0 of 899 | 0 of 900 |
| wasted attempts | 8 on the dead endpoint | 102 | 35 |

- When the failing endpoint shared its tier with a healthy one, the breaker removed it
  with no wasted call: 71 of 89 such requests in run 3.
- When it was alone in its tier, the breaker was bypassed, by design, so that an open
  breaker alone never refuses a request. With lower tiers available this costs one failed
  call per request and buys nothing: 79 of run 3's 102 wasted attempts.
- Failover moved requests **down** a tier: 13 frontier-target requests were served by the
  cheap endpoint in run 3, and 35 by the mid endpoint in run 4. Nothing on the record
  marks this except comparing the target tier with the endpoint that served.
- The two failures in run 2 were requests for which the budget left one affordable
  endpoint, and that endpoint was down.

**Stated capacity against behaviour.** The cloud provider publishes 20 requests a minute.
In run 3 replayed demand peaked at 30% of that while every call was refused: the binding
limit was a daily allowance. In run 4 neither cloud endpoint passed 45% and one refused
37% of calls as overloaded. The capacity reading is only as good as the limits stated,
and here they were not the limits that bound.

### 6.7 Two measurements about tokens

- **The admission estimate depends on the kind of text.** RouteForge sizes a request at
  one token per four characters. Against the serving engine's count on run 1, the ratio
  was 1.72 on prose, 0.79 on code, 0.70 on JSON and 0.52 on log lines, and 0.955 summed —
  an aggregate that hides a threefold spread. On 21 calls from a real coding agent it was
  0.96 to 1.02 [FB §23.4, §24.9]. A per-request budget enforced on this estimate
  over-rejects prose and under-counts logs.
- **Reasoning models and token limits.** Both on-premises models reason before answering,
  and reasoning is billed as output. With limits of 768 to 1,536 tokens, 43% of the cheap
  model's answers and 20% of the mid model's in run 4 had no text. Each is an HTTP 200
  with a full bill, and the record does not distinguish it. The rates belong to this
  synthetic traffic; the mechanism does not.

---

## 7. Negative results and limits

**The router is not monotone in the eligible set.** Eligibility is monotone: relaxing a
requirement never removes an endpoint. The router is not: with a placement clamp and a
budget fallback that respects a risk floor, relaxing placement for a request with a
tight budget and a detected identifier can make it newly rejected as over budget
[FB §17.3]. We have a constructed example and a test that pins it. It did not occur in
2,699 live rows across three runs, in which every relaxation newly rejected nothing. The
only invariant a relaxation satisfies is that the eligible set grows.

**A scoring term can be inert.** One term of the scoring rule was, for the product's
history to that point, algebraically unable to change a ranking; a weight of 0.2 or 20
gave identical outcomes [SP §1.1]. Recording each term's value and source per decision is
what exposed it.

**The router does not choose well among eligible models** (§6.5). Tier is a function of
length, and quality is a default unless measured.

**Not measured.** A saving against a frontier-only baseline: the lab has no paid prices.
A `regressed` verdict from the version check on live endpoints. Coverage of the
uncertainty ranges, directly or under dependence. Any effect on real text: every prompt in
§6 is synthetic except 40 content-free records from one coding-agent session.

**Other threats.** One deployment; each outage is a single event; run 2's three endpoints
were one model under three declarations; endpoint declarations are the operator's and are
not verified; the benchmark has 22 single-turn cases with a deterministic grader; the
version check makes no correction for the number of figures it judges; replay is
first-decision with frozen signals.

---

## 8. From request-level to trajectory-level feasibility

Everything above treats a request as the unit. An agent does not make a request; it makes
a *trajectory*: tens of related calls while it plans, calls tools, reads results, retries
and resumes. RouteForge sees each call separately and keeps no state between them. Two
observations show what that leaves out. Neither feature discussed in this section is
built.

### 8.1 A restriction that does not survive compaction

One conversation from an application with unrestricted placement, run three times on the
lab of §6.1, with synthetic text.

| turn | what the client sent | served by (3 runs) |
|---|---|---|
| T1 | a public question | cloud, cloud, cloud |
| T2 | T1 plus a tool result containing an SSN and an e-mail address | on-premises ×3 |
| T3a | the full history, re-sent, plus a follow-up | on-premises ×3 |
| T3b | a summary of the history, without the SSN, plus the same follow-up | **cloud ×3** |

T2 and T3a are the system working: the identifier is detected, placement is raised, and
the request stays on-premises. T3b is a summary derived from a turn that carried a
restricted identifier. A client that compacts its context, as agents do when a
conversation grows, sends it without the identifier, and the router, correctly finding
nothing sensitive in *this request*, sends it to a cloud endpoint.

No stated guarantee is broken: the guarantee is per request. What fails is the property
an operator is likely to assume — that a conversation which has touched restricted data
stays where that data must stay. That property needs state across requests.

### 8.2 An agent's cost is in the context it re-sends

In the content-free records of one coding-agent session through RouteForge (21 calls),
input was 496,422 tokens and output 1,310: 99.7% input. The prompt grew on every call,
from 13,962 to 27,480 tokens. If each call extends the one before, as those sizes suggest
but content-free records cannot prove, at most 27,480 tokens were new and at least
468,942 (94.5%) had already been sent. An endpoint with a prefix cache can serve those
cheaply; moving the conversation to another endpoint, for a lower price or after a
failure, gives that up. The router of §2 prices each call as though it were the first.
In §6.6, failover moved 121 requests to a different endpoint across two runs; in a
trajectory each is a switch in mid-conversation. This is one session and a motivation,
not a distribution.

### 8.3 The trajectory-level problem

**Envelope.** Let `P_t` be the compiled policy in force at call `t` and `O_t` what is
observed at that call (entity types detected in the request and in tool results). By
default the policy only tightens along the sequence:

`P_{t+1} = meet(P_t, classify(O_t))`,  `E_t = { e : e satisfies P_t }`

with `meet` the stricter value on each axis — the compiler's algebra of §3 applied along
a sequence. `E_t` is then non-increasing unless a named, recorded operation lowers a
constraint. The invariants to check, over generated *event sequences* rather than single
policies, are: an observation may tighten but omission may not loosen; a summary, memory
entry or cached prefix inherits the restrictions of its sources; a sub-agent starts at
least as strict as the context it was given; retry, failover and resume preserve the
envelope; an unknown trajectory fails closed; and NI-1 and NI-2 hold at every step. The
failure artifact is the shortest event sequence that violates one. §8.1 is such a
sequence for the first invariant, found by hand.

**Sequential objective.** With `e_t ∈ E_t`:

`minimise  Σ_t [ cost_t(e_t) + λ_L · latency_t(e_t) + λ_S · switch_t(e_{t−1}, e_t) ]`

where `switch` is the context an endpoint had cached and another must process again.

**Feasible trajectory oracle.** The best sequence in hindsight that stays inside `E_t` at
every step. Regret is measured against it and never against a sequence that leaves the
envelope: the oracle of §4, over sequences.

**What is specific here.** Cache-aware and load-aware routing exist in serving systems,
and we do not claim them. The part that follows from this paper is the order of
operations: the envelope is compiled first, at every step, and the optimisation over
cost, cache and switching happens only inside it. A trajectory identifier supplied by a
client is an input to validate, not a policy; a client must not be able to widen its
envelope by starting a new trajectory, and binding the identifier to something the client
cannot choose freely is the open design question.

**Experiments this defines.** The sequence of §8.1 and its variants (sub-agent, retry,
failover, resume) with and without an envelope; replay of recorded agent sessions under
per-call and prefix-aware pricing, to measure how far per-call accounting misstates the
cost of a task; a per-call router against a switch-aware one on cost per completed task;
and regret against the feasible trajectory oracle.

---

## 9. Related work

**Not verified — see the note at the top.** The areas this paper should be placed against,
with the leads recorded in `arxiv.md` §8:

- *LLM routing and router benchmarks* — cost/quality routing over a fixed pool, and
  benchmarks for it (leads: RouteLLM, RouterBench, RouterEval, UniRoute, WISERouter, and
  several 2026 preprints). The difference to argue: these optimise within a pool; this
  paper is about the pool being a per-request, policy-derived variable, and about
  evaluating with an oracle that respects it.
- *Policy languages and analysis* — XACML, OPA/Rego, and solver-based policy analysis such
  as AWS Zelkova; the literature on configuration errors. The difference to argue: here
  policy is evaluated against recorded traffic, not only against itself.
- *Privacy and redaction for LLM applications* — detection and redaction proxies. The paper's contribution is not in
  this area, and it should say so.
- *Counterfactual and off-policy evaluation* — why it does not apply to logs from a
  deterministic router (§5), and what replay offers instead.
- *Agent serving and cache-aware routing* — prefix-cache and load-aware routing inside
  serving systems, and task-level agent benchmarks (leads: NVIDIA Dynamo's routing and
  agentic-inference documentation; Artificial Analysis AA-AgentPerf). The difference to
  argue for §8: those optimise a trajectory within a serving system; §8 asks what may be
  optimised at all once policy is carried along the trajectory.
- *Information-flow control for agents* — taint and label propagation across tool calls.
  §8.3's envelope is a coarse, per-call instance; the paper should say how it relates.
- *Noninterference* — the classical information-flow property, to which §3's use of the
  term should be related carefully: ours is a property of a policy compiler's outputs, not
  of program executions.

---

## 10. Conclusion

When the set of models a request may use is derived from policy, per request, before
anything is scored, three things become possible that a fixed-pool router does not offer:
a privacy claim that holds by construction and can be checked by properties; an evaluation
that does not penalise a router for obeying its policy; and a way to tell an operator what
a policy change will do to their own traffic before they make it. On a lab with
on-premises and cloud endpoints the first held without exception, the third reproduced
recorded outcomes and predicted a cloud share later observed, and the middle one showed
plainly that the router's own choices among eligible models are its weakest part. We
think that is the right order of priorities for the deployments this is built for, and
that a better scoring rule is a separable problem that the harness described here can
evaluate. The same construction extends from a request to the sequence of calls an
agent makes, where our measurements show both the privacy gap and the cost that a
request-level router cannot see; we leave that as the problem we think is worth solving
next.

---

## Appendix A. Where each number comes from

| statement | source |
|---|---|
| 15 of 18 mutants; two fail-open defects | PC §4.2, §5 |
| inert scoring term; dead endpoint ranks first | SP §1.1, §2.5 |
| dead-endpoint closed form; breaker in simulation | FB §9.5, §12.4 |
| null-rate of the two decision rules | FB §22.7–§22.8 |
| replay agreement 0.0206 → 1.0; rejected rows replayed as served | FB §15.4, §19.4.1 |
| run 1: agreement, token ratios, range width, split halves | FB §23 |
| run 2: outage, agreement 0.926, static-check counterexamples | FB §24 |
| agent-loop rows: token ratio 0.96–1.02 | FB §24.9 |
| run 3: provider outage, agreement 0.919, +$3.12, benchmark | FB §25 |
| run 4: cloud share, agreement 1.0, +$3.74, capacity, version check | FB §26 |
| non-monotonicity counterexample | FB §17.3 |
| trajectory probe; agent session token profile | [`trajectory-feasible-routing.md`](trajectory-feasible-routing.md) §2 |
| artifacts | `docs/research/data/live-dgx-lab-2026-10-0{8,9}*` |
