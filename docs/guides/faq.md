# routeForge FAQ (operator & customer-facing)

Questions that come up repeatedly while configuring per-application policy, residency,
and telemetry. For the full per-app config reference see
[`../reference/per-app-config.md`](../reference/per-app-config.md).

---

## Privacy, residency & telemetry

> **Onboarding a new app and not sure what to set?** Start with
> [`choosing-app-settings.md`](./choosing-app-settings.md) — the decision guide that
> picks one of three recipes for you. The questions below are the specific cases it
> generalizes.

### My customer wants RouteForge only for routing, token/cost metering and Langfuse — no PII checks, no redaction, everything raw. Is that supported?

**Yes. Set `policy_mode: "passthrough"`.** That is a first-class posture, not a
workaround, and it does not cost you the `onprem_only` guarantee.

```json
{ "residency": "onprem_only",
  "policy_mode": "passthrough",
  "capture_io": "full" }
```

`residency` answers *where may compute run*. `policy_mode` answers *how much does the
gate do to the data*. They are independent, so "run only on my hardware, and skip PII
detection/redaction and output verification" is sayable in one configuration.
`passthrough` never widens `allowed_providers`, never clears `onprem_only`, and never
touches placement. Note that `passthrough` does not touch context compression: if
Headroom is enabled for the deployment, it can still transform tool outputs/history —
disable compression too if the app needs byte-exact content end to end.

You still get everything that isn't protection: routing and tier selection, cheap-tier
cost savings, budgets, rate limits, durable usage metering, DecisionRecords and full
Langfuse traces.

Two requirements to know about:

- **`capture_io: "full"` needs the app's own provisioned Langfuse project.** Raw I/O is
  tenant-isolated — it never lands in the shared project. Attach one with
  `PUT /admin/applications/{app_id}/langfuse` first, or the policy write is a 422.
- **`capture_io: "redacted"` needs a detector.** It is rejected only when the app's
  effective `pii_detection` resolves to `off` — which is what a `passthrough` app that
  names no detector inherits. A `passthrough` app that *does* name
  `pii_detection: "regex"` or `"presidio"` **may** use `redacted`: telemetry builds its
  own redacted copy of the raw values rather than reusing whatever the model was shown.
  Use `full` (own project), `off`, or name a detector.

If the deployment has `policy.allow_app_passthrough: false` in `serving.yaml`, no app can
take this posture at all — that is the switch a locked-down install flips once.

**Turning protection on later** is `{"policy_mode": "enforced"}`. One field. The
`app_policy_version` stamp moves, so the customer's conformance probe sees the change
rather than passing on a stale result.

### The gate is mangling my app's data — it redacted an engineering label / part number / field name that isn't PII. What do I do?

If the app's whole input is customer-owned content whose every field is load-bearing
output, use `policy_mode: "passthrough"` (above) — that is the case it exists for.

If you only need unredacted prompts and want **no** telemetry I/O at all, the narrower
form is `pii_detection: "off"` with `capture_io: "off"`, keeping `residency` at
`onprem_only`:

```json
{ "residency": "onprem_only", "pii_detection": "off", "capture_io": "off" }
```

Detection and residency are independent axes. The `onprem_only` floor is computed from the
app's residency in `app/policy/context.py` and never consults `pii_detection`, so turning
detection off cannot lift the app to cloud — the model stays inside the customer's
boundary and no external party ever sees the prompt. On-prem is the *safest* place to run
the gate open, not the most dangerous.

`capture_io: "off"` is what makes that narrower form airtight: with no telemetry I/O
captured there is no egress path for the unsanitized prompt. `redacted` stays blocked
alongside `pii_detection: "off"` for a simpler reason than it used to be: redacted
telemetry needs a detector to build its redacted copy with, and there is none.

