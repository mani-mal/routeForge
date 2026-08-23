# RouteForge LangChain sample

This is a standalone LangChain application for testing RouteForge as the model
provider. The use case is a synthetic customer-support investigator:

1. A planning call sees only public ticket data.
2. LangChain tools load a synthetic customer record and current service status.
3. A synthesis call sends those tool results back through RouteForge. This is
   intentional: the customer result contains an email address and phone number,
   so RouteForge can gate newly introduced tool data and enforce residency.
4. A final review call removes unnecessary personal details from the answer.

Each LLM call is independent. The CLI prints the selected model, detected entity
types, `onprem_only` decision, request ID, and cost for every hop, then totals cost
for the investigation.

All records in this directory are fictional and use reserved/example values.

## Install

From this directory, create an isolated environment with `uv`:

```bash
cd test_app
uv sync --extra dev
```

Set the serving URL and the **application token** supplied by your RouteForge
administrator. The URL must include `/v1`.

```bash
export ROUTEFORGE_BASE_URL=http://localhost:8080/v1
export ROUTEFORGE_API_KEY=rf_live_replace_me
```

Do not use an admin session cookie or `X-API-Key`; inference requires
`Authorization: Bearer rf_live_...`.

## Run

```bash
uv run routeforge-investigator TCK-1001 --check-health
```

The second scenario has a degraded service and exercises different evidence:

```bash
uv run routeforge-investigator TCK-1002
```

For the complete RouteForge decision traces and policy metadata:

```bash
uv run routeforge-investigator TCK-1001 --json
```

Expected non-JSON output resembles:

```text
RouteForge calls
  hop=1 request=req-... model=local-8b onprem_only=False entities=none cost=$0.000012
  hop=2 request=req-... model=local-8b onprem_only=True entities=EMAIL_ADDRESS,PHONE_NUMBER cost=$0.000031
  hop=3 request=req-... model=local-8b onprem_only=False entities=none cost=$0.000018
```

The exact models and policy decisions are controlled by your RouteForge
configuration. Sending `model="auto"` does not pin a model.

## Verify without a running RouteForge server

The tests use an in-process fake HTTP transport and make no network calls:

```bash
uv run pytest
```

## Error behavior

- `401`: check or rotate the app token.
- `429`: the adapter retries briefly, then reports the rate/budget limit.
- `503`: treated as a fail-closed policy refusal, not blindly retried.
- Streaming is always disabled because RouteForge verifies completed outputs.

