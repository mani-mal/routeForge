# P3c-1 — Shadow prices: what each constraint costs

**Date:** 2026-09-26 · **Status:** approved design, not built
**Priority:** 3c, first sub-project. The order is shadow prices → scenarios → uncertainty →
capacity. Handoff doc §30 asks "Which constraint causes the largest business effect?"
**Follows:** P3a policy simulator (`app/eval/{scenario,proposal,simulate}.py`,
`scripts/policy_simulate.py`) and P3b conflicting core (requirement ids).

## 1. Purpose

For the operator's real recorded traffic, answer: **if this one constraint were relaxed, what
would change?** The things measured are cost, rejections avoided, quality, latency and on-prem
share. Every constraint an app actually sets is priced, one at a time, by replaying the same
rows through the real eligibility and routing code with only that constraint loosened.

Constraints tied to a security requirement are priced too, but reported separately as the
**cost of a protection**. They are never presented as a recommendation to relax anything, and
nothing is ever applied. This follows handoff §37: "Security constraints are never
automatically relaxed."

## 2. Non-goals

- No serving-code change. Everything is offline, in `app/eval`, built on the P3a scenario
  machinery.
- No joint relaxations: two constraints relaxed together is scenarios, P3c-2.
- No confidence intervals: uncertainty is P3c-3.
- No capacity effects: capacity is P3c-4.

## 3. Relaxations

A **relaxation** is one requirement, relaxed for one scope. It is applied to a *copy* of the
baseline snapshot doc and/or to the rows. There is no user-facing proposal and no validation
path.

**The baseline** is the current snapshot. When `--proposal` is also given, the baseline is the
current snapshot plus the proposal, so the operator can price the constraints of a proposed
policy.

| requirement id | scope | relaxed to | rows re-simulated |
|---|---|---|---|
| `residency` | app | the app's `residency: any` **and** `escalate_residency_on_detection: false` | that app's |
| `allowed_providers` | app | `null` | that app's |
| `allowed_regions` | app | `null` | that app's |
| `require_no_retention` | app | `false` | that app's |
| `raw_sensitive` | app | every endpoint that declares `accepts_raw_sensitive: false` flipped to `true` | that app's |
| `allowed_tiers` | app | `null` | that app's |
| `allowed_models` | app | `null` | that app's |
| `endpoint_app_allowlist` | endpoint | that onboarded endpoint's `allowed_app_ids: null` | all rows |
| `requested_model` | app | that app's rows replayed with `requested_model: null` | that app's |
| `max_cost_usd_per_request` | app | the app's `max_cost_usd_per_request: 1e12` | that app's |
| `risk_floor` | app | every `routing.risk_floors` entry set to `cheap` | that app's |

**Scoping rule.** A relaxation that edits deployment-wide data is still scoped to one app. This
applies to `raw_sensitive`, whose endpoint declarations are shared, and to `risk_floor`, whose
risk floors are global. For these, only the scoped app's rows are re-simulated under the
relaxed doc, and every other row is counted as unchanged by construction. The price is then
attributable to that app. `endpoint_app_allowlist` is the one endpoint-scoped relaxation, and
it re-simulates all rows.

**Candidates.** Relaxations are enumerated deterministically, ordered by app id, then the P3b
`REQUIREMENT_ORDER`, then endpoint slug. For each app that has at least one simulated row:

- `residency` when the app's residency is not `any`, or when its escalation is on and any of
  its rows has detected entities;
- `allowed_providers`, `allowed_regions`, `allowed_tiers` and `allowed_models` when set
  (non-null);
- `require_no_retention` when `true`;
- `raw_sensitive` when any endpoint declares `accepts_raw_sensitive: false`;
- `requested_model` when any of its rows carries a `requested_model`;
- `max_cost_usd_per_request` always, because the deployment default always applies;
- `risk_floor` when any of its rows' risk floor is above `cheap`.

Also, `endpoint_app_allowlist` for each onboarded endpoint with a non-null `allowed_app_ids`.

`--max-relaxations N` (default 50) caps the number evaluated. When the cap cuts the list short,
the report says how many were skipped.

**Invariant (monotonicity).** Every relaxation only widens eligibility. For every re-simulated
row:
- the relaxed eligible set is a superset of the baseline eligible set;
- a baseline-routed row is never rejected under the relaxation.

A test enforces both. `newly_rejected` is therefore always 0. If a run ever sees a non-zero
value, the CLI exits 2 with an internal-invariant error rather than reporting it.

## 4. Metrics

For each relaxation, compare the baseline arm with the relaxed arm over the re-simulated rows,
using P3a's existing per-row `ArmResult` and bucket-stat helpers. The metrics are:

- `rows_in_scope`;
- `route_changed`;
- `rejections_avoided`, the P3a `newly_served`;
- `cost_usd`: baseline, relaxed and delta;
- `cost_usd_both_routed`: baseline, relaxed and delta over rows routed in both arms;
- `cost_saved_per_day`: `-(both-routed delta) / window_days`, or null when the export window is
  under 1 day. This is like-for-like, so rows the relaxation newly serves are not counted as
  added cost; their cost is in `cost_usd`;