> **Careful — `pii_detection: "off"` is not "nothing is scanned".** It switches off the
> *built-in* detector. If the app has a privacy profile with custom recognizers, those
> still run, still classify, and (while `model_input` is `redact`) still redact. The only
> configuration that scans nothing at all is `pii_detection: off` **together with**
> `model_input: raw` and `client_output: raw` — i.e. `policy_mode: "passthrough"` with no
> named detector.

> **There is a narrower lever still.** If the problem is only that the *model* must see
> the real text — and the client response and traces should stay redacted — set
> `model_input: "raw"` and leave `client_output: "redact"`. That is Recipe D in
> [`choosing-app-settings.md`](./choosing-app-settings.md), and it did not exist before
> the two axes were split apart.

> **Don't reach for `residency: "any"` + an `allowed_providers` pin.** That was the old
> workaround for getting raw capture, and it is strictly weaker: it swaps a hard
> residency floor for a mutable allowlist and drops `ctx.onprem_only`, which the step-9
> streaming floor keys on. `policy_mode` exists so nobody has to misdeclare residency to
> get a protection posture.

**Prefer a narrower lever when you can.** `pii_detection: off` blinds the gate to
*everything*, including a real SSN. If only a few false positives are the problem, use
[`pii_entity_denylist` / `pii_exempt_patterns`](../reference/per-app-config.md) instead — they also
work while `onprem_only`. Reach for `off` when the app's whole input is customer-owned
content whose every field is load-bearing output, so there is nothing to protect and no
enumerable set of shapes to exempt.

### My customer is on-prem, has no PII, and wants to store *everything* (raw prompts + completions) in Langfuse. Can an `onprem_only` app do that?

**Yes — set `policy_mode: "passthrough"` and `capture_io: "full"`, and keep
`residency: "onprem_only"`.** See the first question above for the full configuration.
The app needs its own provisioned Langfuse project, because raw I/O is tenant-isolated
and never lands in the shared one.

This used to be refused. The old rule capped an `onprem_only` app at `capture_io` of
`off` or `redacted` on the reasoning that on-prem meant maximum protection. That
conflated placement with posture and forced customers to misdeclare residency to get the
behaviour they needed — so it was replaced by an explicit `policy_mode` (CLAUDE.md rule
2 amended 2026-09-05; see the [per-app config reference](../reference/per-app-config.md)).

**What has not changed, and will not:**

