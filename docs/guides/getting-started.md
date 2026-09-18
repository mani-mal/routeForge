# RouteForge — Setup & Usage Guide

**Read this first.** It explains, in plain language, what RouteForge is, how to set it up,
and — the part most people get stuck on — **what every policy option means and how to choose
between the values.** Once this makes sense, the companion doc
[`architecture/sample-request-flows.md`](../architecture/sample-request-flows.md)
walks real requests through the internals and the open-source libraries.

- **This doc = the *what* and the *how to configure*.** Concepts and decisions.
- **`sample-request-flows.md` = the *how it works inside*.** Worked traces.
- **[`admin-setup.md`](./admin-setup.md) = the exact HTTP reference.**
  Every endpoint and curl. This guide links to it rather than repeating it.

---

## 1. What RouteForge does (one minute)

Your apps already know how to call OpenAI. RouteForge is a drop-in **OpenAI-compatible
endpoint** you host yourself. Point an app at it with an API token and it sends the same
`POST /v1/chat/completions` it always would. For every call, RouteForge:

1. **Checks privacy first** — scans the text for PII/PHI, redacts it, and decides where the
   request is *allowed* to run (on-prem only? cloud OK?).
2. **Routes to the cheapest allowed model** — a trivial question goes to a cheap/local model;
   only hard requests reach an expensive frontier model.
3. **Verifies the answer** — checks the output for leaks before returning it.
4. **Returns a normal `ChatCompletion` *plus a decision trace*** — a record of what it decided
   and why (which model, what it cost, what it rejected).

The value is that **you don't change your app** — you change *policy*, and RouteForge enforces
privacy and cost rules on every call. It is a **pure router**: it points at model endpoints you
run; it never hosts or scales inference itself.

---

## 2. Two credentials — don't mix them up

This is the single most common setup mistake:

| Credential | Who holds it | What it's for |
|---|---|---|
| **App token** — `rf_live_…`, sent as `Authorization: Bearer` | Your application | Making inference calls: `POST /v1/chat/completions` |
| **Admin session** — a login cookie | A human operator | Managing apps and policy (`/admin/*`), watching traffic (`/api/*`) |

An app token **cannot** manage anything; an admin login is **not** used for inference. You (the
operator) log in as admin, create an app, and hand its token to the developer.

---

## 3. The setup path, in order

The on-prem model pool **boots empty** — RouteForge ships with no bundled model. So the order
matters:

```
boot RouteForge  →  onboard a model  →  create an app  →  set its policy  →  hand over the token  →  the app calls /v1
```

**① Boot** (local, no GPU/keys/network needed — uses regex PII + SQLite):
```bash
docker compose -f deploy/compose/docker-compose.yml up --build
```
Brings up the API (`:8080`), the console UI (`:3001`), and self-hosted Langfuse (`:3000`).

**② Onboard a model.** Because the pool is empty, point RouteForge at your own
OpenAI-compatible endpoint (vLLM / NIM / Azure / etc.). Console → Infrastructure → Onboard, or
`POST /admin/infra/models`. RouteForge **probes it first** and only enables it if it answers.
Full details: [`onboarding-models.md`](onboarding-models.md).

**③ Create an app.** `POST /admin/applications` mints the app and returns its token **once**
— copy it now. (Exact call: [`admin-setup.md` §2](./admin-setup.md).)

**④ Set its policy.** `PUT /admin/applications/{id}/policy` — this is where all the options in
§4 below live. Policy **merges**: a field you omit keeps its stored value.

**⑤ Hand the developer** the base URL (`…/v1`) and the token. They call it like OpenAI:
```bash
curl http://localhost:8080/v1/chat/completions \
  -H "Authorization: Bearer rf_live_…" -H "Content-Type: application/json" \
  -d '{"model": "auto", "messages": [{"role":"user","content":"hello"}]}'
```
Note `"model": "auto"` — the app does **not** pick the model; RouteForge's router does, within
the policy you set.

---

## 4. Every policy option, explained

