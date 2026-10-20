# RouteForge Agentic Trajectory Research Extension

**Extends:** `RouteForge-Priorities-1-3-LLM-Handoff.md`  
**Read order:** Read the base hand-off first, then this document.  
**Purpose:** Incorporate the useful agentic-inference ideas from the reviewed technical report into RouteForge without changing RouteForge into a GPU scheduler or model-serving engine.  
**Status:** Research and IP recommendations. These capabilities should not be represented as implemented until verified in the product and repository.

All endpoint names, prices, cache percentages, context sizes, and performance values in examples below are illustrative unless explicitly tied to a cited benchmark.

---

## 1. Executive recommendation

The strongest extension is to make RouteForge **trajectory-aware**.

The base design makes three request-level decisions:

1. Priority 1 determines which endpoints are permitted.
2. Priority 2 chooses the best permitted endpoint.
3. Priority 3 estimates the workload-wide effect of a change.

An AI agent, however, may make dozens or hundreds of related model calls while planning, calling tools, processing results, retrying, delegating to subagents, and resuming after a delay. Those calls form an **agentic trajectory**. A locally optimal endpoint for one turn may be globally inferior for the task because switching endpoints can lose cached context, repeat a long prefill, add queueing delay, or violate a protection rule introduced by a tool result.

The proposed extension is therefore:

> **Compile a policy envelope for the trajectory, choose endpoints using trajectory state and future cost, and simulate the resulting nonlinear capacity and business impact.**

This produces three linked additions:

| Base priority | Recommended trajectory extension | What it is for |
|---|---|---|
| **1. Policy compiler** | **Trajectory-Scoped Policy Envelope** | Preserves and tightens applicable controls as the agent receives new data, invokes tools, delegates work, retries, or resumes. |
| **2. Feasible-set router** | **Feasible Trajectory Router and Oracle** | Chooses a compliant sequence of endpoints that minimizes total task cost or completion time, including cache loss and switching costs. |
| **3. Counterfactual simulator** | **Nonlinear Agent Capacity Simulator** | Predicts cost, latency, rejection, cache behavior, and sustainable concurrent trajectories after policy or infrastructure changes. |

The best research packaging is:

> **FeasibleRouteBench-Agent: Policy-Constrained and Trajectory-Aware Routing for Stateful LLM Agents**

This is more defensible than presenting cache-aware routing alone, which already exists in serving systems. The differentiator is the integration of policy feasibility, trajectory state, compliant fallback, task-level evaluation, and counterfactual capacity analysis in a serving-system-independent governance layer.

---

## 2. Why per-request routing is insufficient

Consider a claims-processing agent:

1. The user asks a public question; the trajectory begins at `STANDARD` protection.
2. The agent calls a CRM tool.
3. The tool returns an SSN and medical information.
4. The effective protection level becomes `CONFIDENTIAL` and must remain in force for all later turns that contain or derive from that data.
5. By turn 18, the reusable context has grown to 80,000 tokens.
6. The agent must choose among the following endpoints:

| Endpoint | Per-turn price | Reusable context | Queue | Policy result |
|---|---:|---:|---:|---|
| A | $0.05 | 90% cache hit | Moderate | Eligible |
| B | $0.03 | No cache | Low | Eligible |
| C | $0.02 | No cache | Low | Ineligible for the PII-bearing trajectory |

A per-turn price router chooses B. A trajectory-aware router may stay on A because B must reprocess nearly the entire 80,000-token history. C must be removed before optimization, even though it is cheapest.

The system must also remember why protection increased. If the agent retries, creates a subagent, or resumes tomorrow, the inherited trajectory state must prevent the workflow from silently returning to `STANDARD`.

This example exposes three distinct problems:

- **Policy continuity:** later steps must inherit relevant controls and provenance.
- **Sequential optimization:** switching costs and expected remaining work affect the best endpoint.
- **Nonlinear capacity:** many trajectories moving to the same eligible endpoint can create queueing, cache eviction, and overload that a linear cost model misses.

---

# Extension to Priority 1: Trajectory-Scoped Policy Envelope

## 3. Purpose

Extend the Constraint-Isolated Policy Compiler so its output can govern a complete trajectory, not only a single request.

A **trajectory policy envelope** is a versioned state object containing:

