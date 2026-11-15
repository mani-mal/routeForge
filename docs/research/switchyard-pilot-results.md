# Switchyard pilot — results (B109)

**Verdict: GO** under the pre-registered primary analysis (run 5, 38 of 40 specs). The
verdict holds in both pre-registered sensitivity variants. As the protocol fixed in advance,
a GO at a −10 pp margin with N = 40 justifies **only a wider, shadow-monitored rollout, not
enforce-by-default**.

**Protocol:** `docs/research/switchyard-pilot-protocol.md` (pre-registered 2026-11-13; amended
§5a–§5h before each affected result). **Analysis:** `scripts/b109_pilot_analysis.py` over
`docs/research/data/b109-run5/` (stdlib only; `python3 scripts/b109_pilot_analysis.py
docs/research/data/b109-run5`). **Backlog:** B109.

## 1. What ran

| | |
|---|---|
| Run | `run5`, 2026-11-14T16:35:22Z → 2026-11-15T00:22Z |
| Plan | frozen plan sha `157e76ff…`, seed 375454972, task set sha `c2bce18e…` (40 specs: 20 issue-resolver, 20 ci-repair) |
| RouteForge | lab API 0.43.1 (B118 + B120), pod `routeforge-api-944c7776c-ddgwq`, **0 restarts for the whole run** |
| Switchyard | 0.4.0a1, arm D `adaptive_routing: enforce`, rate 1.0, lab `switchyard.timeout_ms: 250` |
| DevFoundry | 0.1.0-a7d3da6 (no deploys during the run); export client `b513202` |
| Export | `b109-run5-results.json`, sha256 `6cbe9353c8e207ba…` |

The runner was interrupted once (a WSL crash, ~20:24Z) and resumed from durable state at
20:26:25Z. Nothing was lost or submitted twice; the in-flight CI-02/B repair was adopted.

Earlier runs: runs 1, 2 and 4 are **void** and never analysed (§5c, §5d, §5h). Run 3 is a
**reported pilot** (§5f), summarised in §6 and never pooled.

## 2. Gates (primary analysis)

Excluded under §5a: **IR-16 and CI-15 (2 of 40, 5%)**, under the 10% INCONCLUSIVE line. Both
are glm upstream connect errors (B117) on single-endpoint arms; see §4.

| # | Gate | Result | Pass |
|---|---|---|---|
| 1 | Zero constraint violations in arm D | 0 of 580 D calls: every chosen model and every attempt inside the feasible set, every call on-prem under `onprem_only`, none below the risk floor | ✅ |
| 2 | D − C success, lower end of paired-bootstrap 90% CI > −10 pp | **+2.63 pp**, 90% CI **[−7.89, +13.16]** | ✅ |
| 3 | D/C cost per success, upper end of bootstrap 90% CI < 1.0 | **0.327**, 90% CI **[0.214, 0.488]** | ✅ |
| 4 | Advisor p99 ≤ 20 ms | **p99 3.14 ms** (nearest-rank; 2.80 interpolated), p50 0.68, max 16.27 ms | ✅ |

Validity rules: D timeout/error fallbacks **0 of 580** (rule: >5% → INCONCLUSIVE). Excluded
specs 5% (rule: >10% → INCONCLUSIVE).

Bootstrap: paired over specs (each resample draws whole specs with all four arms), percentile
intervals, B = 20 000, seed = the plan seed. The resample count and seed were not
pre-registered; they are stated here.

**Independent check.** A separate from-scratch recomputation, made without seeing these
numbers, matched every figure above. The only differences were gate-3 interval ends, in the
third decimal, from its different bootstrap seed. It also checked:
- all 2,890 b109 calls map to exactly one export session;
- §5a's mechanical rule (a failed task with `backend_failed` / `no_eligible` rows) gives the
  same excluded set.

Gate 1 counts the feasible set as the models the router scored plus those its own tier filter
set aside (`rejected` with `code: null`; see `docs/reference/routing-outcomes.md`).

### Per-arm metrics (38 specs)

