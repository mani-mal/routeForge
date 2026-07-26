# Serving Plane Demo UI — Guide

A single-page React app that visualizes the ten-step Serving Plane as requests flow
through it. **The UI is the product in this demo** — the answer text is almost
incidental; what sells is showing the *decision*: which branch a request took, why,
what it cost, and what was excluded.

- **Stack:** React 18 + Vite + Tailwind + Recharts. Source in `ui/src/`.
- **Talks only to our API** — no third-party calls, no client-side secrets.
- **Live** — decisions stream in over SSE; the dashboard fills while the room watches.

This guide covers **what each region shows**, **the controls you have**, and
**how the UI maps to the backend**. Screenshot region names below match the code
(`ui/src/components/`), which differ only in casing from what renders on screen.

---

## 1. Screen layout at a glance

```
┌───────────────────────────────────────────────────────────────────────────┐
│  HEADER:  Serving Plane · Phase-6 demo        104 requests   ● live         │
├───────────────────────────────────────────────────────────────────────────┤
│  CONTROL BAR:  [Headroom off] [Clean question] [PII (SSN)] [Multi-step]     │
│                [High-risk (PHI)]        [Run 100 burst]   coming later: …    │
├──────────────────────┬──────────────────────┬─────────────────────────────┤
│  LEFT  (col-span 4)  │  MIDDLE (col-span 4)  │  RIGHT (col-span 4)         │
│                      │                       │                             │
│  Flow Visualizer     │  Decision Inspector   │  Cost Dashboard             │
│  (10 step nodes +    │  (step-by-step trace, │  (spend vs baseline,        │
│   provider chips)    │   chosen vs rejected) │   savings levers, charts)   │
│                      │                       │                             │
│  Requests list       │  Compression (step 5) │                             │
│  (newest first)      │                       │                             │
└──────────────────────┴──────────────────────┴─────────────────────────────┘
```

Layout source: `ui/src/App.tsx`. On screens narrower than `lg` the three columns
stack vertically.

---

## 2. Header

Top strip (`App.tsx`):

| Element | Meaning |
|---|---|
| **Serving Plane · Phase-6 demo** | Static title. |
| **N requests** | Count of requests currently held in the UI (capped at 200, see §9). |
| **● live / ● disconnected** | SSE connection state. Green dot = the decision stream is connected; grey = dropped (EventSource auto-reconnects in the background). |

---

## 3. Control bar — the live controls

**This is the top row of buttons** and it is how you *drive* the demo
(`ui/src/components/ControlPanel.tsx`). Read it as two groups:

- **Headroom on/off** (far left) — a **mode toggle**, not a request. It flips step-5
  compression on or off for *subsequent* requests. Nothing fires when you click it.
- **The four persona buttons + Run 100 burst** — each of these **fires a real request**
  (or 100) through the full pipeline. When you click one, watch the UI light up
  left → right: the **Flow Visualizer** lights each step as it runs, a new row appears
  in the **Requests list**, and the **Cost Dashboard** ticks up. That is the whole
  point — you are watching one request's decision happen live.

So the intended motion is: *(optionally) set Headroom → click a persona button →
watch it flow → click the new row → click a flow step to inspect it.*

| Control | What it does | Backend call |
|---|---|---|
| **Headroom on/off** | Toggles step-5 context compression. Turn it off, fire a multi-step task, then turn it on and re-fire the *same* task — token counts collapse for the same answer. The button turns green when on. | `POST /api/controls` |
| **Clean question** | Fires a scripted public request → cheap local tier. | `POST /v1/chat/completions` (key `acme-key`) |
| **PII (SSN)** | Fires a request containing an SSN → gate marks it `onprem_only`, cloud providers grey out. | `POST /v1/chat/completions` (key `hospital-key`) |
| **Multi-step task** | Fires an agent task → several hops, each re-entering the router (watch the `↻ ×N` badge). | `POST /v1/chat/completions` (`mock_multistep: true`) |
| **High-risk (PHI)** | Fires a high-risk on-prem request → floors to best allowed tier + `require_verification`. | `POST /v1/chat/completions` (key `hospital-key`) |
| **Run 100 burst** | Fires a 100-request seeded traffic burst at the live API so the dashboard fills. The server self-calls its own `/v1/chat/completions` in-process; the button shows "running…" (pulsing) until it finishes. | `POST /api/traffic` |
| **coming later: …** | Honest note listing deferred controls (e.g. cost-weight slider, per-request provider toggles) not yet wired for this prototype. Read-only text. | — |

