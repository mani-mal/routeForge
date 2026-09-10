# routeForge Console — UI Guide

The admin console for a **multi-tenant model router**. It answers, per application:
*what did this app cost, what did we save, did anything sensitive leak, and what's
actually running?* **The console is the product** — the model's answer text is almost
incidental; what sells is showing the **decision**: which model, why, what it cost, and
what was excluded from the candidate pool.

- **Stack:** React 18 + Vite + Tailwind + Recharts. Source in `ui/src/console/`.
- **Talks only to our API** — no third-party calls, no client-side secrets. The `/api/*`
  read surface is **admin-gated** (session cookie); `/admin/*` is CRUD; `/v1/*` is the
  OpenAI-compatible serving path.
- **Live** — request rows stream in over SSE and every tab re-aggregates as they arrive.

This guide names every value on every screen. Region names match the code
(`ui/src/console/`, `ui/src/console/sections/`). All money is shown to **4 decimal
places** (`$0.0000`); a missing value renders as `–`.

---

## 0. Navigation model

Three levels, top to bottom:

```
Sign-in ──▶ Console Shell ──▶ Fleet Overview ──▶ Application Page (tabbed)
                 │  (left rail)                       ├─ Overview
                 ├─ Overview (Fleet)                  ├─ Cost
                 ├─ Applications                       ├─ Privacy
                 │   (create + config + tokens)        ├─ Traffic
                 ├─ Infrastructure                     ├─ Requests (list + drill-down)
                 │   (onboard on-prem models)          └─ Settings
                 └─ Cross-app usage
                     (DB-backed per-app rollup)
```

- **Console Shell** (`ConsoleShell.tsx`) — the only top-level surface after login. A
  left rail (`STATIONS` in `ConsoleShell.tsx:22-27`) with four sections — **Overview**
  (the Fleet landing screen), **Applications** (create app, per-app config, tokens —
  the admin `ApplicationsPanel`), **Infrastructure** (`InfrastructurePanel`, onboard/
  manage on-prem models via `/admin/infra/models*`), and **Cross-app usage**
  (`CrossAppReport`, the DB-backed per-app rollup). Selecting a fleet row opens the
  per-app **Application Page**.
- **Fleet Overview** (`FleetOverview.tsx`) — the landing screen: an all-apps summary
  strip + a sortable per-application table. Click a row to open that app.
- **Application Page** (`AppPage.tsx`) — the per-app "answer desk": six tabs, scoped to
  **one** application's traffic.

The top-right chrome (`● live/disconnected`, light/dark toggle, **Sign out**, **POWERED
BY CDW**) is the outer app frame (`App.tsx`), above the console shell.

### 0a. The live/disconnected indicator

The dot next to the logo reports whether the browser has an open **SSE (Server-Sent
Events)** channel to `/api/stream` — the push channel that streams new requests into the
console in real time (`App.tsx:59` / `api.ts` `subscribeStream`).

| State | Dot | Meaning |
|---|---|---|
| **live** | 🟢 green | Stream open (`EventSource.onopen`) — new requests appear instantly. |
| **disconnected** | ⚪ grey | Stream dropped or never opened (`EventSource.onerror`). |

**"disconnected" does not mean the app is broken.** Every screen still loads via its
normal `fetch` calls and a **3-second poll**, so data still appears — it just isn't
real-time, and new requests won't stream in live. EventSource **auto-reconnects in the
background**, so the dot often flips back to "live" on its own.

If it stays grey while everything else loads, the usual causes are: (1) the admin session
isn't valid — `/api/stream` is admin-gated, so an expired/absent session cookie makes the
stream 401 (sign in again); (2) the reverse proxy is buffering SSE on the DGX deploy —
`/api/stream` needs `proxy_buffering off` in `ui/nginx.conf`; or (3) a transient drop that
self-heals on reconnect.

---

## 1. Fleet Overview — the landing screen

`FleetOverview.tsx`. Data: `GET /api/fleet` (per-app rows, polled every **3 s**) joined
to `GET /admin/applications` (names + residency). One row per application.

### 1a. Summary strip (all apps combined)

Four tiles across the top, summed over every app in the fleet:

| Tile | What it is | Source |
|---|---|---|
| **Requests** | Total requests handled across all apps. | Σ `count` |
| **Spend** | Total actual USD spent across all apps. | Σ `total_usd` |
| **Saved** | Dollars saved vs. the all-frontier baseline, with `NN.N% vs. all-frontier` beneath. | Σ`baseline_usd` − Σ`total_usd` |
| **Kept on-prem** | Count of requests policy pinned to on-prem serving. | Σ `onprem_count` |

### 1b. Applications table

Sortable (click any column header; ▲/▼ shows the active sort). Columns:

| Column | Meaning |
|---|---|
| **App** | App display name, with the app **id** (tenant id) beneath in grey. |
| **Requests** | Requests handled for this app. |
| **Spend** | Actual USD spent by this app. |
| **Saved** | Dollars saved for this app, with **(NN%)** vs. its own baseline. |
| **PII** | Requests where the gate detected at least one PII/PHI entity. |
| **On-prem** | Requests this app kept on-prem (`onprem_only`). |
| **p50** | Median request latency (ms) for this app. |
| **Tiers** | A mini stacked bar of the app's tier mix (cheap green / mid blue / frontier red). Hover for exact counts. |
| **⚠** | Fires only on a **genuine residency breach** — an `onprem_only` request that was served off-prem (`fleet()['onprem_violations'] > 0`, `FleetOverview.tsx:51`). Because `onprem_only` requests can't reach cloud by construction (rule 3) this is normally `0`; if the glyph ever shows, it means a real leak. *(It used to fire on the much looser `pii_count > 0 && onprem_count < pii_count`, which flagged compliant `private_cloud_ok` traffic as a problem — see `docs/ui-open-items.md` #6.)* |

A tier legend (cheap / mid / frontier dots) sits top-right of the table.

---

## 2. Application Page — header + tabs

`AppPage.tsx`. Header shows **← Fleet** (back), the app **name**, and
`app-id · <default_residency>` beneath. Six tabs follow.

The page owns **one request feed** for this app: seeded from `GET /api/requests?limit=500&app=<id>`
and kept live via the shared SSE stream **filtered to this tenant** (`summary.tenant_id === appId`).
Every tab is a pure aggregation of that feed (`console/aggregate.ts`), so all six move
together as new requests arrive. Newest 500 requests are held client-side.

---

## 3. Overview tab

`sections/OverviewSection.tsx`. The whole story at a glance.

| Value | Meaning |
|---|---|
| **NN.N%** (big green) | **Percent saved vs. the all-frontier baseline** for this app — `(baseline − total) / baseline`. The headline number. |
| **requests** (top-right) | Request volume for this app. |
| **Total spend** | Actual USD this app spent. |
| **Total saved** | `baseline − total` in dollars. |
| **Requests with PII** | Count of requests with ≥1 detected entity type. |
| **Kept off cloud** | Requests that were **both** sensitive (had PII) **and** forced on-prem — the privacy proof (sensitive traffic that demonstrably never left). |

> **Kept off cloud ≠ Forced on-prem.** "Kept off cloud" counts only requests that had
> PII *and* were pinned on-prem. A request can be `onprem_only` without any PII (e.g. an
> app whose default residency is on-prem), so **Forced on-prem** (Privacy tab) is ≥
> **Kept off cloud**.

---

## 4. Cost tab

`sections/CostSection.tsx`. The owner question: *what did this app cost, and how much
did we save?*

### 4a. Headline

- **$X.XXXX** (large) — **actual spend**, with `actual spend vs. $Y.YYYY all-frontier
  baseline` beneath. On-prem tiers are priced at a small **representative amortized cost**
  (`config/pricing.yaml`), so spend is a believable non-zero figure — an honest "cost
  reduction" story, not "free".
- **NN.N%** (green, right) — percent saved vs. baseline.

### 4b. Savings by lever

A horizontal bar split into **two independent levers, never merged**:

- **routing** (blue `#4C8DFF`) — dollars saved by **sending the request to a cheaper
  tier** instead of frontier.
- **compression** (amber `#F5A524`) — dollars saved because **Headroom removed tokens**
  before pricing. Reads **"off / not run"** whenever `compression_saved == 0` (Headroom
  disabled or it never fired).

They satisfy the identity `routing_saved + compression_saved == baseline − actual`.
Keeping them separate is deliberate: the customer must be able to tell which lever is
working.

### 4c. Cost over time vs. quality

Cumulative line chart as requests accrue (ordered by timestamp): **actual** (green) vs.
**baseline** (grey) on the cost (left) axis — the widening gap is the running saving. A
third **dashed quality line** rides a **second, right-hand Y-axis** (`domain [0,1]`): the
**rolling-average quality** (a tier-quality **proxy**, not measured accuracy) up to each
point, skipping requests that carry no `quality` (`connectNulls`). A caption under the
chart states the caveat (`QUALITY_CAVEAT`) plus the quality-signal provenance.

### 4d. Tiles (bottom)

Repeat the cost figures as stat tiles — **Total spend**, **All-frontier baseline**,
**Routing saved**, **Compression saved** (again "off / not run" when zero) — followed by
two quality-density tiles: **Token Intelligence Density** (quality delivered per 1k
tokens) and **Cost Intelligence Density** (quality delivered per $, i.e. Quality/$). Both
densities are a tier-quality proxy and need a per-model quality score.

### 4e. How baseline & "% saved" are computed

The **all-frontier baseline** is **not a flat per-request number** — it is computed from
**each request's own token count**, re-priced at the frontier model's rate, then summed
across every request in the app.

Prices are USD per 1M tokens; the frontier baseline model is `bedrock-frontier`. Cloud
prices come from `config/pricing.yaml`; **on-prem prices do not** — an onboarded model
carries its own `price_input` / `price_output`, set at onboarding time and stored on its
`onboarded_models` row (see [`../onboarding-models.md`](../onboarding-models.md)):

| Model (slug) | Tier | Input /1M | Output /1M | Combined /1M | Source |
|---|---|---|---|---|---|
| *your onboarded on-prem model* | as onboarded | as onboarded | as onboarded | — | `onboarded_models` row |
| `azure-gpt-mid` | mid | $0.15 | $0.60 | $0.75 | `pricing.yaml` |
| `bedrock-frontier` | **frontier (baseline)** | $3.00 | $15.00 | **$18.00** | `pricing.yaml` |

