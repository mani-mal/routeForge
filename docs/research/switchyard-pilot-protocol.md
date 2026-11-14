# Switchyard pilot — evaluation protocol (B109)

**Status:** pre-registered on 2026-11-13, before any arm was run. **Task set frozen 2026-11-13T17:27:21Z.** The margins and gates
below are fixed now. Changing them after results exist must be recorded in this file, with
the reason. **Backlog:** B109. **Design:** `docs/architecture/switchyard-advisor-design.md`.
**Results:** go to `docs/research/switchyard-pilot-results.md`.

## 1. Question

Within RouteForge's feasible set, does NVIDIA NeMo Switchyard's stage router lower the cost
per successful agent task without a material loss in task success, compared with
RouteForge's current router?

Hard constraints (zero violations): it never selects outside the feasible set, never falls
back outside it, never serves below the hard floor, and never sends an `onprem_only`
request to the cloud.

## 2. Workload

RouteForge can't see whether an agent's task succeeded: agent loops are client-driven. The
benchmark therefore comes from a client that does see it. The client is **DevFoundry**, a
coding-agent harness (OpenCode) that already sends real traffic through the DGX lab, about
2,300 requests a week, mostly `issue-resolver.implement` and `ci-repair.*`.

- **Task set:** a fixed set of N DevFoundry tasks, chosen **before** any arm runs and
  frozen by id in `docs/research/data/switchyard-pilot-tasks.json`. The target is N = 40,
  split between `issue-resolver` and `ci-repair`, and taken from tasks DevFoundry already
  has validators for. They must be synthetic or sample repositories, with no customer code.
- **Frozen 2026-11-13T17:27:21Z.** `docs/research/data/switchyard-pilot-tasks.json`, sha256
  `c2bce18e414796b661864ff0891a0c6c9b5425868cbad2c84a2f78d33c93f2ff`. It holds 40 specs (20 IR
  + 20 CI, each split 7 trivial / 7 moderate / 6 harder) on `mani-mal/b109-eval-tasks` at
  `1660fcf` (tag `b109-base`).
  - **Seed:** `375454972` = `int(main_sha[:8], 16)`, so it couldn't be chosen in hindsight.
  - **Arm order:** fixed per spec.
  - **Pins:** identical across arms (packs, environment image digest, policy v2).
  - **Excluded:** 30 `calibration_sessions`.
  - **Hashes only:** request texts and hidden tests are not in this public repo.
- **Success:** DevFoundry's automatic validation verdict (tests/CI pass), plus a scripted
  per-task check. A human review verdict isn't practical across four arms and isn't used.
  RouteForge records neither, so DevFoundry reports them per task, per arm.
- **Isolation (DevFoundry side):** one DevFoundry project per arm, each bound to that arm's
  RouteForge eval-app credential. These projects have no research rights, and their tasks
  are tagged `run_purpose=eval` (DevFoundry F-214) so they stay out of DevFoundry's
  research dataset.
- **Repeats:** each task runs once per arm. Run order is randomized per task, so model-load
  drift isn't confounded with the arm.

## 3. Arms

Each arm is a dedicated RouteForge **eval app** on the DGX lab with its own token. **Every
arm sends `model: "auto"`.** DevFoundry's production app pins every role through
`model_aliases` (`df.* -> glm-5.3-flash`). A pinned request bypasses tier choice, so with
pins neither the router nor Switchyard could change anything. The eval apps therefore have
**no aliases**, and the client request is identical across arms. Each app copies
`dev-foundary`'s policy exactly: `onprem_only`, `passthrough`, `pii_detection: presidio`,
`verified_then_stream`, `model_input`/`client_output: raw`, `content_integrity: exact`,
`capture_io: redacted`, `compression_enabled: false`, and `max_cost_usd_per_request: 2`. Only
the setting under test differs, and **all arms share the same two-model pool**:

| Arm | `allowed_models` | `adaptive_routing` | Meaning |
|---|---|---|---|
| A. always-efficient | `[qwen38-cheap-b109]` | off | lower bound on cost |
| B. always-capable | `[glm-5.3-flash]` | off | upper bound on quality |
| C. current router | `[qwen38-cheap-b109, glm-5.3-flash]` | off | baseline |
| D. Switchyard | `[qwen38-cheap-b109, glm-5.3-flash]` | `enforce`, rate 1.0 | treatment |