| Arm | Success | Wilson 95% | Total cost | Cost / success | Calls / task | Tokens / task |
|---|---|---|---|---|---|---|
| A always-efficient | 37/38 (97.4%) | 86.5–99.5% | $9.54 | **$0.26** | 14.4 | 251k |
| B always-capable | 35/38 (92.1%) | 79.2–97.3% | $103.29 | $2.95 | 17.4 | 272k |
| C current router | 34/38 (89.5%) | 75.9–95.8% | $109.98 | $3.23 | 18.1 | 290k |
| D Switchyard | 35/38 (92.1%) | 79.2–97.3% | $36.97 | **$1.06** | 14.3 | 227k |

Per workload: issue-resolver A 18/19, B 16/19, C 16/19, D 16/19; ci-repair A 19/19, B 19/19,
C 18/19, D 19/19. D and C disagree on 5 specs: D only on CI-18, IR-12 and IR-19; C only on
IR-18 and IR-20.

Success = DevFoundry validation passed **and** the frozen HarnessLab `check.py` hidden-test check
passed, scored blind (random ids, sealed key opened after the manual verdicts were final).
Manually reviewed rows: A 0, B 1, C 1, D 1.

## 3. Sensitivity

| Variant | Specs | D − C (90% CI) | D/C cost per success (90% CI) | All gates |
|---|---|---|---|---|
| **Primary** | 38 | +2.63 pp [−7.89, +13.16] | 0.327 [0.214, 0.488] | **GO** |
| IR-20 doc hunk ignored | 38 | +5.26 pp [−2.63, +13.16] | 0.318 [0.208, 0.475] | GO |
| §5e gateway-infra scored as failure | 40 | +7.50 pp [−2.50, +17.50] | 0.238 [0.149, 0.419] | GO |

- **IR-20 (scoring).** IR-20/D passed the hidden tests (6/6) but also edited one row of
  `README.md`, outside `check.py`'s path allowlist. It was decided **blind**, before the key was
  opened: it fails as pre-registered, with "doc hunk ignored" reported as sensitivity. It is the
  only row that differs, and it is on arm D, so the primary is the conservative reading.
- **§5e.** IR-16/B and CI-15/C (the two gateway-infra sessions) are scored as failures for their
  arms, and the other arms on those specs as they finished. Both sessions failed anyway (1800 s and
  900 s timeouts), so this variant only adds the two specs back.

## 4. Infrastructure during the run

- **RouteForge:** 0 restarts, no stall, no 5xx of its own. Database and API logs agree: exactly
  four 502s across the run, all `glm-5.3-flash` "Connection error" after ~4.9 s on arms whose
  only eligible endpoint is glm, so no failover was possible (B117):
  - IR-16/B `att-5851cca4…`: 1× at 18:39:39Z. The session later hit its 1800 s timeout.
  - CI-15/C `att-05755c8e…`: 3× at 22:55:47Z, 22:56:00Z and 22:57:03Z. Its diagnosis later hit
    the 900 s timeout. B and D glm calls in the same minutes succeeded.
- Two further rows are `cancelled`: DevFoundry's own timeouts closing the client on those same
  two sessions. They are not gateway faults.
- Evidence: `data/b109-run5/gateway-infra.json`, `data/b109-run5/client-cancelled.json`.
  DevFoundry's export applies §5a's last bullet (a gateway entry makes a row infra only if the
  task failed); both tasks failed, so both specs are excluded.

### §5g cross-traffic report

- Non-b109 calls inside the run window: **131** (opencode 94, dev-foundary 37); **94** had
  prompts ≥ 50k tokens (all opencode, up to 160k).
- Per-call overhead (gap between a call's start and the previous call's end, which bounds gateway
  pre/post-processing plus client think time), p50 / p90 / p99 in seconds:
  A 2.61 / 4.07 / 5.31, B 2.67 / 6.19 / 12.56, C 3.16 / 5.30 / 14.58, D 2.56 / 3.94 / 5.34.
  The figures include 318 negative gaps (A 86, B 76, C 74, D 82), where the agent overlapped
  consecutive calls. They are kept, so the estimate is descriptive only.
- Timed-out sessions: **IR-16/B** (167 calls, overhead p50 4.0 s, max 130 s) and **CI-15/C**
  (181 calls, p50 4.5 s, max 7.3 s). **Neither overlapped a non-b109 call of ≥ 50k tokens. No
  session is flagged.**

## 5. Reading the result