- An `onprem_only` app in `enforced` posture — one that has *not* declared a
  no-protection posture — still cannot reach `capture_io=full`. Raw capture of a
  protected, on-prem-confined app was never sensible. A non-`onprem_only` app in
  `enforced` posture already can hold `capture_io=full` (own project required) — that
  is exactly the side effect the next question walks through. Spelled on the axes, the
  `onprem_only` condition is now `model_input: "raw"` **and** `client_output: "raw"`,
  which is exactly what `policy_mode: "passthrough"` resolves to. See
  [the full-capture rule](./choosing-app-settings.md#the-full-capture-rule) — one rule,
  same wording in every guide.
- `capture_io=redacted` is still rejected when `pii_detection` resolves to `off`:
  redacted telemetry needs a detector to redact with. It is **no longer** rejected merely
  for being `passthrough` — telemetry builds its own redacted copy now, so a
  `passthrough` app that names `regex` or `presidio` may use `redacted`.
  `pii_detection: "regex"` is fine with `redacted`; know that regex mode covers exactly
  five patterns (SSN, card, email, phone, US bank number) plus the app's custom
  recognizers, and nothing else — no names, no addresses, no dates.
- Raw I/O only ever reaches the app's **own** project. At export time, `full` with no own
  project steps down to `redacted`, and `redacted` with no detector steps down to `off`.
- `policy.allow_app_passthrough: false` forbids the whole posture install-wide, and gates
  `model_input: "raw"` and `client_output: "raw"` independently.

**Decide it deliberately.** The risk redaction defends even on-prem is that raw PII in
Langfuse is readable by anyone with console access to that project — staying on your own
infrastructure does not remove that exposure surface. `passthrough` is right when the
data is the customer's own and every field is load-bearing; it is wrong for the hospital
persona, which is exactly why the posture is explicit, stamped on every response and
trace, and gated by a deployment ceiling.

**If you'd rather not turn protection off:** `capture_io=redacted` with detection on.
You still get full decision traces, routing, candidate list, cost, latency, and redacted
I/O — everything except the raw entity text.

### Can I just set `residency=private_cloud_ok` (or `any`) so those customers can log everything raw?

> **Don't — this is no longer necessary.** Raw capture is unlocked by
> `policy_mode: "passthrough"`, not by residency. Set the posture and leave residency
> describing where compute may actually run. The rest of this answer is kept because
> the side effect below is real for anyone who *does* move an app off `onprem_only`
> for a legitimate placement reason.

**The write succeeds — but residency is the provider filter, not a telemetry gate, so
this has a side effect you must handle deliberately.**

The valid residency values are `any`, `private_cloud_ok`, and `onprem_only`. Any value
other than `onprem_only` allows the enabled cloud providers into the routing pool. So
moving off `onprem_only` does two things at once:

1. ✅ Unlocks `capture_io=full`. (`pii_detection=off` never required this flip — it is
   available while `onprem_only`, with `capture_io=off`.)
2. ⚠️ **Removes the on-prem lock.** If any cloud provider is enabled
   (`azure`/`bedrock` in `config/serving.yaml`), the router is now free to send that
   customer's traffic to cloud — off their infrastructure. That contradicts "on-prem
   only."

**Whether this is safe depends on the deployment:**

- **No cloud provider enabled** (typical air-gapped install: `azure.enabled=false`,
  `bedrock.enabled=false`): the routing pool resolves to on-prem only anyway, so routing
  stays on-prem even with `residency: any`. Your plan works and nothing egresses.
- **A cloud provider enabled:** `residency: any` means those requests *can and will* go
  to cloud when the router scores it cheaper. Don't do this for an on-prem customer
  without the pin below.

### Legacy config: raw capture via a residency flip + an `allowed_providers` pin

> **Superseded — don't build new deployments this way.** `policy_mode: "passthrough"`
> (first question above) gets you raw capture while keeping `residency: onprem_only` and
> the hard on-prem floor. This section documents the older workaround only so anyone who
> already applied it can recognise and migrate it.

The workaround was: move the app off `onprem_only` to unlock `capture_io=full`, then pin
`allowed_providers: {"onprem"}` so routing couldn't actually reach cloud.

| Field | Value | Effect |
|---|---|---|
| `residency` | `private_cloud_ok` (or `any`) | Unlocked `full` under the old rule |
| `allowed_providers` | `{"onprem"}` | Hard-pinned routing to on-prem |
| `pii_detection` | `off` | No redaction |
| `capture_io` | `full` | Raw I/O to the app's **own** Langfuse project |

**Why it is weaker, and worth migrating off:** it swaps a hard residency floor for a
mutable allowlist. The two are not equivalent — `ctx.onprem_only` is false for such an
app, so the step-9 streaming floor and every other check that keys on it no longer treat
the app as on-prem. If anyone later clears `allowed_providers`, cloud routing silently
becomes possible, and nothing in the app's declared residency says it shouldn't be.

**To migrate:** set `residency: "onprem_only"` and `policy_mode: "passthrough"` in one
write. Keep or drop the `allowed_providers` pin as you like — it is now redundant rather
than load-bearing. The `app_policy_version` stamp moves, so a conformance probe will see
the change.

**Rule of thumb, updated:** the residency label should describe where compute may
actually run, and nothing else. Never adjust it to obtain a telemetry or detection
outcome — that is what `policy_mode` and `capture_io` are for.

---

## Response behavior & output controls

### Does routeForge return the LLM's response to the user unchanged — just wrapping it with a trace?

**Usually yes on the happy path, but *not* as an unconditional guarantee.** routeForge is
OpenAI-compatible and, in the common case, `choices[0].message.content` is the model's
answer verbatim with routeForge fields (`trace`, `cost`, `policy`, …) added *around* it.
But **step 9 (Output Controls & Verification) can alter or block the completion** before
you see it — what the user receives is the *post-verification* text. The raw model
output is kept internally as `raw_completion` (and, for tool calls, `raw_tool_calls`
holds the unredacted arguments) — popped before serialization, never a key on the
response body, never substituted into `choices[0].message.content`, and it reaches
telemetry only in the app's own Langfuse project when `capture_io: "full"`.

Three ways the completion can differ from the raw model output:

1. **Egress PII redaction runs whenever `client_output` resolves to `redact`** — which is
   the default, and what the `enforced` preset gives you. If the model emits PII the
   detector recognizes (an SSN, an email the input never contained), those entity
   **values are scrubbed** from the answer, because model completions can leak PII the
   input gate never saw. It is decided by the `client_output` axis alone and is
   **independent of what the model was shown**: an app running `model_input: "raw"` with
   `client_output: "redact"` sends the model the real values and still returns a redacted
   answer. Set `client_output: "raw"` and no egress redaction runs.
2. **`require_verification` fails closed.** If policy set `require_verification` and the
   completion is "unverifiable," the request is **rejected** — the caller gets an error,
   not the model's text.
3. **Escalation** can re-serve the answer from a different/stronger model (recorded in the
   trace as `escalated`), so the returned text may not come from the first model tried.

**When *is* it returned verbatim?** Redaction depends on the active detector, so:

| App config | Is `content` the raw model output? |
|---|---|
| `client_output: "raw"` (what `policy_mode: "passthrough"` gives you) | **Yes** — no egress redaction runs at all, whatever the detector would have found. |
| `pii_detection` resolving to `off` | **Yes** — a detector runs but finds nothing to scrub (bar the app's custom recognizers, which still run under `off`). |
| `client_output: "redact"`, completion has no detected entities, no `require_verification` | **Yes, in practice** — redacted text == original. |
| `client_output: "redact"`, completion **contains** detected PII | **No** — those values are redacted on egress. This holds even when `model_input: "raw"` showed the model the real text. |
| `require_verification` set + completion unverifiable | **No** — request is rejected. This is identical on both sides of the `client_output` axis: the `UNVERIFIABLE` check reads the completion text, not the detector, and a failed verification returns nothing. |
| `stream_mode: "stream_unverified"` | **Yes**, for that app's streamed traffic — egress redaction and step 9 are waived, *even under `enforced`*. See [`choosing-app-settings.md` §6.7](./choosing-app-settings.md). |

So the precise statement for customers: *routeForge adds the trace and never edits the
answer for style or content — but it does enforce output controls, which can redact PII
the model emitted or block an unverifiable response.* The `VerificationRecord` in the
`trace` (`step:"verify"`, fields `performed / passed / entities_found / escalated`) tells
you exactly what step 9 did on any given request. (Note: `raw_completion` and
`raw_tool_calls` are internal-only — popped before serialization, never in the public
response, `display.output`, or any console snapshot. They reach Langfuse only in the
app's own provisioned project, and only under `capture_io: "full"` — see the
residency/PII entries above. `sanitized_prompt` is different: it's the gate's
already-*redacted* prompt, not raw, so it isn't covered by that restriction — it's also
popped before the public response, but it *is* what the console's `display.input` shows,
in whichever project that app's traces normally go to, not gated to the app's own.
**`capture_io: "redacted"` telemetry no longer reuses it.** The exporter builds its own
redacted copy of the raw values with the app's detector, so a trace labelled `redacted`
is genuinely redacted even for an app whose model was shown raw text — and when no
detector ran, the exporter writes **nothing** rather than falling back to raw.)
