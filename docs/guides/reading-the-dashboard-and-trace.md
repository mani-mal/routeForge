# Reading the dashboard and the technical trace (support-ticket demo)

A walkthrough of every number on the console for one concrete scenario: a test
"support-desk" application that sends customer tickets through routeForge. Use it to
narrate the screens during a demo and to answer the "what does that value mean?"
questions that always come up.

For the panel-by-panel UI reference see [`ui-guide.md`](./ui-guide.md); this doc is the
*worked example* on top of it.

> ⚠ **These screens were captured before the B10 / B11 / B12 fixes (all resolved
> 2026-08-16).** Three specific values below reflect the *old, buggy* behaviour and no
> longer reproduce: the negative `routing saved $-0.0082` (§5, §6b — the baseline now
> prices output tokens, so routing savings are positive), the "Cloud-eligible" summary
> headline on a request where cloud was excluded (§4, §6a), and the "excluded · residency"
> label on the disabled Azure/Bedrock spurs (§5, §6a — the B10 fix removes a kill-switched
> provider from the candidate pool entirely, so it no longer appears as a rejected spur at
> all; see the 2026-09-10 status note below for the exact current labels).
> The narrative of *what each panel means* is still accurate; the exact captured numbers
> in §1/§4/§5 need a fresh demo capture to be re-trued. See §6 for the corrected reading.
>
> **Status check 2026-09-10.** §6a previously described the *current* (fixed) state as
> the kill-switched provider being relabelled `"provider disabled"` — that string does not
> exist anywhere in the code (verified against `ui/src/console/RequestDrilldown.tsx` and
> `app/routing/router.py`) and §6a has been corrected below. The actual current behaviour:
> a cloud provider disabled at the infra kill-switch (`providers.{azure,bedrock}.enabled:
> false`) is dropped from the candidate pool entirely before the router scores anything
> (`ModelRegistry.effective_pool`, `app/apps/registry.py:67-68`) — it never appears as a
> rejected spur at all on a typical lab. The "What happened" band's **Serving providers**
> chips (§4) use a *different*, always-on 3-slot mechanism and show **"provider off"**
> (not "excluded by policy") for a non-`onprem_only` request's excluded providers; the
> Technical-trace rejected-candidate reason for a cloud model excluded by app/tenant scope
> (when its provider *is* enabled) is **"excluded · app policy"**, not "excluded by policy
> (residency)". Confirmed by sending a live request through `TestClient(create_app())`
> with the default (cloud-disabled) config and inspecting the `router` trace record: only
> on-prem candidates appear, rejected or chosen. Current authority:
> [`ui-guide.md`](./ui-guide.md) §7b, §12.

---

## 0. The scenario that produced these screens

The test app holds two tickets and two customer records:

| Ticket | Customer | Service / region | Notes |
|---|---|---|---|
| `TCK-1001` | Avery Morgan (`CUS-204`) | fiber-internet / Austin | router-replacement auth loop |
| `TCK-1002` | Jordan Lee (`CUS-319`) | hosted-voice / Chicago | intermittent voice drops |

For **each ticket** the app runs a three-stage LLM pipeline and calls routeForge **once
per stage**:

```
ticket                                  → planner   → plan
ticket + plan + tool results            → synthesis → draft
ticket + service status + draft         → review    → final answer
```

So **2 tickets × 3 stages = 6 calls into routeForge**. This is the single most important
fact for reading the dashboard: **routeForge counts 6 requests, not 2.** The
planner→synthesis→review orchestration lives *inside the test app*; routeForge sees six
independent, self-contained chat calls. (That is also why "% multi-step" reads 0% — see
§3.)

The ticket/customer payloads carry a name, email, phone, plan, `router_mac`, region, and
timestamps — which is why the gate lights up with PII entity types even though none of it
is an SSN.

---

## 1. Overview tab — the headline numbers

