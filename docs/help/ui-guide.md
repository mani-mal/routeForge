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
                 │                                     ├─ Overview
                 └─ Manage applications                ├─ Cost
                    (create + config + tokens)         ├─ Privacy
                                                        ├─ Traffic
                                                        ├─ Requests (list + drill-down)
                                                        └─ Settings
```

- **Console Shell** (`ConsoleShell.tsx`) — the only top-level surface after login.
  Nav on the left (**Fleet**), and **＋ New application** on the right which opens the
  **Manage applications** panel (create app, per-app config, tokens).
- **Fleet Overview** (`FleetOverview.tsx`) — the landing screen: an all-apps summary
  strip + a sortable per-application table. Click a row to open that app.
- **Application Page** (`AppPage.tsx`) — the per-app "answer desk": six tabs, scoped to
  **one** application's traffic.

The top-right chrome (`● connected/disconnected`, light/dark toggle, **Sign out**,
**POWERED BY CDW**) is the outer app frame, above the console shell.

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
| **⚠** | Amber warning when **sensitive traffic was not fully kept on-prem** — i.e. `pii_count > 0` **and** `onprem_count < pii_count`. It flags a possible residency gap for that app. |

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

- **routing** (blue `#38bdf8`) — dollars saved by **sending the request to a cheaper
  tier** instead of frontier.
- **compression** (amber `#f59e0b`) — dollars saved because **Headroom removed tokens**
  before pricing. Reads **"off / not run"** whenever `compression_saved == 0` (Headroom
  disabled or it never fired).

They satisfy the identity `routing_saved + compression_saved == baseline − actual`.
Keeping them separate is deliberate: the customer must be able to tell which lever is
working.

### 4c. Cost over time

Cumulative line chart as requests accrue (ordered by `created_seq`): **actual**
(green) vs. **baseline** (grey). The widening gap is the running saving.

### 4d. Tiles (bottom)

Repeat the four figures as stat tiles: **Total spend**, **All-frontier baseline**,
**Routing saved**, **Compression saved** (again "off / not run" when zero).

### 4e. How baseline & "% saved" are computed

The **all-frontier baseline** is **not a flat per-request number** — it is computed from
**each request's own token count**, re-priced at the frontier model's rate, then summed
across every request in the app.

Prices are USD per 1M tokens (`config/pricing.yaml`); the frontier baseline model is
`bedrock-frontier`:

| Model (slug) | Tier | Input /1M | Output /1M | Combined /1M |
|---|---|---|---|---|
| `local-8b` | cheap | $0.05 | $0.05 | **$0.10** |
| `local-32b` | mid | $0.10 | $0.10 | **$0.20** |
| `azure-gpt-mid` | mid | $0.15 | $0.60 | $0.75 |
| `bedrock-frontier` | **frontier (baseline)** | $3.00 | $15.00 | **$18.00** |

The cost code (`app/pipeline.py:48-50`) prices a request's tokens at the **combined**
input+output rate:

```
baseline_for_one_request = total_tokens / 1,000,000 × ($3.00 + $15.00)   # = ×$18.00/1M
```

**Worked example — one request using 10,000 tokens, served on the cheap local tier:**

