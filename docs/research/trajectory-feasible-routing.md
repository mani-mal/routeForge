# From request-level to trajectory-level feasible routing

**Status: problem statement and first measurements. Nothing in this document is built.**
Written 2026-10-09 from the owner's design note on agentic trajectories. It records what
RouteForge does today, two measurements that show where the request-level model stops, a
formulation of the trajectory-level problem, and the experiments that would test it. For
the paper, it is the source of the "trajectory" section in
[`paper-draft.md`](paper-draft.md); for the product, the build order is backlog B72.

Artifacts: `docs/research/data/trajectory-probe-2026-10-09/` (the probe script, its
request ids, and the content-free outcome rows of one coding-agent session).

---

## 1. What RouteForge does today

Agent loops belong to the calling application. Each model call an agent makes arrives as
its own request and passes through the gate, eligibility and the router on its own. There
is **no trajectory identity**: nothing links one call to the next, and no state is carried
between them. A placement restriction raised on one call (a detected identifier raising
the request to on-premises) exists for that call only.

This is safe whenever the client re-sends the whole conversation, because the sensitive
text is then detected again on every call. It stops being safe when the client does not.

## 2. Two measurements

### 2.1 A restriction does not survive a compacted history (live, 2026-10-09)

One conversation, application `paper-dev` (unrestricted placement), DGX lab, the four
endpoints of [`feasible-route-bench.md`](feasible-route-bench.md) §25.1. Synthetic text.
The sequence was run three times.

| turn | what the client sent | served by, runs 1 / 2 / 3 |
|---|---|---|
| T1 | a public question | cloud / cloud / cloud |
| T2 | T1 + a tool result containing an SSN and an e-mail address | on-prem / on-prem / on-prem |
| T3a | the full history, re-sent, + a follow-up | on-prem / on-prem / on-prem |
| T3b | a **summary** of the history (no SSN in it) + the same follow-up | **cloud / cloud / cloud** |

T2 and T3a behave as designed: the identifier is detected, placement is raised, the
request stays on-premises. T3b is the gap. The summary was derived from a turn that
carried a restricted identifier; a client that compacts its context — as coding agents
do when a conversation grows — sends it without the identifier, and RouteForge, seeing
nothing sensitive in this request, routes it to a cloud endpoint. Three of three.

Two things this is and is not:

- It is **not** a violation of any guarantee RouteForge states. The guarantee is
  per request, and each request was handled correctly.
- It **is** a counterexample to the reading an operator is likely to make: "once a
  conversation has touched restricted data, it stays on-premises." That property needs
  state across requests, and there is none.

The same sequence also shows a **policy-forced endpoint switch**: T1 was answered by a
cloud endpoint and T2 by an on-premises one. Whatever prefix cache the first endpoint held
for this conversation was of no use to the second.

### 2.2 An agent's bill is almost entirely re-sent context (one real session)

The content-free outcome rows of one coding-agent session through RouteForge
([`feasible-route-bench.md`](feasible-route-bench.md) §24.9): 21 served calls.

| | tokens |
|---|---|
| prompt tokens, all 21 calls | 496,422 |
| completion tokens, all 21 calls | 1,310 |
| share of all tokens that were input | 99.7% |
| prompt size, first call → last call | 13,962 → 27,480 (growing on every call) |
| upper bound on tokens that were new to the endpoint | 27,480 |
| prompt tokens that had been sent on an earlier call, at least | 468,942 (94.5%) |

The last two rows assume each call extends the previous one, which the strictly growing
sizes suggest but the content-free rows cannot prove. Under that reading, **about 17 of
every 18 input tokens the session paid for had already been processed**. An endpoint that
keeps a prefix cache can serve those cheaply; a router that moves the conversation to
another endpoint, for price or after a failure, forfeits it. RouteForge's router prices
each call as if it were the first.

One session, one client, one model. It is a motivation, not a distribution.

### 2.3 What the request-level runs already showed

- Failover moved 121 requests to a different endpoint in two runs (86 and 35;
  [`feasible-route-bench.md`](feasible-route-bench.md) §25.4, §26.3), 114 of them to a
  lower tier than the request targeted. In a trajectory each would be a switch in mid-conversation.
- An agent's **first** call was about 14,000 tokens; a per-request cap sized for chat
  rejected it (§24.9). Budgets are per call, and an agent's cost is per task.

## 3. The trajectory-level problem

A *trajectory* is the sequence of model calls one task makes: planning, tool calls and
their results, retries, sub-agents, a resume after a pause.

**Policy envelope.** Let `P_t` be the policy in force at call `t`: one value on each axis
of placement and protection, with the reason for each. Let `O_t` be what is observed at
call `t` (detected entity types in the request and in tool results). The default
transition only tightens:

