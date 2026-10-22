# Per-application configuration: the protection and delivery knobs

Operator-facing reference for the per-app override knobs — `residency`, `policy_mode`,
`model_input`, `client_output`, `capture_io`, `pii_detection`, `content_integrity`,
`escalate_residency_on_detection`, `pii_entity_denylist`, `pii_score_threshold`,
`pii_exempt_patterns` and `stream_mode`. For the *why* (which knobs are safe to expose
per-app and which aren't), read the per-app-config flexibility design note
first — this page is the how-to. If the question is *"I have a new app, what should I
set?"* rather than *"what does this field do?"*, start with
[`choosing-app-settings.md`](../guides/choosing-app-settings.md) — the decision guide and
its three recipes — and come back here for the field-level detail.

> **This page is authoritative for the axes, their values, the precedence order, the
> presets and the validation rules.** [§ The axes](#the-axes--the-authoritative-model)
> below is the single source; every guide in `docs/guides/` states the same rules and
> links here. Where a guide and this page disagree, this page is wrong until it is
> fixed — but the real authority is `app/policy/compile.py`, which is one pure function
> and is what both the admin API and the request path read.

They all follow the same shape as the existing `capture_io` / `compression_enabled`
per-app overrides: `null` (unset) inherits the deployment-wide default; a set value
overrides it for that application only, subject to a write-time clamp that keeps the
override from ever being able to breach a hard guarantee.

---

## The axes — the authoritative model

There are **two kinds of setting**, and conflating them is the mistake this model exists
to prevent:

| | Governs | Fields |
|---|---|---|
| **Placement** | *Where compute may run.* Nothing else. | `residency`, and the placement-adjacent `allowed_regions` / `require_no_retention` |
| **Protection** | *How much is done to the data.* Nothing else. | `model_input`, `client_output`, `capture_io`, `pii_detection`, `content_integrity`, `stream_mode` |

`allowed_regions` and `require_no_retention` are placement-adjacent **constraints** rather
than a second placement axis: they can only narrow which endpoints a request may reach,
never widen it and never change what the gate does to the data. See
[Endpoint data handling](#endpoint-data-handling--region-retention-raw-sensitive-content).

**Neither may be inferred from the other.** A protection setting can never move
placement, and a placement setting can never change a protection value. The one
exception is declared, named and raise-only: `config/policy.yaml`'s `sensitivity_rules`
may *tighten* residency when an entity is detected, gated per app by
`escalate_residency_on_detection`, and every rule that fires is named on the response and
on the trace. That is the whole cross-axis surface; there is no other path.

All of this is resolved by one pure function, `compile_policy` in `app/policy/compile.py`.
It takes the stored per-app policy plus the deployment settings and returns an
`EffectivePolicy`. The admin API validates that compiled result, `app_policy_version`
hashes it, the gate and the pipeline read it, and the decision certificate reports it.
Nothing re-derives a posture anywhere else.

### The placement axis

| Field | Values | Meaning |
|---|---|---|
| `residency` | `any` \| `private_cloud_ok` \| `onprem_only` | Where compute may run. `onprem_only` filters cloud out of the candidate pool **before** the router scores anything (CLAUDE.md rule 3) — never a post-hoc check. |

`null` inherits the application row's `default_residency`, which itself defaults to `any`.
**A stored residency that is not one of the three legal values denies to `onprem_only`** —
the most restrictive placement — and the write that tried to store it is refused. That is
true in the compiler *and* in `app/policy/context.py`, which calls the same
`resolve_residency` function, so the two cannot disagree about one app's placement.

### The protection axes

| Field | Values | Meaning |
|---|---|---|
| `model_input` | `raw` \| `redact` | What the **model** is shown. `redact` replaces detected entities with placeholders before any backend sees the text; `raw` forwards the original. |
| `client_output` | `raw` \| `redact` | What the **client** receives. `redact` runs egress redaction over the completion (and over `tool_calls.function.arguments`); `raw` returns the model's text verbatim. Independent of `model_input`. |
| `capture_io` | `off` \| `redacted` \| `full` | What **telemetry** stores. `off` = no I/O content at all (metadata only); `redacted` = telemetry builds its *own* redacted copy of the raw values; `full` = raw, and only ever into the app's own Langfuse project. |
| `pii_detection` | `off` \| `regex` \| `presidio` | Which **detector** the above use when one is needed. `null` inherits `serving.policy.pii_detection`. |
| `content_integrity` | `transformations_allowed` (default) \| `exact` | `exact` disables all lossy compression (Headroom) for the app, so its content survives byte-for-byte. A **data** control, not a privacy control: it never changes placement. |
| `stream_mode` | `off` \| `verified_then_stream` \| `stream_unverified` \| `incremental_verify` | How `stream: true` is served. No deployment-wide default — `null` means the app never opted in. See [`stream_mode`](#stream_mode) below. |
| `escalate_residency_on_detection` | `true` (default when `null`) \| `false` | Whether a declared `sensitivity_rules` entry may **raise** this app's residency. It can only ever prevent a tightening; it can never loosen placement, and `false` does not stop the rule from setting `require_verification` or the sensitivity label. |

`policy_mode` is **not a seventh axis.** It is a preset over two of them — see below.

### Precedence — how a value is resolved

For every axis, highest wins:

1. **Deployment ceiling** — `serving.policy.allow_app_passthrough`,
   `serving.policy.allow_app_pii_downgrade`. No app may weaken *past* it by its own
   choice. Note what each one floors an app at, because they differ:
   `allow_app_passthrough: false` clamps `model_input` and `client_output` to `redact`
   outright, so no app on the deployment is in a no-protection posture whatever its row
   or the deployment default says. `allow_app_pii_downgrade: false` clamps `detection`
   back to **the deployment's own `serving.policy.pii_detection`** — not to `presidio`.
   So on a deployment whose default is `regex` (or `off`), closing that ceiling leaves
   every app at `regex` (or `off`); it forbids an app from *choosing* a downgrade, not
   the deployment from *being* one. A deployment that wants `presidio` everywhere has to
   set the default as well as the ceiling.
2. **The explicit per-app value** — the column, when it is not `null`.
3. **The `policy_mode` preset** — for `model_input`, `client_output` and (only when
   `pii_detection` is `null`) `pii_detection`.
4. **The deployment default** — `serving.policy.pii_detection`,
   `observability.langfuse.capture_io`, `serving.policy.default_policy_mode`.

**Anything that cannot be parsed denies.** A ceiling that is not the literal boolean
`true` reads as closed. An axis value that is not a legal member of its vocabulary
resolves to that axis's protective end (`residency → onprem_only`, `model_input →
redact`, `client_output → redact`, `capture_io → off`, `pii_detection → presidio`,
`stream_mode → off`) **and** the write is refused with a 422 naming the value. A typo is
never answered `200` and stored as a policy nobody asked for. This holds for a malformed
*deployment default* too, not only for a per-app value.

### Default versus ceiling — they are different things

The three concepts the guides used to blur:

- A **default** is what an app inherits when it leaves a field `null`. It is a starting
  point, and an app may explicitly differ from it.
- A **ceiling** (`allow_app_passthrough`, `allow_app_pii_downgrade`) is an absolute
  deployment constraint. No app may weaken past it, and clearing a field does not escape
  it — a `reset` is judged on the value the app will inherit afterwards.
- An **explicitly permitted per-app downgrade** may legitimately differ from the default
  without violating any ceiling. "A per-app policy can only narrow the global rules" is
  *not* the rule and never was: an app may set `pii_detection: off` on a deployment whose
  default is `presidio`, as long as `allow_app_pii_downgrade` permits it.

### The presets

`policy_mode` sets two axes at once, and nothing else:

```
policy_mode: enforced     ->  model_input: redact,  client_output: redact
policy_mode: passthrough  ->  model_input: raw,     client_output: raw
```

Plus one narrow inheritance rule: an app whose `policy_mode` resolves to `passthrough`
**and** whose `pii_detection` is `null` resolves `pii_detection` to `off`. An app that
*names* a detector gets the detector it named, in either posture. (Before the compiler,
`passthrough` forced the null detector unconditionally — that was counterexample CE-3,
and there is a [pre-roll audit](#pre-roll-audit-policy_mode-passthrough-rows-that-also-set-pii_detection)
for rows in that shape.)

`policy_mode` is still reported on every response and trace, but it is now **derived**
from the compiled axes rather than read from the column: the stamp reads `passthrough`
exactly when `model_input` and `client_output` are both `raw`, and `enforced` otherwise —
including when a ceiling refused the app's requested `passthrough`. The stamp always
names what ran, never what was asked for.

`policy_mode` never touches placement. It does not widen `allowed_providers`, does not
clear `onprem_only`, and does not appear anywhere in `app/policy/context.py`'s placement
computation.

### The validation rules, with their error codes

Every rule is checked against the **compiled, post-write** effective policy — never
against the request body — so a partial `PUT` that never mentions a field is still judged
on the state the write will actually produce, and a `reset` is judged on what the app
falls back to. A rejected write applies nothing at all. Codes are from
`app/policy/compile.py::validate_effective` and are stable API surface.

| Code | Fires when | Fix |
|---|---|---|
| `<AXIS>_UNKNOWN_VALUE` (e.g. `CAPTURE_IO_UNKNOWN_VALUE`, `RESIDENCY_UNKNOWN_VALUE`) | a stored value is not a legal member of its axis | use one of the values the message lists |
| `AXIS_VALUE_NOT_IMPLEMENTED` | the value is named in the design but deliberately not built — `tokenize`, `authorized_rehydrate`, `resolve_tokens`, `trusted_raw`, `inherit_model` | pick an implemented value; see [Not implemented](#not-implemented--rejected-rather-than-faked) |
| `PASSTHROUGH_FORBIDDEN` | `policy_mode: passthrough`, or `model_input: raw`, or `client_output: raw`, while `allow_app_passthrough` is false | lift the deployment ceiling, or stay protected |
| `PII_DOWNGRADE_FORBIDDEN` | a per-app `pii_detection` of `off`/`regex` while `allow_app_pii_downgrade` is false | lift the ceiling, or use the narrow levers (`pii_entity_denylist` etc.), which this ceiling does not gate |
| `CAPTURE_FULL_NEEDS_OWN_PROJECT` | `capture_io: full` with no provisioned per-app Langfuse project | attach the app's own project first, or use `redacted` |
| `CAPTURE_FULL_ONPREM_NEEDS_NO_PROTECTION` | `capture_io: full` on an `onprem_only` app that has not declared `model_input: raw` **and** `client_output: raw` | declare the posture (`policy_mode: passthrough` resolves both), or use `redacted`/`off` |
| `CAPTURE_REDACTED_NEEDS_A_DETECTOR` | `capture_io: redacted` with `pii_detection` resolving to `off` | set `pii_detection` to `regex` or `presidio`, or use `off`/`full` |
| `STREAM_UNVERIFIED_FORBIDDEN_ONPREM` | `stream_mode: stream_unverified` on an `onprem_only` app | use `verified_then_stream` or `incremental_verify` |
| `STREAM_UNVERIFIED_FORBIDDEN_REDACTED_OUTPUT` | `stream_mode: stream_unverified` with an **explicitly set** `client_output: redact` | use `incremental_verify` or `verified_then_stream`, or set `client_output: raw` |
| `INVARIANT_*` | the save-time invariant check (below) found that this app's compiled policy would break monotonicity, noninterference, or "placement never weakens protection" | report it — reaching one of these means the compiler misbehaved for this app, not that the operator typed something wrong |

**The save-time invariant check.** After the rules above pass, `set_policy` compiles the
same app over a fixed 18-point grid — the three residency values, both extremes of each
protection axis, and three detected-entity signals — and refuses the write if any of
design §7's first three invariants fails. It is bounded and I/O-free by construction
(the grid is a module-level constant), so it adds a small, fixed number of pure-function
calls to the write path and never unbounded work. What it does **not** catch is stated in
[the research note](../research/policy-compiler-noninterference.md#8-what-is-not-proven).

### The two deliberate unlocks

Two combinations that used to be refused are now legal. Each is a consequence of giving a
consumer its own view of the data, and each is a deliberate contract change:

1. **`capture_io: redacted` with `model_input: raw`** — including the
   `passthrough + capture_io: redacted` pair the admin API rejected outright. Telemetry no
   longer reuses whatever view the gate handed the *model*: `app/obs/export.py::_set_io`
   builds its **own** redacted copy from the raw values using the app's detector. So raw
   model input no longer implies raw telemetry. It stays rejected when `pii_detection`
   resolves to `off` (`CAPTURE_REDACTED_NEEDS_A_DETECTOR`) — with no detector there is
   nothing to build an honest redacted copy with, and that was all the old rule was ever
   really protecting against.
2. **`client_output: redact` with `model_input: raw`** — the configuration that could not
   previously be expressed at all: *the trusted, in-boundary model sees the real value;
   the client response is redacted; telemetry stores a redacted copy or nothing.*
   `app/output/verify.py` runs the real detector on egress whenever `client_output` is
   `redact`, whatever the model was shown.

Every other combination behaves exactly as it did. In particular these stay **rejected**:
`onprem_only + enforced + capture_io: full`; `capture_io: full` with no own Langfuse
project; `capture_io: redacted` with no detector; `stream_unverified + onprem_only`.

### Recipes

Built from the axes. `residency` is always your own decision — set it from the customer's
contract, never from the recipe.

| Recipe | `model_input` | `client_output` | `capture_io` | `pii_detection` | Shorthand |
|---|---|---|---|---|---|
| **Protected** (default) | `redact` | `redact` | `redacted` | `presidio` | `policy_mode: enforced` |
| **Protected, no telemetry I/O** | `redact` | `redact` | `off` | `presidio` | `policy_mode: enforced` + `capture_io: off` |
| **Trusted compute, safe client response** | `raw` | `redact` | `redacted` or `off` | `presidio` or `regex` | set both axes explicitly |
| **Router only** | `raw` | `raw` | `full` or `off` | `off` | `policy_mode: passthrough` |
| **Router + meter + tracer, redacted traces** | `raw` | `raw` | `redacted` | `regex` or `presidio` | unlock 1 |

The third row is the one P1a exists for. Write it explicitly — no preset produces it:

```jsonc
PUT /admin/applications/{app_id}/policy
{ "residency": "onprem_only",
  "model_input": "raw",          // the in-boundary model sees the real values
  "client_output": "redact",     // the HTTP response does not
  "capture_io": "redacted",      // nor does the trace
  "pii_detection": "presidio" }
```

### Not implemented — rejected rather than faked

`model_input: tokenize`, `client_output: authorized_rehydrate`, the per-tool axes
(`tool_arguments`, `tool_results_to_model`), the protected-reference broker, and endpoint
data-handling metadata (region, retention) are **not built**. A policy naming one gets a
422 (`AXIS_VALUE_NOT_IMPLEMENTED`) saying so, rather than being accepted and silently run
as the nearest thing that does exist.

There is **no tokenization and no detokenization anywhere in RouteForge.** The gate
produces a redaction map while redacting and then discards it; nothing re-inserts original
values on egress. A redacted value is gone for the rest of that request. The practical
consequence, worth saying to a customer before they design around it: a redacted SSN
cannot be used as a lookup key by a tool. `<US_SSN_1>` is a placeholder, not a reference —
if the tool needs the real value, the app needs `model_input: raw`.

### The decision certificate

Every response's `policy` block and every trace carries what the compiler decided, so an
auditor never has to infer one axis from another:

```
sp.placement, sp.placement_sources
sp.model_input_mode, sp.client_output_mode, sp.telemetry_io_mode
sp.detection_mode, sp.content_integrity_mode, sp.stream_mode
sp.cross_axis_rules_fired, sp.detected_entity_types     # types only, never values
sp.candidates, sp.app_policy_version, sp.routing_policy_version
sp.endpoint_registry_version, endpoint_data_handling    # the endpoint declarations a decision rested on
sp.checks: {chosen_in_eligible_set, fallback_in_eligible_set}
```

`sp.detection_mode` reports the detector that **actually ran on this request**, not the
configured axis. The gate builds a detector only when some consumer needs one — the model
(`model_input: redact`), the client (`client_output: redact`), telemetry
(`capture_io: redacted`) or placement escalation
(`escalate_residency_on_detection`) — so an app in a raw/raw posture with `capture_io: off`
and no escalation scans nothing even if it names `pii_detection: presidio`, and the
certificate says `off`. That matters because `sp.detected_entity_types` is empty in both
cases: "nobody looked" must not read as "looked and found nothing". The configured value is
still available from the admin API's `effective.detection`, and it is folded into
`app_policy_version`.

`sp.checks` is the one per-request proof: the model(s) the gateway actually used were
drawn from *this request's* own eligible set. Policy-wide invariants are vouched for by
`app_policy_version`, which passed the save-time check. Raw values never appear — entity
types and counts are the evidence. The console's request drill-down reads the same fields.

---

## Two ways to set these

Most fields on this page can be set either way; they hit the same endpoint and the same
clamps. **The four P1a data-handling fields — `model_input`, `client_output`,
`content_integrity` and `escalate_residency_on_detection` — are API-only for now:** the
admin API accepts them, `compile_policy` resolves them and the request drill-down displays
the resolved values, but the Settings tab has no control for them yet.

- **Console** — *Application → Settings tab.* Since `ui:0.17.0` every field `PolicyIn`
  accepted at that time has a control there (the four P1a axes above are the exception). The tab is six numbered, collapsible steps, each showing a
  one-line summary of its current values: **1 Access** (API tokens), **2 Placement**
  (residency, providers, tiers, models), **3 Protection** (`policy_mode`, with a
  collapsible *Detection & narrowing* sub-group holding `pii_detection`,
  `pii_score_threshold`, `pii_entity_denylist` and `pii_exempt_patterns`), **4 Telemetry**
  (the app's own Langfuse credentials, then `capture_io`), **5 Delivery and limits**
  (`stream_mode`, Headroom, rate limit, budget) and **6 Privacy rules**. Steps 2–5 are one
  row server-side and one PUT: **Save policy** appears at the foot of each of them and
  every copy writes all four. The console mirrors the clamps below to disable an option it
  knows the server will refuse — always with the reason and the way out on the option
  itself — and shows the server's 422 verbatim under the Save button when a write is
  rejected anyway. The server remains the authority; the console only explains it.
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
Each of steps 2–5 lists exactly which stored values a save will clear, above its Save
button.

`pii_score_threshold` is worth a note: `{"reset": ["pii_score_threshold"]}` removes the
floor entirely, which is **not** the same as `0`. A `0` floor still runs
(`FilteredDetector` drops an entity only when `e.score < self._threshold`, and no Presidio
score is below `0.0`, so it passes everything); `null` means no per-app floor at all.

## `policy_mode` — the protection posture

**Start here.** This is the coarse switch; every knob below it is a refinement within
the `enforced` posture.

> **Console:** *Settings → step 3 Protection → policy mode.* Selecting `passthrough` raises
> an inline banner that **branches on whether this app names a detector**, because the
> answer differs:
> - it always leads with *"nothing is redacted, in either direction"* (both
>   data-handling axes resolve to `raw`);
> - for an app with no `pii_detection` of its own it adds that nothing is detected
>   either — recognizers and sensitivity rules do not run, and no entity can raise
>   residency;
> - for an app that **names** `regex` or `presidio` it says instead that **detection
>   still runs** — entities are detected, classified and traced, the app's custom
>   recognizers run, and a sensitivity rule can still *raise* residency. Redaction is the
>   only thing that stops.
>
> It then names what continues (routing, budgets, metering, the decision trace, Langfuse)
> and that **placement is untouched** — residency in step 2 still decides where compute
> may run. The *Detection & narrowing* sub-group below collapses to a one-line summary
> while `passthrough` is selected — because for the common case (no named detector)
> nothing is scanned — but it stays openable and editable, and naming a detector there is
> exactly how a `passthrough` app keeps detection with redaction off. Its stored values
> are **kept, not cleared** — the summary names what is being kept — and they apply again
> the moment the app returns to `enforced`.
> **API:** `PUT /admin/applications/{app_id}/policy`, field `policy_mode`.

RouteForge separates two things that are easy to confuse:

| Axis | Field | Question it answers |
|---|---|---|
| **Placement** | `residency` | *Where may compute run?* on-prem, private cloud, anywhere |
| **Protection** | `policy_mode` | *How much does the gate do to the data?* |

They are independent. `residency` says nothing about protection, and `policy_mode` says
nothing about placement — it never widens `allowed_providers`, never clears
`onprem_only`, and never touches `app/policy/context.py`. `onprem_only` + `passthrough`
("run only on my hardware, with PII detection/redaction and output verification
switched off") is a valid, honest configuration. Note `passthrough` does not touch
context compression: if Headroom is enabled deployment-wide, it can still transform
tool outputs/history for this app — disable compression too for byte-exact content.

| Value | Compiles to | Meaning |
|---|---|---|
| `enforced` (or unset/`null`) | `model_input: redact`, `client_output: redact` | Privacy-first. Input redaction before any model, egress redaction of the response, sensitivity escalation, custom recognizers, the capture ceiling. The hospital persona. |
| `passthrough` | `model_input: raw`, `client_output: raw` (and `pii_detection: off` **only if the app names no detector**) | RouteForge is a **router, meter and tracer only.** Nothing is redacted in either direction. Raw capture permitted into the app's own Langfuse project. |

> **`passthrough` is a preset over two axes, not a master switch.** It sets `model_input`
> and `client_output` to `raw` and — only when `pii_detection` is unset — resolves
> detection to `off`. An app that *names* a detector in `passthrough` gets it: entities are
> detected, classified and reported, and a declared cross-axis rule can raise residency;
> nothing is redacted, because both data-handling axes stay `raw`. That is a change from
> the pre-compiler behaviour and there is a
> [pre-roll audit](#pre-roll-audit-policy_mode-passthrough-rows-that-also-set-pii_detection)
> for rows in that shape.
>
> **What "untouched" does and does not cover.** `passthrough` says the **PII gate** does
> not alter the data. It says nothing about step 5: if Headroom is enabled deployment-wide
> it can still rewrite message text for token reduction, and its content-based bypasses
> (`high_risk`, `restricted_sensitivity`) are derived from *detected entities*, so for an
> app that detects nothing they never fire. An app that needs byte-exact content needs
> `content_integrity: "exact"` (or `compression_enabled: false`) **as well**.

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
- **`capture_io: "redacted"` needs a detector, not a posture.** A `passthrough` app that
  names `pii_detection: "regex"` or `"presidio"` **may** use `redacted` — telemetry builds
  its own redacted copy of the raw values (the first of the [two
  unlocks](#the-two-deliberate-unlocks)). What is still refused is `redacted` with
  `pii_detection` resolving to `off`, which is what a `passthrough` app that names no
  detector inherits: there is then nothing to build an honest redacted copy with
  (`CAPTURE_REDACTED_NEEDS_A_DETECTOR`). Use `full` (with the app's own project), `off`,
  or name a detector.
- **Boot lint.** An unrecognized `default_policy_mode` fails the boot with a named
  violation rather than resolving to something safe-but-silent.
- **Write-time validation.** An unrecognized per-app `policy_mode` value is a **422**
  (`bad policy_mode '<value>'`), never a silent fallback to `enforced` — the same
  guarantee `pii_detection` gives (`app/api/admin_routes.py:set_policy`).

### Turning protection back on later

`{"policy_mode": "enforced"}`. One field. The `app_policy_version` stamp changes on the
next response, which is how a client's conformance probe learns the posture moved
without anyone having to tell it.

## `pii_detection`

**Console:** *Settings → step 3 Protection → Detection & narrowing → pii detection.*
**API:** `PUT /admin/applications/{app_id}/policy`, body field `pii_detection`.
Values, from `app/policy/enums.py:PII_MODES`:

| Value | Meaning |
|---|---|
| `presidio` (or unset/`null`) | Inherit the deployment's live global detector (`serving.yaml policy.pii_detection`, normally `presidio`). Statistical NER plus Presidio's built-in recognizers. |
| `regex` | Use the lightweight built-in regex detector for this app only. **Exactly five patterns** (`app/policy/detectors.py:_PATTERNS`): `US_SSN`, `CREDIT_CARD`, `EMAIL_ADDRESS`, `PHONE_NUMBER`, `US_BANK_NUMBER`. Nothing else — no names, no addresses, no MRNs, no dates. |
| `off` | Run no built-in detector for this app. **The app's own custom recognizers still run** — see below. |

### Exactly what each value detects and redacts

The single most-asked question, and the one the guides used to answer three different ways:

| | built-in recognizers | the app's custom recognizers (privacy profile) | redacts model input? | redacts client output? | can produce `capture_io: redacted`? |
|---|---|---|---|---|---|
| `presidio` | yes, full Presidio | yes | when `model_input: redact` | when `client_output: redact` | yes |
| `regex` | the five patterns above, and only those | yes | when `model_input: redact` | when `client_output: redact` | yes — at regex fidelity |
| `off` | none | **yes** | nothing is found, so nothing is replaced | nothing is found, so nothing is replaced | no (`CAPTURE_REDACTED_NEEDS_A_DETECTOR`) |

Three consequences worth stating plainly:

- **`regex` really does sanitize** — that is why `regex` + `capture_io: "redacted"` is
  allowed. But it sanitizes only those five shapes. A bare name, an address, an MRN, a
  date, a passport number: all pass through untouched. `regex` is a *lower-fidelity*
  detector, not a partial Presidio.
- **`pii_detection: "off"` is not "guaranteed raw passthrough."** It switches off the
  *built-in* detector. If the application has a privacy profile with custom recognizers,
  those are layered on top (`EffectiveDetector`, `app/policy/effective.py`) and still run
  — so an app with an `MRN-\d{7}` recognizer still has its MRNs detected, classified and
  (under `model_input: redact`) replaced. It also still applies the app's
  `pii_entity_denylist` / `pii_score_threshold` / `pii_exempt_patterns` filtering, which
  is moot with nothing to filter but is not switched off.
- **The only configuration that is genuinely "nothing is scanned"** is `pii_detection:
  off` **together with** the no-protection posture — `model_input: raw` **and**
  `client_output: raw` (what `policy_mode: passthrough` resolves to). That combination
  gets a bare `NullDetector` with no recognizer layering at all: an app the operator has
  declared untouched must not have a leftover custom recognizer quietly rewriting its
  data. This is a deliberate behaviour difference from `pii_detection: "off"` on its own.

**When is a detector built at all?** Whenever *any* consumer needs one: `model_input:
redact`, or `client_output: redact`, or `capture_io: redacted`, or
`escalate_residency_on_detection` is true. Its *kind* then comes from `pii_detection`.
When no consumer needs one, nothing is scanned. This is why the axes are independent: an
app whose model sees raw text but whose client response must be redacted still gets a real
detector, which the old single-switch design could not express.

**Why an app would want this:** the gate redacts every detected entity into a
placeholder before any model sees the text. For a document-generation app that needs
the real values in its output (customer names, order numbers, revision lines), that
redaction is destructive. `pii_detection: off` (or a scoped `regex`) is the escape valve.

**Tool calls are scanned and redacted too.** Under `enforced`, `function.arguments` on
both a replayed assistant turn (ingress) and a model-generated tool call (egress) is
classified and redacted exactly like message content — an entity that appears only in
tool-call arguments still contributes to sensitivity/`onprem_only` escalation, and the
detector's redaction is applied to the arguments. Redaction keeps `arguments` valid JSON:
JSON arguments are redacted value by value (object keys are left alone) and re-serialized
only if something was redacted — arguments with no detected entity are forwarded
byte-for-byte; arguments that are not valid JSON are redacted as a plain string.
Inside tool-call arguments the placeholder is spelled in word characters only —
`REDACTED_US_SSN_1`, not the `<US_SSN_1>` used in message text — because a client executes
those arguments and `<NAME>` is a redirection in a shell. A general-purpose detector such
as `presidio` can match ordinary shell and code text (`-n1`, a file name); for a coding
agent, choose the detector for that app deliberately.
`passthrough` leaves tool call arguments untouched in both directions, same as message
content.

> **Tool arguments are machine-consumed — plan for false positives.** Under `enforced`,
> every value in `arguments` participates in classification the same way message content
> does, **numbers included**: a long numeric ID or Unix timestamp can match
> `US_BANK_NUMBER`/`PHONE_NUMBER`/`DATE_TIME`, and so can raise sensitivity or set
> `onprem_only` exactly as it would in a message. And a matched value is *replaced*: the
> client (or the model, on a replayed turn) receives a placeholder string such as
> `"<URL_1>"` or `"<DATE_TIME_1>"` where the real value was — a JSON number becomes that
> placeholder string — and a tool loop will execute with it. Typical offenders are URLs
> (`URL`), dates and timestamps (`DATE_TIME`), file paths or identifiers tagged `PERSON`,
> and long numbers. A tool-driven app that needs exact argument values should exempt
> those entity types with `pii_entity_denylist` (or specific value shapes with
> `pii_exempt_patterns`), or run in `policy_mode: passthrough`. `US_SSN` can never be
> exempted.

> **Prefer the narrow lever.** `off` is the blunt instrument — it blinds the gate to
> *everything*, a real SSN included. When the problem is a few false-positive entity
> *types* (a shop-order number mis-tagged `US_DRIVER_LICENSE`, a revision date mis-tagged
> `DATE_TIME`) or a few false-positive *value shapes*, reach for `pii_entity_denylist` /
> `pii_score_threshold` / `pii_exempt_patterns` (below) instead: they exempt exactly
> those, and keep every other recognizer on. Reach for `off` when the app's entire input
> is customer-owned content whose every field is load-bearing output — there is then
> nothing to protect and no enumerable set of shapes to exempt.

**Why it's safe:** detection can only ever **raise** residency, never lower it. The app's
own `residency` is the floor, and `_stricter(rule.residency, app.residency)` in
`app/policy/context.py` applies the declared cross-axis rule on top of it — a `max`, not
a replacement. So an `onprem_only` app with `pii_detection: off` still routes on-prem
only: turning detection off removes an *escalation* it might have earned, never the floor
it already has, and detection being off cannot lift it to cloud. What downgrading
detection genuinely costs is the tightening — an SSN that would have forced an
`any`-residency app on-prem no longer does. That is what `escalate_residency_on_detection`
is for, and why counterexample CE-3 mattered. Neither loss is silent: `detection: off` is
in the decision certificate and folded into `app_policy_version`.

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
- `pii_detection == "off"` is **rejected** when the app's effective `capture_io` is
  `"redacted"` (`CAPTURE_REDACTED_NEEDS_A_DETECTOR`). `redacted` telemetry is a promise
  about the content, and the only way to keep it is for the exporter to build a redacted
  copy of the raw values (`app/obs/export.py:_set_io`) — which needs a detector that
  actually ran. With none, there is no honest redacted copy to build. `capture_io="off"`
  is always safe with detection off, at **any** residency. `capture_io="full"` is
  allowed, but only under the guardrails below. `pii_detection == "regex"` is **not**
  rejected by this clamp: the `RegexDetector` really does redact, so telemetry's own copy
  is genuinely redacted — at regex quality (see below for exactly what regex catches),
  not raw. `allow_app_pii_downgrade` still gates `regex` the same as `off`; only the
  `redacted`-specific rejection is narrower.
  *This clamp no longer keys on the posture.* It used to fire for any `passthrough` app
  because the exporter reused whatever view the gate handed the model; telemetry now
  projects independently, so the rule is exactly "is there a detector", nothing more.
- An unrecognized value is a 422, never a silent fallback to `off`.

  **Net envelope for an `onprem_only` app:** while the app is in the `enforced`
  posture, `pii_detection: "off"` may only be paired with `capture_io: "off"` — the
  other two capture modes are each rejected by their own clamp. (`pii_detection: "regex"`
  is not subject to this `redacted` restriction — see above.) No telemetry I/O is
  captured for `off` + `off`, so there is no egress path for the unsanitized prompt, and
  the runtime cannot widen it: `_set_io` returns early on `"off"`, and the fail-closed
  downgrade in `app/obs/export.py` only ever steps `full` down to `redacted`, never `off`
  up.

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
  `PUT /admin/applications/{app_id}/langfuse` — in the console the credential fields sit in
  *Settings → step 4 Telemetry*, directly above `capture_io`, because they gate it (see
  `docs/guides/onboarding-models.md`-adjacent Langfuse provisioning notes).
  **The runtime backstop, in full** (`app/obs/export.py::resolve_capture_io`, the one
  place the decision is made, so the trace and the console's `display` block can never
  disagree): `full` with no own project steps down to `redacted`; `redacted` with no
  detector steps down to **`off`**, not to raw; an unrecognised value is `off`. Never raw
  content under a `redacted` label — that was counterexample CE-1, and it shipped once.
- **`allow_app_pii_downgrade` still gates it.** A deployment with the ceiling set
  `false` cannot reach this combination either, same as any other PII downgrade.
- **Still blocked:** `onprem_only` + `enforced` + `full`
  (`CAPTURE_FULL_ONPREM_NEEDS_NO_PROTECTION`); `full` with no own Langfuse project
  (`CAPTURE_FULL_NEEDS_OWN_PROJECT`); `pii_detection` resolving to `off` +
  `capture_io=redacted` (`CAPTURE_REDACTED_NEEDS_A_DETECTOR` — `regex` and `presidio` +
  `redacted` are both allowed, in either posture); either deployment ceiling off
  (`PASSTHROUGH_FORBIDDEN` / `PII_DOWNGRADE_FORBIDDEN`). Each is a distinct 422 with a
  message naming which guardrail fired, and the console prints that message verbatim.
  **`passthrough` + `capture_io=redacted` is no longer on this list** when the app names a
  detector — see [the two unlocks](#the-two-deliberate-unlocks).

**What `full` stores.** The whole exchange, not just the last message. When a request
has more than one message or offers `tools`, the trace Input is JSON —
`{"messages": [...], "tools": [...], "tool_choice": ...}` — with every message's role,
content, `tool_calls` and `tool_call_id` intact, so an agentic client's earlier turns and
tool results are all there. When the model calls tools, the trace Output is the
assistant message as JSON with its `tool_calls` (a tool-call turn has no text, so it
used to be blank). A single plain message and a plain text answer stay plain text.
`redacted` and `off` are unchanged: raw tool arguments never leave for them.

> **"My customer is on-prem, has no PII, and wants raw capture in Langfuse — do I have
> to flip residency?"** **No — and you shouldn't.** Since PR #34 the answer is
> `policy_mode: "passthrough"` while `residency` stays `onprem_only`. That keeps the
> hard residency floor (cloud is filtered out of the candidate pool by construction,
> rule 3) *and* gives the app raw capture into its own Langfuse project. Flipping
> residency to `any` + an `allowed_providers` pin — the workaround this page used to
> recommend, see [`faq.md`](../guides/faq.md#legacy-config-raw-capture-via-a-residency-flip--an-allowed_providers-pin)
> — is strictly weaker: it trades a floor cloud cannot survive for a mutable allowlist,
> and drops `ctx.onprem_only`, which is what the step-9 streaming floor keys on.
>
> **This applies to raw *telemetry* only.** If the app just needs unredacted prompts to
> reach the model — the gate is mangling part numbers, field names, or engineering
> labels — you do not need `full` at all. Keep `onprem_only`, and either narrow the gate
> (`pii_entity_denylist` / `pii_exempt_patterns`, below) or set `pii_detection: "off"`
> with `capture_io: "off"`.

## `pii_entity_denylist` and `pii_score_threshold`

**Console:** *Settings → step 3 Protection → Detection & narrowing → **exempt types** (a
chip per Presidio built-in; `US_SSN` renders as `US_SSN (locked)`, is not clickable and
cannot be selected) and **min confidence** (the threshold box).*
**API:** the same `PUT /admin/applications/{app_id}/policy` endpoint.

These narrow the gate **without disabling it** — the surgical alternative to
`pii_detection: off`.

> **On the name.** `pii_entity_denylist` reads as "types that are denied" and means the
> opposite: types the gate **exempts** from redaction. `pii_exempt_entity_types` would be
> the honest name, and it is not being renamed — the field is live API surface on
> deployed installs and in operator scripts, and a rename with a deprecated alias is a
> separate change with its own compatibility cost. Read it as *"the denylist of entity
> types the redactor may not touch."*

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

**Console:** *Settings → step 3 Protection → Detection & narrowing → **exempt values**.* Add
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

## `model_input` and `client_output`

**API only** (no console control yet): the same
`PUT /admin/applications/{app_id}/policy` endpoint, fields `model_input` and
`client_output` (each `raw` | `redact` | `null` to inherit). The console's request
drill-down shows the resolved values per request.

The two data-handling axes `policy_mode` presets. Set either explicitly and it wins over
the preset; leave both `null` and the preset decides. They are independent of each other
and of `residency`.

- `model_input: "redact"` — the backend receives the redacted view. `raw` — the backend
  receives the original text. This is the axis that decides whether a trusted, in-boundary
  model can do its job on real values.
- `client_output: "redact"` — egress redaction runs over the completion **and** over
  model-generated `tool_calls.function.arguments`, with the app's detector
  (`app/output/verify.py::egress_detector` is the one place that choice is made, so
  `verify()`, the tool-call redactor and the streaming hold-back redactor cannot drift).
  `raw` — no egress redaction. The `UNVERIFIABLE` leakage check and `require_verification`
  fail-closed behaviour are **identical on both sides of the axis**: a failed verification
  returns nothing, whatever `client_output` says. Redaction is the only thing `raw`
  switches off.

**Both `raw` values are gated by `allow_app_passthrough`, independently.** Each is a
declared no-protection posture in its own right.

Under `client_output: "raw"` the response's `detected_entities` on the egress side is
always empty — not because nothing was there, but because nothing looked. The trace says
so rather than implying a clean verification.

## `content_integrity`

**API only** (no console control yet): field `content_integrity`
(`transformations_allowed` | `exact` | `null`).

`exact` disables **all** lossy compression for this application: Headroom never runs, so
message text, tool outputs and history reach the model byte-for-byte as the app sent them
(modulo whatever the data-handling axes do, which is a separate question). Recorded on
the trace as `sp.content_integrity_mode`.

This is a **data** control, not a privacy control. It never changes placement and it never
changes what is detected or redacted. It exists because the policy gate and the compressor
are different subsystems with different reasons for rewriting text, and turning protection
off says nothing about the second one — the trap that has cost the most time on this
product. An app whose input is a column-aligned table needs `content_integrity: "exact"`
(or `compression_enabled: false`), not `policy_mode: "passthrough"`.

## `escalate_residency_on_detection`

**API only** (no console control yet): field `escalate_residency_on_detection`
(boolean, `null` = `true`).

Whether a declared `config/policy.yaml` `sensitivity_rules` entry may **raise** this app's
residency when an entity is detected. This is the one and only cross-axis path in the
product, and this field is its switch.

- It is **raise-only**. A rule can tighten placement (`any` → `onprem_only`); it can never
  loosen it below the app's own configured floor, in either setting of this field.
- `false` prevents the *placement* tightening only. The rule still applies
  `require_verification` and the sensitivity label.
- Every rule that fires is named in `sp.cross_axis_rules_fired`, and a rule that actually
  *moved* placement is additionally named in `sp.placement_sources`. A rule that fired
  without moving placement appears only in the first — `placement_sources` never claims an
  influence that did not happen.
- It is structurally inert for an app with no detector: `pii_detection` resolving to `off`
  means nothing is observed, so nothing can escalate. Not a bug — the axes are
  independent, and this switch governs whether a rule *may* move placement, not whether
  scanning happens.

## `trajectory_mode` — carrying a restriction along a conversation

An agent's task is a sequence of model calls, and RouteForge handles each call as its own
request. By default a placement raised by a detected entity therefore holds for that
request only: if the client later sends a summary that no longer contains the identifier,
that request is routed as if nothing sensitive had happened.

`trajectory_mode` makes the restriction follow the conversation. RouteForge remembers, per
trajectory, the entity **types** it has seen, and on each later call adds them to what it
detects before matching the sensitivity rules. Whatever the matched rule sets — placement,
sensitivity, risk class, verification — is inherited.

| value | behaviour |
|---|---|
| `off` (default, `null`) | No envelope. Each request stands alone, as before. |
| `header` | Calls that send the same `x-routeforge-trajectory` header share an envelope. A call with no header, or an invalid one, has none. |
| `sticky` | Every call inherits what **any** of the application's live envelopes has seen, whatever header it sends. |

- **The header** is 1–128 characters of `A–Z a–z 0–9 . _ : -`. It is stored only as a hash
  scoped to the application; two applications using the same id never share an envelope.
- **`header` trusts the client.** A client that sends a new id, or none, starts clean. Use
  it when the client is yours. **`sticky` does not trust the client**: once restricted data
  is seen, all of the application's traffic is restricted until the envelope expires.
  Harmless requests then go on-prem too; that is the price of failing closed.
- **It only tightens.** Nothing lowers an envelope except expiry: an entity type stops
  counting one lifetime (`SP_TRAJECTORY_TTL_HOURS`, default 24) after it was last
  **detected** in a request of the trajectory. A call that merely inherits a type does not
  renew it. Expiry is a loosening, so choose the lifetime deliberately.
- **Safe under concurrency.** The envelope is a set of append-only observations that are
  unioned on read, so simultaneous calls of one trajectory cannot overwrite each other.
  Expired observations are deleted hourly.
- **Placement and the rule's own signals only.** No protection axis changes. Redaction is
  unaffected: it works on the text of the current request.
- **Never silent.** The response's `policy` block carries `trajectory_mode` and
  `inherited_entity_types`; the outcome row carries the hashed id and the inherited types;
  `detected_entity_types` on both includes the inherited types, which is what replay reads.
- **Fail closed.** If the envelope cannot be read or written, the request is rejected.
  An unparseable stored value resolves to `sticky`.
- It respects `escalate_residency_on_detection`: an application that has turned placement
  escalation off inherits the rule's other signals but not a placement change.
- Sub-agents are covered only if they send the parent's id. There is no parent/child
  linkage, and no operation that lowers an envelope before it expires.

Only type names and a hash are stored (table `trajectory_envelopes`, in the deployment's
own database). No request text is kept.

## `model_aliases` -- stable names for pinned models

`model_aliases` is a per-application map `{alias -> model slug}`. A client sends the alias
as `model`; RouteForge swaps in the slug before the gate runs, so from there it is an
ordinary slug pin through the same gate, residency, policy and feasibility checks. An alias
to a slug the request may not use fails exactly as pinning that slug directly would.

- **Shape.** Alias names match `^[a-z0-9][a-z0-9._-]{0,63}$` (for example `role.planner`).
  Targets must be a slug in the YAML pool or onboarded (enabled or not).
- **Validation (422, named message).** A name that does not match the pattern, the name
  `auto`, a name equal to a known slug, a target that is not a known slug, a non-string
  target, or an empty map (clear it with `reset: ["model_aliases"]`).
- **Replace, not merge.** A PUT carrying `model_aliases` replaces the whole map. Omit the
  field to leave it alone.
- **Resolution.** `auto` is never an alias. A name that is an alias resolves to its slug.
  A name that is not an alias but is a known slug is a plain slug pin. Any other name, for
  an app that has aliases, is `400` with `error.code == "unknown_model_alias"` and no model
  call; it never falls back to `auto`. An app with no aliases is unchanged by this feature,
  so an unknown name still gets the existing `422 model_not_found`.
- **Version.** Folded into `app_policy_version` only when set, so repointing an alias
  changes the version and an app without aliases keeps its hash.
- **Collision.** Aliases are checked against slugs at write time. If a later onboarding
  reuses an alias name as a slug, the alias wins for that app.
- **Outcome rows.** `routing_outcomes.requested_alias` records the alias sent;
  `requested_model` and `chosen` are the slug.

**Deploy.** Run before rolling the image. The ORM names the column in every policy
`SELECT`, so without `app_policies.model_aliases` every policy read fails -- effectively
a full outage for all applications, not just aliased ones:

```sql
ALTER TABLE app_policies ADD COLUMN IF NOT EXISTS model_aliases JSON;
```

## Model eligibility is two-sided

Worth restating here because it is asked as a policy question and answered in two places:

- The **model's** `allowed_app_ids` must permit the application (set at onboarding, see
  `docs/guides/onboarding-models.md`).
- The **application's** `allowed_providers` / `allowed_tiers` / `allowed_models` must
  permit the model.

Both must hold. On top of that, the request's **effective** residency — the app's floor,
possibly raised by a declared cross-axis rule for *this* request — filters the candidate
pool before the router scores anything. Neither a protection setting nor a telemetry
setting can lower the application's residency floor, and an empty eligible set is a clean
rejection, never a silent widening.

A third pair of constraints sits alongside those two — see the next section.

## Endpoint data handling — region, retention, raw sensitive content

**RouteForge records what an operator DECLARED about an endpoint and enforces policy
against that declaration — it cannot verify a provider's contractual claim.** Everything
in this section rests on that sentence. A `prompt_retention: none` on an endpoint is the
operator's statement about a contract they signed; RouteForge stores it, filters on it,
names it in the trace and shows its provenance to an auditor. It does not, and cannot,
prove the provider honours it.

### What an endpoint declares

Three attributes, on every endpoint RouteForge can reach — an onboarded on-prem row or a
`config/routing.yaml` `model_pool` entry:

| Attribute | Values | Meaning |
|---|---|---|
| `region` | any token, normalized to lowercase (`us`, `eu`, `de`, `onprem`, …) | Where the compute runs. |
| `prompt_retention` | `none` \| `retained` \| `unknown` | Whether the provider keeps prompts. |
| `accepts_raw_sensitive` | `true` \| `false` \| `unknown` | Whether this endpoint may receive **unredacted** sensitive content. |

**`region` is customer vocabulary, not a RouteForge enum.** It is compared exactly, after
lowercasing and stripping, and nothing validates it — RouteForge ships no geography list,
because one customer's `eu` may mean something specific to their contract. The other two
*are* fixed enums, and a value outside them fails the boot (see "Config lint" below).

Set them at onboarding — see `docs/guides/onboarding-models.md` for the API and YAML
shapes, and for the console form.

### Resolution order

Per attribute, most specific wins:

1. **the per-model value** — a column on `onboarded_models`, or a key on a
   `routing.yaml` `model_pool` entry;
2. **the provider default** — `providers.<name>.{region,prompt_retention,accepts_raw_sensitive}`
   in `config/serving.yaml`;
3. **`unknown`**.

`ModelRegistry.effective_pool()` is the one place this resolution happens; every consumer
reads the resolved candidate and none re-derives it. The resolved value **and its
provenance** (`model` / `provider_default` / `unknown`) are what the certificate reports,
so an auditor can see whether a decision rested on a per-endpoint declaration or on a
blanket provider default.

### What an application requires

Two nullable columns on `app_policies`, compiled like every other field:

| Field | Values | `null` means |
|---|---|---|
| `allowed_regions` | non-empty list of region tokens, e.g. `["us"]` | No regional constraint. |
| `require_no_retention` | `true` \| `false` | `false` — no retention constraint. |

They are **placement-adjacent constraints, not protection axes**: they narrow *where* a
request may run and never change what the gate does to the data. They can only ever
narrow the candidate set — no value of either can widen it, reach a provider residency
excluded, or touch `allowed_providers` / `onprem_only`.

`allowed_regions` has no ceiling, no preset and no deployment default — it is
explicit-or-null. An unparseable value (not a list, empty, or holding a non-string or
blank token) **denies** to "matches nothing" and the write is refused with
`ALLOWED_REGIONS_INVALID`. Tokens are normalized to lowercase on both sides, so an app
asking for `"US"` matches an endpoint declaring `us`. An empty list is one of the refused
shapes, so it is **not** how the constraint is cleared — send
`reset: ["allowed_regions"]`, which the 422 message names.

### The three exclusion rules

They run in `app/policy/feasible.py`, immediately **behind** the residency filter and
ahead of everything else — so an excluded endpoint is unreachable by construction rather
than rejected post-hoc, and a contractual refusal is never relabelled by a softer "not
permitted for application". Residency still outranks all three (CLAUDE.md rule 3).

| Rule | Fires when | Reason code | An `unknown` endpoint is |
|---|---|---|---|
| Region | the app set `allowed_regions` and the endpoint's region is not in it | `region_not_allowed` | **excluded** |
| Retention | the app set `require_no_retention` and the endpoint's retention is not `none` | `prompt_retention_not_allowed` | **excluded** |
| Raw sensitive | the request would send raw sensitive content **and** the endpoint declares `accepts_raw_sensitive: false` | `raw_sensitive_not_accepted` | **kept** |

"The request would send raw sensitive content" means the compiled `model_input` is `raw`
**and** either the gate classified the request above `public` **or** the app's compiled
`pii_detection` is `off`. Under `model_input: redact` the model is shown placeholders, so
the rule does not fire — which is how an endpoint that declares
`accepts_raw_sensitive: false` is *eligible after redaction* without needing a third
app-side knob.

**Why `pii_detection: off` counts on its own.** It is the `policy_mode: passthrough`
shape — the preset resolves to `model_input: raw` *and* `pii_detection: off` — and it is
the one app class that by definition sends unredacted content. With no detector nothing
is ever detected, no sensitivity rule matches, and the request classifies `public`
forever; on an "above public" test alone the rule would be permanently inert for exactly
the posture it exists to constrain. An app that has declared it sends *unclassified* raw
data cannot show its content is public, and an endpoint that declared it will not accept
raw sensitive content should not be handed content nobody has classified. That is the
same fail-closed reading as "`unknown` never satisfies a requirement", applied to the
request side. It still only ever bites on an operator's **explicit**
`accepts_raw_sensitive: false`: nothing backfills that declaration, so a passthrough app
over an undeclared pool routes exactly as before.

An excluded endpoint is excluded for failover and for confidence escalation too: both
draw from the same compiled feasible set, so a candidate refused on the first pass cannot
be reached on a retry. If every candidate is excluded, the request is **rejected with
reasons** — never relaxed onto a non-compliant endpoint (CLAUDE.md rule 5).

### `unknown` never satisfies a requirement, and never blocks a request that imposes none

Both halves of that sentence are load-bearing, and together they are why the `unknown`
column above is **not uniform**.

- **Region and retention exclude an `unknown` endpoint,** because *the application
  declared a requirement*. An endpoint that cannot show the property does not qualify —
  that is the entire content of the requirement. An endpoint whose region is `unknown` is
  therefore excluded only for an app that set `allowed_regions`; an app that set neither
  column is unaffected by it.
- **The raw-sensitive rule keeps an `unknown` endpoint** — it fires *only* on a declared
  `false`. This is the exception, and it differs because **no application declared
  anything**: the requirement comes from the request's own shape (a `raw` `model_input`
  over a better-than-public sensitivity, or a `raw` `model_input` with detection off).
  Excluding `unknown` there would retire **every
  endpoint in every existing deployment** on the first request carrying a detected
  entity — a rule nobody asked for. Nothing backfills `accepts_raw_sensitive`, so there is
  no upgrade path that avoids it. Only an operator's explicit `false` is enforced.

The practical consequence, and the upgrade promise: **an application that sets neither
`allowed_regions` nor `require_no_retention` routes exactly as it did before this feature
existed**, on a pool where every endpoint is `unknown` on all three.

A strict mode that excluded any incompletely-described endpoint was considered and
deliberately not built: it would be an outage for every existing deployment on upgrade.

### Config lint and save-time warnings

Four boot-failing violations, on top of the existing config lint:

```
PROVIDER_PROMPT_RETENTION_UNKNOWN providers.<name> prompt_retention=<v> not in ['none','retained','unknown']
PROVIDER_RAW_SENSITIVE_UNKNOWN    providers.<name> accepts_raw_sensitive=<v> not in ['false','true','unknown']
POOL_PROMPT_RETENTION_UNKNOWN     model_pool:<slug> prompt_retention=<v> not in [...]
POOL_RAW_SENSITIVE_UNKNOWN        model_pool:<slug> accepts_raw_sensitive=<v> not in [...]
```

An unset value is never a violation — "not declared here" is the normal state. `region`
is never linted, because it has no enum to lint against.

Saving an app policy whose `allowed_regions` no endpoint currently declares returns a
**warning**, not an error: the matching endpoint may be onboarded later. The warning
names what the app wants, what the pool declares, and that every request is rejected until
one of the two changes.

### Evidence on the response and the trace

```
sp.endpoint_registry_version   # content hash over the effective pool's identity + these three declarations
endpoint_data_handling         # the SERVED endpoint's three values + the provenance of each
sp.candidates                  # rejected entries carry `code`: region_not_allowed | prompt_retention_not_allowed | raw_sensitive_not_accepted
```

`endpoint_registry_version` changes the instant any endpoint's declaration changes, so an
edited declaration becomes visible in the trace instead of silent. It deliberately covers
only name/provider/tier plus the three declarations — a rename or a price change must not
churn a version a replay consumer keys on. See `docs/reference/routing-observability.md`.

## `stream_mode`

**Console:** *Settings → step 5 Delivery and limits → streaming.* The unset option names
what this deployment will actually do with `stream: true` — "(unset — `stream: true`
rejected)", or "(unset — `stream: true` served unstreamed)" where
`server.reject_stream_requests` is false — because `stream_mode` has no deployment-wide
default: unset simply means this app never opted in. `stream_unverified` is disabled for
an `onprem_only` app, with the reason on the option.
**API:** the same `PUT /admin/applications/{app_id}/policy` endpoint, body field
`stream_mode`. Values, from `app/policy/enums.py:STREAM_MODES`:

| Value | What happens | Real backend streaming? | Tokens released before the whole-response verdict? | Egress redaction across chunk boundaries? | Trace records |
|---|---|---|---|---|---|
| `off` (or unset/`null`) | `stream: true` is rejected 400 when `server.reject_stream_requests` is true (today's default behavior, unchanged). | — | — | — | — |
| `verified_then_stream` | The full request runs through the normal verified pipeline (including step 9), then the finished answer is chunked out as OpenAI-compatible SSE. | No — post-hoc chunking of an already-verified answer. No time-to-first-token benefit. | **No.** The verdict is reached first. | Yes — the whole answer was redacted before the first chunk. | the normal `VerificationRecord` |
| `stream_unverified` | Real token-by-token streaming straight from the backend. Step-9 verification is explicitly **waived** for this app's streamed traffic. | Yes | **Yes** — this is the waiver. | **No.** See the caveat above. | `performed=False, reason="waived: stream_unverified"` |
| `incremental_verify` | Real token-by-token streaming through a hold-back redactor (`app/output/streaming_verify.py`) that never releases raw entity text across a chunk boundary. | Yes | Yes, but each released token has passed the hold-back redactor. | **Yes**, per entity — but no whole-response verdict. | `performed=True, reason="incremental"` |

**An incompatible selection is refused at write time, not silently changed** —
`STREAM_UNVERIFIED_FORBIDDEN_ONPREM` and `STREAM_UNVERIFIED_FORBIDDEN_REDACTED_OUTPUT`.
A row that already exists in an incompatible shape is *visibly* floored at runtime to
`verified_then_stream` (the three `StreamFallback` reasons below), never served unverified.

An unrecognized `stream_mode` value is a **422** (`bad stream_mode '<value>'`), never a
silent fallback to `off` — the same guarantee `pii_detection` gives
(`app/api/admin_routes.py:set_policy`).

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
2. **Runtime backstop:** `app/pipeline.py:run_stream` raises `StreamFallback`, which
   `app/api/routes.py` catches and transparently serves as `verified_then_stream` — the
   client still gets a streamed response, but it is the complete, fully-verified one.
   It is raised **before any token is emitted**, for exactly **three** reasons:

   | # | Reason | Why |
   |---|---|---|
   | 1 | `ctx.onprem_only` or `ctx.require_verification` is true **for this request** | the privacy floor. It fires even for an app whose stored residency is looser, when a `sensitivity_rules` entry raised residency on this one request. |
   | 2 | the request is a **client-driven tool conversation** (it offers `tools`, or replays assistant `tool_calls` / `role: "tool"` results) | the live token path flattens messages into one prompt and cannot stream a `tool_calls` answer; the structured path can. Nothing to do with privacy. |
   | 3 | `stream_mode: stream_unverified` **and** an **explicitly set** `client_output: redact` | unverified streaming forwards backend deltas as they arrive, so no egress redaction can run over the complete response. This is the runtime twin of the `STREAM_UNVERIFIED_FORBIDDEN_REDACTED_OUTPUT` write-time rule, for a row written before that rule shipped or straight into the database. |

Reasons 1 and 3 live independently of the app's own configured mode, so no value an app
sets can lower the floor — the same "free-but-floored" shape as `pii_detection` and
`capture_io`.

> **`stream_unverified` waives egress redaction even when `client_output` resolves to
> `redact` through the `enforced` preset.** Say this out loud before enabling it.
> Reason 3 above, and the matching write-time 422, both key on an **explicitly set**
> `client_output: redact` — not on the effective value. That is deliberate: `redact` is
> also what `enforced` resolves to, so keying on the effective value would silently floor
> every `stream_unverified` app in existence, repealing the mode rather than honouring a
> contradiction its operator wrote down. The consequence is real: an `enforced` app that
> opts into `stream_unverified` streams the model's tokens **unredacted**, and step 9 is
> waived for that traffic (recorded on the trace as `performed=False, reason="waived:
> stream_unverified"`).
>
> **So: `client_output: redact` is a guarantee on every path except that one opt-in.**
> An app that wants the guarantee to be absolute should either set `client_output:
> "redact"` explicitly — which makes `stream_unverified` a 422 it cannot configure and a
> floored fallback if the row already exists — or use `incremental_verify`, which *does*
> redact egress (hold-back), or `verified_then_stream`.

**Tool-calling conversations stream as a finished answer.** Agentic clients (Qwen Code,
the `openai` SDK, LangChain) send `stream: true` on every turn, including the turns where
the model should call a tool. A request that offers `tools`, or replays assistant
`tool_calls` / `role: "tool"` results, is always served by the complete, structured
pipeline — `run_stream` raises `StreamFallback` for it, whatever the app's `stream_mode`.
The finished answer is then sent as SSE: any content frames, one frame carrying the
`tool_calls` delta (each call stamped with its stream `index`), and `finish_reason:
"tool_calls"`. The client's tool loop works in every streaming mode; the cost is no
time-to-first-token benefit on those turns — bytes arrive once the model has finished.
Live token streaming of tool calls is not implemented.

The decision-stream SSE (`GET /api/stream`, admin-gated, `app/api/ui_routes.py`) is a
**separate channel** from all of this — it streams `DecisionRecord`s for the console's
live view and never carries chat tokens, regardless of any app's `stream_mode`.

### `max_cost_usd_per_request`

Estimated-cost cap for one request, in USD. `null` inherits `budgets.default_max_spend_usd` (policy.yaml, 0.05). The router estimates `prompt_tokens × input price + (max_tokens or budgets.default_estimated_output_tokens) × output price`. It falls back to a cheaper allowed tier before rejecting, and never goes below the risk floor. A coding agent (≈25K prompt tokens, `max_tokens` 8192) on a model priced $10/$10 per 1M estimates ≈$0.33 and needs this raised, or the model priced realistically. On-prem models are often priced 0.

## Manual DB migration for already-deployed databases

RouteForge has no migration tool — `Base.metadata.create_all` (SQLAlchemy) only creates
*missing* tables at boot; it never `ALTER`s an existing table to add a new column (see
`docs/guides/onboarding-models.md` §(f) for the same pattern with an earlier column addition).
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
ALTER TABLE app_policies ADD COLUMN max_cost_usd_per_request FLOAT; -- 2026-09-15
ALTER TABLE app_policies ADD COLUMN model_input VARCHAR(16);       -- 2026-09-18 (P1a)
ALTER TABLE app_policies ADD COLUMN client_output VARCHAR(16);     -- 2026-09-18 (P1a)
ALTER TABLE app_policies ADD COLUMN content_integrity VARCHAR(32); -- 2026-09-18 (P1a)
ALTER TABLE app_policies ADD COLUMN escalate_residency_on_detection BOOLEAN; -- 2026-09-18 (P1a)
ALTER TABLE app_policies ADD COLUMN allowed_regions JSON;          -- 2026-09-19 (P1b)
ALTER TABLE app_policies ADD COLUMN require_no_retention BOOLEAN;  -- 2026-09-19 (P1b)
```

P1b also adds three columns to a **second** table, `onboarded_models` — the endpoint side
of the same feature (what an operator declares about an endpoint, see
[Endpoint data handling](#endpoint-data-handling--region-retention-raw-sensitive-content)):

```sql
ALTER TABLE onboarded_models ADD COLUMN region VARCHAR(64);            -- 2026-09-19 (P1b)
ALTER TABLE onboarded_models ADD COLUMN prompt_retention VARCHAR(16);  -- 2026-09-19 (P1b)
ALTER TABLE onboarded_models ADD COLUMN accepts_raw_sensitive BOOLEAN; -- 2026-09-19 (P1b)
```

P2a (routing signal measurement) adds two more, on `onboarded_models` and on a
**third** table, `usage_records`:

```sql
ALTER TABLE onboarded_models ADD COLUMN price_cached_input FLOAT;   -- 2026-09-19 (P2a)
ALTER TABLE usage_records    ADD COLUMN cached_prompt_tokens INTEGER; -- 2026-09-19 (P2a)
```

- `onboarded_models.price_cached_input` — USD per 1M prompt tokens the endpoint
  served from its own prompt cache. `NULL` means *undeclared*, and undeclared
  bills cached tokens at the full `price_input` rate: RouteForge never invents a
  discount, so no deployment's cost figures move on upgrade. See
  [`docs/guides/onboarding-models.md`](../guides/onboarding-models.md).
- `usage_records.cached_prompt_tokens` — the provider-reported
  `usage.prompt_tokens_details.cached_tokens` for that call, `0` when the
  provider does not report it (indistinguishable from a genuine cache miss).
  Nullable in the model, written as `0` on every new row.

B50 (published context limits) adds one more column to `onboarded_models`:

```sql
-- 2026-09-22 (B50): where an onboarded model's context window came from
ALTER TABLE onboarded_models ADD COLUMN context_window_source VARCHAR(16);
-- 2026-10-09 (B72, trajectory envelope). The `trajectory_envelopes` table is new and
-- is created at boot; these three columns are on existing tables.
ALTER TABLE app_policies     ADD COLUMN trajectory_mode VARCHAR(16);
ALTER TABLE routing_outcomes ADD COLUMN trajectory_key VARCHAR(32);
ALTER TABLE routing_outcomes ADD COLUMN inherited_entity_types JSON;
```

- `onboarded_models.context_window_source` — `"probed"` when the stored value
  is what RouteForge itself would have recorded (the freshly probed
  `max_model_len`, or, when this probe learned nothing, the existing row's
  own already-`"probed"` value), `"operator"` when the submitted value
  differs from that — a human actually changed it — and `NULL` when
  `context_window` itself is `NULL`. Provenance follows the *value*, not
  whether the field was populated, because the console resubmits the whole
  row on every save: keying it on presence would relabel a probed number
  `"operator"` the moment anyone next edits the model. **Run this `ALTER`
  before rolling the image.** What actually breaks on a database still
  missing it: `GET`/`POST`/`PUT /admin/infra/models` call the store directly
  with no exception handling, so onboarding and editing a model surface a
  raw database error (500) until the column exists — the admin API breaks
  loudly. The serving path does not crash: `ModelRegistry.refresh` wraps its
  load in `try/except` and keeps last-known-good, but a freshly rolled
  process has none, so the onboarded pool reads empty and, by the existing
  "no on-prem model onboarded ⇒ derived reject" rule, `onprem_only` requests
  are **rejected**, never silently sent to cloud — apps that permit cloud
  are unaffected. So on-prem serving does stop, but by refusing requests,
  not by 500ing them; the visible error is on the admin API. Same trap the
  last two deploys hit.

> **A deployment still on pre-P1a code now needs twelve `ALTER`s before the image
> is rolled:** P1a's 4 on `app_policies`, P1b's 5 (2 on `app_policies`, 3 on
> `onboarded_models`), P2a's 2 (1 on `onboarded_models`, 1 on
> `usage_records`), and B50's 1 (on `onboarded_models`).
> `scripts/migrate_app_policies.py` applies all of them and is
> idempotent — it skips a table that is absent and a column that already exists,
> so running it repeatedly is safe. **It has been exercised against SQLite only.
> Run it against a restored Postgres snapshot first; the Spark and DGX both run
> Postgres.** (That dry run is carried forward from P1b and is still outstanding.)

> **Upgrade order for the five P1b columns (2026-09-19): migrate first, then roll the
> image.** Run these five `ALTER`s — or `scripts/migrate_app_policies.py`, which now
> covers both tables and is safe to run repeatedly — **before** rolling any image built
> from the P1b branch. The new image reads `allowed_regions` / `require_no_retention` on
> every policy lookup and the three `onboarded_models` columns on every registry refresh,
> so on a database missing them the failure is a raw DB error, not a clean 422. The old
> image never references them, so applying the `ALTER`s early is always safe.
>
> **No backfill, and no behavior change for existing rows.** All five are nullable with
> no default, so every existing row gets `NULL`. On `app_policies`, `NULL` means *no
> constraint* — `allowed_regions` NULL is "no regional requirement" and
> `require_no_retention` NULL reads as `false`. On `onboarded_models`, `NULL` means
> *undeclared*, which resolves to `unknown`, and an `unknown` endpoint is only ever
> excluded for an application that declared a matching requirement. So after this
> migration **every existing application routes to exactly the endpoints it routed to
> before**, and nothing changes until an operator sets a declaration or a requirement.

> **Upgrade order for the four P1a data-handling columns (2026-09-18): migrate
> first, then roll the image**, same as `max_cost_usd_per_request` above. These
> four are added by Task 2 of the policy-compiler-p1a plan (`app/policy/compile.py`)
> with NO behavior wired to them yet — they add no new decision path on their own —
> but a later task's `AppPolicyView`/`compile_policy` reads them on every policy
> lookup, so on a database without them the same `500` failure mode applies once
> that task's image rolls. Run these `ALTER`s (or `scripts/migrate_app_policies.py`,
> updated with the same four columns) before rolling any image built from a commit
> that reads them. All four are nullable with no default; every existing row gets
> `NULL`, which is "inherit" for each axis, so this migration alone changes no
> app's effective policy.

> **Upgrade order for `max_cost_usd_per_request` (2026-09-15): migrate first, then roll
> the image.** Run this `ALTER` (or `scripts/migrate_app_policies.py`) **before** rolling
> out the new image. The new image reads this column on every policy lookup, so on a
> database without it **every authenticated request fails with `500`** — not just admin
> writes — until the column exists. The old image never references the column, so
> applying it early is safe.

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

### Pre-roll audit: `policy_mode: passthrough` rows that also set `pii_detection`

**Do this BEFORE rolling any image built from the policy-compiler-p1a branch.** It is a
read-only audit, not a migration, and it is the one behavior change in that branch that
can affect an application already in a deployed database.

Until now, `policy_mode: passthrough` forced the null detector *whatever* the app's own
`pii_detection` said. The two axes are independent under the compiler: an app that
**names** a detector now gets it. `passthrough` still means the app's data is untouched —
both `model_input` and `client_output` stay `raw`, so nothing is redacted to the model or
to the client — but **detection runs again**, and detection feeds the declared cross-axis
rule.

The admin API validated `policy_mode` and `pii_detection` independently and never
cross-checked them, so rows in this shape can exist and were accepted at write time. Find
them:

```sql
SELECT application_id, residency, policy_mode, pii_detection, stream_mode, capture_io
FROM   app_policies
WHERE  policy_mode = 'passthrough'
  AND  pii_detection IS NOT NULL;
```

(If the deployment sets `serving.policy.default_policy_mode: passthrough`, widen it to
`WHERE (policy_mode = 'passthrough' OR policy_mode IS NULL) AND pii_detection IS NOT NULL`
— a `NULL` `policy_mode` inherits that default and is just as affected.)

For every row returned, after the roll a request whose content trips a
`config/policy.yaml` `sensitivity_rules` entry can now:

| Consequence | What the operator sees |
|---|---|
| **Residency raised to `onprem_only`** by the rule | cloud providers drop out of `allowed_providers` for that request |
| **A derived reject** if that app has no healthy on-prem model | `onprem_only` with an empty candidate set is rejected, never silently sent to cloud — the correct behavior, but it is a *new* rejection for this app |
| **`require_verification` set** | step 9 runs the full verification path |
| **`stream_unverified` floored** | the request falls back to `verified_then_stream` (`StreamFallback`); real token streaming stops for those requests |
| **Semantic-cache bypass** | a restricted/high-risk classification bypasses the cache, so cached-hit rate drops |
| **`detected_entities` populated** | entity *types* appear on the response `policy` block and on the trace (values never do) |

None of these weakens anything — every one is a *tightening*, and the change exists
because the old behavior was the counterexample `CE-3` ("selecting `passthrough` silently
disabled placement escalation for a detected SSN"). But an app that was quietly running
with escalation disabled may start being rejected or stop streaming, so audit before the
roll rather than diagnosing it afterwards.

**If a row should keep the old behavior**, set its detector off explicitly — this is the
configuration that has always meant "scan nothing", and it is unchanged by the compiler:

```sql
UPDATE app_policies SET pii_detection = 'off'
WHERE  policy_mode = 'passthrough' AND pii_detection IS NOT NULL;
```

Do that per application after confirming with its owner, not as a blanket statement:
setting `pii_detection` on a `passthrough` app was previously inert, so some of those
rows are an operator's *intent* that never took effect, and this change grants it.

Rows with `policy_mode = 'passthrough'` and `pii_detection IS NULL` are unaffected: the
preset still resolves detection to `off`, which is exactly what they do today.
