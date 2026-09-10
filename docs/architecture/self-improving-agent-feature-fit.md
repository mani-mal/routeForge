# Self-improving-agent research → RouteForge feature-fit assessment

**Status:** decision doc, 2026-08-17. Evaluates the owner's research bundle (agent memory /
mem0, prompt optimization / DSPy, harness self-improvement, RLHF weight-training libs) against
RouteForge's product boundaries, and records the **owner's scope decisions** (2026-08-17) that
move those boundaries. Written **before** the Langfuse capture changes (B13→B16) so the capture
topology serves the full roadmap. Sibling reads: `docs/rlhf-readiness.md`,
`docs/eval-logging-backlog.md`, `docs/architecture/inference-layer-boundary.md`,
`docs/architecture/nemo-switchyard-comparison.md`.

Source material (owner's research, `/tmp/harness-rlhf`):
- *Architecting the Self-Evolving Agent* (RSI, OPSD, Mem0, trace mining)
- *Architectures of Continual Learning* (harness-centric shift, OPSD vs GRPO)
- *Beyond the Chatbox: 6 Truths about Agentic Memory* (Mem0 triple-store, Exo split)
- `references.txt` → OpenRLHF, OpenPipe/ART, microsoft/agent-lightning (train weights);
  exoharness/exo, stanford-iris-lab/meta-harness, greyhaven-ai/autocontext (harness);
  mem0ai/mem0 (memory); stanfordnlp/dspy (prompt opt).

> **DSPy was in the design from day one** — the original `llm orchestration loop.drawio`
> carries a "Prompt / Instructions — DSPy / NAT optimizer — templates and examples" node. This
> assessment treats prompt optimization as a first-class serving-plane lever, not an add-on.

---

## 1. The boundary that decides every verdict — as amended by the owner (2026-08-17)

RouteForge is a **cost-reduction control plane + Serving Plane (steps 1–10)**. The governing
line is **not** "capture vs improve" — it is **control-plane vs compute**:

> **RouteForge orchestrates and consumes; it hosts no heavy compute.**
> It **points at** inference (hosts none) and, by the same logic, **orchestrates** training
> (hosts none). A model — distilled, fine-tuned, or off-the-shelf — only ever enters RouteForge
> the one way anything does: as an **onboarded on-prem endpoint** the operator serves.

This amends CLAUDE.md's older, tighter rules (flagged in §6 for an owner-approved edit):
- **Rule #4** evolves from *"promotes/trains nothing, closes no loop"* → **"Improvement Plane is
  orchestrated, not hosted."** RouteForge captures the substrate, **triggers** an external
  training/distillation job on the operator's infra, and onboards the result. It still runs no
  training compute and still has no in-process promotion — onboarding remains the only entry path.
- The cost-reduction guarantee gains **four explicit levers**, all living in the serving plane:
  compression (shipped), prompt optimization (DSPy), persistent memory (context assembly), and
  routing-to-cheaper/distilled models (core). "Token cost → hardware cost" is the through-line.

What stays **out**, unchanged: RouteForge **hosting** training or inference compute, and the
**runtime self-editing harness** (see §2.B) — a gateway that rewrites its own logic at runtime is
un-auditable and kills the on-prem/air-gapped value.

---

## 2. Theme-by-theme verdicts (post-decision)

### A. RLHF / weight optimization — OpenRLHF, OpenPipe/ART, agent-lightning, OPSD/GRPO/DPO

**Decision: orchestrate external, consume result.** ✅ (owner, 2026-08-17)

| | |
|---|---|
| Capture + export the training substrate | ✅ B16 — candidate snapshot (`sp.candidates`) + `routing_policy_version` **shipped in 0.11.0**; feedback API + corrected-outputs + dataset export remain. |
| **Trigger** an external fine-tune/distill job (TRL / OpenRLHF / ART / agent-lightning) on the operator's training infra | ✅ **new — orchestration hook.** RouteForge submits the job + tracks it; the operator's GPUs run it. |
| Consume the trained model | ✅ via onboarding — result becomes an on-prem endpoint; router sends traffic to it. |
| **Host** the training loop (GPUs, TRL in-process) | ❌ still out — RouteForge runs no training compute (control-plane, not compute). |

OPSD/GRPO/DPO are *algorithms the external trainer runs*; RouteForge neither implements nor
executes them. It provides on-policy production trajectories (its trace substrate), which is
exactly the input OPSD/ART want.

### B. Recursive self-improvement / self-editing harness — exoharness/exo, meta-harness, autocontext

**Decision: offline recommend + A/B, human-gated.** ✅ (owner, 2026-08-17) — runtime self-editing
refused.

| | |
|---|---|
| **Runtime self-editing** (agent rewrites its own routing/policy code mid-run, Guardian rebuild-and-soak) | ❌ **CAN'T / SHOULD NOT** — un-auditable; breaks the config-lint boot guarantee, deterministic routing, fail-closed (#5). Not contested by the owner. |
| **Offline, human-gated policy improvement** (mine traces → *recommend* routing-rule / threshold / prompt changes; version + A/B via `routing_policy_version`) | ✅ **CAN** — the safe projection of "harness engineering." Operator applies the change through config; the lint gates it. |
| **Executive / Harness / Sandbox split** (secrets + state out of the LLM's context) | ✅ borrow as a design principle for the step-4 agent runtime — consistent with rules we already hold. |

### C. Agent memory — mem0ai/mem0 (vector + entity + SQLite triple store)

**Decision: RouteForge-owned module.** ✅ (owner, 2026-08-17)

A first-class memory module, framed as an **extension of step 5 (Conditional Context
Optimization)** — retrieving only relevant memories instead of replaying full history is a
**token/cost lever**, same family as Headroom. Router owns retrieval. (For why Headroom
itself — and by the same argument this memory module — stays in RouteForge rather than the
calling app, see [`compression-in-scope.md`](./compression-in-scope.md).)

**Load-bearing constraint (CCR-class, non-negotiable):** the memory store is another persistent
copy of sensitive user data, so it inherits the CCR rules (#6/#7) verbatim — **inside the
residency boundary, enforced TTL, redacted at rest, never exported to telemetry, populated only
with post-gate sanitized content.** If residency can't be guaranteed for a tenant, memory is
disabled for them, exactly like CCR. Memory retrieval re-enters gate → compression → router like
any other call. Design its residency model in its own spec before build (it is the newest, least
specified surface). ✅ **SHIPPED** — that spec now exists
([`persistent-memory-residency-spec.md`](./persistent-memory-residency-spec.md)) and the feature
landed (`app/memory/`).

### D. Prompt optimization — stanfordnlp/dspy

**Decision: in-scope serving-plane feature** (was in the original diagram). ✅

| | |
|---|---|
| Optimize RouteForge's **own** prompts (step-4 classifier, verify/judge, any templated internal prompt) via DSPy over exported datasets, human-gated | ✅ **CAN** — managed feature; the optimizer runs offline, the operator promotes the winning prompt version (tracked like `routing_policy_version`). |
| Rewrite **customer** prompts arriving inside a request | ❌ the one carve-out — those aren't ours to mutate; they pass through. |

### E. Trace mining / continual-learning loop / Judge Agents / distillation datasets

✅ **CAN — highest near-term value; serves the "Observable" guarantee directly.** Mine Langfuse
traces offline (Judge Agents flag bad routing + failure patterns), **report** to the operator,
**export** datasets for external distillation, and feed the orchestration hook (§2.A). This *is*
the B16 substrate. Auto-*applying* findings stays out (same boundary as §2.B).

---

## 3. The capture topology this roadmap requires (resolves the B13/B14/B15 open questions)

The future features split by **what data they need**, and that fixes capture mode + residency:

| Capability | Data it needs | Capture mode | Residency |
|---|---|---|---|
| Router improvement, trace mining, policy A/B, orchestration signal | routing **metadata** (candidates, reason, policy version, feedback) — **not PII** | works on `redacted`; metadata **always-on** | shareable |
| LLM distillation / fine-tune datasets (feeds the §2.A orchestration hook) | **raw** prompt + completion + corrected outputs | requires `full` | **residency-local** |
| Persistent memory store (§2.C) | post-gate sanitized content | n/a (not telemetry) | **residency-local, TTL** |

**Therefore the topology is decided by the roadmap, not by preference:**
1. **Router metadata is always-on**, independent of `capture_io` — 2 of 3 pieces shipped (0.11.0).
2. **Raw `full` is bound per-app to a residency-local, access-controlled project with a TTL** — never
   the shared project, never an `onprem_only` tenant to a shared store (rules #2/#6/#8). This is why
   **B15 (per-app projects) is a precondition for B14's `full`** — and per-app project isolation is
   **already built and wired** (`app/apps/langfuse_provision.py`, auto-provisioned on app create).
3. **"Langfuse stores everything" is safe only as per-project-with-residency**, never one shared
   bucket. Same data goal the research wants (raw substrate for distillation), correct topology.

This is the **Strict** guardrail from the earlier Q1 — and framed against the roadmap it is not a
limitation but the **enabler**: it is what lets Langfuse become the complete distillation substrate
*without* breaking privacy. Confirmed as the design.

---

## 4. Recommendation (what to build, in order)

Everything below is on the **capture + route + orchestrate** side of the amended boundary; none
requires RouteForge to host training or inference compute.

1. **Fix B13 redaction** — feed the exporter the gate's sanitized prompt + redaction map so
   `redacted` telemetry is never weaker than what the model saw. Prerequisite for trusting
   `redacted` as the shareable Target-A substrate.
2. **B14 per-app `capture_io` (off | redacted | full)** with write-time clamps and symmetric UI
   display. Metadata stays always-on regardless. ✅ **SHIPPED.** The clamps landed as an
   own-Langfuse-project requirement on `full` (never relaxed) plus a **posture** condition —
   an `onprem_only` app must declare `policy_mode: "passthrough"` to reach `full` — rather
   than the flat residency lint sketched here; see `docs/per-app-config.md`.
3. **B15 remainder** — admin cross-app reporting from RouteForge's own store + a manual per-app
   Langfuse-creds path (air-gapped operators with no org key). Project isolation core is done.
4. **B16 feedback + dataset substrate** — `POST /v1/feedback` → trace-linked score, correction-
   output form, named score configs, dataset-export readiness. ✅ **SHIPPED** (`POST /v1/feedback`
   in `app/api/routes.py`, `app/obs/scores.py`, `tools/prompt_opt/dataset.py`).
5. **DSPy prompt-optimization** of RouteForge's own prompts (managed, human-gated, versioned).
   ✅ **SHIPPED** (`tools/prompt_opt/`, `app/prompts/store.py`).
6. **Orchestration hook** — trigger an external distill/fine-tune job from exported datasets +
   onboard the result. Closes the loop; hosts no training. ✅ **SHIPPED** (`tools/orchestrate/`).
7. **Harness recursive-learning loop** — mine traces → recommend routing-rule/threshold/prompt
   changes → version + A/B (on `routing_policy_version`) → operator applies via config, lint gates.
   Offline + human-gated; no runtime self-editing. Extends the trace-mining substrate.
   ✅ **SHIPPED** (`tools/recommend/`).
8. **Persistent memory module** (own spec first) — step-5 extension, CCR-class residency guardrails.
   ✅ **SHIPPED** (`app/memory/`; spec: `persistent-memory-residency-spec.md`).

Sequencing rationale: 1→4 are the capture substrate every later feature reads from (do first, in
order — shared data model). 5–7 are independent tracks that consume the substrate; 7 needs its own
residency design before build.

---

## 5. Net

Nothing in the research forces RouteForge to become a compute host. With the boundary re-drawn as
**control-plane vs compute**, every lever the owner wants — compression, prompt-opt, memory,
distillation-and-onboard, feedback-driven routing — lands on the orchestrate-and-consume side
RouteForge already occupies. The single architectural invariant that carries the whole vision:
**RouteForge captures the data and pulls the levers; the operator's infra runs the heavy compute
(inference *and* training); onboarding is the only path a model enters.**

---

## 6. CLAUDE.md amendments this implies (owner approval required before editing)

These decisions contradict the current "non-negotiable rules" as written. Flagging for an explicit,
owner-approved edit — do **not** silently drift:
- **Rule #4** — from "promotes/trains nothing, serves only; if asked to close the loop, stop" →
  "**Improvement Plane is orchestrated, not hosted**: RouteForge triggers external training and
  onboards results; it runs no training compute and no in-process promotion." Keep the
  hosts-no-inference stance intact.
- **Scope banner** ("Serving Plane only. The Improvement Plane … is external and NOT built here")
  — reword to the orchestrated-not-hosted framing.
- **Add memory (mem0)** as an in-scope step-5 extension governed by the CCR residency rules (#6/#7).
- **Add DSPy prompt-optimization** of RouteForge's own prompts as an in-scope managed feature.
- Leave **rules #1, #2, #3, #5, #6, #7, #8 unchanged** — the privacy/residency/fail-closed
  invariants bind harder than ever (memory + raw `full` capture both lean on them).
