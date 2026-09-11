# IP-Worthy Features and Research Opportunities

**Status:** analysis, 2026-08-31. Not a commitment; a ranked menu.
**Method:** five parallel scans — repo inventory; commercial/OSS + patent landscape; arXiv on
routing/cascades; arXiv on privacy/governance; arXiv on caching/compression. Synthesis is mine.

> **Verification caveat — read before citing anything here.** The arXiv IDs below came from
> automated search and are *leads, not citations*. A meaningful share of the 2026-dated IDs
> (`26xx.xxxxx`) could not be independently confirmed in this pass. Verify every ID against
> arxiv.org before it goes in a paper, a filing, or a slide. The patent numbers in §1 *were*
> pulled directly from Google Patents / USPTO PPUBS and are more reliable, but the search was
> incomplete: no assignee-scoped search was run against Microsoft, Google, NVIDIA, Salesforce,
> Adobe, or ServiceNow, all of whom almost certainly hold filings in this space. Treat §1 as a
> floor on prior art, never a ceiling.
>
> **Nothing here is legal advice.** Patentability, claim scope, and freedom-to-operate are
> questions for counsel. See §4 for the one sequencing fact that matters most.

---

## 0. The thesis

There is one structural asset in RouteForge that neither the market nor the literature has,
and everything worth patenting or publishing is a corollary of it:

> **RouteForge treats the set of eligible models as a per-request quantity derived from policy
> *before* any optimization runs. Everyone else treats the candidate pool as a fixed constant.**

That is not a marketing distinction. It is a different mathematical problem.

- **The routing literature** — RouteLLM (2406.18665), FrugalGPT (2305.05176), GraphRouter
  (2410.03834), RouterDC (2409.19886), IRT-Router (2506.01048), UniRoute (2502.08773), and every
  benchmark from RouterBench (2403.12031) to RouterEval (2503.10657) — optimizes
  cost/quality/latency over a pool assumed **fixed, known in advance, and uniformly reachable**.
  Not one models a request-dependent eligible subset. Compliance, where it appears at all,
  appears as a downstream classifier gating the router, never as a constraint the router's own
  objective must respect.
- **The market** does post-hoc filtering. Portkey redacts PII as an optional guardrail. Kong
  filters prompts statelessly per call. Databricks does ABAC on fixed endpoints and doesn't
  route. Bedrock IPR routes within one model family. LiteLLM is transport and reliability.
  None of them make cloud candidates *unreachable by construction* before scoring.
- **The privacy literature** frames residency as a cryptographic problem — TEEs
  (2409.19134, 2509.18886), split inference (2605.23158, 2602.16760). Nobody frames it as a
  *candidate-filtering* problem.

RouteForge's rules 1, 3, and 8 (nothing external before the gate; `onprem_only` is absolute;
residency beats the risk floor) already encode the answer, and `Router.select` already enforces
the filter/rank split structurally — a `RoutingStrategy` cannot reintroduce a residency-filtered
candidate because it never sees one (`app/routing/strategy.py`, `app/routing/router.py:43-49`).

**Everything below is either (a) making that asset explicit and defensible, or (b) an
uncontested gap adjacent to it.** Ideas that are merely good but crowded — a better tier
classifier, a nicer dashboard, another cascade calibration scheme — are deliberately excluded.

---

## 1. What the prior art already forecloses

Establish this first, because it kills several otherwise-attractive ideas.

