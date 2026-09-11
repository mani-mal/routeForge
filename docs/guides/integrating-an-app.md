# Integrating an application with routeForge

routeForge is a **model router**: your applications send OpenAI-compatible chat
requests with an API token, and routeForge decides which model/backend serves each
one, keeps sensitive requests on-prem, enforces per-application policy and quotas,
and records usage. This guide is the whole integration path.

> Coming from the demo? The **traffic generator** (`tests/traffic/`) and its
> **personas** are a *load/seed tool* used to exercise the UI — not the product's
> user model. Real applications integrate as below.

## 1. The admin issues you a token

A routeForge **admin** creates an application for you in the dashboard (**Admin →
Applications → Create application**) and issues an **API token** (shown once, e.g.
`rf_live_…`). The admin also sets your application's policy: which model tiers and
providers you may use, residency (on-prem vs cloud), and any rate/budget caps.

Keep the token secret. If it leaks, the admin revokes it and issues a new one — no
code change on your side beyond swapping the token.

## 2. Your app calls routeForge like OpenAI

routeForge exposes `POST /v1/chat/completions`. Point any OpenAI-compatible client at
the routeForge base URL and pass your token as the API key:

```python
from openai import OpenAI

client = OpenAI(base_url="http://<host>:<port>/v1", api_key="rf_live_...")

resp = client.chat.completions.create(
    model="auto",   # let the router choose; or name a model you're allowed to use
    messages=[{"role": "user", "content": "Summarize this ticket: ..."}],
)
print(resp.choices[0].message.content)
```

`curl` works the same way:

```bash
curl http://<host>:<port>/v1/chat/completions \
  -H "Authorization: Bearer rf_live_..." \
  -H "Content-Type: application/json" \
  -d '{"model":"auto","messages":[{"role":"user","content":"hello"}]}'
```

A runnable example is in [`examples/client/chat.py`](../../examples/client/chat.py).

## 3. What routeForge does with your request

1. **Authenticates** the token → your application (unknown/revoked → `401`).
2. **Enforces your quotas** — over the rate limit or daily budget → `429`.
3. **Runs the privacy gate first** — detects/redacts PII/PHI and sets residency.
4. **Routes** to the cheapest allowed model/tier for the request, within your
   application's allowed providers/tiers. `onprem_only` requests never leave on-prem.
5. **Returns** a normal OpenAI `ChatCompletion` and **records usage** under your app.

## 4. Notes & limits

- **`model` field:** send `"auto"` to let the router choose, or a specific model your
  application is permitted to use. A model you're not allowed to use is rejected.
- **Inbound streaming is per-app opt-in:** by default `stream: true` is **rejected**
  (the output-verification step needs the complete response), and it stays rejected for
  any app with no `stream_mode` set. An admin can opt your app into streaming via
  `AppPolicy.stream_mode` (`verified_then_stream | stream_unverified |
  incremental_verify`); the `onprem_only` / `require_verification` floor still forces the
  fully-verified path regardless. See [`../reference/per-app-config.md`](../reference/per-app-config.md). (The
  dashboard's live decision stream is a separate channel and always on.)
- **Errors:** `401` invalid/missing/revoked token; `429` rate/budget exceeded; `400`
  streaming requested by an app not opted into a `stream_mode`; `422` response failed
  output verification.

## 5. Seeing your traffic (observability)

If the admin has enabled per-application Langfuse projects, your application gets its
**own Langfuse project**; ask your admin for access and you'll see only your own
traces — model chosen, cost, latency, and entity *types* detected (never raw PII).
Otherwise traces land in the shared project.

## 6. Building an agentic app on top of routeForge

routeForge is an OpenAI-compatible model router, so any agent framework that speaks the
OpenAI API (pydantic-ai, LangChain, etc.) can use it as its model backend. The
recommended shape is **harness in your app, routing/policy/observability in routeForge**:
your app owns the tools, state, and the agent loop, and points its model client at
routeForge's base URL.

```python
from pydantic_ai import Agent
from pydantic_ai.models.openai import OpenAIModel
from pydantic_ai.providers.openai import OpenAIProvider

model = OpenAIModel(
    "auto",  # let routeForge's router choose per call
    provider=OpenAIProvider(base_url="http://<host>:<port>/v1", api_key="rf_live_..."),
)
agent = Agent(model, tools=[...])  # your harness owns the tools + loop
```

Four things to get right when the harness lives in your app:

1. **routeForge sees N independent calls, not one trajectory.** Each turn of your agent
   loop is a *separate* `/v1/chat/completions` request — its own privacy gate, routing
   decision, cost, and trace. routeForge's own multi-step path (step-4 task-mode router,
   NAT agent runtime, `agent_budgets`) does **not** engage; that only runs when you hand
   routeForge a single request and let it drive the loop. To reconstruct the trajectory,
   use the session header (§7).