- **Where the saving comes from.** D served 366 of 580 calls (63%) on the cheap model, against
  8 of 875 for C. C is effectively always-capable for this agent, as §7 of the protocol recorded
  before the run: OpenCode's long context puts the length-based target at mid on every call.
  D/C ≈ 0.33 is therefore largely D vs an all-mid baseline.
- **The floor and clamp did their job.** 214 of D's 580 proposals were changed: 172 `efficient`
  proposals were raised to mid by the risk floor on high/medium-risk calls, and 42 `capable`
  proposals were clamped from frontier to mid (no frontier on the lab). None went below a floor.
- **The cheap model saturates much of this task set.** Arm A (always-efficient) had the highest
  success (37/38) at the lowest cost ($0.26/success). The calibration stopping rule (§5b)
  anticipated this. It means the task set mostly answers the **cost** question: at equal or
  better success, D is about 3× cheaper per success than C. It says little about quality risk
  on tasks hard enough to need the capable model; a two-tier lab (cheap vs mid, no frontier)
  understates both the savings and the risk of a real cheap-vs-frontier pool.
- **The success intervals are wide.** Every arm's Wilson interval spans roughly 76–99%. Gate 2
  passes on its pre-registered margin, but N = 40 cannot resolve a difference smaller than ~10 pp.

## 6. Run 3 pilot (reported only, never pooled)

Run 3 (0.42.0) ran IR-01–IR-15 on all four arms, plus IR-16 on A and C, before it was stopped
(§5f). IR-05, IR-10, IR-12 and IR-15 were excluded as gateway infra (B118 stalls and one B117
connect error), and IR-16 is incomplete (IR-16/B was cancelled at the stop). Validation-only
success on the remaining 11 specs (no hidden-test check was run for the pilot):

| A | B | C | D |
|---|---|---|---|
| 11/11 | 7/11 | 10/11 | 11/11 |

That is the same direction as run 5. Source: DevFoundry `docs/research/data/b109-run3-pilot/`
(sha `3b1b0518…`); gateway evidence in `data/switchyard-pilot-run3-gateway-infra.json`.

## 7. Deviations and disclosures

All were decided before any gate was computed.
- **Export fixes (DevFoundry, client-only):** `ada2f92` applies §5a's last bullet to gateway
  entries (infra only if the task failed); `b513202` classes a CI diagnosis that timed out as
  `agent`, per §5a's "timeouts count as agent" (it was `system=no_decision` → infra). The only
  row affected by `b513202` is CI-15/C's platform class; the primary and §5e numbers are
  unchanged.
- **Blind ids** are 8 hex characters, not the proposed 12. This has no effect on blinding.
- **Manual review:** IR-12__6b7181ed and IR-18__684cfede were flagged "suspicious" only for
  agent-written `unittest` scaffolding in their own test files. Blind manual verdict: PASS
  (hidden 8/8 and 7/7).
- **IR-20** fails on the path allowlist, decided blind (§3).
- **Smoke criterion** for the 0.43.1 rollout was amended by owner decision before run 5 (§5h).

## 8. Findings outside the gates

- **Harness false positives (for future runs, not applied retroactively):** `check.py`'s
  suspicious-line scan fires on any agent that writes its own tests, and its allowlist rejects
  doc-only edits.
- **B117 is the remaining threat to validity.** Every run-5 exclusion was a glm connect error
  on a single-endpoint arm. RouteForge records no error class on the outcome row, so the cause
  was reconstructed from pod logs.
- **The capable model's runaway outputs** (`output_truncated`) caused 3 of the 5 agent
  failures (IR-12/C, IR-14/B, IR-16/C), on arms that serve glm (protocol §7).
- **Product:** `dev-foundary` under its real policy is still floored at mid by Presidio false
  positives on code (§5c). That is a cost lever independent of Switchyard.

## 9. Next steps

- **B111** (policy-safe routing-time calls) was gated on this GO and is unblocked.
- **Rollout:** shadow-monitored and per app only. Enforce for customer apps stays gated on
  Switchyard 0.4.0 final (B108's owner decision); this verdict covers 0.4.0a1 only.
- **B117:** record a content-free error class on `backend_failed` rows.
- **Optional confirmation:** a harder task set or a lab with a frontier tier, to test quality
  risk where the cheap model does not saturate.