- current effective constraints on every independent policy axis;
- the provenance of each constraint;
- the data classes observed during the trajectory;
- permitted endpoints, tools, regions, and fallback actions;
- inheritance rules for child agents, retries, checkpoints, and resumes; and
- rules for safely reducing a constraint after data removal or a verified context boundary.

The envelope may tighten as new evidence arrives. It must not silently weaken merely because the next turn omits the sensitive text.

## 4. Required invariants

1. **Monotone accumulation:** newly observed sensitive data can tighten the applicable protection constraint but cannot silently weaken it.
2. **Typed-axis noninterference:** a change in protection does not rewrite placement, retention, tool access, or another axis unless an explicit authorized rule connects them.
3. **Derived-data inheritance:** summaries, embeddings, tool outputs, cached prompts, and agent memory inherit the relevant restrictions of their sources.
4. **Child-agent inheritance:** a subagent receives constraints at least as strict as the parent context it is given.
5. **Retry and resume preservation:** retries, provider fallbacks, restored checkpoints, and delayed resumes retain the applicable envelope and version history.
6. **Fail-closed unknowns:** missing lineage, endpoint declarations, or envelope versions cause rejection or safe escalation, not implicit relaxation.
7. **Explicit declassification:** a constraint may be reduced only through a declared, testable operation such as verified redaction plus a new clean context boundary.
8. **Decision reproducibility:** the system records the envelope version, policy versions, endpoint-registry version, and state transition used for every turn.

## 5. Suggested state transition model

For turn \(t\), let \(P_t\) be the trajectory policy envelope, \(O_t\) the newly observed data and tool results, and \(A_t\) the authorized administrative rules. The default transition is:

\[
P_{t+1} = \operatorname{meet}(P_t,\; \operatorname{classify}(O_t),\; A_t)
\]

The `meet` operation selects the stricter compatible value on each typed axis. A weakening operation must be separately named, authorized, and evidenced; it is not the inverse of ordinary accumulation.

Each turn then obtains a feasible endpoint set:

\[
E_t = \{e \in E : e \models P_t \land e\text{ has complete required metadata}\}
\]

## 6. Proof-harness additions

Generate complete multi-step counterexamples, not only individual policy inputs. Test sequences such as:

- public prompt → sensitive tool output → fallback;
- protected parent → child-agent delegation → child retry;
- sensitive context → redaction → attempted declassification;
- endpoint failure after several cached turns;
- checkpoint under policy version N → resume under version N+1; and
- two tools returning data governed by different placement and retention rules.

For every generated sequence, assert that forbidden weakening, cross-axis mutation, or out-of-envelope fallback is impossible. The failure artifact should show the smallest event sequence that violates an invariant.

## 7. Customer value

This capability answers:

> “If sensitive information enters halfway through an agent workflow, will every later model call, tool call, subagent, retry, and resumed session still obey the right policy?”

The evidence is a replayable state-transition trace rather than a general compliance claim.

## 8. IP candidate

The potentially protectable combination is:

- independently typed policy axes;
- trajectory-scoped monotone state transitions;
- provenance-aware inheritance across tools and child agents;
- explicit, evidence-gated declassification;
- preservation across retries, fallbacks, checkpoints, and resumes; and
- automated generation and minimization of violating event sequences.

Do not frame the claim as ownership of policy inheritance or information-flow control in the abstract. Focus on the concrete mechanism in an LLM endpoint router and the routing failures it detects or prevents.

---

# Extension to Priority 2: Feasible Trajectory Router and Evaluation Plane

## 9. Purpose

Extend routing from “choose the best endpoint for this turn” to “choose the best compliant endpoint sequence for the task.”

The router still receives the feasible set from Priority 1. It must never trade policy compliance for cost, speed, cache affinity, or predicted quality.

## 10. Trajectory-aware decision inputs

In addition to the base request and endpoint features, consider:

- trajectory identifier and parent/child relationship;
- current step type, such as planning, tool selection, synthesis, or verification;
- current context length and expected context growth;
- cached-prefix location and estimated reusable-token count;
- queue depth and predicted wait at each endpoint;
- endpoint-switch and repeated-prefill cost;
- expected remaining turns or probability of continuation;
- task priority and completion-time SLO;
- tool-wait duration and cache-eviction risk;
- endpoint reliability and compliant fallback options; and
- current trajectory policy envelope and feasible set.