The worked example below uses an illustrative on-prem cheap model at **$0.05 in /
$0.05 out** (`$0.10` combined) — substitute your own onboarding prices.

The cost code (`app/pipeline.py:48-50`) prices a request's tokens at the **combined**
input+output rate:

```
baseline_for_one_request = total_tokens / 1,000,000 × ($3.00 + $15.00)   # = ×$18.00/1M
```

**Worked example — one request using 10,000 tokens, served on the cheap on-prem tier:**

```
baseline (if it had gone to frontier):  10,000 / 1e6 × $18.00 = $0.1800
actual   (cheap on-prem @ $0.10/1M):    10,000 / 1e6 × $0.10  = $0.0010
routing saved on this request:          $0.1800 − $0.0010     = $0.1790
```

Sum across the app's requests to get the tab's figures, e.g. for 148 requests:

```
Σ baseline = $15.0340     (every request re-priced at frontier $18/1M)
Σ actual   = $0.1700      (each at whatever tier it really used)
Σ saved    = $14.8640  →  98.9%   ← the headline
```

(Sanity check: `$15.0340 ÷ $18/1M ≈ 835k tokens` across 148 requests, ~5.6k each.) The
baseline is **live** — it moves with real traffic, nothing is hardcoded. On-prem tiers
are priced at a small **representative amortized** rate (not $0), so actual spend and
routing savings read believably rather than as a flat 100%.

---

## 5. Privacy tab

`sections/PrivacySection.tsx`. The owner question: *did anything sensitive leak?*
Subtitle: "Sensitive requests kept on your infrastructure."

| Value | Meaning |
|---|---|
| **Requests with PII** | Requests where the gate detected ≥1 entity type. |
| **Forced on-prem** | Requests marked `onprem_only` (cloud removed from the pool before routing). |
| **Kept off cloud** | The intersection: PII **and** on-prem — the requests that were sensitive *and* provably never left. |
| **Detected entity types** | Red chips of the **entity TYPES** seen across this app (`DATE_TIME`, `LOCATION`, `US_SSN`, `PERSON`, …). **Types only — never raw values.** This is the core honesty rule. "None detected." when empty. |

---

## 6. Traffic tab

`sections/TrafficSection.tsx`. The owner question: *what's actually running, and how
fast?*

| Value | Meaning |
|---|---|
| **N** (big) + **requests** | Request volume for this app. |
| **Tier mix** (donut) | Requests per tier, coloured cheap mint / mid blue / frontier amber (§9a). A legend beneath lists `tier count`. Hover a slice for the count. |
| **p50 latency** | Median request latency (ms). |
| **p95 latency** | 95th-percentile request latency (ms). |
| **% multi-step** | Share of requests that took the **agent path** (`hops > 1`) — i.e. made more than one routed LLM call. |

> **Multi-step latency now sums per-hop.** Agent-path requests report the total of every
> hop's real latency (fixed 2026-08-06; earlier builds reported 0 ms because per-hop
> latency wasn't summed into the request total). Both single-shot and multi-step latency
> are correct.

---

## 7. Requests tab — the drill-down

`AppPage.tsx` renders a two-column split: **RequestList** (left) + **RequestDrilldown**
(right). Click a row on the left to load its full trace on the right
(`GET /api/requests/{id}`).

### 7a. Request list (left)

`components/RequestList.tsx`. Newest first; selected row highlighted green. Each row:

- **Tier dot** — cheap green / mid blue / frontier red.
- **Model** — human-facing name (e.g. `Qwen2.5-7B-Instruct (local)`); hover shows the
  routing slug. From `display_name` — on the cloud pool that comes from
  `config/routing.yaml`, on an on-prem model from its onboarded row.
- **`N hops`** badge — multi-step requests (agent path).
- **Cost** (right, USD 4dp).
- **Sensitivity** (`public` / `confidential` / `restricted`) and **latency** beneath.

### 7b. Drill-down (right) — two altitudes

`RequestDrilldown.tsx`. Two bands: a plain-language summary, then the full technical
trace.

#### Band 1 — "What happened" (owner-facing)

