# routeForge — Developer & Admin References

This directory is the onboarding path for routing an application's LLM traffic
through routeForge. It has two audiences:

- **Admins** who set up routeForge, create an application, issue its token, and
  set its policy.
- **Developers** who build an application (including an agentic one) that sends
  requests to routeForge and consumes the response.

## What routeForge is (one paragraph)

routeForge is an **OpenAI-compatible model router and policy gateway**. Your
application sends normal chat-completion requests with an API token; for every
call routeForge runs a privacy gate first (PII/PHI detection + redaction + a
residency decision), then routes the call to the cheapest model/backend allowed
by your application's policy, verifies the output, records usage, and returns a
standard `ChatCompletion` **plus a decision trace** describing what it did and
why. Sensitive requests never leave on-prem when policy says so.

## The documents

| Doc | For | Read it to… |
|---|---|---|
| [`admin-setup.md`](./admin-setup.md) | Admin | Create an application, issue/revoke tokens, set routing + privacy policy, wire up per-app observability. |
| [`api-reference.md`](./api-reference.md) | Developer | Know the exact HTTP contract: auth header, request/response schema, every error code, the decision trace. |
| [`agentic-app-guide.md`](./agentic-app-guide.md) | Developer | Build a multi-step / agentic app: how the agent path is triggered, how per-hop routing works, how to read the trace, patterns and gotchas. |

## The two auth domains (read this first)

routeForge has **two completely separate credentials**. Mixing them up is the
most common onboarding mistake.

| Credential | Who holds it | What it opens |
|---|---|---|
| **App token** `rf_live_…` in `Authorization: Bearer` | Your application | `POST /v1/chat/completions`, `GET /health` |
| **Admin session cookie** `rf_admin_session` | A human admin (dashboard) | `/admin/*` management, `/api/*` console (history, stats, the live decision SSE stream) |

An app token **cannot** call `/admin/*` or `/api/*`. An admin cookie is **not**
used for inference. A developer sees their own traffic either through the
`trace` array returned in each response, or through a per-application Langfuse
project the admin provisions — never through the console API.

## Base URL

Your application talks to the **serving** base URL: `http://<host>:<port>/v1`
(default port `8080`). On the DGX lab deploy that is the API NodePort, e.g.
`http://10.89.197.201:30808/v1`. The admin dashboard is served from the UI host.
