---
name: consumer-contract-tests
description: Use before shipping ANY change to the serving path (/v1/chat/completions, the gateway, the policy gate, response shaping) or when a client application reports that RouteForge broke its integration. Codifies the HTTP-boundary regression tests that keep RouteForge a drop-in OpenAI-compatible endpoint for the many different applications that install and call it. Trigger on mentions of OpenAI compatibility, tool calling, a client/consumer app failing, wire shape, over-redaction of real values, or "why didn't a test catch this".
---

# Consumer contract tests

RouteForge is a **product installed at many customer sites** and called by many
different applications through one surface: `POST /v1/chat/completions` with an
`rf_live_…` token. Those apps use the stock `openai` SDK, LangChain `ChatOpenAI`,
PydanticAI, etc. — they assume a **drop-in OpenAI-compatible endpoint**.

Every serving bug that has reached a customer had the same root cause: **no test
played the role of a real client app hitting the HTTP boundary with a realistic
payload.** Unit tests passed because they asserted internal behavior, not the
contract a consumer depends on.

**Never build anything in RouteForge for one named application.** The bug reports
come from specific apps; the fix and its tests are generic. No app name in a
symbol, test id, fixture, comment, or config. The example data may be domain-shaped
(a setup sheet, a support ticket) but the *contract* is what's under test.

## The rule

Before shipping a change to the serving path — `app/api/routes.py`,
`app/pipeline.py`, `app/gateway/**`, `app/policy/gate.py`, `app/models.py` request/
response models — there must be a test that drives the **full `/v1/chat/completions`
stack** (a token, a policy, a recording backend) and asserts the guarantee a
consumer relies on. If you can't point at that test, write it before you ship.

## The two standing contract files

- `tests/test_openai_compat.py` — the **wire shape**: response carries
  `object`/`created` and validates against the real `openai` SDK
  `ChatCompletion` model; a client-driven **tool-calling round-trip** (assistant
  `content:null`+`tool_calls`, a `role:"tool"` result, `finish_reason:"tool_calls"`)
  reaches the backend structured, not flattened. *(Absent → the §4.7/§4.1 misses.)*
- `tests/test_pii_entity_filter.py` — the **data-fidelity contract**: a value the
  app legitimately needs (a business identifier) survives to the model verbatim
  when the app has exempted its entity type, while a protected entity (`US_SSN`)
  never does. *(Absent → the §4.2a over-redaction miss.)*

Extend the matching file; add a new one only for a genuinely new contract dimension.

## What every contract test asserts, and how

Drive the real boundary and record what actually reached the model — do not assert
against the trace's claims:

```python
from app.gateway.backends import litellm_backend
from tests._litellm_fake import CALLS, make_recording_acompletion, make_response

monkeypatch.setattr(litellm_backend, "acompletion",
                    make_recording_acompletion(make_response(content="ok")))
r = client.post("/v1/chat/completions",
                headers={"Authorization": f"Bearer {token}"}, json={...})
wire = "".join(str(m.get("content") or "") for m in CALLS[-1]["messages"])
```

- **Wire shape** — parse the response with the real SDK: `pytest.importorskip(
  "openai.types.chat").ChatCompletion.model_validate(body)`. Anything less lets
  shape drift ship.
- **What reached the model** — assert on `CALLS[-1]` (the recording backend), not
  on `out["trace"]`. The gate's redaction, the gateway's field threading, and the
  message structure are only real if they show up on the wire.
- **Realistic payloads** — a representative real document/turn, not `"hi"`. The
  §4.2a bug only reproduces on structured identifiers (order numbers, dates), which
  a toy prompt never contains.
- **Both directions of a guarantee** — the thing that must pass through AND the
  thing that must not (e.g. denylisted identifier survives *and* SSN still redacted).

## Gotcha — a Protocol/signature change hides as a routing error

The gateway's `except Exception` swallows a `TypeError` from a changed
`InferenceBackend.generate`/`generate_stream` signature and reports it as a backend
*failure*, which failover then surfaces as `NoEligibleCandidate: no candidates
after residency filter` — a misleading routing error, not a signature error. When
you extend that Protocol, update **every** test double: `tests/_litellm_fake.py`
plus each fake `generate` override across the gateway/cache/memory/task-mode tests.
A green `test_openai_compat.py` is the signal the threading actually works.

## When a client app files a bug

1. Reproduce it as a **failing** HTTP-boundary test first (systematic-debugging).
2. Fix the code generically.
3. Keep the test — it is the regression guard, named for the *contract*, not the app.
4. If it's a new contract dimension, note it in the relevant file's module docstring
   so the next person sees the shape of guarantee being protected.
