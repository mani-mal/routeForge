# Architecture Review — Serving Plane (Steps 1–8)

**Scope:** `doc/architecture/llm orchestration loop.drawio` (Serving Plane, steps 1–8),
`CLAUDE.md`, the 8 skills in `.claude/skills/`, `BUILD_PLAN.md`, and the four config
files. Reviewed 2026-07-20.

**Verdict:** This is a well-above-average architecture package. The plane separation,
per-call routing, fail-closed defaults, post-compression pricing, and dual savings
attribution are all correct and consistently enforced across the docs. The skills are
genuinely useful — they carry constraints, not boilerplate. The problems are almost
all **consistency drift between files** (some of which will cause real bugs, one of
which breaks routing entirely) and a handful of **underspecified seams** in the
architecture itself. Details below, worst first.

---

## 1. Defects that will cause real bugs

### 1.1 Provider name mismatch: `onprem` vs `on_prem` — breaks routing

- `config/serving.yaml` → `providers.onprem`
- `config/routing.yaml` → `model_pool[].provider: on_prem`
- `config/pricing.yaml` → `provider: on_prem`

The privacy gate computes `allowed_providers` (a `frozenset[str]`) from
`serving.yaml`, and the router filters the pool with set membership
(`candidate.provider not in ctx.allowed_providers → -inf`). With these two spellings,
**every on-prem candidate is filtered out of every request** — or, if the gate uses
`routing.yaml` names, cloud filtering breaks instead. This is precisely the silent
failure mode CLAUDE.md's pitfalls section warns about, already present in the repo
before a line of code exists.

**Fix:** pick one spelling (`onprem`), and add a startup consistency check (see §4.1).

### 1.2 The "strong" tier doesn't exist in the pool, but the rules require it

- `routing-and-gateway` skill, hard rule 1: *"`risk_class: high` routes to the strong
  tier regardless of cost score."*
- `serving.yaml`: `azure: {tiers: [strong]}`
- `routing.yaml` `model_pool`: tiers present are `cheap`, `mid`, `mid`, `frontier` —
  **no `strong`**, and `azure-gpt-mid` is tier `mid`, contradicting `serving.yaml`.
- `routing.yaml` `risk_floors: {high: mid}` — contradicting the skill's "high → strong".

So a high-risk request either targets a tier with zero members, or floors at `mid`
depending on which file the implementer trusts. Additionally, for the `hospital`
tenant (`on_prem_only`) a high-risk request can *never* reach `strong`/`frontier`
(both cloud) — the interaction between the high-risk floor and `onprem_only` is
undefined everywhere. That combination (high-risk + PHI) is the demo's most
important persona.

**Fix:** one canonical tier list; define precedence explicitly: *residency beats
risk floor* — an `onprem_only` high-risk request goes to the best on-prem tier and
sets `require_verification` (step 9), never to cloud. Write that sentence into
CLAUDE.md; it is currently only implied.

### 1.3 Three disagreeing sources for budgets and thresholds

| Value | serving.yaml | routing.yaml | policy.yaml |
|---|---|---|---|
| agent max_steps | 6 | 5 | — |
| agent max_spend_usd | 0.50 | 0.05 | — |
| default max_spend_usd | — | — | 0.05 |
| confidence threshold | 0.55 | 0.60 | — |
| escalation cap | `max_escalations: 1` | (implied by threshold) | — |

Whichever file the implementer reads first wins, and tests will pass against one and
fail against the other. `serving.yaml` also duplicates the sensitivity/risk bypass
flags that `policy.yaml` owns conceptually.

**Fix:** one owner per concern — `serving.yaml`: infrastructure switches only;
`policy.yaml`: budgets, sensitivity, residency; `routing.yaml`: scoring, thresholds,
pool, escalation. Delete the duplicates rather than syncing them.

### 1.4 Sensitivity enum: three variants