These are the fields on `PUT /admin/applications/{id}/policy`. Any field left `null` (unset)
means "inherit the deployment default / add no extra restriction from this app."

> **Default vs. ceiling — the distinction the rest of this section relies on.** These
> are three different things and blurring them is how "a per-app policy can only narrow
> the global rules" got written down, which is not the rule:
>
> - A **default** is what an app inherits when it leaves a field `null`. An app may
>   explicitly differ from it.
> - A **ceiling** — `policy.allow_app_passthrough`, `policy.allow_app_pii_downgrade` —
>   is an absolute deployment constraint. No app may weaken past it, and clearing a field
>   does not escape it: a `reset` is judged on the value the app will inherit afterwards.
> - An **explicitly permitted per-app downgrade** may legitimately differ from the
>   default without violating any ceiling. An app *may* set `pii_detection: "off"` on a
>   deployment whose default is `presidio`, as long as `allow_app_pii_downgrade` permits
>   it — that is a downgrade the deployment has authorised, not a widening.
>
> The precedence order, in full: **deployment ceiling → explicit per-app value →
> `policy_mode` preset → deployment default.** Anything that cannot be parsed denies.
> Authoritative version, with every error code:
> [`per-app-config.md`](../reference/per-app-config.md#the-axes--the-authoritative-model).

Quick map of what each field controls:

| Field | In one line |
|---|---|
| `residency` | **Where** the request may run (on-prem only? cloud OK?). The strongest privacy lever. |
| `allowed_providers` | Which backends this app may use: `onprem`, `azure`, `bedrock`. |
| `allowed_tiers` | Which cost tiers it may use: `cheap`, `mid`, `frontier`. |
| `allowed_models` | Optionally pin exact model slugs. |
| `max_spend_usd_per_day` | Daily USD budget; over it → request rejected. |
| `max_rpm` | Requests-per-minute cap. |
| `compression_enabled` | Turn Headroom context compression on/off for this app. |
| `policy_mode` | A **preset** over `model_input` and `client_output` — the coarse protection switch. *(detailed below)* |
| `model_input` | `raw` \| `redact` — what the **model** is shown. Set it to override the preset. *(API only — no console control yet.)* |
| `client_output` | `raw` \| `redact` — what the **client** receives. Independent of `model_input`. *(API only.)* |
| `content_integrity` | `exact` disables all lossy compression for this app. A data control, not a privacy one. *(API only.)* |
| `escalate_residency_on_detection` | Whether a declared sensitivity rule may **raise** this app's residency. Default `true`; raise-only either way. *(API only.)* |
| `pii_detection` | **How hard the gate looks for PII** — which detector runs. *(detailed below)* |
| `pii_entity_denylist` / `pii_score_threshold` / `pii_exempt_patterns` | Narrow the gate without disabling it — exempt specific entity types, low-confidence matches, or value shapes. See [`per-app-config.md`](../reference/per-app-config.md). |
| `capture_io` | **How much of the prompt/answer is stored in telemetry.** *(detailed below)* |
| `stream_mode` | Whether/how token streaming is allowed. *(summary below)* |
| `reset` | The one way to put a field *back* to `null` — see the merge note below. |

> **The PUT merges — `null` does not clear.** A field you omit, *or send as an
> explicit JSON `null`*, keeps the app's currently stored value. To clear a list,
> send `[]`. To return any field to "inherit the deployment default", name it in
> `reset`, e.g. `{"reset": ["policy_mode", "pii_score_threshold"]}`. Every floor
> below is evaluated on the **effective post-merge, post-reset** state.

### `residency` — where a request may run

| Value | Meaning |
|---|---|
| `any` | No location restriction — cloud or on-prem, whichever the router prefers. |
| `private_cloud_ok` | Approved private-cloud providers allowed; used for "sensitive but not top-secret." |
| `onprem_only` | **Absolute.** Cloud providers are removed from the candidate pool *before* routing even scores. If no healthy on-prem model exists, the request is **rejected** — never quietly sent to cloud. |

Falls back to the app's `default_residency` when unset. This is the field that enforces
"sensitive data never leaves our infrastructure."

### `allowed_providers` / `allowed_tiers` / `allowed_models`

Three narrowing filters on which models the router may pick:
- **`allowed_providers`** — subset of `onprem | azure | bedrock`.
- **`allowed_tiers`** — subset of `cheap | mid | frontier` (cost/capability bands).
- **`allowed_models`** — specific model slugs (e.g. `azure-gpt-mid`) when you need exact control.
  Usually left null; the tier/provider filters are enough.

### `max_spend_usd_per_day` / `max_rpm`

Guardrails. Over the daily USD cap or the requests-per-minute rate → the request is rejected
`429` and a `rejected` usage row is recorded (so overspend is visible, not silent).

### `compression_enabled`

Turns on **Headroom** context compression for this app (step 5). It shrinks tool outputs and
history *before* the model call, so you pay for fewer tokens. `null` inherits the global toggle.
Safety bypass rules always still apply (high-risk/restricted content is never compressed). It's
off by default so you can measure spend before/after.

---

### `policy_mode` — the protection posture (start here)

**This is the coarse switch; `pii_detection` and the narrowing levers are refinements
*within* it.** RouteForge keeps two things apart that are easy to confuse:

| Axis | Field | Question it answers |
|---|---|---|
| **Placement** | `residency` | *Where may compute run?* |
| **Protection** | `model_input`, `client_output`, `capture_io`, `pii_detection`, `content_integrity`, `stream_mode` | *How much is done to the data?* |

They are **independent**. No protection setting ever moves placement, and no placement
setting ever changes a protection value. `policy_mode` never widens `allowed_providers`,
never clears `onprem_only`, and never touches placement.

`policy_mode` is a **preset over two of those axes**, not a switch of its own:

| Value | Compiles to | Meaning |
|---|---|---|
| `enforced` *(default; also what `null` inherits)* | `model_input: redact`, `client_output: redact` | Privacy-first: input redaction before any model, egress redaction of the response, sensitivity escalation, custom recognizers, the capture ceiling. |
| `passthrough` | `model_input: raw`, `client_output: raw` — plus `pii_detection: off` **only when the app names no detector** | RouteForge is a **router, meter and tracer only** — nothing redacted in either direction. |

Setting `model_input` or `client_output` explicitly overrides the preset. That is how you
express the combination no preset produces: `model_input: "raw"` with
`client_output: "redact"` — the in-boundary model sees the real values, the client
response does not.

Reach for `passthrough` when the app's whole input is the customer's **own** content
and every field of it is load-bearing output it must reproduce (engineering drawings,
order documents, contracts). There, redaction is a correctness bug, not a safety
feature. `passthrough` disables PII detection/redaction and output verification — it
does **not** disable context compression: if Headroom is enabled, it can still
transform tool outputs/history, so a byte-exact requirement also needs compression
turned off for the app. `onprem_only` + `passthrough` — *"run only on my hardware, and
turn the gate off"* — is a valid, honest configuration.

**Guards:** the deployment ceiling `policy.allow_app_passthrough` in `serving.yaml`
(default `true`; set `false` and no app may take the posture — and it gates
`model_input: raw` and `client_output: raw` independently); `capture_io: redacted` is
refused only when the app's `pii_detection` resolves to `off`, which is what a
`passthrough` app that names no detector inherits — a `passthrough` app that *does* name
`regex` or `presidio` may use `redacted`; and the posture is never silent — it is folded
into `app_policy_version`, stamped in every response's `policy` block, and emitted on the
trace as `sp.policy_mode`.

> **The `passthrough` stamp is derived, not stored.** It reads `passthrough` exactly when
> `model_input` and `client_output` both resolve to `raw` — including when a ceiling
> refused the app's requested posture, in which case it reads `enforced`. The stamp always
> names what ran, never what was asked for.

Unset inherits the deployment-wide `policy.default_policy_mode` in `config/serving.yaml`
(env `SP_DEFAULT_POLICY_MODE`). Deep dive: [`per-app-config.md`](../reference/per-app-config.md);
decision guide: [`choosing-app-settings.md`](./choosing-app-settings.md).

---

### `pii_detection` — how hard the gate looks for PII  ⭐ (your question)

**What it is:** before *anything* downstream sees the request, the gate scans the text for
sensitive entities (SSNs, names, emails, medical IDs…) and **replaces each one with a
placeholder** (e.g. `123-45-6789` → `<US_SSN>`). This field controls which *detector* does that
scan for this app.

| Value | What it does | Cost / tradeoff |
|---|---|---|
| `presidio` *(default; also what `null` inherits)* | Full **Microsoft Presidio** + spaCy NER, plus the app's custom recognizers. Catches many entity types including names, locations, dates. | Most thorough; needs the `presidio` extra installed. |
| `regex` | A lightweight built-in regex detector with **exactly five patterns** — `US_SSN`, `CREDIT_CARD`, `EMAIL_ADDRESS`, `PHONE_NUMBER`, `US_BANK_NUMBER` — plus the app's custom recognizers. | Fast, no ML dependency. It genuinely redacts those five shapes, and *nothing else*: no names, no addresses, no dates, no MRNs. |
| `off` | **No built-in detector.** The app's own custom recognizers still run. | Use only when the app *needs* the real values (see below). |

> **`off` is not "guaranteed raw passthrough."** It switches off the *built-in* detector.
> A privacy profile's custom recognizers are still layered on and still detect, classify
> and (while `model_input` is `redact`) redact. The only configuration that scans nothing
> at all is `pii_detection: off` **together with** `model_input: raw` and
> `client_output: raw` — i.e. `policy_mode: "passthrough"` with no named detector, which
> gets a bare null detector with no recognizer layering.
>
> **A detector is built when any consumer needs one** — `model_input: redact`, or
> `client_output: redact`, or `capture_io: redacted`, or
> `escalate_residency_on_detection`. `pii_detection` chooses its *kind*, not whether it
> exists.

**Why an app would ever turn detection down or off:** redaction is destructive. A
document-generation app that must echo back real customer names, order numbers, or addresses
would get placeholders instead of the real values. `pii_detection: off` (or a scoped `regex`) is
the escape valve for those apps.

**The key thing to understand — detection ≠ residency.** Turning detection down does **not**
let data escape on-prem. Residency is enforced *separately*. An `onprem_only` app with
`pii_detection: off` still runs on-prem only. Detection controls *classification and redaction*,
not *where the request may go*.

**Safety floors (enforced when you save the policy — you'll get a `422` if you violate one):**
- **A deployment-wide lock:** `allow_app_pii_downgrade` in `serving.yaml` (default `true`). A
  hospital-grade deployment sets this `false` once, and then *no* app can downgrade detection.
- **`off` is rejected if `capture_io` is `redacted`** (`CAPTURE_REDACTED_NEEDS_A_DETECTOR`)
  — redacted telemetry needs a detector to build its redacted copy with, and with
  detection off there is none. (Pair `off` with `capture_io: off` or `full` instead.)
  **`regex` + `capture_io: redacted` is allowed** — the regex detector still redacts, so
  the copy is genuinely redacted, just at regex quality (see the five patterns above).
  **This clamp keys on the detector, not on the posture:** telemetry builds its *own*
  redacted copy of the raw values rather than reusing whatever the model was shown, so
  `policy_mode: "passthrough"` + `capture_io: "redacted"` is legal whenever the app names
  a detector.
- **`onprem_only` apps *may* downgrade detection** — detection and residency are independent
  axes, so this buys nothing to refuse. In the `enforced` posture, `pii_detection: "off"`
  needs `capture_io: "off"`: with no telemetry I/O captured there is no egress path for
  the unsanitized prompt. (`pii_detection: "regex"` is not `off` — it still sanitizes —
  so an `onprem_only` app running `regex` may also use `capture_io: "redacted"`; only
  `capture_io: "full"` needs `policy_mode: "passthrough"`, regardless of `pii_detection`.)
  Do **not** flip residency to `any` + an `allowed_providers` pin to work around a
  detection or telemetry rule — that swaps a hard residency floor for a mutable allowlist
  and is strictly weaker.
- An unrecognized value is a `422`, never a silent fallback to `off`.

**Prefer the narrow levers.** `off` blinds the gate to *everything*, a real SSN included.
When only a few false positives are the problem, keep detection on and use
`pii_entity_denylist` (exempt entity *types*), `pii_score_threshold` (drop low-confidence
matches) or `pii_exempt_patterns` (exempt *value shapes*). They carry no clamp of their own
at any residency, and `US_SSN` can never be exempted by any of them.

Deep dive: [`per-app-config.md`](../reference/per-app-config.md).

---

### `capture_io` — how much prompt/answer text is stored in telemetry  ⭐ (your question)

**What it is:** separate from *routing*, RouteForge records every request for observability
(Langfuse traces, the console). This field controls **how much of the actual prompt and
completion text** gets stored. Usage/billing records **never** contain text regardless — this is
only about the trace payload.

| Value | What's stored in the trace | Use it for |
|---|---|---|
| `off` *(deployment default)* | **No prompt/answer text at all** — only metadata (model, tokens, cost, latency, sensitivity, status). | Maximum privacy. The safe default, and the one setting that is always legal at any residency and any posture. |
| `redacted` | A redacted copy of the prompt and the answer that **telemetry builds itself**, with the app's detector — not a reuse of whatever the model was shown. | Debugging and observability without exposing raw PII, *including* for an app whose model sees raw text. |
| `full` | The **raw, unredacted** prompt and completion. | Evaluation / prompt-tuning where you need the real text. **Tightly gated (below).** |

**How to choose:**
- Start at **`off`** (or the deployment default). You still get full cost/routing metadata.
- Move to **`redacted`** when developers need to *see the conversations* to debug, but you
  never want raw PII in Langfuse.
- Use **`full`** only for an app you deliberately want raw I/O from — and only under these
  guardrails, each enforced independently at policy-write time:
  - **Only to the app's *own* Langfuse project.** `full` is rejected unless the app has its own
    provisioned project; raw I/O never touches the shared/default project. **This condition is
    never relaxed by anything.** If that per-app project can't be built at runtime, the
    exporter steps `full` down to **`redacted`** — and if no detector ran, it steps
    `redacted` down to **`off`**, never to raw. No code path exports raw content under a
    `redacted` label.
  - **An `onprem_only` app must first declare a no-protection posture** — that is
    `model_input: "raw"` **and** `client_output: "raw"`, which is exactly what
    `policy_mode: "passthrough"` resolves to. While it is in the `enforced` posture the
    gate is running, so residency beats eval convenience and `full` is refused
    (`CAPTURE_FULL_ONPREM_NEEDS_NO_PROTECTION`). A **non-`onprem_only`** app may hold
    `full` in either posture, own project still required. That is the whole full-capture
    rule; see [`choosing-app-settings.md`](./choosing-app-settings.md#the-full-capture-rule),
    which states it identically. Placement does not move either way: `onprem_only` stays
    absolute in both postures.
  - **`redacted` is refused only when `pii_detection` resolves to `off`**
    (`CAPTURE_REDACTED_NEEDS_A_DETECTOR`) — redacted telemetry needs a detector to redact
    with. It is **no longer** refused merely for being `passthrough`: telemetry projects
    independently now, so a `passthrough` app that names `regex` or `presidio` may use
    `redacted`. Use `full` (own project) or `off` if you want no detector at all.

  *(Before the 2026-09-05 `policy_mode` release, `onprem_only` was used as a proxy for
  "maximum protection" and `full` was refused for on-prem apps outright. That forced
  customers to misdeclare residency to get a protection posture, which is strictly less
  safe — hence the split into two explicit axes.)*

In short: `off` = metadata only, `redacted` = safe text, `full` = raw text for eval behind a
fence. Deep dive: [`per-app-config.md`](../reference/per-app-config.md).

---

### `stream_mode` — whether token streaming is allowed

By default RouteForge **rejects** `stream: true`, because step-9 verification needs the *whole*
answer before releasing it. An app can opt into one of four modes:

| Value | Behavior |
|---|---|
| `off` *(default)* | `stream: true` is rejected. |
| `verified_then_stream` | Full verified pipeline runs, then the finished answer is chunked out. Safe; no speed benefit. |
| `stream_unverified` | Real token streaming; step-9 verification **waived** (recorded in the trace). Blocked for `onprem_only`. |
| `incremental_verify` | Real token streaming through a hold-back redactor that never releases a raw entity split across chunks. Weaker than `verified_then_stream` (can't enforce a whole-response verdict). |

**Fail-closed floor:** a request falls back to `verified_then_stream` — visibly, never
silently — for exactly three reasons, none of which an app setting can lower: (1)
`onprem_only` or `require_verification` holds *for that request*; (2) it is a client-driven
tool conversation (which needs the structured path, not a privacy matter); (3) the app set
`stream_unverified` **and** an explicit `client_output: "redact"`.

> **`stream_unverified` waives egress redaction even under `enforced`.** Both the
> write-time 422 and the runtime floor key on an **explicitly set** `client_output:
> "redact"`, not on the effective value — otherwise every `stream_unverified` app in
> existence would be floored, repealing the mode. So an `enforced` app that opts into
> `stream_unverified` streams the model's tokens unredacted. `client_output: "redact"` is
> a guarantee on every path *except* that opt-in; set the axis explicitly (then
> `stream_unverified` is a 422 you cannot configure) or use `incremental_verify`.

Deep dive: [`per-app-config.md`](../reference/per-app-config.md#stream_mode).

---

### The privacy profile (custom recognizers + sensitivity rules)

Set separately via `PUT /admin/applications/{id}/privacy`. Two parts:

- **Custom recognizers** — teach the gate a customer-specific identifier the built-ins don't
  know, e.g. a medical record number `MRN-1234567`. You give it a `name`, a `regex`, and a
  confidence `score`. It then becomes a detectable entity.
- **Sensitivity rules** — map *which entities were detected* to an outcome
  (`sensitivity` / `risk_class` / `residency` / `require_verification`). This is how detecting an
  SSN forces `restricted` + `onprem_only`. See §5.

Exact shapes and limits: [`admin-setup.md` §5](./admin-setup.md).

---

## 5. Sensitivity is *decided*, not *set*

A frequent point of confusion: **there is no "sensitivity" field on an app.** Sensitivity is
computed *per request* from what the gate detects, via the ordered rules in `config/policy.yaml`
(or the app's own privacy rules). The default rules:

| If the gate detects… | → sensitivity | → residency | → risk | → verify? |
|---|---|---|---|---|
| `US_SSN`, `MEDICAL_LICENSE`, `CREDIT_CARD`, bank/employee IDs | `restricted` | `onprem_only` | `high` | **yes** |
| `PERSON`, `EMAIL_ADDRESS`, `PHONE_NUMBER`, `LOCATION`, `DATE_TIME` | `confidential` | `private_cloud_ok` | `medium` | no |
| *(nothing detected)* | `public` | `any` | `low` | no |

So the **same app** can produce a `public` request (a weather question) and a `restricted`
request (a question with an SSN) back to back — and the SSN one is automatically forced on-prem
with verification, regardless of the app's own residency. This is why `pii_detection` matters:
if detection is `off`, nothing is detected, so everything classifies `public` (though the app's
own `residency` floor still applies).

Two things govern whether that escalation can happen at all, and both are declared:
`pii_detection` decides whether anything is *observed*, and the per-app
`escalate_residency_on_detection` decides whether a matched rule may **raise** placement.
The rule is raise-only in either setting — it can never lower the app's own residency
floor — and `false` still lets it set `require_verification` and the sensitivity label.
Every rule that fires is named on the response and the trace
(`sp.cross_axis_rules_fired`), and a rule that actually *moved* placement is additionally
named in `sp.placement_sources`. This is the **only** path from a protection observation
to placement; no protection setting moves placement by itself.

---

## 6. Making a request and reading the response

The response is a standard `ChatCompletion` with an added **decision trace** — the "why."
Each step that ran contributes a record: which model was chosen, which candidates were rejected
and why, cost, latency, the policy verdict, the verification result. That trace is the product's
core value: you can always see *why* a request went where it did.

The exact response shape is in [`api-reference.md`](../reference/api-reference.md).

---

## 7. Watching what happened

Two windows, both admin-gated:

- **The console** (`/api/*`): recent requests, per-app rollups, aggregate cost/latency/tier
  stats, and a **live decision stream** (the flow visualizer). This never shows chat text — it's
  the decision layer.
- **Langfuse**: the full trace per request. **What text you see here is exactly what
  `capture_io` allows** — nothing (`off`), sanitized (`redacted`), or raw (`full`, own project
  only).

Usage/billing records store metadata only — never prompt or answer text — no matter the
`capture_io` setting.

---

## 8. Four starter recipes

Concrete policies for common apps (the first three are the ones traced in the flows doc):

**Internal chatbot** — low sensitivity, cloud fine, cost-capped:
```jsonc
{ "allowed_tiers": ["cheap","mid"], "allowed_providers": ["azure"],
  "max_spend_usd_per_day": 5.0, "max_rpm": 120,
  "pii_detection": "regex", "capture_io": "off", "stream_mode": "verified_then_stream" }
```
*(`pii_detection: regex` is compatible with any `capture_io`, including `"redacted"` — the
regex detector still sanitizes the prompt, just at lower fidelity than `presidio` (it misses
entities without a pattern, e.g. names). See the `capture_io` section below.)*

**Hospital / PHI** — nothing leaves on-prem, strongest detection, verified:
```jsonc
{ "residency": "onprem_only", "allowed_providers": ["onprem"], "allowed_tiers": ["mid","frontier"],
  "max_spend_usd_per_day": 50.0,
  "pii_detection": "presidio", "capture_io": "off", "stream_mode": "verified_then_stream" }
```
*(For an `onprem_only` app in the default `enforced` posture, `capture_io: full` and
`stream_unverified` are both refused — those guarantees can't be waived. `pii_detection` is
free to downgrade even here: `off` then needs `capture_io: "off"`, while `regex` may also
use `capture_io: "redacted"` (it still sanitizes); for a hospital app you want detection
left at `presidio` anyway.)*

**Eval / prompt-tuning app** — cloud, raw capture for analysis (its own Langfuse project):
```jsonc
{ "residency": "private_cloud_ok", "allowed_tiers": ["mid","frontier"],
  "allowed_providers": ["azure","bedrock"],
  "pii_detection": "off", "capture_io": "full" }
```

**Router-only / load-bearing data** — the customer's own documents, every field must be
reproduced verbatim, and they want raw traces, all while staying on their hardware:
```jsonc
{ "residency": "onprem_only", "policy_mode": "passthrough", "capture_io": "full" }
```
*(Needs the app's own Langfuse project. The on-prem floor is fully intact — `passthrough`
turns off protection, never placement.)*

For picking between these on a real onboarding, use
[`choosing-app-settings.md`](./choosing-app-settings.md) — three questions, three
recipes, and what each one costs you.

---

## Next

Now read [`architecture/sample-request-flows.md`](../architecture/sample-request-flows.md)
— it takes these same three apps and traces real requests through all ten steps, showing exactly
where each open-source library (Presidio, LiteLLM, Headroom) does its work.

**See also:** [`admin-setup.md`](./admin-setup.md) (exact HTTP) ·
[`per-app-config.md`](../reference/per-app-config.md) (`pii_detection`/`capture_io`/`stream_mode` floors) ·
[`onboarding-models.md`](./onboarding-models.md) (populating the on-prem pool) ·
[`ui-guide.md`](./ui-guide.md) (the console).