`P_{t+1} = meet(P_t, classify(O_t))`

where `meet` takes the stricter value on each axis. This is the per-request compiler's
algebra ([`policy-compiler-noninterference.md`](policy-compiler-noninterference.md))
applied along a sequence. The eligible set at each call is then
`E_t = { e : e satisfies P_t }`, and is non-increasing along the trajectory unless an
explicit, recorded operation lowers a constraint.

**Invariants a harness should check over generated event sequences:**

1. *Monotone accumulation.* An observation can tighten the envelope; omission of the
   sensitive text on a later call cannot loosen it (§2.1 is this, violated).
2. *Axis independence.* A change on a protection axis moves no placement value, and the
   reverse, except through a declared rule — NI-1 and NI-2, per step.
3. *Derived data inherits.* A summary, a memory entry or a cached prefix carries the
   restrictions of what it was made from.
4. *Child inherits.* A sub-agent starts with an envelope at least as strict as the
   context it was handed.
5. *Retry, failover and resume preserve.* None of them re-derives the envelope from the
   current request alone.
6. *Unknown fails closed.* A missing trajectory id or envelope version is treated as the
   strictest envelope the application could have, or rejected.
7. *Lowering is explicit.* A constraint is lowered only by a named, recorded operation.

**Sequential objective.** With `e_t ∈ E_t` the endpoint chosen at call `t`:

`minimise  Σ_t [ cost_t(e_t) + λ_L · latency_t(e_t) + λ_S · switch_t(e_{t−1}, e_t) ]`

subject to `e_t ∈ E_t` at every `t`. `switch` is the cost of leaving an endpoint: the
prefix it had cached and must now be processed again elsewhere. §2.2 says this term can
dominate.

**Feasible trajectory oracle.** For evaluation, the best sequence in hindsight that stays
inside `E_t` at every step. Regret is measured against it and never against a sequence
that leaves the envelope — the trajectory form of FeasibleRouteBench's oracle.

## 4. What it would take

| step | what | depends on |
|---|---|---|
| 1 | A trajectory id (client-supplied header, validated; absent = today's behaviour) on the outcome row | nothing; content-free |
| 2 | A per-trajectory envelope store with a TTL, inside the deployment boundary; the gate reads and tightens it | 1 |
| 3 | Sequence properties in the harness: the seven invariants over generated event sequences, with the shortest violating sequence as the failure artifact | 2 |
| 4 | Task-level reporting: cost and calls per trajectory, switches, tokens re-sent | 1 |
| 5 | Cached-token counts recorded per call where the endpoint reports them | exists for pricing (P2a); not on the outcome row |
| 6 | A switch-cost term in scoring and in replay | 4, 5 |
| 7 | A feasible trajectory oracle and regret in the benchmark | 4, 5 |

Steps 1 to 3 are a privacy feature and are worth building on their own. Steps 4 to 7 are
the routing and evaluation half.

**Boundaries.** RouteForge stays a router. Prefix caches, queues and GPU placement belong
to the serving systems behind the endpoints; RouteForge would read what they report and
would not manage them. A client's trajectory id and any hints it sends are inputs to be
validated, never policy: a client cannot widen its own envelope by declaring a new
trajectory, so an unknown or reset id must not be more permissive than the application's
own policy, and a deployment that needs the guarantee must bind the id to something the
client cannot choose freely. That binding is the open design question.

## 5. Experiments that would test it

| id | experiment | measures |
|---|---|---|
| T-E1 | §2.1's sequence and its variants (sub-agent, retry, failover, resume, two tools with different rules), with and without the envelope | calls that leave the envelope: expected 3 of 3 without, 0 with |
| T-E2 | Replay real agent sessions with per-call pricing against prefix-aware pricing | how far per-call cost overstates or understates task cost |
| T-E3 | Per-call router against a switch-aware router on recorded sessions | cost per completed task, switches per task |
| T-E4 | A policy change that moves agent traffic on-premises, with capacity read per trajectory | concurrent trajectories against stated limits |
| T-E5 | Regret against the feasible trajectory oracle | per strategy, within the envelope |

T-E1 needs steps 1 to 3. T-E2 needs only step 5 and more recorded sessions.

## 6. Limits of what is written here

- §2.1 is three repetitions of one synthetic conversation, with a hand-written summary
  standing in for a client's compaction.
- §2.2 is one session; the prefix assumption is inferred from sizes, not observed.
- No part of §3 or §4 exists. No claim is made that a trajectory-aware router would do
  better; that is what §5 is for.
- Cache-aware and load-aware routing exist in serving systems. What is specific here is
  the envelope: eligibility that is computed first, per step, and carried along the
  sequence.
