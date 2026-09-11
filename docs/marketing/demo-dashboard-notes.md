# Reading the fleet dashboard — PII vs on-prem, residency, and the ⚠️ warning

Notes captured while running the PCAI demo (2026-07-28). They explain a question that
looks like a bug but isn't: *why does an app show many PII requests but 0 kept on-prem,
with a yellow warning?* Keep this for demo narration and to avoid re-deriving it.

## What the "Run burst" / `POST /api/traffic` actually does

The **burst** (UI "Run burst" button, or `POST /api/traffic {requests,rps}`) triggers the
**internal test traffic generator** — `tests.traffic.generator.run_traffic(seed=42)` —
*inside the API pod*. It samples the weighted persona mix in `tests/personas.yaml` and
self-calls `/v1/chat/completions` over real HTTP, so the dashboard fills exactly as it
would from a real operator. It is fire-and-forget (returns `{"status":"started"}`) and
keeps running after any client that started it disconnects.

A direct `POST /v1/chat/completions` (with a `Bearer <app-token>`) is **not** the
generator — it's one real request with whatever prompt you send. Use the burst for
aggregate proof; use a single `/v1/chat/completions` call to narrate one decision.

## PII detected ≠ must be on-prem

Residency is decided by **two** things, and raw PII presence is neither:

1. **The entity's risk class** (`config/policy.yaml → sensitivity_rules`):

   | Entities detected | Sensitivity | Residency |
   |---|---|---|
   | `US_SSN, CREDIT_CARD, US_BANK_NUMBER, MEDICAL_LICENSE, EMPLOYEE_ID` | **restricted** | **onprem_only** (+ require_verification) |
   | `PERSON, EMAIL_ADDRESS, PHONE_NUMBER, LOCATION, DATE_TIME` | **confidential** | **private_cloud_ok** (cloud eligible) |
   | none | public | any |

2. **The tenant's own residency** (`policy.yaml → tenants`):
   `acme → private_cloud_ok`, `hospital → onprem_only`, `default → any`.

A request is forced on-prem **iff** its entities are `restricted` **or** its tenant is
`onprem_only`. Low/medium-risk PII on a `private_cloud_ok` tenant is **redacted on egress
and allowed to (private) cloud** — correctly *not* kept on-prem.

## Worked example (the 407 / 161 PII / 0 on-prem Acme row)

- Acme's personas ask innocuous things — *"capital of France?"*, *"day after Monday?"*.
  Presidio flags `France` as **LOCATION** and `Monday` as **DATE_TIME** — real PII entity
  *types*, so they count in the **PII** column (~161 of 407).
- Those land in the **`confidential`** bucket → residency **`private_cloud_ok`**. Acme's
  tenant residency is also `private_cloud_ok`. So: detected → redacted → allowed to cloud
  → **0 on-prem. Working as designed.**
- Acme never generates `restricted` entities (SSN, card, bank #, …) — those personas all
  run as **hospital**. That's why **General Health = 92 PII / 92 on-prem**: it's the
  `onprem_only` tenant, so *every* request stays local regardless of entity.

**This is the good demo point:** routing is nuanced and policy-aware — low-risk PII to a
cloud-eligible tenant is redacted and allowed to cloud; high-risk PII (or an `onprem_only`
tenant) is forced local. It is *not* a blunt "all PII on-prem".

## The yellow ⚠️ ("Sensitive traffic not all kept on-prem") is over-eager

It fires from a deliberately simple rule (`ui/src/console/FleetOverview.tsx:51`):

```js
warn: pii_count > 0 && onprem_count < pii_count
```

i.e. "this app had PII-flagged requests that didn't all stay on-prem." For Acme
(161 PII, 0 on-prem) → `0 < 161` → warns. But for a `private_cloud_ok` tenant carrying
only `confidential` entities that is **compliant**, not a leak. The heuristic conflates
"any PII entity detected" with "sensitive data that must be on-prem", and so it *undersells*
the exact story you want to tell.

**Fixed 2026-07-30 (`docs/archive/ui-open-items.md` #6).** The warning now fires only on a genuine
residency breach — an `onprem_only` request served **off-prem**
(`fleet()['onprem_violations']`, red glyph). Because `onprem_only` requests can't reach
cloud by construction (Rule 3), this is 0 in the demo: Acme no longer warns, and the glyph
now means a real leak if it ever shows. So the narration above ("PII detected ≠ must be
on-prem") is exactly what the dashboard now reflects, instead of contradicting it.

## Verified 2026-09-10

Re-ran the exact mechanism this doc describes against a live app (`POST /admin/login`,
then `run_traffic(base_url, n=500, rps=200, seed=42, ...)` — the same call
`/api/traffic` makes) rather than trusting the numbers as written:

- **`/api/traffic` and the persona mix**: confirmed against `app/api/ui_routes.py` —
  the endpoint body is `{requests, rps}` (`TrafficStart`, defaults 100/20), it returns
  `{"status": "started", "requests": n}` immediately, and it drives
  `tests.traffic.generator.run_traffic` with the fixed seed 42, exactly as described.
  `tests/personas.yaml`'s six weighted personas (45/20/15/12/5/3, `acme-key` /
  `hospital-key`) matched what's described above.
- **Real split at n=500, seed=42, current code**: **408 acme / 92 hospital** (this
  dev environment runs the `regex` PII detector, not Presidio — see below — so
  entity-level PII counts aren't directly comparable, but the acme/hospital *split*
  depends only on persona sampling, not the detector). The `onprem_count` for hospital
  came back **92**, matching this doc's own worked-example figure — the **93** quoted
  for hospital in `docs/marketing/routeforge-demo-talking-points.md`'s Fleet Overview
  table is off by one against a from-scratch run on current code. Treat every exact
  digit in both docs as **illustrative, from one past captured run** — the mechanism
  (risk-class-driven residency, the fixed warning heuristic) is what's load-bearing
  and what was re-verified here, not the specific dollar/count figures, which drift
  slightly as personas/code evolve even with a fixed seed.
- **PII entity counts specifically** (the 161/92 in the worked example above) need
  Presidio to reproduce — this dev environment doesn't have `presidio-analyzer`
  installed, so those exact counts weren't independently re-derived. The underlying
  rule they rest on (`config/policy.yaml → sensitivity_rules`, `LOCATION`/`DATE_TIME`
  → `confidential` → `private_cloud_ok`; `US_SSN` et al. → `restricted` →
  `onprem_only`) was read directly from the current config and matches this doc's
  description.
