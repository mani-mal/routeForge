# routeForge FAQ (operator & customer-facing)

Questions that come up repeatedly while configuring per-application policy, residency,
and telemetry. For the full per-app config reference see
[`../per-app-config.md`](../per-app-config.md).

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
gate do to the data*. They are independent, so "run only on my hardware, and don't touch
my data" is sayable in one configuration. `passthrough` never widens
`allowed_providers`, never clears `onprem_only`, and never touches placement.

You still get everything that isn't protection: routing and tier selection, cheap-tier
cost savings, budgets, rate limits, durable usage metering, DecisionRecords and full
Langfuse traces.

Two requirements to know about:

- **`capture_io: "full"` needs the app's own provisioned Langfuse project.** Raw I/O is
  tenant-isolated — it never lands in the shared project. Attach one with
  `PUT /admin/applications/{app_id}/langfuse` first, or the policy write is a 422.
- **`capture_io: "redacted"` is rejected** for a `passthrough` app. That exporter path
  reuses the gate's sanitized prompt, which is raw when nothing was sanitized, and it
  writes to the *shared* project. Use `full` or `off`.

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
because that exporter path reuses the gate's sanitized prompt, which is raw when
detection is off.

> **Don't reach for `residency: "any"` + an `allowed_providers` pin.** That was the old
> workaround for getting raw capture, and it is strictly weaker: it swaps a hard
> residency floor for a mutable allowlist and drops `ctx.onprem_only`, which the step-9
> streaming floor keys on. `policy_mode` exists so nobody has to misdeclare residency to
> get a protection posture.

**Prefer a narrower lever when you can.** `pii_detection: off` blinds the gate to
*everything*, including a real SSN. If only a few false positives are the problem, use
[`pii_entity_denylist` / `pii_exempt_patterns`](../per-app-config.md) instead — they also
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
2 amended 2026-09-05; see the [per-app config reference](../per-app-config.md)).

**What has not changed, and will not:**

- An `enforced` app — one that has *not* declared a no-protection posture — still cannot
  reach `capture_io=full`, at any residency. Raw capture of a protected app was never
  sensible.
- `capture_io=redacted` is still rejected whenever nothing is being sanitized
  (`passthrough`, or `pii_detection` `off`/`regex`), because that path reuses the gate's
  sanitized prompt and writes to the **shared** project.
- Raw I/O only ever reaches the app's **own** project.
- `policy.allow_app_passthrough: false` forbids the whole posture install-wide.

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
you see it — what the user receives is the *post-verification* text, and the raw model
output is preserved separately in `raw_completion` for telemetry, never substituted into
`choices[0].message.content`.

Three ways the completion can differ from the raw model output:

1. **Egress PII redaction always runs.** If the model emits PII the detector recognizes
   (an SSN, an email the input never contained), those entity **values are scrubbed** from
   the answer — because model completions can leak PII the input gate never saw, so egress
   is redacted by default.
2. **`require_verification` fails closed.** If policy set `require_verification` and the
   completion is "unverifiable," the request is **rejected** — the caller gets an error,
   not the model's text.
3. **Escalation** can re-serve the answer from a different/stronger model (recorded in the
   trace as `escalated`), so the returned text may not come from the first model tried.

**When *is* it returned verbatim?** Redaction depends on the active detector, so:

| App config | Is `content` the raw model output? |
|---|---|
| `pii_detection=off` | **Yes** — nothing is scrubbed. |
| detection on, completion has no detected entities, no `require_verification` | **Yes, in practice** — redacted text == original. |
| detection on, completion **contains** detected PII | **No** — those values are redacted on egress. |
| `require_verification` set + completion unverifiable | **No** — request is rejected. |

So the precise statement for customers: *routeForge adds the trace and never edits the
answer for style or content — but it does enforce output controls, which can redact PII
the model emitted or block an unverifiable response.* The `VerificationRecord` in the
`trace` (`step:"verify"`, fields `performed / passed / entities_found / escalated`) tells
you exactly what step 9 did on any given request. (Note: `raw_completion` and
`sanitized_prompt` are returned for full-capture telemetry; whether they reach Langfuse
still obeys the app's `capture_io` — see the residency/PII entries above.)