- `quality_mean` delta;
- `latency_ms.p50` delta;
- `onprem_share`: baseline and relaxed;
- `affected_apps`: the apps with any route change or avoided rejection;
- `moves`: the top 5 `(from → to)` pairs.

A relaxation whose rows all route identically is **non-binding**. Non-binding relaxations are
counted, not listed.

## 5. Output

**CLI.** `scripts/policy_simulate.py` gains `--shadow-prices` and `--max-relaxations N`.

- With `--shadow-prices`, `--proposal` becomes optional. Without a proposal the ordinary
  baseline-vs-proposal sections are omitted, and the baseline is the snapshot.
- Exit codes are unchanged: 0 ok; 2 input or internal-invariant error; 3 new rejections from
  the proposal when `--fail-on-new-rejections` is set.

**`simulation.json` block `shadow_prices`:**

```json
{
  "baseline": "snapshot" | "snapshot+proposal",
  "evaluated": 12, "skipped_by_cap": 0, "non_binding": 7,
  "constraints": [
    {"requirement": "allowed_tiers", "scope": {"app": "acme"}, "security": false,
     "relaxed_to": "null",
     "rows_in_scope": 120, "route_changed": 30, "rejections_avoided": 4,
     "cost_usd": {"baseline": …, "relaxed": …, "delta": …}, "cost_saved_per_day": …,
     "quality_mean_delta": …, "latency_p50_delta": …,
     "onprem_share": {"baseline": …, "relaxed": …},
     "affected_apps": ["acme"], "moves": [{"from": …, "to": …, "count": …}]}
  ]
}
```

- Only binding constraints are listed.
- Ordering is `(-(cost_saved_per_day or -delta), -rejections_avoided, requirement, scope)`.
- `security` is `true` for the P3b `SECURITY_IDS`.
- The block is deterministic: sorted keys, floats rounded to 6 places.

**`report.md` section "Shadow prices".** It opens with this caveat, verbatim:

> **What each constraint costs.** Each row relaxes ONE constraint for one app (or endpoint) and
> replays the same recorded traffic. Nothing is applied. Security constraints are shown as the
> cost of a protection — they are never a recommendation to relax them.

It then has two tables:
- **Operational constraints:** non-security, ranked.
- **Cost of protection:** security ids, the same columns, each row labelled "price of a
  protection — not a recommendation".

A footer gives the non-binding count and the number skipped by the cap.

## 6. Components

| Unit | File | Role |
|---|---|---|
| Relaxations | `app/eval/shadow.py` (new) | A `Relaxation` dataclass holding `requirement`, `scope`, `relaxed_to`, and a `apply(doc, rows) -> (doc, rows_in_scope)` function. `enumerate_relaxations(doc, rows, *, cap)` enumerates candidates. |
| Evaluation | `app/eval/shadow.py` | `async shadow_prices(records, baseline_doc, *, base, cap) -> dict`. It builds the baseline scenario once. Per relaxation it builds the relaxed scenario, re-simulates the in-scope rows with `simulate_row`, checks the monotonicity invariant, and computes the §4 metrics. Every scenario is closed in a `finally`. |
| Summary and render | `app/eval/shadow.py` | `render_shadow_section(block) -> str` |
| CLI | `scripts/policy_simulate.py` | The two flags, wiring, and exit code 2 on an invariant breach. |

`app/eval` may import serving code; serving never imports `app/eval`.

## 7. Tests

1. **Per-requirement relaxation.** For every requirement id, a fixture where that constraint
   binds gives `rejections_avoided > 0` or `route_changed > 0` for exactly that relaxation.
2. **Non-binding.** A constraint that never binds, such as `allowed_tiers` listing every tier,
   gives zero deltas and is counted in `non_binding`, not listed.
3. **Scoping.** Relaxing app A's `residency` never re-simulates or changes app B's rows. The
   same holds for `risk_floor` and `raw_sensitive`, which edit shared data.
4. **Monotonicity (Hypothesis).** Across random fixtures and relaxations, the relaxed eligible
   set is a superset of the baseline set and `newly_rejected == 0`.
5. **Security separation.** Security ids appear only in the protection table, and the caveat
   and per-row label are present.
6. **Cap.** `--max-relaxations 2` evaluates 2 and reports the rest as skipped.
7. **Determinism.** Byte-identical output across two runs.
8. **CLI.** `--shadow-prices` without `--proposal` works. An injected invariant breach
   (monkeypatched) exits 2.
9. **Live round trip.** Real requests through `TestClient` feed the export and snapshot, and
   the shadow-price run on them gives a binding `allowed_tiers` entry for an app restricted to
   one tier.

## 8. Documentation

- `docs/reference/policy-simulator.md`: a new "Shadow prices" section. It covers the
  relaxation table, the scoping rule, the metrics, the two tables and the non-recommendation
  rule.
- `docs/research/feasible-route-bench.md` §17: the model (one-at-a-time relaxation over
  recorded traffic, scoped attribution), the monotonicity invariant, and a Spark results slot
  ("Pending the Spark run").

## 9. Deployment

This is offline tooling only: no schema change and no serving change. After merge, run
`--shadow-prices` once against the Spark's real snapshot and export, record the results in §17,
and deploy the image so the tooling ships with it. That is a routine rebuild with no ALTER.
