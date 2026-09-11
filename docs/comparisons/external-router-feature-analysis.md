# External Router Feature Analysis — Plano, Devin Fusion, and the Multi-Model Report

**Date:** 2026-08-28
**Status:** Analysis / recommendation (no code changes yet)
**Scope:** Compare RouteForge's routing + gateway layer against three external sources, decide
what to adopt, adapt, or reject — and answer directly: *should a small LLM decide which model
to use?*

---

## 0. Sources analyzed

| Source | What it is | Routing philosophy |
|---|---|---|
| **[Plano](https://github.com/katanemo/plano)** (Katanemo, ex-DigitalOcean Inference Router) | AI-native Envoy-based proxy / data plane for agentic apps. Config-driven agents in YAML. | **Preference-aligned LLM router.** A small model ([Arch-Router 1.5B](https://huggingface.co/katanemo/Arch-Router-1.5B) / [Plano-Orchestrator 4B](https://huggingface.co/katanemo/Plano-Orchestrator-4B)) reads the request, infers *(domain, action)*, maps it to operator-defined preferences → model. |
| **[Devin Fusion](https://cognition.com/blog/devin-fusion)** (Cognition) | Multi-model orchestration for coding agents. 60% cost cut at frontier quality; 88% of merged PRs driven by the router. | **"Sidekick."** A frontier *Lead* stays in the loop and delegates mechanical work to a cheap *Sidekick*; both keep separate persistent caches; model switches happen "for free" during compaction. |
| **`info.pdf`** — *Feature Analysis Report: Frontier AI Model Routing and Orchestration Frameworks* | Synthesis report comparing the two above (+ DigitalOcean Inference Router). Catalogs the feature set of the category. | Frames the market as **"Sidekick vs. Classifier"** — mid-session delegation vs. pre-task selection. |

All three converge on one thesis that is **already RouteForge's cost-reduction guarantee**: stop
paying frontier prices for mechanical work; match task complexity to model capability; keep a
resiliency/failover story. So this is an apples-to-apples comparison, not a new direction.

---

## 1. The central question: should a *small LLM* decide which model to use?

You keep reading this pattern (Plano's 4B orchestrator, Devin Fusion's "lightweight classifiers").
Here is the honest assessment for **RouteForge specifically**.

### What the small-LLM router actually buys you

RouteForge's step-6 model selection today is **length-based, not semantic**. Complexity is literally
`min(1.0, tokens / 2000)` (`app/routing/task_mode_llm.py:93`) bucketed by static thresholds
(`app/routing/router.py:43-49`, `config/routing.yaml:32-35`). That heuristic is genuinely weak:

- *"Prove Fermat's Last Theorem."* → short → **cheap tier**. Wrong.
- A 1,500-token boilerplate log-formatting request → **frontier tier**. Also wrong.

Plano's insight — decoupling **routing policy** (operator preferences over *domain × action*) from
**model assignment** — is a real answer to that weakness. A small classifier reading the *content*
picks a better tier than counting tokens ever will.

### Why you should NOT let it be the *decision-maker* in RouteForge

RouteForge's product identity is three guarantees — **privacy-first, cost-reducing, observable** —
and the observability one is load-bearing: *"every decision is captured in a `DecisionRecord`… the
'why' is the whole point."* An opaque LLM verdict fights that on three fronts:

1. **Auditability.** A deterministic score (`quality·q − cost·c − latency·l + capacity`,
   `app/routing/strategy.py:55-62`) is explainable on a projector to a customer's engineers. "The
   4B model said so" is not. Customers deploying on-prem, air-gapped, in regulated settings need
   the reproducible verdict.
2. **Fail-closed (rule #5).** An LLM router is a new failure/latency surface that must itself
   fail closed. A deterministic scorer can't "error."
3. **Ordering (rule #1: nothing external before the gate).** A router-LLM is an external model
   call. It **cannot** run before step 2, and it must respect `allowed_providers` or it can break
   `onprem_only` (rule #3).

### The RouteForge-native answer: advisory signal, not decision-maker

Adopt the small-LLM router as a **richer complexity/domain *signal into* the existing deterministic
scorer**, never as a replacement for it. This is not a compromise — it's the pattern RouteForge has
**already** established twice:

- **`stage_signals`** (`app/routing/signals.py`, default OFF, `config/routing.yaml:83-92`) feeds a
  **raise-only, residency-clamped** tier bias from agent-loop state. It can nudge but the residency
  filter still caps it — it can *never* lift `onprem_only` to cloud.
- **`task_mode_llm.py`** already exists: the step-4 classifier is *"a routed call"* that goes
  through steps 5–7, respects `allowed_providers`, emits a `DecisionRecord`, and **falls back to the
  deterministic `decide()` on any error** (`app/routing/task_mode.py:9-29`,
  `app/routing/task_mode_llm.py:55-136`). It's off by default by design.

So the move is: **an optional, onboarded, on-prem small model produces a `semantic_complexity` /
`domain` signal; the deterministic filter-then-rank pipeline still makes and logs the decision.**
Because the classifier is itself a routed call constrained to `allowed_providers`, it can run on the
customer's own on-prem endpoint (Arch-Router is 1.5B — trivially self-hostable), preserving the
air-gap. The residency filter still runs first, so it cannot break `onprem_only`. And if the model
is slow, unavailable, or garbles output, we fall back to `tokens/2000`, fail-closed intact.

**Verdict: yes to a small LLM as an advisory complexity signal (opt-in, on-prem, fallback to
length). No to a small LLM as the routing authority.** This directly upgrades the weakest part of
the current router while keeping every non-negotiable rule.

---

## 2. Feature-by-feature catalog

Legend — RouteForge status: ✅ have · 🟡 partial · ❌ absent (all confirmed in code, refs below).

| # | Feature (source) | What it does | RouteForge status | Fit / recommendation |
|---|---|---|---|---|
| F1 | **Preference-aligned LLM router** (Plano) | Small model infers *(domain, action)* → operator preference map → model. | 🟡 length-based tier only; LLM task-mode classifier scaffolded but OFF (`task_mode_llm.py`) | **ADAPT (high).** Add as advisory `semantic_complexity`/`domain` signal into the scorer. §1. |
| F2 | **Cache-aware routing / model affinity** (Plano + Cognition) | Stable session key pins the model across turns to reuse a **warm prompt cache** (up to ~10× cheaper cached input). | ❌ absent — router re-selects from scratch **every call and every agent hop** (`runtime.py:135-137`); `session_id` used only for memory writes (`pipeline.py:418-420`) | **ADAPT (high).** Biggest concrete cost lever missing. §3.1 |
| F3 | **Switch-cost / routing budget** (DigitalOcean) | `X-Routing-Max-Switch-Spend-Pct` (default 20%): only switch models if rebuilding context is "worth it" vs. the cache-miss cost. | ❌ absent (no notion of switch cost) | **ADAPT (med).** The guardrail that makes F2 safe. As per-app config, **not** a client header. §3.1, §4 |
| F4 | **Named selection policies** — Optimal / Cost / Speed (Plano/DO) | Preset weight profiles per business KPI (min latency vs. strict budget vs. quality). | ❌ absent — single **global** weight dict (`config/routing.yaml:15`); strategy registry has only `"default"` (`strategy.py:65`) | **ADOPT (high, low-risk).** Ship as per-app weight presets. Maps cleanly onto existing per-app config. §3.2 |
| F5 | **Real-time "fastest" routing** — rolling 30-min window (DO/Plano) | Route on *live* latency, not stale benchmarks. | ❌ absent — latency is a **static per-tier constant** `{cheap:0.2, mid:0.5, frontier:1.0}` (`strategy.py:26`); real latency measured but only logged (`litellm_backend.py:91`), never re-enters the score | **ADOPT (med).** Feed measured latency back as an EWMA. Makes the "Speed" policy (F4) real. §3.3 |
| F6 | **Fallback / failover** (DO) | Auto-failover to secondary models on rate-limit/outage. | ✅ have — lateral same-tier switch + tier escalation on backend error, bounded by `max_failover_attempts` (`gateway.py:63-119`, `config/routing.yaml:53`) | **KEEP.** But note gaps → F7. Streaming path has **no** failover (`gateway.py:137-162`) — worth fixing. |
| F7 | **Circuit breaker / provider health** (implied by DO resiliency) | Keep a downed provider out of the pool across requests; distinguish rate-limit from other errors. | ❌ absent — `failed_models` is **per-request only**; a downed provider is re-tried on every new request; `except Exception` treats 429 like any error (`gateway.py:82`) | **ADOPT (med).** Real reliability gap. §3.4 |
| F8 | **Sidekick / Lead-model delegation** (Devin Fusion) | Persistent frontier *Lead* + cheap *Sidekick* run in parallel with separate caches; Lead stays "in the loop." | ❌ absent — single NAT `tool_calling_agent`, one LLM config (`sp_router`), each hop re-routes independently and **sequentially** (`runtime.py:130-189`) | **MOSTLY REJECT / partial-adapt.** §5 — big architectural change; the *cache-affinity* half (F2) is the part worth stealing, not the two-agent harness. |
| F9 | **Compaction-time model switching** (Devin Fusion) | Switch tier "for free" during a compaction event, since it forces a cache miss anyway. | ❌ absent (no affinity to preserve, no compaction hook into routing) | **ADAPT (low, later).** Elegant once F2 + Headroom (step 5) exist — switch tier when Headroom compacts, since the cache dies regardless. §3.5 |
| F10 | **Manual model ranking** (DO) | Hard-coded model priority sequence for mission-critical paths. | 🟡 partial — per-app `allowed_models` scoping + requested-model pinning exist (`router.py`), but no ordered preference list | **ADOPT (low).** Small extension of existing per-app scope. |

---

## 3. Recommended adoptions (grounded in RouteForge's rules)

### 3.1 Cache-aware routing + switch-cost budget  *(F2 + F3) — highest cost ROI*

**The gap:** RouteForge re-runs `Router.select` from scratch on every call *and every agent hop*
(`app/agent/runtime.py:135-137`). The report's central operational point is that **cached input can
be ~10× cheaper than uncached**, so a needless model switch that triggers a cache miss can *erase*
the savings from a cheaper per-token price. RouteForge is currently paying that penalty on every
multi-hop request — invisibly, because cost is measured but the *counterfactual* cache miss isn't.

**The adaptation (RouteForge-native):**
- Derive a stable **session/prefix key** (there's already a `session_id`; extend it into the router).
- Add **model affinity**: within a session, prefer the previously-chosen model *when it's still
  eligible after the residency + budget filters*. Affinity is a **tie-break/bias**, never an override
  of the policy filters — `onprem_only` still filters cloud out first (rule #3).
- Add a **switch-cost budget** (per-app config, default ~20%): only switch to a "better" model if the
  score gain outweighs the estimated cache-rebuild cost.
- **Residency partition it exactly like the existing semantic cache does** — that cache already
  partitions by `(tenant_id, onprem_only, allowed_providers)` and refuses to serve a cloud entry to
  an `onprem_only` request (`app/cache/store.py:53-59`). Reuse that discipline so affinity can never
  leak across the residency boundary.
- **Emit it in the `DecisionRecord`:** "stayed on model X for cache affinity (switch cost 34% >
  budget 20%)." The *why* stays auditable.

### 3.2 Named selection policies — Optimal / Cost / Speed  *(F4) — best effort/value ratio*

Today there's one global weight vector and a strategy registry with a single `"default"` entry
(`config/routing.yaml:15`, `app/routing/strategy.py:65`). Ship **preset weight profiles** and let an
operator select one **per app** (RouteForge already does per-app `pii_detection` and `stream_mode` —
this is the same seam):

```yaml
# routing.yaml — sketch
selection_policies:
  optimal: {quality: 1.0, cost: 0.6, latency: 0.3, capacity: 0.2}   # current default
  cost:    {quality: 0.5, cost: 1.0, latency: 0.2, capacity: 0.2}
  speed:   {quality: 0.5, cost: 0.3, latency: 1.0, capacity: 0.2}
```

Low risk (it's just weight vectors through the existing scorer), high demo value, and it makes the
cost-vs-latency trade-off a first-class operator control instead of a code change.

### 3.3 Real latency into scoring  *(F5)*

The latency term is fabricated (`_L = {cheap:0.2, mid:0.5, frontier:1.0}`, `strategy.py:26`) while
**real latency is already measured** and thrown away into the `DecisionRecord`
(`litellm_backend.py:91`). Feed it back as a per-model **EWMA** (or rolling window) so the "Speed"
policy (F4) reflects current conditions, not a constant. Keep a static prior for cold models.

### 3.4 Cross-request circuit breaker  *(F7)*

`failed_models` lives for one request (`gateway.py:82-94`). Add a lightweight breaker that (a) marks
a provider unhealthy across requests after N failures with a cooldown, and (b) distinguishes 429 /
rate-limit from hard errors (currently a blanket `except Exception`). Also give `run_stream` the
failover loop `run` has (`gateway.py:137-162`). This is a genuine reliability gap, independent of the
cost features.

### 3.5 Compaction-time tier switching  *(F9) — later, once F2 lands*

Devin Fusion's neat trick: because a compaction event forces a cache rebuild anyway, that's the
*free* moment to change tier. RouteForge has the two pieces to make this native — **Headroom** (step
5 compression) and, once F2 exists, model affinity. Hook: when Headroom compacts a hop's context,
release affinity and let the scorer re-pick tier "for free." Defer until F2 is in.

---

## 4. Explicitly reject / adapt-with-care

- **Client-supplied routing headers** (`X-Model-Affinity`, `X-Routing-Max-Switch-Spend-Pct`). In a
  single-tenant dev proxy these are client conveniences. In RouteForge — **multi-tenant, policy-gated
  — a client must not be able to override the operator's policy from a request header.** Adopt the
  *mechanics* (affinity, switch budget) but drive them from **per-app operator config**, not raw
  inbound headers. A header could at most *request* a preference the policy is free to ignore.
- **Small LLM as the routing *authority*** (Plano's core design). Rejected as the decision-maker for
  the auditability / fail-closed / ordering reasons in §1. Adopt it as an *advisory signal* only.
- **Routing on live third-party leaderboards** (Arena / Artificial Analysis, as Plano's "Optimal"
  policy does). RouteForge deploys air-gapped; it can't phone home to a benchmark service. Quality
  priors must stay local/config-driven (`app/routing/quality.py`). Fine as an *offline* input an
  operator bakes into config; never a runtime dependency.

---

## 5. On the "Sidekick" architecture (Devin Fusion)  *(F8)*

The Sidekick model is compelling and it's where the report lands for *high-ambiguity* work: keep a
frontier **Lead** in the loop, delegate mechanical work to a cheap **Sidekick**, both with persistent
caches. But it's a **large architectural change** and it partly conflicts with a RouteForge
invariant:

- RouteForge's agent path is a **single** NAT `tool_calling_agent` where *"every LLM call re-enters
  compression (5) and the router (6)"* — per-call routing is the single most important behaviour to
  show in the UI. A persistent two-agent Lead/Sidekick harness with sticky per-agent model binding
  cuts against "re-route every hop."
- What RouteForge *already* achieves via per-hop routing is the same *outcome* — cheap and expensive
  tiers mixed within one task — just **without the persistent warm-cache affinity**. So the valuable,
  extractable half of Sidekick is **F2 (cache affinity)**, not the two-agent topology.

**Recommendation:** don't rebuild the Lead/Sidekick harness now. Capture its win through F2 + F4 +
F9. Revisit a true Lead/Sidekick split only if evidence shows the per-hop router degrades quality on
*ambiguous, multi-turn* tasks (the report's stated Sidekick sweet spot) in a way affinity can't fix.
If revisited, it belongs behind the NAT runtime as an opt-in workflow, still emitting `DecisionRecord`
per delegated call.

---

## 6. Priority summary

| Priority | Item | Why |
|---|---|---|
| **P1** | **F4 Named selection policies** (§3.2) | Lowest risk, high operator + demo value, reuses per-app seam. |
| **P1** | **F1 Advisory semantic-complexity signal** (§1) | Fixes the weakest part of the router (`tokens/2000`); scaffolding already exists; answers the core question. |
| **P2** | **F2+F3 Cache-aware routing + switch budget** (§3.1) | Largest *cost* lever missing; RouteForge silently pays cache-miss penalties per hop today. |
| **P2** | **F5 Real latency into scoring** (§3.3) | Data already measured & discarded; makes "Speed" policy real. |
| **P3** | **F7 Cross-request circuit breaker** (§3.4) | Reliability gap; streaming path also lacks failover. |
| **P3** | **F10 Manual ranking** | Small extension of per-app scope. |
| **Later** | **F9 Compaction-time switching** (§3.5) | Elegant once F2 + Headroom coexist. |
| **Reject/park** | **F8 Sidekick harness**, client routing headers, LLM-as-authority, live leaderboards | §1, §4, §5. |

---

## 7. The through-line

Every worthwhile idea in these frameworks is adoptable **only in RouteForge's grammar**: as a
deterministic, residency-clamped, `DecisionRecord`-emitting extension of the filter-then-rank
pipeline — never as an opaque authority that runs before the gate or overrides operator policy. The
single most important reframing of your question:

> A small LLM should *inform* RouteForge's routing decision. It should never *be* the decision.

That keeps privacy-first + fail-closed + observable intact while finally replacing `tokens / 2000`
with something that reads the actual request.
