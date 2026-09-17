# Exporting eval / RLHF datasets from routeForge traces

This is a guide for the **external consumer** that turns routeForge's Langfuse traces
and feedback scores into a training or evaluation dataset — typically a customer's ML
engineer running Hugging Face TRL, OpenRLHF, or an equivalent offline pipeline.

routeForge does not run this pipeline itself. Per the project's control-plane
boundary (`CLAUDE.md` rule #4; see `docs/architecture/decisions/self-improving-agent-feature-fit.md`
§1), routeForge **produces the substrate** — traces and feedback scores in Langfuse — and
may **trigger** an external training job later, but it hosts no training compute and has
no in-process promotion path. Everything below runs in a **separate project**, against
Langfuse's own API, outside routeForge's process.

## 1. What you read

Two Langfuse surfaces, both scoped to the project(s) your application(s) were exported
to (see `docs/guides/integrating-an-app.md` §5 for how an app gets its own project):

- **Traces** — via the Langfuse UI, its export/blob features, or its API. Each request
  routeForge serves emits one `serving.request` trace, plus one `serving.<step>` child
  span per pipeline step (`serving.router`, `serving.compression`, `serving.verify`, …).
  An agent trajectory is N traces, one per call your loop made — routeForge runs no
  loop of its own.
- **Scores** — feedback attached to a trace, via `GET` on Langfuse's public scores API
  (the same `/api/public/scores` surface routeForge's `POST /v1/feedback` writes to;
  see `app/obs/scores.py`).

You do not need routeForge running to build a dataset — once the traces and scores
exist in Langfuse, export is a Langfuse-side operation.

## 2. Field → dataset column

### Trace attributes

Set in `app/obs/export.py` (`_set_io`, `export_trace`). All of these except
`input.value`/`output.value` are **always on** — emitted regardless of the app's
`capture_io` mode, because they carry no PII.

| Trace field | Meaning | When present |
|---|---|---|
| `input.value` | The request prompt | `capture_io` ≠ `off`. **Raw** for a `full`-eligible app; the gate's redacted/sanitized prompt for `redacted` |
| `output.value` | The model completion | `capture_io` ≠ `off`. For `full`, this is the **pre-verify raw model completion** (`raw_completion or completion`) — what the model actually said, not the step-9 client-facing masked reply. For `redacted`, it's the egress-redacted completion |
| `sp.candidates` | Chosen model + full rejected-candidate set, each with a reason | always |
| `sp.routing_policy_version` | Content hash of the routing + pricing config in effect at decision time | always |
| `sp.chosen`, `sp.reason` | Per-step chosen model / selection reason (on the child spans) | always |
| `sp.request_id`, `sp.application_id`, `sp.model`, `sp.cost_total_usd`, `sp.cost_baseline_usd`, `sp.sensitivity`, `sp.onprem_only`, `sp.detected_entities` | Request/decision metadata | always |
| `langfuse.session.id` | Groups turns from one multi-call trajectory into a Langfuse Session | when the app sent `x-routeforge-session` |

The full attribute reference, including the child-span fields, lives in
`docs/guides/integrating-an-app.md` §7 — read it before writing an export script; this
guide only adds the dataset-column mapping on top.

### Feedback scores

Posted via `POST /v1/feedback` (app-token) or the admin UI; names and dataTypes are
defined in `app/obs/scores.py` (`SCORE_NAMES`, `_SCORE_DATATYPES`):

| Score name | dataType | Meaning | Dataset use |
|---|---|---|---|
| `route_correct` | NUMERIC (1/0) | Was the routing decision right? | Target A filter/label — e.g. `route_correct = 0` selects hard routing negatives |
| `quality` | NUMERIC | Reviewer-assigned response quality | Threshold filter, e.g. keep only `quality >= 4` |
| `preferred_model` | CATEGORICAL | The model a reviewer would have preferred instead | Router-training label |
| `failure_type` | CATEGORICAL | Failure taxonomy label | Filter/stratify column |
| `corrected_output` | posted as value `1` (a NUMERIC marker), **text lives in the score's `comment` field** | The human-corrected completion | `chosen` side of a preference pair (see §3) |

`corrected_output` is the one score whose payload is *not* its `value` — the score
value is always the fixed marker `1`; the actual correction text is the `comment` you
sent as the fourth argument to `post_score(app_langfuse, trace_id, "corrected_output",
1, <text>)`. Read the comment field, not the value, when extracting the correction.

## 3. Capture-mode prerequisite

Which trace fields exist at all depends on the app's `capture_io` setting and which
Langfuse project the trace landed in — see `docs/roadmap/rlhf-readiness.md` §2 and §5 for the
full Target A / Target B split. Summary for dataset export:

