# Choosing an application's settings

**The question this page answers: "I have a new customer app — what should I set?"**

The [FAQ](./faq.md) answers specific questions and
[`per-app-config.md`](../reference/per-app-config.md) is a per-field reference. Neither is what you
want in front of you while onboarding. This page is: three questions, three recipes, and
the handful of things that have actually cost us time.

Read it top to bottom the first time. After that, jump to the recipe.

---

## 1. Start with the decision, not the fields

Ask the customer three questions, in this order. They determine everything else.

**Q1 — Does this app's input contain real personal data that isn't the customer's own?**

Not "could it theoretically". Does a request carry a third party's name, SSN, MRN, or
contact details — someone the customer is holding data *about*? A hospital app: yes. A
CAD app generating setup sheets from the shop's own drawings: no.

- **Yes → Recipe A (Protected).** Stop here; the other questions are refinements.
- **No, and every field of the input is load-bearing output** → continue to Q2.
- **No, but only a handful of values keep getting mis-flagged** → don't change the
  posture at all. Go to [§5, the narrow levers](#5-prefer-the-narrow-levers-first).

**Q2 — Where may compute run?**

`onprem_only`, `private_cloud_ok`, or `any`. This is **placement** and it is a completely
separate decision from Q1. Answer it on the customer's contract and their infrastructure,
never on how much protection you plan to run. `onprem_only` + no protection is a valid,
honest configuration; so is `any` + full protection.

**Q3 — Do they need raw prompts and completions for eval?**

Do they want to open Langfuse and read the actual text a model was sent and returned —
to score it, build an eval set, or debug output quality?

- **Yes → Recipe B (Router only).** Raw capture requires a declared no-protection
  posture *and* the app's own Langfuse project.
- **No, and they'd rather no I/O left the serving path at all → Recipe C.**
- **No, redacted traces are fine → Recipe A.**

| | Q1: third-party PII? | Q3: needs raw I/O? | Recipe |
|---|---|---|---|
| Hospital, HR tool, anything holding data *about* people | yes | — | **A — Protected** |
| Customer's own documents, every field load-bearing | no | yes | **B — Router only** |
| Air-gapped, strictest, or "don't log our text anywhere" | either | no | **C — Protected, no telemetry I/O** |

---

## 2. The three recipes

| Recipe | `policy_mode` | `capture_io` | For |
|---|---|---|---|
| **A — Protected** (default) | `enforced` | `redacted` | Any app whose input may hold real personal data |
| **B — Router only — no PII checks** | `passthrough` | `full` | The customer's own data, every field load-bearing |
| **C — Protected, no telemetry I/O** | `enforced` | `off` | Air-gapped / strictest |

All three are set the same two ways, and they hit the same endpoint and the same
write-time clamps:

- **Console** — *Applications → the app → **Settings** tab.* Every field below has a
  control there, in six numbered steps: **1 Access**, **2 Placement**, **3 Protection**,
  **4 Telemetry**, **5 Delivery and limits**, **6 Privacy rules** (see the
  [UI guide](./ui-guide.md#8a-the-six-steps)). A recipe touches steps 3 and 4 only.
- **API** — `PUT /admin/applications/{app_id}/policy` (admin cookie auth).

> **The console can apply a recipe for you.** A **Start from a recipe** picker sits above
> step 1 with these same three recipes. It shows what it would change as
> `field: current → new` and **fills the form without saving** — you review it and then
> press Save. It sets `policy_mode` and `capture_io` only, and never touches placement —
> so the rule below still holds: `residency` is your decision, set it yourself in step 2.

`residency` appears in the JSON of every recipe because you should always set it
explicitly. It is **not** part of what the recipe decides — see Q2.

---

### Recipe A — Protected (the default)

```json
PUT /admin/applications/{app_id}/policy
{ "residency": "onprem_only",
  "policy_mode": "enforced",
  "capture_io": "redacted" }
```

**Console:** step 3 Protection → policy mode = `enforced` (or leave it at "(global
default)"). Step 4 Telemetry → capture io = `redacted`.

**Who it's for.** Everyone, until someone gives you a reason otherwise. Any app that
processes data *about* people who are not the customer.

**What you get.** PII detection, input redaction before any model sees the text, egress
redaction of the completion, sensitivity escalation, the app's custom recognizers, and
the capture ceiling. Traces carry the redacted prompt and completion, so you can still
debug shape, cost, latency, routing and the rejected-candidate list.

**What it costs you.** The model never sees the real values. For a document generator
that must reproduce its input, that is a correctness bug, not a safety feature — a
dimension label rewritten to `<PERSON_1>` destroys the identity of a value while leaving
the value itself intact, silently. If that's your app, you want §5's narrow levers or
Recipe B.

> `capture_io` is `off` deployment-wide by default (`observability.langfuse.capture_io`
> in `config/serving.yaml`). Setting `redacted` per app is a deliberate opt-in to
> redacted trace I/O, not the state you inherit.

---

### Recipe B — Router only, no PII checks

```json
PUT /admin/applications/{app_id}/policy
{ "residency": "onprem_only",
  "policy_mode": "passthrough",
  "capture_io": "full" }
```

**Console:** step 3 Protection → policy mode = `passthrough`. Step 4 Telemetry → capture
io = `full`.
**Prerequisite:** the app must already have its **own** Langfuse project, or the save is
a 422 — attach it in the same step, directly above `capture_io`; see §6.1 below.

**Who it's for.** The app's entire input is the customer's **own** content, and every
field of it is load-bearing output the app is contractually required to reproduce —
engineering drawings, order documents, contracts, forms the customer already owns. There
is no third party to protect and no enumerable set of shapes to exempt.

**What you get.** RouteForge stays the router, the meter and the tracer: model and tier
selection, cheap-tier cost savings, budgets, rate limits, durable usage metering, the
full `DecisionRecord` per step, the decision stream, and Langfuse traces with the real
text in them. Turning protection off does not turn RouteForge into a dumb proxy.

**What it costs you — say this out loud to the operator.** Nothing is detected, so
nothing is redacted, in either direction:

- **A real SSN pasted into a prompt reaches the model verbatim, and reaches Langfuse
  verbatim.** So does a real name, a real MRN, a real card number. There is no
  entity-level backstop left — `US_SSN` protection is a property of the *detector*, and
  in `passthrough` there is no detector.
- **The app's own custom recognizers do not run either.** This is deliberately stronger
  than `pii_detection: "off"`, which still runs a profile's recognizers. An app the
  operator has declared untouched must not have a leftover custom recognizer quietly
  rewriting its data.
- **Egress verification finds nothing to redact.** Step 9 is handed the same null
  detector, so a completion that leaks PII the input never contained is returned as-is.
- **Anyone with console access to that Langfuse project can read the raw text.** Staying
  on the customer's own infrastructure does not remove that exposure surface; it only
  bounds who can reach it.

This is a decision the operator makes consciously, which is why it is never silent: the
posture is folded into `app_policy_version`, stamped in every response's `policy` block,
and emitted on the trace as `sp.policy_mode`.

**What it does *not* cost you.** Placement. `passthrough` never widens
`allowed_providers`, never clears `onprem_only`, and never touches placement at all. With
`residency: "onprem_only"` the cloud providers are filtered out of the candidate pool
*before* the router scores anything — the on-prem guarantee is fully intact. "Run only on
my hardware, and don't touch my data" is one honest configuration, not a compromise.

> **Never flip residency to get this.** The old workaround —
> `residency: "any"` + an `allowed_providers: ["onprem"]` pin — is strictly weaker: it
> swaps a hard floor cloud cannot survive for a mutable allowlist, and it drops
> `ctx.onprem_only`, which the step-9 streaming floor and other checks key on.
> `policy_mode` exists precisely so nobody has to misdeclare placement to obtain a
> protection posture.

**Deployment ceiling.** `policy.allow_app_passthrough: false` in `config/serving.yaml`
(env `SP_ALLOW_APP_PASSTHROUGH`) forbids this posture install-wide, for every app,
whatever its own policy says. That is the one switch a locked-down install flips instead
of relying on per-app discipline. A malformed value parses as `false` — the ceiling
closes on a typo rather than opening.

---

### Recipe C — Protected, no telemetry I/O

```json
PUT /admin/applications/{app_id}/policy
{ "residency": "onprem_only",
  "policy_mode": "enforced",
  "capture_io": "off" }
```

**Console:** step 3 Protection → policy mode = `enforced`. Step 4 Telemetry → capture io
= `off`.

**Who it's for.** Air-gapped installs, and any customer whose answer to "may we store
your prompt text anywhere" is no. It is also the right shape for the narrower case where
an app needs *unredacted prompts to reach the model* but no raw telemetry at all — add
`"pii_detection": "off"` to the JSON above and keep `capture_io: "off"`. That
combination is airtight: with no telemetry I/O captured there is no egress path for the
unsanitized prompt.

**What it costs you.** Traces carry no prompt or completion text at all — you keep the
decision record, the candidate list, cost, latency, tokens and tier, and lose the
ability to look at what was actually said. Eval on this app has to happen outside
RouteForge.

---

## 3. Worked example — MillWise

MillWise is a CAD/CNC customer. Its prompts are dense machining tables: tool numbers,
offsets, drawing numbers, revision lines, attribute columns. **Every field is
load-bearing output** — the generated setup sheet has to reproduce the input exactly, and
a wrong or missing value is a scrapped part.

**What went wrong under `enforced`.** Presidio is a statistical NER model, and on that
input it did what statistical NER does on dense structured text: it invented people.
Attribute labels like `HARD2` and `SOFT1` were tagged `PERSON` and rewritten to
`<PERSON_1>` / `<PERSON_2>`. The value `42363` was detected and destroyed. Order numbers
landed as `US_DRIVER_LICENSE`; revision dates as `DATE_TIME`; the same value, serialized
compactly by the client's tool layer, came back as `PHONE_NUMBER` on the next turn.

**Why the narrow levers could not finish the job.** We tried them, in order, and they are
still the right first move (§5):

1. `pii_entity_denylist: ["US_DRIVER_LICENSE"]` — fixed order numbers.
2. `+ "DATE_TIME"` — fixed revision lines.
3. `+ "PHONE_NUMBER"` — fixed the compact-serialization type flip.
4. `pii_exempt_patterns` for the identifier *shapes*, to survive the type flipping that
   no list of types could catch.

Each round fixed the reported symptom and the next request found a new one. That is the
signal. **When the list of exemptions is not converging because *every* field is
load-bearing, stop adding exemptions and change the posture.** An exemption list is the
right tool for an enumerable set of false positives; it is the wrong tool for "there is
nothing here to protect."

**Where MillWise landed — Recipe B, exactly:**

```json
PUT /admin/applications/millwise/policy
{ "residency": "onprem_only",
  "policy_mode": "passthrough",
  "capture_io": "full",
  "pii_entity_denylist": [] }
```

`residency` stays `onprem_only` — the on-prem floor never moved, and was never the
problem. The denylist is sent as an explicit `[]` because the PUT merges; omitting it
would have kept the three stored types (see §6.5).
`capture_io: "full"` was accepted with no provisioning step because MillWise already had
its own Langfuse project.

### The trap: the failure was density-dependent

**A short probe came back clean while the full table was being mangled.**

This burned us twice, in two different ways, and both times the conclusion "the fix
didn't work" was wrong about *which* thing was broken:

- Hand-written probe prompts — a few spaced-out fields — scored differently from the real
  payload. Presidio's confidence on a token depends on its surroundings. The same value
  scores `DATE_TIME (0.85)` in a hand-typed JSON blob with spaces after the colons, and
  `PHONE_NUMBER (0.40)` in the client's compact serialization. Denylisting `DATE_TIME`
  made every probe pass while every real turn still mangled.
- A conformance probe result can also simply **predate the config change**. See
  §6.4.

**So: test with the customer's real payloads, at real density, through their real
client.** Mint a temporary token (`POST /admin/applications/{id}/tokens`), send the exact
request shape their app sends — including `tools` / `tool_choice` if they use tool
calling — read `policy.detected_entities` off the response, and revoke the token. A
five-field probe proves nothing about a two-hundred-row table.

---

## 4. What none of these recipes changes

Worth being able to say without hedging:

- **Placement is untouched by protection.** `residency` governs where compute may run and
  nothing else; `policy_mode` governs what the gate does to the data and nothing else.
  Neither is ever inferred from the other. `onprem_only` remains absolute in either
  posture — cloud is filtered out of the candidate pool by construction, never checked
  after the fact.
- **The gate still fails closed.** In `enforced`, a gate error rejects the request; there
  is no "no policy = allow" path.
- **`US_SSN` can never be denylisted, thresholded, or pattern-exempted.** It is in
  `FilteredDetector.PROTECTED_ENTITIES`: naming it in `pii_entity_denylist` is a 422, no
  `pii_score_threshold` can drop it, and no regex — however SSN-shaped — can un-redact
  one. This holds however an `enforced` app is tuned. (It does not survive `passthrough`,
  because `passthrough` removes the detector entirely — that is the whole point of making
  the posture explicit and versioned.)
- **Raw I/O is tenant-isolated.** `capture_io: "full"` only ever writes to the app's own
  Langfuse project. The shared project never receives raw I/O, and the exporter fails
  closed to `redacted` if the app's project is missing at export time.
- **`stream_unverified` is never available to an `onprem_only` app**, in any posture, and
  an `onprem_only` or `require_verification` request can never have step-9 verification
  waived regardless of the app's `stream_mode`.

---

## 5. Prefer the narrow levers first

**`passthrough` is not the answer to "the gate redacted something it shouldn't have."**
It is the answer to "there is nothing here to protect and no enumerable set of shapes to
exempt." Those are different problems.

If a handful of false positives are the issue, stay in `enforced` and reach for these —
they keep every other recognizer running, carry no clamp of their own at any residency or
posture, and are not subject to the `allow_app_pii_downgrade` ceiling:

| Lever | Console | Use when |
|---|---|---|
| `pii_entity_denylist` | step 3 Protection → Detection & narrowing → **exempt types** | A few entity *types* are consistently wrong on this app's data (an order number tagged `US_DRIVER_LICENSE`) |
| `pii_score_threshold` | step 3 Protection → Detection & narrowing → **min confidence** | Low-confidence recognizers are the noise; many false positives score around 0.6 |
| `pii_exempt_patterns` | step 3 Protection → Detection & narrowing → **exempt values** | The same *value shape* keeps landing under a different type from one request to the next |

Full field reference: [`per-app-config.md`](../reference/per-app-config.md).

Two intermediate stops between "narrow lever" and Recipe B, before you conclude the whole
posture has to go:

- **`pii_detection: "off"` with `capture_io: "off"`** — the model gets unredacted prompts;
  nothing is traced. Narrower than `passthrough` in what it permits (no raw telemetry) but
  blunter in what it disables (the detector sees nothing) — and note it still runs the
  app's custom recognizers, which `passthrough` does not.
- **`pii_detection: "regex"`** — a scoped, deterministic detector instead of statistical
  NER. Worth trying when the false positives are all Presidio's model being creative on
  structured text, which is exactly the MillWise failure mode.

Both are PII downgrades and both are gated by `policy.allow_app_pii_downgrade`.

---

## 6. Gotchas that will cost you an afternoon

### 6.1 `capture_io: "full"` needs the app's own Langfuse project — **project** keys, not org keys

`capture_io: "full"` is rejected with a 422 unless the app has its own provisioned
Langfuse project. Two ways to get one:

- **Auto-provision on create** — set `LANGFUSE_ORG_PUBLIC_KEY` / `LANGFUSE_ORG_SECRET_KEY`
  once for the deployment, and every new application gets a dedicated project
  automatically. These are **organization**-scoped keys, from **Organization → Settings →
  API Keys**. This path is Langfuse Enterprise-only on self-hosted.
- **Attach by hand** — `PUT /admin/applications/{app_id}/langfuse` with a project id,
  public key, secret key and host. **These must be that project's own project-scoped
  keys, from Project → Settings → API Keys — not the organization keys.** They look
  identical (both are `pk-lf-…` / `sk-lf-…`), which is exactly the trap. Org keys here
  will not do what you want.

Two more details on the manual path that bite:

- `host` is the **server-reachable OTLP ingestion** host (in-cluster, e.g.
  `http://langfuse-web:3000`), not the browser URL you're looking at.
- The secret is encrypted at rest with `ENCRYPTION_KEY`.

The console now puts the two together: **the Langfuse credential fields are in step 4
Telemetry, directly above `capture_io`**, precisely because one gates the other. (They
used to sit at the top of the Settings tab, roughly a thousand pixels above the control
they unlock, and the dependency was invisible.) Attach the credentials with their own
**Save credentials** button first — the step's summary line reads *"own Langfuse project
✓"* once they are set — then pick `full` and save the policy. Pick `full` first and the
save 422s.

### 6.2 `capture_io: "redacted"` is refused whenever nothing is sanitized

Two independent 422s, each naming which guardrail fired:

- `policy_mode: "passthrough"` + `capture_io: "redacted"`
- `pii_detection` of `off` or `regex` + `capture_io: "redacted"`

The reason is the same in both cases and it is not pedantry: the `redacted` exporter path
**reuses the gate's sanitized prompt**. When the gate sanitized nothing, that "sanitized"
prompt is the raw one — so `redacted` telemetry would carry raw I/O under a label saying
it doesn't, into the *shared* project. Use `full` (with the app's own project) or `off`.

Every clamp is evaluated on the **effective post-write value**, not on what your request
body happens to name. A partial PUT that touches only `pii_detection` is still judged
against the app's stored `capture_io`, so a stale `redacted` is re-validated on every
write.

### 6.3 Headroom is a separate data-integrity risk, and `policy_mode` does not cover it

**This is the one that will surprise you.** `passthrough` turns off the *policy gate*. It
says nothing about step 5, Conditional Context Optimization (Headroom), which rewrites
message text for a different reason entirely — token reduction.

The default compressor (`DeterministicCompressor`, `app/compression/compressors.py`) is
lossy in exactly the way a column-aligned table cannot survive:

- it collapses every run of **2+ spaces or tabs to a single space**
  (`_MULTI_SPACE = re.compile(r"[ \t]{2,}")`), destroying column alignment;
- it **drops consecutive duplicate non-blank lines**, so two identical rows in a table
  become one;
- it strips trailing whitespace and collapses runs of 3+ blank lines.

**And its safety bypasses cannot fire under `passthrough`.** `bypass_on.high_risk` keys on
`ctx.risk_class == "high"` and `bypass_on.restricted_sensitivity` keys on
`ctx.sensitivity == "restricted"` — but both of those are derived from *detected entities*
(`_match_rule` in `app/policy/context.py`). In `passthrough` nothing is detected, so
classification falls through to the public/low catch-all rule and **neither bypass ever
triggers**. Precisely the app that most needs its text left alone is the app whose
content-based bypasses are silently inert.

What is left protecting a `passthrough` app is only:

- `bypass_on.user_prompt: true` — which protects the **last message only**; and
- `min_tokens: 2000` — below which compression is skipped, and a dense table is well
  above it.

**Where this actually bites, checked against the code:**

| Path | Compressed? |
|---|---|
| Plain single-prompt request | **No, in practice.** The request is flattened into one message, which `user_prompt: true` protects. Only memory-recalled context prepended ahead of it is eligible. |
| Client-driven tool calling (`tools` in the request, or replayed tool turns) | **No.** Bypassed outright with `bypass_reason: "tool_calling"` — Headroom would corrupt tool_call/tool_result structure. |
| Multi-step agent path | **Yes.** Every hop re-enters compression (`app/agent/internal_endpoint.py`) and compresses every message but the last — including tool outputs and history carrying the customer's tables. |

**The recommendation for a load-bearing-data customer: leave Headroom inheriting the
global** (`compression_enabled: null`, step 5 Delivery and limits → Headroom = "(global
default)"). The deployment default
is `context_optimization.enabled: false`, so today that means off. Do not set
`compression_enabled: true` on such an app, and if the operator ever turns Headroom on
deployment-wide, set `compression_enabled: false` explicitly on every load-bearing-data
app first.

> One residency note while you're here: Headroom's CCR caches the *originals* locally so
> they can be retrieved. Under `passthrough` those originals are the raw customer text.
> CCR is local-only, TTL-bounded and never exported to telemetry by construction — but it
> is a second copy of the data inside the boundary, which is another reason to leave
> compression off for these apps.

### 6.4 `app_policy_version` moves on every change — hand it to the customer

Every field on this page is folded into `AppPolicyView.version()`: residency, the
provider/tier/model allowlists, `policy_mode`, `pii_detection`, the denylist, the score
floor, the exempt patterns, `capture_io`, `compression_enabled`, `stream_mode`, budget and
rpm. The result is stamped in **every response's `policy` block** and onto the trace as
`sp.app_policy_version`.

**Tell the customer to record it alongside their conformance-probe result.** That is the
whole reason it exists: a probe that stores the version it ran against can tell a stale
PASS from a live one, instead of you doing pod-log timestamp forensics to work out that
the failing probe at 10:50 predated the config commit at 10:52. Every change you make
moves the stamp, immediately and visibly.

### 6.5 The PUT merges — `null` keeps a value, `reset` clears it

`set_policy` persists `body.model_dump(exclude_none=True)`. A field your request omits —
**or sends as an explicit JSON `null`, which parses the same way** — keeps the
application's currently stored value. It is left untouched, not blanked back to "inherit
the deployment default."

- `{"pii_entity_denylist": null}` **does not clear a denylist.** Send `[]`, or name the
  field in `reset`. Same for `pii_exempt_patterns` and every other list field.
- To actually put a field back to `null` = inherit the deployment default, name it in the
  `reset` list: `{"reset": ["policy_mode", "pii_score_threshold"]}`. An unknown name is a
  422; naming a field in `reset` *and* sending a value for it in the same request is a
  422 contradiction.
- **Console:** moving a control back to its **"(global default)"** option clears that
  field. Each of steps 2–5 lists exactly which stored values a save will clear, above its
  Save button — and one **Save policy** press writes all four steps at once.

Every clamp is evaluated on the post-reset effective value, and a rejected write applies
nothing at all. Worth knowing for Recipe B specifically:
`{"reset": ["policy_mode"]}` alone on a `passthrough` + `capture_io: "full"` app is
refused — it would leave the app `enforced` and still storing raw I/O. Clear the capture in
the same write: `{"capture_io": "off", "reset": ["policy_mode"]}`.

### 6.6 New columns need a manual `ALTER` on an already-deployed database

RouteForge has no migration tool. `create_all` only creates *missing tables*; it never
`ALTER`s an existing one. On a database that predates a field's release, the first admin
write touching it fails with a raw database error rather than a clean 422 — **including
from the console**, which writes through the same endpoint. Run the `ALTER`s listed in
[`per-app-config.md`](../reference/per-app-config.md#manual-db-migration-for-already-deployed-databases)
before rolling the new image.

---

## 7. Turning protection on later

**`passthrough` is not a one-way door.** This matters when a customer is deciding, and it
is the reason starting there is acceptable rather than reckless.

To turn protection back on:

```json
PUT /admin/applications/{app_id}/policy
{ "policy_mode": "enforced" }
```

**One field.** Console: step 3 Protection → policy mode = `enforced`.

What happens:

- The gate resumes on the next request: detection, input redaction, egress redaction,
  sensitivity escalation, custom recognizers, the capture ceiling.
- **The narrow levers were kept, not cleared, and apply again immediately.** Anything
  stored in `pii_entity_denylist`, `pii_score_threshold` or `pii_exempt_patterns` while
  the app was in `passthrough` was dormant, not deleted — under `passthrough` the console
  collapses the Detection & narrowing group to a one-line summary that names what is being
  kept ("inert while passthrough — 3 exempt types kept"), but never destroys their values.
  The group stays openable and editable there. Flipping the
  posture must never throw away the tuning someone spent an afternoon on. So a year of
  accumulated exemption work survives the round trip.
- `app_policy_version` moves on the next response, which is how the customer's
  conformance probe learns the posture changed without anyone having to tell it.
- **You must deal with `capture_io` in the same write** if it is `full`: an `enforced`
  app cannot hold `capture_io: "full"` at any residency, and an `onprem_only` +
  `enforced` app certainly cannot. Send it together:
  `{"policy_mode": "enforced", "capture_io": "off"}` (or `"redacted"`, which is legal
  again once the gate is sanitizing something).
- Placement does not move, in either direction. It never did.

The reverse trip — `enforced` → `passthrough` — is the same single field, plus the
`capture_io` pairing rules and the app's own Langfuse project if you want `full`.

---

## 8. Onboarding checklist

1. [ ] Ask Q1/Q2/Q3 (§1) and pick a recipe.
2. [ ] Set `residency` from the contract, independently of the recipe.
3. [ ] For Recipe B: confirm the app has its own Langfuse project **first**, with
       *project*-scoped keys (§6.1).
4. [ ] `PUT …/policy` with the recipe's JSON. Send `[]` for any list you mean to clear.
5. [ ] Leave `compression_enabled` at "(global default)" for a load-bearing-data app
       (§6.3).
6. [ ] Probe with the customer's **real payload**, at real density, through their real
       client — not a hand-typed sample (§3).
7. [ ] Read `policy.app_policy_version` off the response and hand it to the customer for
       their conformance probe (§6.4).

## See also

- [`per-app-config.md`](../reference/per-app-config.md) — the per-field reference for every knob
  named here, with the exact clamps and their 422 messages.
- [`faq.md`](./faq.md) — the specific questions these recipes generalize.
- [`ui-guide.md`](./ui-guide.md#8a-the-six-steps) — what each console control does, step
  by step.
- [`admin-setup.md`](./admin-setup.md) — creating the application,
  tokens, and per-app Langfuse credentials (console: steps 1 and 4).
- [`langfuse-per-app-projects.md`](./langfuse-per-app-projects.md) — auto-provision vs.
  the manual per-app key path, in full.