The four scripted buttons are defined in `ui/src/api.ts` (`SCRIPTED`) — canned message
bodies mapped to persona API keys. While a request is firing the buttons disable and
the pressed one reads "firing…".

> The result of any fired request also arrives via the SSE stream, so even if the
> `fetch` fails the panels still update from the stream.

---

## 4. LEFT column

### 4a. Flow Visualizer — the centrepiece (interactive)

`ui/src/components/FlowVisualizer.tsx`. Renders the **ten serving-plane steps** as an
**interactive, numbered spine**. Each node is a `<button>` showing its number (`01`–`10`),
name, and a one-line caption of what the step does. **Click any node** to focus that
step's detail in the Decision Inspector (click again, or "← all steps", to return to the
full trace). This is how a customer walks the whole pipeline one step at a time.

The ten nodes (`STEPS` in `api.ts`, each with a `caption`):

`01 Request · 02 Policy Gate · 03 Policy Context · 04 Task Mode · 05 Compression ·
06 Model Router · 07 Gateway · 08 Backend · 09 Verify · 10 Response`

**Why is Compression "step 5"?** The numbers are **fixed positions in the
architecture's ten-step Serving Plane**, not an order the UI invented. Every request
travels the same 1→10 spine; each node is always the same step. Step 5 is
*Conditional Context Optimization* (Headroom) — it sits between task-mode selection (4)
and the model router (6) on purpose: compression must run **after** the policy gate has
sanitized the content and **before** the router prices tokens, so the router scores the
*compressed* size. That ordering is load-bearing (see `CLAUDE.md`), which is why the
step keeps its number even on requests where compression doesn't fire.

**Clicking a node changes the Decision Inspector.** Each of the ten nodes drives a
step-focused view in the middle column. What you see per step:

| Click | Inspector shows |
|---|---|
| **01 Request** | Request id + served model. **Not the prompt text** — see §5a / FAQ. |
| **02 Policy Gate** | Sensitivity, `onprem_only`, detected entity **types** (never values). |
| **03 Policy Context** | The `allowed_providers` the router is now locked to. |
| **04 Task Mode** | `direct answer` vs `multi-step agent (N hops)`. |
| **05 Compression** | tokens before → after + reduction %, or "off / bypassed — reason". |
| **06 Model Router** | Chosen model + reason, and the **rejected** candidates with reasons (one card per hop). |
| **07 Gateway** | "no fallback" normally, or the fallback reason if a backend failed. |
| **08 Backend** | Served model, tokens in/out, latency — the actual serving path. |
| **09 Verify** | required / performed / passed, entity types found on egress. |
| **10 Response** | Token total + the **two-lever cost split** + the model's answer text. |

Click **"← all steps"** in the inspector to leave step-focus and see the full trace.

Each node has a **state dot** (this is what replaced the old cryptic "N steps recorded"):

- 🟢 **recorded** (green) — the step emitted a decision record you can inspect
  (Compression, Model Router, Verify, and Gateway on fallback).
- 🔵 **traversed** (accent) — the request passed through, but the step emits no per-request
  record; its detail is reconstructed from the policy / usage / cost blocks (Request,
  Policy Gate, Policy Context, Task Mode, Backend, Response).
- ⚪ **skipped** (grey) — not part of this request's path.

A small legend under the spine spells these out. The **selected** node gets an accent ring.

- **`↻ ×N` badge** on the Model Router node appears when a request made more than one
  hop (multi-step) — the visible proof that routing is *per call*, not per request.

**Serving providers** (below the nodes) — three chips: **On-Prem**, **Azure**,
**Bedrock**.

- **Eligible** (green) vs **excluded by policy** (grey). For a PII/`onprem_only`
  request the two cloud chips grey out with reason *"excluded by policy (residency)"*.
  **This single grey-out is the whole privacy story** — it shows cloud was removed from
  the candidate pool *before* the router scored anything, not filtered after.
- Eligibility comes from the selected request's `policy.allowed_providers`. With no
  request selected, all three show eligible.

### 4b. Requests list

`ui/src/components/RequestList.tsx`. Newest-first list; **click a row to inspect it**
(drives the middle column). The selected row is highlighted green.

Each row shows:

- **Tier dot** — colour-coded (see §7).
- **Model** — the real, human-facing model name (e.g. `Qwen2.5-7B-Instruct (local)`);
  hover shows the internal routing slug (`local-8b`). Names come from `display_name` in
  `config/routing.yaml`.