**Cheap = `qwen38-cheap-b109`** (the real Qwen3.8-27B server, onboarded a second time as
tier `cheap` through `deploy/dgx_lab/alias-proxy-qwen.yaml`, with `allowed_app_ids` set to the
four eval apps only). **Mid = `glm-5.3-flash`.** The lab's `glm-flash-econ` was rejected as
the cheap model. It is a price alias of `glm-5.3-flash`, the same model, so quality would be
identical across arms by construction.

**Live check (2026-11-13, 0.42.0).** Arms C and D were sent a short request carrying
repeated test failures. C (length heuristic) served `qwen38-cheap-b109`, while D (Switchyard,
applied) escalated to `glm-5.3-flash`. On a plain question both served cheap. The lab has no frontier
model, so `capable` clamps to mid, the strongest model deployed there. The `dev-foundary`
production app is never switched to enforce; it has no `adaptive_routing` setting, so it
stays in shadow under the lab's `enforce` ceiling.

## 4. Metrics

Per arm, overall and per workload:
- **task success rate**, with a Wilson 95% interval;
- **total cost** and **cost per successful task**, priced from RouteForge's outcome rows;
  cost includes every call of the task;
- **calls per task** and **tokens per task**;
- **routing overhead**: `sp.switchyard.latency_ms` p50/p99 for arm D;
- **advisor behaviour** (arm D): role split, the share of calls where the floor or clamp
  changed the proposal, and the fallback causes;
- **constraint violations**: selections or fallbacks outside the feasible set, and
  floor/placement breaches. These come from the trace candidates and the outcome rows.

## 5. Go/no-go (fixed now)

**GO** requires all of the following:
1. **Zero** constraint violations in arm D.
2. **Non-inferior success:** D's success rate minus C's is above −10 percentage points at
   the lower end of a paired bootstrap 90% interval over tasks.
3. **Cheaper:** D's cost per successful task is below C's, with the upper end of the
   bootstrap 90% interval on the ratio D/C under 1.0.
4. **Overhead within budget:** advisor p99 is at most 20 ms.

Anything else is **NO-GO**, or **INCONCLUSIVE** when the intervals are too wide to decide
at N = 40. INCONCLUSIVE may extend N once, to 80, and the extension is recorded here.

**Why −10 pp.** With N = 40, a tighter margin can't be resolved. The gate is set before
seeing data, so it can't be tuned toward a result. A GO at this margin justifies only a
wider, shadow-monitored rollout, not enforce-by-default.

### 5a. Exclusions and infrastructure failures

These rules were added 2026-11-13, before any arm ran. They are procedural and change no gate.

- **Single attempt.** Each arm gets exactly one attempt per spec. DevFoundry enforces this
  with `task_budget_max.attempts = 1`, so agent-level and queue retries can't give one arm an
  extra try.
- **Infrastructure failures are excluded symmetrically.** If a spec's attempt in any arm
  fails for an infrastructure reason, that spec is excluded from all four arms. That covers
  a worker crash or restart, a RouteForge 5xx / `provider_unavailable` / dropped stream, or a
  model-server outage, as opposed to the agent's own work. Evidence comes from DevFoundry's
  `failure_class` (`none | agent | infra`), cross-checked against RouteForge outcome rows for
  those sessions. Exclusions are reported with their count. **If more than 10% of specs are
  excluded, the run is INCONCLUSIVE.**
- **Classification is fixed in advance, with no post-hoc reclassification.** Infra wins on
  disagreement, and anything unmapped counts as infra. For ci-repair, a row is infra if
  either step is. **Timeouts count as `agent`**, because a slow model is part of that arm's
  behaviour. The one exception is mechanical: a session whose RouteForge outcome rows show
  `backend_failed`, `no_eligible` or a 5xx is infra.
- **Final failure-class mapping** (DevFoundry `dc8d70e`, taken on attempt 1, first match
  wins):
  - **infra:** an `agent.error` with HTTP 5xx or 429; then infra state reasons (`worker_lost`,
    `lease_expired`, `stale`, `worker_error`, `tree_not_verified`); then the categories
    `provider_unavailable`, `capacity`, `infrastructure`, `environment` and
    `source_authorization`; then anything unmapped, a hand cancel, or `no_attempt`.
  - **agent:** `agent_error`, `validation_failure`, `timeout`, `budget_exhausted` (the task's
    own token, cost or wall budget), `no_change` and `output_truncated`.
  - **none:** `completed` or `awaiting_review` on the arm's own patch.
- **Repair not offered** (ci-repair; DevFoundry `7dd4d73`). If the arm's own diagnosis judged
  the failure not repairable, the row is `agent` (`repair_not_offered: diagnosis=<verdict>`).
  If the spec is repairable but no repair was offered for a system reason, the row is
  `infra` (`repair_not_offered: system=<reason>`).
