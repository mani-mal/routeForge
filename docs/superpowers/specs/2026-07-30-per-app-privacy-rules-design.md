# Design: Customer-configurable per-application privacy rules

**Date:** 2026-07-30
**Status:** Approved (brainstorming) → ready for implementation plan
**Origin:** architecture-review §4.1 ("trivial questions land on mid tier"). Root cause
re-diagnosed live on `:0.5.0`: Presidio tags "France" as `LOCATION`, and the *global*
`policy.yaml` maps `LOCATION → confidential → risk_class medium → risk_floors.medium: mid`.
The floor, not complexity, forces the mid tier. Rather than a one-off global reclassification,
we make the privacy classification **per-application and customer-configurable**, which fixes
4.1 as a side effect and adds a real multi-tenant product capability.

---

## 1. Goals & non-goals

**Goals**
- Each application defines its own **sensitivity rules** (entity type → sensitivity,
  risk_class, residency, require_verification) and its own **custom entity recognizers**
  (regex-based), editable from the admin console.
- Full per-app control: a customer may classify their own entities however they wish,
  **including downgrading** a built-in entity. The application's `default_residency`
  remains the only backstop (via the existing `_stricter()` combination).
- Ship the demo apps with seeded rule sets that demonstrate the feature and close 4.1.

**Non-goals**
- No per-app `risk_floors` (the `risk_class → minimum tier` map stays global in
  `routing.yaml`). Customers steer the effective floor by choosing an entity's `risk_class`.
- No immutable ops-owned "hard floor" on classification (explicitly rejected in
  brainstorming — full control was chosen). The tenant `default_residency` is the backstop.
- Not the Improvement Plane. This is serving-plane policy only.

---

## 2. Guardrail model (decided)

**Full per-app control**, with the application `default_residency` as the only backstop.

- A per-app rule may set any entity to any (sensitivity, risk_class, residency,
  require_verification). Downgrades are allowed.
- Effective residency = `_stricter(rule.residency, app.default_residency)` — unchanged
  from today. Consequence: for an **onprem_only** application (Hospital Agent), no rule can
  route data off-prem — `_stricter(any, onprem_only) == onprem_only`. `onprem_only` stays
  **absolute** for onprem_only tenants (CLAUDE.md rule 3). A non-onprem application that
  downgrades its own data is exercising its own explicit choice.
- The UI must warn loudly on any downgrade that could send a sensitive-looking entity to
  cloud, because the blanket "sensitive data never leaves" story now depends on customers
  not misconfiguring.

---

## 3. Architecture (Approach 1: shared NLP + per-app regex overlay)

Chosen over (2) Presidio ad-hoc recognizers per call and (3) a full per-app AnalyzerEngine
registry, because it is the least code, backend-agnostic (works under both `presidio` and
the `regex` fallback), keeps a single heavy Presidio engine, and reuses the existing
`RegexDetector` machinery for custom recognizers.

### 3.1 New DB tables (auto-created by `create_all`; no manual migration)

`create_all` creates new tables but never ALTERs existing ones, so **new tables** avoid the
manual-migration gotcha (adding *columns* to `app_policies` would need a manual `ALTER` on the
deployed shared Postgres; new tables are created automatically on next boot).

- **`app_recognizers`** — PK `(application_id, name)` (FK CASCADE to applications)
  - `regex: str` — the pattern
  - `score: float` (default 0.85) — detection confidence
  - `enabled: bool` (default True)
  - `name` is the custom entity type, e.g. `BADGE_ID`, `MRN`.

- **`app_sensitivity_rules`** — PK `(application_id, position)` (FK CASCADE)
  - `position: int` — order, most-severe-first (mirrors `policy.yaml` semantics)
  - `entities: JSON list[str]` — entity types this rule matches (built-in ∪ custom)
  - `sensitivity: str`, `risk_class: str`, `residency: str`, `require_verification: bool`

### 3.2 Resolved value object

`AppPrivacyProfile { recognizers: list[Recognizer], rules: list[SensitivityRule] }`,
resolved once per request alongside the existing `AppPolicyView`.

- An application with **no rows** falls back to today's global
  `settings.policy.sensitivity_rules` and built-in-only detection. **Every existing app
  behaves exactly as today** until edited — backward compatible by construction.
- The resolver guarantees a **terminal catch-all** rule (`entities: [] → public/low/any`) so
  `_match_rule` is a total function even if the customer's set omits one.

### 3.3 Shared canonical enums

`sensitivity` (public/internal/confidential/restricted), `risk_class` (low/medium/high),
`residency` (any/private_cloud_ok/onprem_only) are extracted into **one shared module**
reused by (a) the boot config-lint and (b) the write-time API validation, so they can never
drift.

### 3.4 In-memory profile cache

Single-node, in-memory (like `RateLimiter` / `SessionStore`):
`application_id → (EffectiveDetector, rules)`. Cleared for that application on any
recognizer/rule write through the admin API.

---

## 4. Data flow per request

1. Token → application (existing). Resolve `AppPolicyView` **and** `AppPrivacyProfile`
   (cache, else DB).
2. **Gate** builds an *effective detector* = the shared global Presidio engine (built-ins,
   spaCy loaded once at boot) **+** the app's custom recognizers run as a cheap regex second
   pass. Entities merged and de-duped by span (highest score / longest wins).
3. `build_context` classifies via the **app's** rule set (`_match_rule` over app rules,
   terminal public catch-all guaranteed) → sensitivity / risk_class / residency /
   require_verification, then combines residency with the app's `default_residency` via
   `_stricter()` (**backstop**).