| Patent / app | Assignee | Filed | Claims, roughly | What it forecloses |
|---|---|---|---|---|
| **US 12,105,844 B1** | HiddenLayer | 2024-03 | Intercepting proxy detects + tokenizes PII in GenAI **outputs**, classifies type, redacts selectively **per requestor-class policy** | Generic "proxy redacts output PII by type and requestor" is taken. Our step-9 egress redaction is *not* novel on its own. |
| **US 12,236,193** | Cast AI | 2024-04 | Classify request → select **cheapest LLM meeting a quality bar**; also prompt-similarity token compression | "Cheapest model above a quality threshold" is taken. Plain cost/quality tier routing is not novel. |
| **US 12,259,913 B1** | Inventus | 2024-02 | Hybrid semantic + lexical LLM cache, merged by reciprocal rank fusion | Generic semantic caching for LLM Q&A is occupied. |
| **US 2025/0165752 A1** *(published app)* | Infobip | 2023-11 | Capability classifier routes among providers; **also** caching, query compression, health monitoring, cost/accuracy intent routing | Broad and uncomfortably close to generic router-plus-cache-plus-compression. Assume the obvious combinations are contested. |
| **US 12,068,944 B2** | Netskope | 2023-06 | Multi-tenant cloud tags routes with **locality/residency**, enforces via telemetry + remediation | Generic cloud residency routing exists — but it is **not LLM-inference-specific** and is telemetry-and-remediation (post-hoc), not filter-before-scoring. Adjacent, not a direct hit. |
| **US 12,541,616 B2** | SAP | 2023-11 | Per-attribute access layer gating which object fields an LLM may see | Field-level LLM data gating is taken. |
| **US 12,450,494 B1** | Citibank | 2024-12 | Validates agent proposed actions against guideline-derived expected actions | Agent action verification is occupied. |
| **US 12,174,954 B1** | HiddenLayer | 2024-05 | Detects leakage-tag "Easter eggs" in system prompts | Narrow; mostly irrelevant to us. |

**What this means concretely.** Do not build a filing around: output PII redaction by a proxy;
cheapest-model-above-quality-bar; semantic caching; or "router + cache + compression" as a
combination. Those are crowded or claimed.

**What survives** is the thing none of them have: **eligibility as a derived, per-request,
pre-optimization quantity, and the mechanisms that only make sense once you have it.**

---

## 2. IP-worthy features, ranked

Each entry: the mechanism, what already exists in the repo, what to build, why it is novel
against the prior art above, and the risk.

### Tier 1 — file first

---

#### IP-1. Monotone privacy-context ratchet across agent hops

**The single strongest idea in this document.**

