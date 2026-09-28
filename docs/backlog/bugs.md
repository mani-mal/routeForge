# Bugs backlog — work later

Running list of known defects/observations to pick up later. Newest first.
**This is now the single backlog.** [`archive/ui-open-items.md`](../archive/ui-open-items.md) and
[`archive/eval-logging-backlog.md`](../archive/eval-logging-backlog.md) have been folded in and archived to
`docs/archive/` — every item in both was verified against the code (not
assumed from its status heading) during the 2026-09-10 docs sweep, and every
one was confirmed closed; nothing was migrated because nothing was live. See
the audit trail in that sweep's task report.

> **Agent removal 2026-09-17.** RouteForge's internal NeMo Agent Toolkit runtime and the
> step-4 `multi_step` branch were removed; step 4 now always decides `direct` and agent
> loops are client-driven. Entries below that are framed around "the multi-step path", an
> "agent hop", or `agent_budgets` describe a path that no longer exists — they are kept as
> written, for history. Where that changes an item's *current* standing it is noted inline
> on the item.

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
> The same sweep also folded [`archive/ui-open-items.md`](../archive/ui-open-items.md) and
> [`archive/eval-logging-backlog.md`](../archive/eval-logging-backlog.md) into this file (see the note at the top) —
> every item in both verified closed, nothing migrated.

---

