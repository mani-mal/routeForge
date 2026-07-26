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
- **No inbound streaming:** `stream: true` is rejected — the output-verification step
  needs the complete response. (The dashboard's live decision stream is separate.)
- **Errors:** `401` invalid/missing/revoked token; `429` rate/budget exceeded; `400`
  streaming requested; `422` response failed output verification.

## 5. Seeing your traffic (observability)

If the admin has enabled per-application Langfuse projects, your application gets its
**own Langfuse project**; ask your admin for access and you'll see only your own
traces — model chosen, cost, latency, and entity *types* detected (never raw PII).
Otherwise traces land in the shared project.