- **Guard refusal** (DevFoundry `c23c7f4`). A repair refused by DevFoundry's ci-repair guard is
  `agent` (`guard_refused:<rules>`). It is the arm's own patch tripping a documented rule
  that every arm faces. Every CI reference fix passes the guard with 0 violations. Other
  `policy_denied` causes are infra.
- **Manual review is blind to the arm.** The checker flags patches that look like attempts to
  forge the hidden-test result. Before any verdict, patches are renamed to random ids and the
  id→arm key is sealed. The reviewer records the verdict, reason, reviewer and time before
  the key is opened. A patch found forging counts as `agent` failure for its arm. Manually
  reviewed rows are counted per arm, and if they decide the gate, that is called out.
- **Only DevFoundry's export sessions are analysed.** RouteForge-originated test calls
  (no session) are never joined.
- **Not analysed:** the calibration specs and the two arm C smoke sessions, all listed under
  `calibration_sessions` in the frozen task JSON.
- **No deploys during the run window.** Neither RouteForge nor DevFoundry is deployed while
  it runs.
- **ci-repair success depends on the outcome** (amended 2026-11-13, before freezing). A spec
  succeeds when its repair passes both validation and the hidden tests. A failure in the
  diagnosis step is kept in `failure_evidence` and doesn't by itself fail a spec whose repair
  succeeded. Cost per spec includes both steps' calls.
- **Transient backend errors on a successful task don't count as infra.** A session with
  `backend_failed` calls whose task still succeeds stays `none`. Only a failed task with
  such rows is infra.

### 5b. Calibration log and stopping rule

- **Round 1** (15:26–15:45Z, the original repo). The issue-resolver specs hit a ceiling: both
  arms passed both of them. The ci-repair rows are void because of two DevFoundry bugs
  (BUG-137 branch detection, BUG-138 `diagnosed` unmapped), both fixed. → The repo was rebuilt
  from scratch to avoid diff-to-answer leakage, and the issue-resolver specs re-tiered 7/7/6.
- **Round 2** (16:09–~17:05Z, rebuilt repo `4d93770`). All 8 pairs passed both checks on A
  and B. The cheap arm passed both hard issue-resolver specs. → One more hardening round:
  root causes two or more calls away in another module, behaviour pinned only in docstrings,
  and CI failures whose test points at the wrong module.
- **Round 3** (17:0x–17:15Z, repo `1660fcf`). CAL-07 passed on both arms. CAL-08: A failed
  the hidden tests, B passed — a clean cheap-model miss. **The stopping rule is satisfied.**
  CAL-09 exposed a spec-validity bug rather than a difficulty one: stale-assertion CI specs
  can't be repaired under the guard's no-test-changes rule. CI-01, CI-13 and CI-15 were
  replaced by same-tier failures whose correct repair is in source, with no recalibration.
- **Stopping rule.** Round 3 is the last. If the cheap arm still passes every hard
  calibration spec, the set is frozen anyway, and "the cheap model saturates this task set"
  is reported as a finding. B109 then mainly answers the cost question (D vs C at equal
  success).

### 5c. Run 1 voided: risk floor made the cheap tier unreachable (amended 2026-11-13)

The first frozen run started at 18:03:31Z and **was stopped after IR-10 (IR-11 partial)**.
Wiring was correct, and arm D showed `applied` on every call. But every call was classed
`confidential` → `medium` risk, because Presidio detects `PERSON`, `LOCATION` and `DATE_TIME`
(plus `URL` and `US_DRIVER_LICENSE`) in source code and agent context. The deployment's
`risk_floors.medium = mid` therefore raised every target to mid. Arm D's `efficient` proposals
were applied and then floored back to mid, so **D was identical to C by construction**.

**Amendment (owner decision).** All four eval apps get the same
`pii_entity_denylist: [PERSON, LOCATION, DATE_TIME, URL, US_DRIVER_LICENSE]`. This is the
per-app, surgical "detect-but-exempt" lever: exempted types are dropped before redaction and
classification. Detection otherwise stays Presidio, and placement stays `onprem_only`. The task
repo is synthetic with no real PII, and US_SSN, cards and email are still detected. A live check
after the change classed a code snippet with names, URLs and dates as `public`/`low`, and both
C and D could serve the cheap tier. **This is the one deliberate difference from the
`dev-foundary` policy.** It is applied identically to every arm.