**Mechanism.** The `ExecutionPolicyContext` is computed once at request entry and is immutable
for the request's life. That is correct for step 1 but *wrong for multi-step*: at hop 3, a tool
has returned a document, a retrieval has pulled a record, a model has emitted an entity — and
the request now contains sensitivity that did not exist at hop 1. The ratchet makes the context
**monotonically narrowing**: at every hop, newly observed content is re-gated, and the eligible
set may only shrink. Residency may tighten (`internal` → `onprem_only`), never loosen. A hop
that touches PHI permanently narrows every subsequent hop of that request. Formally: a lattice
of policy contexts with a meet operation, where hop *n+1*'s context is `meet(context_n,
gate(observed_n))` — a greatest-lower-bound that is provably non-increasing.

**Already in the repo.** `_stricter()` in `app/policy/context.py:15-16` is the meet operation,
applied once. The gate is re-runnable. Every hop already re-enters compression and the router.
The raise-only signal pattern (`app/routing/signals.py`) is the same shape applied to tiers.

**Build.** Re-gate tool outputs and retrieved content at each hop; apply `meet` to the carried
context; emit a `context_narrowed` event in the DecisionRecord with the triggering entity class
(never the value); assert in tests that eligible-set cardinality is non-increasing across hops.

**Why novel.** The agentic routing cluster — Agentic Routing (2607.11399), ACRouter
(2606.22902), TRACE-Router (2607.22465), Agora (2607.09600) — routes per hop on *harness state*
(task progress, difficulty, verifier score). **None routes on accumulating privacy state.** No
paper models a scenario where hop 3's pool is provably narrower than hop 1's because of what was
seen upstream. The closest adjacent work is OCELOT (2606.12341), which budgets *cumulative*
leakage across a trajectory — it establishes that per-hop-only checking is insufficient, but it
budgets rather than constrains eligibility. On the patent side, nothing in §1 touches multi-hop
context propagation at all; Citibank's 12,450,494 validates *actions*, not the eligible model
set. The claim shape — *an immutable policy context that may only narrow across hops of one
agent execution, where narrowing removes inference targets from the candidate pool before
scoring* — appears genuinely open.

**Risk.** Medium implementation cost: re-gating every tool output adds gate latency per hop, and
Presidio is not free. Mitigate with a content-hash skip for unchanged spans. Also a UX cliff: a
request can now become un-serveable mid-flight (pool narrows to empty) — that must be a clean,
explained rejection, not a crash. This is the same "derived reject" pattern as the empty on-prem
pool, so the semantics already exist.

---

#### IP-2. Redaction-cost accounting — over-redaction as a priced, first-class quantity

**Mechanism.** Redaction is currently a correctness feature with an invisible economic cost.
Every redaction changes the token count, which changes the complexity score, which can change
the tier, which changes the price — and it changes the cache key, which can turn a hit into a
miss. Make that measurable: a `RedactionImpact` record on every request carrying tokens
added/removed by redaction, the tier the request *would* have taken un-redacted, the cache
outcome delta, and the resulting USD delta. Then close the loop: surface per-app over-redaction
cost so an operator can see what a `pii_exempt_patterns` entry is *worth*, and tune exemptions
against evidence rather than complaints.

**Already in the repo.** All the levers ship: `pii_entity_denylist`, `pii_score_threshold`,
`pii_exempt_patterns` (value-shape, type-independent — merged in `2ad3157`),
`EffectiveDetector`/`FilteredDetector` composition (`app/policy/effective.py`). The DecisionRecord
already carries chosen + rejected candidates and post-compression token counts. The tier function
is `min(1.0, tokens_after_compression / 2000)` — *literally a function of token count*, so
redaction demonstrably moves it.

**Build.** Compute the counterfactual (un-redacted token count → tier) alongside the real path;
record both; aggregate per app; expose in the console next to the existing savings split.

**Why novel.** The privacy literature measures over-redaction against **task success** —
SlotGuard (2607.17147) shows generic redaction "significantly degrades task success" and typed
slots preserve it; MathEd-PII (2602.16571) shows numeric false positives in tutoring. **Nobody
measures redaction against downstream cost, tier selection, or cache-hit rate.** Both the
privacy scan and the caching scan independently flagged this as absent. The HiddenLayer patent
redacts by type per requestor class; it does not price the redaction. And the value-shape
exemption lever itself (exempting by the *shape of the value* rather than the entity *type*,
because Presidio assigns the same false-positive value different types depending on whitespace)
is a narrower unclaimed mechanism than anything in §1.

**Risk.** Low. Almost entirely additive instrumentation over paths that already exist. The
counterfactual tier is cheap to compute (it's arithmetic on a token count, not a second
inference). This is the best effort-to-value ratio in the document.

---

#### IP-3. Streaming entity hold-back with a stated guarantee — and a stated impossibility

**Mechanism.** The `incremental_verify` hold-back redactor
(`app/output/streaming_verify.py`) already uses a dual guard: a fixed 48-char window plus an
*unbounded* trailing-run regex `[\w.@+\-]+` that refuses to release a token run reaching the
buffer's end. The fixed window alone cannot bound arbitrary-length entities (email local-parts);
the trailing-run guard covers exactly that. Formalize this as a **release oracle** with a
provable property, and pair it with the honest negative result: *no streaming scheme can enforce
a whole-response predicate without unbounded hold-back* — which is why `verified_then_stream`
exists and why the floor is non-waivable.

**Already in the repo.** The redactor, the four stream modes, and the floor enforced twice
independently (admin write-time 422 for `onprem_only` + `stream_unverified`; runtime
`StreamFallback` in `pipeline.run_stream`).

**Build.** State the guarantee precisely (no entity of a class expressible as a bounded regex or
bounded-length token run is ever emitted in full across a chunk boundary); property-test it with
adversarial chunk splits at every offset of known entities; emit a per-chunk release certificate
into the trace.

**Why novel.** Every streaming-moderation paper found — SentGuard (2606.02041), hidden-state
probes (2606.10487), early-stop (2506.09996), Qwen3Guard (2510.14276) — targets **toxicity and
harm**, optimizes an *expected* accuracy/latency curve, and none claims a worst-case guarantee.
**No paper does streaming verification for PII entity leakage.** The chunk-boundary hold-back
problem appears genuinely unaddressed. Separately, the non-waivable floor (an app's chosen
stream mode cannot override `onprem_only`/`require_verification`) has no analogue in any product
surveyed.

**Risk.** Low to build. The honesty is the point: publishing the impossibility result alongside
the mechanism is *stronger*, not weaker, and pre-empts the obvious reviewer objection.

---

#### IP-4. Tamper-evident decision ledger bound to policy content hashes

**Mechanism.** Hash-chain the DecisionRecord stream (each record commits to its predecessor), and
bind every record to the **content hashes of the policy that produced it** —
`routing_policy_version` (sha256 over routing.yaml + pricing.yaml) and `app_policy_version`. This
gives two things no audit log gives: (a) tamper evidence, and (b) **retroactive re-verification** —
"replay every decision made under policy hash X and prove it was consistent with that policy,"
which is a fundamentally different capability from "here are the logs."

**Already in the repo.** Both version stamps ship (`11a0685` app_policy_version for stale-probe
detection; `sp.routing_policy_version` as a 12-hex content hash, emitted regardless of
`capture_io`). `sp.candidates` carries the chosen *and rejected* set with reasons. The substrate
exists; the chain and the replay do not. Today the audit trail is mutable rows and housekeeping
actively `DELETE`s.

**Build.** G3′ from the governance backlog (already ranked #1 there by ROI): append-only chained
records, an anchor/checkpoint scheme, and a `verify --policy-hash X` replay command.

**Why novel.** Traccia (2607.14309) generates OTel-based compliance evidence mapped to EU AI Act
articles; Policy-as-Prompt (2509.23994) logs provenance. **Neither binds a decision to a content
hash of the policy that produced it**, and neither supports replay-verification. The governance
literature's dominant assumption is that compliance is achieved by *mapping obligations to
evidence after the fact* — a documentation problem. Strategic Auditee Gaming (2605.06340)
formally shows why that is insufficient: continuous-audit regimes get gamed through coverage
gaps. A ledger that makes the decision-to-policy binding cryptographic is the structural answer
to exactly that finding. Nothing in §1 is close.

**Risk.** Medium. Chained ledgers and data-retention/deletion obligations (GDPR erasure) are in
tension — resolve with hash-only commitment to redacted content, never raw. Deletion of *content*
must not break the *chain*.

---

### Tier 2 — strong, more work

---

#### IP-5. Joint compression × cache × model-choice optimization under an eligibility constraint

**Mechanism.** Three savings levers interact and currently fight: compression reduces tokens but
*invalidates the prefix cache* (query-aware compressors produce a different prefix per call);
provider prompt-cache discounts (as low as ~1/30 the uncached price) mean the cheap model may be
the *more expensive* choice once you forfeit a warm cache on the expensive one; and the router
re-selects from scratch every hop, cold-missing caches it just warmed. Optimize them jointly,
subject to the eligible set.

**Already in the repo.** The residency-partitioned semantic cache
(`app/cache/store.py`) with partition key `(tenant_id, onprem_only, frozenset(allowed_providers))`,
enforced on write **and** independently re-checked on read. Post-compression token scoring. The
routing/pricing content hash. The parked F1–F5 caching roadmap.

**Build in order.** **F1 first** — cached-token accounting and cached-input pricing. This is a
live correctness bug, not a feature: RouteForge currently bills provider-cached hits at full
price, so every savings number it reports is *wrong in the conservative direction*. Then F2
(cache affinity + switch-cost budget), then F3 (stop stripping `cache_control` /
`prompt_cache_key` via `drop_params:True`).

**Why novel.** The Token Production Function position paper (2605.11733) explicitly names
compression, cache, and routing as interacting levers **and explicitly declines to formalize
their joint optimization while calling for it**. Cache-Aware Prompt Compression (2607.15516)
formalizes two of the three. CacheRouter (2608.22708) models provider cache-discount pricing as a
routing variable but within a fixed pool. The Harness Effect (2607.06906) separates two levers
and finds 41% of blended-cost reduction is attributable to harness design rather than model
choice. **Nobody spans all three, and nobody does it under a hard eligibility constraint.**

**Risk.** High complexity, and the eligibility constraint makes it harder, not easier — but that
constraint is also what makes it *ours*. Do F1 regardless of whether the rest happens; it fixes
reported numbers.

---

#### IP-6. Unified policy context as the single source for both model eligibility and tool capability

**Mechanism.** Today tools are forwarded to the backend untouched, the agent runs on a hardcoded
`sk-internal` loopback credential, and `agent_budgets.max_spend_usd` / `per_call_timeout_s` are
declared but dormant. Fix all of it from one place: mint a **per-run agent identity** whose
capability scope is *derived from the same `ExecutionPolicyContext` that constrains routing*. One
policy object, two enforcement surfaces — which models may be called, and which tools may be
invoked with what scope. An `onprem_only` request cannot call an egress tool, and that falls out
of the same object rather than a second parallel policy.

**Already in the repo.** The context, the immutability, the budget fields (dormant), the
governance backlog items G2′ (scoped per-tool permissions) and G4′ (per-run identity).

**Why novel.** The privacy scan found **zero results** across multiple query variants for the
combination of "agent identity," "least privilege," and scoped tool-credential delegation — a
striking absence given the volume of jailbreak/guardrail work. Governance by Construction
(2605.20874) proposes pre-execution checkpoints and CAGE (2607.29190) certifies tool
authorization, but neither derives tool scope from the same object that constrains model
selection. The unification is the claim.

**Risk.** Medium-high; touches the agent runtime and credential handling. Sequence after IP-1,
since the ratchet is what makes per-hop tool scope meaningful.

---

#### IP-7. TOCTOU-safe policy revocation — a kill switch that lands mid-flight

**Mechanism.** Every switch in RouteForge today takes effect on the *next* request. A policy
change during a long multi-step run does not touch the run in flight. Re-validate the
`app_policy_version` / `routing_policy_version` hash at each hop boundary; on mismatch, either
re-gate under the new policy or abort cleanly with a recorded reason.

**Already in the repo.** The version stamps and the stale-probe detection they were built for —
this is the same mechanism pointed inward at ourselves.

**Why novel.** "Verdict Staleness" (2608.26306) names exactly this TOCTOU class: a guardrail
approval computed at time T can be stale by execution time. It identifies the problem; **no
system surveyed enforces re-validation at hop boundaries.** RouteForge already computes the
hashes that make the check nearly free, which is an unusually good position to be in.

**Risk.** Low-to-medium. Define abort semantics carefully — partial work, partial spend, and what
the client sees.

---

#### IP-8. Zero-traffic cold-start routing for operator-onboarded pools

**Mechanism.** Every learned router in the literature needs training data: preference labels
(RouteLLM), an outcome matrix (RouterBench), bandit feedback (WISERouter 2607.23765), or
representative-prompt profiles (UniRoute 2502.08773). RouteForge boots with an **empty pool** in
an **air-gapped** deployment with **no traffic and no labels**, and the operator onboards
arbitrary endpoints at runtime. Turn the existing probe-gated onboarding into a capability
profiler: a fixed local probe battery run at onboard time yields a capability vector, which seeds
routing from request one and is refined online.

**Already in the repo.** Onboarding is *already probe-gated* — the probe exists, it just answers
"is it alive" instead of "what is it good at." `ModelRegistry`, `has_healthy_onprem()`, and the
derived-reject-on-empty-pool semantics.

**Why novel.** The routing scan flagged air-gapped/zero-data bootstrapping as an explicit
unstudied setting. UniRoute is closest but still requires offline representative-prompt profiling
per candidate *before* deployment — not a cold boot. And "Most of the LLM Routing Gap Is Task
Type" (2608.23023) plus LLMRouterBench (2601.07206) both suggest coarse signals capture most of
the achievable gain, which means a small probe battery may be *sufficient* — the field's own
recent negative results argue this is tractable.

**Risk.** Medium. Probe battery design is the whole problem, and it must run entirely locally.

---

### Tier 3 — defensive filings on shipped mechanisms

These are already built and working. They are cheap to file and they fence the core.

- **IP-9. Derived rejection from an empty eligible set.** No `inference_backend: none` flag; <!-- purged-ok -->
  instead `has_healthy_onprem()` false → `onprem` drops from `allowed_providers` → an
  `onprem_only` request has no eligible candidate → reject. AWS/Databricks/Azure route among
  *pre-declared* endpoints; none treats pool emptiness as itself the trigger for a hard reject
  rather than a silent cloud fallback. (`app/policy/context.py:63-64`)
- **IP-10. Residency as a cache-partition dimension with dual enforcement.** Partition key
  includes `(onprem_only, frozenset(allowed_providers))`, every entry self-describes its
  producing provider, and residency is checked **on write and independently again on read** — so
  a breach requires two independent bugs. The entire KV-offload literature (Mooncake, LMCache,
  CacheGen) treats moving cache to CPU/disk/network as a pure performance win with **zero**
  residency discussion. (`app/cache/store.py`)
- **IP-11. Raise-only, residency-clamped advisory signals.** Every heuristic or model that wants
  to influence routing — stage signals, confidence escalation, the proposed semantic-complexity
  LLM signal, a future drift radar — enters through the same shape: it can only *raise* a tier
  floor inside a `max(...)` that residency clamps afterward. It can never lower a floor and can
  never lift `onprem_only` to cloud. This is a reusable composition pattern for keeping ML
  signals **advisory rather than authoritative** in a regulated system. (`app/routing/signals.py`)
- **IP-12. Cross-file config lint as a boot gate.** Fails startup on semantic drift across four
  YAMLs — duplicate concern ownership, tier-name mismatch, missing price. Commercial gateways
  validate schema; nothing surveyed cross-validates *ownership* semantics. Modest, but nothing
  else looks like it.

---

### Explicitly not worth pursuing

- A better semantic/complexity tier classifier. Crowded, and 2608.23023 argues coarse task-type
  routing captures most of the gain anyway. Fix the *honest* gaps instead (below).
- Generic semantic caching, output PII redaction by a proxy, cheapest-above-quality-bar routing —
  see §1.
- Anything requiring RouteForge to host training or inference compute. Out of scope by rule 4,
  and the boundary is the product.

---

## 3. Research paper ideas

You have something almost no routing paper has: **a real, deployed, multi-tenant, policy-gated
system with production traces and a hard constraint the literature doesn't model.** Play that,
not algorithmic novelty — you will lose a benchmark-chasing fight with a lab and win an
"in-production system with a constraint nobody studies" fight easily.

Ordered by *expected acceptance × strategic value to RouteForge*.

---

**R1. "Eligibility-Constrained LLM Routing: When the Candidate Pool Is a Per-Request Variable"**
*Venue: MLSys, or NeurIPS Datasets & Benchmarks.*

The flagship. Formalize routing where the eligible set `E(request)` is derived from policy before
optimization, prove the composition properties (residency clamp dominates the risk floor; raise-only
signals cannot escape `E`), and — the real contribution — **release a benchmark with an
eligibility axis.** Take RouterBench/RouterEval's structure and add per-request eligibility masks
drawn from realistic policy profiles (healthcare/PHI, finance, air-gapped defense). Show that
published routers degrade badly or violate constraints when the pool is masked, because they were
never designed for it. This creates the axis and makes RouteForge its reference implementation.
Highest strategic value of anything in this document.

*Assets you already have:* the invariant, `sp.candidates` (chosen + rejected with reasons) as
ground truth, persona traffic generation, the residency clamp.

---

**R2. "The Price of Redaction: Over-Redaction as an Economic Quantity in LLM Gateways"**
*Venue: PETS, FAccT, or an ACL industry track.*

Both independent scans flagged this gap. The privacy literature measures over-redaction against
task success (SlotGuard, MathEd-PII); nobody measures it against **cost**. You can: redaction
changes token count → changes `min(1.0, tokens/2000)` → changes tier → changes price, and changes
the cache key → changes hit rate. Quantify the full chain on production traces across your three
exemption levers (entity denylist, score threshold, value-shape patterns). Include the
false-positive taxonomy you found the hard way — that Presidio assigns *the same value* different
entity types depending on surrounding whitespace, which is precisely why a type-independent
value-shape exemption was necessary.

*Assets:* every lever ships; DecisionRecords carry pre/post token counts; you have real
over-redaction incidents from a real deployment.

---

**R3. "Monotone Privacy Contexts for Multi-Hop Agents"**
*Venue: IEEE S&P, USENIX Security, or an OSDI/SOSP workshop.*

The IP-1 mechanism as a paper. Lattice formalization, the meet operation, the non-increasing
eligibility proof, and an empirical demonstration that single-entry gating is unsound: construct
agent tasks where hop 1 is clean and a tool output introduces PHI, then show that every surveyed
per-hop router (which conditions on harness state) happily sends hop 3 to cloud. OCELOT
(2606.12341) supports the premise that per-hop-only checking misses accumulating risk; you supply
the enforcement mechanism it lacks. Pairs naturally with R1 — publish R1 first to establish the
framing, then R3 as the multi-hop extension.

---

**R4. "What Streaming Verification Can and Cannot Guarantee"**
*Venue: USENIX Security or NDSS.*

Three parts, and the negative result is the strongest one:
1. **Impossibility:** no streaming scheme enforces a whole-response predicate without unbounded
   hold-back. Hence `verified_then_stream` and hence a non-waivable floor.
2. **Mechanism:** the dual-guard release oracle (fixed window + unbounded trailing-run), with the
   property it *does* guarantee, adversarially chunk-tested at every split offset.
3. **Taxonomy:** the four stream modes as the actual design space of latency-vs-guarantee, with
   measured TTFT for each.

Every streaming-moderation paper found targets toxicity, optimizes an expected-value curve, and
claims no worst-case property. Nobody does PII entity hold-back at all. A crisp impossibility
result plus a shipped mechanism plus honest limits is a very publishable shape.

---

**R5. "Attributing Savings: Decomposing Cost Reduction Across Routing, Compression, and Caching"**
*Venue: MLSys or IMC.*

A measurement-methodology paper. Vendors report blended savings numbers that are unfalsifiable.
You already refuse to (rule: track routing savings and compression savings separately against one
all-frontier baseline). Generalize to three levers, define a clean attribution methodology, and
demonstrate the interactions — including the one that embarrasses everyone: **compression
invalidates prefix caches**, so measured compression savings are systematically overstated when
cache effects are ignored. Note honestly that RouteForge's own accounting currently overstates
spend by billing provider-cached hits at full price (caching-fit F1) — fixing that *is* part of
the paper's contribution. 2607.06906 and 2608.23962 are the only papers cleanly separating even
two levers; 2605.11733 calls for exactly this and declines to build it.

---

**R6. "Cache Correctness Under Multi-Tenant Privacy Constraints"**
*Venue: CCS or USENIX Security.*

Unify two currently-separate literatures into one threat model: KV-cache timing side channels
across tenants (2608.09225) and embedding-level semantic-cache poisoning/collision (2608.01718,
2601.23088, with the NP-hardness result at 2603.03301). Add the third axis nobody has: **residency
constraints on caches holding original text** — the entire KV-offload line treats cache placement
as a pure latency/throughput problem with no boundary constraint. Present dual-enforcement
residency partitioning (check on write, re-check on read) as a defense with a stated cost in
hit rate. Your CCR rules (local-only, enforced TTL, never exported to telemetry) are the design
this literature is missing.

---

**R7. "Decision Records as Compliance Artifacts: From Evidence Mapping to Structural Enforcement"**
*Venue: FAccT, AIES, or an AI Act / policy-technical venue.*

Position + systems paper. The governance literature's dominant assumption is compliance-as-
documentation: map obligations to evidence after the fact (Traccia, the ISO 42001/NIST/EU AI Act
reconciliation cluster). Strategic Auditee Gaming (2605.06340) proves that regime is gameable.
Argue the alternative — compliance by construction, where non-compliant actions are *structurally
impossible* rather than *detected* — and use RouteForge as the existence proof: gate-before-scoring,
fail-closed on gate *error* (not just on detection, which is rarer in the literature than you'd
think), non-waivable floors, hash-bound decision ledger, and a "why" record carrying rejected
alternatives at every step. Governance by Construction (2605.20874) and CAGE (2607.29190) are your
allies; you're the deployed instance.

---

**R8. "Cold-Start Routing for Air-Gapped, Operator-Onboarded Model Pools"**
*Venue: MLSys, or an EMNLP/NAACL industry track.*

The IP-8 mechanism. Frame the setting the literature never addresses — no labels, no traffic, no
internet, a pool that starts empty and grows by operator action — and show a local probe battery
at onboarding time gets you most of the way. The field's own recent results (2608.23023,
2601.07206) arguing that coarse signals capture most achievable gain make this *more* plausible,
not less. Cite them as motivation.

---

**R9. Experience paper: "Correctness Bugs in a Production LLM Policy Gateway"**
*Venue: an industry/experience track (OSDI/ATC/SREcon-adjacent).*

Lower prestige, unusually high credibility, and cheap — you already lived it. Real, specific,
non-obvious failures worth writing down: the SSN-vs-spaCy dedupe bug where nondeterministic
`DATE_TIME`/`PHONE_NUMBER` tagging beat the SSN regex backstop because the backstop carried
`score=None→0.0`, fixed by seeding `score=1.0`; the delimiter-joined partition key proven
non-injective by a concrete collision (`("a\x1fx","b","c")` and `("a","x","b\x1fc")` join
identically), fixed with tuple keys; a fabricated per-tier latency constant that no one noticed
was never measured; multi-step latency summing to zero. The theme — *privacy invariants fail
through mundane engineering defects, not through clever attacks* — is a real contribution to a
field obsessed with adversarial robustness.

---

## 4. Sequencing, and the one thing to get right

**Do these first, regardless of IP or publication — they are correctness, and every number you
would put in a paper depends on them:**

1. **Caching F1: cached-token accounting.** Cost figures are currently wrong. Nothing that cites
   your savings numbers is safe until this lands. Blocks R5.
2. **Real measured latency into the scoring function.** `_L = {cheap:0.2, mid:0.5, frontier:1.0}`
   is a fabricated constant while actual latency is measured and discarded
   (`litellm_backend.py:91`). Any reviewer will find this. It is also just wrong.
3. **Cross-request circuit breaker.** `failed_models` is per-request only, and a bare
   `except Exception` treats a 429 like any other error.

**Then, in order:** IP-2 (cheapest, highest ratio, unblocks R2) → IP-4 (governance backlog already
ranks it #1 by ROI) → IP-1 (strongest claim, unblocks R3) → IP-3 (mostly formalization of shipped
code) → the rest.

**The sequencing fact that matters most.** Public disclosure — an arXiv preprint, a conference
talk, a public repo, a blog post — starts clocks and, in most jurisdictions outside the US,
destroys novelty for a later filing. The US grace period is limited and does not extend abroad.
Since several items here are *both* patent candidates and paper candidates (IP-1/R3, IP-3/R4,
IP-2/R2), **the filing decision has to precede the publication decision for each one**, and that
decision belongs with counsel, not with this document. The mechanisms in Tier 3 are already shipped
in a public repo, which is itself a fact worth raising with counsel early.

---

## 5. Honest limitations of this analysis

- **The patent search is incomplete.** No assignee-scoped search against the large filers. Google
  Patents' advanced assignee filter was not reachable from the tooling used. Treat §1 as a floor.
- **A meaningful share of the arXiv IDs are unverified**, particularly 2026-dated ones. Verify
  before citing. Some may not exist.
- **"Nobody does X" claims are bounded by search reach.** They mean "not found by targeted search
  across arXiv, vendor documentation, and patent databases in one pass." That is genuine evidence
  of a gap, not proof of one. The strongest claims here (no eligibility axis in any routing
  benchmark; no streaming PII hold-back; no residency constraint in the KV-cache literature) were
  each corroborated by more than one independent scan, which raises confidence but does not settle
  it.
- **Novelty ≠ patentability.** Obviousness, claim drafting, and freedom-to-operate are not
  assessed here and cannot be.