| `capture_io` | Project | `input.value` / `output.value` | Usable for |
|---|---|---|---|
| `off` | any | absent | Target A only, from `sp.*` metadata + scores — no I/O to build `{prompt, chosen, rejected}` from |
| `redacted` | any | egress-redacted (masked entities) | Target A (routing signal isn't PII) — **not** Target B; masked text breaks SFT/DPO training |
| `full` | the app's own **residency-local** Langfuse project | raw prompt + raw pre-verify completion | Target A **and** Target B |

**Target B (SFT/DPO on the underlying LLM) requires `full` capture in a
residency-local project.** Per-app `full` is gated at policy-write time
(`app/api/admin_routes.py:set_policy`), not by the boot config lint. Two independent
conditions:

- the app has its **own** provisioned residency-local Langfuse project — never relaxed,
  so raw I/O only ever lands in that app's own project, never the shared one; **and**
- if the app is `onprem_only`, its `policy_mode` is explicitly `passthrough`. An
  `onprem_only` app still in the `enforced` posture cannot reach `full` at all
  (CLAUDE.md rules 2/8). See [`../reference/per-app-config.md`](../reference/per-app-config.md).

(The boot config lint is a separate, deployment-wide check: it rejects a global
`observability.langfuse.capture_io: full` in `serving.yaml` whenever any tenant is
`onprem_only`.)

If you're building a preference dataset and your export came from the shared/default
project, stop: it's redacted-only by construction (see §5).

## 4. The `{prompt, chosen, rejected}` transform

A DPO-style preference pair needs three fields per example. The mapping, straight from
the trace + score fields above:

| TRL field | Source |
|---|---|
| `prompt` | trace `input.value` |
| `rejected` | trace `output.value` (the raw model completion) |
| `chosen` | the trace's `corrected_output` score — its **comment**, not its value |

A trace with no `corrected_output` score has no preference pair. It's still useful as
an unlabeled sample (SFT target if you trust the raw completion) or as a router-only
sample (via `sp.candidates`, `route_correct`, `preferred_model`), but it does not
produce a `{prompt, chosen, rejected}` row.

### Worked example

A trace from a `full`-eligible app, with `input.value` and `output.value` set, plus one
`corrected_output` score attached later by a reviewer:

```
trace.input.value  = "Draft a refund policy email for order #4821."
trace.output.value = "Sorry, we can't help with that."
score.name          = "corrected_output"
score.value          = 1                      # fixed marker, not the payload
score.comment        = "Hi — happy to help. Your refund for order #4821 ..."
```

produces one preference pair:

```json
{
  "prompt":   "Draft a refund policy email for order #4821.",
  "rejected": "Sorry, we can't help with that.",
  "chosen":   "Hi — happy to help. Your refund for order #4821 ..."
}
```

### Illustrative export script

This is a sketch of the transform, not code routeForge ships — it talks to Langfuse's
API directly, from the external consumer's own project:

```python
# Illustrative only. Runs in the external eval/RLHF consumer, against Langfuse's
# own client — routeForge does not ship or run this.
from langfuse import Langfuse

lf = Langfuse(host=LANGFUSE_HOST, public_key=PUBLIC_KEY, secret_key=SECRET_KEY)

pairs = []
for trace in lf.fetch_traces().data:
    prompt = trace.input
    rejected = trace.output
    if not prompt or not rejected:
        continue  # capture_io was off, or this trace predates capture

    correction = next(
        (s for s in lf.fetch_scores(trace_id=trace.id).data
         if s.name == "corrected_output"),
        None,
    )
    if correction is None:
        continue  # no human correction -> no preference pair

    pairs.append({
        "prompt": prompt,
        "rejected": rejected,
        "chosen": correction.comment,   # correction text lives in the comment
    })
```

Add `route_correct`, `quality`, `preferred_model`, and `failure_type` scores as extra
filter columns the same way — fetch each trace's scores and join on `trace_id`.

## 5. Session grouping for multi-call trajectories

If the app that produced these traces sent `x-routeforge-session: <run-id>` on every
call of an agentic run, routeForge stamps it as `langfuse.session.id` and Langfuse
groups the N per-turn traces into one Session. Walk the Session to reconstruct a
trajectory instead of treating each turn as an independent example. Full details
(header rules, what gets grouped, and why) are in
`docs/guides/integrating-an-app.md` §7 — this guide doesn't restate them.

## 6. Privacy note

Raw prompts and raw completions (`input.value` / `output.value` with real text, not
masked entities) exist **only** in a `full`-eligible app's **own residency-local**
Langfuse project. Every `redacted`-mode trace, and every trace in the shared/default
project, carries masked I/O only — entity values replaced with `<TYPE>` markers. A
dataset built from a shared-project export is redacted-only by construction: it is safe
for Target A (router) training but cannot be used for Target B (LLM fine-tuning), which
needs the real text. If you need a `{prompt, chosen, rejected}` dataset with real text,
confirm with the routeForge admin that the source app is provisioned with `capture_io:
full` and its own residency-local Langfuse project (`docs/roadmap/rlhf-readiness.md` §5)
before you export.