- **`on-prem`** badge if the request was `onprem_only`.
- **`N hops`** badge for multi-step requests.
- **Cost** (USD, 4 dp) on the right.
- **Sensitivity** (e.g. `public`, `confidential`, `restricted`) and **latency** below.

---

## 5. MIDDLE column

### 5a. Decision Inspector — the legibility panel (step-aware)

`ui/src/components/DecisionInspector.tsx`. The detail for the **selected** request. This
is what makes a decision legible on a projector.

Header line: **sensitivity**, **onprem_only** (true/false), and **entity-type chips**
(red) for any PII/PHI detected — *types only, never the raw values* (e.g. `LOCATION`,
`US_SSN`). Top-right is the **live Langfuse deep-link** ("Langfuse trace ↗", opens this
request's trace) — or "Langfuse off" when telemetry keys aren't set.

It has **two modes**, driven by whether a flow step is selected:

- **Step focus** (a node clicked in the flow visualizer): a focused explanation of *that
  step* for this request — including the six "traversed" steps that emit no record. The
  panel resolves each: **Gate/Context** from `policy` (sensitivity, residency, allowed
  providers, entity types), **Task Mode** from the hop count, **Backend** from
  usage/latency, **Response** from usage + the two-lever cost split + the answer text,
  and **Router/Compression/Verify/Gateway** from their records. "← all steps" returns.

> **Why you never see the question you asked.** The **Request** step (01) shows the
> request id and served model but *not* the prompt text — the panel says so explicitly
> ("The raw prompt is never stored or shown"). This is deliberate: a request can carry
> PII (the *PII (SSN)* persona literally contains an SSN), and a core UI rule is **never
> render raw PII**. So the input is not echoed anywhere. You *can* see the model's
> **answer** — it renders at the bottom of the **Response** step (10). If you need to
> know what was asked, the scripted prompts live in `ui/src/api.ts` (`SCRIPTED`) and the
> traffic prompts in `tests/traffic/personas.py`.
- **Full trace** (no step selected): one card per trace record, each showing (as
  applicable):

- **Step name** and its **latency**.
- **chosen:** the selected model/branch (green) **— reason**.
- **rejected:** the candidates that lost, each with a tier dot, name, `(provider)`,
  and **the reason** it was excluded (e.g. *"excluded by policy (residency)"*,
  *"below target tier"*). **The rejected list with reasons is the most important thing
  on this panel** — it's what proves the routing logic.
- **entity types found** (red chips) for verification steps.
- **est / actual / cumulative cost** — per-hop and running total (multi-step totals are
  accumulated per hop, never computed once at the end).
- **hop #N** for agent hops.

Top-right **"Langfuse trace ↗"** is a **live deep-link** to this request's full trace in
Langfuse (cost, latency, trajectory, zero raw PII), built from the request's Langfuse
trace id (`langfuse_url` on the response). It shows **"Langfuse off"** when telemetry keys
(`LANGFUSE_PUBLIC_KEY`/`SECRET_KEY`) aren't set, rather than linking to a dead page.

### 5b. Compression (step 5)

`ui/src/components/CompressionView.tsx`. The Headroom view for the selected request.

- If compression didn't run: **"compression off / n/a"**, or **"Bypassed — <reason>"**
  when it was intentionally skipped (e.g. *below_min_tokens*, *high risk*). Customers
  ask why, so the reason is always shown.
- When it ran: **tokens before → after** with a large **reduction %**, and *"reduction
  via <compressor>"*.
- Three tiles: **CCR stored** (yes/no), **CCR retrievals** (count), **→ cloud** (count
  of retrievals that went to cloud — should stay 0 for residency-bound tenants).
  Counts only — **CCR cache contents are never rendered**.

---

## 6. RIGHT column — Cost Dashboard

`ui/src/components/CostDashboard.tsx`. Aggregate view across all requests; fed by
`/api/stats` so numbers move during a traffic burst.

- **Headline:** big **actual spend** vs. the **all-frontier baseline**, and the **%
  saved** (green). On-prem tiers are priced at a small **representative amortized cost**
  (`config/pricing.yaml`), so actual spend is a believable non-zero figure and the saving
  is high-but-not-a-flat-100% — an honest "cost reduction" story rather than "free".
- **Savings by lever:** a horizontal bar split into **routing** (blue `#38bdf8`) and
  **compression** (amber `#f59e0b`), with the dollar figures beneath. **The two levers
  are shown separately, never merged** — so the customer can tell which one is working.
- **Tier distribution:** donut of requests per tier, coloured by tier (§7).
- **Cost over time:** cumulative **actual** (green) vs. **baseline** (grey) line as
  requests accrue.
- **Tiles:** **p50 latency**, **p95 latency**, **on-prem kept** (count of requests
  policy kept on-prem).

---

## 7. Colour & label legend

Tier colours are consistent everywhere — dots, donut, rejected candidates (`TIER_COLOR`
in `api.ts`):

| Tier | Colour |
|---|---|
| `cheap` | 🟢 green `#22c55e` |
| `mid` | 🔵 blue `#3b82f6` |
| `frontier` | 🔴 red `#ef4444` |
| `?` (unknown) | ⚪ grey `#6b7280` |

There is **no `strong` tier** — tiers are exactly `cheap | mid | frontier`. Savings-bar
colours (routing blue / compression amber) are distinct from the tier palette on
purpose so the two ideas don't blur.

All money is shown to 4 decimal places (`$0.0000`); missing values render as `–`.

---

## 8. UI → backend flow

### 8a. Endpoints (all under `app/api/`)

The Vite dev server proxies `/api` and `/v1` to the backend at `127.0.0.1:8080`
(`ui/vite.config.ts`). The client is `ui/src/api.ts`.

| UI action | Method + path | Handler |
|---|---|---|
| Initial + refresh request list | `GET /api/requests?limit=100` | `ui_routes.list_requests` |
| Inspect one request | `GET /api/requests/{id}` | `ui_routes.get_request` |
| Dashboard stats | `GET /api/stats` | `ui_routes.get_stats` |
| Read control state | `GET /api/controls` | `ui_routes.get_controls` |
| Toggle Headroom | `POST /api/controls` | `ui_routes.set_controls` |
| Start traffic burst | `POST /api/traffic` | `ui_routes.start_traffic` |
| Poll traffic status | `GET /api/traffic` | `ui_routes.traffic_status` |
| Live decision stream | `GET /api/stream` (SSE) | `ui_routes.stream` |
| Fire a chat request | `POST /v1/chat/completions` | `routes.py` (the real serving path) |

### 8b. The live stream (SSE)

`subscribeStream()` opens an `EventSource` on `/api/stream`. Two event shapes
(`StreamEvent` in `api.ts`):

- **`request`** — a completed request summary. Prepended to the list (cap 200); if
  nothing is selected it auto-selects; triggers a `stats` refresh.
- **`step`** — one `TraceRecord` as a step happens. Accumulated per `request_id` into
  `liveTraces` (cap 50 requests) so the flow visualizer can light up *before* the full
  trace is fetched.

A 3-second poll of `/api/stats` and `/api/traffic` runs as a backstop in case SSE drops
an update or the burst button's label needs to flip back.

### 8c. End-to-end: what happens when you fire a request

1. You click a scripted button → `POST /v1/chat/completions` with the canned body +
   persona key.
2. Backend runs the ten steps; **the policy gate runs first** — nothing external sees
   the raw request until it clears. Each step emits a `DecisionRecord`.
3. Records stream out on `/api/stream` as `step` events → the **Flow Visualizer** lights
   nodes live; a final `request` event adds the row to the **Requests list**.
4. Selecting that row calls `GET /api/requests/{id}` → the full trace populates the
   **Decision Inspector** and **Compression** panels.
5. The `request` event refreshes `/api/stats` → the **Cost Dashboard** updates.

Every panel is driven by the `DecisionRecord`/trace contract — the "why" behind each
decision is the demo.

---

## 9. Behaviours & limits worth knowing

- **Empty state:** a fresh demo has zero requests — panels show "fire one from the
  control panel" prompts. Fire a scripted request or **Run 100 burst** to populate.
- **Caps:** UI holds the newest **200** requests (`REQUEST_CAP`) and live traces for the
  newest **50** requests (`LIVE_TRACE_CAP`). Older ones drop from the client; the
  backend still has them.
- **Privacy invariants the UI upholds:** never renders raw PII (entity *types* only),
  never renders CCR cache contents (counts only), and greys excluded providers rather
  than hiding them.
- **Trace availability:** if you select a request whose full trace hasn't loaded, the
  inspector shows "still streaming" and falls back to the live trace until
  `GET /api/requests/{id}` returns.

---

## 10. Running the UI

```bash
cd ui
npm install        # first time
npm run dev        # Vite dev server on :5173, proxies /api + /v1 to :8080
```

The backend must be reachable at `127.0.0.1:8080` (the proxy target). For the packaged
demo the UI is built (`npm run build` → `ui/dist/`) and served behind nginx
(`ui/nginx.conf`); see the `greenlake-deploy` skill for compose/Helm.

---

## 11. FAQ — reading the demo correctly

Short answers to the things people trip over on first viewing.

### "The Requests list / rejected list shows several models — are they all real?"

No. There are **four models in the pool** (`config/routing.yaml` → `model_pool`), but on
the current DGX deployment only **one is actually served**:

| Model in pool | Display name | Real on this deploy? |
|---|---|---|
| `local-8b` (cheap) | Qwen2.5-7B-Instruct (local) | ✅ **Real** — served by vLLM. |
| `local-32b` (mid) | Qwen2.5-7B-Instruct (local) | ✅ Real, but **the same 7B** — `local-32b` is an alias of the one Qwen2.5-7B until a distinct 32B is deployed. Representative, not yet distinct. |
| `azure-gpt-mid` (mid) | GPT-4o mini (Azure) | ❌ **Candidate only.** `providers.azure.enabled: false` — never actually called. |
| `bedrock-frontier` (frontier) | Claude Sonnet (Bedrock) | ❌ **Candidate only.** `providers.bedrock.enabled: false` — never actually called. |

The two cloud models exist **in the pool so the routing story is visible** — they show
up in the **rejected / greyed-out** list with a reason (e.g. "excluded by policy
(residency)" for a PII request, or provider-disabled otherwise). A model that was
*chosen* and *served* is always one of the local pair today. Both local tiers deliberately
share the same display name because they are the same weights — this is called out in
`config/routing.yaml` and is honest by design.

### "The traffic uses persona questions (`personas.py`) — can I see them in Langfuse?"

**No — and that's the privacy guarantee working, not a gap.** Langfuse holds the
**decision trace**, not the conversation. Each trace has: the request id, tenant, chosen
model, cost, sensitivity, `onprem_only`, and detected entity **types** — and that's it.
The **prompt and the answer are never exported** (`input`/`output` are `null` on every
trace; only `sp.*` decision attributes are set in `app/obs/export.py`). This is the
"telemetry redacted on egress" rule: raw content never leaves for a telemetry sink.

So the persona questions in `tests/traffic/personas.py` (e.g. *"What is the capital of
France?"*, the SSN-bearing patient message, the multi-step log-summary task) are sent to
the API but **do not appear in Langfuse**. Where to actually see them:

- The **prompts**: read `tests/traffic/personas.py` (`_SIMPLE_QUESTIONS`,
  `_build_pii_bearing`, etc.) and `ui/src/api.ts` (`SCRIPTED`).
- The **answers**: the **Response** step (10) in the Decision Inspector renders the
  model's completion.
- The **decisions**: Langfuse (per-step spans, cost, latency, entity types, zero PII).

### "Why does the Response step show 'compression saved'?"

The **Response** step (10) is where the per-request **cost story** is totalled. It shows
four figures plus the answer:

| Row | Meaning |
|---|---|
| **actual spend** | What this request really cost, at the model it actually used. |
| **all-frontier baseline** | What it *would* have cost if every call went to the frontier model. The thing we're saving against. |
| **routing saved** | Dollars saved by **sending the request to a cheaper tier** instead of frontier. |
| **compression saved** | Dollars saved because **Headroom removed tokens** before pricing — fewer tokens billed. **$0 when Headroom is off.** |

These are the **two independent savings levers**, kept separate on purpose so you can
tell which one is doing the work (the Cost Dashboard shows the same split as a bar).
They satisfy the identity `routing_saved + compression_saved == baseline − actual`. To
see *compression saved* become non-zero, turn **Headroom on** and fire the **Multi-step
task** — the duplicated tool-log in that persona is what Headroom compresses.

### "The Langfuse trace link doesn't open the trace."

The link is correct and the trace exists — but Langfuse gates trace pages behind a
**logged-in browser session**. Open `http://<langfuse-host>:30300` once and sign in as
`demo@example.com` / `demodemo` (the seeded demo user, who is a member of the
`demo-project`), then click the link again. A browser that isn't logged in — or is
logged in as a different account — gets bounced to the sign-in page instead of the trace.

---

*Source of truth: `ui/src/` (components), `ui/src/api.ts` (the API contract),
`app/api/ui_routes.py` (backend endpoints). This guide follows the design brief in
`.claude/skills/demo-ui/`.*
