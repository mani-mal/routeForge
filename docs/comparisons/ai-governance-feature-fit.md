# AI Governance Feature Fit — the "AI Control Tower" checklist vs. RouteForge

> **Superseded 2026-09-17:** the internal NAT agent and the multi-step path were
> removed; agent loops are client-driven. Anything below that reasons about an
> in-process agent hop, `agent_budgets`, or `routing.stage_signals`
> (`app/routing/signals.py`, also removed) describes a path that no longer exists.
> The equivalent setting is a client-driven loop where each hop is its own request.

**Date:** 2026-08-29
**Status:** Analysis / positioning + buildable-feature backlog (no code changes yet)
**Scope:** Map a widely-circulated *Agentic AI Governance* checklist ("The Real Agentic AI
Governance Checklist" / "AI Control Tower", R. Gohel) onto RouteForge's shipped feature
surface. Answer two questions: (1) **What can we already say we do**, in the governance
vocabulary buyers now use? (2) **What is worth building** to widen the customer base and
sharpen marketability — without breaking RouteForge's non-negotiable rules or product
boundary.

All ✅/🟡/❌ statuses below are confirmed against the code (refs inline).

---

## 0. Why this checklist matters to RouteForge

The checklist's framing device *is already RouteForge's pitch:*

> **Regular AI** — a closed loop. Prompt → generate → **human reviews** → human acts.
> Nothing happens without a human in the middle.
> **Agentic AI** — an open loop. Goal → agent decides → agent acts → chains actions →
> repeats. **No human checkpoint unless you build one in.**

RouteForge **is** the checkpoint you build in. It is the mandatory, OpenAI-compatible
chokepoint every model call passes through — direct calls *and* every hop of a multi-step
NAT agent run re-enter the policy gate and router (`app/agent/runtime.py`,
`app/agent/internal_endpoint.py`). The product already sells three guarantees
(privacy-first, cost-reducing, observable). This checklist gives us a **third vocabulary**
— *governance* — that regulated, air-gapped buyers (hospital / finance / gov personas) are
actively shopping for. Much of what we ship maps straight onto it; a handful of real gaps,
if closed, would let us position RouteForge as an **AI governance gateway**, not "just a
router."

The image is really two lists of the same six ideas — the six "Control Tower" beams and
the six "Governance Stack" columns. Consolidated into six governance domains:

| # | Governance domain (checklist) | RouteForge status | One-line |
|---|---|---|---|
| G1 | **Policy Engine** — enforce rules consistently across all agents | ✅ **have** (bounded) | Centralized gate, runs first, fail-closed; PII-centric, not a general rule DSL |
| G2 | **Permission Control / Scoped Permissions** — least privilege, scoped to the task | 🟡 **partial** | Strong per-*app* scoping; no per-task / per-tool scoping |
| G3 | **Audit Logs / Immutable Audit Trail** — record every action, tamper-proof | 🟡 **partial** | Every decision recorded; **not** immutable / tamper-evident |
| G4 | **Identity Management / Agent Identity** — verify every agent, know who acts | 🟡 **partial** | Per-*app* token identity; **no per-agent** identity |
| G5 | **Kill Switch & Rollback** — stop unsafe actions instantly, clean rollback | 🟡 **partial** | Many fail-closed toggles, all next-request; **no mid-flight abort, no rollback** |
| G6 | **Behavior / Drift Monitoring · Shadow Agent Sweeps** — detect anomalies & drift | 🟡→❌ **partial** | Rich observability; **zero anomaly / drift / shadow logic** |

**The headline:** two of six are a genuine "we already do this," and the four that are
partial (G3–G6) are exactly the four highest-value governance features to build. None of
them require RouteForge to host compute or cross its product boundary — they all live in
the gateway/policy/observability plane the product already owns.

---

## 1. Domain-by-domain: what we can already claim

### G1 — Policy Engine ✅ (with honest caveats)

**Have.** A single centralized gate, `app/policy/gate.py:Gate.evaluate` →
`app/policy/context.py:build_context`, driven by `config/policy.yaml`
(`sensitivity_rules`, `budgets`, `residency`, `tenants`). It **runs first** (non-negotiable
rule #1), **fails closed** (rule #5), classifies by detected entities
(`app/policy/detectors.py`), escalates residency (`_stricter`), applies risk floors and
`require_verification`, and merges per-app overrides (`app/policy/merge.py`,
`effective.py`). It is applied *identically* to the direct path and every agent hop. This
is a stronger "enforce rules consistently across all agents" story than most competitors,
because in RouteForge it is **architecturally unavoidable**, not a bolt-on.

**Caveat to be honest about in sales:** it is a **fixed YAML policy schema** (entity →
sensitivity → residency + budgets), **not** a general policy engine — no OPA/Rego, no
NeMo Guardrails, no prompt-injection / jailbreak rules. See G1′ in §2 for the upgrade.

### G2 — Permission Control / Scoped Permissions 🟡

**Have.** Least-privilege scoping *per application*: `AppPolicy` (`app/apps/models.py`)
carries `allowed_tiers`, `allowed_providers`, `allowed_models`, `residency`,
`max_spend_usd_per_day`, `max_rpm`, plus `pii_detection` / `capture_io` / `stream_mode`.
It is **narrow-only** — per-app policy can tighten but never widen the tenant baseline
(`app/policy/context.py:build_context`, `app/routing/router.py:select`). That is a real
"nothing more, nothing less" story at the *app* granularity.

**Gap.** Scoping stops at the app. There is **no per-task, per-request, or per-tool**
scoping. In the agent runtime, tools are forwarded untouched
(`app/agent/internal_endpoint.py` forwards `tools=tools`) — no tool allowlist, no
sandboxing, no capability scoping — and of the `agent_budgets` in `config/policy.yaml`
only `max_steps` is enforced (`max_spend_usd`, `per_call_timeout_s` are declared but
dormant). The checklist's phrase is *"scoped to the task at hand, not the role on paper"* —
today RouteForge scopes to the role (app), not the task. See G2′.

### G3 — Audit Logs / Immutable Audit Trail 🟡

**Have.** *Every* decision is recorded: `DecisionRecord` (`app/models.py`) per routing
decision and per agent hop, `UsageRecord` DB metering
(`app/apps/store.py:record_usage`), `RequestSnapshot` for the console, and redacted
Langfuse export (`app/obs/export.py:export_trace`). "Record every action" — genuinely yes.

**Gap.** *"Tamper-proof, always available"* — **not yet.** These are plain mutable
SQLAlchemy rows; there is no append-only enforcement, no hash-chaining, no signing, and
housekeeping actively `DELETE`s (`store.py:prune_snapshots`). Revocation is softer —
`store.py:revoke_token` only flips `token.status = "revoked"` and stamps `revoked_at`,
it does not delete the row — but neither path is append-only or tamper-evident, which is
the actual gap. Langfuse export is best-effort and redacted (no content integrity
guarantee). For regulated buyers this is the single highest-value gap — see G3′.

### G4 — Identity Management / Agent Identity 🟡

**Have.** Applications (tenants) authenticate with opaque, hashed bearer tokens
(`app/apps/tokens.py`, sha256, `rf_live_*`) resolved fail-closed
(`app/apps/store.py:resolve_application` rejects unknown / revoked / non-`active`). "Know
who is acting" — at the **app** level, yes, and fail-closed.

**Gap.** *"Identity is issued to the agent itself, not borrowed from a human [app]
credential."* Today it **is** borrowed: in a multi-step run every hop executes as the
originating app, and the internal endpoint authenticates with a hardcoded
`SecretStr("sk-internal")` gated only by a loopback check + `run_id`
(`app/agent/runtime.py`, `app/agent/internal_endpoint.py:_is_loopback`). No per-agent /
per-run credential, no agent registry. See G4′.

### G5 — Kill Switch & Rollback 🟡

**Have.** Plenty of *fail-closed* off switches: provider on/off
(`serving.yaml providers[].enabled` → `app/apps/registry.py:effective_pool`), per-model
admin disable (`registry.is_enabled` → reject in `router.py`), per-app kill (set
`Application.status != "active"` or revoke token), budget/RPM hard-stops (429 in
`app/api/routes.py`), and pervasive rejection (`PipelineRejected`, `AgentCapExceeded`).

**Gap.** Every switch takes effect **on the next request** — there is **no mid-flight
abort** of an in-progress request or agent run, and **no rollback / checkpoint / resume**:
`runtime.py:run` drains hop state on failure and re-raises with no compensating action.
"Stop unsafe actions *instantly*" and "clean rollback" are both unbuilt. See G5′ (and a
scope caveat: RouteForge governs *model calls*, not arbitrary world-actions — "rollback"
must be defined carefully).

### G6 — Behavior / Drift Monitoring · Shadow Agent Sweeps 🟡→❌

**Have.** Strong *observability*: Prometheus exporter (`app/obs/metrics.py`,
`routeforge_*` families, `GET /metrics`), per-app usage stats, and a live redaction-safe
`DecisionRecord` SSE stream (`app/api/bus.py`, `GET /api/stream`). Every decision carries
`sp.candidates` and `sp.routing_policy_version`.

**Gap.** It is **pure observability with zero anomaly logic** — no baseline modeling, no
behavioral-drift detection, no alerting layer, no shadow/unauthorized-agent sweep.
`stage_signals` is a routing heuristic, not drift detection. Unauthorized use is *prevented*
at auth time but never *detected or alerted*. This is the "Behavior Monitoring Radar" that
the checklist draws as the headline beam, and RouteForge has the raw signal (the decision
stream) but none of the radar. See G6′.

---

## 2. Buildable features — ranked by (marketability × fit ÷ effort)

Legend: **Fit** = how cleanly it lands on existing seams without breaking rules #1–#8 or the
pure-router boundary. All of these live in the gateway/policy/observability plane — **none
requires RouteForge to host compute** (rule #4 intact).

| Rank | Feature | Domain | Marketability | Fit | Effort |
|---|---|---|---|---|---|
| **1** | **Tamper-evident audit trail** | G3′ | **Very high** (regulated buyers) | High | Med |
| **2** | **Scoped per-tool / per-task permissions** | G2′ | High | High | Med |
| **3** | **Live kill switch (mid-flight cancel)** | G5′ | High (great demo) | High | Low–Med |
| **4** | **Per-run agent identity** | G4′ | Med–High | High | Med |
| **5** | **Drift / anomaly radar (advisory)** | G6′ | **Very high** | Med | Med–High |
| **6** | **Pluggable policy engine (OPA/guardrails)** | G1′ | Med | Med | High |

### G3′ — Tamper-evident audit trail *(rank 1 — best ROI)*

**What.** Turn the existing `DecisionRecord` / `UsageRecord` stream into an **append-only,
hash-chained ledger**: each record stores `prev_hash` + `content_hash` (over the canonical
decision fields), optionally signed with a deployment key; add a `verify` endpoint/CLI that
walks the chain and reports the first break; write the chain to WORM-friendly storage and
stop `DELETE`-ing audited rows (retire/segregate instead of prune).

**Why it sells.** "Immutable Audit Trail" is the checklist column regulated buyers care
about most, and RouteForge already emits the perfect atomic unit (the `DecisionRecord` —
"the *why* is the whole point"). This upgrades *what we already have* rather than adding a
new subsystem. Air-gap-friendly (local keys, no phone-home).

**Rule-fit.** Redaction still applies (rule #2) — hash the **redacted** record; never chain
raw PII. CCR contents stay out (rule #6).

### G2′ — Scoped per-tool / per-task permissions *(rank 2)*

**What.** Extend `AppPolicy` with an **allowed-tools list** (and optional per-tool budget /
rate), enforce it where the agent forwards tools (`internal_endpoint.py`), and **actually
enforce the dormant `agent_budgets`** (`max_spend_usd`, `per_call_timeout_s`), not just
`max_steps`. Optionally allow a per-run *narrowing* of tools below the app grant.

**Why it sells.** Directly answers "Scoped Permissions — scoped to the task, not the role."
It closes a concrete, honest gap (tools currently pass through untouched) and it's the
same per-app config seam that already carries `pii_detection` / `stream_mode`.

**Rule-fit.** Narrow-only, like all per-app policy. Fail-closed on an un-allowlisted tool.

### G5′ — Live kill switch *(rank 3 — cheapest strong demo)*

**What.** Make the existing off-switches take effect **mid-flight**: re-check app
`status` / token-revocation / a new per-run "abort" flag at each agent hop
(`runtime.py` already loops hop-by-hop) and cooperatively cancel — reject the in-progress
run with a recorded `killed` decision. Add an admin `POST …/runs/{run_id}/kill`.

**Why it sells.** "Stop unsafe actions *instantly*, built-in, always tested" — a
one-button live demo (kill a running agent from the console, watch it halt on the next hop)
is exactly the kind of thing that lands in an evaluation session.

**Scope caveat on "rollback."** RouteForge governs *model calls*, not side effects in the
world, so honest "rollback" = **conversation/agent-state checkpoint + resume** in the NAT
runtime (halt cleanly, discard the poisoned hop, optionally resume from the last good
checkpoint) — **not** undoing external actions a tool already took. Market the halt +
clean-state guarantee; do **not** overclaim "rollback of agent actions."

### G4′ — Per-run agent identity *(rank 4 — pairs with G2′)*

**What.** Replace the hardcoded `sk-internal` loopback credential with a **run-scoped,
short-lived, scoped token** minted per agent run, carrying the run's tool/tier scope
(feeds G2′) and making every hop **individually attributable** in the audit trail (feeds
G3′). Optional lightweight agent/run registry.

**Why it sells.** "Identity issued to the agent itself, not borrowed from a human
credential." It's the missing piece that makes per-agent audit and per-task scoping
*mean* something. Best built alongside G2′/G3′.

### G6′ — Drift / anomaly radar *(rank 5 — highest marketing ceiling)*

**What.** A **local, advisory** analyzer over the existing `DecisionRecord` SSE stream that
maintains per-app baselines and raises alerts on drift: cost/spend spikes, tier-mix drift
(e.g. an app suddenly floods the frontier tier), verification-failure-rate climbs, RPM/token
anomalies, residency-clamp frequency. Surface as console alerts + a `routeforge_*` alert
metric.

**Why it sells.** This is the checklist's headline "Behavior Monitoring Radar / Drift
Monitoring." RouteForge uniquely already has the clean signal (every decision, structured,
redacted) — competitors have to scrape logs to get what we stream natively.

**Rule-fit — the important one.** Keep it **advisory, not authority** — the same discipline
RouteForge already applies to `stage_signals` and the step-4 LLM classifier ("advisory
signal, not decision-maker"; see `docs/comparisons/external-router-feature-analysis.md`
§1). It **alerts**, it does not autonomously block or re-route. It must run **locally**
(air-gap; no phone-home to a cloud anomaly service). If any ML detector is used, it's an
**onboarded on-prem model** producing an advisory score — RouteForge hosts no detector
compute (rule #4).

### G1′ — Pluggable policy engine *(rank 6 — biggest lift, do last)*

**What.** Generalize the fixed YAML policy into a **pluggable engine**: an OPA/Rego (or
CEL) adapter behind the existing `Gate` interface, and optional NeMo-Guardrails
prompt-injection / jailbreak rails. Ship the current YAML schema as the default engine so
nothing regresses.

**Why later.** Highest effort and highest risk — the gate is load-bearing (rules #1, #5).
Any new engine must **still run first, still fail closed, still respect `allowed_providers`
so it can't break `onprem_only`**, and stay air-gap-local. Real value for buyers with
existing OPA estates, but sequence it after the four cheaper wins.

---

## 3. Explicitly out of scope / reframe honestly

- **"Shadow Agent Sweeps" — detecting agents that *bypass* RouteForge.** RouteForge is the
  chokepoint; it can only see traffic that reaches it. Detecting rogue agents talking to
  model endpoints *around* the gateway is a **network-perimeter concern**, outside the
  product boundary. **Reframe, don't build:** RouteForge's answer to shadow agents is
  *prevention by construction* — if every model endpoint is onboarded through RouteForge and
  the network only permits egress via the gateway, there is no shadow path. Within its own
  traffic, G6′ (anomaly radar) catches *misbehaving known* agents. Sell the
  chokepoint-by-construction story; don't claim network discovery we don't do.

- **Rollback of external agent actions.** Out of scope — RouteForge governs model calls, not
  the side effects of tools. See the G5′ caveat: build **halt + clean-state checkpoint**,
  market that precisely, never "undo what the agent did in your systems."

- **Hosting anomaly-detection / guardrail compute.** Rule #4 stands. Detectors and rails run
  as advisory logic or as **onboarded on-prem models**, never as inference compute RouteForge
  stands up.

---

## 4. Bottom line for positioning

RouteForge can *today* credibly claim **Policy Engine** (unavoidable, fail-closed, runs
first) and a strong **per-app Permission Control** and **decision audit** story — that
already covers half the checklist and, on enforcement guarantees, exceeds bolt-on
governance layers because ours is architecturally in the request path.

The four partials (G3–G6) are a coherent, boundary-respecting roadmap that converts
RouteForge's marketing from *"model router with good observability"* to **"AI governance
gateway / AI Control Tower for the serving plane."** Recommended build order:
**G3′ (immutable audit) → G2′+G4′ (scoped per-tool identity, together) → G5′ (live kill
switch) → G6′ (anomaly radar) → G1′ (pluggable policy) last.**

---

*Related: `docs/comparisons/external-router-feature-analysis.md` (advisory-signal-not-authority
pattern, reused by G6′), `docs/architecture/decisions/self-improving-agent-feature-fit.md` (the
orchestrated-not-hosted boundary that G6′ detectors must respect), `docs/reference/per-app-config.md`
(the per-app config seam G2′/G4′ extend), `docs/reference/routing-observability.md` (the `DecisionRecord`
stream G3′/G6′ build on).*
