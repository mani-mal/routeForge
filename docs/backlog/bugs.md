# Bugs backlog — work later

Running list of known defects/observations to pick up later. Newest first.
**This is now the single backlog.** [`docs/ui-open-items.md`](../archive/ui-open-items.md) and
[`docs/eval-logging-backlog.md`](../archive/eval-logging-backlog.md) have been folded in and archived to
`docs/archive/` — every item in both was verified against the code (not
assumed from its status heading) during the 2026-09-10 docs sweep, and every
one was confirmed closed; nothing was migrated because nothing was live. See
the audit trail in that sweep's task report.

> **Status sweep 2026-08-22.** Re-verified against the code at `main` (merge `ed64312`,
> deployed DGX `:0.14.0`). Resolved-but-unmarked items carry a `✅ RESOLVED` / `PARTIAL`
> marker in their heading with a one-line evidence note; original write-ups kept for history.
> **No open code-correctness bug remains.** B3 (PR #16), B13 (folded into B14/F1), B14
> (F1 Phase B + PR #17) are now resolved. *(One more — B20, a real privacy-routing bug —
> was found and fixed on 2026-08-23, after this sweep; see the 2026-08-23 update below.
> It is resolved, so the statement still holds.)* Remaining: **B2** is an infra/capacity note (scale
> vLLM), not a code fix; **B15** is PARTIAL (admin cross-app rollup + manual per-app creds
> shipped, but automatic project-per-app provisioning still needs `LANGFUSE_ORG_*` config —
> this is why every app currently lands in the shared `demo-project`); **B16** is PARTIAL
> (all 3 substrate gaps shipped; only routing score-configs + dataset-export readiness remain).
> Forward *feature* work is tracked in the audit, not here.
>
> _Prior sweep 2026-08-16: listed B2/B3/B13 + B14–B16 as open._
>
> _Superseded 2026-09-05: **B24** below is an open code-correctness bug, found after this
> sweep. The sweep's "no open code-correctness bug remains" is accurate as of its own date
> only._
>
> **Update 2026-08-23 (DGX `:0.18.0`).** The **seven-feature** program is now MERGED to
> `main` and deployed. Six landed as PRs #18–#23 (commits numbered #1/7…#6/7): role-aware
> raise-only tier bias, failure-driven lateral model switch, semantic cache (CCR-class),
> persistent memory (CCR-class), trace-mining recommend-loop + external-training
> orchestration hook, and DSPy prompt-optimization — all default-OFF. The **seventh** is
> PR #24: a quality/accuracy axis on the cost dashboard + a pluggable routing-strategy
> interface. Plus a config-hardening fix (semantic_cache block made optional so a
> pre-feature-6 mounted serving.yaml boots unchanged). The `:0.18.0` bump came from the
> B20 SSN-dedupe fix (commit `e87e8df`), landed *after* this sweep. One item surfaced
> while integrating the program — B20 below — initially read as a flaky test but was a
> **real MEDIUM privacy-routing bug** (an SSN could downgrade off `onprem_only` and route
> to cloud); now **RESOLVED**.

---

> **Update 2026-09-05 (docs audit).** A sweep of `docs/` against the code turned up one
> **open code-correctness bug**, recorded below as **B24**. It is a straggler from the
> OpenAI tool-calling change (`0546fab`, 2026-08-29), which widened `Message.content` to
> `str | None`: three of the four consumers that read raw message text were updated to
> coerce it, one was not. Same class as B22/B23 in shape — a contract moved and one
> mirror of it didn't — but here both copies are server-side.

---

> **Update 2026-09-10 (docs-reclassification sweep).** Five code/config findings
> turned up while verifying the `docs/` tree against the code for the docs
> reclassification project (not while adding a feature) — recorded below as
> **B26**–**B30**. None are fixed here; this sweep only writes backlog entries.
> The same sweep also folded [`docs/ui-open-items.md`](../archive/ui-open-items.md) and
> [`docs/eval-logging-backlog.md`](../archive/eval-logging-backlog.md) into this file (see the note at the top) —
> every item in both verified closed, nothing migrated.

## B26 — Catch-all exception handler in the request path logs nothing

**Severity:** medium (operational; makes production 500s undiagnosable)
**Found:** 2026-09-10, docs-reclassification Task 3, while debugging a multi-step example
that returned a bare `500 internal error`. **Open.**

`app/api/routes.py:247-249`:

```python
except Exception:
    metrics.observe_request("-", "error")
    raise HTTPException(500, "internal error")
```

This is the last handler in the chat-completions endpoint's exception chain (after the
named `PipelineRejected` / `NoEligibleCandidate` / `AgentCapExceeded` /
`BackendUnavailable` / `PolicyGateError` / `HTTPException` cases). It catches every
other exception, bumps the `error` metric, and raises a generic 500 — but never calls
`logging.*` or `log.exception(...)`, unlike the other `except Exception` blocks in this
same file (e.g. lines 365-366, 387-388, 402-403, 442-443, 461-462, 605-606, 630-631,
all of which log via `logging.getLogger("serving.obs").exception(...)` or `log.exception(...)`).
The traceback for whatever actually broke is discarded the moment this branch is hit;
only the metric and a generic client-facing message survive. Operationally this means a
genuine bug anywhere in `pipeline.run()` is invisible in logs — it shows up only as an
unexplained 500 count on the dashboard, with no way to tell which of the countless
possible causes it was.

## B27 — `capture_io` accepts an invalid value with 200 and stores it silently

**Severity:** low-medium (fail-safe today, but silent, undocumented, and inconsistent
with its three sibling fields)
**Found:** 2026-09-10, docs-reclassification Task 4 review. **Open.**

`app/api/admin_routes.py`'s `set_policy` validates `residency`, `pii_detection`,
`stream_mode`, and `policy_mode` against their enums and returns `422` on an unrecognized
value (lines 420-427):

```python
if body.residency is not None and body.residency not in RESIDENCY:
    raise HTTPException(422, f"bad residency {body.residency!r}")
if body.pii_detection is not None and body.pii_detection not in PII_MODES:
    raise HTTPException(422, f"bad pii_detection {body.pii_detection!r}")
if body.stream_mode is not None and body.stream_mode not in STREAM_MODES:
    raise HTTPException(422, f"bad stream_mode {body.stream_mode!r}")
if body.policy_mode is not None and body.policy_mode not in POLICY_MODES:
    raise HTTPException(422, f"bad policy_mode {body.policy_mode!r}")
```

`capture_io` (`PolicyIn.capture_io`, `app/api/admin_routes.py:118`) has no such check —
there is no `CAPTURE_IO_MODES` import or membership test anywhere in `set_policy`.
**Confirmed by execution** (not just reading): a fresh `TestClient(create_app())` against
an in-memory SQLite DB, logged in as admin, `PUT /admin/applications/{id}/policy` with
`{"capture_io": "bogus_capture"}` returns **`200`**, and the follow-up response body
echoes `"capture_io": "bogus_capture"` back in the stored policy — it is persisted
verbatim, not rejected or normalized.

At serve time, `app/obs/export.py:_set_io` (lines 66-80) only special-cases `"off"`
(early return), `"redacted"` (redact then set), and `"full"` (set raw, gated on the
process-global `_capture_io_full` flag which is only ever armed for a real `"full"`
value). An unrecognized string matches none of the three branches and falls through to
the unconditional `set_attribute` calls at the bottom — but since `_capture_io_full`
stays `False` for anything other than a literal `"full"`, `RedactingSpanExporter`
(`app/obs/otel.py:217`) still redacts the `input.value`/`output.value` keys on export.
So the practical effect today is fail-safe (equivalent to `redacted`, not a raw leak) —
but that safety is incidental to how the fall-through happens to interact with the
exporter, not a deliberate, validated default, and it is silent: the operator gets no
signal that their config value was nonsense. Every sibling field would have told them.

## B28 — Purged `inference_backend` axis survives in the HPE Helm values (values, templates, and a stale wiring claim) <!-- purged-ok -->

**Severity:** low (cosmetic/documentation risk only — no application code reads these keys)
**Found:** 2026-09-10, docs-reclassification Task 5; extended 2026-09-10 (fix round 1 —
review caught two more stale artifacts from the same axis, listed below as 3 and 4).
**Open, deliberately not fixed here.**