**Void data.** The run-1 sessions (44, listed in `data/switchyard-pilot-void-run1.json`) are
excluded. They were never analysed: they were inspected only for routing correctness. The
frozen task set, seed and arm orders are unchanged. Run 2 starts from spec 1 with fresh plan
state.

**Product finding (independent of B109).** Under its real policy, `dev-foundary`'s traffic is
floored at mid by the same Presidio false positives on code. That is a cost lever, through the
same denylist, outside this experiment.

### 5d. Run 2 voided: advisor timeouts turned D into C (amended 2026-11-13)

Run 2 started at 21:26:08Z and was **stopped after IR-01 (IR-02 partial)**. The C and D rows
were low risk with the cheap tier reachable, which confirmed the §5c fix. On IR-01's arm D,
Switchyard advised and applied `efficient` on the first 3 calls. From about 9.2k routing tokens
onwards, **every call fell back with `fallback_cause: timeout`** (20 ms budget), and the length
rule then picked mid. Measured in isolation inside the same api pod, the advisor takes 0.4–3.2
ms even at 100 tool turns. The timeouts were therefore event-loop contention on a 2-CPU pod
shared with concurrent lab traffic, not the advisor's own cost.

**Amendments (owner decision).**
- The lab sets `switchyard.timeout_ms: 250`. The advisor's real per-call latency
  (`sp.switchyard.latency_ms`, which includes any loop wait) is still recorded and still
  judged by gate 4 (p99 ≤ 20 ms).
- **New validity rule for arm D.** If more than **5%** of D's calls in the analysed run have
  an advisor fallback of `timeout` or `error`, D's result is **INCONCLUSIVE**. That is reported
  with the rate, not scored as D=C. A load regression can't silently turn the treatment into
  the baseline again. (Suggested by DevFoundry.)
- The run-2 sessions (6, in `data/switchyard-pilot-void-run2.json`) are void and never
  analysed. Run 3 restarts from spec 1 with fresh state and the same frozen plan.

### 5e. Run 3: gateway stalls correlated with arm (amended 2026-11-14, before analysis)

**Finding.** During run 3, the lab api pod was killed by its liveness probe 10 times between
00:01Z and 02:57Z. The policy gate runs Presidio synchronously on the event loop. On very
large prompts that blocks the loop for about 16 s per request, so `/health` times out (B118).
The exact kill times come from Prometheus (`kube_pod_container_status_last_terminated_timestamp`).
They map to three sessions: IR-05/B, IR-12/C and IR-12/B. A fourth, IR-10/B, received a
gateway 502 with no kill. Under §5a all four are infra, so IR-05, IR-10 and IR-12 are
excluded from every arm. DevFoundry could not see the drops itself, because OpenCode retried
the dropped streams silently. The evidence is RouteForge's, passed to the export as
`--gateway-infra`. The run-2 advisor timeouts (§5d) were very likely the same loop stall.

**Why this matters.** These infra events are **not independent of the arm**. Huge prompts
come from GLM looping in B and C (6–14M input tokens per session), and every excluded
session was a looping B/C session that ended in an 1800 s timeout. Symmetric exclusion
therefore removes B/C failures and inflates B and C relative to A and D. For the D-vs-C
gates that bias runs **against** D: C's success rate rises, and C loses expensive failures
from its cost per success. (Suggested by DevFoundry.)

**Amendments (fixed before any gate is computed).**
- **Primary analysis is unchanged.** §5a exclusion and the >10%-of-specs INCONCLUSIVE rule
  decide the verdict.
- **Required sensitivity analysis.** Every gate is also computed with gateway-infra specs
  included and each gateway-infra session scored as a **failure** for its arm. The other arms
  on that spec are scored as they finished. Both results are reported side by side, along
  with the count of specs lost and the arms of the sessions that caused each loss.
- **How the two combine.** The verdict comes from the primary analysis. If the primary is
  GO, it stands, because the exclusion bias runs against D. If the primary is NO-GO or
  INCONCLUSIVE but the sensitivity analysis would pass every gate, the results doc says so
  explicitly, and the recommended next step is a confirmation run on a build with B118
  deployed. It is not a GO.
- Runs after run 3 must use a build with B118, the gate off the event loop.

### 5f. Run 3 stopped as a pilot; run 4 on the B118 build (amended 2026-11-14, owner decision)

