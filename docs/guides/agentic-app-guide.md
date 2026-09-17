# Building an Agentic Application on routeForge

You're building an agent — a loop that makes several LLM calls, uses tools, and
decides its next step from the last result.

> **routeForge never runs the loop.** It is a model provider and a policy
> chokepoint, not an orchestrator: the loop, the tools, and the control flow all
> belong to your application. (An internal agent runtime existed until
> 2026-09-17; it was removed, and step 4 now always decides `direct`.) Each call
> your loop makes arrives as its own request and is separately gated, routed,
> verified, priced and traced.

---

## Your agent, routeForge as the model provider

Your application owns the agent loop (LangChain, LlamaIndex, your own
orchestrator, any agent framework). You point its LLM client at
routeForge's `/v1` base URL. **Every LLM call your agent makes is an independent
`POST /v1/chat/completions`** — so every step is separately privacy-gated,
routed to the cheapest allowed model, output-verified, priced, and traced.

This is the honest expression of routeForge's core promise: routing and privacy
are **per-call**, not per-conversation. A 4-step agent hits the router 4 times
and may land on 4 different tiers.

```python
from openai import OpenAI

rf = OpenAI(base_url="http://<host>:<port>/v1", api_key="rf_live_...")

def llm(messages):
    resp = rf.chat.completions.create(model="auto", messages=messages)
    raw = resp.model_dump()
    # keep the trace for your own audit / cost tracking
    log_step(request_id=raw["id"], model=raw["model"],
             cost=raw["cost"]["total_usd"], trace=raw["trace"])
    return resp.choices[0].message.content

# your agent loop
history = [{"role": "system", "content": "You are a support agent."}]
history.append({"role": "user", "content": task})
while not done:
    step = llm(history)
    history.append({"role": "assistant", "content": step})
    # ... parse tool calls, run tools, append tool results as user/assistant turns ...
```

What you get and owe:

- **Independent routing per step.** A cheap step goes to the cheap tier; a hard
  step floors to a higher tier by the risk rules. Read `raw["model"]` /
  `raw["cost"]` per call.
- **Correlation is yours.** Each response has its own `id`. Keep your own
  conversation/trace id alongside the per-call `id`s to stitch a session together.
- **Cost accounting is yours to sum.** Each call returns its own `cost`; add them
  up for the task total. (routeForge sums within a single request, not across
  separate requests you make.)
- **Privacy holds automatically.** If a step's content contains PII, that step
  goes `onprem_only` on its own — you don't manage residency per step.
- **Streaming is a per-app capability, off by default.** Unless your admin sets
  your app's `stream_mode`, `stream:true` is rejected and each call returns a
  complete verified response. Agent loops don't need token streaming anyway; they
  need the finished step. (See `stream_mode` in [`../reference/per-app-config.md`](../reference/per-app-config.md).)

### Native tool calling

You do not have to hand-roll tool parsing: routeForge accepts `tools` /
`tool_choice` on the request and returns a standard `tool_calls` assistant turn
with `finish_reason: "tool_calls"`. Your framework's normal tool loop works
unchanged — the wire format and the replay shape are in
[`api-reference.md`](../reference/api-reference.md#tool-calling). Note that a request
carrying `tools` is always served as a single completion and bypasses step-5
compression.

### Tool outputs contain data the input gate never saw

When a tool returns a customer record, feed that tool output back as a message on
the **next** routeForge call — routeForge re-runs the gate on it, so a record a
tool pulled gets detected and its residency enforced on that step. Don't route
tool results around routeForge to a model directly; that bypasses the gate.

---

## Reading and using the decision trace

The `trace` array on every response is your per-call audit and cost feed. A
practical loop:

```python
raw = resp.model_dump()
task_cost = 0.0
for rec in raw["trace"]:
    if rec["step"] == "router":
        task_cost += rec.get("actual_cost_usd") or rec["est_cost_usd"]
        print(f"{rec['chosen']} "
              f"(${rec.get('actual_cost_usd')}, {rec['latency_ms']}ms) — {rec['reason']}")
    elif rec["step"] == "verify" and not rec["passed"]:
        print("verification failed:", rec["reason"])
    elif rec["step"] == "compression" and rec["applied"]:
        print(f"compressed {rec['tokens_before']}→{rec['tokens_after']} "
              f"({rec['reduction_pct']}%)")
```

`policy.detected_entities` (entity **types** only) tells you when a step tripped
the privacy gate; `policy.onprem_only` tells you it was pinned on-prem.

---

## Gotchas

- **The `model` field is a hard pin, not a hint.** `"auto"` lets the router
  choose; any concrete slug pins routing to that exact model, and a slug outside
  your app's permitted/available pool is rejected `422 model request rejected:
  requested model '<slug>' is not permitted or unavailable`. A pin can only narrow
  within your admin's policy — it can't reach a model you're not allowed. For
  agent loops, `"auto"` is usually what you want so each step lands on the
  cheapest allowed tier.
- **Rejections are fail-closed, not crashes — and each has its own status.** No
  candidates (e.g. `onprem_only` with no on-prem model) → `422 no model is
  available…`; all backends failed → `502 no backend could serve this request`;
  the agent step-cap tripped → `500 agent did not converge within its step
  limit`; a privacy-gate error → `503 policy evaluation unavailable`. Distinguish
  them from a real outage before blindly retrying.
- **Bad token is `401`, not `403`.** Handle token rotation on `401`.
- **Inbound streaming is off by default** (per-app `stream_mode`). Build your loop
  around complete responses.
- **The SSE decision stream (`/api/stream`) is admin-only.** Your app can't
  subscribe with its token — use the response `trace` or a Langfuse project for
  programmatic observability.
- **Feed tool outputs back through routeForge**, not around it, so the egress gate
  sees them.

See [`api-reference.md`](../reference/api-reference.md) for the exact schemas and error
table, and [`admin-setup.md`](./admin-setup.md) for what your admin controls.