4. Router/gateway **unchanged** — target tier still `max(complexity_tier,
   risk_floors[risk_class], min_tier)` clamped to residency-allowed, from the *global*
   `risk_floors`.
5. **Output verify (step 9) uses the same effective detector**, so custom entity types are
   caught leaking on egress. The pipeline builds the effective detector per request and hands
   it to **both** the gate and verify (a change from today's single global `self._detector`).
6. Telemetry egress redaction masks all entity types, custom included.

**Fail-closed (rule 5):** any error resolving a profile or running a custom regex → reject
the request; never fall through to global-allow.

---

## 5. Write-time validation & runtime regex safety

**Admin API validation** (reject with 422 before the write persists):
- Regex **compiles**; entity `name` matches `^[A-Z][A-Z0-9_]{1,31}$` and does not collide
  with a Presidio built-in type.
- **ReDoS screen**: cap pattern length; reject known-catastrophic shapes (nested quantifiers /
  overlapping alternation) via a linter check, then run the pattern against adversarial inputs
  under a wall-clock budget and reject if it exceeds it.
- **Enums** validated against the shared canonical sets; rule `entities[]` must reference known
  types (built-ins ∪ this app's recognizers); caps on number of recognizers/rules per app.

**Runtime safety** (defense in depth — customers own the regex):
- Custom-recognizer pass runs with a **per-match timeout** via the `regex` module's
  `timeout=` (small new dependency, scoped to this pass) **plus** a hard input-length cap.
- A timeout → fail-closed reject, logged.

---

## 6. UI — Settings tab → AppConfig, new "Privacy rules" panel

- **Recognizers table**: name / pattern / score / enabled; add-edit-delete; a live "test
  against sample text" box that highlights matches.
- **Rules table**: ordered (up/down); entities multi-select (built-ins + custom); sensitivity /
  risk_class / residency dropdowns; require-verification checkbox.
- **Downgrade warning banner** whenever a rule sends a sensitive-looking entity to
  `residency: any` / low risk.
- **"Reset to global defaults"** button.
- Backed by `GET` + `PUT /admin/apps/{id}/privacy` (whole-profile, atomic, validated as a
  unit).

Honesty rules unchanged: entity **types** only in the console, never raw values, never CCR
contents.

---

## 7. Demo seeding (idempotent, first-boot only)

Narrative: the platform is deployed for a **healthcare system**; several agent applications
are created under it. **Display names only** change; app **ids and tokens stay the same**
(`acme`/`acme-key`, `hospital`/`hospital-key`, `default`/`demo-key`) so existing tests,
personas, and docs do not ripple.

| id | new display name | default_residency | seeded profile |
|---|---|---|---|
| `acme` | **RAG Agent** | private_cloud_ok | defaults, but `LOCATION` + `DATE_TIME` → internal / low / any |
| `hospital` | **Hospital Agent** | onprem_only | defaults + recognizer `MRN = \bMRN-\d{7}\b` → restricted / high / onprem_only |
| `default` | **Support Agent** | any | defaults, but `LOCATION` + `DATE_TIME` → internal / low / any |

Effect:
- "What is the capital of France?" on RAG/Support Agent → `LOCATION` now internal/low → target
  **cheap → local-8b**. **4.1 closed for the demo**, and the Acme/RAG confidential over-count
  disappears.
- Hospital Agent gains the custom `MRN` recognizer beat; its `onprem_only` tenant default keeps
  everything on-prem regardless of rule edits.

`PERSON`/`EMAIL_ADDRESS`/`PHONE_NUMBER` stay confidential; `US_SSN`/financial stay
restricted/onprem_only in all seeds.

---

## 8. Testing

- **Unit**: regex compile / ReDoS rejection; effective-detector merge & dedup on overlapping
  spans (built-in + custom); resolver fallback to global when app has no rows; terminal
  catch-all guarantee.
- **Gate**: demoted `LOCATION` → `internal` not `confidential`; `MRN` recognizer → detected,
  restricted, onprem_only; **downgrade still floored** — a hypothetical Hospital `SSN→any`
  rule stays onprem_only via `_stricter` (proves the backstop).
- **Egress**: a custom entity type is redacted by both output verify **and** the exporter.
- **Router regression (the 4.1 test)**: "capital of France" on RAG Agent → `local-8b` cheap.
- **API**: reject bad regex / bad enum / unknown entity / ReDoS pattern (422).
- **Backward-compat**: an application with no profile classifies identically to today.
- **Seeding**: demo apps get the expected profiles idempotently on first boot.

---

## 9. CLAUDE.md hard-rule interactions

- **#1 nothing external before the gate** — custom recognizers run *inside* the gate; ordering
  unchanged.
- **#2 redact on egress** — extended to custom entity types via the effective detector in verify
  and the exporter.
- **#3 onprem_only is absolute** — for onprem_only tenants, `_stricter` makes any downgrade a
  no-op, so the guarantee holds by construction. A non-onprem tenant downgrading its own data is
  its own choice.
- **#5 fail closed** — profile/regex errors reject the request.
- **#8 residency beats risk floor** — `risk_floors` stay global; per-app rules feed `risk_class`
  into the unchanged floor logic.

---

## 10. Out of scope / follow-ups

- Per-app `risk_floors` and per-app scoring weights.
- Sharing/inheriting rule sets across applications (templates).
- Audit log of privacy-rule edits (worth adding later given the downgrade power).
