# Per-application configuration: the protection and delivery knobs

Operator-facing reference for the per-app override knobs — `policy_mode`,
`pii_detection`, `pii_entity_denylist`, `pii_score_threshold`, `pii_exempt_patterns`
and `stream_mode`. For the *why* (which knobs are safe to expose per-app and which
aren't), read `docs/architecture/per-app-config-flexibility.md` first — this page is the
how-to.

They all follow the same shape as the existing `capture_io` / `compression_enabled`
per-app overrides: `null` (unset) inherits the deployment-wide default; a set value
overrides it for that application only, subject to a write-time clamp that keeps the
override from ever being able to breach a hard guarantee.

## Two ways to set these

Every field on this page can be set either way; they hit the same endpoint and the same
clamps.

- **Console** — *Application → Settings tab → the policy editor.* Since `ui:0.17.0`
  every field `PolicyIn` accepts has a control there, grouped into four labelled
  sections: **Placement** (residency, providers, tiers, models), **Protection**
  (`policy_mode`, with a nested *Detection & narrowing* sub-group holding
  `pii_detection`, `pii_score_threshold`, `pii_entity_denylist` and
  `pii_exempt_patterns`), **Telemetry** (`capture_io`), and **Delivery and limits**
  (`stream_mode`, Headroom, rate limit, budget). The console mirrors the clamps below
  to disable an option it knows the server will refuse — always with the reason and the
  way out on the option itself — and shows the server's 422 verbatim under the Save
  button when a write is rejected anyway. The server remains the authority; the console
  only explains it.
- **Admin API** — `PUT /admin/applications/{app_id}/policy`, as in the examples
  throughout this page. Still the scriptable path, and still exactly equivalent.

The per-field sections below each name the console control alongside the API field.

## The PUT merges — `null` does not clear (`reset` does)

**This is the sharpest edge in the whole per-app surface, and it bites anyone scripting
against the endpoint.** `set_policy` persists `body.model_dump(exclude_none=True)`, so a
field the request body omits — **or sends as an explicit JSON `null`, which parses the
same way** — keeps the application's *currently stored* value. It is left untouched, not
blanked back to "inherit the deployment default". Every clamp on this page is then
evaluated on that *effective* (post-merge) value, so a partial PUT touching one field is
judged on the real state the write will actually produce, rather than on a value that is
about to be silently wiped.

One consequence worth stating plainly:

- **`{"pii_entity_denylist": null}` does not clear a denylist.** Send `[]`, or name the
  field in `reset` (below). The same holds for `pii_exempt_patterns` and every other
  list field.

Nothing in this codebase relies on clear-by-omit.

### `reset` — the one way to put a field back to `null`

Because `null` means "leave it alone", a field that has been set could never be returned
to `null` = *inherit the deployment default*. `reset` is the explicit, additive escape:

```jsonc
// clear the stored posture and the stored confidence floor; leave everything else alone
PUT /admin/applications/millwise/policy
{ "reset": ["policy_mode", "pii_score_threshold"] }
```

- Merge semantics are **unchanged**. A field is cleared only when it is named in `reset`.
- Names are validated against the writable policy columns (`app/apps/store.py`'s
  `POLICY_FIELDS`). An unknown name is a **422**, never a silent no-op.
- Naming a field in `reset` *and* sending a value for it in the same request is a **422**
  contradiction, not a precedence rule.
- **Every clamp is evaluated on the post-reset effective value.** A reset changes what the
  write produces, so it is judged on that state, and a rejected write applies nothing at
  all. In particular: an `onprem_only` + `passthrough` + `capture_io: "full"` application
  is legal, but `{"reset": ["policy_mode"]}` alone would leave it **`enforced`** and still
  storing raw I/O — refused with the usual 422 (CLAUDE.md rules 2/8). Clear the capture in
  the same write (`{"capture_io": "off", "reset": ["policy_mode"]}`) and it succeeds.
  Deployment ceilings fire the same way: on an install with
  `default_policy_mode: passthrough` and `allow_app_passthrough: false`, clearing an app's
  explicit `enforced` is refused, because the default it would inherit is forbidden.
- Resetting `pii_detection` is never a downgrade — `null` means "inherit the deployment's
  live global detector", the state of an app that never overrode the field.

**Console:** moving a control back to its "(global default)" option now clears that field.
The editor lists exactly which stored values a save will clear, above the Save button.

`pii_score_threshold` is worth a note: `{"reset": ["pii_score_threshold"]}` removes the
floor entirely, which is **not** the same as `0`. A `0` floor still runs
(`FilteredDetector` drops an entity only when `e.score < self._threshold`, and no Presidio
score is below `0.0`, so it passes everything); `null` means no per-app floor at all.

## `policy_mode` — the protection posture

**Start here.** This is the coarse switch; every knob below it is a refinement within
the `enforced` posture.

> **Console:** *Settings → Protection → policy mode.* Selecting `passthrough` raises an
> inline banner stating exactly what stops (detection, redaction in both directions,
> custom recognizers, sensitivity rules), what continues (routing, budgets, metering,
> the decision trace, Langfuse), and that **placement is untouched** — residency above
> still decides where compute may run. The narrowing knobs below dim while
> `passthrough` is selected, because nothing is detected, but their stored values are
> **kept, not cleared**, and apply again the moment the app returns to `enforced`.
> **API:** `PUT /admin/applications/{app_id}/policy`, field `policy_mode`.

RouteForge separates two things that are easy to confuse:

| Axis | Field | Question it answers |
|---|---|---|
| **Placement** | `residency` | *Where may compute run?* on-prem, private cloud, anywhere |
| **Protection** | `policy_mode` | *How much does the gate do to the data?* |

They are independent. `residency` says nothing about protection, and `policy_mode` says
nothing about placement — it never widens `allowed_providers`, never clears
`onprem_only`, and never touches `app/policy/context.py`. `onprem_only` + `passthrough`
("run only on my hardware, and don't touch my data") is a valid, honest configuration.

| Value | Meaning |
|---|---|
| `enforced` (or unset/`null`) | Privacy-first. PII detection, input redaction, egress redaction, sensitivity escalation, custom recognizers, capture ceiling. The hospital persona. |
| `passthrough` | RouteForge is a **router, meter and tracer only.** No detection, no redaction in either direction, no custom recognizers. Raw capture permitted to the app's own Langfuse project. |

Unset inherits the deployment-wide `policy.default_policy_mode` in
`config/serving.yaml` (env `SP_DEFAULT_POLICY_MODE`), so an install where no customer
wants PII checks sets it once rather than per app.

### Which customers want `passthrough`

The app's entire input is the customer's **own** content, and every field of it is
load-bearing output the app is contractually required to reproduce — engineering
drawings, order documents, contracts, medical forms the customer already owns. There is
no third party to protect, and redaction is not a safety feature there but a
correctness bug: a dimension label rewritten to `<PERSON_1>` destroys the identity of a
value while leaving the value itself intact, silently.

Reach for `passthrough` when there is nothing to protect **and** no enumerable set of
shapes to exempt. If only a handful of false positives are the problem, stay `enforced`
and use `pii_entity_denylist` / `pii_exempt_patterns` below — they keep every other
recognizer on.

### What you still get in `passthrough`

Everything that is not protection: model routing and tier selection, cost scoring and
the cheap-tier savings lever, budgets, rate limits, durable usage metering, the full
`DecisionRecord` per step, the decision stream, and Langfuse tracing. Turning protection
off does not turn RouteForge into a proxy — it is still the router and the meter.

### Guards

- **Deployment ceiling.** `policy.allow_app_passthrough` in `config/serving.yaml`
  (default `true`; env `SP_ALLOW_APP_PASSTHROUGH`). Set `false` and **no** app on the
  deployment may be `passthrough`, whatever its own policy says — the one setting a
  locked-down (e.g. hospital) install flips instead of relying on per-app discipline.
  A malformed `SP_ALLOW_APP_PASSTHROUGH` parses as `false`: the ceiling closes on a
  typo rather than opening.
- **Never silent.** `policy_mode` is folded into `AppPolicyView.version()`, stamped in
  every response's `policy` block, and emitted on the trace as `sp.policy_mode`. An
  unprotected app is never unprotected invisibly, and flipping the posture moves the
  `app_policy_version` stamp — so a customer's conformance probe detects the change
  instead of silently passing on a stale result.
- **Raw capture stays tenant-isolated.** `capture_io: "full"` still requires the app's
  own provisioned Langfuse project. `passthrough` relaxes the *residency* condition on
  `full`, never the own-project one: one customer's raw data must never land in
  another's traces or in the shared project.
- **`capture_io: "redacted"` is rejected** for a `passthrough` app — that exporter path
  reuses the gate's sanitized prompt, which is raw when nothing was sanitized. Use
  `full` (with the app's own project) or `off`.
- **Boot lint.** An unrecognized `default_policy_mode` fails the boot with a named
  violation rather than resolving to something safe-but-silent.

### Turning protection back on later

`{"policy_mode": "enforced"}`. One field. The `app_policy_version` stamp changes on the
next response, which is how a client's conformance probe learns the posture moved
without anyone having to tell it.

## `pii_detection`

**Console:** *Settings → Protection → Detection & narrowing → pii detection.*
**API:** `PUT /admin/applications/{app_id}/policy`, body field `pii_detection`.
Values, from `app/policy/enums.py:PII_MODES`:

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
  but only under the guardrails below.
- An unrecognized value is a 422, never a silent fallback to `off`.

  **Net envelope for an `onprem_only` app:** while the app is in the `enforced`
  posture, `pii_detection` may be `off`/`regex` only with `capture_io: "off"` — the
  other two capture modes are each rejected by their own clamp. No telemetry I/O is
  captured, so there is no egress path for the unsanitized prompt, and the runtime
  cannot widen it: `_set_io` returns early on `"off"`, and the fail-closed downgrade in
  `app/obs/export.py` only ever steps `full` down to `redacted`, never `off` up.

  *Amended by PR #34.* The `full` half of that envelope is a property of the
  **`enforced` posture**, not of `onprem_only` itself. An `onprem_only` app the
  operator has explicitly placed in `policy_mode: "passthrough"` — a declared
  no-protection posture — may use `capture_io: "full"` into **its own** Langfuse
  project. See the `capture_io` clamp under [`policy_mode`](#policy_mode--the-protection-posture)
  above. The own-project condition is never relaxed by anything.

### Operator-gated raw eval capture (`pii_detection=off` + `capture_io=full`)

An app may combine `pii_detection=off` with `capture_io=full`: raw, unredacted prompts
and completions are sent to Langfuse for eval purposes. This is a deliberate, narrow
exception to "telemetry is redacted on egress" (CLAUDE.md rule 2), approved by the
product owner — not a bug. Every one of these must hold, and each is enforced
independently:

- **An `onprem_only` app must first declare `policy_mode: "passthrough"`.** For an
  `onprem_only` app still in the `enforced` posture the clamp rejects the write
  outright, whether or not detection is downgraded — the gate is running, so residency
  beats eval convenience (CLAUDE.md rule 8). *Before PR #34, `onprem_only` was used as
  a proxy for "maximum protection" and `full` was refused for on-prem apps
  unconditionally. Removing that proxy is the whole point of `policy_mode`: placement
  and protection are separate axes, and an app whose operator has explicitly declared a
  no-protection posture is coherent with raw capture at any residency. `passthrough`
  changes nothing about placement — `onprem_only` stays absolute in either posture.*
  Independently of all this, an `onprem_only` app may take the detection downgrade with
  `capture_io="off"` in either posture.
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
- **Still blocked:** `onprem_only` + `enforced` + `full`; `full` with no own Langfuse
  project; `passthrough` + `capture_io=redacted`; `off`/`regex` + `capture_io=redacted`;
  either deployment ceiling off. Each is a distinct 422 with a message naming which
  guardrail fired, and the console prints that message verbatim.

> **"My customer is on-prem, has no PII, and wants raw capture in Langfuse — do I have
> to flip residency?"** **No — and you shouldn't.** Since PR #34 the answer is
> `policy_mode: "passthrough"` while `residency` stays `onprem_only`. That keeps the
> hard residency floor (cloud is filtered out of the candidate pool by construction,
> rule 3) *and* gives the app raw capture into its own Langfuse project. Flipping
> residency to `any` + an `allowed_providers` pin — the workaround this page used to
> recommend, see [`help/faq.md`](help/faq.md#recommended-config-raw-langfuse-capture-and-routing-pinned-on-prem)
> — is strictly weaker: it trades a floor cloud cannot survive for a mutable allowlist,
> and drops `ctx.onprem_only`, which is what the step-9 streaming floor keys on.
>
> **This applies to raw *telemetry* only.** If the app just needs unredacted prompts to
> reach the model — the gate is mangling part numbers, field names, or engineering
> labels — you do not need `full` at all. Keep `onprem_only`, and either narrow the gate
> (`pii_entity_denylist` / `pii_exempt_patterns`, below) or set `pii_detection: "off"`
> with `capture_io: "off"`.

## `pii_entity_denylist` and `pii_score_threshold`

**Console:** *Settings → Protection → Detection & narrowing → **exempt types** (a chip
per Presidio built-in; `US_SSN` is rendered locked with a 🔒 and cannot be selected) and
**min confidence** (the threshold box).*
**API:** the same `PUT /admin/applications/{app_id}/policy` endpoint.

These narrow the gate **without disabling it** — the surgical alternative to
`pii_detection: off`.

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
- **No clamp of its own, at any residency or posture.** A denylist or a threshold is
  accepted while residency stays `onprem_only` and the app stays in the `enforced`
  posture — the strongest configuration available. An `onprem_only` app is precisely the
  case where in-boundary redaction of a *benign* identifier buys nothing (there is no
  third party to protect it from), so exempting a false-positive type there is safe.
  Unlike `pii_detection: off/regex`, these levers are not a downgrade of the detector
  and are not subject to the `allow_app_pii_downgrade` ceiling or the `capture_io`
  pairing rules — the gate keeps running on everything else, so nothing about the
  sanitized-prompt seam changes.

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

## `pii_exempt_patterns`

**Console:** *Settings → Protection → Detection & narrowing → **exempt values**.* Add
one regex per row; each row validates as you type (length, then compilability) and a
browser-only tester lets you paste candidate values and see, line by line, which would
be exempted. Nothing typed into that tester is sent anywhere.
**API:** the same `PUT /admin/applications/{app_id}/policy` endpoint, field
`pii_exempt_patterns` (list of regex strings, or `null`).

The **type-independent** complement to `pii_entity_denylist`. A denylist exempts an
entity *type*; this exempts a *value shape*, whatever type Presidio happened to give it.
Reach for it when the same value keeps getting redacted under a different label from one
request to the next — a shop-order code that lands as `US_DRIVER_LICENSE` in one prompt
and `DATE_TIME` in the next — so no list of types ever quite catches it.

`FilteredDetector` compiles each pattern and drops a detected entity when any pattern
**`fullmatch`es the entity's value** (`app/policy/effective.py`). Two things follow from
`fullmatch` that catch people out:

- The pattern must match the **whole** value, not a substring. `\d{4}` does not exempt
  `12 3456 789A`; write the pattern for the entire shape.
- It is matched against the *detected value*, not against the prompt. The console's
  tester takes one candidate value per line for exactly this reason, and approximates
  Python's `fullmatch` with `^(?:…)$`.

Limits, enforced at write time with a 422 each: at most **25** patterns, each at most
**200** characters (a ReDoS/DoS guard on operator-supplied regex), and every pattern
must compile under Python's `re`. JS and Python regex dialects are not identical, so the
console's inline check is an early warning, not the authority.

**`US_SSN` is exempt from being exempted.** The pattern check skips
`PROTECTED_ENTITIES` entirely, so no regex — however SSN-shaped — can un-redact one.
Same guarantee as the denylist and the threshold, enforced at the same seam.

## `stream_mode`

**Console:** *Settings → Delivery and limits → streaming.* The unset option is labelled
"(unset — `stream: true` rejected)", because `stream_mode` has no deployment-wide
default: unset simply means this app never opted in. `stream_unverified` is disabled for
an `onprem_only` app, with the reason on the option.
**API:** the same `PUT /admin/applications/{app_id}/policy` endpoint, body field
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

The same applies to every column added since, each nullable and each meaning "inherit
the deployment default" when `NULL`. On a database deployed before the corresponding
release, run whichever it is missing:

```sql
ALTER TABLE app_policies ADD COLUMN pii_entity_denylist JSON;      -- 0.20.x
ALTER TABLE app_policies ADD COLUMN pii_score_threshold FLOAT;     -- 0.20.x
ALTER TABLE app_policies ADD COLUMN pii_exempt_patterns JSON;      -- 0.22.0
ALTER TABLE app_policies ADD COLUMN policy_mode VARCHAR(16);       -- policy_mode release
```

> **The console does not exempt you from this.** Since `ui:0.17.0` the Settings tab
> offers a control for every one of these fields, and it writes through the same
> `PUT /admin/applications/{id}/policy`. On a database missing the column, saving from
> the console fails the same way as `curl` — with a database error rather than a clean
> validation message. Run the `ALTER`s first.

`policy_mode` is `NULL` for every existing row, which inherits
`serving.policy.default_policy_mode` — `enforced` on a deployment that has not changed
it. So no existing application's protection posture moves as a result of the upgrade.
Run the `ALTER` **before** rolling the new image, not after: the old image never
references the column, so it is safe to apply early, while the new admin API errors with
a raw database error (not a clean 422) if an operator sets `policy_mode` before the
column exists.

Both are nullable with no default — existing rows get `NULL`, which is exactly "inherit
the deployment default" for both fields, so this is safe to run against a live database
with existing applications and does not change behavior for any app until an admin
explicitly sets a value. Works unmodified on both SQLite and Postgres (SQLite ignores the
`VARCHAR` length but accepts the syntax; Postgres enforces it).

Run it once per already-deployed environment (DGX, PCAI, any customer install predating
this change) before rolling out this version, or the first admin write attempting to set
either field will fail with a raw DB error instead of a 422.