**Why.** By IR-16, run 3 had lost 4 of 40 specs to gateway infra under §5a: IR-05, IR-10,
IR-12 and IR-15. A fifth loss makes the primary INCONCLUSIVE. Every loss came from a B or C
session where GLM looped into a huge prompt. Two failure modes caused them. The first is the
B118 event-loop stall: 10 liveness kills, plus 502s after the stall. The second is one
upstream connect error (IR-15/B, 04:00:48Z, B117). Both scale with prompt size, and at the
observed rate about 6 more of the remaining 24 specs would have been lost. The evidence is in
`data/switchyard-pilot-run3-gateway-infra.json` (5 sessions, matched by DevFoundry's export).

**Decision (owner, 2026-11-14).**
- **Run 3 stopped** at 13:24Z. IR-01 to IR-15 ran on all four arms, and IR-16 on A and C;
  IR-16/B was cancelled in flight. Run 3 is a **reported pilot**: it is summarised in the
  results doc, with §5e's sensitivity view where useful, but it is **never pooled with run 4
  and never decides the verdict.**
- **The lab moves to 0.43.0**, which contains B118: detector work runs off the event loop.
  A large-prompt smoke must show `/health` answering during a long Presidio pass before
  run 4 starts.
- **Run 4** uses the same frozen task list and plan (sha `157e76ff…`, seed 375454972, the
  same order), the task repo at `1660fcf`, fresh DevFoundry state and the label `run4`. All
  §5a–§5e rules apply unchanged. Sharing the order with the pilot means the pilot cannot be
  used as an order-effect check; it was never meant to be one.
- B117 connect errors can still exclude specs in run 4. If run 4 is INCONCLUSIVE on
  exclusions alone, the next step is B117, not another rerun.

## 6. Procedure

1. **Freeze inputs:** the RouteForge image/version, the Switchyard version (0.4.0a1),
   `routing.yaml` and the model set. Record the commit SHAs here.
2. **Create the four eval apps** with the policies in §3.
3. **Freeze the task list** (§2) and commit it.
4. **Run DevFoundry** over the task list once per arm, in randomized order, against each
   arm's token.
5. **Collect results.** DevFoundry provides the per-task verdicts. RouteForge provides the
   per-app outcome rows, through the admin export or the shadow report.
6. **Analyse** with a script committed next to the results, then write
   `switchyard-pilot-results.md` with the GO / NO-GO / INCONCLUSIVE verdict.

## 7. Threats to validity

- **Small N:** the intervals will be wide. The gate is accordingly modest.
- **Coding-only workload:** stage routing is tuned for coding agents, so this is its best
  case, not a general verdict.
- **Concurrent lab traffic.** At the owner's choice, OpenCode's lab-sample work runs on the
  same worker slots and the same `glm-5.3-flash` backend during the run. That adds
  time-varying load, mitigated by running all four arms of a spec back to back in a random
  order. It also inflates the recorded advisor latency through event-loop contention.
- **Two-tier lab:** with no frontier model, the efficient-vs-capable spread is
  cheap-vs-mid. That understates both the savings and the quality risk of a real
  cheap-vs-frontier pool.
- **Stateless advisor:** B106 is blocked upstream, so no holds are carried across turns.
  Upstream's own numbers assume them.
- **The baseline routes nearly everything to mid for this agent.** In arm C's smoke runs
  (2026-11-13, sessions `att-64c4…` and `att-e6a4…`, 11 calls, before any arm ran), every
  call targeted and was served by `glm-5.3-flash`. OpenCode's long system prompt and history
  put the length-based target at mid on every call. For this workload, C is therefore
  close to B (always-capable): D's cost gate is in effect measured against an almost
  all-mid baseline. This is a real property of the current router, recorded here before
  any results. It doesn't change the gates.
- **Residual forgery risk.** The patched code runs in the same process as the hidden tests,
  so a deliberately obfuscated forgery can't be ruled out. Several things mitigate it:
  agents never see the checker or the hidden tests; binary patches and suspicious names are
  refused or flagged for blind review; and success also requires visible validation. The
  risk is symmetric across arms. It was accepted rather than rewriting all 44 hidden suites.
- **The capable model's long-context repetition.** On the lab, about 0.3–0.4% of
  `glm-5.3-flash` calls run away to the 32k output cap (degenerate repetition, visible as
  speculative-decoding acceptance of 1.0). The rate per call is steady since 11-11, and the
  server is unchanged and lightly loaded. It is treated as a property of the capable model
  wherever it serves (B, C, and D's mid calls), not as infrastructure. Some of those
  truncations are labelled `tool_calls` by vLLM rather than `length` (B116). That isn't
  fixed during the run, so the system stays fixed.
- **Alpha dependency:** 0.4.0a1. The verdict applies to this version only.
