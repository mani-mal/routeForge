# Switchyard pilot — evaluation protocol (B109)

**Status:** pre-registered on 2026-11-13, before any arm was run. The margins and gates
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
- **Not analysed:** the calibration specs and the two arm C smoke sessions, all listed under
  `calibration_sessions` in the frozen task JSON.
- **No deploys during the run window.** Neither RouteForge nor DevFoundry is deployed while
  it runs.
- **ci-repair success** requires both the diagnosis and the repair to succeed. Cost per spec
  includes both steps' calls.

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
- **Alpha dependency:** 0.4.0a1. The verdict applies to this version only.
