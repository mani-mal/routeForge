# Building an Agentic Application on routeForge

You're building an agent — a loop that makes several LLM calls, uses tools, and
decides its next step from the last result. There are two ways an agent and
routeForge relate. Pick the first unless you specifically want routeForge to run
the loop for you.

---

## Pattern A (recommended): your agent, routeForge as the model provider

Your application owns the agent loop (LangChain, LlamaIndex, your own
orchestrator, NeMo Agent Toolkit — anything). You point its LLM client at
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

What you get and owe with Pattern A:

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
- **No streaming.** Each call returns a complete verified response (`stream:true`
  is rejected). Agent loops don't need token streaming; they need the finished
  step.

### Tool outputs contain data the input gate never saw

When a tool returns a customer record, feed that tool output back as a message on
the **next** routeForge call — routeForge re-runs the gate on it, so a record a
tool pulled gets detected and its residency enforced on that step. Don't route
tool results around routeForge to a model directly; that bypasses the gate.

---

## Pattern B: let routeForge run a multi-step workflow

routeForge has a built-in multi-step path (its step-4 task-mode router → the NeMo
Agent Toolkit runtime). One request fans out into several internal hops, each of
which re-enters routing and compression and emits its own `agent_hop`
`DecisionRecord`; you get one response back covering the whole workflow.

**How it's triggered** — automatically, from the request itself. No first-class
API flag. A request goes multi-step when **either**:

1. `mock_multistep: true` is present on the body (an explicit demo hint), **or**
2. the last **user** message contains a trigger phrase — `"step by step"` or
   `"look up"`.

Otherwise it's a single direct call. (The LLM task classifier is off in this
build; these are rules only.)

```bash
curl http://<host>:<port>/v1/chat/completions \
  -H "Authorization: Bearer rf_live_..." -H "Content-Type: application/json" \
  -d '{"model":"auto","mock_multistep":true,
       "messages":[{"role":"user","content":"Look up the order and refund it step by step"}]}'
```

**The response** has the same top-level schema as a direct call, aggregated:

- `model` = the **last** hop's chosen slug.
- `usage.prompt_tokens` = sum of per-hop post-compression tokens;
  `usage.completion_tokens` = tokens of the final answer.
- `cost` = summed per-hop actual vs baseline (routing and compression levers kept
  separate).
- `trace[]` = one `DecisionRecord` per hop (`step:"agent_hop"`, with `hop_index`,
  `chosen`, `actual_cost_usd`, `latency_ms`, `tokens_priced`, `serving_engine`),
  interleaved `CompressionRecord`s, and a final `VerificationRecord`. This is how
  you read per-hop cost/latency/model out of one response.

**Limits.** The workflow is capped by the server's `agent_budgets.max_steps`
(default 5). Exceeding it fails closed → `503 policy evaluation unavailable`. The
tool set and behavior of the built-in workflow are fixed by routeForge, not
supplied by you — which is why Pattern B is mostly a demo/showcase of per-call
routing, not a place to put *your* tools.

---

## Which to choose

| Want… | Use |
|---|---|
| Your own tools, your own control flow, production agent | **Pattern A** |
| Each of your agent's steps individually gated, routed, priced | **Pattern A** |
| A one-shot showcase of routeForge fanning a task into routed hops | Pattern B |
| To keep routeForge a stateless model provider | Pattern A |

For the application you're building — an agent with your own tools — **Pattern A**
is the fit. Point your framework's LLM client at routeForge and let every model
call flow through it.

---

## Reading and using the decision trace

Whichever pattern, the `trace` array is your per-step audit and cost feed. A
practical loop:

```python
raw = resp.model_dump()
task_cost = 0.0
for rec in raw["trace"]:
    if rec["step"] in ("router", "agent_hop"):
        task_cost += rec.get("actual_cost_usd") or rec["est_cost_usd"]
        print(f"hop {rec['hop_index']}: {rec['chosen']} "
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

- **Don't rely on the `model` field to pick a model.** It's ignored for routing.
  Your reachable models are set by your admin's policy, not your request.
- **`503` is fail-closed, not a crash.** No candidates (e.g. `onprem_only` with no
  local backend), gate error, all tiers failed, or the agent step-cap tripped all
  surface as `503 policy evaluation unavailable`. Distinguish it from a real
  outage before blindly retrying.
- **Bad token is `401`, not `403`.** Handle token rotation on `401`.
- **No inbound streaming.** Build your loop around complete responses.
- **The SSE decision stream (`/api/stream`) is admin-only.** Your app can't
  subscribe with its token — use the response `trace` or a Langfuse project for
  programmatic observability.
- **Feed tool outputs back through routeForge**, not around it, so the egress gate
  sees them.

See [`api-reference.md`](./api-reference.md) for the exact schemas and error
table, and [`admin-setup.md`](./admin-setup.md) for what your admin controls.