| Field | Value here | How it's derived |
|---|---|---|
| **saved vs. all-frontier baseline** | `27.4%` | `total_saved ÷ (total_spend + total_saved)`. It's the aggregate saving vs. an imaginary run where every one of the 6 calls went to the frontier tier. |
| **requests** | `6` | Count of chat calls that reached the router (the 6 stage-calls, §0). |
| **TOTAL SPEND** | `$0.0677` | Σ actual cost of all 6 calls (input+output tokens × the chosen model's price). |
| **TOTAL SAVED** | `$0.0255` | Σ (all-frontier baseline − actual) across the 6 calls. |
| **REQUESTS WITH PII** | `6` | Every call carried customer data, so the gate flagged PII on all 6. |
| **KEPT OFF CLOUD** | `0` | Count of calls the gate forced on-prem (`onprem_only = true`). None here — PII was detected but the sensitivity was `confidential`, not `restricted`, so nothing was *forced* on-prem by residency. |

> **PII detected ≠ kept off cloud.** "6 requests with PII" and "0 kept off cloud" are
> consistent: detecting PII sets sensitivity and redacts it; only `onprem_only` (the
> `restricted` case) removes cloud from the candidate pool. Good line to pre-empt in a
> demo.

> **Cost tab, not walked through here:** its "Cost over time" chart now also overlays a
> dashed **quality line** on a second axis (a tier-quality proxy, not measured accuracy),
> and the tiles include **Quality/$** density (Cost Intelligence Density) alongside the
> spend/saved figures — see `ui-guide.md` §4 and `ui/src/console/sections/CostSection.tsx`.

---

## 2. Privacy tab

| Field | Value | Meaning |
|---|---|---|
| **REQUESTS WITH PII** | `6` | Same count as Overview — every call had detectable personal data. |
| **FORCED ON-PREM** | `0` | No call was pinned on-prem by residency (`onprem_only`). |
| **KEPT OFF CLOUD** | `0` | Same as forced-on-prem — how many candidates cloud was removed for. |
| **DETECTED ENTITY TYPES** | `DATE_TIME, LOCATION, MAC_ADDRESS, PERSON, PHONE_NUMBER, URL, US_DRIVER_LICENSE` | The union of Presidio entity **types** seen across all 6 calls. Only the *types* are stored — never the values. `PERSON`=Avery/Jordan, `PHONE_NUMBER`=the `+1-202…` numbers, `MAC_ADDRESS`=`router_mac`, `LOCATION`=Austin/Chicago, `DATE_TIME`=the timestamps. `URL`/`US_DRIVER_LICENSE` are Presidio firing on `example.test` emails and MAC/ID-shaped strings (expected false-ish positives worth naming out loud). |

---

## 3. Traffic tab

| Field | Value | How it's derived |
|---|---|---|
| **requests** | `6` | Same 6 calls. |
| **TIER MIX** | `mid 6` | All six calls routed to the **mid** tier (the onboarded `nemotron-nano-8b-v1`). The donut is a single colour because there's one tier in play. |
| **P50 LATENCY** | `3327 ms` | Median end-to-end latency across the 6 calls. |
| **P95 LATENCY** | `5271 ms` | 95th-percentile latency. |
| **% MULTI-STEP** | `0.0%` | Fraction of requests whose trace holds more than one routing decision. routeForge runs no agent loop of its own (the internal agent runtime was removed 2026-09-17), so every request is one routed call and this tile only moves when the **gateway escalates** — a first-choice candidate failed or came back low-confidence and a second model was routed for the same request. Here it is 0%: six single-shot `direct answer` calls, none escalated, with the planner→synthesis→review orchestration living in the **test app** (§0). |

---

## 4. Request drill-down — the "What happened" band

This is the plain-language summary at the top of one request (here, one of the six
stage-calls). Each card restates a decision the trace makes in full below it.

| Card | Value shown | Where it comes from |
|---|---|---|
| **PRIVACY** | `Cloud-eligible` + `PERSON` chip | Reflects the gate's `onprem_only` flag (`false` ⇒ "cloud-eligible"), with the detected entity chips. **See the caveat in §6** — for this app the *app policy* still excluded cloud, so this headline is narrower than it looks. |
| **MODEL** | `nemotron-nano-8b-v1` · `mid tier` | The model the router chose (§5, Model Router). |
| **VERIFIED** | `Clean` · checked on egress | Step 9 ran a leakage check on the completion and it passed. |
| **COST** | `$0.0105` vs `$0.0023 all-frontier` | Actual spend for this call vs. what the frontier tier would have cost for the same tokens. |
| **LATENCY** | `5006 ms` | End-to-end for this call. |
| **SERVING PROVIDERS** | `On-Prem eligible` / `Azure excluded by policy` / `Bedrock excluded by policy` | The candidate pool after policy filtering. On-prem was the only eligible path (see §6). |

---

## 5. Technical trace — field by field

The trace is the ten-step decision record for the request, top to bottom. This is "the
why" — the part that makes the demo credible.

### Request — *incoming OpenAI-compatible call*
- **request id** `req-2c0a5fca` — correlates the UI, the logs, and the Langfuse trace.
- **served model** `nemotron-nano-8b-v1` — the model that ultimately answered.
- Note: *"The raw prompt is never stored or shown."* This is deliberate — see §7.

### Policy Gate (step 2) — *detect & redact PII/PHI, set residency*
- **sensitivity** `confidential` — the classification the gate assigned from the detected
  entities and tenant rules.
- **onprem_only** `false` — residency did **not** pin this request on-prem. (It would be
  `true` for `restricted`/PHI traffic like the hospital persona.)
- **detected (types)** `PERSON` — entity *types* found in this specific call (the planner
  stage saw the customer name). Values are redacted before anything downstream sees them.

### Policy Context (step 3) — *immutable policy that travels with the request*
- **allowed providers** `onprem` — the only provider the router may pick from for this
  request. **This is the real constraint** (§6), narrower than `onprem_only=false` alone
  implies.
- **onprem_only** `false` — carried forward from the gate.
- **sensitivity** `confidential` — carried forward.

### Task Mode (step 4) — *always direct*
- **mode** `direct answer` — a single model call answers. Step 4 always decides
  `direct`: routeForge runs no agent loop of its own, so the multi-stage work is the
  app's (confirms §0).

### Compression (step 5, Headroom) — *shrink tool output & history*
- **status** `bypassed — below_min_tokens` — the payload was too small to be worth
  compressing, so Headroom was skipped. Hence **compression saved $0.0000** later.
- **CCR stored / retrievals / cloud** `0 / 0 / 0` — nothing was cached, retrieved, or
  sent to cloud by the compressor.

### Model Router (step 6, Switchyard) — *pick model + tier per call, by cost & risk*
- **chosen** `nemotron-nano-8b-v1`, **tier mid (target mid)** — the router targeted the
  mid tier and picked the cheapest eligible model in it.
- **rejected spurs** — the alternatives and *why* each lost:
  - `GPT-4o mini (Azure)` — **excluded · residency** (not in `allowed_providers`).
  - `Claude Sonnet (Bedrock)` — **excluded · residency** (same).
  - `Qwen2.5-7B (local vLLM)` — **below target tier** (cheap tier; router was aiming mid).
- **cost $0.0105 · est $0.0019 · 5006 ms** — actual vs. pre-call estimate vs. latency.

### Gateway (step 7) — *quotas, breakers, fallback on failure*
- *"No fallback — the first chosen backend succeeded."* The primary served it; no breaker
  tripped.

### Backend (step 8) — *runs on the chosen serving path*
- **served model** `nemotron-nano-8b-v1`.
- **latency** `5006 ms`.
- **tokens** `152 in / 972 out` — the actual token counts the cost is computed from.

### Verify (step 9) — *leakage check on the completion*
- **required** `false` — this request didn't mandate verification (`require_verification`
  is set for high-risk/PHI traffic, not this `confidential` call).
