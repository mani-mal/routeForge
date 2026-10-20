# Non-deterministic model selection and System-One decisions: pros, cons, and who owns what

**Date:** 2026-10-16 · **Status:** analysis only. Nothing here is built or approved.
**Inputs read:**
- *Jev Engineering for Coding Agents* (independent synthesis of TypeSafe founder Diogo
  Almeida's design notes, Sept 2026, 12 pp.):
  <https://github.com/yibie/jev-engineering-zh/blob/main/assets/original.pdf>
- `docs/roadmap/system-one-jev-model.md` (including Appendix A, the owner's original notes)
- `docs/archive/design-handoffs/RouteForge-Agentic-Trajectory-ArXiv-Extension.md`
- `docs/archive/design-handoffs/RouteForge-Priorities-1-3-LLM-Handoff.md` (§12–21, Priority 2)
- `docs/architecture/multidimensional-policy-model.md` (§4, §10–11)
- `docs/archive/design-handoffs/policy-protection-redesign-handoff.md` (skimmed for scope statements)
- The code, for how routing works today. Facts below come from the code, not from CLAUDE.md.

**Questions:**
1. What are the pros and cons of having RouteForge choose the model for a request with a
   non-deterministic method, meaning a learned, probabilistic, or stochastic one?
2. Some decisions belong to the calling application rather than to RouteForge: picking a
   tool, reusing or rebuilding the cache, compressing context, delegating to sub-agents, and
   others. How well can applications make those decisions with System-One models?

---

## 1. Short answer

- **Model choice in RouteForge:** a System-One model is worth adding **as an advisory scoring
  input, not as an authority, and not yet.** Today's tier signal is prompt length alone, so a
  semantic difficulty signal would fix a real gap. Three things should come first:
  - **switch-cost scoring**, without which a per-call classifier makes the PDF's own "routing
    trap" worse for multi-turn traffic;
  - a **shadow-mode** measurement on real traffic;
  - an **in-boundary** model.
- **Stochastic selection** (random exploration, sampling a model in proportion to its score):
  **no, not for live routing.** It buys learning data at the cost of reproducibility, cost
  predictability, and customer trust. RouteForge can get most of that learning data offline,
  through the outcome tables, the policy simulator, and the feasible-oracle bench.
- **App-side decisions:** most of the PDF is about these, and they really do belong to the
  application, because they need state RouteForge never sees: the chunk store, the tool
  registry, the plan, and which files will be touched. System-One models fit some of them very
  well and some poorly (§6). RouteForge's job is to **govern the System-One call itself**,
  since it reads raw application state, and to **give apps the prices and cache facts** their
  decisions need. It should not make those decisions for them.

---

## 2. What the PDF actually argues

The PDF describes a **coding-agent harness**, not a gateway. Its thesis: "if an agent is just a
loop, the loop is not where the leverage is. The leverage is in what the loop feeds the model
each time it turns." Jev is "the decision layer beside" the model that writes the code. It
receives explicit, typed application state plus a predefined question and returns a typed answer
(choice, score, or yes/no) with a probability.

It names six per-turn decision points (Table I):

| Decision point | Question to Jev | Typed answer |
|---|---|---|
| Context | How visible should this chunk be for this query? | hide / short / long / full |
| Cache | Reuse the cached prefix or rebuild? | yes/no + probability |
| Routing | Can this subtask leave the frontier model? | choice + cost estimate |
| Tools | Which tool fits this intent? | ranked top-k |
| Permissions | Should this command run? | allow / ask / deny |
| Security | Which files will this task touch? | sensitivity score |

It explains six habits of current agents as consequences of KV-cache economics, which it calls
"the tyranny of the KV cache": routing that loses money, tool schemas that crowd the context,
query-blind compaction, rarely used sub-agents, restarts that throw away good state, and the
"batteries" debate.

### 2.1 The routing arithmetic, checked

The PDF uses Opus at $5/$25 per 1M tokens (input/output) and Sonnet at $3/$15. X is context
tokens, Y is generated tokens, and Z is tokens read during the work.

- Pure Opus: `25Y + 5Z`
- Opus → Sonnet → Opus: `3X + 20Y + 8Z`

With X = 0.65, Y = 0.12, Z = 0.23, pure Opus costs **4.15** and the routed path costs **6.19**.
I re-computed both figures and they are correct.

**Caveats the PDF does not state:**
- Path 1 charges nothing at all for the context X. Even a cached prefix is billed, at about
  10% of the input price. Adding `0.5·X = 0.325` gives 4.475, which is still cheaper. The
  conclusion survives.
- Path 2 charges Sonnet the full uncached price for X. If Sonnet keeps its own warm cache over
  a long hand-off, the gap narrows.

**The lesson the PDF draws is the right one:** "routing priced per token, rather than per
context rebuild, is wrong." Routing pays off only when the cheap model gets a small,
purpose-built context and the return trip does not make the frontier model reread everything.

### 2.2 How much weight the evidence can bear

- The PDF says of itself that the token-share table is "an illustrative estimate" and that the
  cost figures are illustrative.
- The only third-party measurement is from Microsoft's fastcontext project: reading and
  searching are 56.2% of tool-use turns and 46.5% of main-agent tokens in GPT-5.4 trajectories.
- No harness was built or measured in the PDF. "None of this requires a better model" is a
  hypothesis. It assumes a System-One model is accurate on harness states it was never trained
  on, and nobody has tested that.
- `docs/roadmap/system-one-jev-model.md` §5 already notes the gaps in evidence for the open candidate,
  AutoJev-27B:
  - the accuracy and calibration figures come from the author's own test set;
  - two sources give different accuracy (84.6% vs 91.9%);
  - it shares a base model with a model we serve, which creates a self-preference risk.

### 2.3 The point that matters most for RouteForge

All six decisions in the PDF need **explicit application state**: addressable chunks, a tool
registry, the plan, and the files a subtask will touch. A gateway receives only the
**already-assembled** `messages[]` (and `tools[]`) for one call. RouteForge cannot reach back
into a chunk store, cannot shrink the context it was handed without breaking content integrity,
and cannot know what the next turn will ask. So most of the PDF's leverage sits on the
application side of the boundary. That is a property of where each component sits, not a gap
in RouteForge.

---

## 3. "Non-deterministic" means four different things

People use the word for four mechanisms with very different risks. The pros and cons depend on
which one is meant.

| # | Mechanism | Same input → same model? | Example |
|---|---|---|---|
| **A** | Deterministic rule (today) | Yes, exactly | `tier = f(tokens/2000)`, weighted score over eligible candidates |
| **B** | Learned classifier, System-One style | Yes, apart from tiny numeric jitter; it can be **stably wrong** | Jev/AutoJev "difficulty score" or "task category" |
| **C** | Stochastic policy | **No, by design** | ε-greedy or Thompson-sampling bandit; pick a model in proportion to its score |
| **D** | Generative LLM as router | No: temperature, parse failures, prose | "Ask a chat model which model to use" |

- **D** is dominated by B for this job: it is slower, costs output tokens, can fail to parse,
  and its probabilities mean nothing. Do not consider it further.
- **B** is not random. Its risk is *error* plus a weaker audit story.
- **C** is random on purpose. Its risk is *irreproducibility*.

### 3.1 What RouteForge does today

From the code:
- **Tier target.** `complexity = min(1.0, tokens_after / 2000)` (`app/pipeline.py:933`), using
  the token count after compression. The cut-offs are `cheap_max: 0.35` and `mid_max: 0.70`
  (`config/routing.yaml:61`). That works out to roughly ≤ 700 tokens → cheap, ≤ 1,400 → mid,
  and anything larger → frontier, before the risk floor and escalation raise it.
- **Ranking.** A deterministic `RoutingStrategy` ranks only the eligible set that
  `Router.select` has already filtered (`app/routing/strategy.py`). It records the provenance of
  every scoring term.
- **Randomness.** None in selection.
- **Switch cost.** Nothing in `app/routing/` scores endpoint affinity or switch cost. The
  `sticky` value of `trajectory_mode` keeps the **policy envelope** across calls, not the
  endpoint (`app/policy/trajectory.py`).

The practical consequence: a 40-token "prove this lemma" goes to the cheap tier, and a
2,000-token pasted log with "summarize this" goes to frontier. Length is a weak stand-in for
difficulty, and that gap is real.

---

## 4. Option B, a System-One difficulty or category signal inside RouteForge: pros and cons

### Pros

1. **Fills the semantic gap.** It can tell "short but hard" from "long but trivial", which is
   the main weakness of the length rule (§3.1).
2. **Typed output with a confidence score.** You can set a threshold and fall back to the rule
   when it is unsure. That is much safer than parsing prose (mechanism D).
3. **Cheap per decision.** The claims are about $0.04 per 1M input tokens, no output tokens,
   and one forward pass that answers several questions at once (category, difficulty, and a
   sensitivity check).
4. **Fits the seam that already exists.** It becomes another scoring term next to quality,
   latency, and availability, with provenance (`measured` / `classifier` / `fallback`).
   `RoutingStrategy` can only reorder the eligible set, so **a wrong classification cannot
   reach an ineligible endpoint.** The worst it can do is cost and quality damage, not a
   privacy breach.
5. **Can tighten, raise-only.** A confident "this is sensitive" verdict can *add*
   `onprem_only`, as a second detector beside Presidio. That is the same pattern as
   `sensitivity_rules` (`jev-model.md` §3.1).
6. **Good research material for the paper:** semantic vs length tiering, compared against the
   feasible oracle (FeasibleRouteBench), with a dead band and recorded probabilities.

### Cons

1. **It adds latency to every request.** The claims are 70–500 ms, or 88 ms for a small local
   model. The length rule costs roughly 0 ms. For a 300 ms cheap-tier answer, that is a large
   relative increase.
2. **GPU cost in the boundary.** AutoJev-27B needs about 49 GiB in BF16. On the lab and Spark
   hardware it would displace a served model. Small models (AgentJev-0.6B) are cheap, but
   their accuracy is unproven.
3. **It is stably wrong some of the time.** The published claims imply roughly 1 decision in 8
   to 1 in 12 is wrong, and that comes from the author's own test set, not our traffic.
   Errors push hard tasks to cheap models (quality loss) or easy ones to frontier (cost loss).
4. **The audit story gets weaker.** "Tier = frontier because tokens > 1,400" can be replayed
   from the trace. "Tier = frontier because a 27B model said 0.73" can be replayed only if the
   probability, model hash, question, and threshold are recorded. Replaying by re-running the
   model is not good enough.
5. **It makes the routing trap worse.** This is the PDF's own argument turned on RouteForge. A
   semantic classifier judges each call separately, so within one trajectory it will flip
   tiers more often than the length rule does, because length grows steadily. Each flip throws
   away the provider-side prefix cache and forces a re-prefill. That is exactly the 6.19 vs
   4.15 case. **Without switch-cost or affinity scoring in step 6, a better per-call classifier
   can raise cost per task while lowering cost per call.** The trajectory extension (§10–11)
   names switch cost S_t as part of the objective. It is not built.
6. **Clients can game it.** An application, or a user inside one, can phrase prompts so they
   look "hard" and get frontier at the operator's expense. The length rule can be gamed too,
   by padding, but that is visible. Per-app tier caps and budgets limit the damage.
7. **Placement constraint.** The classifier reads the raw prompt. Under rule 1 it has to run
   after the gate, and it has to be an on-prem endpoint. A hosted Jev API would send raw
   content outside the boundary before routing, which is exactly what RouteForge exists to
   prevent.
8. **Drift and versioning.** Upgrading the model changes routing for the same traffic. It has
   to be an onboarded, versioned event folded into `sp.routing_policy_version`, never a silent
   swap.
9. **Self-preference risk.** AutoJev is built on the Qwen3.8 family that RouteForge also serves
   (`jev-model.md` §5). This matters more for quality labelling than for difficulty scoring,
   but it is not zero for either.

---

## 5. Option C, stochastic selection (exploration): pros and cons

### Pros

1. **Counterfactual data.** Without some exploration, you never learn how the cheap model would
   have done on traffic that always went to mid. Logged selection probabilities allow unbiased
   off-policy evaluation.
2. **Load spreading** across equivalent endpoints. Load balancing already does this below
   RouteForge, so it adds little here.
3. **Avoids lock-in** to an endpoint that scored well early on (the bandit "cold start" problem).

### Cons

1. **Irreproducible by default.** The same request gives a different model, a different answer,
   and a different cost. That breaks "every decision is explainable from the trace" unless the
   seed and probability are recorded. It also breaks the policy simulator's replay agreement,
   which P3a/B55 worked hard to bring to 1.0.
2. **Customers see it.** Users notice when identical requests get different-quality answers.
   For regulated customers, the hospital persona among them, "we randomly sent your request to
   a weaker model to learn" is hard to defend even when the model was eligible.
3. **Cost variance.** Budgets and per-request cost caps become probabilistic.
4. **Mostly unnecessary.** RouteForge already has offline routes to the same knowledge:
   - `routing_outcomes` and `outcome_feedback` (P2b-2);
   - human-approved calibration (P2b-3);
   - the feasible-oracle bench, including `--live`;
   - the policy simulator.

   A **shadow** evaluation, where a candidate model is sampled on a copy and never returned,
   gets counterfactual quality without exposing users to it.

**If exploration is ever wanted:**
- make it **seeded by request id**, so it is reproducible;
- restrict it to the feasible set (Priority 2 §16 already requires this);
- make it opt-in per app and never on by default;
- exclude `onprem_only` and `require_verification` traffic;
- record the propensity on the trace.

---

## 6. Decisions that belong to the application: how well System-One fits each

**Why these are the application's.** Each needs state that exists only inside the agent loop.
RouteForge sees one already-assembled call at a time. Agent loops are client-driven, and every
call re-enters the gateway on its own.

| App decision | Fit for System-One | Why | Main risk when it is wrong | What RouteForge can contribute |
|---|---|---|---|---|
| **Pick tool** (ranked top-k from intent) | **Strong** | A ranked choice over a known, finite set is exactly what the model is shaped for. A wrong argument type becomes a validation error. It also removes schema bloat (PDF §IV.B; tool schemas are 5–12% of tokens). | Wrong tool means a wasted turn. That is visible and recoverable. | Nothing at decision time. It could report the share of each request spent on `tools[]` schemas, so apps can see the overhead. |
| **Context: chunk visibility** (hide/short/long/full per query) | **Strong on paper, risky in practice** | It is query-aware, which is the thing blind compaction lacks. Reading and searching are the largest token cost, so the payoff is the largest. | **The failure is silent.** A hidden chunk the model needed gives a confidently wrong answer and no error. It also costs one call per chunk per turn, which needs batching. | It cannot do this itself: the gateway sees the assembled context, not the chunks. Headroom at step 5 compresses without knowing the query, the weaker form the PDF criticises, and it does little on the direct path anyway. RouteForge can enforce `content_integrity: exact` for apps that do their own assembly. |
| **Cache: reuse or rebuild** | **Weak to moderate** | The cost half is plain arithmetic: cached tokens, prices, prefix length. A formula beats a model there. The model only helps with the quality half: is the old context drifted or corrupt? | A needless rebuild wastes money. A wrong reuse carries stale or corrupt state forward. | **Price facts.** Per-endpoint cached vs uncached prices, and the cached-token counts it already records (P2a), so the app can do the arithmetic. |
| **Route a subtask / delegate to a sub-agent** | **Moderate** | "Can this leave the frontier model?" is a reasonable yes/no question. **But the saving comes from the small, purpose-built context the app builds, not from the classifier.** | Quality loss on hard subtasks, and wasted cost if the result has to be reread. | **Feasibility and pricing.** Accept the app's hint (step type, desired tier) as **advisory, validated, and capped** (trajectory extension §10), and enforce that the delegated call stays inside the trajectory envelope. |
| **Permissions** (allow/ask/deny a command) | **Advisory only** | The PDF itself uses programmable rules (`deny if touches ~/.ssh`) plus deeper inspection. That matches the asymmetric-authority principle. | A false *allow* runs something dangerous. | Same principle as RouteForge's gate: a model may **tighten** (move toward ask or deny) but never grant an allow on its own. |
| **Security: which files will this touch** → route by sensitivity | **Moderate (prediction)** | PDF Table IV is RouteForge's feasible-set idea, applied *before* the data is seen. | A missed prediction means sensitive content reaches an endpoint chosen for "public". | **This is RouteForge's strongest point of overlap.** The app predicts, and RouteForge enforces placement on what is **actually sent**. If the prediction misses, the gate and the trajectory envelope still catch observed entities. The two layers back each other up. |
| **Conditional instructions, goal de-duplication, background tasks** | Moderate to strong | Rule-like conditions ("touches `*.tsx`") often need no model at all. A model helps with fuzzy conditions ("is this prose?"). | Missing or extra instructions; duplicated work. | None. These sit entirely in the application. |

### 6.1 What limits how well applications can use System-One models

1. **The harness has to change first.** The gains come from **explicit, typed, addressable
   state**. Most agents, including today's coding agents, keep an append-only transcript. A
   System-One model added to an append-only agent can do tool picking and permissions, but not
   the large context and routing gains. Those need a different harness.
2. **Latency adds up.** Several questions per turn, times hundreds of turns. Batching questions
   into one call helps (that is the claim), but per-chunk visibility over a large chunk store is
   still many scores per turn.
3. **Accuracy on unfamiliar states is unknown.** The published accuracy is on the author's
   benchmark. Harness questions (is this grep hit relevant, is this context stale) are probably
   off-distribution. Every app would need its own agreement study.
4. **The errors that matter most are silent.** Tool-pick errors are visible. Context-visibility
   errors are not. The most valuable use is also the hardest to evaluate.
5. **The System-One call is itself a model call over raw state.** If the app uses a hosted Jev,
   its chunks, files, and secrets go to that provider. For RouteForge's customers, who are
   on-prem and often air-gapped, this means either a local open model (availability is thin
   today; the owner is waiting for one, see `jev-model.md`) or routing the scoring call through
   RouteForge's policy gate.
6. **Model supply.** TypeSafe's Jev is a hosted product. The open copies (AutoJev-27B,
   AgentJev-0.6B) are early and come with the evidence caveats in §2.2.

---

## 7. Where the boundary falls

| Concern | Owner | Reason |
|---|---|---|
| Eligibility, residency, PII gate, fail-closed | **RouteForge, deterministic** | A wrong probabilistic answer breaks a guarantee. A model may only tighten. |
| Tier or difficulty signal for a single call | **RouteForge, optional advisory model term** | The effect is bounded to cost and quality, and it ranks only inside the eligible set. |
| Switch cost and cache affinity across a trajectory's calls | **RouteForge, deterministic** (not built) | It is arithmetic over prices and cached tokens. No model is needed, and it guards against the routing trap. |
| Context assembly, chunk visibility, tool pick, sub-agent context, conditional instructions | **Application** | They need explicit app state the gateway never sees. |
| Cache reuse or rebuild | **Application** decides; **RouteForge** supplies the prices and cached-token facts | The app holds the state; RouteForge holds the economics. |
| File-sensitivity prediction | **Application** predicts; **RouteForge** enforces on observed content | Two layers that back each other up. |
| The System-One scoring call itself | **Governed by RouteForge** if it is ever routed through it | It reads raw state, so placement rules apply to it as to any model call. |

---

## 8. Recommendation and order of work

1. **No stochastic live routing (C).** Keep learning offline: the outcome tables, P2b-3
   calibration, the feasible-oracle bench, and the simulator. Revisit only for an opt-in app
   with seeded, recorded propensities.
2. **First: deterministic switch-cost and cache-affinity scoring in step 6.** This is
   already on the backlog as caching-cost F2 and trajectory step 4, and it does not depend on
   any System-One model. It is the RouteForge-side answer to the PDF's main economic argument,
   and it is a precondition for (3). Otherwise a better classifier makes per-task cost worse.
3. **Then: a System-One difficulty/category signal (B), in shadow mode.**
   - Run it after the gate, on an onboarded on-prem endpoint, on a sample of traffic.
   - Record its tier next to the length tier in `routing_outcomes`, and act on nothing.
   - Measure how often the two disagree and what outcome follows (FeasibleRouteBench and
     human labels).
4. **Promote it to a live, advisory scoring term only if shadow mode shows a feasible-oracle
   gain.** Guard-rails:
   - a dead band (for example 0.45–0.55) that falls back to the length rule;
   - the recorded probability, model hash, and threshold;
   - opt-in per app;
   - its version folded into `sp.routing_policy_version`;
   - raise-only on any protection effect.
5. **For applications:** publish guidance, not features.
   - Tool pick and permissions (tighten-only) are good first uses of System-One models.
   - Context visibility has the highest payoff but needs a harness rewrite and an eval of its
     silent failures.
   - Delegation saves money only with a purpose-built small context.

   On the RouteForge side, offer:
   - **validated, capped hints** (trajectory id, step type, desired tier);
   - **price and cached-token facts**;
   - **a governed path for the scoring call** (needs a non-chat endpoint type, which is not
     built).
6. **For the paper:** two comparisons are worth stating as hypotheses, not results:
   - "semantic vs length tiering, with and without switch cost, against the feasible
     trajectory oracle";
   - "per-call classifier accuracy does not equal per-task cost reduction".

   No numbers exist yet.

---

## 9. Notes for harness-lab

harness-lab is a separate project. It is an application-side harness meant to improve itself
from Langfuse data. This section collects what from this analysis carries over to it, and what
RouteForge's Langfuse data does and does not give it. The harness-lab design is not described
here; these are constraints and suggestions only.

### 9.1 Which side of the boundary harness-lab is on

Every decision in §6 (tool pick, context visibility, cache reuse, delegation, permissions,
conditional instructions) is a **harness-lab decision**, not a RouteForge one. RouteForge
stays the governed path for the model calls the harness makes, and for any System-One scoring
call that reads raw state.

### 9.2 What harness-lab can learn from RouteForge's Langfuse traces

Checked in `app/obs/export.py` and `app/api/routes.py`.

- **Always present, with no content in them:**
  - the routing certificate: `sp.chosen`, `sp.candidates`, `sp.scoring`, `sp.model`,
    `sp.reason`, `sp.routing_policy_version`, `sp.endpoint_registry_version`;
  - cost: `sp.cost_total_usd`, `sp.cost_baseline_usd`, `sp.cost_cache_saved_usd`;
  - the protection posture: `sp.placement`, `sp.model_input_mode`, `sp.client_output_mode`,
    `sp.telemetry_io_mode`, `sp.detection_mode`, `sp.cross_axis_rules_fired`, and the rest of
    the certificate;
  - entity **types** and the sensitivity class: `sp.detected_entity_types`, `sp.sensitivity`.
- **Grouping a harness run.** Send the `X-RouteForge-Session` header to get
  `langfuse.session.id`, which groups the calls of one run. `X-RouteForge-Trajectory` links
  calls into a trajectory envelope, and `X-RouteForge-Workload` labels the workload. **Without
  these headers, harness-lab cannot rebuild its own turns from Langfuse.**
- **Prompt and completion text** depend on the app's resolved `capture_io`
  (`resolve_capture_io`):
  - `off`: nothing;
  - `redacted` (the default): a copy with PII removed. It is useful for learning, but the
    entity values are gone;
  - `full`: raw I/O, but **only into the app's own Langfuse project.** If that project is
    missing, it silently becomes `redacted`. If no detector ran, `redacted` becomes `off`.

  Raw-text learning (for example, "was this chunk needed?") needs `capture_io=full` on a
  dedicated harness-lab app with its own project, plus the deployment ceilings that allow it.
- **Not in Langfuse at all:** the harness's own state. That means the chunk store, which
  chunks were hidden, which tools were offered vs picked, and the System-One probabilities.
  harness-lab must **log its own decisions** (question, options, probabilities, model
  version, threshold, outcome) next to the trace, with a shared id. Otherwise the improvement
  loop cannot tell which decision caused which result.
- The content-free `routing_outcomes` and `outcome_feedback` tables live in RouteForge's DB,
  not in Langfuse. They are exported via the admin API (P2b-2).

### 9.3 Lessons from RouteForge's improvement work that apply to a self-improving harness

1. **Recommend offline; a human approves.** RouteForge's quality calibration (P2b-3) never
   changes routing on its own. It produces a recommendation, a replay diff, and an admin apply.
   A harness that rewrites its own thresholds or prompts with no gate can drift silently.
2. **Record decisions; don't recompute them.** Store the probability and model hash for each
   System-One decision, so a replay uses what happened rather than what a re-run would say
   (§4, con 4). Use a dead band around thresholds, as in `jev-model.md` §4.
3. **Evaluate silent failures on purpose.** Tool-pick errors show up in traces. Hidden-chunk
   errors do not. They only show up as a wrong final answer. You need a counterfactual (re-run
   a sample with the chunk shown) or labelled tasks. Langfuse alone will under-report them.
4. **Judge self-preference.** If the judge or decision model shares a base with the model
   doing the work (AutoJev/Qwen3.8), "improvement" can mean "more like the judge". Check
   agreement per served model, or use a judge from a different family.
5. **Measure per task, not per call.** The routing trap (§2.1) applies inside the harness too.
   A change that makes each call cheaper can make the finished task cost more. Score sessions
   or trajectories (group by `langfuse.session.id`), not single generations.
6. **Tighten only, for anything protective.** Self-improvement may make permissions stricter
   on its own; loosening them needs a human.
7. **Separate measured from illustrative.** The PDF's token shares and prices are estimates.
   Re-measure them on harness-lab traces before tuning toward them.

### 9.4 Harness experiments worth running, in priority order

1. Tool pick with a System-One model vs every schema in context: tokens per turn, tool
   accuracy, task success.
2. Query-aware chunk visibility vs append-only vs blind compaction: tokens, task success, and
   a silent-failure audit on a sample.
3. Delegating with a small, purpose-built context vs the full transcript vs staying on
   frontier: cost **per task**, checked against the PDF's 4.15 / 6.19 arithmetic using real
   cached-token prices.

---

## 10. Sources

- *Jev Engineering for Coding Agents* (PDF linked above). Independent synthesis, not affiliated
  with TypeSafe. Its own figures are illustrative; fastcontext figures are as it reports them.
- `docs/roadmap/system-one-jev-model.md`: the System-One decision memo (2026-09-24).
- `docs/archive/design-handoffs/RouteForge-Agentic-Trajectory-ArXiv-Extension.md` §2, §9–12: switch cost and
  trajectory objective; agent hints are inputs, not trusted declarations.
- `docs/archive/design-handoffs/RouteForge-Priorities-1-3-LLM-Handoff.md` §12–18: feasible-set masking before
  normalisation; exploration only inside the feasible set.
- `docs/architecture/multidimensional-policy-model.md` §4.2, §10–11: complexity is separate from
  sensitivity; hard constraints vs optimisation preferences.
- Code: `app/pipeline.py:933`, `config/routing.yaml:61`, `app/routing/strategy.py`,
  `app/policy/trajectory.py`.
