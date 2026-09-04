# Per-application configuration: `pii_detection` and `stream_mode`

Operator-facing summary of the two per-app override knobs added on
`feat/per-app-flexibility`. For the *why* (which knobs are safe to expose per-app and
which aren't), read `docs/architecture/per-app-config-flexibility.md` first — this page
is the how-to.

Both knobs follow the same shape as the existing `capture_io` / `compression_enabled`
per-app overrides: `null` (unset) inherits the deployment-wide default; a set value
overrides it for that application only, subject to a write-time clamp that keeps the
override from ever being able to breach a hard guarantee.

**`PUT /admin/applications/{app_id}/policy` merges, it does not replace.** A field the
request body omits (or sends as `null`) keeps the application's *currently stored*
value — it is left untouched, not blanked back to "inherit deployment default". Every
clamp above is evaluated on this same *effective* (post-merge) value, so a partial PUT
that only touches one field is judged on the real state the write will actually produce,
not on a value that's about to be silently wiped. One consequence: clearing a field back
to "inherit default" now needs an explicit value — omission (or an explicit JSON `null`,
which parses the same way) no longer clears a previously-set field. No test or UI flow
in this codebase relies on clear-by-omit.

## `pii_detection`

Set on `PUT /admin/applications/{app_id}/policy` (or the equivalent admin UI field),
body field `pii_detection`. Values, from `app/policy/enums.py:PII_MODES`:

| Value | Meaning |
|---|---|
| `presidio` (or unset/`null`) | Inherit the deployment's live global detector (`serving.yaml policy.pii_detection`, normally `presidio`). |
| `regex` | Use the lightweight regex detector for this app only. |
| `off` | Detect nothing for this app — the gate does not redact PII before the model sees it. |

**Why an app would want this:** the gate redacts every detected entity into a
placeholder before any model sees the text. For a document-generation app that needs
the real values in its output (customer names, order numbers, revision lines), that
redaction is destructive. `pii_detection: off` (or a scoped `regex`) is the escape valve.

> **Prefer the narrow lever.** `off` is the blunt instrument — it blinds the gate to
> *everything*, a real SSN included. When the problem is a few false-positive entity
> *types* (a shop-order number mis-tagged `US_DRIVER_LICENSE`, a revision date mis-tagged
> `DATE_TIME`) or a few false-positive *value shapes*, reach for `pii_entity_denylist` /
> `pii_score_threshold` / `pii_exempt_patterns` (below) instead: they exempt exactly
> those, and keep every other recognizer on. Reach for `off` when the app's entire input
> is customer-owned content whose every field is load-bearing output — there is then
> nothing to protect and no enumerable set of shapes to exempt.

**Why it's safe:** detection drives *classification*, not residency. Residency is
enforced independently by `_stricter(rule.residency, app.residency)` in
`app/policy/context.py` regardless of what — or whether — the gate detected anything. An
`onprem_only` app with `pii_detection: off` still routes on-prem only; detection being
off cannot lift it to cloud.

That independence is why `onprem_only` apps **may** downgrade detection (with
`capture_io: "off"` — see the floors). An on-prem-only app is the *safest* place to run
the gate open: the model is inside the customer's boundary and no external party ever sees
the prompt. Refusing it there bought no protection and pushed operators toward
`residency: "any"` + an `allowed_providers` pin, which is strictly weaker — a mutable
allowlist in place of a hard residency floor, with `ctx.onprem_only` (the step-9 streaming
floor, the `capture_io=full` clamp) dropped along the way.

**The floors (all enforced at admin write-time, `app/api/admin_routes.py:set_policy`,
HTTP 422 on violation, keyed on the app's *effective* post-write residency/`capture_io` —
request body value if provided, else the app's stored policy, else its default):**

- **Deployment ceiling:** `policy.allow_app_pii_downgrade` in `config/serving.yaml`
  (default `true`; env override `SP_ALLOW_APP_PII_DOWNGRADE`). Set `false` to forbid
  *every* app on this deployment from downgrading detection at all — a locked-down
  (e.g. hospital) deployment sets this once instead of relying on per-app discipline.
- `pii_detection` in `("off", "regex")` is **rejected** when the app's effective
  `capture_io` is `"redacted"` — the `redacted` exporter path reuses the gate's
  *sanitized* prompt (`app/obs/export.py:_set_io`); with detection off nothing was
  sanitized, so `redacted` telemetry would silently carry raw PII. `capture_io="off"` is
  always safe with detection off, at **any** residency. `capture_io="full"` is allowed,
  but only under the guardrails below — never for an `onprem_only` app.
- An unrecognized value is a 422, never a silent fallback to `off`.

  **Net envelope for an `onprem_only` app:** `pii_detection` may be `off`/`regex` only
  while `capture_io` is `"off"` — the other two capture modes are each rejected by their
  own clamp. No telemetry I/O is captured, so there is no egress path for the
  unsanitized prompt, and the runtime cannot widen this: `_set_io` returns early on
  `"off"`, and the fail-closed downgrade in `app/obs/export.py` only ever steps `full`
  down to `redacted`, never `off` up.

### Operator-gated raw eval capture (`pii_detection=off` + `capture_io=full`)

A **non-`onprem_only`** app may combine `pii_detection=off` with `capture_io=full`: raw,
unredacted prompts and completions are sent to Langfuse for eval purposes. This is a
deliberate, narrow exception to "telemetry is redacted on egress" (CLAUDE.md rule 2),
approved by the product owner — not a bug. Every one of these must hold, and each is
enforced independently:

- **`onprem_only` never permits it.** The `capture_io=full` clamp rejects the write
  outright for an `onprem_only` app, whether or not detection is downgraded (CLAUDE.md
  rule 8: residency beats eval). This is the guard that holds — not the detection
  downgrade, which an `onprem_only` app *may* take with `capture_io="off"`.
- **Raw I/O only ever reaches the app's own provisioned Langfuse project**, never the
  shared/default project. `capture_io=full` is rejected (422) unless
  `load_app_langfuse(app)` resolves — the app must have its own project, either
  auto-provisioned at creation or attached manually via
  `PUT /admin/applications/{app_id}/langfuse` (see `docs/onboarding-models.md`-adjacent
  Langfuse provisioning notes). `app/obs/export.py` fails closed to `redacted` at export
  time if the per-app tracer build ever fails, as an independent runtime backstop.
  `app/obs/*` was not changed by this feature — it already had this behavior.
- **`allow_app_pii_downgrade` still gates it.** A deployment with the ceiling set
  `false` cannot reach this combination either, same as any other PII downgrade.
- **Still blocked:** `onprem_only` + `full`; `full` with no own Langfuse project;
  `off`/`regex` + `capture_io=redacted`; the downgrade ceiling off. Each is a distinct
  422 with a message naming which guardrail fired.

> **"My customer is on-prem, has no PII, and wants raw capture in Langfuse — can I just
> flip residency?"** Yes, but residency is also the provider filter, so flipping it off
> `onprem_only` un-pins routing to cloud. See the recommended config (residency +
> `allowed_providers={"onprem"}` pin) in [`help/faq.md`](help/faq.md#recommended-config-raw-langfuse-capture-and-routing-pinned-on-prem).
>
> **This applies to raw *telemetry* only.** If the app just needs unredacted prompts to
> reach the model — the gate is mangling part numbers, field names, or engineering labels
> — do **not** flip residency. Keep `onprem_only` and set `pii_detection: "off"` with
> `capture_io: "off"`.

## `pii_entity_denylist` and `pii_score_threshold`

Set on the same `PUT /admin/applications/{app_id}/policy` endpoint. These narrow the
gate **without disabling it** — the surgical alternative to `pii_detection: off`.

| Field | Type | Meaning |
|---|---|---|
| `pii_entity_denylist` | list of entity-type strings, or `null` | Entity **types** the gate still detects but does **not** redact for this app. Names must be Presidio built-ins (`app/policy/recognizers.py:PRESIDIO_BUILTINS`), e.g. `US_DRIVER_LICENSE`, `DATE_TIME`, `LOCATION`. `null`/`[]` = redact everything detected. |
| `pii_score_threshold` | float in `[0.0, 1.0]`, or `null` | Detected entities scoring **below** this Presidio confidence are treated as non-matches. Culls low-confidence recognizers wholesale (many false positives score ~0.6). `null` = no floor. Only affects *scored* matches — regex/custom-recognizer hits (no score) are never dropped by it. |

Implemented as `FilteredDetector` (`app/policy/effective.py`), which wraps the app's base
detector and drops the denylisted / sub-threshold entities **before** redaction and
classification. So a value the app exempted reaches the model verbatim, and — because it
was never treated as an entity — never contributes to sensitivity/residency either.

**Why this is the preferred lever:**

- **Surgical, not wholesale.** The app keeps `PERSON`, `EMAIL_ADDRESS`, `US_SSN`, etc.
  and only stops mangling the two or three types that were false positives on its data.
- **Allowed for `onprem_only` apps.** Unlike `pii_detection: off/regex` (rejected for
  `onprem_only`, see the floors above), a denylist is accepted while residency stays
  `onprem_only` — the strongest posture. An `onprem_only` app is precisely the case where
  in-boundary redaction of a *benign* identifier buys nothing (there is no third party to
  protect it from), so exempting a false-positive type there is safe.

**The one hard limit — `US_SSN` can never be suppressed.** It is in
`FilteredDetector.PROTECTED_ENTITIES`: naming it in `pii_entity_denylist` is a 422, and no
`pii_score_threshold` can drop it (its backstop scores `1.0`). This preserves CLAUDE.md
rules 3/8 (`onprem_only` is absolute; an SSN can never slip the gate to cloud) regardless
of how an app is tuned. Unknown entity names and out-of-range thresholds are also 422s.

**Recommended recipe** for an app that carries structured business identifiers and must
stay on-prem (no `pii_detection` change needed):

```json
PUT /admin/applications/{app_id}/policy
{ "residency": "onprem_only",
  "pii_entity_denylist": ["US_DRIVER_LICENSE", "DATE_TIME"] }
```

## `stream_mode`

Set on the same `PUT /admin/applications/{app_id}/policy` endpoint, body field
`stream_mode`. Values, from `app/policy/enums.py:STREAM_MODES`:

| Value | What happens | Real backend streaming? |
|---|---|---|
| `off` (or unset/`null`) | `stream: true` is rejected 400 when `server.reject_stream_requests` is true (today's default behavior, unchanged). | — |
| `verified_then_stream` | The full request runs through the normal verified pipeline (including step 9), then the finished answer is chunked out as OpenAI-compatible SSE. | No — post-hoc chunking of an already-verified answer. No time-to-first-token benefit. |
| `stream_unverified` | Real token-by-token streaming straight from the backend. Step-9 verification is explicitly **waived** for this app's streamed traffic (recorded in the trace as `performed=False, reason="waived: stream_unverified"`). | Yes |
| `incremental_verify` | Real token-by-token streaming through a hold-back redactor (`app/output/streaming_verify.py`) that never releases raw entity text across a chunk boundary. Recorded as `performed=True, reason="incremental"`. | Yes |

**`incremental_verify`'s documented limitation:** the hold-back verifier protects
against *per-entity* leakage split across a chunk boundary (e.g. an SSN arriving as
`"...123-45-"` then `"6789"`). It **cannot** enforce a *holistic* whole-response verdict
— the `UNVERIFIABLE` marker `app/output/verify.py` fails closed on for the non-streamed
path — because by the time the tail of the response arrives, earlier tokens have already
reached the client. It is a deliberately **weaker** guarantee than
`verified_then_stream`, not a full replacement for it. Choose it only for apps that need
both low time-to-first-token and per-entity protection, and where a fail-closed
whole-response verdict is not required.

**The fail-closed floor — the one that matters most:**

`onprem_only` and `require_verification` requests can **never** have step-9 verification
waived, no matter what `stream_mode` the app has configured. This is enforced twice,
independently:

1. **Admin write-time (422):** `stream_mode == "stream_unverified"` is rejected when the
   app's effective residency is `onprem_only` (`app/api/admin_routes.py:set_policy`) — the
   unsafe combination can't even be configured.
2. **Runtime backstop:** `app/pipeline.py:run_stream` raises `StreamFallback` whenever
   the request routes `multi_step`, or `ctx.onprem_only`/`ctx.require_verification` is
   true for *this specific request* (e.g. a per-app `stream_unverified` app that happens
   to submit a high-risk restricted-sensitivity prompt this one time). `app/api/routes.py`
   catches `StreamFallback` and transparently falls back to `verified_then_stream` — the
   client still gets a streamed response, but it is the complete, fully-verified one.

Both checks live independently of the app's own configured mode, so no value an app sets
can lower the floor — the same "free-but-floored" shape as `pii_detection` and
`capture_io`.

The decision-stream SSE (`GET /api/stream`, admin-gated, `app/api/ui_routes.py`) is a
**separate channel** from all of this — it streams `DecisionRecord`s for the console's
live view and never carries chat tokens, regardless of any app's `stream_mode`.

## Manual DB migration for already-deployed databases

RouteForge has no migration tool — `Base.metadata.create_all` (SQLAlchemy) only creates
*missing* tables at boot; it never `ALTER`s an existing table to add a new column (see
`docs/onboarding-models.md` §(f) for the same pattern with an earlier column addition).
`pii_detection` and `stream_mode` are two new nullable columns on the existing
`app_policies` table (confirmed against `app/apps/models.py:AppPolicy`:
`String(16)` / `String(24)` respectively). A **fresh** database picks these up
automatically via `create_all`. An **already-deployed** SQLite or Postgres database needs
a manual `ALTER` before an operator can set either field — without it, the admin API call
fails with a database error (missing column), not a clean validation error:

```sql
ALTER TABLE app_policies ADD COLUMN pii_detection VARCHAR(16);
ALTER TABLE app_policies ADD COLUMN stream_mode VARCHAR(24);
```

Both are nullable with no default — existing rows get `NULL`, which is exactly "inherit
the deployment default" for both fields, so this is safe to run against a live database
with existing applications and does not change behavior for any app until an admin
explicitly sets a value. Works unmodified on both SQLite and Postgres (SQLite ignores the
`VARCHAR` length but accepts the syntax; Postgres enforces it).

Run it once per already-deployed environment (DGX, PCAI, any customer install predating
this change) before rolling out this version, or the first admin write attempting to set
either field will fail with a raw DB error instead of a 422.