Agent-provided hints are useful inputs, but they are not trusted policy declarations. RouteForge should validate, cap, or ignore hints that conflict with observed state or policy.

## 11. Sequential objective

Let \(e_t \in E_t\) be the endpoint selected at turn \(t\). A simple trajectory objective is:

\[
\min_{e_1,\ldots,e_T}
\sum_{t=1}^{T}
\bigl(
C_t(e_t) + \lambda_L L_t(e_t) + \lambda_S S_t(e_{t-1},e_t)
\bigr)
\]

subject to:

\[
e_t \in E_t, \quad Q_t(e_t) \ge q_t, \quad \Pr(\text{SLO violation}) \le \epsilon
\]

where:

- \(C_t\) is predicted monetary or compute cost;
- \(L_t\) is predicted latency or completion-time contribution;
- \(S_t\) is switching cost, including lost cache and repeated prefill;
- \(Q_t\) is predicted task-quality contribution; and
- \(E_t\) is the policy-feasible set compiled for the turn.

The implementation need not know the true future. It can use a finite look-ahead, learned continuation estimate, contextual bandit with switching cost, or receding-horizon optimization. The research comparison should make that approximation explicit.

## 12. Feasible trajectory oracle

The base hand-off proposes a feasible oracle for request-level evaluation. For agentic work, define an offline **feasible trajectory oracle**:

> Given the realized trajectory, endpoint outcomes, policy-feasible sets, cache state, queues, and switching costs, choose the best compliant endpoint sequence under the experiment's objective.

This oracle supplies an upper-bound comparator for evaluation; it is not a deployable online policy because it can use future information unavailable at decision time.

Recommended regret metric:

\[
R_{\text{traj}} = U(\pi^{*}_{\text{feasible}}) - U(\pi_{\text{online}})
\]

where \(U\) is task-level utility and \(\pi^{*}_{\text{feasible}}\) is the offline policy-feasible oracle. Report regret only among compliant choices. An infeasible but faster or cheaper endpoint is never part of the comparator.

## 13. Policy-conditioned Pareto frontier

Construct a cost–completion-time–quality frontier separately for each policy class. This avoids comparing a restricted healthcare trajectory against endpoints it could never legally or contractually use.

The useful result is not one global “best model” curve. It is a family of frontiers conditioned on constraints such as:

- public versus confidential data;
- unrestricted versus US-only placement;
- retention permitted versus zero retention;
- external processing permitted versus on-premises only; and
- normal versus business-critical task priority.

The distance from the online decision to its applicable frontier can be reported as **policy-conditioned Pareto regret**.

## 14. Baselines

Evaluate at least these strategies:

1. Global ranking followed by policy filtering.
2. Cheapest feasible endpoint per turn.
3. Highest-quality feasible endpoint per turn.
4. Base feasible router without trajectory state.
5. Cache-affinity-only routing.
6. Queue-load-only routing.
7. Trajectory-aware RouteForge online policy.
8. Offline feasible trajectory oracle.

## 15. Customer value

This capability answers:

> “Are we minimizing the cost and completion time of the finished agent task, or merely making each isolated API call look cheap?”

Customer reporting should emphasize cost per successful task and end-to-end completion time, not only cost per token or per-call latency.

## 16. IP candidate

Cache-aware and load-aware routing are established techniques. The stronger candidate is a router that:

- first compiles a changing policy-feasible set for each trajectory step;
- optimizes a sequence of choices using cache, queue, switch, continuation, and task-quality state;
- keeps retries and fallbacks within the inherited trajectory envelope;
- learns only from policy-feasible comparisons; and
- evaluates against a feasible trajectory oracle and policy-conditioned frontier.

The novelty assessment must compare this complete claim combination with prior work; individual elements should be expected to have prior art.

---

# Extension to Priority 3: Nonlinear Agent Capacity Simulator

## 17. Purpose

Extend the traffic-weighted counterfactual simulator so it models stateful trajectories and endpoint operating envelopes rather than assuming that latency and cost remain fixed as traffic moves.

For example, a new privacy rule may exclude two cloud endpoints. A linear simulator might move their traffic to an on-premises endpoint and multiply request count by its historical average price. A realistic simulator must also ask whether the destination can sustain the added concurrent trajectories while satisfying time-to-first-token and output-rate SLOs. Overload may increase queues, evict useful cache entries, cause repeated prefills, trigger retries, or reject work.