- privacy-gate skill: `none | low | high | restricted`
- policy.yaml: `restricted | confidential | public`
- serving.yaml: `default_sensitivity: none`

The gate emits one enum; the compression bypass rules (`restricted_sensitivity`),
the policy rules, and the UI all consume it. Three names for the scale means at
least one consumer silently never matches.

**Fix:** one enum, defined once (suggest `public | internal | confidential |
restricted`), referenced by name from skills instead of restated.

---

## 2. Architecture gaps (steps 1–8 as designed)

### 2.1 The redaction map has no return path — responses will contain `<SSN_1>`

The gate replaces entities with stable placeholders and keeps the map internal
(`redaction_map_id`, "map itself never leaves the gate" — correct). But nothing in
steps 9–10, the diagram, or any skill describes **re-inserting values into the
response** before it returns to the caller. As designed, a user who asks "verify
this SSN: 123-…" gets an answer talking about `<SSN_1>`. For the demo this may even
be desirable (it *shows* redaction) — but that's a decision, not an accident, and
the map's lifecycle (storage, TTL, per-request scope, never exported) is unspecified
even though it is a second PII store exactly like the CCR cache.

**Fix:** decide and document: (a) demo mode returns placeholders deliberately, or
(b) step 10 detokenizes inside the gate's trust boundary. Either way, give the
redaction map the same treatment CCR got: residency, TTL, never-in-telemetry, and a
test asserting it.

### 2.2 Circular dependency between step 5 (compression) and step 6 (routing)

Headroom compresses *for a target model* (`compress(messages, model=target_model)` —
tokenizers differ per model), but the target model isn't known until the router runs
— and the router must score **post-compression** token counts. As drawn, 5 → 6 is a
straight arrow; in code it's a cycle.