- **performed** `false` — so the full leakage scan wasn't run…
- **passed** `true` — …but the lightweight egress check still passed.
- **escalated** `false` — no re-route to a stronger model was triggered.
- **found (types)** `DATE_TIME, LOCATION, PERSON` — entity types present in the
  *response*, checked at egress (redaction happens on the way out, not only on intake).
- **note** `not required` — restates why the heavy check was skipped.

### Response (step 10) — *governed answer + full decision trace*
- **tokens** `1124 total` — `152 in + 972 out`.
- **actual spend** `$0.0105`.
- **all-frontier baseline** `$0.0023` — what the same tokens would cost at frontier price.
- **routing saved** `$-0.0082` — actual **minus** baseline. It's **negative** here: the
  mid model was *more* expensive per token than the frontier baseline for this token mix,
  so routing *cost* money on this call rather than saving it. See §6 — this is a pricing
  artifact you want to understand before the demo.
- **compression saved** `$0.0000` — Headroom was bypassed (§5), so it saved nothing.

---

## 6. Two things the old capture got wrong — now fixed

Both were real defects in the capture above; both are **resolved** (2026-08-16, verified
in code). They're kept here because the old screens are still in circulation and a sharp
customer may have seen them — this is what you say now.

**(a) "Cloud-eligible" but cloud was excluded — fixed (B11 + B10).** The old capture's
*What happened* PRIVACY card said "Cloud-eligible — every provider was eligible," yet
SERVING PROVIDERS greyed out Azure and Bedrock and the router rejected them for
*residency*. What actually happened: the **gate** didn't force on-prem
(`onprem_only=false`), but Azure and Bedrock are **disabled at the infra kill-switch**
(`config/serving.yaml`, `providers.{azure,bedrock}.enabled: false`), so `allowed_providers`
collapses to `onprem` and the only eligible path is on-prem. The summary card used to read
the gate flag instead of the effective candidate pool (overstating cloud-eligibility), and
the router mislabelled the kill-switched providers as "residency" exclusions.
**Now:** the eligibility line is derived from `allowed_providers` (so a request with cloud
disabled reads as on-prem, not "cloud-eligible"), and the router's fix goes further than a
relabel — a provider dropped by the kill-switch is removed from the candidate pool
*entirely*, before the router scores anything (`ModelRegistry.effective_pool`,
`app/apps/registry.py:67-68`), so it never surfaces in the rejected list at all, under any
label. (The top-band **Serving providers** chip strip is a separate, always-on 3-slot
display — see §4 — and still shows a kill-switched provider as **"provider off"**, not
"excluded by policy" or "provider disabled".) Narrate it simply as "cloud providers are
turned off on this lab, so everything stays on-prem." (Was **B11** summary card / **B10**
router-pool fix in [`../backlog/bugs.md`](../backlog/bugs.md).)