```
baseline (if it had gone to frontier):  10,000 / 1e6 × $18.00 = $0.1800
actual   (it went to local-8b):         10,000 / 1e6 × $0.10  = $0.0010
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
| **Tier mix** (donut) | Requests per tier, coloured cheap green / mid blue / frontier red. A legend beneath lists `tier count`. Hover a slice for the count. |
| **p50 latency** | Median request latency (ms). |
| **p95 latency** | 95th-percentile request latency (ms). |
| **% multi-step** | Share of requests that took the **agent path** (`hops > 1`) — i.e. made more than one routed LLM call. |

> ⚠ **Known bug:** multi-step (agent) requests currently report **0 ms** latency, which
> drags p50/p95 down and makes the latency figures understate reality. Per-hop latency
> isn't summed into the request total yet. Tracked; single-shot latency is correct.

---

## 7. Requests tab — the drill-down

`AppPage.tsx` renders a two-column split: **RequestList** (left) + **RequestDrilldown**
(right). Click a row on the left to load its full trace on the right
(`GET /api/requests/{id}`).

### 7a. Request list (left)

`components/RequestList.tsx`. Newest first; selected row highlighted green. Each row:

- **Tier dot** — cheap green / mid blue / frontier red.
- **Model** — human-facing name (`Qwen2.5-7B-Instruct (local)`); hover shows the routing
  slug (`local-8b`). From `display_name` in `config/routing.yaml`.
- **`N hops`** badge — multi-step requests (agent path).
- **Cost** (right, USD 4dp).
- **Sensitivity** (`public` / `confidential` / `restricted`) and **latency** beneath.

### 7b. Drill-down (right) — two altitudes

`RequestDrilldown.tsx`. Two bands: a plain-language summary, then the full technical
trace.

#### Band 1 — "What happened" (owner-facing)

One sentence, no jargon:

> Checked for sensitive data (**found DATE_TIME** / **none**) → **kept on your
> infrastructure** / **eligible for cloud** → answered by **&lt;model&gt;** → **verified
> clean** / **flagged for review** / **not verified**, in **&lt;latency&gt;** for
> **&lt;cost&gt;**.

Beneath it, the **Serving providers** strip — three chips **On-Prem / Azure / Bedrock**:

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

`admin/ApplicationsPanel.tsx` (`AppConfig`). Per-app admin config — policy (default
sensitivity, residency, budgets) and **API tokens** (create / revoke; the full token is
shown once at creation, only its prefix thereafter). This is CRUD over `/admin/*`, not a
read view.

---

## 9. Colour & label legend

**There are two separate colour systems, and both contain a blue — don't confuse them.**

### 9a. Tier colours — *which model/tier the router chose*

Used on **request-row dots, the Traffic donut, the fleet mini-bars, and rejected
candidates** (`TIER_COLOR` in `api.ts`). The dot answers "*what ran?*", **not** anything
about compression.

| Tier | Colour | Means |
|---|---|---|
| `cheap` | 🟢 green `#22c55e` | routed to the cheap tier (`local-8b`) |
| `mid` | 🔵 blue `#3b82f6` | routed to the mid tier (`local-32b`) |
| `frontier` | 🔴 red `#ef4444` | routed to the frontier tier |
| `?` (unknown) | ⚪ grey `#6b7280` | tier not resolved |

> **Example:** in the Requests list, a plain public question shows a **green** dot (cheap
> tier); a `confidential` / `2 hops` request shows a **blue** dot (mid tier) — the router
> picks a higher tier for heavier/more-sensitive work. Green vs blue here has **nothing to
> do with compression** (which may be off for both).

There is **no `strong` tier** — tiers are exactly `cheap | mid | frontier`.

### 9b. Savings-lever colours — *how money was saved* (Cost tab only)

Used **only** on the Cost tab "Savings by lever" bar. This answers "*how did we save?*",
not "what ran."

| Lever | Colour | Means |
|---|---|---|
| routing | 🔵 blue `#38bdf8` | saved by choosing a cheaper model |
| compression | 🟠 amber `#f59e0b` | saved by removing tokens (Headroom) |

> **Heads-up — the two blues are close.** Mid-tier `#3b82f6` and routing `#38bdf8` are
> both blue but mean unrelated things (a *tier* vs a *savings lever*) and never appear in
> the same panel. If they still read as related on a projector, recolour one (see the
> FAQ / open items).

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
| Settings (policy/tokens) | `/admin/applications/*`, `/admin/tokens/*` | `admin_routes` |
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

No. There are **four models in the pool** (`config/routing.yaml` → `model_pool`), but on
the current DGX deploy only **one** is actually served:

| Model in pool | Display name | Real on this deploy? |
|---|---|---|
| `local-8b` (cheap) | Qwen2.5-7B-Instruct (local) | ✅ Real — served by vLLM. |
| `local-32b` (mid) | Qwen2.5-7B-Instruct (local) | ✅ Real, but **the same 7B** — an alias until a distinct 32B lands. Representative, not distinct. |
| `azure-gpt-mid` (mid) | GPT-4o mini (Azure) | ❌ Candidate only — `providers.azure.enabled: false`. |
| `bedrock-frontier` (frontier) | Claude Sonnet (Bedrock) | ❌ Candidate only — `providers.bedrock.enabled: false`. |

The two cloud models exist **in the pool so the routing story is visible** — they appear
in the **rejected / greyed-out** list with a reason. A model that was *chosen and served*
is always one of the local pair today.

### "How do I turn Headroom (compression) on? There's no button."

**Correct — there is no console control for it today.** The old demo UI had a
"Headroom on/off" toggle; it was dropped when the per-application console replaced the
3-column layout, and no equivalent was added back. The backend still supports a **live**
toggle — it's just not wired to any button.

| Layer | Toggle present? |
|---|---|
| Backend `POST /api/controls` | ✅ Mutates `app.state.co_cfg` in place — next request picks it up, **no restart** (`app/api/ui_routes.py:99`). Admin-gated. |
| API client `api.setControls()` | ✅ Present (`ui/src/api.ts:141`). |
| Console UI | ❌ Nothing renders/calls it on any tab. |

Two ways to enable it until a control is added:

```bash
# 1. Live, via the admin-gated API (needs the admin session cookie):
curl -X POST http://<api-host>:30808/api/controls \
  -H 'Content-Type: application/json' -b 'rf_admin_session=<cookie>' \
  -d '{"headroom_enabled": true}'

# 2. At startup: config/serving.yaml → context_optimization.enabled: true
#    (defaults OFF so the demo can show before/after). Requires a restart.
```

**Caveat:** this switch is **global** (`app.state.co_cfg`, server-wide), **not
per-application** — one lever for the whole server, not per-tenant. Because the demo
narrative depends on toggling Headroom live (story step 4 in `CLAUDE.md`), the missing
button is a known gap; the natural fix is a Headroom toggle in the app **Settings** tab
or a global control in the console header.

### "Why does multi-step latency read 0 ms?"

**Known bug** (Traffic tab and drill-down). Per-hop latency on the agent path isn't yet
summed into the request-level total, so multi-step requests report 0 ms and pull the
p50/p95 down. Single-shot latency is correct. Tracked for a fix.

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

**Known gaps & deferred UI work:** see `docs/ui-open-items.md` — includes the planned
redesign of the "What happened" band (§7b), the missing Headroom toggle, the 0 ms
multi-step latency bug, per-request `$0.0000` precision, and the two-blues recolour.

---

*Source of truth: `ui/src/console/` (screens), `ui/src/console/sections/` (tabs),
`ui/src/console/aggregate.ts` (the per-tab math), `ui/src/api.ts` (the API contract),
`app/api/ui_routes.py` (backend). Design brief: `.claude/skills/demo-ui/`.*
