# Plan: per-app `policy_mode` (enforced | passthrough)

**Date:** 2026-09-05
**Driver:** MillWise (and, per the owner, "all the customers using routeForge don't
want PII and other checks"). RouteForge must support customers who use it purely as a
model router + token/cost meter + Langfuse tracer, with no PII detection and no
redaction in either direction — while staying the privacy-first product for customers
who do want that.

## The problem

`residency` currently conflates two independent concerns:

1. **Placement** — where compute may run (on-prem vs cloud).
2. **Protection** — how much the gate does to the data (detection, redaction, and the
   telemetry capture ceiling).

An app that wants "run only on my hardware, and don't touch my data" cannot say so.
Declaring `onprem_only` forces maximum protection, so the operator must instead set
`residency: "any"` + an `allowed_providers` pin — lying about placement to obtain a
protection posture. That is the workaround MillWise is on today, and it is strictly
weaker: a mutable allowlist replaces a hard floor, and `ctx.onprem_only` (which the
step-9 streaming floor and the `capture_io=full` clamp key on) goes false.

## The change

Split the two axes. `residency` keeps meaning **placement only**. A new per-app
`policy_mode` owns **protection**.

| `policy_mode` | Meaning |
|---|---|
| `enforced` (default) | Today's behaviour, unchanged. Privacy-first: detection, redaction, sensitivity escalation, capture ceiling. The hospital persona. |
| `passthrough` | Router + meter + tracer only. No PII detection, no input redaction, no egress redaction, no custom recognizers. Raw capture permitted to the app's **own** Langfuse project. Residency still pins routing. |

`onprem_only` + `passthrough` then becomes a coherent, honest configuration.

Everything that is not protection stays fully active in `passthrough`: routing and tier
selection, cost scoring, budgets, rate limits, usage metering, DecisionRecords, the
decision stream, and Langfuse tracing. That is the point — the customer still gets the
product.

## Non-negotiable guards (do not drop any of these)

- **Deployment ceiling.** `serving.yaml policy.allow_app_passthrough` (default `true`,
  env `SP_ALLOW_APP_PASSTHROUGH`). `false` forbids `passthrough` install-wide, for
  every app, regardless of per-app policy. Mirrors `allow_app_pii_downgrade`.
- **Never silent.** `policy_mode` is folded into `AppPolicyView.version()`, stamped in
  every response's `policy` block, and emitted on the trace. An unprotected app must
  never be unprotected invisibly, and flipping the mode must move the version stamp so
  a customer's conformance probe detects it (the stale-probe lesson).
- **Raw capture stays tenant-isolated.** `capture_io: "full"` continues to require the
  app's own provisioned Langfuse project. Passthrough relaxes the *residency*
  condition on `full`, never the own-project condition — one customer's raw data must
  never land in another's traces, or in the shared project.
- **`capture_io: "redacted"` remains incompatible with no-detection.** The redacted
  exporter path reuses the gate's sanitized prompt, which is raw when nothing was
  sanitized. `passthrough` + `redacted` is a 422, same seam as `pii_detection: off` +
  `redacted`.
- **Residency is untouched.** `passthrough` must not widen `allowed_providers`, must
  not clear `onprem_only`, and must not affect `app/policy/context.py`'s placement
  logic in any way. Rule 3 holds exactly as before.

## Deployment-wide default

`serving.yaml policy.default_policy_mode: enforced|passthrough` (env
`SP_DEFAULT_POLICY_MODE`), so an install where no customer wants PII checks sets it
once instead of per app. Per-app `policy_mode` is `null` = inherit the global default,
matching how `pii_detection` and `capture_io` already behave.

## CLAUDE.md rule 2 amendment

Rule 2 currently keys the raw-capture exception on residency ("a **non-`onprem_only`**
app ... `onprem_only` never permits it"). That encodes the assumption this plan
removes — that on-prem implies maximum protection. Re-key it on `policy_mode`: raw
capture is permitted for an app the operator has explicitly placed in `passthrough`,
to that app's own project, subject to the deployment ceiling. Rules 3 and 8 are
unchanged — they are about placement and tiers, not protection.

## Tasks

1. **Data + config layer.** `POLICY_MODES` enum; `AppPolicy.policy_mode` column;
   `store.upsert_policy` allowed-set; `AppPolicyView.policy_mode` + `version()`;
   `ServingCfg.default_policy_mode` + `allow_app_passthrough`; loader env overrides;
   `config/serving.yaml`. Tests: version reactivity, config load/override.
2. **Runtime behaviour.** Effective-mode resolution in `app/pipeline.py`; passthrough
   forces the null detector and skips custom recognizers on both the plain and
   tool-calling paths; egress redaction skipped; `policy_mode` stamped on the response
   policy block and the trace. Tests: labels/values survive verbatim, residency
   unchanged, custom recognizers do not fire.
3. **Admin API.** Validate `policy_mode`; ceiling clamp; re-key the `capture_io=full`
   clamp from residency to policy_mode (own-project condition kept); passthrough +
   `redacted` rejected. All clamps keyed on EFFECTIVE post-merge values, as the
   existing ones are. Tests for each 422 and for the newly-permitted state.
4. **Docs.** CLAUDE.md rule 2; `docs/per-app-config.md`; `docs/help/faq.md`; note the
   manual `ALTER TABLE app_policies ADD COLUMN policy_mode VARCHAR(16)` required on
   deployed Postgres (no auto-migrations — `create_all` never ALTERs).

## Verification

`LITELLM_LOCAL_MODEL_COST_MAP=True uv run pytest tests` must be green (baseline before
this work: 717 passed, 1 skipped).