**(b) "routing saved" went negative per-request — fixed (B12).** The old capture showed
`routing saved $-0.0082` on this request while the Overview showed a positive aggregate.
Root cause: the "all-frontier baseline" was priced on `tokens_before` — roughly the prompt
only — so it **omitted the 972 output tokens the model generated**, while `actual spend`
included them. That understated the baseline and drove `routing_saved` negative, even
though a real frontier model would generate those same output tokens and cost *more*.
**Now:** the baseline prices the actual **input + output** token basis, so `routing_saved`
is **positive** for a cheaper-tier request and zero for a same-tier one — the flagship "we
save money by routing" number reads correctly on the screen meant to prove it. (Was **B12**
in [`../backlog/bugs.md`](../backlog/bugs.md).)

---

## 7. Why the trace shows the *response* but not the *request*

By design, and it's the core of the privacy story:

- **Rule #1 — nothing external before the gate.** The raw prompt can contain unredacted
  PII/PHI. routeForge never persists or displays it; the Request section shows only
  metadata (request id, served model) and the note *"the raw prompt is never stored or
  shown."*
- **The response is safe to show** because it has already passed step 9's egress leakage
  check and been redacted on the way out. It's the *governed* response, not raw model
  output.

So the asymmetry is intentional: **raw input is never surfaced; the verified, redacted
output is.** That is exactly the guarantee the demo is meant to prove — don't "fix" it by
echoing the prompt.

---

## 8. "Langfuse has this data — can we remove it?"

Short answer: **the single-request traces don't contain raw prompts, and the one path
that used to leak them has already been closed.** Details, because it's a fair worry:

- **`serving.request` traces (these direct calls) store no raw I/O.** They attach only
  redacted attributes — request id, tenant, model, costs, sensitivity, and entity
  **types**. The "Input" column in Langfuse is blank *by design* (Rule #2, redact on
  egress). Nothing to remove there.
- **The old leak is gone with the agent.** The internal agent runtime used the NeMo
  Agent Toolkit's native Langfuse exporter, which emitted raw workflow input and per-hop
  tool I/O bypassing our redaction. That exporter was unwired, and on 2026-09-17 the
  agent runtime itself was removed. Every trace now comes solely from our own redacted
  path (`app/obs/export.py`).
- **If you want to remove even the redacted metadata**, turn the Langfuse exporter off in
  `config/serving.yaml` (`observability.langfuse.enabled: false`) — but that also drops
  the trace view the demo relies on.
- **Full raw I/O capture is a deliberate, gated opt-in** (`capture_io: off | redacted |
  full`, default effectively off/redacted) intended only for a residency-local eval
  store — never on by default. See [`../archive/eval-logging-backlog.md`](../archive/eval-logging-backlog.md)
  for the full design and guardrails.

So: for this demo, Langfuse is **already** free of raw prompts; the redacted trace is
what you're seeing, and it's the intended state.