`PlainSummary` in `RequestDrilldown.tsx`. No jargon, no step numbers — **labelled outcome
cards**, each answering one question an owner actually asks (the run-on arrow-chain
sentence this replaced was retired in `:0.5.0`, `docs/ui-open-items.md` #1):

| Card | Verdict | The *why* line beneath it |
|---|---|---|
| **Privacy** (shield) | **Kept on-prem** / **On-prem only** / **Cloud-eligible** | Derived from the *effective candidate pool* (`policy.allowed_providers`), **not** `onprem_only` alone — so a request whose cloud providers are simply disabled reads "On-prem only", not "Cloud-eligible". Detected entity **types** render as chips below. |
| **Model** (chip) | tier dot + model display name + `<tier> tier` | States why this model won — "cheapest model in the *N* tier, the lowest tier that met this task's risk bar", or the hop count for a multi-step request. |

Under the two cards, a row of three metric chips — **Verified** (`Clean` / `Flagged` /
`Not checked`), **Cost** (precise USD, vs the all-frontier baseline) and **Latency**
(end-to-end, or `N hops`) — then a thumbs up/down feedback control and a collapsible
reviewer **correction form** (`corrected_output`, optional `preferred_model`), which post
scores to the app's own Langfuse project.

Beneath all of that, the **Serving providers** strip — three chips **On-Prem / Azure /
Bedrock**:

- **eligible** (green) vs **excluded — residency** / **excluded by policy** (grey).
- For an `onprem_only` request the two cloud chips grey out with *"excluded — residency"*.
  **This grey-out is the whole privacy story** — cloud was removed from the candidate
  pool *before* the router scored anything, not filtered after. Eligibility comes from
  the request's `policy.allowed_providers`.

#### Band 2 — "Technical trace" (admin-facing)

The full ten-step Serving Plane for this one request, each step as a card with its
one-line caption. Top-right is the **live Langfuse deep-link** ("Langfuse trace ↗",
opens this request's trace; "Langfuse off" when telemetry keys aren't set — see FAQ).
A multi-step request shows a note: *"Multi-step request — N agent hops, each re-entering
compression and the router."*

The ten step cards and the values each shows:

| # · Step | Caption | Values shown |
|---|---|---|
| **1 Request** | Incoming OpenAI-compatible call | request id; served model. **Not the prompt** — "raw prompt is never stored or shown". |
| **2 Policy Gate** | Detect & redact PII/PHI, set residency | sensitivity; `onprem_only`; **detected (types)** as red chips (never values). |
| **3 Policy Context** | Immutable policy travels with the request | **allowed providers** (the pool the router is locked to); `onprem_only`; sensitivity. |
| **4 Task Mode** | Direct answer vs. multi-step agent | mode: `direct answer` or `multi-step agent (N hops)`. |
| **5 Compression** | Headroom shrinks tool output & history | per hop: `tokens_before → tokens_after` + **reduction %** *via &lt;compressor&gt;*, or **"bypassed — reason"**; plus **CCR stored / CCR retrievals / → cloud** (counts only, never cache contents). "off / not applicable" when it didn't run. |
| **6 Model Router** | Picks model + tier per call, by cost & risk | **one card per hop.** chosen model (green) + reason; **rejected** candidates each with tier dot, name, `(provider)`, and **reason** (e.g. "excluded by policy (residency)", "below target tier"); est/actual cost, tokens priced, and **cumulative** spend across hops. |
| **7 Gateway** | Quotas, breakers, fallback on failure | "No fallback — the first chosen backend succeeded", or the fallback reason if a backend failed. |
| **8 Backend** | Runs on the chosen serving path | served model; latency (first hop); **tokens** `in / out`. The real serving path (mock / vLLM / Dynamo). |
| **9 Verify** | Leakage check on the completion | required; performed; passed; escalated; **found (types)**; note. Egress leakage check — redaction on the way out, not just intake. |
| **10 Response** | Governed answer + full decision trace | **tokens total**; **actual spend**; **all-frontier baseline**; **routing saved**; **compression saved**; then the model's **answer text** (scrollable). |

> **The rejected list (step 6) with reasons is the single most important thing in the
> trace** — it's what proves the routing logic and the residency guarantee on a projector.

---

## 8. Settings tab

`admin/AppSettings.tsx` (`AppConfig`, re-exported through `admin/ApplicationsPanel.tsx`
so both the admin Applications panel and the per-app page mount the same component).
Per-app admin config as a permanent **summary strip** and **six numbered, collapsible
steps** (8a) — the last of which is the **privacy rules** editor (8b). This is CRUD over
`/admin/*`, not a read view.

### 8a. The six steps

> **What should a new app be set to?** See
> [`choosing-app-settings.md`](./choosing-app-settings.md) — three questions, three
> recipes, and what each one costs you.

Since `:0.18.0` the tab is six numbered, collapsible steps with a **Start from a
recipe** picker; since `:0.17.0` this form covers **every** field the admin policy API
accepts. Before
that, six of them — `policy_mode`, `pii_detection`, `stream_mode`,
`pii_entity_denylist`, `pii_score_threshold`, `pii_exempt_patterns` — existed only over
`curl`, which is why some deployed apps carry values the console could not previously
show you.

It used to render all of that as one flat scroll with every field expanded at the same
visual weight. It is now six numbered steps, each **collapsed to a one-line summary of
its own current values**, so the page is scannable before anything is opened. The
numbering is the recommended order to work through for a new app; the grouping is the
point rather than decoration: **placement and protection are separate axes**, and a flat
stack of rows was exactly what let operators read one as implying the other.

| # | Step | Controls | Saved by |
|---|---|---|---|
| **1** | **Access** | API tokens — list, issue, revoke | `POST /admin/applications/{id}/tokens`, `POST /admin/tokens/{token_id}/revoke` (immediate, no Save button) |
| **2** | **Placement — where compute may run** | `residency`, allowed `providers`, allowed `tiers`, allowed `models` | **Save policy** |
| **3** | **Protection — what the gate does to the data** | `policy_mode`, plus a collapsible **Detection & narrowing** sub-group: `pii detection`, `min confidence`, `exempt types`, `exempt values` | **Save policy** |
| **4** | **Telemetry** | the app's own **Langfuse credentials**, then `capture_io` (`off` / `redacted` / `full`) | **Save credentials** (`PUT …/langfuse`) for the credentials; **Save policy** for `capture_io` |
| **5** | **Delivery and limits** | `stream_mode`, Headroom (compression) override, `max rpm`, budget `$/day` | **Save policy** |
| **6** | **Privacy rules** (badged *advanced*) | custom recognizers + sensitivity rules — see 8b | **Save privacy rules** (`PUT …/privacy`) |

**The summary strip answers "what is this app set to?" without opening anything.** It
sits above step 1 and never collapses: app **name**, app **id**, then the *effective*
`residency` (violet — the residency dimension, §9c), `policy_mode` (amber when it is
`passthrough`), `capture`, and `detection` (which reads `off (passthrough)` when the
posture has turned detection off). While any step holds unsaved edits the strip says so
by number — *"unsaved edits in steps 3, 4 — shown values are not saved yet"* — and each
affected step header carries a dot.

A **Start from a recipe** picker sits between the summary strip and step 1. It offers the
three recipes from [`choosing-app-settings.md`](./choosing-app-settings.md#2-the-three-recipes)
— *Protected*, *Router only — no PII checks*, *Protected, no telemetry I/O* — shows what
each would change as `field: current → new`, and **fills the form without saving**: you
review the change and then press Save. It sets `policy_mode` and `capture_io` only and
never touches placement.

**Step 1 — Access.** The token list with each token's prefix, name and status. *+ issue
token* mints one and reveals it **exactly once**, with a copy control; it is stored
hashed and can never be shown again. *revoke* is immediate and cannot be undone — issue
the replacement first, roll the caller over, then revoke the old one.

**Step 2 — Placement.** Where a request is permitted to execute. `onprem_only` is
absolute: cloud providers are filtered out of the candidate pool *before* the router
scores anything. Nothing in any step below can change this. The `providers`, `tiers` and
`models` controls all read "none/empty = all".

**Step 3 — Protection.** `policy_mode` **is the coarse switch.** `enforced` (the default)
means detection, redaction in both directions, sensitivity escalation, custom
recognizers, and the capture ceiling. `passthrough` means RouteForge is a router, meter
and tracer only: nothing is detected and nothing is redacted, so the prompt reaches the
model verbatim and the completion comes back verbatim. Selecting it raises an inline
banner that says exactly that, alongside what *continues* unchanged — routing and tier
selection, budgets, rate limits, usage metering, the full decision trace, Langfuse
tracing — and one thing worth being blunt about: **`passthrough` does not touch
placement.** Residency in step 2 still decides where compute may run, `onprem_only` stays
absolute, and `onprem_only` + `passthrough` ("run only on my hardware, and don't touch my
data") is a valid, honest configuration with the on-prem guarantee fully intact. The
posture is never silent: it is folded into `app_policy_version`, stamped on every
response, and emitted on the trace.

**The Detection & narrowing sub-group refines the `enforced` posture.** It is a
collapsible group inside step 3: open by default while the posture is `enforced` (it is
the substance of that posture), and collapsed to a single line under `passthrough`
— *"inert while passthrough — 3 exempt types kept"* — instead of a screenful of controls
that do nothing. It stays openable and editable there, and its stored values are **kept,
not cleared**; opening it under `passthrough` shows a note saying so. Flipping the
posture must never destroy the configuration you spent time on; set the app back to
`enforced` and the same values apply again.

- **`pii detection`** — leaving it unset inherits the deployment's live detector, which
  is *not* a downgrade. Only an explicit `off` or `regex` is, and it blinds the gate to
  everything. Prefer the three narrow levers.
- **`min confidence`** (`pii_score_threshold`) — drops detected entities scoring below
  it before redaction; many false positives score around 0.6. Never applies to
  `US_SSN`, and never drops regex or custom-recognizer hits, which carry no score.
  Blank the box to remove a stored floor entirely; `0` is a different setting — a floor
  that passes every score.
- **`exempt types`** (`pii_entity_denylist`) — entity types the gate still detects but
  does not redact for this app. Presidio built-ins only. `US_SSN` renders as
  `US_SSN (locked)`, is not clickable, and can never be selected.
- **`exempt values`** (`pii_exempt_patterns`) — regexes matched against the *whole
  detected value*, whatever type Presidio gave it: the type-independent complement to
  exempt types, for a value shape that keeps landing under a different label. Rows
  validate as you type, and a tester below lets you paste candidate values (one per
  line) to see which would be exempted. **That tester runs entirely in your browser** —
  nothing typed into it is sent anywhere. `US_SSN` is never exempted, however a pattern
  reads.

**Step 4 — Telemetry.** `capture_io` decides what this app's traces carry. Raw I/O only
ever reaches this app's *own* Langfuse project — never the shared one. **The app's
Langfuse credentials now live here, directly above `capture_io`, because they gate it:**
host, public key, secret key and project id, with their own **Save credentials** button
(they used to sit at the very top of the tab, roughly a thousand pixels from the control
they unlock). Use that project's **project**-scoped keys, not organization keys; `host`
is the server-reachable ingestion host (e.g. `http://langfuse-web:3000`), not the browser
URL. The line above the fields says whether credentials are set. The secret is
write-only — it is never rendered back, before or after saving. Full detail in
[`../langfuse-per-app-projects.md`](../langfuse-per-app-projects.md).

**Step 5 — Delivery and limits.** How the answer is delivered, and the app's quotas.

**Step 6 — Privacy rules.** Badged *advanced* and, like every other step, closed until
you open it — see 8b.

**Long explanations are one click away, not deleted.** Every control keeps one short
inline hint; the longer prose — what the server clamps do and why — moves behind a small
`?` toggle next to the label. Nothing was removed in the restructure, only relocated.

**"Inherited" markers name the actual value.** Each control's unset option reads
`(global default — redacted)` rather than a bare "(global default)", using the deployment
envelope from `GET /admin/me` (`default_policy_mode`, `default_pii_detection`,
`default_capture_io`, `reject_stream_requests` and the two ceilings). Two exceptions,
both deliberate: `residency`'s unset option names the *application's* default
(`(app default — onprem_only)`), and `stream_mode` has no deployment-wide default at all,
so its unset option instead names what the deployment will do with `stream: true`
("rejected", or "served unstreamed"). Headroom's "(global default)" is the one marker
that still names no value — the envelope does not carry it. Against a server that
predates the envelope the console falls back to naming nothing rather than guessing.

**Options the server would refuse are disabled with the reason on the option itself**,
not silently greyed. `capture_io = full` needs the app's own Langfuse project *and*, for
an `onprem_only` app, `policy_mode = passthrough`; `capture_io = redacted` is
unavailable under `passthrough` or an explicit `pii_detection` of `off`/`regex`, because
that export path reuses the gate's sanitized prompt and there is no sanitized prompt to
reuse; `stream_unverified` is unavailable for an `onprem_only` app, which may never
waive step-9 verification. The value an app is *currently* set to is never disabled —
you can always see what it actually is.

Two honesty notes about what this form does and doesn't guarantee:

- **The server is the authority, not this form.** The console mirrors the write-time
  clamps so you find out early, but it never invents a rule of its own. When a save is
  rejected, the server's 422 appears verbatim under the Save button — it names the rule
  that fired and the alternative to use, and that text is your guidance. (Until
  `:0.17.0` this form discarded every error, so a rejected save looked like a no-op.
  See B22/B23 in `bugs-backlog.md`.)
- **Saving merges — with one deliberate exception.** A control you don't touch keeps
  whatever the app has stored. Moving a control back to its "(global default)" option is
  the exception: that field is cleared and the app inherits the deployment default again
  (the console sends it in the PUT's `reset` list). The line above Save names exactly
  which stored values a save will clear, because clearing one is a real change — dropping
  `policy_mode`, for instance, hands the app back to the deployment's posture, and the
  server refuses that write outright if it would leave an `enforced` `onprem_only` app
  still storing raw I/O.

**One PUT covers steps 2–5.** Placement, protection, telemetry and delivery are one row
server-side, so **Save policy** appears at the foot of each of those four steps and every
copy does the same single `PUT /admin/applications/{id}/policy`. When other steps also
hold unsaved edits the button says which ones it will carry along ("Also saves unsaved
edits in steps 2, 5"), so a save from step 4 is never a surprise. The three genuinely
separate writes keep their own buttons: **Save credentials** in step 4, **Save privacy
rules** in step 6, and the token actions in step 1, which apply immediately.

Field-by-field semantics, the clamps, and the equivalent `curl` are in
[`../per-app-config.md`](../per-app-config.md).

### 8b. Privacy rules

`admin/PrivacyRulesEditor.tsx`, mounted as **step 6** of the Settings tab and badged
*advanced*; its header summarises what the app has (*"global defaults"*, or *"2 custom
recognizers · 3 sensitivity rules"*). Once opened it stays mounted, so collapsing the
step never throws away unsaved rule edits. Lets an owner override this **one
application's** classification without touching
the global `config/policy.yaml` — the same rules that both the policy gate (Serving
Plane step 2) and output verification (Serving Plane step 9) consult for this app. Backed by
`GET`/`PUT /admin/applications/{id}/privacy`.

| Section | What it does |
|---|---|
| **Custom recognizers** | Add app-specific PII patterns: a **name** and a **regex** (plus a match-score field). These extend Presidio's built-in entity types with ones only this app cares about (e.g. an MRN pattern for a hospital app). |
| **Sensitivity rules** | An ordered list — **first match wins** — mapping one or more **entities** (chips, built-in + this app's custom names) to a **sensitivity** (`public`/`internal`/`confidential`/`restricted`), **risk** (`low`/`medium`/`high`), **residency** (`any`/`private_cloud_ok`/`onprem_only`), and a **require verification** checkbox. Reorder rules with **↑/↓**; the order determines which rule fires for an entity. |
| **Downgrade warning** | An amber banner appears whenever a rule maps a sensitive-looking entity (SSN, card, bank number, medical license, name, email, phone) to residency **"any"** — i.e. it would let that entity type leave on-prem. It only *flags* the deliberate downgrade; it does not block saving. A tenant's residency floor still can't be loosened underneath it — the backend only ever applies an app rule if it is **at least as strict** as the tenant default. |
| **Test against sample text** | A client-side-only box: type or paste sample text and it highlights matches from the recognizers above, entirely in the browser. Nothing is sent to the server — it previews regexes, it does not query live redaction. |
| **Save / Reset to global defaults** | **Save privacy rules** persists the recognizers + rules for this app. **Reset to global defaults** clears both lists so the app falls back to classifying exactly like the global `policy.yaml` (identical to an app with no profile at all). |

No custom recognizers or rules means the app classifies with the global defaults only —
the panel says so under each empty list. This is the mechanism behind
architecture-review.md §4.1: the seeded RAG/Support Agent profiles demote `LOCATION`/
`DATE_TIME` to `internal`/`low` so a casual place-name mention (e.g. "capital of France")
no longer trips the medium risk floor and stays on the cheap tier.

---

## 9. Colour & label legend

**Colour carries meaning here — the same hue means the same thing everywhere.** The
palette is single-sourced in `ui/src/tokens.ts`; that file is the authority if this table
ever drifts from it again.

### 9a. Tier colours — *which model/tier the router chose*

Used on **request-row dots, the Traffic donut, the fleet mini-bars, and rejected
candidates** (`TIER_HEX`). The dot answers "*what ran?*", **not** anything about
compression.

| Tier | Colour | Means |
|---|---|---|
| `cheap` | 🟢 mint `#35D6A0` | routed to the cheap tier |
| `mid` | 🔵 blue `#4C8DFF` | routed to the mid tier |
| `frontier` | 🟠 amber `#F5A524` | routed to the frontier tier |
| `?` (unknown) | ⚪ grey `#6b7280` | tier not resolved |

> **Frontier is amber, not red.** Red is reserved — see 9c. Landing on the frontier tier
> is an expensive outcome worth noticing, not a failure, and the palette says so.

There is **no `strong` tier** — tiers are exactly `cheap | mid | frontier`.

### 9b. Savings-lever colours — *how money was saved* (Cost tab only)

Used **only** on the Cost tab "Savings by lever" bar. This answers "*how did we save?*",
not "what ran." Both levers deliberately reuse a tier hex rather than introducing new
hues (`LEVER_HEX`):

| Lever | Colour | Means |
|---|---|---|
| routing | 🔵 blue `#4C8DFF` (= mid) | saved by choosing a cheaper model |
| compression | 🟠 amber `#F5A524` (= frontier) | saved by removing tokens (Headroom) |

### 9c. Signal colours — *dimensions that are not tiers*

| Signal | Colour | Means |
|---|---|---|
| privacy / residency | 🟣 violet `#8B7BF7` | the residency dimension — never a tier |
| alert | 🔴 red `#F2555A` | **residency breach or health failure only** |
| quality | 🩷 pink `#EC4899` | the quality-proxy axis (cost-vs-quality chart) |

> **Red means something is wrong.** It is not used for any tier, cost or lever. If you
> see red, read it as an alarm — most often the fleet's residency-breach glyph.

---

## 10. UI → backend map

The `/api/*` console surface is **admin-gated** — every call carries the admin session
cookie (`same-origin`); the packaged deploy's nginx reverse-proxies `/api`, `/admin`,
`/v1` same-origin to the API pod so the cookie applies.

| UI screen | Method + path | Handler |
|---|---|---|
| Fleet table | `GET /api/fleet` | `ui_routes` |
| Fleet app names/policy | `GET /admin/applications` | `admin_routes` |
| App request feed (seed) | `GET /api/requests?limit=500&app=<id>` | `ui_routes.list_requests` |
| Drill-down full trace | `GET /api/requests/{id}` | `ui_routes.get_request` |
| Live updates (all tabs) | `GET /api/stream` (SSE) | `ui_routes.stream` |
| Settings steps 1–6 | `/admin/applications/*` (policy, langfuse, privacy, tokens), `/admin/tokens/*`, `/admin/me` | `admin_routes` |
| Cross-app usage report | `GET /admin/usage/by-application` | `admin_routes` |
| Infrastructure (onboard models) | `/admin/infra/models*` | `admin_routes` |
| Headroom toggle (header) | `GET`/`POST /api/controls` | `ui_routes` |
| Serving a request | `POST /v1/chat/completions` | `routes.py` |

Every panel is driven by the `RequestSummary` / `TraceRecord` contract in `ui/src/api.ts`
— the "why" behind each decision is the demo.

---

## 11. Privacy invariants the UI upholds

- **Never renders raw PII** — entity **types** only, everywhere (list, chips, gate,
  verify).
- **Never renders CCR cache contents** — compression shows **counts** only.
- **Greys excluded providers** rather than hiding them — the exclusion (and its reason)
  is the point.
- **Never echoes the prompt** — the Request step shows id + model, not the input text
  (a request can literally carry an SSN). The model's **answer** is shown at the Response
  step; the input is not.

---

## 12. FAQ — reading the console correctly

### "What does '2 hops' mean?"

A **hop = one routed LLM call.** Most requests are single-shot: gate → route → one call.
A **multi-step** request goes to the **agent runtime**, which splits the task into
several sub-calls, and **each hop independently re-enters compression (step 5) and the
router (step 6)** — so a 2-hop request made two separate routing decisions and could
have landed on different tiers. The `N hops` badge is the visible proof that **routing
is per-call, not per-request** — the single most important behaviour this product shows.

### "The rejected list shows several models — are they all real?"

Not all of them are *reachable*. The effective pool the router scores is built by the
`ModelRegistry` and has two halves:

| Half | Where it comes from | Real on a typical deploy? |
|---|---|---|
| **On-prem candidates** | Whatever an operator has **onboarded** and left enabled (Infrastructure → Onboard; `onboarded_models`). The pool **boots empty** — routeForge ships no bundled model. | ✅ Real — these are the operator's own OpenAI-compatible endpoints, and are what actually serves. |
| `azure-gpt-mid` (mid) — GPT-4o mini (Azure) | `config/routing.yaml` → `model_pool` | ❌ Candidate only while `providers.azure.enabled: false`. |
| `bedrock-frontier` (frontier) — Claude Sonnet (Bedrock) | `config/routing.yaml` → `model_pool` | ❌ Candidate only while `providers.bedrock.enabled: false`. |

The two cloud models exist **in the pool so the routing story is visible** — they appear
in the **rejected / greyed-out** list with a reason ("provider disabled", or "excluded ·
residency" for an `onprem_only` request). A model that was *chosen and served* is always
an onboarded on-prem one on a lab with cloud switched off. See
[`../onboarding-models.md`](../onboarding-models.md).

### "How do I turn Headroom (compression) on?"

There **is** a console control now: a **Headroom toggle in the top-right chrome**
(`ui/src/components/HeadroomToggle.tsx`, mounted in `App.tsx:64`, shown only while
signed in — `{auth === "in" && <HeadroomToggle />}`). It sits between the live/
disconnected dot and the theme toggle, showing **on / off** with an animated switch.
Clicking it flips the global compression default live (optimistic UI, then reconciled
with the server), reading `GET /api/controls` and writing `POST /api/controls`.

| Layer | Toggle present? |
|---|---|
| Backend `POST /api/controls` | ✅ Mutates `app.state.co_cfg` in place — next request on either the direct or per-hop agent path picks it up, **no restart**. Admin-gated. |
| API client `api.controls()` / `api.setControls()` | ✅ Present (`ui/src/api.ts`). |
| Console UI | ✅ **HeadroomToggle** in the header chrome (`App.tsx:64`). |

**Scope:** this switch is the **global default** (`app.state.co_cfg`, server-wide). A
per-application override in an app's **Settings** tab, step 5 Delivery and limits
(`AppPolicy.compression_enabled`), **wins** over it; apps left at "(global default)"
follow this switch. Safety bypasses
(high-risk / restricted / below-min-tokens) still apply on top regardless — see
`app/compression`.

You can also set the startup default in `config/serving.yaml` →
`context_optimization.enabled` (defaults OFF so the demo can show before/after; a restart
applies it, but the header toggle changes it live without one), or drive it via curl:

```bash
# Live, via the admin-gated API (needs the admin session cookie):
curl -X POST http://<api-host>:30808/api/controls \
  -H 'Content-Type: application/json' -b 'rf_admin_session=<cookie>' \
  -d '{"headroom_enabled": true}'
```

### "What's in Langfuse vs. this console?"

Different jobs, same source. **Langfuse** is the operator's system of record — every
trace, per-hop spans, tool I/O, cost, latency, **PII-redacted** (prompts/answers are
`null`; only `sp.*` decision attributes are exported). The **console** is the customer's
decision story — which branch, why, what was excluded — in plain language. The Langfuse
link deep-links to *this* request's trace, but the console never needs Langfuse to tell
its story.

### "The Langfuse trace link bounces me to a login."

Langfuse gates trace pages behind a **logged-in browser session**. Open
`http://<langfuse-host>:30300` once and sign in as `demo@example.com` / `demodemo`, then
click the link again.

### "Why can't I see the question that was asked?"

Deliberate. A request can carry PII (the SSN persona literally does), and the UI **never
renders raw input**. The **answer** is shown at the Response step; the prompts live in
`ui/src/api.ts` (`SCRIPTED`) and `tests/traffic/personas.py`.

---

**Known gaps & deferred UI work:** see `docs/ui-open-items.md` — **all seven items are now
closed.** The "What happened" redesign, the Headroom toggle, the 0 ms multi-step latency
bug, per-request `$0.0000` precision, the two-blues recolour, the over-eager fleet ⚠, and
the labelled/copyable Application ID (#7, shipped `ui:0.17.0`) have all shipped.

---

*Source of truth: `ui/src/console/` (screens), `ui/src/console/sections/` (tabs),
`ui/src/console/aggregate.ts` (the per-tab math), `ui/src/api.ts` (the API contract),
`app/api/ui_routes.py` (backend). Design brief: `.claude/skills/demo-ui/`.*
