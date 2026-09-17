# RLHF readiness — the feedback & dataset substrate (future feature)

> **Status check 2026-09-10.** The title and the "Status" line right below are stale —
> re-verified against HEAD, the F1–F5 substrate this doc designs has shipped (0.11.0 +
> F1 Phase D): `sp.candidates` and `sp.routing_policy_version` in `app/obs/export.py`,
> `POST /v1/feedback` + `SCORE_NAMES`/`post_score` in `app/obs/scores.py`
> (`app/api/routes.py`), and the offline `tools/recommend/`, `tools/prompt_opt/`,
> `tools/orchestrate/` tools plus `docs/guides/exporting-eval-datasets.md` all exist and
> match the claims below. Only the *training itself* stays external and out of scope —
> that boundary is unchanged. Current authority: the "Status update" section
> immediately below (§0) and `docs/guides/exporting-eval-datasets.md`.

**Status:** ~~design-only, build later~~ — **substrate shipped** (0.11.0 + F1 Phase D,
see the status check above); external training/promotion remains out of scope.
Logged from the owner's research note (a scratch file, never checked into this repo) on 2026-08-05.

> **Update 2026-08-19:** the substrate this doc designs (F1–F5 in §6, and the three
> gaps in §4) has since shipped, across **0.11.0** and **F1 Phase D**. See the status
> update after §1 for what changed and where. Training itself is still external and
> out of scope — nothing below changes the boundary.
>
> **Update 2026-08-23 (features #4 & #5 of the seven-feature program):** the two
> *improvement* halves this doc anticipated now ship as offline, operator-gated tools.
> **Feature #5 — trace-mining recommend-loop + external-training orchestration** realizes
> §2 **Target A** and §8 steps 7–8: `tools/recommend/` mines the local metering/trace
> substrate into a `RecommendationReport` (`report.py`) for an operator to **hand-apply**
> — it recommends, it does not retrain or auto-apply; `tools/orchestrate/` **triggers** an
> external distill/fine-tune job, tracks its status, and surfaces the result for
> onboarding, but **NEVER auto-onboards** (probe-gated, operator-initiated — CLAUDE.md
> rule 4). **Feature #4 — DSPy prompt-optimization** (`tools/prompt_opt/`) is the
> offline prompt-improvement counterpart. All three are default-OFF and honor the scope
> boundary below — RouteForge still only *captures and orchestrates*, never trains or
> promotes in-process.

**Scope boundary (read first — CLAUDE.md rule):** the **Improvement Plane** —
reward-model training, DPO/PPO/GRPO, LLM fine-tuning, policy retraining, evaluation
gates, promotion, registry — is **out of scope** for routeForge and stays external
(Hugging Face TRL / OpenRLHF), exactly as `rlhf.txt` itself concludes. What *is* in
scope, and what this feature builds, is the **data/feedback substrate** that makes
external RLHF possible: capturing user + reviewer feedback as Langfuse scores linked
to traces, emitting RLHF-ready decision metadata, and making traces exportable as
training/eval datasets. This is consistent with `docs/archive/eval-logging-backlog.md`:
*capturing the data is fine and in scope; building training pipelines on top is the
out-of-scope part.* routeForge **captures and exports**; something outside **trains
and promotes**.

> The mermaid pipeline diagram referenced in `rlhf.txt` (a local
> `Downloads\mermaid-diagram` path) was not included with the note and isn't in the
> repo. The "pipeline progression" section below reconstructs it in text.

---

## Status update — what's shipped since this was written (2026-08-19)

The three gaps in §4 and the required work in §6 are now closed. The router-training
and dataset-export substrate this doc called for is real, not just designed:

- **F2 — eligible/rejected candidate snapshot**: shipped in **0.11.0** as the
  always-on `sp.candidates` trace attribute (chosen model + full rejected set, each
  with a reason). Emitted regardless of `capture_io`, as §5 required.
- **F3 — routing-policy version**: shipped in **0.11.0** as the always-on
  `sp.routing_policy_version` attribute (a routing+pricing content hash), so Langfuse
  experiments can compare routing-policy versions and attribute regressions.
- **F1 — feedback API**: shipped in **F1 Phase D** as `POST /v1/feedback`
  (app-token-authenticated, writes a trace-linked Langfuse score to the calling app's
  OWN project, fails closed with no post when the app has no provisioned project) plus
  an admin UI thumbs/correction form. Every chat response now returns its trace id so
  a caller can attach feedback later.
- **F5 — routing-specific score configs**: shipped alongside F1 — `route_correct`
  (numeric), `quality` (numeric), `preferred_model` (categorical), `failure_type`
  (categorical), and `corrected_output` are standardized score names with lazily
  created Langfuse score configs (`app/obs/scores.py`).
- **Raw-completion gap** (not one of the original three, found while building F1
  Phase D): before this, `output.value` on a `full`-eligible app's trace was the
  step-9 client-facing *masked* reply, not what the model actually said — unusable as
  a DPO `rejected` value. Now `output.value` carries the **pre-verify raw model
  completion** on the `full` path (`app/obs/export.py` `_set_io`). This was the
  blocking gap for Target B.
- **F4 — dataset-export readiness**: the trace/score → dataset-column mapping and the
  `{prompt, chosen, rejected}` transform (with a worked example) are now written up in
  [`docs/guides/exporting-eval-datasets.md`](../guides/exporting-eval-datasets.md).
- **Session grouping**: multi-call agentic trajectories can be tied together via the
  `x-routeforge-session` header → `langfuse.session.id`, documented in
  `docs/guides/integrating-an-app.md` §7.

**What has not changed:** the scope boundary in the intro above. RouteForge produces
substrate and orchestrates; it does not train, evaluate-gate, or promote. Target B
(LLM fine-tuning) still runs entirely outside routeForge, in TRL/OpenRLHF, against
data exported per `exporting-eval-datasets.md`.

## 1. What Langfuse gives us (and what it doesn't)

Langfuse is the **feedback-data and evaluation layer** for RLHF, not the training
engine. Out of the box it provides: prompt/response capture (traces), **end-user
feedback stored as scores linked to traces**, a human-evaluation UI, **annotation
queues**, **corrected outputs**, score configs, LLM-as-a-judge, code-based eval,
**dataset creation from production traces**, prompt/model experiments, score
analytics, and data export (CSV/JSON/API/blob).

It does **not** do: reward-model training, DPO/PPO/GRPO, or fine-tuned-model
deployment. Those need TRL / OpenRLHF and stay outside routeForge.

## 2. Two improvement targets — keep them separate

**Target A — improve the routeForge router (highest value, start here).**
Human feedback like *"asked to fix a Java auth bug → router picked the general
model → reviewer says the coding model was correct"* trains/repairs:
task classification, model selection, cost-vs-quality, app policies, primary/fallback
ordering, latency/SLA selection, on-prem vs cloud. **This does not need LLM RLHF** —
a small classifier, reranker, contextual-bandit policy, or even better deterministic
rules suffices, trained *offline* from exported feedback.

**Target B — improve an underlying LLM (later, opt-in).** Fine-tune e.g. a coder
model from production interactions. Langfuse builds the records; TRL trains:
- **SFT:** `{prompt, completion}` where completion = the reviewer's **corrected output**.
- **DPO:** `{prompt, chosen, rejected}` where rejected = original low-quality
  response, chosen = reviewer-preferred/corrected. Needs two answers + an explicit
  preference per request.

Target A works on **redacted** data (routing signal isn't PII). **Target B needs the
real prompt + real completions** — so it requires `capture_io: full` in a
**residency-local** project (see §5 matrix). This is the single most important
capture-mode consequence.

## 3. Fields to capture per request

The RLHF note's recommended fields, mapped to routeForge:

| Field | routeForge source | Captured today? |
|---|---|---|
| Request ID | `sp.request_id` | ✅ |
| Application | `sp.application_id` | ✅ |
| Task classification | none — step 4 is a constant `direct` (the multi-step branch and the classifier were removed 2026-09-17) | ❌ |
| **Eligible models (candidate snapshot)** | `RouterDecision.rejected[]` + `chosen`, exported as `sp.candidates` | ✅ **shipped 0.11.0** (was ❌ when this table was written) |
| Selected model | `sp.model` | ✅ |
| **Selection reason** | `RouterDecision.reason` (lifted as `sp.reason` on the router step) | ⚠️ partial (unchanged) |
| **Routing-policy version** | `sp.routing_policy_version` (routing+pricing content hash) | ✅ **shipped 0.11.0** (was ❌ when this table was written) |
| Model response | `output.value`; raw pre-verify completion when `capture_io: full` | ✅ **raw-completion gap closed in F1 Phase D**; still gated by `capture_io != off` |
| Success / user feedback / human score / preferred model / corrected output | `POST /v1/feedback` → Langfuse scores (`route_correct`, `quality`, `preferred_model`, `failure_type`, `corrected_output`) | ✅ **shipped F1 Phase D** (was ❌ when this table was written) |
| Latency / TTFT | per-step `latency_ms` | ⚠️ partial (TTFT not captured) |
| Tokens & cost | `sp.cost_*`, usage | ✅ |
| Fallback occurred | gateway step | ✅ (in trace) |

**The make-or-break fields for Target A** (per `rlhf.txt`): *which models were
eligible, which was selected, why, what result, whether the user accepted, which model
should have been chosen instead.* Without the **eligible-model snapshot + selection
reason**, you can evaluate an answer but cannot train or diagnose the router. Two of
those (eligible snapshot, policy version) are exactly the gaps below.

## 4. The three gaps in routeForge today (grounded in code)

**Status: all three closed — see the status update above.** Left as originally
written below (this was the historical case for the work); each item now carries a
status line.

1. **Eligible/rejected snapshot never reaches Langfuse.** `app/routing/router.py`
   builds a rich `RouterDecision` (`chosen`, `reason`, `rejected: list[RejectedCandidate]`
   with reasons like residency / not-permitted / over-budget / below-tier /
   disabled). But `app/obs/export.py` lifts only `sp.chosen` + `sp.reason` per step —
   the full candidate pool + per-candidate reasons are dropped. Training-ready router
   data must include the whole snapshot.
   **✅ Shipped 0.11.0** — always-on `sp.candidates` JSON attribute: chosen model +
   full rejected set, each with a reason.
2. **No routing-policy version.** Nothing in `app/routing`, `app/policy`, or
   `app/config/schema.py` stamps a router/policy version. Without it you cannot run
   Langfuse **experiments** comparing policy versions, or attribute a regression to a
   change. Add a `routing_policy_version` (hash or semver of the routing.yaml +
   scoring config) emitted on every trace.
   **✅ Shipped 0.11.0** — always-on `sp.routing_policy_version` attribute, a content
   hash of the routing + pricing config in effect at decision time.
3. **No feedback ingestion path.** There is no endpoint to accept user/reviewer
   feedback and write it to Langfuse as a **score linked to the original trace**.
   Feedback arrives *asynchronously* (after the user reacts), so routeForge must (a)
   return the trace/request id to the caller, and (b) accept feedback against it later
   and post it to the **trace's own project** (using that app's `app_langfuse` creds).
   **✅ Shipped F1 Phase D** — `POST /v1/feedback` (app-token), fails closed with no
   post when the app has no provisioned project; every chat response now returns its
   trace id so a caller can attach feedback later.
4. **Raw completion gap (found while building the above, not one of the original
   three).** `output.value` on a `full`-capture trace was the step-9 client-facing
   *masked* completion, not what the model actually said — a DPO `rejected` value
   needs the real output, not the sanitized one.
   **✅ Shipped F1 Phase D** — `output.value` now carries the pre-verify raw
   completion on the `full` path (`app/obs/export.py` `_set_io`).

## 5. Capture-mode × RLHF-target matrix (why B13/B14 matter here)

| `capture_io` | Target A (router) | Target B (LLM fine-tune) |
|---|---|---|
| `off` | ⚠️ router **metadata** still usable (see note) | ❌ no I/O to train on |
| `redacted` | ✅ ideal — routing signal intact, no raw PII | ❌ masked PII breaks SFT/DPO text |
| `full` (residency-local) | ✅ | ✅ real prompt/completions for SFT/DPO |

**Note that changes B14:** the **router-diagnostic metadata** (eligible snapshot,
selection reason, policy version, feedback scores) is **not raw PII** and must be
emitted **regardless of `capture_io`** — even an `off` app should yield router-training
signal from metadata + feedback. So `capture_io` gates only the raw prompt/completion
**I/O**, never the routing metadata. (Today `capture_io` already gates only
`input.value`/`output.value`, and `sp.*` is always emitted — so this is "extend the
always-on `sp.*` set with the eligible snapshot + policy version," not a redesign.)

## 6. Required work (the in-scope substrate)

**Status: F1–F5 all shipped** — across 0.11.0 (F2, F3) and F1 Phase D (F1, F4, F5).
Left as originally scoped below for the record.

- **F1 — Feedback API → Langfuse score.** `POST /v1/feedback` (and a UI thumbs-up/down
  + reviewer form) taking `{request_id | trace_id, score(s), preferred_model?,
  corrected_output?, failure_type?}`; writes a Langfuse **score** (and/or corrected
  output) linked to the trace, in that app's project. Return the trace/request id on
  every chat response so callers can attach feedback later.
  **✅ Shipped F1 Phase D.**
- **F2 — Export the eligible/rejected snapshot + selection reason** as structured,
  always-on trace attributes (fixes gap 1). One JSON attribute (e.g.
  `sp.candidates`: `[{model, tier, eligible, reason}]`) is enough and is
  training-ready.
  **✅ Shipped 0.11.0** as `sp.candidates`.
- **F3 — Routing-policy version stamp** on every trace (fixes gap 2), so experiments
  can compare versions and regressions are attributable.
  **✅ Shipped 0.11.0** as `sp.routing_policy_version`.
- **F4 — Dataset-export readiness.** Document/enable turning high-quality traces +
  corrected outputs into Langfuse **datasets**, and the export → TRL prompt/chosen/
  rejected transform (the transform lives *outside* routeForge).
  **✅ Shipped F1 Phase D** — see
  [`docs/guides/exporting-eval-datasets.md`](../guides/exporting-eval-datasets.md) for the
  field mapping and worked transform.
- **F5 — Routing-specific score configs**: `route_correct` (bool), `preferred_model`
  (categorical), `failure_type` (categorical), plus overall thumbs — standardized so
  analytics/agreement work.
  **✅ Shipped F1 Phase D** as the `SCORE_NAMES` set in `app/obs/scores.py`
  (`route_correct`, `quality`, `preferred_model`, `failure_type`, `corrected_output`),
  with lazily-created Langfuse score configs so each renders with the right widget.

**Out of scope (external):** reward model, DPO/PPO/GRPO, the classifier/policy
*retraining* itself, offline-eval gates, and promotion. routeForge only ever updates
its classifier/policy **after** an external offline evaluation approves it — never
auto-promote (CLAUDE.md rule #4).

## 7. Do B13/B14/B15 support this? (reconciliation)

Mostly yes — they're the **capture substrate** — with three amendments:

- **B13 (honest redaction):** ✅ supports Target A (router signal isn't PII) and is
  *required* so `redacted` datasets are trustworthy. Consequence to record: `redacted`
  is fine for router RLHF but **cannot** feed LLM fine-tuning (Target B) — that needs
  `full`. No change to B13 itself; note the target split.
- **B14 (per-app `capture_io`):** ✅ and it's the enabler for Target B (an app opts
  into `full`, residency-local, to build fine-tuning data) while others stay redacted.
  **Change required:** the router-diagnostic metadata (F2 eligible snapshot + F3 policy
  version) must be **always-on**, independent of `capture_io` — add them to the
  always-emitted `sp.*` set, not gated by the tri-state.
  **✅ Done** — `sp.candidates` and `sp.routing_policy_version` are set unconditionally
  in `export_trace` (0.11.0), before the `capture_io` branch that gates `input.value`/
  `output.value`.
- **B15 (per-app projects + admin rollup):** ✅ per-app project = per-app training
  dataset; admin rollup (redacted metadata) = cross-app router-eval signal.
  **Change required:** feedback scores (F1) must be written to the **same project as
  the trace** they annotate (Langfuse scores are trace-linked within a project), using
  that app's `app_langfuse` creds. The redacted rollup is enough for router eval; LLM
  fine-tuning reads the app's own `full` project.
  **✅ Done** — `post_score` (`app/obs/scores.py`) posts straight to `app_langfuse`'s
  project via REST and never falls back to the shared project.

Net: no rework of B13–B15, but **B14 gains "router metadata is always-on"** and **B15
gains "feedback lands in the trace's project."** Those two amendments are what make the
substrate RLHF-ready. The feedback API (F1), snapshot/version export (F2/F3), and
dataset-export (F4) are net-new and tracked as B16.

## 8. Pipeline progression (reconstructed from `rlhf.txt`)

1. Add thumbs-up/down feedback (F1).
2. Add routing-specific scores: `route_correct`, `preferred_model`, `failure_type` (F5).
3. Annotation queues for low-rated / fallback / high-cost requests.
4. Reviewers add corrected outputs.
5. Convert high-quality traces into Langfuse **datasets** (F4).
6. Compare routing-policy versions via **experiments** (needs F3).
7. Export approved data for external training.
8. Update the routeForge classifier/policy **only after offline evaluation** (never
   auto).

Start with **supervised router improvement + offline evaluation**, not full PPO RLHF.

## 9. Open questions

- ~~Trace-id strategy for async feedback~~ **✅ resolved in F1 Phase D**: `export_trace`
  returns a `TraceExport(url, trace_id)` — the chat response carries the 32-hex OTel
  trace id directly, so a caller attaches feedback against it later without deriving
  or storing anything extra.
- Where does the router classifier/policy *live* once retrained externally — reload as
  config (in scope) vs a model artifact (Improvement-Plane, out of scope)?
- Retention/TTL for `full` fine-tuning datasets vs `redacted` router-eval data (likely
  different TTLs, per residency).