Four stale artifacts, all from the `inference_backend` axis the 2026-08-03 pure-router <!-- purged-ok -->
pivot removed (CLAUDE.md: "There is no `inference_backend` axis and no separate <!-- purged-ok -->
`serving_paths` block"): <!-- purged-ok -->

1. `deploy/hpe/routeforge/values.yaml:114` — `inferenceBackend: mock # mock | vllm | dynamo | none`.
2. `deploy/hpe/routeforge/values.yaml:117-118` — `vllm: {baseUrl: http://vllm:8000/v1}`.
3. `deploy/hpe/routeforge/values.yaml:120-123` — the adjacent `dynamo:` block
   (`endpoint`, `routerMode: kv`, `timeoutS: 60`), equally dead, previously uncited here.
4. `deploy/hpe/routeforge/templates/api.yaml:47-49` — worse than dead: the comment
   directly above the container's env vars claims `SP_INFERENCE_BACKEND` (line 50) is <!-- purged-ok -->
   "read from env by `app/config/loader.py`, and env WINS over the mounted
   serving.yaml." Traced `app/config/loader.py` in full: `SP_INFERENCE_BACKEND` is not <!-- purged-ok -->
   read there, or anywhere in the codebase. The chart doesn't just carry a dead key —
   it carries an operator-facing comment actively asserting the dead key is wired up.
   (The "env wins" claim is true of `SP_PII_DETECTION`, the sibling var in the same
   block — just not of `SP_INFERENCE_BACKEND`.) <!-- purged-ok -->

**Confirmed no application code reads any of them** — this claim is scoped to actual
reads, not mentions: `\grep -rl "inference_backend\|INFERENCE_BACKEND" app/` returns <!-- purged-ok -->
exactly two files, and both are comments/docstrings *documenting the removal*, not
reads — `app/policy/context.py:62` ("... never silently routed to cloud (CLAUDE.md <!-- purged-ok -->
backend rule; replaces the old `inference_backend == "none"` drop)") and <!-- purged-ok -->
`app/gateway/backends/README.md:4` ("There is no `inference_backend` axis..."). <!-- purged-ok -->
`\grep -rl dynamo app/**/*.py` similarly turns up only a docstring in
`litellm_backend.py` naming what it replaced. No file under `app/` treats any of these
keys as live config — the Pydantic settings schema has no `inference_backend` field. <!-- purged-ok -->

The Helm templates still render all four into a ConfigMap and container env
(`deploy/hpe/routeforge/templates/serving-config.yaml:17-27`, `templates/api.yaml:50-51`,
`templates/NOTES.txt:6,30-31`), but nothing on the Python side consumes it. Harmless
today, but misleading on two counts: an operator reading `values.yaml` would reasonably
assume `inferenceBackend`/`vllm`/`dynamo` still select something, and one reading
`api.yaml`'s comment would be told outright (incorrectly) that `SP_INFERENCE_BACKEND` is <!-- purged-ok -->
consumed.

**Deliberately not fixed in this task.** Editing a Helm values file (and the templates
that render it) is deployment-affecting and needs its own change with its own testing —
out of scope for a docs-only sweep.

## B29 — UI stats type omits `cache_saved_usd`, which the API already returns

**Severity:** medium (a real savings figure the API produces cannot be rendered)
**Found:** 2026-09-10, docs-reclassification Task 7 review. **Open.**

`ui/src/api.ts`'s `RequestSummary` interface (lines 9-25) declares `routing_saved_usd`
and `compression_saved_usd` (lines 19-20) but has no `cache_saved_usd` field anywhere in
the file — confirmed absent. `GET /api/stats` and `GET /api/fleet`
(`app/api/ui_routes.py:135-146`, via `stats_over`/`fleet_over` in `app/api/history.py`)
both return `cache_saved_usd` (`app/api/history.py:43`, aggregated at lines 142-144 and
171-173 — "feature #6: the third savings lever (skipped model)"). The console has no
type-level knowledge this field exists, so it cannot render it: confirmed
`ui/src/console/sections/CostSection.tsx` has no mention of "cache" at all — the
Savings-by-lever chart shows only routing and compression, silently dropping the third
lever the API already computes and ships.

This is an instance of this project's **documented recurring defect class**: the console
re-encoding a piece of the server contract and drifting from it once the server moves
(see **B22**/**B23** above, and the "Never silent" lesson recorded in this file's
2026-09-05 update note) — here the drift is an omission (a returned field with no
client-side type) rather than a stricter re-derived rule, but the root cause is the same:
a second, hand-maintained copy of a server contract that nothing keeps in sync.

## B30 — Router's `capacity` scoring term is a constant, not a signal

**Severity:** low (no incorrect ranking today, but the scoring function is less rich
than it presents itself, and `routing.yaml`'s weight implies otherwise)
**Found:** 2026-09-10, docs-reclassification Task 8; independently confirmed in review.
**Open.**

`app/routing/strategy.py:60`, `DefaultRoutingStrategy.rank`'s `score()`:

```python
return (weights["quality"] * q - weights["cost"] * norm
        - weights["latency"] * _L[c.tier] + weights["capacity"] * 1.0)
```

`weights["capacity"] * 1.0` is added identically to every candidate's score — a literal
constant, not a per-candidate term, so it can never change the ranking (it shifts every
candidate's score by the same amount, which `sorted()` is invariant to). Confirmed there
is no live signal it could vary on even in principle: `PoolCandidate`
(`app/config/schema.py:6-22`) has fields for `name`, `provider`, `tier`, `display_name`,
`litellm_model`, and `quality` — no `capacity` attribute of any kind. Meanwhile
`config/routing.yaml:15` gives it a real, nonzero weight
(`weights: {quality: 1.0, cost: 0.6, latency: 0.3, capacity: 0.2}`), which makes the
scoring function look like it accounts for a fourth dimension (candidate capacity/load)
that it does not. Either it should be fed a real per-candidate signal or the term
(and its config weight) should be removed so the scoring function's inputs match what it
actually computes.

---

> **Update 2026-09-07 (tool-execution boundary).** Recorded **B25**: RouteForge's internal
> NAT agent can only reach tools compiled into the image, so an application's own tools
> require a source change — unacceptable, since RouteForge cannot be customised per
> customer. **Decision: no serving-path change.** Tool execution stays with the
> application via the client-driven loop; the internal agent path is a routing
> demonstration, not a tool runtime, and docs/diagrams should say so. B25 captures the
> deployment-time design (registry + generic HTTP adapter + per-app allowlist) and the
> residency/egress/tenancy constraints it must answer, for whenever it is picked up.

## B25 — FEATURE: application-registered tools must be deployment-time, not a RouteForge source change

**Severity:** design debt (no defect in the serving path; the shipped behaviour is correct
and safe — this is about a missing operator surface)
**Found:** 2026-09-07, tracing what the request-flow diagram actually claims. **Open, not scheduled.**

**Decision taken now (2026-09-07):** *no change to the serving path.* Tool execution stays
with the **application** — the client-driven loop (`tools` in the request → RouteForge
returns `finish_reason: "tool_calls"` → the app runs the tool and replays) is the one
supported path for real tools, and it is complete. The internal NAT agent path is a
**demonstration of multi-hop routing**, not a tool runtime, and should be described that
way in docs and in any diagram. This item records the gap so the eventual fix isn't
re-derived from scratch.

### What's wrong today

Tools reachable by RouteForge's own NAT agent are fixed at **build time**:

- `nat_tools/` is a proper NAT plugin package (`[project.entry-points.'nat.components']`),
  installed as an editable path dep (`pyproject.toml:44,80`). Adding a tool means writing
  a Python `@register_function` into that package and **rebuilding the image**.
- The tool list is a static YAML literal — `config/nat_workflow.yaml:37`
  `tool_names: [calculator, doc_search, crm_lookup]` — and the path to that file is a
  hardcoded constant with no env override (`app/agent/runtime.py:53`). Nothing mounts it
  (`deploy/compose/docker-compose.yml` declares no volumes), so it is baked into the image
  too: even the *list* can't be changed on a running deployment, unlike the four main YAMLs.
- Two of the three shipped tools are deliberate placeholders — `doc_search` and `crm_lookup`
  return a canned string and make no call (`nat_tools/src/nat_tools/tools.py:90,129`, both
  docstring-labelled FAKE). Only `calculator` computes anything.
- The tool set is **global**, not per-app. There is no `AppPolicy` field scoping which
  application may reach which tool — unlike `allowed_models`, `residency`, `stream_mode`.

So a customer who wants their own tool has to fork RouteForge. **We cannot customise the
source per customer** — that is the whole objection. Compare model onboarding, which got
this right: the pool boots empty, the operator adds an endpoint through an admin API,
probe-gated, no restart (`docs/onboarding-models.md`). Tools should mirror that shape.

### What "right" looks like

Deployment-time registration, no RouteForge code change, per-app scoped:

- An `onboarded_tools` table + registry alongside `onboarded_models`, with an admin API
  (`POST /admin/infra/tools`) and console screen.
- A **generic HTTP-tool adapter** driven by a stored OpenAI function-schema + endpoint +
  auth-ref, so registering a tool is data, not Python. No per-customer plugin package.
- Per-app allowlist (`AppPolicy.allowed_tools`), folded into `app_policy_version` like
  every other posture field.
- The agent's tool list assembled **per request** from (registry ∩ app allowlist) instead
  of read from static YAML — the same move `ModelRegistry.effective_pool()` already makes
  for models.
- Probe-gated onboarding (call the tool endpoint once, validate the schema) so a broken
  tool fails at registration, not mid-trajectory.

### Constraints any design must answer first

Tool execution puts RouteForge on the data path to a customer's internal systems, which
model routing does not. These are not details to settle in implementation:

1. **Residency (rule 3).** A tool endpoint has a *placement*. An `onprem_only` app must not
   have its arguments POSTed to a SaaS tool. The tool registry needs a residency attribute
   and the same filter-before-selection treatment the candidate pool gets — never a
   post-hoc check.
2. **Egress redaction (rule 2).** Tool *arguments* are model output crossing a boundary,
   and tool *results* re-enter the prompt. Both are exactly the leak surface step 9 and the
   exporter exist for. Arguments need the egress path; results need to be treated as
   untrusted input, not as instructions.
3. **Trust boundary.** A tool result is attacker-influenceable text entering the agent
   loop (prompt injection). Whatever the answer is, it has to be stated, not assumed.
4. **Multi-tenancy.** Tenant A's tool must be unreachable on tenant B's request by
   construction, not by prompt.
5. **Budget + observability.** A tool call costs wall-clock and can fail; it needs a
   timeout, a per-request cap folded into `agent_budgets`, and its own `DecisionRecord`
   (tool chosen, why, latency, outcome) or the trajectory has an invisible hop — same
   defect class as B5's `latency_ms: 0`.
6. **Boundary check (rule 4).** Registering a tool endpoint is pointing at something the
   operator runs — the same posture as onboarding a model, so it stays in scope. *Hosting*
   or executing customer code inside RouteForge does not.

### Interim (no code change)

- Keep describing tool execution as the application's job; the app-driven loop is
  documented, tested (`tests/test_openai_compat.py`) and correct.
- Where docs or diagrams show RouteForge calling a tool transparently to the application,
  label it as the internal demo agent with its three built-in tools — not as the
  integration path a customer uses.
- Related: **B17** (grouping a client-driven workflow's requests under one parent row) is
  the observability half of the app-driven loop, and gets *more* valuable given this
  decision — if the app owns the loop, the console has to be able to show it as one story.

---

## B24 — `task_mode.decide()` crashes on a user message with `content: null` → `500 internal error`

**Severity:** low-medium (correctness; wrong status code on a legitimately-shaped request)
**Found:** 2026-09-05, docs-vs-code audit. **Open.**

`app/routing/task_mode.py:19-25` reads the last user message and lowercases it
without coercing `None`:

```python
last_user = ""
for m in reversed(request.messages):
    if m.role == "user":
        last_user = m.content        # ← str | None since 0546fab
        break
lowered = last_user.lower()          # ← AttributeError when content is None
```

`Message.content` became `str | None` in the OpenAI tool-calling fix (`0546fab`) so a
tool round-trip could parse, and its own docstring says consumers reading raw text
"MUST coerce `content or ""`". Three did — `app/policy/gate.py:36`,
`app/api/routes.py:322`, and `count_tokens` (`app/gateway/backends/base.py:16`, which
funnels every token-count call site). `task_mode.decide()` is the one that was missed.

**Reproduced:**

```
ChatRequest(messages=[Message(role="user", content=None)])
→ decide(ctx, req) → AttributeError: 'NoneType' object has no attribute 'lower'
```

**Blast radius.** The gate runs *before* step 4 and survives (it coerces), so this is
not a privacy hole — nothing is routed or egressed. The exception falls through to the
catch-all in `app/api/routes.py` and becomes **`500 internal error`** with
`metrics.observe_request("-", "error")`. That is the actual defect: a malformed-input
case reported as a server fault, which contradicts the honest-status-code work in B1
and would page an operator for a client-side problem.

**Reachability.** Narrow but real — it needs the *last* `role: "user"` message to carry
a null/absent `content`:
- `content` is optional on the model, so `{"role": "user"}` with the key omitted hits it;
- clients replaying a turn that carried only attachments/multimodal parts send
  `"content": null`;
- it does **not** fire on the tool-calling path, which forces `direct` and never calls
  `decide()` (`app/pipeline.py:422`) — so the change that introduced it also hid it from
  its own tests.

**Fix (one line, plus the guard that stops it recurring):**
- `last_user = m.content or ""` in `app/routing/task_mode.py`.
- Optional but worth it: decide whether a user turn with no text is a `422` at the API
  boundary rather than silently classifying as `direct` — if a request has no readable
  user text at all, "malformed body" is the more honest answer than routing it.
- Add a `content=None` case to `tests/test_compression_and_mode.py`, which is where the
  step-4 rules `decide()` is pinned today. `tests/test_openai_compat.py` won't catch it:
  it exercises the tool-calling path, which bypasses `decide()` entirely.

**Related cleanup (not a bug, no behaviour change):** `AppPolicy.capture_io`'s docstring
in `app/apps/models.py` still states the pre-PR-#34 rule — that `full` requires an app
that is "NOT onprem_only". The live rule is own-Langfuse-project (always) **plus**
`policy_mode: "passthrough"` for an `onprem_only` app; see `docs/per-app-config.md`.
Worth correcting in the same pass so the model file doesn't read as a third authority.
(The similar comment in `app/config/schema.py`'s `LangfuseCfg` is *correct* — it
describes the deployment-wide boot lint, which is still residency-keyed.)

---

> **Update 2026-09-05 (`ui:0.17.0`).** Every per-app policy field is now settable from
> the console (`AdminPolicy` completed to match `PolicyIn` 14-for-14). **B21** is
> resolved, and two previously-undocumented bugs found while doing it are recorded
> below as **B22** and **B23**. Both are the *same class of defect*: **the console had
> encoded a server rule locally, and drifted when the server moved.** That is the
> lesson worth carrying out of this change — a client-side copy of a policy rule is a
> second authority, and a second authority is a bug waiting for the first one to
> change. The console's job is to *explain* the server's clamps and show their 422s,
> never to re-derive them; where it must predict one to disable a control, the
> prediction has to be traceable to the server function it mirrors and be permissive
> on doubt, because a UI that is stricter than the API silently removes a
> configuration the operator is entitled to.

## B22 — Console refused `capture_io=full` for every `onprem_only` app (stale mirror of a replaced server rule)  ✅ RESOLVED (2026-09-05, `ui:0.17.0`)

**Severity:** medium (blocked a legitimate, shipped configuration; no data risk)
**Found:** 2026-09-05, while adding the `policy_mode` control to the Settings tab

**What it was.** `PolicyEditor` disabled the `full` option in the eval-capture dropdown
whenever the app's effective residency was `onprem_only`. That mirrored the *old*
server rule, where `onprem_only` doubled as a proxy for "maximum protection" and raw
capture was refused for on-prem apps outright. PR #34 (`policy_mode`) replaced that
proxy: placement (`residency`) and protection (`policy_mode`) are separate axes, and
`set_policy` now refuses `full` only for an `onprem_only` app **still in the `enforced`
posture** (`app/api/admin_routes.py`, `eff_residency == "onprem_only" and
eff_policy_mode != "passthrough"`). The UI was never updated, so the console was
**refusing a configuration the API accepts.**

**Impact.** This is precisely what blocked the MillWise change and sent the operator to
`curl`: `onprem_only` + `passthrough` + `full` is valid and honest — the on-prem
guarantee is fully intact, the app has simply declared it has nothing to protect — and
the console said no with no reason and no way forward. Nothing was unsafe; a supported
posture was unreachable.

**A second, quieter half.** The gate the UI *did* apply was also wrong on the other
side: it tested `langfuse_project_id` alone, while the server's condition is
`load_app_langfuse(app) is not None` — project id **and** a key pair. An app with a
project id but no keys was offered `full` and got a 422 (which, per B23, it never saw).

**Fix (`ui/src/admin/ApplicationsPanel.tsx`).** The predicate now mirrors `set_policy`
exactly: `full` requires the app's own Langfuse project (`langfuse_project_id` **and**
`has_langfuse_keys`) and, for an `onprem_only` app, an effective `policy_mode` of
`passthrough`; when it is blocked the option is labelled with *why* and with the way
out, rather than being silently greyed. The same pass corrected `redacted`, which is
refused wherever nothing is sanitized (`passthrough`, or an explicit `pii_detection` of
`off`/`regex`) and was previously always offered. The currently-stored value is never
disabled, so an operator can always see what the app is actually set to.

## B23 — `PolicyEditor.save()` swallowed every error; a rejected save looked like a no-op  ✅ RESOLVED (2026-09-05, `ui:0.17.0`)

**Severity:** medium (silent failure on the one screen where the server does all the
reasoning)
**Found:** 2026-09-05, same read-through as B22

**What it was.** `PolicyEditor.save()` was `try { … } finally { setBusy(false) }` — no
`catch`, no error state. Every `PUT /admin/applications/{id}/policy` failure was
discarded: the button flipped back to "Save policy" and nothing else changed. A 422 and
a success were indistinguishable on screen.

**Why that matters more here than elsewhere.** The admin clamps are the *authority* on
every posture decision in this form, and each 422 `detail` is written to name the rule
that fired and the alternative to use — "`capture_io='redacted'` cannot be combined
with `policy_mode='passthrough'` … use `capture_io='full'` with the app's own Langfuse
project, or `capture_io='off'`". That text is the operator's only guidance on this
screen, and all of it went in the bin. Combined with B22 the failure was total: the
console blocked one valid posture outright and, for everything it did let you attempt,
refused to say why the server had said no.

**Fix.** Two changes. `PolicyEditor` gained an error state rendered under the Save
button, verbatim. And the shared `j<T>()` fetch helper in `ui/src/api.ts` gained
`errDetail()`, which pulls FastAPI's `detail` out of a non-2xx body — previously every
admin error surfaced as a bare status line, so even a `catch` would have had nothing
useful to show. Both were needed; either alone leaves the operator guessing.

## B20 — SSN downgraded out of `onprem_only` when spaCy tags it DATE_TIME/PHONE (dedupe defeated the SSN backstop)  ✅ RESOLVED (2026-08-23)

**Severity:** MEDIUM (real privacy-routing gap, not just a flaky test — surfaced *as* a
flaky test). **Found:** 2026-08-23 while integrating PRs #18–#23 (pre-existing; present
on baseline `c5a98bd`; no feature touched it).

**What it really was.** `test_traffic_personas.py::test_generated_ssns_are_detected_by_presidio`
flaked ~40–60% *across process starts* (seeded inputs, so not the generator). Root cause:
`PresidioDetector.analyze` appends a regex `US_SSN` backstop (B4), but spaCy
nondeterministically tags an SSN-format string (e.g. `818-30-4221`, `818` = a real LA
area code) as `DATE_TIME`/`PHONE_NUMBER` on the *same span*. `dedupe` (`app/policy/merge.py`)
resolves same-span overlap by highest `score`; the backstop had `score=None`→0.0 and the
Presidio entity tied/won, so the merged entity was typed `DATE_TIME` and the `US_SSN` was
dropped. Impact beyond the test: `US_SSN`→`restricted`+`onprem_only` (policy.yaml:14–16)
but `DATE_TIME`/`PHONE_NUMBER`→the lower tier (residency not onprem_only), so a
misclassified SSN could **route to a cloud provider** (rule 3/8). Redaction still masked
the span (no raw leak), but the residency/sensitivity downgraded.

**Fix (`app/policy/detectors.py`).** The regex `US_SSN` backstop now carries `score=1.0` —
an exact SSN-format match is a high-confidence privacy signal that MUST win `dedupe`, so an
SSN is always typed `US_SSN` regardless of spaCy's per-process mood (fail-safe: SSN-format
→ most-sensitive interpretation). Verified 0 misses across 8 process starts + 6/6 test runs.
Regression locked at the dedupe level in `tests/test_policy_merge.py::
test_scored_ssn_wins_type_over_coincidental_datetime_overlap` (deterministic, no Presidio
dep). This restores B4's intent (an SSN never slips the gate) through the merge step.

## B21 — Can't edit an onboarded model in the console (only enable/disable + delete)  ✅ RESOLVED (2026-09-05, `ui:0.17.0`)

**Resolution.** `ModelRow` in `ui/src/admin/InfrastructurePanel.tsx` now has an **edit**
action that expands an inline form over every field `OnboardIn` accepts — display name,
endpoint URL, `api_key_env`, tier, prices, quality, context window, allowed-app list,
notes — pre-filled from the row and submitted through the existing `onboardApi.update`
(`PUT /admin/infra/models/{slug}`). No backend change, exactly as the write-up predicted.

Two things the fix had to respect, both properties of the existing PUT:

- **`slug` is read-only in the edit form.** The PUT is keyed by the *path* slug and
  ignores a slug in the body, so a "rename" that looked like it worked would silently
  edit the old row. Renaming a model is delete + re-onboard, and the form says so.
- **The PUT re-probes on every call and still returns `200` when the probe fails**
  (`final_enabled = body.enabled and ok`). So a save can succeed and leave the model
  disabled. The row now renders the returned `probe_status` / `probe_error` after
  saving instead of silently showing a model that is off. Original write-up kept below.

**Severity:** medium (admin usability; forces delete-and-re-onboard)
**Found:** 2026-08-28, Infrastructure panel

Once a model is onboarded, the console has no way to change its fields. The
`ModelRow` in `ui/src/admin/InfrastructurePanel.tsx:218-329` renders only a
`Toggle` (enable/disable) and a `delete` button — there is no edit form. To
correct a tier, endpoint URL, `api_key_env`, price, quality, context window,
allowed-app list, display name, or notes, an operator must delete the row and
re-onboard from scratch (re-probing, re-typing every field). This is the same
class of gap as B6 (couldn't rename an application) but for the model fleet.

**Not a backend gap.** The edit path already exists and is already wired: the
enable toggle calls `PUT /admin/infra/models/{slug}` (`onboardApi.update`,
`admin_routes.py:580 edit_onboarded_model`) with the *full* `OnboardIn` body,
spreading the row's current fields and flipping only `enabled`
(`InfrastructurePanel.tsx:236-252`). The PUT re-validates and re-probes the whole
body on every call. So the fix is UI-only: surface an "edit" action that opens
the same field set as `OnboardForm` (line 66), pre-filled from the row, and PUTs
the edited body — the toggle proves the round-trip works.

**Fix idea:** add an edit button/row-expander next to `delete` that reuses the
`OnboardForm` inputs (prefilled), submitting via the existing `onboardApi.update`.
`slug` stays the identity key (PUT is keyed by slug); if slug itself must change,
that's a delete+create, so keep slug read-only in the edit form. Own SDD task.

## B1 — Multi-step 503s mislabeled "policy evaluation unavailable"  ✅ RESOLVED (2026-08-16, verified in code)

**Resolution:** the fail-closed 503 is now scoped to the policy-setup stage only
(`app/api/routes.py:149-171` raises `PolicyGateError` just for profile/registry setup);
downstream `pipeline.run()` failures map to distinct statuses (422/500/502) and a broad
`except Exception` returns 500 "internal error". The 503 now means what it says. Original
write-up kept below.

**Severity:** medium (misleading, not data-unsafe)
**Found:** 2026-07-31, DGX burst on `:0.6.0`

`app/api/routes.py:104` wraps the **entire** `pipeline.run()` in one broad
`except Exception -> HTTPException(503, "policy evaluation unavailable")`. The
gate (`app/policy/gate.py`) actually clears fine; for multi-step requests the
exception is raised **downstream** (agent hop / backend call / timeout), but
the caller still sees the gate's fail-closed message. This points debugging at
the wrong step (step 2) when the real failure is in steps 5–8.

**Fix idea:** narrow the fail-closed catch to the gate/context stage only;
give downstream pipeline failures their own label + status (e.g. 502/504 for
backend/timeout), so the gate 503 means what it says. Keep fail-closed
semantics for anything before the gate clears. Own SDD task.

## B2 — Multi-step burst saturates single vLLM (real inference capacity)

**Severity:** medium (capacity characteristic, not a correctness bug)
**Found:** 2026-07-31, DGX burst on `:0.6.0`

Post-LiteLLM-swap, each agent hop does **real** ~16–24s vLLM inference (was
instant fake). Under a burst, dozens of long-lived multi-step requests overlap
on the **single** on-prem vLLM pod; some exceed `vllm.timeout_s: 60` →
exception → B1 503. **Lowering rps does not help** — the requests are
long-lived, so they overlap regardless of arrival rate (19/150 failed at rps=3
vs 21/150 at rps=8, all `multi_step`).

**Levers (pick per goal):**
- Scale vLLM (2–3 replicas behind the on-prem vLLM service) — real fix, needs GPU + manifest change.
- Trim the `multi_step` share in `tests/personas.yaml` for the burst demo — cheap, keeps the "500-req burst" moment clean.
- Raise `vllm.timeout_s` and/or bound agent concurrency — mitigates, doesn't remove.
- Blocked from scaling the **API** tier by B3.

## B3 — In-memory dashboard state blocks horizontal API scaling  ✅ RESOLVED (2026-08-22, PR #16 / :0.13.0)

**Resolution:** shared-state landed in PR #16 — the durable `request_snapshots` table replaces
the process-local `HistoryStore` for console list/detail/stats, and `PgDecisionBridge`
(Postgres LISTEN/NOTIFY, inert on sqlite) carries the SSE decision stream across replicas.
The API tier can now be scaled horizontally with a coherent dashboard (see
`docs/architecture/api-load-balancing.md`). Original write-up kept below.

**Severity:** medium (architectural; blocks B2's API-scaling lever)
**Found:** 2026-07-31

`app/api/history.py` (ring buffer) and `app/api/bus.py` (SSE decision stream)
are **process-local**. Running >1 API replica load-balances requests across
pods, so the flow visualizer and cost dashboard each render only the fraction
of traffic that hit their pod — the dashboard splits. So the API tier cannot
be scaled out for throughput without first moving history + the decision bus
to a **shared store** (DB or Redis pub/sub). Until then, single API replica is
a hard constraint for a coherent demo.

## B4 — Presidio SSN recognizer misses raw SSN on egress  ✅ RESOLVED (2026-08-16, verified in code)

**Resolution:** a regex `US_SSN` layer covers Presidio's blocklisted sample SSNs
(`app/policy/detectors.py:81-100`) and the egress redactor reuses the same `RegexDetector`
(`app/obs/redact.py:14-25`, `app/obs/export.py:55-56`), so raw SSNs are masked on egress.
Original write-up kept below.

**Severity:** low (pre-existing, unrelated to the LiteLLM swap)
**Found:** 2026-07-31 (also present on `:0.5.0`)

In one multi-step output the egress redactor masked `PERSON` but left a raw
`123-45-6789` in the completion. Egress redaction (CLAUDE.md rule 2) missed
the SSN pattern in model-generated text. Needs a recognizer/tuning pass on the
**egress** path (the input gate caught it; egress didn't).

## B5 — Multi-step latency reported as 0 ms  ✅ RESOLVED (2026-08-06, Cluster C)

**Severity:** medium (resolved)
Per-hop latency now flows end-to-end: each `agent_hop` DecisionRecord records
the real measured `latency_ms` (`app/agent/internal_endpoint.py`, from the
backend's wall-clock timing), and `app/api/history.py` + the DB usage row sum
it into the request total; the UI drilldown reads the summed value. Closed out
in Cluster C with hardened `> 0` latency assertions
(`tests/test_litellm_multistep.py`, `tests/test_agent_hop_compression.py`) and a
corrected `app/gateway/backends/base.py` comment. Cross-ref memory
`routeforge-multistep-latency-zero-bug` (now stale/resolved).

## B6 — Can't rename an application  ✅ RESOLVED (2026-08-16, verified in code)

**Resolution:** `PATCH /applications/{app_id}` (`app/api/admin_routes.py:252-257`, `UpdateAppIn.name`)
with an Edit form in `ui/src/admin/ApplicationsPanel.tsx:257-356`. Original write-up kept below.

**Severity:** medium (admin usability)
**Found:** 2026-08-04, UI

Editing an application's name in the admin UI doesn't persist (or the field is
not editable). Operators can't correct/rename an app after creation. Needs a
working edit path (UI field + PATCH/PUT to the app record) in
`ui/src/admin/ApplicationsPanel.tsx` and the corresponding API route.

## B7 — Can't delete an application  ✅ RESOLVED as soft-archive (2026-08-16, verified in code)

**Resolution (partial):** implemented as archive/restore via `PATCH status=archived`
(`ui/src/admin/ApplicationsPanel.tsx:364-368`, `app/api/admin_routes.py:252`) rather than a
destructive `DELETE` route. If true deletion (with cascade) is required, treat this as still
open. Original write-up kept below.

**Severity:** medium (admin usability)
**Found:** 2026-08-04, UI

No working way to delete an application from the admin UI. Operators accumulate
stale/test apps with no cleanup path. Needs a delete action in
`ui/src/admin/ApplicationsPanel.tsx` + a DELETE API route (and decide cascade
behavior for the app's policy/rows).

## B8 — Application setup doesn't show the cost currency unit  ✅ RESOLVED (2026-08-16, verified in code)

**Resolution:** setup inputs are labeled `price / 1M input ($)` and `price / 1M output ($)`
(`ui/src/admin/InfrastructurePanel.tsx:157,161`). Original write-up kept below.

**Severity:** low (cosmetic/clarity)
**Found:** 2026-08-04, UI

In the application setup screen, cost values render without a `$` unit, so the
number is ambiguous. Add the `$` prefix (and confirm USD/1M framing matches
`pricing.yaml`) on the setup UI.

## B9 — Cost density explanation text is truncated  ✅ RESOLVED (2026-08-16, verified in code)

**Resolution:** the explanation moved to a tooltip (`info=` prop → `title=`) in
`ui/src/console/sections/CostSection.tsx:138,143` (`ui/src/components/common.tsx:39,49`); no
more clipping. Original write-up kept below.

**Severity:** low (cosmetic/clarity)
**Found:** 2026-08-04, UI

Under Cost, the "Token Intelligence Density" and "Cost Intelligence Density"
StatTiles have `sub` explanation text ("quality delivered per 1k tokens / per
$ — needs a per-model quality score") that gets clipped and doesn't display
fully (`ui/src/console/sections/CostSection.tsx:135-144`). Either show the full
text (let the tile wrap/expand) or move the explanation into a tooltip/info
balloon (`?` hover) so the label stays compact.

## B10 — Router shows disabled cloud providers as spurs, mislabeled "residency"  ✅ RESOLVED (2026-08-16, verified in code)

**Resolution:** `ModelRegistry.effective_pool()` now drops candidates whose provider isn't
enabled in `serving.providers` (`app/apps/registry.py:67-68`), so killed cloud paths never
enter the pool; the residency rejection reason is now distinct from "not permitted"
(`app/routing/router.py:63-64`). Original write-up kept below.

**Severity:** medium (misleading demo trace + wrong rejection reason)
**Found:** 2026-08-04, UI (Model Router card, decision drilldown)

The Model Router card lists *GPT-4o mini (Azure)* and *Claude Sonnet (Bedrock)*
as rejected spurs labeled **"excluded · residency"**, even though both cloud
providers are disabled at the infra kill-switch (`config/serving.yaml:61-62`,
`azure/bedrock enabled: false`). Two distinct defects:

1. **Disabled providers appear as candidates at all.**
   `ModelRegistry.effective_pool()` (`app/apps/registry.py:60-67`)
   unconditionally merges the entire static YAML cloud `model_pool`
   (`config/routing.yaml:60-61`) without consulting
   `providers.{azure,bedrock}.enabled`. So a killed cloud path still enters the
   candidate list. Per CLAUDE.md, "cloud path availability =
   providers.{azure,bedrock}.enabled" — a disabled provider shouldn't be a
   candidate. These aren't onboarded registry models either, which is why they
   read as phantom entries.

2. **Wrong rejection reason.** When the router drops a candidate whose provider
   is absent from `ctx.allowed_providers` (`app/routing/router.py:61-66`), it
   hardcodes `reason="excluded by policy (residency)"`. But
   `ctx.allowed_providers` is `serving.providers.*.enabled` filtered by
   residency (`app/policy/context.py:56-72`) — so a provider dropped by the
   kill-switch is mislabeled "residency" even for a non-sensitive request with
   no PII. The two causes (disabled vs. residency) are conflated.

**Fix idea:** exclude candidates whose provider is disabled in `serving.yaml`
from `effective_pool()` (or before the router loop) so they don't show as spurs
at all; and/or split the `allowed_providers` rejection into distinct reasons
("provider disabled" vs. "excluded · residency") so the label tells the truth.
Decide whether disabled providers should be hidden entirely or shown greyed with
an honest "provider off" reason. Own SDD task.

## B11 — "What happened" summary card labels request "Cloud-eligible" when cloud was excluded  ✅ RESOLVED (2026-08-16, verified in code)

**Resolution:** the eligibility line is now derived from `p.allowed_providers` (the effective
pool), not the `onprem_only` flag (`ui/src/console/RequestDrilldown.tsx:432-435`). Original
write-up kept below.

**Severity:** medium (misleading demo headline; contradicts the panel below it)
**Found:** 2026-08-04, request drilldown (support-ticket demo)
Cross-ref: B10 (same request, router-card side of the same confusion).

The *What happened* PRIVACY card headlines a request **"Cloud-eligible — no
sensitive data found, so every provider was eligible"** while, on the very same
screen, SERVING PROVIDERS greys out Azure and Bedrock and the Model Router
rejects them. In the ticket demo cloud is excluded because
`providers.{azure,bedrock}.enabled: false` in `config/serving.yaml`, so
`ctx.allowed_providers` is `onprem` only — yet the summary still says every
provider was eligible. Two smaller wrongs compound it: the card also shows a
`PERSON` chip right beside "no sensitive data found," and the sensitivity is
`confidential`.

**Root cause:** the summary card derives "cloud-eligible" from the gate's
`onprem_only` flag alone (`false` ⇒ "every provider eligible"), instead of from
the **effective candidate pool** (`ctx.allowed_providers`) after infra
kill-switch + residency filtering. `onprem_only=false` only means *residency
didn't force on-prem*; it does not mean cloud is actually reachable.

**Fix idea:** compute the card's eligibility line from `ctx.allowed_providers`
(does it contain any cloud provider?), not from `onprem_only`. When cloud is
absent, say why (e.g. "cloud providers disabled" vs. "kept on-prem by
residency"). Reconcile the "no sensitive data found" wording with the detected
entity chips so a `PERSON`/`confidential` request doesn't read as clean. Pairs
naturally with B10. Own SDD task.

## B12 — "All-frontier baseline" omits completion tokens → routing_saved goes negative  ✅ RESOLVED (2026-08-16, verified in code)

**Resolution:** `Pipeline._baseline(in_tokens, out_tokens)` now prices
`in_tokens×input_rate + out_tokens×output_rate` and is called with the actual input+output
token counts (`app/pipeline.py:59-64,159-160,237,249`), so routing_saved is positive for a
cheaper-tier request and zero for a same-tier request. Original write-up kept below.

**Severity:** medium (undercuts the flagship cost-savings claim; number goes
negative on the screen meant to prove savings)
**Found:** 2026-08-04, request drilldown + Overview (support-ticket demo)

A direct request to the mid model (152 in / 972 out) shows `actual spend
$0.0105`, `all-frontier baseline $0.0023`, and `routing saved $-0.0082` —
routing appears to *cost* money. The Overview simultaneously reports `+$0.0255 /
27.4%` saved across the 6 calls, and the per-request baseline ($0.0023) is ~6.7×
smaller than the implied aggregate baseline (`spend+saved` ⇒ ~$0.0155/req).

**Root cause:** `Pipeline._baseline()` (`app/pipeline.py:58-60`) prices a single
token count at the blended `(input+output)` rate, and the direct path feeds it
`comp.tokens_before` (`app/pipeline.py:150`) — the **pre-completion** token count
(~the prompt, ~128 tokens). So the baseline excludes the 972 output tokens the
model actually generated, while `total` (actual spend) includes them.
`routing_saved = baseline_usd - total` (`app/pipeline.py:153`) is therefore
comparing frontier-price-of-the-prompt against mid-price-of-prompt+completion —
apples to oranges — and goes negative. A real frontier model answering the same
request would emit ~972 output tokens too (~972 × $15/1M ≈ $0.015), so the honest
all-frontier baseline is *higher* than actual and routing_saved should be
**positive** (~+$0.005).

Two compounding defects:
1. **Wrong token basis.** Baseline is priced on `tokens_before` (input only), not
   the actual input+output the request consumed.
2. **Wrong rate model.** `_baseline` multiplies tokens by `(input+output)` summed
   rate, charging every token both prices, instead of
   `in_tokens×input_rate + out_tokens×output_rate`.

The per-request-vs-aggregate mismatch is almost certainly the same root cause
surfacing differently between the drilldown value and the dashboard aggregation
(`app/api/history.py` / cost section) — verify both read the same corrected
figure once (1) and (2) are fixed.

**Fix idea:** price the baseline on the *actual* completion's input+output token
counts (same basis as `total`), with input and output priced at their respective
frontier rates; keep the additive identity `compression_saved + routing_saved ==
baseline_usd - total`. Add a unit test that a same-tier request yields
`routing_saved == 0` and a cheaper-tier request yields `routing_saved > 0`.
Re-verify the Overview aggregate against summed per-request values. Own SDD task.

## B13 — `capture_io: redacted` stores raw application input in Langfuse (weaker than the gate); UI claims it never does  ✅ RESOLVED (2026-08-22, folded into B14/F1)

**Status 2026-08-22:** structural cause fixed. `routes.py` now threads the gate's
`sanitized_prompt` into `export_trace`, and `_set_io` (`app/obs/export.py:72`) prefers that
sanitized text over independently re-redacting the raw body — so `redacted` telemetry can no
longer be weaker than what the model saw. The false "raw prompt is never stored" caption is
gone; the drilldown renders bodies by the per-app display mode
(`ui/src/console/RequestDrilldown.tsx`). The only residual is inherent, not a defect: business
fields (subject/customer_id) aren't PII *entity types*, so redaction can't mask them — an app
carrying such fields uses `capture_io=off` or its own `full` project. Original write-up kept below.

**Prior status 2026-08-16 (historical):** structural cause was still present — raw
`body.messages[-1].content` passed to `export_trace`, re-redacted independently.

**Severity:** medium (privacy-story integrity; Rule #2. Demo lab uses synthetic
data so not a live breach, but it undercuts the central claim on-camera)
**Found:** 2026-08-04, verified by curling the DGX-lab Langfuse trace
`6d892726ae704ab83320dd3a3d3d8a27` (`serving.request`).

The DGX lab overrides the shipped default: `SP_CAPTURE_IO=redacted`
(`deploy/dgx_lab/routeforge-app.yaml:127`), so `input.value`/`output.value` are
attached. Two problems surfaced:

1. **Input is stored essentially raw.** The trace `input.value` is the verbatim
   application payload — `{"customer_id":"CUS-204","region":"Austin","subject":
   "Cannot connect after router replacement","summary":"Customer replaced a home
   router…","ticket_id":"TCK-1001",…}` — byte-for-byte the source ticket. Notably
   `"region":"Austin"` is stored **unredacted** even though the Privacy tab
   flagged `LOCATION` and the model's *own* prompt had Austin masked (the
   completion echoes it as `<PERSON_1>`). So telemetry redaction is **weaker than
   the gate's**: `routes.py:122` passes the **raw** `body.messages[-1].content` to
   `export_trace`, which re-runs Presidio (`_set_io` → `redact`, `app/obs/export.py:31-33`)
   on the compact JSON and under-detects. Business fields (subject, summary,
   customer_id) aren't entity types at all, so they pass through untouched. Net:
   `redacted` mode ≈ raw application input in Langfuse.

2. **The UI claim is false in this mode.** The drilldown Request card states *"The
   raw prompt is never stored or shown — nothing external sees it until the gate
   clears it"* (`ui/src/console/RequestDrilldown.tsx:149`), unconditionally — but
   Langfuse (an external sink) has the prompt whenever `capture_io != off`. The
   copy needs to reflect the mode, or the guarantee is misleading.

**Root cause:** the telemetry path redacts the *raw request body* independently
instead of reusing the gate's already-sanitized text + redaction map. The gate
and the exporter therefore disagree on what's PII.

**Fix idea:** feed `export_trace` the gate's **sanitized** prompt + its redaction
map (the same masking the model received), not `body.messages[-1].content`; then
`redacted` telemetry can never be weaker than what the model saw. For the UI copy:
**B14 supersedes the copy-only patch** — instead of just relabeling the Request
card, drive the actual request+response *display* from the per-app setting
(symmetric: both bodies shown redacted/raw, or metadata-only when `off`). Short-term
demo mitigation: set `SP_CAPTURE_IO=off` on the DGX deploy to restore blank Input.
Cross-ref [`docs/eval-logging-backlog.md`](../archive/eval-logging-backlog.md) and B14. Own SDD task.

## B14 — FEATURE: per-application capture_io (off | redacted | full) in app settings  ✅ SHIPPED (F1 Phase B :0.12.0; + pii_detection/stream_mode in PR #17 :0.14.0)

**Resolution:** per-app `capture_io` (`AppPolicy.capture_io`, `app/apps/models.py`) is threaded
per request into `export_trace` with a fail-closed clamp (`app/api/routes.py`), symmetric UI
display by mode (`RequestDrilldown.tsx`), and the per-app residency lint (onprem_only cannot
reach `full`). PR #17 (:0.14.0) extended the same per-app pattern to `pii_detection` and
`stream_mode`, plus operator-gated raw eval capture (`allow_app_pii_downgrade`). Router-diagnostic
`sp.*` metadata is always-on regardless of `capture_io` (:0.11.0). Original write-up kept below.

**Type:** feature (with a design-bug component: the current global-only switch is
too coarse for a multi-tenant product).
**Severity:** medium (unblocks per-app eval/audit without breaking the privacy
story; requested by owner 2026-08-04).
**Depends on:** B13 (redaction gap) — per-app `redacted` is only trustworthy once
`redacted` actually redacts. Cross-ref [`docs/eval-logging-backlog.md`](../archive/eval-logging-backlog.md).

**What:** move `capture_io` from a single global switch
(`serving.observability.langfuse.capture_io` + `SP_CAPTURE_IO`) to a
**per-application** setting chosen in the app's settings UI, so each application
decides whether *its* Langfuse traces store `off` (no I/O), `redacted` (values
masked to `<TYPE>`), or `full` (raw prompt + completion). Langfuse then stores
per the owning app's choice.

**Why it fits routeForge:** the product is multi-tenant/multi-app; per-app policy
already exists (`AppPolicy`/`AppPrivacyProfile`: `allowed_providers`,
`allowed_tiers`, custom recognizers), and `app/obs/export.py:export_trace`
**already routes each app's trace to its own Langfuse project** (`app_langfuse`),
so raw data for app X is already isolated to X's project. The global switch is the
outlier: it forces all-or-nothing and makes `full` a boot failure whenever *any*
tenant is `onprem_only` (`app/config/lint.py:107-117`), even for apps that carry
no sensitive data. Per-app capture removes that false coupling.

**Design:**
- New field on the app's policy/profile (DB-backed `AppPolicy` or
  `AppPrivacyProfile`), e.g. `capture_io: off | redacted | full`, default =
  inherit global / `off`. Surfaced in the Applications settings panel
  (`ui/src/admin/ApplicationsPanel.tsx`).
- Thread the app's value into `export_trace(capture_io=…)` per request instead of
  reading the global (`app/api/routes.py:121`).
- **Per-app residency lint (the load-bearing guardrail).** The global "reject
  `full` if any onprem_only tenant" check becomes per-app: an app may select
  `full` only if it is NOT onprem_only / cannot carry `restricted` traffic, OR its
  `app_langfuse` target is residency-local + access-controlled. The setting is
  **opt-in within policy, never a policy override** — an onprem_only app cannot use
  it to send raw PHI to a shared project (Rules #2/#6/#8; residency beats eval).
- **Fail closed:** missing/invalid/unknown → most-restrictive (`off`/`redacted`),
  never raw.
- **RLHF amendment (see B16 / `docs/rlhf-readiness.md`):** `capture_io` gates only
  the raw prompt/completion **I/O**. Router-diagnostic metadata (eligible/rejected
  candidate snapshot, selection reason, routing-policy version) is **not PII** and
  must be emitted **always-on**, independent of the tri-state — even an `off` app must
  still yield router-training signal. Add these to the always-emitted `sp.*` set.
- **The setting governs the routeForge UI display too, not just Langfuse.** Today
  the drilldown shows the *response* but hides the *request* behind a (false)
  "never stored" caption — asymmetric and inconsistent. Drive both surfaces from
  the per-app setting, symmetrically: `off` → show neither body (metadata only);
  `redacted` → show request **and** response, masked; `full` → show both raw. This
  **supersedes B13's copy-only fix** (`RequestDrilldown.tsx:149`) — fix the
  *display*, not just the caption.
- **Trust-boundary nuance (keep display and export distinct in the schema).** The
  routeForge UI is *first-party* (owning tenant/operator viewing their own request
  inside the boundary); Langfuse is *egress* (external sink, possibly a shared
  project). "Raw in the UI" is a lighter risk than "raw in Langfuse". Couple them
  under one tri-state in v1 for simplicity, but model *display* vs *export* as two
  concepts so a later split (UI raw / Langfuse redacted) needs no rewrite. The
  residency guardrail binds the **export/full** side, not the first-party UI view.
- Carry the `full` warning per app: raw store must be access-controlled with a
  retention TTL.

**Scope guard:** this captures *data* (in scope). Eval/regression *pipelines* on
top = Improvement Plane = out of scope (CLAUDE.md).

**Gotcha:** new AppPolicy/AppPrivacyProfile column needs a manual `ALTER` (or DB
recreate) on deployed DBs — `create_all` never migrates (see memory
`routeforge-schema-no-migrations`). Own SDD task; plan first.

## B15 — FEATURE: per-application Langfuse project (isolation) + admin cross-app reporting  🟡 PARTIAL (2026-08-22)

**Status 2026-08-22:** two of three halves shipped. **(a) Admin cross-app reporting ✅** —
the "Cross-app usage" console tab (`ui/src/admin/CrossAppReport.tsx` ← `GET
/admin/usage/by-application`) is the DB-backed source-of-truth rollup, so cross-app reporting
does NOT depend on Langfuse. **(b) Manual per-app creds ✅** — `PUT
/admin/applications/{id}/langfuse` (F1 Phase C) + `load_app_langfuse` route each app's traces to
its own project when configured. **(c) Automatic project-per-app provisioning ⏳ STILL OPEN** —
`LangfuseProvisioner` needs `LANGFUSE_HOST` + `LANGFUSE_ORG_PUBLIC_KEY` + `LANGFUSE_ORG_SECRET_KEY`
(org-scoped key pair); those are **not set on DGX**, so provisioning is a no-op and every app
falls back to the shared `demo-project` keyed by the deployment `LANGFUSE_PUBLIC_KEY`. Making
project-per-app the *default* (auto-create on app onboarding) + the optional redacted rollup
project remain. Original write-up kept below.

**Type:** feature (tenancy/observability).
**Severity:** medium (multi-tenant data separation + precondition for B14 raw
capture; currently all apps land in the shared `demo-project`).
**Relates to:** B14 (per-app capture_io) — this is the store-isolation half of the
same workstream. Langfuse is **v3** (`deploy/dgx_lab/langfuse.yaml:277`).

**Current state:** on the DGX box every application exports to the single shared
project `demo-project` — so any project viewer sees *all* apps' traces. The per-app
machinery already exists but is unused: `export_trace(app_langfuse=…)`
(`app/obs/export.py`) routes to an app's own project, `get_app_tracer(host,
public_key, secret_key)` builds a per-app exporter keyed by its keys, and
`app_trace_url(project_id,…)` (`app/obs/otel.py:51-56`) builds per-project
deep-links. Today nothing populates `app_langfuse` per app.

**What:** make **project-per-application** the product default — each app exports
to its own Langfuse project, and its owner has keys/access to only that project.

**Pros:** real tenant isolation (Langfuse v3 project = the RBAC + data boundary);
**enables per-app residency** (an `onprem_only` app points at a residency-local
project while others use shared/cloud — the precondition B14's `full` mode needs; a
single shared project cannot satisfy it); per-app retention/TTL, quotas, blast
radius; clean owner deep-links.

**Cons:** **no native cross-project aggregation in Langfuse v3** (dashboards +
Metrics API are project-scoped; org roles grant per-project access, not a merged
view) — see admin reporting below; operational overhead (N key pairs to provision,
store, rotate; automate project+key creation on app onboarding via Langfuse Admin
API); fragmented per-project dashboards.

**Admin cross-app / app-wise reporting (the key design point):**
1. **Primary: report from routeForge's OWN admin store, not Langfuse.** routeForge
   computes every DecisionRecord and already aggregates across all apps (Overview/
   Cost/Traffic/Privacy tabs). Cross-app + app-wise rollups belong here — source of
   truth, not gated on Langfuse, and keeps raw tenant data out of any shared store.
2. **Optional: a redacted, metadata-only "admin rollup" Langfuse project.** Second
   copy carrying only `sp.*` (entity **types**, cost, model, sensitivity — never raw
   I/O), so it's residency-safe to co-mingle; filter/group by `sp.application_id`.
   Cost: double-write + a second store to govern.
3. External aggregation over each project's Metrics API into a warehouse/Grafana —
   most flexible, most work, duplicates (1).

**Design:** per-app Langfuse creds on the app record (host/public_key/secret_key/
project_id), surfaced/managed in admin onboarding; provision project+keys on app
create (Langfuse Admin API); **fail-closed** — no per-app project configured ⇒ fall
back to `off`/shared-redacted, **never raw-to-shared**. The per-app residency lint
(B14) binds the export target. Admin cross-app reporting served from routeForge's
admin console (1), with the redacted rollup project (2) optional.

**RLHF amendment (see B16 / `docs/rlhf-readiness.md`):** feedback scores must be
written to the **same Langfuse project as the trace** they annotate (scores are
trace-linked within a project), using that app's `app_langfuse` creds. The redacted
rollup project suffices for cross-app router eval; LLM fine-tuning reads the app's own
`full` project.

**Sequencing (whole workstream):** B13 (honest redaction) → B14 (per-app capture_io
governing storage + symmetric UI display) → B15 (per-app projects + admin rollup).
Shared files/data model — plan together. Own SDD task; plan first.

## B16 — FEATURE (future): RLHF-readiness — feedback capture, router-decision export, dataset export  🟡 PARTIAL (2026-08-16)

**Status 2026-08-22:** all three substrate gaps now shipped. Gaps 1 & 2 (`:0.11.0`) — the
always-on `sp.candidates` snapshot + `sp.routing_policy_version` content hash on every trace.
Gap 3 (F1 Phase D) — `POST /v1/feedback` (`app/api/routes.py`) writes a Langfuse score to the
trace's own project (fail-closed when the app has no project), and every chat response returns
`trace_id`. **Remaining (feature-shaped, not a gap):** routing-specific score configs
(`route_correct`, `preferred_model`, `failure_type`) and dataset-export readiness (traces +
corrected outputs → Langfuse datasets → external TRL transform). Training stays out of scope.
Original write-up kept below.

**Prior status 2026-08-16 (historical):** gaps 1 & 2 shipped; gap 3 still open.

**Type:** feature (future; design in `docs/rlhf-readiness.md`).
**Severity:** low-now / high-later (unlocks feedback-driven router + model
improvement; owner flagged 2026-08-05 as a future build).
**Scope boundary:** routeForge builds only the **capture/feedback/export substrate**.
Reward-model/DPO/PPO training, LLM fine-tuning, policy *retraining*, eval gates, and
promotion are the **Improvement Plane = out of scope** (CLAUDE.md), external via
TRL/OpenRLHF. Captures data; does not train or auto-promote (rule #4).
**Builds on:** B13/B14/B15 (the capture substrate) — with the two amendments noted in
B14 (router metadata always-on) and B15 (feedback lands in the trace's project).

Langfuse is the feedback-data + eval layer (traces, scores, annotation queues,
corrected outputs, datasets, experiments, export) — not the trainer. Two improvement
targets: **(A) the routeForge router** (highest value; trains a small classifier/
reranker/bandit or better rules from feedback — works on **redacted** data), and
**(B) an underlying LLM** (SFT/DPO from corrected outputs — needs **`full`,
residency-local** capture). See `docs/rlhf-readiness.md` §2/§5.

**Three gaps to close (grounded in code):**
1. **Eligible/rejected snapshot never reaches Langfuse.** `app/routing/router.py`
   builds `RouterDecision{chosen, reason, rejected[]}` with per-candidate reasons, but
   `app/obs/export.py` lifts only `sp.chosen`/`sp.reason`. Export the full candidate
   snapshot as an always-on structured attr (e.g. `sp.candidates`). *The make-or-break
   field for training/diagnosing the router.*
2. **No routing-policy version.** Nothing stamps one (`app/routing`/`app/policy`/
   `app/config/schema.py`). Add `routing_policy_version` (hash/semver of routing +
   scoring config) on every trace so Langfuse experiments can compare versions.
3. **No feedback ingestion path.** Add `POST /v1/feedback` (+ UI thumbs/reviewer form)
   → Langfuse **score linked to the trace** in the app's project; return the
   trace/request id on every chat response so async feedback can attach later.

**Also:** routing-specific score configs (`route_correct`, `preferred_model`,
`failure_type`); dataset-export readiness (traces + corrected outputs → Langfuse
datasets → external TRL prompt/chosen/rejected transform). Full field list, matrix,
and pipeline progression in `docs/rlhf-readiness.md`. Own SDD task; plan first
(after/with B13–B15).

## B17 — FEATURE: group a client-driven workflow's requests under one parent row (correlation id)

**RESOLVED 2026-08-06 (branch `feat/b17-request-grouping`).** A client tags a workflow run
via the `X-RouteForge-Session` header (opaque, ≤128 chars, `[A-Za-z0-9._:-]`, fail-open);
RouteForge sanitizes and stores it on each `RequestSummary`, exposes it on `/requests` +
drilldown, groups tagged rows into an expandable parent→children tree in the console, and
stamps `langfuse.session.id` on the trace root span so the run's N traces group into one
Langfuse Session. In-memory only — no DB column; durable/cross-replica grouping stays B3.
The reference integration (sample sends the header, one id per investigation run) was
applied to the local **untracked** `test_app` sample and is NOT part of this PR. Spec:
`docs/superpowers/specs/2026-08-06-b17-request-grouping-design.md`.

**Type:** feature (observability/UX).
**Severity:** low-now / medium-later (real-integration ergonomics; most customers
orchestrate client-side).
**Found:** 2026-08-05, walking the Requests tab with `test_app/routeforge_sample`.

**Observation.** `test_app/routeforge_sample` is a **client-side** LangChain workflow
(`agent.py` `RunnableSequence`: plan → synthesize → review). Each stage does its own
`model.invoke`, so **one run = three separate direct `/v1/chat/completions` requests**
to RouteForge, and two runs render as **six independent rows** (each `hops = 1`). From
RouteForge's view these are unrelated direct calls — the "agent" lives in the client,
NOT in RouteForge's own step-4 agent runtime. RouteForge therefore has **no link**
between the three calls that make up one investigation.

**Do NOT confuse two mechanisms:**
- **Server-side multi-step** (step 4 → agent runtime): all hops already land in ONE
  request's `resp["trace"]` and ONE `RequestSummary` row (`app/api/history.py`, `hops`
  field). Nesting these in the list is pure UI over existing data. The sample does not
  exercise this path.
- **Client-side workflow** (the sample): N independent direct requests. Grouping them
  needs a correlation id the client sends; RouteForge does not capture one today.

**What:** let a client tag the calls of one workflow with a **correlation/session id**;
RouteForge captures it, stores it on each request, and the Requests list groups tagged
rows into an **expandable parent → children tree** (parent = the workflow/session with
summed cost + latency + call count; children = the individual LLM calls with per-call
model/tier/cost/latency).

**Design sketch (own SDD spec — plan first):**
- **Client contract:** accept an OpenAI-compatible carrier — prefer the standard `user`
  field and/or a header (e.g. `X-RouteForge-Session`) — so any OpenAI SDK / LangChain
  client can set it without a bespoke API. Update `test_app/routeforge_sample` to send a
  per-investigation id across its three `model.invoke` calls (reference integration).
- **Server:** capture the id on the request, thread it into the `DecisionRecord` /
  `RequestSummary` (`session_id: str | None`), and expose it on the `/requests` list +
  drilldown payloads. No policy/routing behaviour change; it is a label only. Redaction
  rules still apply (a session id is operator-supplied, not PII, but treat it as opaque).
- **Langfuse sessions (same id, second surface).** `app/obs/export.py:export_trace`
  currently sets only `sp.*` + `input/output` on the root span and **never sets a session
  id**, so Langfuse shows N ungrouped traces and no Session (observed 2026-08-05: six
  traces, zero sessions, for two sample runs). Stamp the correlation id as Langfuse's OTel
  **session attribute** on the root span (e.g. `langfuse.session.id` / `session.id` —
  verify the exact key against the running Langfuse v3 OTel mapping) so the workflow's
  calls group under one Session automatically. **Granularity: one session = one workflow
  run, NOT one application** — a session-per-app would merge every request the app ever
  makes into a single session. One correlation id drives both the RouteForge Requests-tab
  tree and the Langfuse Session grouping.
- **UI:** in the Requests list, collapse rows sharing a `session_id` under one expandable
  parent; ungrouped (no id) requests render flat as today. Reuse the drilldown's per-hop
  presentation for the children. Mock:

  ```
  ▾ investigation TCK-1001 · 3 calls   confidential   $0.0342   11.6s
       ├─ 1  nemotron-nano-8b  cheap  $0.0097  2716 ms
       ├─ 2  nemotron-nano-8b  cheap  $0.0134  3327 ms
       └─ 3  nemotron-nano-8b  cheap  $0.0111  5271 ms
     direct-qa                        public         $0.0021  0.9s   (no session, flat)
  ```

**Scope guard:** captures/labels/displays a correlation id (in scope). No workflow
*execution* moves into RouteForge — the client still orchestrates (pure-router boundary,
CLAUDE.md rule #4). Cross-ref B3 (a shared session store is needed before this survives
multi-replica API scale-out, same as history/bus). Own SDD task; plan first.