2. **Streaming is off unless your app opts in.** By default `stream: true` is rejected
   (output verification needs the complete response), so configure your model client to
   disable streaming or every call fails with `400`. If your workload needs streaming,
   ask your admin to set a `stream_mode` on the app — subject to the verification floor
   (see §4 and [`../reference/per-app-config.md`](../reference/per-app-config.md)).
3. **Tool calls pass through the gateway.** `tools` / `tool_choice` in, `tool_calls`
   out, unchanged — this is what makes a tool-using agent work. If you rely on
   tool-calling, validate the round-trip against your onboarded model first.
4. **Per-app policy applies to *every* call.** Your token carries your policy (allowed
   models, residency, budget), enforced on each turn. Size the app's budget/residency
   for the whole agent workload, not a single call — an `onprem_only` app keeps *every*
   turn on-prem.

## 7. Collecting traces for eval / RLHF (external consumer)

Every request emits one `serving.request` trace with these always-on attributes (they
are not PII and are present regardless of the app's `capture_io` mode):

| Attribute | Meaning |
|---|---|
| `sp.request_id` | correlates to the DecisionRecord / console |
| `sp.application_id`, `sp.tenant_id` | which app/tenant (falls back to tenant if unset) |
| `sp.model` | model chosen |
| `sp.cost_total_usd`, `sp.cost_baseline_usd` | actual cost vs all-frontier baseline |
| `sp.sensitivity`, `sp.onprem_only` | policy verdict |
| `sp.detected_entities` | entity **types** only (e.g. `US_SSN`), never values |
| `sp.routing_policy_version` | routing+pricing content hash at decision time |
| `sp.app_policy_version` | content hash of the per-app policy in force (moves on every policy edit — pair it with a conformance-probe result to tell a stale PASS from a live one) |
| `sp.policy_mode` | the app's protection posture (`enforced` \| `passthrough`), emitted whenever it resolves |
| `langfuse.session.id` | groups turns into one Langfuse **Session** (see below) |

When routeForge runs the **multi-step path**, per-hop **child spans** `serving.step-N`
carry `sp.chosen`, `sp.reason`, `sp.candidates`, `sp.hop_index`, `sp.actual_cost_usd`,
and `sp.latency_ms`.

**Reconstructing an app-side multi-agent run.** When your app owns the harness, a run is
N separate top-level traces, not one trace with hop-spans. Tie them together by sending a
stable session id on every call:

```
x-routeforge-session: <run-id>
```

routeForge stamps it as `langfuse.session.id`, and Langfuse groups all N turns into one
Session your consumer walks as the trajectory. Header rules (fail-closed — an invalid
value is silently dropped, not rejected): **max 128 chars**, charset **`[A-Za-z0-9._:-]`**
(letters, digits, `. _ : -`); leading/trailing whitespace is stripped. A UUID, ULID, or
`run:<uuid>` all pass.

**Raw text vs. metadata.** All attributes above are **always on** — enough for
reward/routing eval. Raw prompt/completion (`input.value` / `output.value`) exist **only**
when the app's `capture_io = full`, which requires the app's **own** provisioned Langfuse
project (never relaxed — raw I/O is tenant-isolated) and, for an `onprem_only` app, an
explicitly declared `policy_mode: "passthrough"`. See
[`../reference/per-app-config.md`](../reference/per-app-config.md). So SFT/DPO `{prompt, chosen, rejected}`
pairs come only from `full`-eligible apps; scores and routing signal come from everywhere.