> **Update 2026-09-11 (final whole-branch review, PR #39).** One code/spec gap
> found while reviewing the whole `docs/reclassification` branch — recorded
> below as **B31**. Not fixed here; this review only writes the backlog entry.

---

> **Update 2026-09-14 (DGX Spark onboarding).** Onboarding a keyed vLLM endpoint
> from the console failed twice for reasons the UI didn't explain. One was
> **B19** (fixed long ago on a branch that was never merged — now landed; see
> B19). The other is a UX gap, recorded below as **B32**. Setting up a per-app
> Langfuse project on the same box then took several saves, recorded as **B33**.

---

> **Update 2026-09-15 (onboarding conformance, Tasks 1–10).** Every new app
> deployment hit a different policy failure because tests sent tiny synthetic
> requests and real clients sent complex shapes (20+ tools, streaming, content
> parts, null content, 25K tokens). The admin API also accepted impossible
> configurations and rejected them opaquely, so every incident took several
> rounds. Resolved with a conformance matrix (`tests/onboarding/`), forwarded
> generation params, budget estimation at `max_tokens`, per-app `max_cost_usd_per_request`,
> tier fallback when over budget, and actionable rejection messages — recorded
> below as **B34**.

---

> **Update 2026-09-19 (P2a routing signal measurement).** **B30** is resolved —
> the inert `capacity` term is now a measured `availability` signal, latency is
> measured too, and every scoring term reports its provenance on `sp.scoring`.
> Nine findings surfaced during implementation and review, and were ruled out of
> that slice's scope rather than patched into it; they are recorded below as
> **B38**–**B46**. None is a regression: B38, B39, B41 and B42 bound how much the
> new signal can know, B40 and B46 are test-coverage gaps, B43 is a deferred
> API-surface decision, B44 is a comment, and **B45** is the one product question
> the slice deliberately refused to answer by guessing. All nine are named in
> [`docs/research/routing-signal-provenance.md`](../research/routing-signal-provenance.md) §6.
>
> **One correction is worth reading on its own.** The design spec and the first
> draft of the research artifact both asserted that *"a dead endpoint ranks
> last."* That is **false** — availability is an additive term bounded by
> `weights.availability`, so a dead endpoint loses at most `0.2` and can still
> rank **first**. Both documents now carry the correction inline rather than a
> silent edit, and B45 below is the measurement question underneath it.

---

> **Update 2026-09-21 (signal hardening).** A follow-up slice closes **B38**,
> **B39**, **B40** and **B41** — each marked `✅ RESOLVED` below with the
> resolving commit and a short "how". Boot seeding no longer aggregates
> `usage_records`; it replays a new per-attempt log (`endpoint_observations`)
> at each row's true age, which is also what makes B38/B39 resolvable at all
> (a real `error` row now exists to seed from, and the replayed quantity is
> backend-only latency, never a whole-trace sum). One new low-severity item is
> recorded as **B47**. See
> [`docs/research/routing-signal-provenance.md`](../research/routing-signal-provenance.md)
> §3A for the property that makes replay safe, and
> [`docs/reference/routing-observability.md`](../reference/routing-observability.md) §4
> for the operator-facing contract.

## B59 — FEATURE: per-app tokens-per-minute / concurrency limits, and a limiter shared across replicas  🔴 OPEN

**Severity:** medium (fairness on a shared model; no effect on privacy or routing correctness).
**Found:** 2026-09-27, evaluating RouteForge as the per-user key gateway for a shared
8-GPU GLM 5.3 vLLM deployment (see `docs/guides/per-user-access-to-a-shared-model.md`).

**What's missing.**
- The only per-app rate lever is `max_rpm`. On a shared model the scarce resource is
  KV cache, which is measured in tokens. A coding agent's request routinely carries 100K+
  prompt tokens, so an RPM cap does not stop one user's agent loop from monopolising the
  shared queue. `max_spend_usd_per_day` and `max_cost_usd_per_request` (with nominal
  prices) are a coarse daily substitute, not per-minute fairness.
- `RateLimiter` (`app/apps/policy.py`) is in-process and in-memory. Its docstring puts
  multi-node deployment "out of scope", but Helm supports `replicaCount.api > 1`. With N
  replicas, the effective limit is `N × max_rpm`, and a pod restart resets the window.

**Wanted.** Per-app `max_tpm` (prompt + completion, charged when the request is admitted
from the estimated prompt tokens and reconciled on completion) and `max_concurrent`
(in-flight requests). Both should be backed by the DB, or by a shared store that already
exists, so that limits hold across replicas. Reply 429 with `Retry-After`, in the OpenAI
error envelope (B49). LiteLLM Proxy's per-key TPM/RPM/parallel limits are the reference
behaviour operators will compare against.

## B58 — FEATURE: live token streaming for tool-bearing requests (coding agents)  🔴 OPEN

**Severity:** medium (UX; answers are correct, but nothing appears until generation finishes).
**Found:** 2026-09-27, same evaluation as B59.

**What happens.** Streaming passes backend tokens through live only when the request is
not `onprem_only`, not `require_verification`, **and not a tool conversation**.
`app/pipeline.py::_is_tool_conversation` is true whenever the request offers `tools`.
Tool-bearing requests fall back to the buffered `pipeline.run` and then emit the finished
answer as SSE frames (PR #41, which fixed lost `tool_calls`). OpenCode, Claude-Code-style
clients and most IDE agents send `tools` on **every** request. For them, every turn is
buffered, whatever the app's `stream_mode`. Keepalive frames prevent client timeouts, but
on a large model, long answers show nothing for tens of seconds.

**Wanted.** Stream text deltas live while assembling `tool_calls` deltas, passing them
through as OpenAI streaming `tool_calls` chunks (index/id/function.arguments fragments).
Tool-call arguments must still be classified and redacted per PR #49 whenever
`model_input`/`client_output` is `redact`. That argues for streaming live only under
`client_output: raw` (passthrough) at first, and holding back the `tool_calls` portion
under `redact`. The step-9 floor (`onprem_only` / `require_verification` → verified) is
a separate question. For an `onprem_only` + `passthrough` app, whether that floor should
still apply is an owner decision, since rule 2a says posture, not placement, decides
protection.

## B57 — RouteForge configures no logging, so every `serving.*` INFO record is dropped in deployment  ✅ RESOLVED (2026-09-26)

**Severity:** medium (observability; no effect on routing or privacy).
**Found:** 2026-09-25, P3b live verification on the Spark.

**What happens.** Nothing in `app/` or the deploy files calls `logging.basicConfig`,
`dictConfig` or `setLevel`. The root logger therefore stays at Python's default WARNING level
with no handler. Every `logging.getLogger("serving.*").info(...)` record is dropped:
- the P3b `no_eligible_model` structured line (`app/api/routes.py::_log_no_eligible`);
- the client-disconnect line (`_record_cancelled`);
- the other INFO lines in the same file.

WARNING and above reach stderr only through Python's last-resort handler, as a bare message
with no logger name, level, timestamp or JSON.

**Evidence.** On the Spark, the api container logged zero `serving.*` lines in 24 hours. The P3b
check sent two `no_eligible` requests: both 422 bodies and both outcome rows carried the core,
but no log line appeared.

**Effect.** CLAUDE.md promises "structured JSON logs with a `request_id`". That promise does not
hold in deployment. Tests pass because pytest's `caplog` captures records whatever the handler
configuration.

**Fix (proposed).** Configure logging once at app start, in `create_app`:
- add a stderr handler for the `serving` logger namespace;
- set its level from an env var (e.g. `SP_LOG_LEVEL`, default `INFO`);
- leave the root and third-party loggers at WARNING, because litellm is very chatty at INFO.

Add a test that a `serving.api` INFO record reaches the configured handler.

**Resolution.** `create_app()` now calls `app/obs/log_config.py::configure_logging` as its
first line. It does four things:
- attaches one stderr handler to the `serving` logger, one line per record:
  `<time> <LEVEL> <logger> <message>`. The structured lines already carry JSON in the message.
- sets the `serving` logger's level from `SP_LOG_LEVEL`. The default is `INFO`; an unknown
  value falls back to `INFO` and logs a warning.
- is idempotent. The handler is named, so repeated `create_app()` calls never stack handlers.
- leaves the root logger and every third-party logger (litellm) untouched.

The handler resolves `sys.stderr` at emit time, like `logging.lastResort`, so it never writes
to a stream a test runner has since closed.

`propagate` stays `True`, and this is deliberate. pytest's `caplog` listens on the root logger,
so `propagate = False` would blind every existing caplog test. A plain `uvicorn app.main:app`
process has no root handler, so nothing prints twice: uvicorn configures only its own
`uvicorn.*` loggers. Verified locally: each startup and prune INFO line appears once. A
deployment that adds its own root handler would see serving lines once per handler.

Docs: `SP_LOG_LEVEL` is added to `.env.example`, `deploy/spark/.env.example` and both compose
files (`${SP_LOG_LEVEL:-INFO}`). `docs/reference/conflicting-core.md` §1 now says the
`no_eligible_model` line reaches stderr.

Tests: `tests/test_logging_config.py`. These tests read what the handler wrote to stderr
(`capsys`), not caplog:
- a `serving.api` INFO record is printed;
- two `create_app()` calls leave exactly one handler;
- the root and litellm loggers are untouched;
- `SP_LOG_LEVEL=WARNING` suppresses INFO;
- an invalid level falls back to INFO with a warning;
- the P3b `no_eligible_model` line is printed end to end through `TestClient` for an app with
  an empty feasible set.

**Deploy.** Code only: no schema change and no `ALTER`. After the next image roll,
`docker compose logs api` should show `INFO serving.*` lines. Set `SP_LOG_LEVEL=WARNING` to
quieten them.

## B56 — first-fail `ctx.allowed_models` can admit an out-of-scope model if an endpoint declaration changes between the gate and the router  ✅ RESOLVED (2026-09-26)

**Severity:** low (a timing window measured in milliseconds; placement is not affected).
**Found:** 2026-09-25, P3b Task 1 review.

**What happens.** `build_context` builds `ctx.allowed_models` from each candidate's
**first-fail** code. It removes names whose first failure is `residency` or `app_scope`. A
model that is outside the app's `allowed_models` / `allowed_tiers` but was first refused at
stage 2 (region, retention, raw-sensitive) therefore stays in the set.

**Effect.** Suppose an operator changes that endpoint's data-handling declaration so it now
passes stage 2, and the change lands in the window between the gate building the context and
the router running `feasible_set`. The router's `_ctx_scope` check (`c.name in
ctx.allowed_models`) then admits the model although the app's scope excludes it.
- Placement is re-checked at the router, so rule 3 (`onprem_only`) and rule 8 are not
  affected.
- Only non-security app scope can be bypassed, and only inside that window.
- The same window exists for a registry disable or an `allowed_app_ids` change, whose
  `model_scope` code is also not subtracted.

**Fix (proposed).** Since P3b the context carries the app's own compiled
`app_allowed_tiers` / `app_allowed_models`. `_ctx_scope` can additionally require
`Scope.from_context(ctx)`. That can only narrow the set, and it is a no-op when no race
happens. Add a regression test that mutates the declaration after `build_context` and asserts
the router refuses the model.

**Resolution.** `app/policy/feasible.py::_ctx_scope` now requires both the first-fail set
`ctx.allowed_models` **and** the app's exact scope, `Scope.from_context(ctx)` (the compiled
`app_allowed_tiers` / `app_allowed_models` the context has carried since P3b; `None` = no
constraint). The extra check can only narrow. Without a race it is a no-op: every name the
exact scope refuses was already refused by placement, a data-handling declaration or app
scope. It lives inside the app-scope stage, so `candidate_rejection` codes, reasons and
order are unchanged: a candidate it refuses reads `app_scope` / "not permitted for
application", as before. Contexts with no app lists (internal paths, hand-built test
contexts) are unaffected. The same change closes the window for the `allowed_models` /
`allowed_tiers` part of the registry-disable and `allowed_app_ids` variants noted above.
Those two gates are also re-checked at the router by their own stage (`model_scope`).

Tests: `tests/test_b56_scope_race.py`. This file builds a real registry and a real
`build_context` for an app with `allowed_models={A}` and `allowed_regions={eu}`. Endpoint B
declares `us` when the context is built, so it is region-refused and stays in
`ctx.allowed_models`. B is then re-declared `eu` and the registry refreshed before
`feasible_set` / `Router.select` run on the same context. B is now refused as `app_scope`
and A is chosen. Before the fix B was eligible and, being cheaper, would have been chosen
(the test failed RED on exactly that assertion). The Hypothesis agreement property in
`tests/test_policy_violations.py` passes unchanged.

**Deploy.** Code only: no schema change, no `ALTER`, no config change.

## B55 — Outcome rows don't record the pinned model or `max_tokens`  ✅ RESOLVED (2026-09-24)

**Severity:** low (fidelity of an offline tool only; no effect on production routing).
**Found:** 2026-09-24, P3a policy-simulator build. Confirmed by the first real Spark run:
baseline agreement 0.0206 (190/194 rows recorded `qwen3.8-27b`, which Qwen Code pins, and
replayed unpinned to `nemotron-3.5-lightning`).

`routing_outcomes` records what the router decided and what was served, but not two things
the request itself carried: a client's pinned `model` (when the app is allowed to pin one)
and its `max_tokens` (or `max_completion_tokens`). Neither is a routing-decision output, so
neither had a reason to be captured before there was a consumer that needed to replay a
request from its outcome row alone.

**Effect.** The P3a policy simulator (`app/eval/simulate.py`, `docs/reference/policy-simulator.md`
§6) replays each outcome row's first routing decision through the real eligibility and
routing code. With no pinned model recorded, every row replays as an ordinary routed
decision — a pinned request that would have hit `model_not_found` (or simply bypassed the
router entirely) is indistinguishable from one that was never pinned. With no `max_tokens`
recorded, every row prices its completion at the deployment's default output-token estimate
(`expected_output_tokens(settings, None)`) rather than the request's real cap. Both lower
the simulator's baseline agreement (`docs/reference/policy-simulator.md` §5) for traffic
that pins models or sets a non-default `max_tokens`, and both are pure replay-fidelity
losses — they change nothing about what was actually served or how it was billed.

**Fix.** Two nullable `routing_outcomes` columns, `requested_model` and `max_tokens`,
populated from the request at the same point the other decision fields are written. Needs
an `ALTER` on every deployed database that already has the table (per
`docs/reference/routing-outcomes.md` §1, `create_all` never adds a column to an existing
table).

**Resolution.** `RoutingOutcome` has `requested_model` (`VARCHAR(100)`, null = unpinned /
`model: "auto"`) and `max_tokens` (`INTEGER`, the client's `max_completion_tokens` else
`max_tokens`, read by `app/routing/router.py::client_output_cap`, the same reading the router
prices at). Both are set in `outcome_context` on the `run` and `run_stream` paths, so every
status that writes a row carries them, and the export includes them. The simulator
(`app/eval/simulate.py::simulate_row`) re-applies the pin to the rebuilt context and passes
the cap to `Router.select`; rows without the keys (older exports) replay unpinned.

**Deploy.** Run before rolling the image (against an unmigrated table every outcome write
fails, swallowed, and every read of it -- export, `/v1/feedback` lookup -- errors):

```sql
ALTER TABLE routing_outcomes ADD COLUMN IF NOT EXISTS requested_model VARCHAR(100);
ALTER TABLE routing_outcomes ADD COLUMN IF NOT EXISTS max_tokens INTEGER;
```

Tests: `tests/test_outcome_pin.py` (real HTTP: ok/backend_failed/no_eligible/stream rows,
export), `tests/eval/test_simulate.py` (pinned replay, removal → `newly_rejected`, old rows,
malformed fields, `max_tokens` reaches the budget check),
`tests/eval/test_policy_simulate_cli.py::test_live_round_trip_pinned_baseline_is_perfect`
(0.25 agreement before the simulator fix, 1.0 after).

## B54 — onboarded on-prem models scored at the 0.5 quality floor regardless of tier or published quality  ✅ RESOLVED (2026-09-24)

**Severity:** low–medium (ranking only; never eligibility or placement).
**Found:** 2026-09-24, in review of P2b-3 Task 2 (offline quality calibration).

`QualityResolver` built its tier and published-quality maps once, when
`create_app` constructed it. At that point the `ModelRegistry` had not loaded its
onboarded rows yet, because it loads them lazily on the first request. So the maps
held only the static YAML pool. Every onboarded on-prem model fell through to the
flat `_TIER_DEFAULT_FLOOR` (0.5) and never got its tier's `tier_quality`.

**Effect.** An onboarded `mid` model scored quality 0.5, while a YAML `mid` peer (for
example `azure-gpt-mid`) scored 0.75. That is 0.25 × `weights.quality` lost in the
quality term purely for being onboarded. `frontier` lost 0.5. The published `quality`
set at onboarding (`onboarded_models.quality`) was ignored in the same way, **for every
tier, `cheap` included**: an onboarded cheap model with a published quality of 0.66 still
scored 0.5. Only an onboarded cheap model with no published quality was unaffected
(`tier_quality.cheap == 0.5` equals the floor).

**Fix.** `ModelRegistry.generation` increments on every successful `refresh`. The
resolver rebuilds its maps from `registry.effective_pool()` whenever the generation
has moved, so models present at first load and models onboarded later both resolve
to their own published quality or tier (`app/routing/quality.py::_sync_pool`). As a
result, with zero `model_quality` rows, **any onboarded model with a non-null published
`quality` now scores it (any tier, cheap included)**, and an onboarded mid/frontier
model without one scores its tier default instead of 0.5. That is the one intended
deviation from pre-P2b-3 identity (spec §7).

**Deploy check.** Every row this returns is scored differently after deploy:

```sql
SELECT slug, tier, quality FROM onboarded_models WHERE tier <> 'cheap' OR quality IS NOT NULL;
```

Tests: `tests/test_quality_serving.py`
(`test_b54_onboarded_mid_model_scores_its_tier_default_over_http`,
`test_model_onboarded_after_boot_resolves_to_its_tier_default`),
`tests/test_quality_final_fixes.py::test_b54_onboarded_cheap_model_published_quality_is_scored`.

## B53 — streamed responses never carried the terminal `usage` frame  ✅ RESOLVED (2026-09-23)

**Severity:** high for agent clients (context overflow), otherwise low.
**Found:** 2026-09-23, from a Qwen Code session on the DGX Spark (diagnosed in a
separate session).

`/v1/chat/completions` returned `usage` on the non-streamed path, but no stream mode
ever emitted OpenAI's terminal usage chunk, even when the client sent
`stream_options: {"include_usage": true}`. The counts existed internally, because
metering used them, but `openai_chunk` had no way to serialize them.

**Impact.** Qwen Code drives its context accounting from reported usage. With none,
it fell back to a character-based estimate that undercounted token-dense JSON and
code history by at least 1.58×. In one measured case a real 253,953-token prompt
never crossed its 160,563-token auto-compaction trigger. Compaction fired too late,
the history outgrew the 262,144-token window, and every later turn failed with
`400 ContextWindowExceeded`. The session was then unrecoverable, because compaction
itself had to send the oversized history.

**Fix.** A new `app/api/streaming.py::usage_chunk` builds the frame, and
`app/api/routes.py::_stream_usage` reads the client's `include_usage`. The frame
(`choices: []`, the same `created`/`id`/`model` as the rest of the stream, `usage`
equal to the non-stream body's) is emitted after the finish frame and before
`[DONE]` in all four emit sites:
- `verified_then_stream`, both plain and keepalive;
- `stream_unverified`;
- `incremental_verify`.

The frame appears **only** when the client asked for it, so without it the stream is
byte-identical. `stream_options` is never forwarded upstream. A disconnect or backend
failure emits no frame, and metering in the `finally` blocks is untouched.
Tests: `tests/test_stream_include_usage.py`.

## B52 — `feasible_set` trusts `ctx.allowed_providers` even when `ctx.onprem_only` is set  ✅ RESOLVED (2026-09-23)

**Severity:** low, defence in depth only. It is unreachable through the real gate today.
**Found:** 2026-09-23, final review of the endpoint circuit breaker. A reviewer probe hand-built an
inconsistent `ExecutionPolicyContext`. **Resolved 2026-09-23** on `fix/b52-and-outcome-followups`.

When `feasible_set` (`app/policy/feasible.py`) is given a context, it takes placement **only** from
`ctx.allowed_providers`. `ctx.onprem_only=True` changes nothing but the rejection label
(`residency`). So a context with `onprem_only=True` whose `allowed_providers` includes a cloud
provider **routes to cloud**. The real gate never builds such a context, because `build_context`
narrows `allowed_providers` to `{onprem}` under `onprem_only`. That leaves rule 3's "cloud is
unreachable by construction" resting on one function, `build_context`, alone.

**Fix direction:** in `feasible_set`, intersect the providers with `{onprem}` whenever the resolved
`onprem_only` is true, so the invariant also holds at the single eligibility seam. Add a
Hypothesis property: for any context with `onprem_only`, no eligible candidate has a
provider other than `onprem`.

**Resolution (2026-09-23):** `feasible_set` now intersects the providers with `{onprem}`
(`ONPREM_ONLY_PROVIDERS`) whenever the resolved `onprem_only` is true — the context's, the explicit
argument, or the policy's residency — before stage 1, so a cloud candidate is rejected as
`residency` even from an inconsistent context. Every context the real gate builds was already
narrowed, so no routing decision changes. Pinned by
`tests/test_feasible_set.py::test_b52_*`, including a Hypothesis property over random
contexts/providers/exclusions: under `onprem_only` every eligible candidate is `onprem`.

## B51 — unmeasured models look ~80× faster than measured reasoning models, so every restart or onboarding pays a quality-blind warm-up  ✅ RESOLVED (2026-09-23)

**Severity:** medium (routing quality; no privacy or eligibility impact — a signal can only reorder)
**Found:** 2026-09-22, first live FeasibleRouteBench run on the Spark
([`docs/research/feasible-route-bench.md`](../research/feasible-route-bench.md) §10).
**Resolved 2026-09-23** on `fix/b51-peer-latency-prior` by a **peer prior**: an unmeasured or
stale model's latency is now the median measured latency of the other eligible candidates in
its tier (provenance `peer_prior`, `app/routing/strategy.py::_with_peer_prior`); the tier
default applies only when no same-tier peer is measured, so an empty store still scores
exactly as before. The other fix direction below — seeding from the onboarding probe — was
rejected: the probe is a `GET /models` reachability check, not a completion, so it measures
nothing about generation latency (the "probe already makes a real call" premise below was
wrong), and even a one-token completion would time TTFT, not the 16–18 s of reasoning that
caused this. `max_latency_norm` is unchanged (not part of this fix).
**Verified** offline with a Spark-shaped simulation (three free `cheap` on-prem models at
3.6/16.2/18.4 s, Llama failing claim summaries; starter suite, seed 0): `router_online` went from
`{qwen: 12, nemotron: 5, llama: 5}`, 15/22 passed, to `{qwen: 22}`, 22/22; the shipped
`healthcare-sim` report is byte-identical before and after. **Live-verified 2026-09-23**
on the Spark at `5da06a4` (research doc §11.1): `router_online` went from 0.636 (Qwen ×12,
Nemotron ×5, Llama ×5) to **0.955 (Qwen ×22)**, 0 violations. Note what this does *not* do: with every model above the 3 s cap, latency now ties
them all, so there is no latency-driven exploration at all — a new model gets traffic only on
quality or cost. The durable fix is still a measured quality signal (P2b-2 → P2b-3).
**Durable-fix pointer, 2026-09-23:** P2b-2 is built on `feat/routing-outcome-records`. Every
routed request now writes a content-free outcome record, and `/v1/feedback` scores are kept
locally per request and workload
([`docs/reference/routing-outcomes.md`](../reference/routing-outcomes.md)), so "Llama fails
claim summaries" becomes visible once clients tag and score. The router does not read these
records yet; learning from them is P2b-3
([`docs/research/feasible-route-bench.md`](../research/feasible-route-bench.md) §13.4).

**Update 2026-09-24:** P2b-3 is built — an offline, human-approved calibration workflow
(export → `scripts/quality_recommend.py` → review the replay diff → apply via
`/admin/quality/calibrations`) that, once an admin applies a set, does make the router read
a measured quality signal for the first time. See
[`docs/reference/quality-calibration.md`](../reference/quality-calibration.md) and
[`docs/research/feasible-route-bench.md`](../research/feasible-route-bench.md) §14. This
closes the "durable fix" gap **only where labels already exist and an admin has applied a
set** — it is not automatic, it is not exploration, and an unlabelled or unreviewed
workload is exactly as blind as before. Not deployed.

Two P2a signal defaults interact badly with slow reasoning models:

1. **Optimistic unmeasured latency.** Until a model has `signals.min_samples` (5) observations,
   the router scores it at the tier default — `tier_latency_ms.cheap: 200` ms
   (`config/routing.yaml:23`). On the Spark the three `cheap` models actually take 3.6 s
   (Llama), 16.2 s (Qwen) and 18.4 s (Nemotron) mean. A measured model therefore looks
   ~80× slower than any unmeasured peer, and the router shifts traffic to unmeasured models until
   each has 5 samples.
2. **The latency cap flattens them afterwards.** `max_latency_norm: 3.0` caps the latency term at
   3 s. Every model here is slower than that, so once measured they all score the same on latency
   and latency stops distinguishing them at all.

**Observed** (live run, seed 0, 22 cases, one run — a signal, not a quotable result): the
learning arm `router_online` sent **exactly 5** requests to each unmeasured model (Nemotron 5,
Llama 5) before returning to Qwen, and its pass rate fell to 64% versus 95% without
measurements, because some of that warm-up traffic landed on Llama, which failed every claim
summary. The mechanism is **inferred** from the pick counts matching `min_samples` exactly and
from the config values above; it has not been traced call by call.

**Why it matters in production.** Every API restart (the signal store replays the observation log,
but a model with < 5 logged observations is still unmeasured) and every newly onboarded model
costs ~5 requests per model of latency-driven exploration that ignores whether the model can do
the task. There is no measured quality signal to hold it back (quality is a tier default for every
onboarded model).

**Fix directions (owner decision; none built):**
- Seed an unmeasured model's latency from its onboarding probe (the probe already makes a real
  call) instead of the tier default, or make the tier default pessimistic rather than optimistic.
- Revisit `max_latency_norm` for deployments whose models are all multi-second.
- The durable fix is a real quality signal (P2b-2 outcome records → P2b-3 learning), so a fast
  model that fails a workload is not preferred for it. *(P2b-2 built 2026-09-23; see the pointer
  above.)*
- Measure any change with FeasibleRouteBench live before adopting it.

## B50 — `/v1/models` doesn't report each model's context limit, so clients hand-set it  ✅ RESOLVED (2026-09-22, `76b2206`)

**Severity:** low (improvement, not a defect)
**Found:** 2026-09-22, reported from a Qwen Code session on the Spark.
**Resolved 2026-09-22** on `fix/b49-b50-openai-client-compat` (design
`docs/superpowers/specs/2026-09-22-openai-client-compat-design.md` §4; code `4f87a1a` …
`76b2206`). The onboarding probe now captures the endpoint's own `max_model_len` on
onboard and on edit, storing it on `context_window` with a new `context_window_source`
column (`"operator"` \| `"probed"` \| `null`, provenance decided by the stored value, not
by whether the field was sent — an operator value always wins and a probed number is
never guessed). `GET /v1/models` publishes it as `max_model_len`, omitted (never guessed)
when unknown; routeForge still does not enforce it (an oversized request still fails
upstream, B36). Documented in full in
[`docs/reference/api-reference.md`](../reference/api-reference.md#get-v1models) and
[`docs/guides/onboarding-models.md`](../guides/onboarding-models.md#b1-context_window-and-context_window_source).

`GET /v1/models` (`app/api/routes.py:169`) returns only `id`, `object`, `created` and
`owned_by`. A client such as Qwen Code therefore can't learn a model's real context window
and relies on a hand-set number. Hand-set too high, the conversation overflows and the
request fails (B36's 400). Hand-set too low, context is wasted.

RouteForge already has a place for the number: onboarded models carry a `context_window`
column (`app/apps/models.py`), shown in the console, but nothing fills it automatically and
nothing reads it. The upstream server usually knows it: vLLM's own `/v1/models` returns
`max_model_len` (e.g. `131072` for `nemotron-3.5-lightning`, `262144` for `qwen3.8-27b`),
and the onboarding probe already calls that endpoint.

**Fix, roughly.**
1. On onboarding and on every re-probe, read `max_model_len` from the upstream `/v1/models`
   entry matching the slug and store it in `context_window`, unless the operator set a value
   by hand (an explicit value wins; record which source it came from).
2. Return it from RouteForge's `/v1/models` as `max_model_len` (vLLM's field name, which
   clients already understand), omitted when unknown — never a guessed default.
3. Test: a probe against a fake upstream reporting `max_model_len` stores it; an operator
   value is not overwritten; `/v1/models` includes it only when known.

## B49 — non-streamed errors aren't in the OpenAI error format, so clients show only "API error"  ✅ RESOLVED (2026-09-22, `7faeeb3`)

**Severity:** low–medium (improvement; every client loses the actionable message)
**Found:** 2026-09-22, reported from a Qwen Code session on the Spark.
**Resolved 2026-09-22** on `fix/b49-b50-openai-client-compat` (design
`docs/superpowers/specs/2026-09-22-openai-client-compat-design.md` §3; code `de3824b` …
`7faeeb3`). Every `/v1/*` failure — both the plain HTTP path and the SSE error frame —
now renders `{"error": {"message", "type", "param", "code"}}` from one shared mapping
(`app/api/openai_errors.py`), so the two paths can't drift and an OpenAI SDK client's
`except openai.BadRequestError as e: e.code` now reads a real code
(`context_length_exceeded`, etc.) instead of finding nothing. `/admin/*` and `/api/*`
are unchanged (`{"detail": ...}`, the console still reads it). Full code table,
which codes are OpenAI's vs RouteForge's own, and the SSE wire-shape change, are in
[`docs/reference/api-reference.md`](../reference/api-reference.md#errors).

When a request fails before any response has started, the route raises
`HTTPException(status, detail)` (`app/api/routes.py`, `_error_status_detail`), and FastAPI's
default handler returns `{"detail": "..."}`. OpenAI SDK clients read `error.message` /
`error.code` from an `{"error": {...}}` body; finding none, Qwen Code shows only
**"API error"** — even when RouteForge's message says exactly what to do, as B36's context
overflow does ("request exceeds the model's context window … shorten the conversation").

The streamed path is already closer: `sse_error_frame` (`app/api/streaming.py:32`) sends
`{"error": {"message", "type": "routeforge_error", "code": <HTTP status int>}}` — but `code`
is the numeric status, not OpenAI's string codes.

**Fix, roughly.**
1. Register an exception handler for the `/v1/*` routes that renders every error as
   `{"error": {"message": ..., "type": ..., "param": null, "code": ...}}`, keeping the HTTP
   status. Admin routes (`/admin/*`) keep their current shape — the console reads `detail`.
2. Map the known cases to OpenAI's codes: context overflow (`ContextWindowExceeded`) →
   `code: "context_length_exceeded"`, `type: "invalid_request_error"`; a rejected pinned model /
   no eligible model → `model_not_found`-style or `invalid_request_error`; 401 →
   `invalid_api_key`; 429 (budget / rate limit) → `rate_limit_exceeded`; 502 / 503 →
   `type: "server_error"`. Use the same mapping for `sse_error_frame` so streamed and
   non-streamed errors can't drift (one mapping, as `_error_status_detail` already is).
3. Pass the upstream reason through where it is safe (B36 already keeps the context-overflow
   reason whole); never include PII or endpoint URLs (the existing `_scrub_endpoints`
   redaction still applies).
4. Test against the OpenAI Python SDK: a context-overflow request raises `BadRequestError`
   whose `.code == "context_length_exceeded"` and whose message is RouteForge's text. Add it to
   `tests/test_openai_compat.py` (the `consumer-contract-tests` skill covers this boundary).

## B48 — FeasibleRouteBench failure injection for B45: four things it must get right first  ✅ RESOLVED (2026-09-22, `8344b21`)

**Severity:** evaluation-validity prerequisite (no product defect; nothing on the serving
path changes)
**Found:** 2026-09-21, FeasibleRouteBench (P2b-1) final whole-branch review.
**Resolved 2026-09-22** on `feat/b48-failure-injection` (spec `73b7af6`, §6 amended
`153a0d4`; code `290b555` … `8344b21`). Nothing on the serving path or in `config/`
changed. Every requirement is met, and each has a test that fails when it is broken
(research notes §9.7):

- **Failure injection.** Simulated endpoint profiles gain `outages` (inclusive
  case-index ranges), `error_rate` (seeded HTTP-503 flakes) and `failure_latency_ms`
  (`290b555`).
- **(a) Leakage.** `router_warm` is **removed**. The reported arm is now
  `router_online`: its store holds only the attempts this arm made on earlier cases. A
  prior-cases-only `router_warm_prior` is kept as a full-information reference. Both arms
  refuse to score a case they have already seen (`LeakageError`). The falsification: an
  injected leak trips the no-leak check, and a leaking arm avoids a dead endpoint one
  case early (`1a6712b`, `8344b21`).
- **(b) Error-row cost.** Strategies are scored on failover chains. Cost is the sum over
  attempts, and latency includes each failed attempt's failure latency. So a strategy that
  picks a dead endpoint pays for the failover and can no longer look cheaper (`a543abb`).
- **(c) Production-aligned failures.** Injected failures pass
  `counts_as_endpoint_unhealth`. A matrix row records that predicate's verdict on the
  backend exception, taken exactly one hop below the gateway's own exhaustion exception.
  The sweep makes one attempt (`max_failover_attempts = 0`). Both warm arms count an
  error only when the verdict is true (`f446c91`, `b32abb1`, `3a3364e`).
- **(d) The single-shot vs failover gap.** It is **modelled**, not just stated. Router
  arms re-run the real `Router.select(…, exclude=…)`, capped at 1 + the deployment's
  `max_failover_attempts` (3 attempts). The new metrics are `wasted_attempts` and
  `exhausted_rate`. An empty chain is "no choice" (production's 422); an all-failed
  chain is exhausted (production's 502) (`a543abb`, `1a6712b`, `8344b21`).
- **Found on the way:** a 1 s-per-case synthetic clock would stop availability from ever
  recovering within a suite. The fix is a case clock (`--seconds-per-case`, default 30),
  and B45 is reported at 1, 30 and 300 s per case (`153a0d4`, `8344b21`).

See [`docs/research/feasible-route-bench.md`](../research/feasible-route-bench.md) §9 and
the operator guide. The original write-up follows, for history.

B45 can only be answered by a sweep in which endpoints actually fail (the first simulated
run is null by construction, see the B45 update). Adding failure injection to the harness
is the obvious next step, and done naively it would produce a confident wrong answer.
Before any failure-injected B45 result is quoted, the harness must:

- **(a) Add a prior-cases-only warm arm.** Today's `router_warm` replays every row swept so
  far **including the current case's own rows**, so it has seen this case's outcomes on
  every eligible endpoint. With failures injected, that is outcome leakage that inflates
  exactly the availability effect B45 measures: the warm router would "learn" that an
  endpoint is failing from the very call it is being scored on. A warm arm that replays
  only cases strictly before the one being scored (or a disjoint prior sweep) is needed,
  and it is the one to report.
- **(b) Fix error-row $0 cost in the cost aggregates.** An error row has `cost_usd = 0.0`,
  and `mean_chosen_cost` counts a chosen error row as 0.0. A strategy that keeps choosing
  a dead endpoint would look *cheaper*, which skews `mean_chosen_cost` and the headline
  saving. Error rows need their own treatment (excluded, or charged the attempted call)
  before a failing endpoint can appear in the matrix.
- **(c) Inject only exceptions production counts as endpoint unhealth.** Injected
  exceptions must be types `app/gateway/gateway.py::counts_as_endpoint_unhealth` accepts
  (no status, 5xx, 429), or the pipeline's own signal store will not record them as
  availability failures. `strategies.warm_signals` currently counts **any** error row as
  a failure; its error counting must be aligned with `counts_as_endpoint_unhealth`, or the
  warm arm and production disagree on what a failure is.
- **(d) State the single-shot vs failover modelling gap.** The oracle scores each
  `(case, endpoint)` as a single shot (the sweep disables escalation and a pinned call
  cannot fail over). Production fails over (`max_failover_attempts`): a request whose
  first choice fails is usually still served, at extra latency and a wasted call. The
  oracle therefore overstates the quality cost of picking a failing endpoint and ignores
  the latency/cost of the failover. Either model failover in the scoring, or state the gap
  next to every failure-injected result.

Related: B45 (the question), B46 (`counts_as_endpoint_unhealth` boundary coverage),
[`docs/research/feasible-route-bench.md`](../research/feasible-route-bench.md) §5.5 and §6.4.

---

## B47 — a backend failure on a real-token stream after the client already disconnected writes no usage row at all

**Severity:** low
**Found:** 2026-09-21, signal hardening Task 7 self-review. **Open.**

On a real-token stream (`stream_unverified` / `incremental_verify`), a backend failure
that happens **after** the client has already disconnected surfaces inside
`_finalize_stream`'s drain-on-disconnect path, not the new `_record_failed_stream`
(which only runs when the *consuming* generator is still live to reach its `finally`).
That drain path only logs the failure ("drain-on-disconnect failed") — no
`usage_records` row is written at all, neither `cancelled` nor `error`. Pre-existing
behaviour, not introduced by this branch; the ruling that shipped B38's error-row work
deliberately left disconnect handling on the drain path unchanged.

**Fix, if picked up.** Route that failure through `_record_failed` (or
`_record_cancelled`, since the client is already gone) from within the drain's
exception handler.

---

## B46 — `counts_as_endpoint_unhealth` is tested in the interior of every branch and at none of its boundaries

**Severity:** low (coverage gap; the classifier is correct today)
**Found:** 2026-09-19, P2a Task 9 review. **Resolved 2026-09-19 (fix wave).**
`tests/routing/test_signal_wiring.py` now parametrizes 399 and 429 (expect `True`) and
400/401/403/404/422/428/430/499 (expect `False`), closing both the lower and upper
boundary of `not (400 <= code < 500)` and the 429 carve-out. No production change --
the classifier was already correct at every boundary.

`app/gateway/gateway.py::counts_as_endpoint_unhealth` decides endpoint unhealth with

```python
if code == 429:
    return True
return not (400 <= code < 500)
```

`tests/routing/test_signal_wiring.py` exercises **400, 401, 403, 404, 422** (expect
`False`), **500, 502, 503, 504** (expect `True`), **429** (expect `True`), a no-status
exception, and `ContextWindowExceeded`. Every one of those is comfortably inside a
branch. **The boundaries — 399, 428, 430 and 499 — are untested**, and they are exactly
the edges of the range comparison and of the 429 carve-out.

So an off-by-one (`400 < code`, `code <= 500`, `code >= 429`, `code in range(400, 499)`)
survives this suite. The practical blast radius is small — 399 is not a real HTTP status,
428 and 430 are rare — but 499 (nginx's "client closed request") is not rare behind a
proxy, and misclassifying it would push traffic off a healthy endpoint every time a
client disconnected, which is precisely the failure mode ruling 2 exists to prevent.

**Fix.** Add 399, 428, 430 and 499 to the existing `@pytest.mark.parametrize` lists.
Four lines; no production change. Worth doing before anyone edits that predicate.

## B45 — is `weights.availability: 0.2` large enough for a dead endpoint to lose? Unmeasured, and must not be tuned by guess  ✅ RESOLVED (2026-09-23, step-7 circuit breaker)

**Severity:** design question, not a defect (the formula does what it says; the question
is whether the magnitude is right)
**Found:** 2026-09-19, P2a Task 9 review, while falsifying a claim in the design spec.
**Measured 2026-09-23 (simulated + live; see the updates below). The weight question is
answered.** **Resolved 2026-09-23** on `feat/endpoint-circuit-breaker` by building the
step-7 circuit breaker (`app/gateway/breaker.py`): after `breaker.failure_threshold` (3)
consecutive endpoint failures a model is excluded from routing for a cooldown (30 s,
doubling on each failed half-open result up to 300 s), through the last stage of the
eligibility seam (`circuit_open`). `weights.availability` stays 0.2 as a ranking preference.
Measured on the same simulated outage scenario in
[`docs/research/feasible-route-bench.md`](../research/feasible-route-bench.md) §12; operator
contract in [`docs/reference/routing-observability.md`](../reference/routing-observability.md) §5.

Availability enters the step-6 score as an additive term `+ w.availability · availability`
with `availability ∈ [0, 1]`. So moving a model from perfectly healthy to completely dead
changes its score by **exactly `w.availability`, and no more**. With the shipped weights
(`config/routing.yaml`: quality 1.0, cost 0.6, latency 0.3, **availability 0.2**), that is
a `0.2` penalty, against a cost term spanning `0.6` across the eligible set and a quality
term spanning `1.0`.

**Consequence, demonstrated rather than argued:** a fully dead endpoint
(`availability = 4.0e-08`, `src: measured`, `n = 10`) scored **+0.3800** against a healthy
but pricier peer at **+0.0400** — and **ranked first**. A dead endpoint outranks a healthy
peer whenever the peer is worse on cost by more than a third of the set maximum, or worse
on quality by more than `0.2`.

This is not a bug in the formula. A bounded additive preference is exactly what was
specified, and availability is explicitly **not** a circuit breaker (P2a §2, "out of
scope"). It became an issue because the design spec described it in language — *"a dead
endpoint ranks last"* — that promised protection the formula does not provide, and that
sentence was the stated reassurance for shipping without a breaker. Both the spec and
`docs/research/routing-signal-provenance.md` now carry the correction inline.

**The open question is the magnitude**, and it has two sides that trade against each
other on *every* request, not only on requests involving a sick endpoint:

- too low, and a failing endpoint keeps winning on price until failover absorbs the
  cost — extra latency and a wasted call on every request that picks it;
- too high, and availability starts overriding genuine cost and quality differences
  between two perfectly healthy models, which is the term reaching beyond its remit.

**Do not tune this now.** Changing a scoring weight inside P2a would break the premise of
the identity property (design §6.2): that with an empty signal store the new formula
reproduces the old scores exactly, so every post-deploy behaviour change is attributable
to a measurement existing. A weight change is a behaviour change with no measurement
behind it, and it would also destroy the clean control arm P2b needs
(`signals.enabled: false`).

**This is P2b's question.** The evaluation plane is the first thing in the programme able
to answer it: sweep `weights.availability` against a feasible-oracle benchmark with
injected endpoint failures and report the regret curve. Until then the honest operator
guidance — now in `docs/reference/routing-observability.md` — is that availability is a
ranking preference, and the way to take a failing endpoint out of rotation is to disable
it.

> **Update 2026-09-21 (FeasibleRouteBench, P2b-1). Still open.** The instrument now
> exists. `scripts/feasible_bench.py --variants eval/variants/b45.yaml` scores
> `router_cold:<variant>` and `router_warm:<variant>` against the feasible oracle for any
> weights mapping. The first run is **simulated**: starter suite, `healthcare-sim.yaml`,
> seed 0, suite hash `3339c773…d8199`, code `e8e8682` (re-verified at `0a92a73`: every
> figure unchanged). It gives a **null result**. All
> four variants (availability 0.2 / 0.0 / 1.0 / 2.0) have the same pass rate (0.909),
> cost regret (3.2e-05 USD) and floor-miss rate (0.091) as the unvaried router, in both
> the cold and the warm arm. The formula predicts this. No sweep call failed, so
> availability was the same for every candidate (neutral when cold, **exactly 1.0** when
> warm: the replayed rows are successes only, from the store's start state, and decay
> pulls toward 1.0), and a constant term cannot move a ranking. The run answers neither side of the question
> above. What would answer it is a **failure-injected** sweep: a simulated per-endpoint
> error rate (not built; the simulated backend never errors), or a `--live` run with a
> deliberately failing endpoint. What that injection must get right before its result
> can be trusted is **B48**. See
> [`docs/research/feasible-route-bench.md`](../research/feasible-route-bench.md) §5.5 and
> the operator guide [`docs/guides/feasible-route-bench.md`](../guides/feasible-route-bench.md).

> **Update 2026-09-22 (B48 resolved; first real answer, simulated). Still open, pending a
> live run.**
>
> **The run.** Failure injection now exists (B48). A designed outage scenario was run
> with the online router arm across seeds 0–9 and 1, 30 and 300 s per case: 30 runs, all
> exit 0, no violations.
>
> - Suite: `eval/suites/outage.jsonl`, hash `78e1be2b…9969`.
> - Endpoints: `eval/endpoints/outage-sim.yaml`, hash `bdb74978…f7d6`. Two equal-quality
>   `cheap` endpoints; the 4× cheaper one is down for cases 10–19.
> - Code: `8344b21`.
>
> **The answer for this simulation is no.** At `availability: 0.2`,
> `router_online:default` sent the first attempt of every outage case to the dead
> endpoint. Its attempt chains are identical, case by case, to weight 0 in all 30 runs.
> Failover still served the requests, so the cost of 0.2 here is one wasted attempt plus
> the failed call's latency (1000 ms, a **designed** value in the endpoint file) on every
> request during the outage.
>
> **Why.** The formula predicts this. The availability term can take back at most `w`,
> and the dead endpoint's score lead (mostly cost) is 0.42–0.54. In general, with equal quality and
> latency, a fully dead endpoint loses at `w = 0.2` only if its price is more than about
> **2/3 of its peer's**.
>
> **Higher weights** (mean over seeds, wasted attempts per 30-case run, weight 0 = 10.5):
>
> - `w = 2`: 5.5 at 1 s/case (+39% mean chosen cost), 5.7 at 30 s (+35%), no change at
>   300 s. Latency regret improves from 326 ms to 217/221 ms.
> - `w = 1`: saves 0.6 attempts at 1 s/case at +44% cost, with latency regret worse
>   (381 ms), because it sticks to the pricier endpoint after recovery. It saves 0.5
>   attempts at 30 s at +2%, and nothing at 300 s.
>
> Both lag the outage by several failures. All of their extra cost comes after the outage
> ends: they keep the recovered, 4× cheaper endpoint out of rotation (about a minute of
> clock time at `w = 1`). The saving scales with the designed failure latency, and the
> cost with the designed price ratio (0.25).
>
> **Recommendation to the owner, not a config change:** keep 0.2 as a ranking
> preference. Treat "take a dead endpoint out" as a breaker question (step 7, not built)
> or operator disable, not as a weight to raise. Revisit only after a live run.
> **Changing `weights.availability` is the owner's decision;** `config/routing.yaml` is
> untouched.

> **Update 2026-09-23 (live run). Answered: the live result agrees with the simulation.**
> A `--live` outage run on the DGX Spark at `5da06a4` used real Llama (cheap, illustrative
> 0.10/1M) and Nemotron (0.40/1M) endpoints. A proxy dropped Llama's connection for cases
> 10–19, and no model server was stopped (`scripts/outage_proxy.py`). The drops surfaced
> through the real gateway as `InternalServerError … Connection error` with
> `counts_unhealth: true`. At `availability: 0.2` the online router sent **all 10** outage
> first attempts to the dead endpoint, the same as weights 0 and 1. At weight 2 it moved
> only 2 of them. Failover served every case (0 exhausted, 0 violations). One seed and one
> failure shape (an immediate drop). Details are in
> [`docs/research/feasible-route-bench.md`](../research/feasible-route-bench.md) §11.2.
> **The measurement question is closed.** A weight does not keep a dead endpoint out without
> overriding genuine cost differences. **Remaining owner decision:** build the step-7
> circuit breaker (or rely on operator disable). Keep 0.2 as the ranking preference;
> `config/routing.yaml` is untouched.
>
> **Limits.** This is one designed scenario with simulated failures. Outages are indexed
> by case, not time. The outage was one-sided: the healthy peer drew no flakes at seed 0
> and 14 in total across seeds 1–9. No live run has been made. Full result, formula,
> switch points and limits:
> [`docs/research/feasible-route-bench.md`](../research/feasible-route-bench.md) §9.

> **Update 2026-09-23 (breaker built). ✅ RESOLVED.** The owner chose the breaker. It is
> per process, fed only by `counts_as_endpoint_unhealth` failures (a 4xx never trips it),
> and runs as `feasible_set`'s **last** stage, so every policy reason wins over it and it
> can never widen placement. If every policy-eligible candidate is open, the router tries
> one anyway and marks the decision `circuit_bypass`. Half-open is evidence-based with no
> single-trial lease: after the cooldown every request may use the model, and the first
> reported result closes or reopens it. `enabled: false` is the pre-breaker behaviour.
>
> **Measured (simulated, §12; seeds 0–9, breaker on vs `router_online:breaker_off`):**
>
> - **1 s/case:** 3 dead first attempts per outage instead of 10 (exactly
>   `failure_threshold`), 4.4 vs 10.5 wasted attempts per run, latency regret 199 vs 326
>   ms. Cost +51% mean chosen cost, all after recovery: the 30 s cooldown outlasts the rest
>   of the suite, so the recovered, 4× cheaper endpoint never comes back within it.
> - **30 s/case:** 6 of 10 (3 to trip, 3 failed half-open results), 7.0 wasted per run,
>   latency regret 265 ms, +39% cost; the endpoint is back 7 cases after recovery.
> - **300 s/case:** no effect, at any seed. That is the bench's case clock (no cooldown up
>   to `max_cooldown_s` outlasts a 300 s gap), not production, which uses wall-clock time.
>
> **Not shown:** a live breaker run, concurrent requests (the accepted half-open cost),
> several replicas (each trips separately), streaming, other breaker settings.

**Related.** B37 (`/health` reports a dead model as available for 22 hours) and B38
(a failed request writes no usage row, so availability never seeds — **resolved
2026-09-21**, see below) both made a dead endpoint *less* visible to this signal than it
should be; B37 still does. The step-7 breaker (built 2026-09-23, above) is the other answer
to the same problem; it does not change what `/health` reports (B37).

## B44 — `test_default_strategy_matches_pre_refactor_scoring` is an ordering-only oracle and reads like a stronger guarantee than it is

**Severity:** trivial (documentation of a test; no defect)
**Found:** 2026-09-19, P2a Task 6 review. **Resolved 2026-09-19 (fix wave):** a comment
at `tests/test_routing_strategy.py:73-80` (just inside
`test_default_strategy_matches_pre_refactor_scoring`) now points at
`tests/routing/test_strategy_identity.py::
test_empty_signal_store_reproduces_the_legacy_scores_exactly` as the score-level pin.

`tests/test_routing_strategy.py::test_default_strategy_matches_pre_refactor_scoring`
compares the strategy's output *ordering* against a golden re-implementation, over a
single tier. The P2a Task 6 reviewer mutated the latency term to candidate-set-max
normalization — which breaks the identity property outright — and this test **stayed
green**, because within one tier a set-max latency term is constant across candidates
and cannot change an ordering. The new
`tests/routing/test_strategy_identity.py::test_empty_signal_store_reproduces_the_legacy_scores_exactly`
went red, as designed, and is the **only** thing in the repo pinning score-level
identity.

**Fix.** A comment at `tests/test_routing_strategy.py:95-106` pointing at the
score-level pin, so a future reader does not retire the identity test believing the
older oracle covers it.

## B43 — the response `usage` object carries a flat `cached_prompt_tokens`, not OpenAI's nested shape

**Severity:** low (API-surface decision, deliberately deferred; benign today)
**Found:** 2026-09-19, P2a Task 2 ruling. **Open — needs a decision, not a fix.**

P2a threads the provider-reported cached prompt-token count through to the response so
the meter can persist it. It is exposed as a **flat** key:

```json
"usage": {"prompt_tokens": 1000, "completion_tokens": 50, "total_tokens": 1050,
          "cached_prompt_tokens": 768}
```

OpenAI's own shape is nested — `usage.prompt_tokens_details.cached_tokens` — which is
also the shape RouteForge *reads* from upstream providers
(`app/gateway/backends/litellm_backend.py::_usage_dict`). So RouteForge currently
consumes the standard shape and emits a non-standard one.

Benign in practice: OpenAI-compatible clients ignore unknown `usage` keys, and
`tests/test_openai_compat.py` uses a subset check, so nothing breaks. But it is a
divergence on the one surface that is supposed to be a drop-in, and adopting the nested
shape later is a breaking change for anything that starts reading the flat key.

**Decide explicitly:** emit the nested shape (and possibly keep the flat key as a
deprecated duplicate for one release), or document the flat key as intentional. See the
`consumer-contract-tests` skill — this is exactly the class of wire-shape drift it exists
to catch.

## B42 — `SignalStore` reports `samples: 0` on the `disabled` path even when observations exist

**Severity:** trivial (cosmetic honesty gap in the scoring record)
**Found:** 2026-09-19, P2a Task 4 review. **Open.**

`app/routing/signals.py`'s `latency()` and `availability()` return early when
`signals.enabled` is false:

```python
if not self._cfg.enabled:
    return SignalValue(default, "disabled", 0)
```

The `0` is hardcoded, while `observe_success` / `observe_error` keep incrementing the
internal counters regardless of `enabled`. So a `disabled` term reports `n: 0` even
though the store may hold hundreds of observations for that model. The
`tier_default` / `neutral` paths do the opposite — they deliberately report the *true*
count so the record is honest about how little (or how much) is known.

Defensible as "zero signals were consulted", but it is inconsistent with the two
sibling paths and slightly understates what an operator would recover by flipping
`signals.enabled` back on. Either report the true count, or document the `0` as "consulted",
in the type.

## B41 — streaming requests feed no routing-signal observations at all  ✅ RESOLVED (2026-09-21, `f582181`)

**Severity:** low (bounded, deliberate; but it silently excludes whole applications)
**Found:** 2026-09-19, P2a Task 7 ruling. **Resolved 2026-09-21 (signal hardening).**
`Gateway.run_stream` now observes its terminal chunk as an availability-only success
(`observe_stream_success`, `app/gateway/gateway.py`) and logs time-to-first-token,
without touching the latency EWMA — a stream's timing is a different quantity from a
non-streaming call's backend latency, so mixing them would still corrupt it (reason 2
below still holds; only reason 1, "no failure path", is closed). An endpoint error
before or during the stream is now observed too, under the same
`counts_as_endpoint_unhealth` classifier as non-streaming calls. A client disconnect or
a stream ending without a terminal chunk still records nothing — neither is
attributable to the endpoint.

**Correction (2026-09-21):** the claim that Qwen Code / kit-gtm contribute nothing was
wrong — every Qwen Code request carries `tools`, which raises `StreamFallback` before
`run_stream`, so it is served by `Gateway.run()` and was always observed (live:
`availability n=98 measured`). Only plain-chat streaming was blind.

`SignalStore.observe_success` / `observe_error` are called only from
`Gateway.run()` (`app/gateway/gateway.py`), never from `run_stream()`. A **streaming-only
application therefore contributes nothing to the measured signal**, however much traffic
it serves. Real clients this affects today: the Qwen Code / kit-gtm agent clients on the
Spark, which stream.

Whether the endpoints it uses *report* a default depends on the rest of the deployment:
the store is keyed **per model, not per application**, so any other application sending
non-streaming traffic to the same model makes it `measured` — on that other application's
traffic. It is the single-tenant or all-streaming deployment whose endpoints stay at
`tier_default` / `neutral` indefinitely. Both shapes are wrong in their own way: in the
first, a model is ranked on a signal measured from requests nothing like the ones being
ranked; in the second, the signal never exists at all.

**This was ruled deliberate, not overlooked.** Two reasons, both worth keeping:

1. `run_stream()` has no retry loop, so it would record successes without a comparable
   failure path — a biased signal is strictly worse than an absent one.
2. A stream's latency is time-to-last-token over a whole generation. That is not the
   same quantity as a non-streaming call's latency, and mixing the two would corrupt
   the EWMA rather than enrich it.

**Fixed as suggested.** Streaming is observed separately: availability moves on a
stream's terminal chunk, time-to-first-token is logged (not scored), and latency is
never touched — exactly the shape this entry recommended, not a call to
`observe_success` from `_agen()`'s terminal chunk.

## B40 — no clock seam on `RoutingStrategy.rank`, so staleness and availability decay can't be tested through the strategy  ✅ RESOLVED (2026-09-21, `6a60244`)

**Severity:** low (test-seam gap; production behaviour is correct)
**Found:** 2026-09-19, P2a Task 6 review. **Resolved 2026-09-21 (signal hardening).**
`rank` on both the `RoutingStrategy` Protocol and `DefaultRoutingStrategy` gained
`now: float | None = None`, forwarded to both `SignalStore` reads; `Router.select`
passes nothing (production still reads the real clock). Every implementation and test
fake — the Protocol, the default strategy, and the one test spy found by grepping
`def rank` — changed in the same commit, per this entry's own warning about
Protocol-signature drift. Two new tests exercise the staleness cliff and the
availability decay *through* `rank(now=...)` for the first time.

`DefaultRoutingStrategy.rank` calls `signals.latency(...)` / `signals.availability(...)`
without the `now` argument those methods accept, so both read `time.monotonic()`. Production
is correct — writer and reader share the same clock — but **no test can exercise the
staleness cliff or the availability decay *through* the strategy**, only at the store level
(`tests/routing/test_signals.py` covers both there). It also made two Task 6 tests
clock-mix: they had to observe on the real clock, because observing on a synthetic one and
reading on the real one makes every measurement look stale.

**Fixed as above.** Protocol-signature drift was the risk called out here; the fix
changed the Protocol, the one implementation, and the one test fake in the same commit.

## B39 — the whole-trace `latency_ms` sum blends escalation, compression and cache latency into the boot-seeded signal, not just escalation  ✅ RESOLVED (2026-09-21, `3286adc`)

**Severity:** low (affects the boot seed only; self-correcting as live observations accumulate)
**Found:** 2026-09-19, P2a Task 7 review. **Widened 2026-09-19 (fix wave). Resolved
2026-09-21 (signal hardening).** Boot seeding no longer aggregates
`usage_records.latency_ms` at all — that table is not read for seeding any more.
Seeding now replays `endpoint_observations` (`app/apps/store.py::load_seed_observations`
+ `SignalStore.replay`), whose `latency_ms` column is populated only from
`Gateway.run`'s own per-hop, backend-only `observe_success` call — the exact quantity
the live signal already used, never a trace-wide sum. A semantic-cache hit never calls a
backend, so it never writes a row to this log either (see B41's resolution note and
`docs/research/routing-signal-provenance.md` §3A for "B39/B41 resolved by
construction").

`app/api/routes.py::_finalize` computes the persisted usage row's latency as

```python
latency_ms = sum(float(r.get("latency_ms", 0.0)) for r in trace
                 if r.get("latency_ms") is not None)
```

— the sum over the **whole trace** — and writes it against `resp["model"]`, the model that
served the final answer. That is the right number for "how long did this request take",
and the wrong number to attribute to one model.

**Originally recorded as an escalation-only problem: it is not.** The same line also sums
in `CompressionRecord.latency_ms` and `CacheRecord.latency_ms` (`app/models.py:255,284`),
both present in `trace` whenever their step ran. So what gets **seeded** is whole-pipeline
latency, while `Gateway.run`'s `observe_success` records **backend-only** latency per hop
— two different quantities blended into one EWMA both labelled `measured`. On a
confidence-driven escalation the earlier hop's time is charged to the model that answered
second, exactly as originally filed. With the shipped defaults (Headroom/`context_
optimization.enabled: false`, semantic cache off) that escalation case is the only one
that fires and this collapses to the original framing.

**With Headroom on — a shipped, advertised feature — it is a systematic upward bias, not
just an escalation artifact.** Every compressed request's `CompressionRecord.latency_ms`
(the compression step's own wall time) adds to the seeded figure for whatever model served
that request, on every request, not only on escalations. Toggling Headroom on therefore
inflates every served model's seeded latency by roughly the compression step's cost,
uniformly, which the escalation framing did not capture.

P2a's boot seeding averages `usage_records.latency_ms` per model
(`aggregate_model_signals`), so an escalation-heavy application seeds its frontier models
with inflated latency and mildly under-prefers them until live observations wash it out;
a Headroom-on deployment seeds *every* model's latency slightly high, uniformly, until the
same washout.

**Bounded.** Live observation is per-hop and correct (`Gateway.run` calls
`observe_success(cand.name, result.latency_ms)` with that hop's own backend-only latency).
Pure failover is unaffected, because failed-hop `DecisionRecord`s are hardcoded
`latency_ms=0.0`.

**A third contributor, found separately (fix wave, 2026-09-19): a semantic-cache hit.**
A cache hit returns a full response stamped with the serving model's slug (`entry.model`,
`app/pipeline.py`) and a trace whose only latency entry is the ~3 ms cache lookup. That
flows through the same `_finalize` path into an `ok` `usage_records` row for that model,
which `aggregate_model_signals` averages in alongside genuinely-served rows. A model whose
recent window is mostly cache hits can seed at a few hundred milliseconds `measured` with
tens of samples, and outrank a genuinely faster peer that has never been cache-served.
Mitigated today only because `semantic_cache.enabled: false` is the shipped default
(`config/serving.yaml`). A fix needs a cache-hit marker on `usage_records` distinguishing
a cache-served row from a model-served one — out of scope for this fix wave, same as the
rest of B39.

**Resolved 2026-09-21, not by marking cache hits but by no longer reading
`usage_records` for seeding at all.** A cache hit never calls a backend, so `SignalStore`
never applies an observation for it and no `endpoint_observations` row exists to seed
from — the pollution path is gone, not patched. `aggregate_model_signals` itself is
deleted (`app/apps/store.py`), so there is no `usage_records` average left to blend
anything into.

## B38 — no `usage_records` row is written for a failed request, so the availability signal can never seed  ✅ RESOLVED (2026-09-21, `c3782cc`)

**Severity:** low–medium (bounded by `min_samples` after each restart; but it makes one
documented enum value dead)
**Found:** 2026-09-19, P2a Task 7 review. **Resolved 2026-09-21 (signal hardening).**
A failed request now writes exactly one `usage_records` row with `status = "error"`
(`app/api/routes.py::_record_failed`, wired into the non-stream 502 path, the keepalive
SSE error path, and a real-token stream whose backend fails mid-generation) — model is
the last attempted candidate (`BackendUnavailable.attempted_models[-1]`), tokens and cost
are zero, `latency_ms` is wall time to failure. `usage_summary` / `usage_by_application`
gain `error_count`; every other figure (spend, tokens, budgets, latency percentiles)
stays computed over `ok` rows only, asserted by test. Availability now seeds correctly
from these rows via the observation-log replay (§B39's resolution), closing the "always
seeds at 1.0" consequence below. **Not fixed by this change:** a backend failure on a
real-token stream *after* the client already disconnected still writes no row at all —
filed separately as B47.

`app/apps/store.aggregate_model_signals` seeds the P2a signal store from rows with
`status IN ('ok','error')`. That filter is correct against the documented status enum —
but **nothing in the codebase ever writes `status = 'error'`.** `app/api/routes.py`
writes exactly three values: `"ok"` (`_finalize`, the served path), `"rejected"` (the
daily-budget rejection) and `"cancelled"` (client disconnect). When `pipeline.run()`
raises — `BackendUnavailable`, `NoEligibleCandidate`, precisely what a failing endpoint
produces — the handler maps the exception to an `HTTPException` and returns; `_finalize`
and therefore `record_usage` are never reached, so **no row is written at all.**
Separately, a request that succeeds via failover persists only the *winning* model's row;
the candidates the gateway observed failing live get no row.

Consequence for P2a: **latency seeds correctly from `ok` rows; availability always seeds
at `1.0`.** After a restart a badly-failing endpoint starts neutral and needs
`signals.min_samples` (default 5) live observations to be down-weighted again — five
failed-and-failed-over requests per replica restart, not a correctness hole, and never an
eligibility change (a signal can only reorder).

Consequence beyond P2a, and the reason this is worth its own entry: **failed requests are
invisible to the meter.** They do not appear in the usage table, the cost dashboard, or
any per-app aggregate. An application whose endpoint is down looks, in the console, like
an application that sent no traffic.

**Fix, roughly.** ~~Meter failed requests — write an `error` row from the exception path
with the model that was attempted, zero tokens and zero cost.~~ Done as described above.

**Latent latency/samples inversion, found in the fix wave, 2026-09-19. Resolved
2026-09-21 (signal hardening) — not by patching `aggregate_model_signals`, but by
deleting it.** The inversion this note warned about (`samples` counting `ok+error` while
the average silently computed over `ok` rows only, so an all-error window could seed a
dead endpoint as `measured` at `0.0` ms — the strongest possible preference for the worst
candidate) lived entirely inside `aggregate_model_signals`, which no longer exists.
Seeding now replays `endpoint_observations` directly: `SignalStore.replay` re-applies
each row through the *same* `observe_success` / `observe_error` methods live traffic
uses, so each signal's sample count comes from its own observations (an `error` row
never contributes to the latency count, because `observe_error` never touches latency
state) — there is no separate aggregation step left to get this wrong in.

## B37 — `/health` reports a model as available for 22 hours after its endpoint died

**Observed (DGX Spark, 2026-09-18).** The vLLM container serving `qwen3.8-27b`
stopped at `00:13:48Z` (clean SIGTERM, exit 0, `restart: unless-stopped`, so it
stayed down). For the next ~22 hours `GET /health` kept answering:

```
{"status":"ok","backend":"litellm","models":["llama3.2:latest","qwen3.8-27b"]}
```

while every request to that model failed — 11 logged `502 no backend could serve
this request`, first at `00:13:48Z` (the same second the server shut down), last
at `13:16:43Z`. An operator checking `/health` during the outage would have
concluded RouteForge was healthy and the client was at fault.

**Cause.** `app/api/routes.py::health` returns `registry.effective_pool()` name-for-name
— the onboarded+enabled rows — with no reachability check:

```python
pool = registry.effective_pool() if registry else s.routing.model_pool
return {"status": "ok", "backend": "litellm", "models": [c.name for c in pool]}
```

`effective_pool()` answers "what is configured and enabled", which is the right
input for routing eligibility and the wrong answer to "what can serve a request
right now". Onboarding is probe-gated at write time, but nothing re-probes after.

**Why it matters beyond the confusion.** `ModelRegistry.has_healthy_onprem()` is
what drops `onprem` from `allowed_providers` and turns an `onprem_only` request
into a derived reject (CLAUDE.md, "No on-prem model onboarded ⇒ derived reject").
If that flag is as stale as the `/health` list, the derived-reject guarantee is
answering from configuration rather than from reality — worth checking whether
the two share a source, and whether an `onprem_only` app gets a clean reject or a
502 when its only endpoint is down.

**Fix, roughly.** Give `/health` a liveness dimension rather than a flat list:
report each model with a last-probe result and timestamp, and make "healthy" mean
probed-recently-and-answered. Needs a background re-probe with a bounded interval
and a stale-after window; `/health` must stay cheap and must not probe inline on
every call. Decide explicitly what the top-level `status` means when some models
are down but others serve — degraded is probably right, and whatever is chosen
should be what a load balancer and an operator both read correctly.

**Cross-refs.** B32 (onboarding reports a 401 as "unreachable" — same probe
surface). The `/metrics` families would be the natural place to expose per-model
liveness for alerting, so an outage pages someone instead of waiting for a user
to notice.

## B36 — A conversation that outgrew the model's context window was reported as `502`  ✅ RESOLVED (2026-09-16)

**Observed (DGX Spark, agent client).** A long coding-agent session grew to
~252K prompt tokens against a vLLM model served with `--max-model-len 262144`.
From then on every turn got an upstream `400` ("This model's maximum context
length is 262144 tokens …"), which the gateway treated like any backend failure
and returned as `502 no backend could serve this request`. The client read it as
an outage and retried the identical oversized request; the useful part of the
upstream message (the input-token count) was also cut off by the 200-char
error truncation.

**Fix.** The LiteLLM backend translates `litellm.ContextWindowExceededError`
into backend-neutral `app.errors.ContextWindowExceeded`. The gateway still fails
over (a peer with a larger window may serve it); only when **every** tried
candidate refused for that reason does it raise `ContextWindowExceeded`, which
the route maps to `400 request exceeds the model's context window: … -- shorten
the conversation … or lower max_tokens`, with the upstream reason kept whole
(scrubbed + redacted). Any other failure in the mix stays `502`. The SSE error
frame on keepalive streams carries `code: 400`. Regression guard:
`tests/test_context_window_overflow.py`.

## B35 — Agent tool turns retried every 120 s; abandoned work metered as served; traces unusable  ✅ RESOLVED (2026-09-16)

**Found by** a data audit of a live agent-client app. 88 of 433 requests were
retries whose starts were exactly 120–122 s apart. The client's transport
timeout covers connect plus first response bytes, and RouteForge sent nothing
until a 5–10 minute tool turn finished. Each abandoned attempt kept generating
on the backend, overlapped the retry, and was written as a `status=ok` usage
row, snapshot and Langfuse trace. In Langfuse every span was 0 s long, there was
no generation or token usage, and `sessionId` was empty. One 502 had no logged
cause.

**Fixed.**
- Streamed turns that wait for a complete answer get SSE keepalives after
  `server.stream_keepalive_s`. Late failures become SSE error frames.
- A client disconnect cancels the backend work and meters it as `cancelled`.
- Trace spans carry real timings, and every router hop that chose a model is a
  Langfuse generation with its model, tokens and cost.
- A session id is derived when the client sends none.
- Upstream backend failures and unhandled errors are logged.

**Unchanged by design.** Real token streaming (`stream_unverified`,
`incremental_verify`) still drains on disconnect to keep accounting.

---

## B34 — Every new app onboarding hit a different policy failure  ✅ RESOLVED (2026-09-15)

**Root cause.** Tests sent tiny synthetic requests, one policy field at a time; real clients (Qwen Code, openai SDK, LangChain) send content parts, `stream:true` on every turn, 20+ tool schemas, replayed tool turns, `content:null` and 25K-token prompts. The admin API also accepted configurations that could never route, and rejections did not name the field to change, so each incident took several rounds (16 incidents across millwise, demo, kit-gtm).

**Fixed.** `tests/onboarding/` conformance matrix (real client shape × policy recipe × model price, "served or told why"); B24; generation params forwarded; budget estimate prices output at `max_tokens` and counts tool schemas; tier fallback when over budget; per-app `max_cost_usd_per_request`; actionable 400/422/502 details; save-time refusal of impossible policies plus `warnings` for unroutable apps and agent-blocking model prices.

**Upgrade note.** Run the `app_policies.max_cost_usd_per_request` `ALTER` (or `scripts/migrate_app_policies.py`) before rolling out the new image, or every authenticated request returns 500 until it runs (see `docs/reference/per-app-config.md` "Manual DB migration").

**Behaviour change.** `max_tokens` / `max_completion_tokens` are now forwarded to the backend, so a large value against a small-context backend can fail upstream where it was previously dropped.

**Rule for new request-path work.** Add the client shape to `tests/onboarding/clients.py` and let the matrix run it under every recipe.

- Follow-up: boot-time check that fails with the missing column named.

---

## B33 — Per-app Langfuse credentials form always looks like "add new", never "edit existing", and never checks what it saves

**Severity:** low–medium (UX; no privacy impact — raw capture still only goes to
the app's own project). Hit by every operator who sets per-app Langfuse credentials.
**Found:** 2026-09-14, DGX Spark, app `go-to-market-app` (`kit-gtm`).

An app has exactly **one** credential set (host, public key, secret key, project
id — columns on `applications`), and **Save credentials** replaces all of it
(`PUT /admin/applications/{id}/langfuse`). Nothing is ever added. But the form in
step 4 (`LangfuseCredsForm`, `ui/src/admin/AppSettings.tsx`) doesn't show that:

1. **It always shows four empty fields.** After a save it clears them, and the
   grey placeholders (`http://langfuse-web:3000`, `pk-lf-…`, `cmt5…`) look like
   pre-filled defaults. The operator saved several times, thinking each save
   either failed or added another key set.
2. **The saved values are never shown.** Host, public key and project id aren't
   secret, but they aren't rendered back, so a mistake stays invisible. In this
   session the secret key was pasted into the project id field, and later the
   host was the browser URL — both saved with `200`, both only found via SQL.
3. **"credentials set ✓" means stored, not working.** It is
   `has_langfuse_keys` (a public key and a secret are present). Nothing checks the
   pair authenticates, the project id is that pair's project, or the host is
   reachable **from the api container**. `http://spark-ae6d:3002` (works in the
   browser, refused inside the container) passed silently.
4. **All four fields are required on every save**, including the secret, so
   fixing one wrong field means retyping the others.

**Fix ideas:** (1) when credentials exist, show a read-only summary (host,
public key, project id, secret as `••••`) with an **Edit** button and a
**Remove** action, and show the empty form only when nothing is set; (2) in edit
mode, prefill the non-secret fields and allow leaving the secret blank to keep
it (server-side merge); (3) validate on save from the api process —
`GET {host}/api/public/projects` with the pair must succeed and return the given
project id — and 422 naming the failing check ("host not reachable from the api
container", "keys rejected", "keys belong to project X, not Y"); (4) put the
"server-reachable host, not the browser URL" note beside the field instead of in
a tooltip.

## B32 — Onboard form's "api key env" silently accepts a raw key; a 401 is reported as "unreachable"

**Severity:** low–medium (UX; no privacy impact). Every operator onboarding an
authenticated endpoint (vLLM `--api-key`, NIM) is likely to hit it.
**Found:** 2026-09-14, DGX Spark, onboarding a vLLM server started with `VLLM_API_KEY`.

The field takes the **name** of an environment variable in the api container
(`app/apps/probe.py:22`, `litellm_backend.py:78-81` resolve it with
`os.environ.get`). The operator typed the **key itself** (`api key env:
<the key>`). Nothing rejects that: the lookup finds no such variable, the probe
sends no `Authorization` header, vLLM answers `401`, and the row is saved
disabled with a health badge of **unreachable** — which sent debugging toward the
network (the endpoint was reachable). The `probe_error` column does hold
`401 Unauthorized`, but the fleet table doesn't surface it.

**Fix ideas:** (1) on onboard/edit, 422 when `api_key_env` is set but that
variable is absent from the api process environment ("env var X is not set in
the api container"); (2) distinguish `unauthorized` from `unreachable` in
`probe_status`, or show `probe_error` in the fleet row; (3) relabel the field
"API key env var name" with a placeholder like `SP_ONPREM_API_KEY`. Operator
workaround today: put the key in `SP_ONPREM_API_KEY` (already passed into the
api container by every compose bundle) and set the field to that name. Related
trap on the same form, already handled well: the slug must equal the endpoint's
served model name, and the probe says so explicitly.

## B31 — CLAUDE.md rule 2's "declared no-protection posture" isn't enforced for non-onprem_only apps

**Severity:** medium (spec/code mismatch — either the rule overstates what is enforced,
or the `capture_io=full` clamp is missing a condition)
**Found:** 2026-09-11, final whole-branch review of PR #39 (docs-reclassification). **Open.**

CLAUDE.md rule 2 says raw capture (`capture_io="full"`) requires "the app to be in a
declared no-protection posture." `app/api/admin_routes.py`'s clamp (`~648-661`) enforces
that only for `onprem_only` apps: it refuses `full` when `eff_residency == "onprem_only"
AND eff_policy_mode != "passthrough"`. A non-`onprem_only` app with `pii_detection` left
at `presidio`, its own provisioned Langfuse project, and `capture_io: "full"` is
**accepted** — `tests/test_admin_api.py::test_policy_raw_capture_allowed_for_non_onprem_app_with_own_langfuse`
confirms this (no `policy_mode` in the request body, so it inherits `enforced`). That app
has declared no no-protection posture at all: `policy_mode` is `enforced` and
`pii_detection` is the fully-protective default. Either the rule overstates what the code
enforces, or the clamp is missing a residency-independent posture check. This is also
*why* the `docs/guides/faq.md` line fixed elsewhere in this same review read as plausible
before the fix — the code really does let an `enforced` app hold `capture_io=full` once
it is not `onprem_only`.

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

## B30 — Router's `capacity` scoring term is a constant, not a signal  ✅ RESOLVED (2026-09-19, P2a)

**Severity:** low (no incorrect ranking today, but the scoring function is less rich
than it presents itself, and `routing.yaml`'s weight implies otherwise)
**Found:** 2026-09-10, docs-reclassification Task 8; independently confirmed in review.

> **Resolved 2026-09-19 (P2a) — renamed and made real.** The original entry
> understated this. `capacity` was not merely hardcoded: it added the *same*
> constant to every candidate, so it cancelled out of the ordering and never
> influenced a single routing decision — changing `weights.capacity` from `0.2`
> to `20` would have changed nothing. It is now `availability`, computed from an
> EWMA over call outcomes (`app/routing/signals.py`). The rename is deliberate:
> true capacity (queue depth, admission pressure) is **unobservable across an
> OpenAI-compatible boundary**, so a term named `capacity` promised a measurement
> the protocol cannot deliver. `capacity` remains a deprecated alias in
> `routing.yaml` (renamed by a `RoutingCfg` validator with a boot warning), so no
> deployed config fails to start.
>
> The same slice replaced the other placeholder term: `latency` was `_L[tier]`, a
> per-tier constant, and is now an EWMA over observed latency with a
> `tier_default` fallback. Both terms, plus `quality` and `cost`, now report
> their provenance per candidate on `sp.scoring`. With an empty signal store the
> new formula reproduces the pre-P2a scores **numerically** — see
> `tests/routing/test_strategy_identity.py` and
> `docs/research/routing-signal-provenance.md`.
>
> Five limitations of the new signal, found while building it, are filed
> separately as **B38**–**B42** (plus **B43**/**B44**, an API-surface decision and
> a test-comment nit, and **B45**/**B46** from the final review). None is a
> regression, and every one is named in the research artifact's "what is not
> demonstrated" section. **B45 is the one to read**: the `availability` term is
> real now, but it is bounded by its weight (`0.2`), so a dead endpoint can still
> rank first — replacing an inert term with a measured one did not make it a
> health gate, and nothing here claims it did.

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
>
> **Update 2026-09-17.** The internal NAT agent and `nat_tools/` were deleted. The decision
> above is unchanged and now unconditional: the client-driven loop is the *only* path for
> real tools. B25's "what's wrong today" section below describes the removed build-time
> arrangement and is kept for the design constraints it records, not as current state.

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
probe-gated, no restart (`docs/guides/onboarding-models.md`). Tools should mirror that shape.

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

## B24 — `task_mode.decide()` crashes on a user message with `content: null` → `500 internal error` ✅ RESOLVED (2026-09-15)

**Severity:** low-medium (correctness; wrong status code on a legitimately-shaped request)
**Found:** 2026-09-05, docs-vs-code audit. **Resolved 2026-09-15.**

Fixed: task_mode coerces None content to ""; regression tests/test_task_mode_null_content.py and the conformance matrix.

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
`policy_mode: "passthrough"` for an `onprem_only` app; see `docs/reference/per-app-config.md`.
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

## B19 — Onboarded model with a slashed slug (`org/model`) can't be edited or deleted  ✅ RESOLVED (2026-09-14; fix written 2026-08-11 but not merged until then)

**Severity:** medium (resolved). Admin usability + a stuck-row trap; every
HF/Dynamo-style model id hits it.
**Found:** 2026-08-11, DGX lab, onboarding a Dynamo-served `Qwen/Qwen2.5-1.5B-Instruct`.
**Re-found:** 2026-09-14, DGX Spark, onboarding `nvidia/Qwen3.8-27B-NVFP4` — the
console's edit and delete both 404'd. The 2026-08-11 fix (`d48cd63`) had been
written and tested on `fix/b19-onboard-slug-routes` but that branch was never
merged, and this entry was lost when the backlogs were consolidated, so main
still shipped the bug. Cherry-picked onto main.

The edit/delete routes were declared `PUT`/`DELETE /admin/infra/models/{slug}`.
A bare `{slug}` path parameter matches `[^/]+` — it does **not** match a slash —
so real OpenAI-compatible ids (`Qwen/Qwen2.5-1.5B-Instruct`,
`meta-llama/Llama-3.1-8B-Instruct`, …) 404'd on the slash. The UI already sends
`encodeURIComponent(slug)` (`ui/src/api.ts`), but uvicorn percent-decodes `%2F`
back to `/` **before** Starlette routes, so any onboarded row with a `/` in its
slug was permanently un-editable and un-deletable through the API and the UI.
This compounded into a stuck-row trap: a bad-endpoint onboard (`POST`, slug in
the body — works fine) probes-unhealthy and is saved **disabled**; a re-`POST`
then hard-409'd, and PUT/DELETE couldn't reach it — so the only way to clear it
was deleting the row straight from the `onboarded_models` DB table.

**Fix (`app/api/admin_routes.py`).** Both routes now use the path converter —
`PUT`/`DELETE /admin/infra/models/{slug:path}` — so the slash is captured; the
UI's `encodeURIComponent` is unchanged and now round-trips. Additionally, `POST`
of an existing **disabled** slug is now an idempotent re-probe + upsert instead
of a hard 409, so a bad-endpoint onboarding self-heals via a clean re-POST with
no DB surgery. An already-**enabled** row still 409s (edit a working model with
PUT), and a yaml cloud-pool name still 409s. Regression tests
(`tests/test_onboard_api.py`): `test_slashed_slug_put_and_delete_roundtrip`
(POST→PUT→DELETE on an `org/model` slug end-to-end) and
`test_repost_of_disabled_slug_self_heals`.

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
- Trim the long-request share (`large_context`) in `tests/personas.yaml` for the burst demo — cheap, keeps the "500-req burst" moment clean. *(2026-09-17: the `multi_step` persona this originally named was replaced by `large_context` when the internal agent was removed; a burst is now N single calls, so the overlap this item describes is much reduced.)*
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
Cross-ref [`docs/archive/eval-logging-backlog.md`](../archive/eval-logging-backlog.md) and B14. Own SDD task.

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
`redacted` actually redacts. Cross-ref [`docs/archive/eval-logging-backlog.md`](../archive/eval-logging-backlog.md).

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
- **RLHF amendment (see B16 / `docs/roadmap/rlhf-readiness.md`):** `capture_io` gates only
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

**RLHF amendment (see B16 / `docs/roadmap/rlhf-readiness.md`):** feedback scores must be
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

**Update 2026-09-23 (P2b-2).** Still PARTIAL. The **router-decision** export now exists:
content-free per-request outcome records plus locally kept feedback scores, exported by
`GET /admin/outcomes/export` as NDJSON
([`docs/reference/routing-outcomes.md`](../reference/routing-outcomes.md)). `route_correct`,
`preferred_model` and `failure_type` are accepted score names. What remains is the
**LLM-side** dataset (corrected outputs → Langfuse datasets → TRL transform): the outcome
records deliberately hold no text, so they cannot serve it.

**Prior status 2026-08-16 (historical):** gaps 1 & 2 shipped; gap 3 still open.

**Type:** feature (future; design in `docs/roadmap/rlhf-readiness.md`).
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
residency-local** capture). See `docs/roadmap/rlhf-readiness.md` §2/§5.

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
and pipeline progression in `docs/roadmap/rlhf-readiness.md`. Own SDD task; plan first
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
- **Server-side multi-step** (step 4 → agent runtime): all hops landed in ONE request's
  `resp["trace"]` and ONE `RequestSummary` row (`app/api/history.py`, `hops` field).
  *Removed 2026-09-17* — this path no longer exists, and the sample never exercised it.
- **Client-side workflow** (the sample): N independent direct requests. This is now the
  only shape. Grouping them needs a correlation id the client sends.

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
