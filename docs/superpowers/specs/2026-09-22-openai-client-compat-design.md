# OpenAI client compatibility: error envelope (B49) and published context limits (B50)

**Status:** design, 2026-09-22.
**Closes:** backlog B49, B50.
**Origin:** both were reported from a live Qwen Code session against the DGX Spark — a real
OpenAI-SDK client talking to RouteForge, which is the only reason we know about either.

## 1. Problem

Two independent defects, both of the same class: **RouteForge knows something useful and does
not tell the client.**

- **B49.** Every non-streamed failure returns FastAPI's default `{"detail": "..."}`. OpenAI SDK
  clients read `error.message` / `error.code`; finding neither, Qwen Code showed the user
  **"API error"** — even when the message said exactly what to do ("request exceeds the model's
  context window … shorten the conversation"). The streamed path already emits an
  `{"error": {...}}`-shaped frame, so the two paths also disagree with each other.
- **B50.** `GET /v1/models` returns `id`, `object`, `created`, `owned_by` and nothing else, so a
  client cannot learn a model's context window and must hand-set it. Too high and the
  conversation overflows (B36's 400); too low and context is wasted. RouteForge already has a
  `context_window` column and the onboarding probe already calls the upstream `/v1/models`,
  where vLLM publishes `max_model_len`. Nothing connects the two.

Neither touches the policy gate, eligibility, or routing. B49 changes only how an
already-decided rejection is *rendered*; B50 adds a field to a listing and fills a column that
today is write-only.

## 2. Non-goals

- No new error *decisions*. The status codes and message texts `_error_status_detail` produces
  today are kept verbatim. This is a rendering change.
- No proactive context-window enforcement. RouteForge still does not compare a prompt against
  `context_window` before dispatch; the upstream's 400 remains the mechanism (B36). B50
  publishes the number, nothing more.
- No background re-probe loop. The probe runs on onboard and on edit, as it does today.
- No change to `/admin/*` or `/api/*` error shapes — the console reads `detail`.

## 3. B49 — the OpenAI error envelope

### 3.1 The shape

Every failure on a `/v1/*` route renders as

```json
{"error": {"message": "...", "type": "invalid_request_error", "param": null, "code": "context_length_exceeded"}}
```

The HTTP status is unchanged. The OpenAI Python SDK reads `code`, `param` and `type` off the
inner `error` object (verified against the installed SDK, 2.46.0:
`APIError.__init__` reads `body.get("code")` where `body` is `data["error"]`), so this is the
shape that makes `exc.code == "context_length_exceeded"` true on the client.

### 3.2 Where it is applied

A single app-level exception handler registered in `create_app()`, guarded by path:

- `request.url.path.startswith("/v1/")` → the envelope.
- anything else → Starlette's default handler, unchanged. `/admin/*`, `/api/*`, `/health` and
  `/metrics` keep `{"detail": ...}`.

FastAPI exception handlers are app-level, not per-router, so the path guard is the mechanism.
`/v1/*` is served by its own prefix-less `APIRouter` (`app/api/routes.py:40`), distinct from the
`/api/*` and `/admin/*` routers, so no other surface is reachable through this handler.

Two handlers are registered:

1. `HTTPException` — covers every `raise HTTPException(...)` on `/v1/*` (auth 401, rate limit
   and budget 429, stream reject 400, unknown `score_name` 422, the mapped domain exceptions,
   and the unmapped 500).
2. `RequestValidationError` — a malformed request body today returns FastAPI's
   `{"detail": [ ... list of pydantic errors ... ]}`, which is the same "API error" experience.
   It renders as the envelope with `type: "invalid_request_error"`, `code: "invalid_request"`,
   the HTTP status left at FastAPI's 422, and a message summarising the first error with its
   field path. **The status is deliberately not changed to OpenAI's 400** — that would be a
   behaviour change beyond rendering, and nothing in the complaint needs it.

### 3.3 One mapping, two renderers

New module `app/api/openai_errors.py`, the single owner of status → `type` and exception →
`code`:

| Raised by | Status | `type` | `code` |
|---|---|---|---|
| `ContextWindowExceeded` | 400 | `invalid_request_error` | `context_length_exceeded` |
| stream rejected (`_STREAM_REJECT`) | 400 | `invalid_request_error` | `streaming_not_supported` |
| auth failure | 401 | `invalid_request_error` | `invalid_api_key` |
| `PipelineRejected`, message starts `requested model` | 422 | `invalid_request_error` | `model_not_found` |
| `PipelineRejected`, verification | 422 | `invalid_request_error` | `response_failed_verification` |
| `NoEligibleCandidate` | 422 | `invalid_request_error` | `no_eligible_model` |
| unknown `score_name` (`/v1/feedback`) | 422 | `invalid_request_error` | `invalid_value` |
| rate limit | 429 | `rate_limit_error` | `rate_limit_exceeded` |
| daily budget | 429 | `insufficient_quota` | `insufficient_quota` |
| unmapped exception | 500 | `server_error` | `internal_error` |
| `BackendUnavailable` | 502 | `server_error` | `backend_unavailable` |
| `PolicyGateError` | 503 | `server_error` | `policy_unavailable` |

`context_length_exceeded`, `invalid_api_key`, `model_not_found`, `rate_limit_exceeded`,
`insufficient_quota` and `invalid_value` are OpenAI's own codes — the ones clients already
branch on. `no_eligible_model`, `response_failed_verification`, `streaming_not_supported`,
`backend_unavailable` and `policy_unavailable` have no OpenAI equivalent and are RouteForge's
own; they are documented as such. A code is never invented to impersonate an OpenAI condition
that did not occur.

Statuses with no specific code fall back to a `type` derived from the status class
(4xx → `invalid_request_error`, 5xx → `server_error`) and `code: null`.

**Carrying a code from a raise site.** `_error_status_detail` maps the domain exceptions and is
extended to return the code alongside status and detail. The inline `HTTPException` raises
(401, 429, 400, 422) carry theirs through a thin `ApiError(HTTPException)` subclass holding
`openai_type` / `openai_code`. The handler uses those when present and falls back to the
status-derived pair otherwise — so an `HTTPException` raised anywhere on `/v1/*`, now or later,
still renders as a valid envelope. Nothing is required to remember to opt in.

### 3.4 The streamed path uses the same mapping

`sse_error_frame` currently sends `{"error": {"message", "type": "routeforge_error", "code":
<int status>}}`. It is changed to take the same `(type, code)` the HTTP path uses, so a failure
renders identically whether or not the response had already started.

**This is a wire change for anyone parsing the streamed frame**: `type` goes from the constant
`routeforge_error` to a real OpenAI type, and `code` goes from an integer status to a string
code. It is the point of the fix — the backlog asks for one mapping precisely so the two paths
cannot drift — and it moves the frame *towards* the OpenAI contract, not away from it. Called
out in the release note.

### 3.5 What must not change

- **Message texts are kept verbatim.** They are already scrubbed of endpoints and PII upstream
  in the gateway (`_scrub_endpoints` then `redact_value`, truncated to 200 chars) before they
  reach `_error_status_detail`. The envelope adds no new text and reads no new source, so it
  cannot widen what leaks. A test asserts a 502's rendered message still carries `<endpoint>`
  and no host or IP.
- **`PolicyGateError` stays opaque.** Its detail is the fixed string "policy evaluation
  unavailable" — deliberately saying nothing about why the gate failed. The envelope carries
  that string and `code: policy_unavailable`, which names the *class* of failure, not its
  cause. Fail-closed behaviour is untouched.
- Status codes, metering, usage-error rows and the `ClientDisconnected` 499 path are untouched.

### 3.6 Test-suite churn

Roughly 17 assertions across 10 test files read `r.json()["detail"]` on a `/v1` response. They
move to the envelope. A `conftest` helper (`v1_error(r) -> dict` returning the inner `error`
object) keeps the change mechanical and gives future tests one way to ask.

**`detail` is not kept alongside `error`.** Two shapes carrying the same message is precisely
the drift class the repo's config lint exists to prevent, and a stale reader would keep working
while the canonical shape moved. One shape, one owner; the test churn is the cost of that and is
paid once.

### 3.7 The consumer-contract test

The real assertion is the one the complaint came from: a genuine OpenAI SDK client. `TestClient`
is an `httpx.Client` subclass (verified), and `openai.OpenAI` accepts `http_client=`, so the SDK
can be pointed at the in-process app with its lifespan running:

```python
oc = openai.OpenAI(base_url="http://testserver/v1", api_key=token, http_client=client)
with pytest.raises(openai.BadRequestError) as ei:
    oc.chat.completions.create(model=..., messages=...)
assert ei.value.code == "context_length_exceeded"
assert "shorten the conversation" in ei.value.body["message"]
```

Note `ei.value.message` is the SDK's own `"Error code: 400 - {...}"` wrapper, not RouteForge's
text — assert against `body["message"]`. If the SDK cannot be driven this way in the test
environment, the fallback is to assert the response JSON shape directly and validate it through
the SDK's parsing of the real response bytes; the fallback is weaker and must be labelled as
such rather than quietly substituted.

## 4. B50 — publish each model's context limit

### 4.1 Capture

`probe_endpoint` already fetches the upstream `GET {endpoint}/models` and reads each entry's
`id`. It also reads `max_model_len` from the entry whose `id` matches the slug, and returns it.
Its return type becomes a small frozen dataclass (`ProbeResult(ok, detail, max_model_len)`)
rather than a third tuple element, so the two call sites read by name.

Accepted only when it is an integer (or an integer-valued float/numeric string) **greater than
zero**. Anything else — missing, null, a string, zero, negative — is `None`. **No default is
ever guessed.** An endpoint that publishes nothing keeps `context_window` unknown, and unknown
is reported as absent, never as a number.

`probe_endpoint` still never raises, and **parsing the context limit can never turn a good probe
into a failed one**. Reachability and context capture are independent: if the slug is among the
served ids, the probe succeeds whatever `max_model_len` says or fails to say.

*Corrected 2026-09-22, before implementation.* An earlier draft of this section claimed a
malformed upstream payload "yields `max_model_len=None`, not a failed probe". That is wrong about
the existing code and would have been wrong to build: a payload whose `data` is not a list of
model objects means RouteForge cannot confirm the slug is served at all, which is exactly the
condition the probe exists to detect, and it correctly fails today. The real invariant is
narrower and is the one tested: **a reachable endpoint that serves the slug but publishes a
missing, null, zero, negative or unparseable `max_model_len` is still a successful probe**, with
the limit left unknown.

### 4.2 Operator value wins, and the source is recorded

A new nullable column `context_window_source` on `onboarded_models`: `"operator"`, `"probed"`,
or NULL when `context_window` is NULL.

On both write paths (`POST /admin/infra/models`, `PUT /admin/infra/models/{slug}`):

- body carries an explicit non-null `context_window` → store it, source `"operator"`. The probed
  value is discarded. An explicit value always wins, and re-probing never overwrites it.
- body omits it or sends null, and the probe returned a value → store the probed value, source
  `"probed"`.
- neither → NULL, source NULL.

This deliberately does **not** copy the omission-preserving treatment used for
`price_cached_input` and `accepts_raw_sensitive`. `context_window` is full-replace on omission
today, the console always resubmits the whole body, and the rule above already gives the
operator the last word on every write that carries their value. Adding a third preservation
semantic would buy nothing and cost a reader's understanding.

**The value is only as fresh as the last onboard or edit.** There is no re-probe loop, so a
model reconfigured upstream with a different `--max-model-len` keeps the old number until an
operator re-saves it. Stated in the guide, not papered over.

### 4.3 Carrying it to the listing

`context_window` is write-only today: it never leaves the DB row. Four links to add —
`_OnboardedRow`, `ModelRegistry.refresh`, `PoolCandidate`, and the onboarded branch of
`effective_pool()` — and then the handler.

`GET /v1/models` gains `max_model_len` on each entry, **omitted entirely when unknown**:

```json
{"id": "nemotron-3.5-lightning", "object": "model", "created": 0, "owned_by": "onprem", "max_model_len": 131072}
```

vLLM's field name, because that is the name clients already read. Static YAML pool entries
(cloud) have no such column and always omit it. Extra keys on a model object are ignored by
OpenAI clients, so this stays a compatible superset.

### 4.4 Database migration

`context_window` already exists on every deployed database — it was part of the original
`onboarded_models` table, which was created whole rather than altered. **`context_window_source`
is new and needs an `ALTER TABLE onboarded_models ADD COLUMN context_window_source VARCHAR(16)`
before the image is rolled**, following the repo's established practice: appended to
`MODEL_COLUMNS` in `scripts/migrate_app_policies.py` and to the migration table in
`docs/reference/per-app-config.md`.

Failure mode if skipped: every onboarded-model read fails, which takes on-prem routing down.
This is the same trap the last two deploys hit, so it goes in the PR description and the release
note, not only in a doc.

### 4.5 Console

The edit form's `context window (optional)` input stays editable. When the stored source is
`"probed"`, the field shows a hint that the value came from the endpoint and that typing one
takes it over. A number RouteForge inferred must not look like one the operator asserted — the
same honesty the P1b data-handling declarations follow (`data_handling_source`).

## 5. Invariants and tests

**B49**
1. Every `/v1/*` failure body parses as `{"error": {message, type, param, code}}`; `param` is
   present and null; the HTTP status is unchanged from today, asserted per status.
2. The OpenAI SDK raises the right exception class with the right `.code` for at least context
   overflow (400), auth (401) and rate limit (429).
3. `/admin/*` and `/api/*` error bodies are still `{"detail": ...}` — a regression test, since
   the console depends on it.
4. The streamed error frame and the non-streamed body carry the same `type` and `code` for the
   same exception. Asserted by driving one exception down both paths.
5. A 502's message still contains `<endpoint>` and no host, IP or URL.
6. `PolicyGateError` still renders exactly "policy evaluation unavailable".
7. An `HTTPException` raised on `/v1/*` with no registered code still renders a valid envelope
   (status-derived `type`, `code: null`).

**B50**
8. A probe against a fake upstream publishing `max_model_len` stores it with source `"probed"`.
9. An operator-supplied `context_window` is not overwritten by a probe that reports a different
   number, on both POST and PUT; source is `"operator"`.
10. A probe whose entry has no `max_model_len`, or a non-numeric or non-positive one, stores
    NULL — asserted per bad-value case, and no default appears anywhere.
11. `GET /v1/models` includes `max_model_len` when known and omits the key when not.
12. A reachable endpoint serving the slug with an unusable `max_model_len` still yields a
    successful probe (`ok=True`, limit unknown). A payload that does not let RouteForge confirm
    the slug is served remains a **failed** probe, as it is today.
13. `probe_endpoint`'s HTTP parsing gets its first direct test (it is stubbed out everywhere
    today), via `httpx.MockTransport`.

## 6. Documentation

- `docs/backlog/bugs.md`: B49 and B50 resolved, with commits.
- `docs/reference/` — the error envelope: the full code table, which codes are OpenAI's and
  which are RouteForge's, and the `/admin` exemption.
- `docs/guides/onboarding-models.md`: `context_window` / `context_window_source` in the field
  table, that the value is captured on onboard and on edit only, and that it is never guessed.
- `docs/reference/per-app-config.md` + `scripts/migrate_app_policies.py`: the new ALTER.
- Release note covering both wire changes: `/v1/*` error bodies, and the streamed frame's
  `type`/`code`.