**Fix (pragmatic, demo-grade):** compress once against a canonical tokenizer
(cl100k or the cheap tier's), treat router cost estimates as estimates, record
`actual_cost_usd` from the response (`DecisionRecord` already has the field). Note
in the routing skill that per-model recompression is a known approximation. Do not
try to iterate compression↔routing; unbounded loops for marginal token accuracy.

### 2.3 Two overlapping config axes for step 8: `inference_backend` vs `serving_paths`

`inference_backend: mock|vllm|dynamo|none` (singular, top-level) and
`serving_paths: {dynamo_tensorrt, vllm_semantic, managed_cloud}` (plural, all
disabled by default) describe the same thing at different granularities.
`inference_backend: vllm` with `serving_paths.vllm_semantic.enabled: false` is
representable and meaningless. The diagram shows three concurrent serving paths;
the config implies one backend at a time; neither shows `mock` — meaning **the demo
runs 100% on a path the architecture diagram doesn't contain**.

**Fix:** make `serving_paths` the single axis (each path enabled + config, `mock` as
a first-class path), derive nothing from a second knob. Add mock to the diagram as a
fourth serving-path box labeled "demo/dev" — it makes the diagram honest and gives
the customer a natural "what's real vs simulated" answer.

### 2.4 Confidence-based escalation has no defined signal

`confidence_threshold: 0.55` / `0.60` exists in config, the skill says "below
threshold, escalate one tier", the `low_confidence` persona triggers it — but no
document says **what confidence is**: logprobs? a self-grading call (which is itself
an LLM call that must re-enter steps 5–6)? a mock-only scalar? This is the least
implementable part of the whole design as written.

**Fix:** for the prototype, define it as backend-reported (mock emits a seeded
confidence; real backends use mean token logprob when available, else 1.0), and say
so in `routing-and-gateway`. If a judge model is ever used instead, state that the
judge call goes through the gateway like any other call.

### 2.5 Step 4's ambiguity classifier is an unrouted LLM call

"Rules first; small approved classifier when ambiguous" — that classifier is an LLM
call that happens *before* step 6, so nothing routes it and nothing prices it. It's
post-gate (sanitized), so not a privacy hole, but it can silently violate
`onprem_only` if it defaults to a cloud model, and its cost is invisible to the
accumulator the demo depends on.

**Fix:** one sentence in CLAUDE.md: *the task-mode classifier is an LLM call like
any other — it goes through steps 5–7, respects `allowed_providers`, and emits a
DecisionRecord.* For the prototype, rules-only is enough; keep the classifier off.

### 2.6 Streaming is unresolved between step 1 and step 9

The API is OpenAI-compatible (`/v1/chat/completions`), where clients expect
`stream: true`. Step 9 runs leakage checks and structured-output validation on the
*complete* response — incompatible with token streaming unless you buffer (breaks
streaming) or scan incrementally (partial-leak window). Nothing in the docs
addresses this.

**Fix:** for the demo, reject `stream: true` with a clear error (decision streaming
to the UI over SSE is a separate channel and already specified). Note it as an open
production question in CLAUDE.md.

### 2.7 CCR retrieval into cloud prompts

The residency rules for the CCR *store* are excellent (local PVC, TTL, never in
telemetry). One flow is unstated: when compression summarizes content and a
**cloud** model requests the original via CCR, the original (sanitized, but full)
is inserted into a cloud prompt. That's within policy for `private_cloud_ok`
tenants, but it should be an explicit statement plus a counter
(`ccr_retrievals_to_cloud`) — the customer *will* ask "so the originals never go to
the cloud?" and today the honest answer is "mostly."

### 2.8 The decision stream is unauthenticated telemetry

The UI's SSE/WebSocket carries every `DecisionRecord`: tenants, entity types, costs,
routing reasons. Fine on a demo laptop; in the GreenLake deploy it's a multi-tenant
metadata leak. One line in `greenlake-deploy` ("decision stream requires auth and is
tenant-scoped outside demo mode") is enough for now.

---

## 3. CLAUDE.md and skills — fitness for building steps 1–8

### 3.1 Coverage map

| Step | Module | Skill | Coverage |
|---|---|---|---|
| 1 | `app/api/routes.py` | — | Thin, acceptable (FastAPI boilerplate) |
| 2–3 | `app/policy/` | `privacy-gate` | **Strong** — best skill in the set |
| 4 | `app/routing/task_mode.py` | none (NAT skill covers runtime, not the mode decision) | **Gap** — see §2.5 |
| — | `app/agent/runtime.py` | `nemo-agent-toolkit` | Strong; the "gateway as NAT's model endpoint" note is the key insight |
| 5 | `app/compression/` | `headroom-compression` | **Strong** — CCR residency and bypass rules are exactly what a builder needs |
| 6 | `app/routing/router.py` | `routing-and-gateway` | Good, but contradicts `routing.yaml` (§1.2, §1.3) |
| 7 | `app/gateway/` | `routing-and-gateway` | Good; fallback-rechecks-policy rule is the right emphasis |
| 8 | `app/gateway/backends/` | `inference-backends` (+ NVIDIA Dynamo skills) | Good; correctly delegates Dynamo to vendor skills |
| 9 | `app/output/verify.py` | **none** | **Biggest skill gap** — in scope per CLAUDE.md, no skill, ~2 lines of guidance total |
| cross | UI / traffic / deploy | `demo-ui`, `traffic-and-tests`, `greenlake-deploy` | Strong; the ten critical assertions are the best artifact in the repo |

**Recommendation:** add an `output-controls` skill (or a section in `privacy-gate`)
covering step 9: what a leakage check is (re-run Presidio on the completion),
what `require_verification: true` from `policy.yaml` actually triggers (that field
is currently consumed by nothing), escalation semantics, sampling rate, and the
streaming decision from §2.6. Fold step 4 guidance into `routing-and-gateway`.

### 3.2 Numbering and scope drift (cosmetic but confusing)

- The diagram has **10** serving-plane steps; CLAUDE.md says "the nine steps" then
  lists 10 rows; the README says "steps 1–9"; you asked about "steps 1–8". Pick one
  canonical count (the diagram's 1–10) and align the prose.
- `routing-and-gateway` titles itself "Steps 5–7" but the README's table says it
  covers "Steps 6–8", and both it and `headroom-compression` claim step 5. Give each
  skill exactly one step range matching the diagram.
- The `npx skills add` block lists 2 Dynamo skills in CLAUDE.md, 3 in README, 4 in
  `inference-backends`. Keep the full list in `inference-backends` only; have the
  others point at it.
- The folder is `doc/`; conversations and conventions say "docs". Rename to `docs/`
  before code cements the path.

### 3.3 Structural issue: facts are restated instead of referenced

The tier table, budgets, thresholds, and enum values each appear in 2–4 places
(CLAUDE.md, skills, configs). Every drift found in §1 is this pattern failing.
Skills should state *rules and invariants* ("router must price `tokens_after`") and
**reference** config for *values* ("weights: see `config/routing.yaml`") — never
restate numbers. CLAUDE.md mostly gets this right; the routing skill's inline YAML
pool example is the main offender (it shows a 4-tier pool that doesn't match the
real one — delete it or replace with a pointer).

### 3.4 What's genuinely good (keep, don't dilute)

- The seven hard rules are repeated with identical meaning everywhere — that's the
  redundancy you *want*.
- The ten critical assertions in `traffic-and-tests` encode the demo's honesty as
  tests. This is the strongest artifact here; wire them into CI on day one.
- The cut list with "never cut" items, the fail-closed defaults, redaction-on-egress
  (not just intake), CCR-as-second-PII-copy framing, the mock-first demo discipline,
  and the air-gapped Headroom/Presidio fallbacks are all things most teams discover
  the hard way.
- Per-call (not per-request) routing with visible hop loops is the differentiating
  claim, and every relevant file reinforces it consistently.

---

## 4. Recommended actions, in order

1. **Config lint at startup (do this first, in Phase 1).** A Pydantic settings layer
   that loads all four YAMLs and cross-validates: provider names identical across
   files (§1.1), every tier referenced by `risk_floors`/`providers` exists in the
   pool (§1.2), no duplicated keys across files (§1.3), one sensitivity enum (§1.4).
   Exit non-zero with a named violation. This turns every drift class above into a
   boot failure instead of a silent demo lie, and keeps working as configs evolve.
2. **Resolve §1.1–§1.4 now** — they're 30 minutes of edits while the repo is still
   docs-only, and every one becomes a debugging session after Phase 3.
3. **Write the missing sentences** for: residency-beats-risk-floor (§1.2),
   redaction-map lifecycle (§2.1), canonical-tokenizer compression (§2.2),
   classifier-goes-through-the-gateway (§2.5), no-streaming-in-demo (§2.6).
   Five sentences total; each closes a real ambiguity.
4. **Collapse `inference_backend`/`serving_paths` into one axis** and add `mock` to
   the diagram (§2.3).
5. **Add the `output-controls` skill** for step 9 and re-scope the skill step ranges
   (§3.1, §3.2).
6. **Define the confidence signal** in `routing-and-gateway` (§2.4).
7. Later / production-track: tenant-scoped decision stream (§2.8), CCR-to-cloud
   retrieval counter (§2.7), gate latency budget (Presidio cold start is on the
   critical path of every request; the `regex` fallback already exists — add a
   startup warm-up call so the first demo request isn't the slow one).

## 5. Overall assessment

The architecture is sound and the demo focus is disciplined — the two-plane split,
the ordering invariants (gate → compression → router), and the honesty mechanisms
(separate savings levers, ten assertions, never-cut list) are exactly right. The
package is ready to build from **after** the §1 fixes: today, an implementer
following the files literally would ship a router that filters out every on-prem
model and a high-risk path that targets a nonexistent tier. Everything else in this
review is refinement; §1 plus the startup config lint is the difference between the
demo working and the demo quietly lying.