## 18. Operating-envelope model

For each endpoint, estimate performance as a function of workload state:

\[
(\text{latency},\text{throughput},\text{failure},\text{cost})
= f_e(\text{concurrency},\text{context},\text{cache hit},\text{queue},\text{SLO class})
\]

The function may be an empirical response surface, queueing model, trace-replay model, or calibrated simulator. It must expose uncertainty and must not pretend that one benchmark configuration universally represents an endpoint.

## 19. Counterfactuals to support

- a protection or placement policy becomes stricter;
- an endpoint fails after several cache-rich turns;
- a provider price changes but migration causes repeated prefill;
- an agent pauses for a tool long enough for its cache to be evicted;
- context grows from 5,000 to more than 100,000 tokens;
- traffic suddenly shifts toward long-running agent tasks;
- subagents share a large common prefix;
- a destination endpoint reaches its concurrency or latency envelope; and
- a deployment changes from colocated to disaggregated prefill/decode serving.

The last item is an input scenario, not a feature RouteForge should implement inside the serving stack.

## 20. Outputs

Report distributions and risk, not only averages:

- cost per attempted and successful task;
- p50 and p95 end-to-end task completion time;
- p50 and p95 time to first token and output-token rate where measurable;
- sustainable concurrent trajectories under each SLO;
- request and trajectory rejection rate;
- policy-eligible capacity by policy class;
- endpoint utilization and overload probability;
- endpoint switches per trajectory;
- repeated-prefill tokens and estimated wasted prefill cost;
- KV/prefix-cache hit ratio;
- tool-wait/resume latency;
- retry and fallback frequency;
- successful-task quality; and
- uncertainty intervals for the counterfactual.

## 21. Customer value

This capability answers:

> “If we tighten this policy or move agent traffic, will the remaining compliant infrastructure still finish tasks on time and at the predicted cost?”

It connects governance decisions to capacity planning and task economics without claiming to operate the customer's GPUs.

## 22. IP candidate

The candidate is not generic Monte Carlo or queue simulation. It is the integration of:

- traffic-weighted policy recomputation;
- trajectory-level endpoint eligibility;
- stateful cache and switch-cost replay;
- nonlinear endpoint operating envelopes;
- policy-induced congestion and rejection prediction; and
- business outcomes measured per successful task.

---

# ArXiv Research Plan

## 23. Proposed paper

**Working title:** *FeasibleRouteBench-Agent: Policy-Constrained and Trajectory-Aware Routing for Stateful LLM Agents*

**Core claim to test:** Per-turn model routing misestimates the cost, latency, and compliance of stateful agent workloads; enforcing a trajectory policy envelope and optimizing over feasible endpoint sequences improves task-level utility without policy violations.

## 24. Research questions

1. How much do per-turn routers understate task cost and completion time when context reuse and endpoint switching are included?
2. Does trajectory policy continuity prevent violations introduced after tool results, retries, resumes, or child-agent delegation?
3. Can policy-feasible cache- and queue-aware routing reduce cost per successful task and p95 completion time relative to per-turn feasible routing?
4. How accurately can a trace-driven counterfactual predict overload after a policy change concentrates traffic onto fewer endpoints?
5. Which state variables provide most of the benefit: policy history, cache state, queue state, expected remaining turns, or step type?

## 25. Hypotheses

- **H1:** Per-turn routers systematically understate cost and completion time compared with trajectory-aware evaluation.
- **H2:** Policy-feasible cache-aware routing reduces cost per successful task and p95 completion time without increasing policy violations.
- **H3:** Static counterfactuals underestimate overload after policy shifts because endpoint capacity and latency curves are nonlinear.
- **H4:** Trajectory policy continuity prevents violations that appear after sensitive tool output, resumption, retry, or subagent creation.

## 26. Dataset and trace schema

Each replayable trajectory should record, with sensitive content removed or synthetically generated:

```json
{
  "trajectory_id": "traj-001",
  "parent_trajectory_id": null,
  "turn": 18,
  "step_type": "tool_result_synthesis",
  "input_tokens": 81240,
  "output_tokens": 620,
  "observed_data_classes": ["DIRECT_IDENTIFIER", "HEALTH_DATA"],
  "policy_envelope_version": 7,
  "feasible_endpoints": ["endpoint-a", "endpoint-b"],
  "endpoint_selected": "endpoint-a",
  "cached_prefix_tokens_by_endpoint": {
    "endpoint-a": 73000,
    "endpoint-b": 0
  },
  "queue_state_by_endpoint": {
    "endpoint-a": 4,
    "endpoint-b": 1
  },
  "tool_wait_ms": 3200,
  "latency_ms": 1900,
  "cost": 0.05,
  "quality_outcome": 1,
  "task_completed": false
}
```

Real production traces require consent, minimization, retention controls, and removal of prompts, secrets, personal data, and customer identifiers. A publishable benchmark should prefer synthetic policy labels and sanitized or public agent trajectories.

## 27. Experiments

Run controlled replays for:

1. Sensitive data introduced halfway through a trajectory.
2. Policy tightening while a task is executing.
3. Endpoint failure after multiple cached turns.
4. A tool wait that causes cache eviction.
5. A sudden burst of concurrent trajectories.
6. Context growth from 5K to 100K+ tokens.
7. Parent and child agents sharing prompt prefixes.
8. A policy shift from cloud endpoints to constrained on-premises capacity.
9. A price decrease that appears attractive per call but causes expensive re-prefill.

For each experiment, compare every baseline, the online trajectory-aware method, and the offline feasible trajectory oracle.

## 28. Ablations

- remove trajectory hints;
- remove cache state;
- remove queue state;
- remove expected remaining-turn estimates;
- remove the trajectory policy envelope;
- replace nonlinear operating envelopes with fixed averages; and
- allow unrestricted global-oracle comparisons to demonstrate why infeasible benchmarks are misleading.

## 29. Primary metrics

### Policy and correctness

- policy violations per trajectory;
- correct rejection rate when the feasible set is empty;
- false rejection rate;
- envelope inheritance failures;
- fallback compliance rate; and
- reproducibility of decisions from recorded versions.

### Task economics and experience

- task success rate;
- cost per successful task;
- p50/p95 completion time;
- endpoint switches per trajectory;
- repeated-prefill tokens;
- prefix-cache hit ratio; and
- feasible trajectory regret.

### Capacity

- maximum concurrent trajectories under defined SLOs;
- throughput of successful tasks;
- overload and rejection probability; and
- counterfactual prediction error with confidence intervals.

## 30. Reproducibility requirements

- publish the policy schema and synthetic endpoint registry;
- publish sanitized traces or a deterministic trace generator;
- version the models, endpoints, serving configuration, policy set, and benchmark harness;
- state the SLOs and quality threshold before measurement;
- separate measured results from simulated results;
- report confidence intervals and failed runs;
- disclose when cache state or queue state is estimated rather than observed; and
- avoid claiming hardware-generational results outside the exact tested configuration.

---

# Product and IP Prioritization

## 31. Recommended implementation order

| Order | Deliverable | Why now | Research/IP value |
|---:|---|---|---|
| **1** | Trajectory identity, event schema, and policy-envelope persistence | Establishes the state required by every later feature. | High: enables formal invariants and reproducible traces. |
| **2** | Multi-step proof-harness scenarios | Produces immediate governance value before learned routing. | High: concrete counterexamples strengthen technical evidence. |
| **3** | Task-level metrics and trace replay | Replaces misleading per-call reporting and supplies an evaluation plane. | High: foundation for a publishable benchmark. |
| **4** | Cache/switch-aware feasible router | Captures a large practical inefficiency while keeping policy deterministic. | Medium-high: strongest when claimed with policy and trajectory state. |
| **5** | Feasible trajectory oracle and regret analysis | Makes online evaluation scientifically defensible. | High for arXiv; useful for model selection and regression testing. |
| **6** | Nonlinear capacity counterfactuals | Converts policy changes into operational and financial forecasts. | High customer value; IP depends on integration details. |
| **7** | Learned look-ahead or continuation model | Improves sequential decisions after reliable telemetry exists. | Research-worthy, but defer until the data plane is trustworthy. |

## 32. Patent-oriented claim clusters to investigate

These are invention-disclosure themes, not legal conclusions about novelty or patentability:

1. **Trajectory policy envelope:** typed monotone constraints with provenance, inheritance, explicit declassification, and preservation through retries and child agents.
2. **Policy-feasible sequential routing:** endpoint-sequence optimization that includes cache, queue, switching, continuation, and task outcome while excluding infeasible actions before learning.
3. **Feasible trajectory oracle:** evaluation and learning restricted to the time-varying policy-feasible action set.
4. **Policy-induced capacity simulation:** replay of policy changes through trajectory eligibility, cache movement, nonlinear capacity, and rejection to estimate business outcomes.
5. **Minimal trajectory counterexample generation:** automatically finding and reducing the shortest tool/agent/retry sequence that violates a routing invariant.

Before filing or publishing, conduct a claim-charted prior-art search across policy engines, information-flow control, constrained reinforcement learning, contextual bandits with switching cost, LLM routers, workflow security, stateful service routing, and trace-driven capacity simulation.

## 33. Separation of patent and publication work

Public disclosure can affect patent rights. Coordinate the paper, public repository, conference submission, customer presentation, and patent filing schedule with qualified IP counsel. Preserve dated design notes, benchmark definitions, commits, experiment configurations, and inventor contributions.

---

# Scope Boundaries and Corrections

## 34. What RouteForge should consume, not implement

Treat the following as endpoint capabilities or telemetry supplied by systems such as vLLM, NVIDIA Dynamo, Kubernetes, or a cloud provider:

- prefill/decode disaggregation;
- GPU placement and scheduling;
- tensor, expert, data, or hybrid parallelism;
- MoE token-to-expert routing;
- rack-scale NVLink or network configuration;
- worker-level KV-cache placement; and
- fixed worker counts in deployment manifests.

RouteForge may consume observed queue, cache, latency, price, health, and SLO signals and route among externally managed endpoints. It should not claim those infrastructure mechanisms as RouteForge inventions.

## 35. Claims from the reviewed report that require caution

- Do not state that internal trajectory tokens are universally unbilled. Billing and accounting vary by provider and deployment; describe measured input, output, cached, reasoning, and infrastructure costs explicitly.
- Do not reuse exact concurrency numbers without a dated benchmark snapshot, complete configuration, and reproducible methodology.
- Do not generalize large hardware-generation throughput or cost multipliers beyond the specific model, precision, SLO, system topology, software version, and workload tested.
- Do not assume disaggregated prefill/decode always improves performance. Its value depends on prompt/decode mix, concurrency, transfer overhead, and the serving system.
- Do not describe cache affinity or queue-aware routing by itself as novel; existing serving frameworks document these mechanisms.

---

# External Research Anchors

Use these as technical context and prior-art starting points, not as proof of RouteForge novelty:

1. Artificial Analysis, **AA-AgentPerf** — task/trajectory-oriented agent-serving benchmark and live results:  
   <https://artificialanalysis.ai/articles/aa-agentperf>
2. NVIDIA Dynamo, **Routing Concepts** — cache-aware routing combined with projected worker load:  
   <https://docs.nvidia.com/dynamo/dev/knowledge-base/modular-components/router/routing-concepts>
3. NVIDIA Dynamo, **Agentic Inference** — agent hints, priority, prefix reuse, and agent-lifecycle-aware serving concepts:  
   <https://docs.nvidia.com/dynamo/v1.0.1/blog/agentic-inference>
4. NVIDIA Dynamo, **Disaggregated Serving** — separation of prefill and decode and associated workload tradeoffs:  
   <https://docs.nvidia.com/dynamo/v1.3.0/user-guides/disaggregated-serving>

The paper's related-work section should additionally search primary literature for constrained sequential decision-making, policy-aware model routing, information-flow control for agents, cache-affinity scheduling, and trace-driven inference simulation.

---

## 36. Handoff instruction for another LLM

When using this extension:

1. Preserve the base hand-off's rule that deterministic policy defines the feasible set before optimization.
2. Treat the trajectory extension as proposed research, not an implemented capability.
3. Keep RouteForge above the model-serving layer.
4. Express benefits at the completed-task level.
5. Compare against policy-feasible baselines and an offline feasible trajectory oracle.
6. Separate measured observations, simulator estimates, and illustrative examples.
7. Do not write patent claims or assert novelty without a dedicated prior-art search and counsel review.

The central design test is:

> **Can RouteForge prove that every step of a stateful agent trajectory stayed inside its evolving policy envelope, while choosing a near-optimal compliant endpoint sequence and accurately predicting the task-level effect of policy changes?**
