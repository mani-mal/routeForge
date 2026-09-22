# OpenAI Client Compatibility (B49 + B50) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Render every `/v1/*` failure in OpenAI's `{"error": {...}}` envelope (B49), and capture
and publish each onboarded model's real context limit (B50), so OpenAI-SDK clients stop showing
a bare "API error" and stop hand-setting context windows.

**Architecture:** One new module owns the status→`type` / exception→`code` mapping and is used by
both the HTTP error handler and the SSE error frame, so the two paths cannot drift. Separately,
the onboarding probe — which already calls the upstream `/v1/models` — also reads
`max_model_len`, stores it on the existing `context_window` column with a new
`context_window_source` provenance column, and the registry carries it out to `GET /v1/models`.

**Tech Stack:** Python 3.12, FastAPI, Pydantic v2, SQLAlchemy (async), pytest, `openai` SDK
(2.46.0, already a dev dependency), `httpx.MockTransport`.

**Spec:** `docs/superpowers/specs/2026-09-22-openai-client-compat-design.md` — read it first;
it carries the reasoning behind every decision below, and it is the binding authority where this
plan is silent or wrong.

## Global Constraints

- **Run pytest with `LITELLM_LOCAL_MODEL_COST_MAP=True`** or the suite wedges in D-state on a
  litellm cost-map fetch. Every test command in this plan already includes it.
- `tests/test_pii_detection_pipeline.py` fails to import in this worktree (the venv lacks the
  `presidio` extra). That is **pre-existing** — always run with
  `--ignore=tests/test_pii_detection_pipeline.py` and never "fix" it.
- **No secrets in code, tests or fixtures.** Use `example.com` hosts and obviously fake tokens.
- **No PII in any new test payload.** If a test needs a PII-shaped string, use the reserved
  synthetic SSNs `078-05-1120` / `219-09-9999` and `example.com` emails.
- **Never guess a context window.** An unknown value is NULL and is omitted from the response —
  never a default, never zero.
- **Message texts are kept verbatim.** This work renders existing errors differently; it does not
  reword them or change any HTTP status.
- **`/admin/*` and `/api/*` keep `{"detail": ...}`.** The console reads it.
- Type-hint everything; Pydantic models at boundaries.
- Prefer boring, readable code — customers' engineers read this.
- Commit after each task with a conventional-commit subject.

---

### Task 1: The error mapping module

**Files:**
- Create: `app/api/openai_errors.py`
- Test: `tests/test_openai_errors_unit.py`

**Interfaces:**
- Consumes: nothing (pure module, no app imports beyond `fastapi.HTTPException`).
- Produces, for Tasks 2–3:
  - `ApiError(HTTPException)` with `__init__(self, status_code: int, detail: str, *, code: str | None = None, type: str | None = None)`, storing `self.openai_code` / `self.openai_type`.
  - `error_type_for_status(status: int) -> str`
  - `error_payload(status: int, message: str, *, code: str | None = None, type: str | None = None) -> dict` → `{"error": {"message", "type", "param", "code"}}`
  - Constants: `CODE_CONTEXT_LENGTH_EXCEEDED = "context_length_exceeded"`, `CODE_INVALID_API_KEY = "invalid_api_key"`, `CODE_MODEL_NOT_FOUND = "model_not_found"`, `CODE_RATE_LIMIT_EXCEEDED = "rate_limit_exceeded"`, `CODE_INSUFFICIENT_QUOTA = "insufficient_quota"`, `CODE_INVALID_VALUE = "invalid_value"`, `CODE_NO_ELIGIBLE_MODEL = "no_eligible_model"`, `CODE_RESPONSE_FAILED_VERIFICATION = "response_failed_verification"`, `CODE_STREAMING_NOT_SUPPORTED = "streaming_not_supported"`, `CODE_BACKEND_UNAVAILABLE = "backend_unavailable"`, `CODE_POLICY_UNAVAILABLE = "policy_unavailable"`, `CODE_INTERNAL_ERROR = "internal_error"`.
  - `TYPE_INVALID_REQUEST = "invalid_request_error"`, `TYPE_RATE_LIMIT = "rate_limit_error"`, `TYPE_INSUFFICIENT_QUOTA = "insufficient_quota"`, `TYPE_SERVER = "server_error"`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_openai_errors_unit.py`:

```python
"""Unit tests for the OpenAI error envelope mapping (B49, design §3.3)."""
import pytest

from app.api.openai_errors import (
    ApiError, TYPE_INVALID_REQUEST, TYPE_SERVER, TYPE_RATE_LIMIT,
    error_payload, error_type_for_status,
)


@pytest.mark.parametrize("status,expected", [
    (400, TYPE_INVALID_REQUEST), (401, TYPE_INVALID_REQUEST),
    (404, TYPE_INVALID_REQUEST), (422, TYPE_INVALID_REQUEST),
    (429, TYPE_RATE_LIMIT),
    (500, TYPE_SERVER), (502, TYPE_SERVER), (503, TYPE_SERVER),
])
def test_type_derived_from_status(status: int, expected: str) -> None:
    assert error_type_for_status(status) == expected


def test_payload_shape_has_all_four_openai_keys() -> None:
    body = error_payload(400, "too long", code="context_length_exceeded")
    assert set(body) == {"error"}
    err = body["error"]
    assert err == {"message": "too long", "type": TYPE_INVALID_REQUEST,
                   "param": None, "code": "context_length_exceeded"}


def test_payload_without_code_still_valid() -> None:
    err = error_payload(418, "teapot")["error"]
    assert err["code"] is None and err["param"] is None
    assert err["message"] == "teapot"
    assert isinstance(err["type"], str) and err["type"]


def test_explicit_type_overrides_status_default() -> None:
    err = error_payload(429, "no funds", code="insufficient_quota",
                        type="insufficient_quota")["error"]
    assert err["type"] == "insufficient_quota"


def test_api_error_carries_code_and_type() -> None:
    exc = ApiError(401, "invalid or missing API token", code="invalid_api_key")
    assert exc.status_code == 401
    assert exc.detail == "invalid or missing API token"
    assert exc.openai_code == "invalid_api_key"
    assert exc.openai_type is None  # falls back to the status-derived type


def test_api_error_is_an_http_exception() -> None:
    from fastapi import HTTPException
    assert isinstance(ApiError(429, "slow down"), HTTPException)
```

- [ ] **Step 2: Run it to verify it fails**

Run: `LITELLM_LOCAL_MODEL_COST_MAP=True .venv/bin/python -m pytest tests/test_openai_errors_unit.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.api.openai_errors'`

- [ ] **Step 3: Write the module**

Create `app/api/openai_errors.py`:

```python
"""The OpenAI error envelope for `/v1/*` (backlog B49).

One owner of the status -> `type` and exception -> `code` mapping, used by
both the HTTP exception handler (`app/main.py`) and the SSE error frame
(`app/api/streaming.py`), so a streamed and a non-streamed failure can never
describe the same condition differently.

Codes come in two kinds, and the difference is deliberate:

* OpenAI's own codes -- `context_length_exceeded`, `invalid_api_key`,
  `model_not_found`, `rate_limit_exceeded`, `insufficient_quota`,
  `invalid_value` -- which clients already branch on.
* RouteForge's own -- `no_eligible_model`, `response_failed_verification`,
  `streaming_not_supported`, `backend_unavailable`, `policy_unavailable` --
  for conditions OpenAI has no equivalent for.

A code is never invented to impersonate an OpenAI condition that did not
occur: a policy rejection is not dressed up as a quota error.
"""
from __future__ import annotations

from fastapi import HTTPException

TYPE_INVALID_REQUEST = "invalid_request_error"
TYPE_RATE_LIMIT = "rate_limit_error"
TYPE_INSUFFICIENT_QUOTA = "insufficient_quota"
TYPE_SERVER = "server_error"

# OpenAI's codes.
CODE_CONTEXT_LENGTH_EXCEEDED = "context_length_exceeded"
CODE_INVALID_API_KEY = "invalid_api_key"
CODE_MODEL_NOT_FOUND = "model_not_found"
CODE_RATE_LIMIT_EXCEEDED = "rate_limit_exceeded"
CODE_INSUFFICIENT_QUOTA = "insufficient_quota"
CODE_INVALID_VALUE = "invalid_value"
# RouteForge's own.
CODE_NO_ELIGIBLE_MODEL = "no_eligible_model"
CODE_RESPONSE_FAILED_VERIFICATION = "response_failed_verification"
CODE_STREAMING_NOT_SUPPORTED = "streaming_not_supported"
CODE_BACKEND_UNAVAILABLE = "backend_unavailable"
CODE_POLICY_UNAVAILABLE = "policy_unavailable"
CODE_INTERNAL_ERROR = "internal_error"


def error_type_for_status(status: int) -> str:
    """The OpenAI `error.type` for a status with no more specific mapping.

    429 is a rate-limit error unless the raiser says otherwise (a budget
    rejection passes `type=TYPE_INSUFFICIENT_QUOTA` explicitly); everything
    else splits on the status class.
    """
    if status == 429:
        return TYPE_RATE_LIMIT
    if status >= 500:
        return TYPE_SERVER
    return TYPE_INVALID_REQUEST


def error_payload(status: int, message: str, *, code: str | None = None,
                  type: str | None = None) -> dict:
    """The response body for a `/v1/*` failure.

    `param` is always present and null: the OpenAI SDK reads it off the inner
    `error` object, and an absent key and a null key are not the same thing to
    every client. RouteForge never attributes a failure to one request field,
    so it is never populated.
    """
    return {"error": {"message": message,
                      "type": type or error_type_for_status(status),
                      "param": None,
                      "code": code}}


class ApiError(HTTPException):
    """An `HTTPException` that also names its OpenAI `code`/`type`.

    Raised in place of a plain `HTTPException` at `/v1/*` sites whose code
    cannot be derived from the status alone -- 429 means rate limit in one
    place and exhausted budget in another. A plain `HTTPException` raised on
    `/v1/*` still renders a valid envelope (status-derived type, null code),
    so nothing has to remember to opt in.
    """

    def __init__(self, status_code: int, detail: str, *,
                 code: str | None = None, type: str | None = None) -> None:
        super().__init__(status_code, detail)
        self.openai_code = code
        self.openai_type = type
```

- [ ] **Step 4: Run the tests**

Run: `LITELLM_LOCAL_MODEL_COST_MAP=True .venv/bin/python -m pytest tests/test_openai_errors_unit.py -q`
Expected: PASS (6 tests)

- [ ] **Step 5: Commit**

```bash
git add app/api/openai_errors.py tests/test_openai_errors_unit.py
git commit -m "feat(api): OpenAI error code/type mapping module (B49)"
```

---

### Task 2: Wire the envelope into the HTTP and SSE paths

**Files:**
- Modify: `app/main.py` (register handlers in `create_app`, near `app.include_router` at ~:337)
- Modify: `app/api/routes.py` (`_error_status_detail` ~:206-225; inline raises at :177, :202, :356, :357, :366, :387, :506, :514, :1284, :1287; the SSE call at ~:964)
- Modify: `app/api/streaming.py` (`sse_error_frame` ~:32-37)
- Modify: `tests/conftest.py` (add the `v1_error` helper)
- Modify (assertion updates only): `tests/test_streaming.py:157`, `tests/test_error_metering.py:240`, `tests/test_rejection_messages.py:8`, `tests/test_context_window_overflow.py:43`, `tests/onboarding/test_rejection_points.py:26,34,49,50`, `tests/test_error_taxonomy.py:15,26,37`, `tests/test_stream_keepalive.py:75`, `tests/test_stream_unverified.py:193`, `tests/test_per_request_cap.py:37`

**Interfaces:**
- Consumes from Task 1: `ApiError`, `error_payload`, and every `CODE_*` / `TYPE_*` constant.
- Produces for Task 3:
  - `_error_status_detail(exc) -> tuple[int, str, str | None] | None` — now `(status, detail, code)`.
  - `sse_error_frame(status: int, detail: str, code: str | None = None, type: str | None = None) -> str`
  - `tests/conftest.py::v1_error(response) -> dict` returning the inner `error` object.

**Important:** `tests/test_per_request_cap.py:37` and `tests/test_policy_save_validation.py` assert
on **`/admin/*`** responses — check each file's URL before editing. Only `/v1/*` assertions move to
the envelope; `/admin/*` ones must stay on `detail` and are a deliberate regression guard.

- [ ] **Step 1: Write the failing integration test**

Add to `tests/test_error_taxonomy.py` (it already drives each domain exception to its status):

```python
def test_v1_errors_use_the_openai_envelope(client, monkeypatch) -> None:
    """Every /v1 failure body is {"error": {message, type, param, code}} (B49)."""
    r = client.post("/v1/chat/completions",
                    headers={"Authorization": "Bearer rf_live_nope"},
                    json={"model": "any", "messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code == 401
    body = r.json()
    assert "detail" not in body
    err = body["error"]
    assert set(err) == {"message", "type", "param", "code"}
    assert err["code"] == "invalid_api_key"
    assert err["type"] == "invalid_request_error"
    assert err["param"] is None
    assert err["message"] == "invalid or missing API token"


def test_admin_errors_keep_the_detail_shape(client) -> None:
    """The console reads `detail`; /admin is deliberately not converted."""
    r = client.get("/admin/infra/models")
    assert r.status_code in (401, 403)
    assert "detail" in r.json() and "error" not in r.json()
```

- [ ] **Step 2: Run it to verify it fails**

Run: `LITELLM_LOCAL_MODEL_COST_MAP=True .venv/bin/python -m pytest tests/test_error_taxonomy.py -q`
Expected: FAIL on the first test — `KeyError: 'error'` (body is still `{"detail": ...}`).

If `test_admin_errors_keep_the_detail_shape` does not get a 401/403 (e.g. the admin route is
open in the test config), replace it with any `/admin/*` request that does fail, and say so in
the report. The point is the shape, not the status.

- [ ] **Step 3: Add the `v1_error` test helper**

In `tests/conftest.py`, add:

```python
def v1_error(response) -> dict:
    """The inner `error` object of a /v1 failure body (B49 envelope).

    One place for tests to ask, so the next envelope change is one edit.
    """
    body = response.json()
    assert "error" in body, f"not an OpenAI error envelope: {body!r}"
    return body["error"]
```

Export it for tests that import it directly (`from tests.conftest import v1_error`).

- [ ] **Step 4: Extend `_error_status_detail` to carry codes**

In `app/api/routes.py`, import the constants and rewrite the function to return a 3-tuple.
Keep every message string **byte-for-byte as it is today**:

```python
def _error_status_detail(exc: BaseException) -> tuple[int, str, str | None] | None:
    """The (status, detail, openai_code) the HTTP path returns for a serving
    exception. One mapping, used by the plain HTTP path and by the keepalive
    stream's error frame, so the two can never drift -- including the code the
    client branches on (B49)."""
    if isinstance(exc, PipelineRejected):
        message = str(exc)
        rejected_model = message.startswith("requested model")
        prefix = ("model request rejected: " if rejected_model
                  else "response failed verification: ")
        code = (CODE_MODEL_NOT_FOUND if rejected_model
                else CODE_RESPONSE_FAILED_VERIFICATION)
        return 422, f"{prefix}{message}", code
    if isinstance(exc, ContextWindowExceeded):
        return 400, (f"request exceeds the model's context window: {exc} -- "
                     "shorten the conversation (compact or summarise the history) "
                     "or lower max_tokens"), CODE_CONTEXT_LENGTH_EXCEEDED
    if isinstance(exc, NoEligibleCandidate):
        return (422, f"no model is available for this request's policy: {exc}",
                CODE_NO_ELIGIBLE_MODEL)
    if isinstance(exc, BackendUnavailable):
        return (502, f"no backend could serve this request: {exc}",
                CODE_BACKEND_UNAVAILABLE)
    if isinstance(exc, PolicyGateError):
        return 503, "policy evaluation unavailable", CODE_POLICY_UNAVAILABLE
    return None
```

Update both call sites:
- `routes.py:~501-514` (`chat_completions`): the unmapped branch becomes
  `raise ApiError(500, "internal error", code=CODE_INTERNAL_ERROR)`; the mapped branch becomes
  `raise ApiError(mapped[0], mapped[1], code=mapped[2])`. The `mapped[0] >= 500` warning log and
  the `_record_failed` call are unchanged — note `mapped` is now a 3-tuple, so any positional
  unpack must be updated.
- `routes.py:~947-964` (`_keepalive_stream`): the `mapped is None` default becomes
  `(500, "internal error", CODE_INTERNAL_ERROR)`, and `yield sse_error_frame(*mapped)` still
  works positionally once `sse_error_frame` takes `(status, detail, code)` — verify, don't assume.

- [ ] **Step 5: Convert the inline raises**

Replace each `/v1/*` `HTTPException` with an `ApiError` carrying its code. **Leave
`routes.py:164` (`/metrics`, 503) as a plain `HTTPException`** — it is not a `/v1` route.

| Line (pre-edit) | Replacement |
|---|---|
| `:177` | `ApiError(401, "invalid or missing API token", code=CODE_INVALID_API_KEY)` |
| `:202` | `ApiError(400, _STREAM_REJECT, code=CODE_STREAMING_NOT_SUPPORTED)` |
| `:356` | `ApiError(400, _STREAM_REJECT, code=CODE_STREAMING_NOT_SUPPORTED)` |
| `:357` | `ApiError(401, "invalid or missing API token", code=CODE_INVALID_API_KEY)` |
| `:366` | `ApiError(429, "rate limit exceeded for this application", code=CODE_RATE_LIMIT_EXCEEDED)` |
| `:387` | `ApiError(429, "daily budget exceeded for this application", code=CODE_INSUFFICIENT_QUOTA, type=TYPE_INSUFFICIENT_QUOTA)` |
| `:1284` | `ApiError(401, "invalid or missing API token", code=CODE_INVALID_API_KEY)` |
| `:1287` | `ApiError(422, f"unknown score_name: {body.score_name!r}", code=CODE_INVALID_VALUE)` |

Note `except HTTPException: raise` at `routes.py:~494-495` still works: `ApiError` is an
`HTTPException`, so a re-raise passes it through with its code intact. Verify that line is
unchanged and still re-raises.

- [ ] **Step 6: Update `sse_error_frame`**

In `app/api/streaming.py`:

```python
def sse_error_frame(status: int, detail: str, code: str | None = None,
                    type: str | None = None) -> str:
    """A failure after the SSE response already started (status 200 is on the
    wire). OpenAI SDK stream parsers raise APIError on a data frame carrying an
    `error` object.

    Shares `error_payload` with the non-streamed path (B49), so the same
    exception describes itself identically whether or not the response had
    already started. `type`/`code` used to be the constant "routeforge_error"
    and the integer HTTP status; they are now the same OpenAI values the HTTP
    body carries.
    """
    from app.api.openai_errors import error_payload
    return sse_frame(error_payload(status, detail, code=code, type=type))
```

Prefer a module-level import if it creates no cycle — check; `openai_errors` imports only
`fastapi`, so it should not.

- [ ] **Step 7: Register the handlers**

In `app/main.py::create_app`, after the `include_router` calls (~:339) and before
`add_middleware`:

```python
    # B49: OpenAI-shaped errors on /v1/* only. /admin/* and /api/* keep
    # FastAPI's {"detail": ...} -- the console reads it. Handlers are
    # app-level in FastAPI, so the surface is selected by path here.
    @app.exception_handler(StarletteHTTPException)
    async def _http_exception_handler(request: Request, exc: StarletteHTTPException):
        if not request.url.path.startswith("/v1/"):
            return await http_exception_handler(request, exc)
        return JSONResponse(
            status_code=exc.status_code,
            content=error_payload(exc.status_code, str(exc.detail),
                                  code=getattr(exc, "openai_code", None),
                                  type=getattr(exc, "openai_type", None)),
            headers=getattr(exc, "headers", None))

    @app.exception_handler(RequestValidationError)
    async def _validation_handler(request: Request, exc: RequestValidationError):
        if not request.url.path.startswith("/v1/"):
            return await request_validation_exception_handler(request, exc)
        first = exc.errors()[0] if exc.errors() else {}
        loc = ".".join(str(p) for p in first.get("loc", ()) if p != "body")
        message = first.get("msg", "invalid request body")
        return JSONResponse(
            status_code=422,
            content=error_payload(422, f"{loc}: {message}" if loc else message,
                                  code="invalid_request"))
```

Imports needed at the top of `app/main.py`:

```python
from fastapi import Request
from fastapi.exception_handlers import (
    http_exception_handler, request_validation_exception_handler)
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.api.openai_errors import error_payload
```

Register against **`StarletteHTTPException`**, not `fastapi.HTTPException` — Starlette's default
handler is keyed on the former, and FastAPI's `HTTPException` subclasses it. Registering the
subclass alone misses 404s and other Starlette-raised errors.

- [ ] **Step 8: Run the new tests**

Run: `LITELLM_LOCAL_MODEL_COST_MAP=True .venv/bin/python -m pytest tests/test_error_taxonomy.py tests/test_openai_errors_unit.py -q`
Expected: the two new tests PASS; the three pre-existing `["detail"]` assertions in
`test_error_taxonomy.py` now FAIL — that is the expected churn, fixed in the next step.

- [ ] **Step 9: Migrate the `/v1` assertions**

Run the whole suite to find every break:

```bash
LITELLM_LOCAL_MODEL_COST_MAP=True .venv/bin/python -m pytest -q \
  --ignore=tests/test_pii_detection_pipeline.py
```

For each failure **on a `/v1/*` response**, change `r.json()["detail"]` to
`v1_error(r)["message"]`. Leave every `/admin/*` assertion alone. The known sites are listed in
**Files** above; trust the suite over that list.

While you are in `tests/onboarding/test_rejection_points.py:50`, note the assertion
`"UNVERIFIABLE" not in r.text.replace(r.json()["detail"], "")` — it strips the detail text out of
the raw body before checking for a leak. Rewrite it against the new message
(`r.text.replace(v1_error(r)["message"], "")`) so it keeps testing the same thing: that the token
does not appear anywhere *outside* the message.

- [ ] **Step 10: Run the full suite**

Run: `LITELLM_LOCAL_MODEL_COST_MAP=True .venv/bin/python -m pytest -q --ignore=tests/test_pii_detection_pipeline.py`
Expected: PASS, no failures.

- [ ] **Step 11: Commit**

```bash
git add -A
git commit -m "feat(api): render /v1 errors in the OpenAI envelope, shared with the SSE frame (B49)"
```

---

### Task 3: The consumer-contract test with the real OpenAI SDK

**Files:**
- Modify: `tests/test_openai_compat.py`
- Test: same file

**Interfaces:**
- Consumes from Task 2: the envelope on every `/v1` failure; `v1_error` from `tests/conftest.py`.
- Produces: nothing downstream.

This is the test that reproduces the original complaint: the client that showed "API error" was
an OpenAI SDK client, so the assertion must go through the SDK.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_openai_compat.py`. Reuse the file's existing fixtures for a valid app token
and a working model — read the file first and follow its established setup rather than inventing
a new one.

```python
def _sdk(client, token: str):
    """A real OpenAI SDK client speaking to the in-process app.

    `TestClient` is an `httpx.Client` subclass and `openai.OpenAI` accepts an
    `http_client`, so this is the genuine SDK parse path -- the same code that
    turned RouteForge's 400 into a bare "API error" for Qwen Code (B49).
    """
    openai = pytest.importorskip("openai")
    return openai.OpenAI(base_url="http://testserver/v1", api_key=token,
                         http_client=client, max_retries=0)


def test_sdk_reads_context_overflow_code(client, monkeypatch) -> None:
    openai = pytest.importorskip("openai")
    # Drive the backend to raise ContextWindowExceeded -- copy the mechanism
    # from tests/test_context_window_overflow.py rather than reinventing it.
    ...
    with pytest.raises(openai.BadRequestError) as ei:
        _sdk(client, token).chat.completions.create(
            model=MODEL, messages=[{"role": "user", "content": "hi"}])
    assert ei.value.code == "context_length_exceeded"
    assert ei.value.type == "invalid_request_error"
    # `.message` is the SDK's own "Error code: 400 - {...}" wrapper; the
    # RouteForge text lives on the parsed body.
    assert "shorten the conversation" in ei.value.body["message"]


def test_sdk_reads_auth_failure_code(client) -> None:
    openai = pytest.importorskip("openai")
    with pytest.raises(openai.AuthenticationError) as ei:
        _sdk(client, "rf_live_nope").chat.completions.create(
            model="any", messages=[{"role": "user", "content": "hi"}])
    assert ei.value.code == "invalid_api_key"
```

Set `max_retries=0` — the SDK retries 429s and 5xx by default, which would make a rate-limit
test slow and a server-error test misleading.

- [ ] **Step 2: Run to verify they fail if the envelope regresses**

Run: `LITELLM_LOCAL_MODEL_COST_MAP=True .venv/bin/python -m pytest tests/test_openai_compat.py -q`
Expected: PASS (Task 2 already shipped the envelope). To prove the tests bite, temporarily
revert the handler registration, confirm both fail with `code is None`, then restore it. Report
that you did this — a contract test that cannot fail is not a test.

- [ ] **Step 3: Add the stream/non-stream parity test**

The spec's invariant 4: one exception, both paths, same `type` and `code`.

```python
def test_streamed_and_plain_errors_agree(client, monkeypatch) -> None:
    """A failure describes itself identically whether or not the response had
    already started (B49 design §3.4)."""
```

Drive the same exception (`NoEligibleCandidate` is easiest — no backend needed) down the plain
path and the keepalive-stream path, parse the `data:` frame's JSON out of the SSE body, and
assert both carry `type == "invalid_request_error"` and `code == "no_eligible_model"`.
`tests/test_stream_keepalive.py` shows how to force the keepalive path; follow it.

- [ ] **Step 4: Add the redaction regression test**

```python
def test_backend_error_message_still_scrubs_endpoints(client, monkeypatch) -> None:
    """The envelope adds no text and reads no new source, so it cannot widen a
    leak -- pinned, because it renders a message that used to be opaque."""
```

Make the backend raise an exception whose text contains `http://10.1.2.3:8000/v1`, assert the
rendered `v1_error(r)["message"]` contains `<endpoint>` and contains neither `10.1.2.3` nor
`http://`.

- [ ] **Step 5: Add the unregistered-exception test (spec invariant 7)**

An `HTTPException` raised on `/v1/*` by code that never heard of `ApiError` — FastAPI's own 405
on a wrong method, a future route's plain raise — must still render a valid envelope rather than
a half-shape. Nothing should have to remember to opt in.

```python
def test_plain_http_exception_on_v1_still_renders_an_envelope(client) -> None:
    """A raise that names no code gets a status-derived type and a null code."""
    r = client.get("/v1/chat/completions")  # GET on a POST-only route -> 405
    assert r.status_code == 405
    err = v1_error(r)
    assert set(err) == {"message", "type", "param", "code"}
    assert err["code"] is None
    assert err["type"] == "invalid_request_error"
```

If the route's method set makes 405 unreachable, use any other `/v1` path that raises a plain
`HTTPException` (a 404 on `/v1/nonexistent` also exercises the Starlette-raised path, which is
the reason Task 2 registers against `StarletteHTTPException`). Say which you used.

- [ ] **Step 6: Run the suite**

Run: `LITELLM_LOCAL_MODEL_COST_MAP=True .venv/bin/python -m pytest -q --ignore=tests/test_pii_detection_pipeline.py`
Expected: PASS

- [ ] **Step 7: Commit**

```bash
git add tests/test_openai_compat.py
git commit -m "test(api): OpenAI SDK contract tests for the error envelope (B49)"
```

---

### Task 4: Capture `max_model_len` in the probe

**Files:**
- Modify: `app/apps/probe.py` (whole file, 35 lines)
- Modify: `app/api/admin_routes.py` (the two `probe_endpoint` call sites, ~:968 and ~:1026 — signature only in this task; the storage decision is Task 5)
- Test: `tests/test_probe.py` (new — `probe_endpoint` has no direct test today)

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces for Task 5:
  - `ProbeResult` frozen dataclass with fields `ok: bool`, `detail: str`, `max_model_len: int | None`.
  - `probe_endpoint(endpoint: str, slug: str, api_key_env: str | None) -> ProbeResult`
  - `parse_max_model_len(entry: object) -> int | None` (module-level, so Task 5 and tests can use it directly)

- [ ] **Step 1: Write the failing tests**

Create `tests/test_probe.py`. `probe_endpoint` is stubbed out everywhere today, so this is its
first real test — cover the HTTP parse with `httpx.MockTransport`.

```python
"""Direct tests for the onboarding probe's HTTP parsing (B50).

`probe_endpoint` is monkeypatched away in every other test file, so this is
the only place its real behaviour is pinned.
"""
import httpx
import pytest

from app.apps.probe import ProbeResult, parse_max_model_len, probe_endpoint


@pytest.mark.parametrize("value,expected", [
    (131072, 131072),
    (262144.0, 262144),
    ("131072", 131072),
    (None, None),
    (0, None),
    (-1, None),
    ("very long", None),
    (True, None),          # a bool is not a context window
    (1.5, None),           # a non-integral float is not a token count
])
def test_parse_max_model_len(value, expected) -> None:
    assert parse_max_model_len({"id": "m", "max_model_len": value}) == expected


def test_parse_missing_key_is_none() -> None:
    assert parse_max_model_len({"id": "m"}) is None


def _transport(payload: object, status: int = 200) -> httpx.MockTransport:
    return httpx.MockTransport(lambda req: httpx.Response(status, json=payload))


@pytest.mark.anyio
async def test_probe_captures_max_model_len(monkeypatch) -> None:
    payload = {"data": [{"id": "other", "max_model_len": 4096},
                        {"id": "qwen", "max_model_len": 262144}]}
    # Patch the client the probe builds so it uses the mock transport; read
    # app/apps/probe.py and follow how it constructs httpx.AsyncClient.
    ...
    res = await probe_endpoint("http://example.com/v1", "qwen", None)
    assert isinstance(res, ProbeResult)
    assert res.ok is True
    assert res.max_model_len == 262144


@pytest.mark.anyio
async def test_probe_without_max_model_len_still_succeeds() -> None:
    """Reachability and context capture are independent."""
    ...
    res = await probe_endpoint("http://example.com/v1", "qwen", None)
    assert res.ok is True and res.max_model_len is None


@pytest.mark.anyio
async def test_probe_survives_a_malformed_payload() -> None:
    """A garbage body is not a failed probe -- and never raises."""
    ...  # payload = {"data": "not-a-list"}
    res = await probe_endpoint("http://example.com/v1", "qwen", None)
    assert res.max_model_len is None
    assert isinstance(res.detail, str)


@pytest.mark.anyio
async def test_probe_failure_reports_not_ok() -> None:
    ...  # status 500
    res = await probe_endpoint("http://example.com/v1", "qwen", None)
    assert res.ok is False and res.max_model_len is None
```

Read `app/apps/probe.py` and the repo's existing async-test convention (check whether other
tests use `@pytest.mark.anyio`, `@pytest.mark.asyncio`, or `asyncio.run`) and follow it — do not
introduce a new async test style.

- [ ] **Step 2: Run to verify they fail**

Run: `LITELLM_LOCAL_MODEL_COST_MAP=True .venv/bin/python -m pytest tests/test_probe.py -q`
Expected: FAIL — `ImportError: cannot import name 'ProbeResult'`

- [ ] **Step 3: Implement**

In `app/apps/probe.py`:

```python
@dataclass(frozen=True)
class ProbeResult:
    """What one reachability probe learned about an endpoint.

    `ok`/`detail` are the reachability verdict the admin routes gate
    `enabled` on. `max_model_len` is independent: a reachable endpoint that
    publishes no context limit is still a successful probe (B50).
    """
    ok: bool
    detail: str
    max_model_len: int | None = None


def parse_max_model_len(entry: object) -> int | None:
    """The context window an upstream `/v1/models` entry declares, or None.

    vLLM publishes `max_model_len`; other OpenAI-compatible servers may not
    publish anything. Accepts an integer, an integral float, or a numeric
    string, and only when strictly positive. Everything else -- missing,
    null, a bool, a fractional float, an unparseable string, zero, negative --
    is None. **No default is ever guessed**: an unknown context window stays
    unknown, because a wrong number is worse than no number (a client that
    trusts it overflows, which is exactly the B36 failure this is meant to
    prevent).
    """
```

Implementation notes: reject `bool` explicitly (it is an `int` subclass); accept `float` only
when `value.is_integer()`; wrap the string branch in `try/except (TypeError, ValueError)`.

Then make `probe_endpoint` return `ProbeResult(...)` on every path, reading
`max_model_len` from the entry whose `id` equals `slug`. The existing behaviour is otherwise
untouched: same URL, same auth header, same "never raises" contract, same `detail` strings.

Update the two call sites in `app/api/admin_routes.py` to the new return type. In **this task**
keep their behaviour identical — `ok, detail = res.ok, res.detail` — and do not yet use
`max_model_len`. Task 5 owns that decision.

- [ ] **Step 4: Run the tests**

Run: `LITELLM_LOCAL_MODEL_COST_MAP=True .venv/bin/python -m pytest tests/test_probe.py tests/test_onboard_api.py -q`
Expected: PASS. `tests/test_onboard_api.py` stubs `probe_endpoint` with async functions returning
a 2-tuple — those stubs must be updated to return `ProbeResult`, or the call sites will unpack
wrongly. Fix every stub you find (`tests/test_onboard_api.py`,
`tests/test_onboard_cached_price.py`, `tests/test_policy_save_validation.py`).

- [ ] **Step 5: Run the full suite**

Run: `LITELLM_LOCAL_MODEL_COST_MAP=True .venv/bin/python -m pytest -q --ignore=tests/test_pii_detection_pipeline.py`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add -A
git commit -m "feat(onboarding): probe captures the endpoint's max_model_len (B50)"
```

---

### Task 5: Store it, with the operator's value winning

**Files:**
- Modify: `app/apps/models.py` (`OnboardedModel`, ~:336)
- Modify: `app/apps/store.py` (`_ONBOARDED_FIELDS`, ~:562-567)
- Modify: `app/api/admin_routes.py` (`onboard_model` ~:947-1004, `edit_onboarded_model` ~:1012-1049, the serializer at ~:72)
- Modify: `scripts/migrate_app_policies.py` (`MODEL_COLUMNS`, ~:78-84)
- Modify: `docs/reference/per-app-config.md` (the migration table, ~:1172-1181)
- Test: `tests/test_onboard_api.py`

**Interfaces:**
- Consumes from Task 4: `ProbeResult.max_model_len`.
- Produces for Task 6: `OnboardedModel.context_window` populated, and
  `OnboardedModel.context_window_source: str | None` ∈ `{"operator", "probed", None}`.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_onboard_api.py`, following its existing probe-stub pattern (stubs now return
`ProbeResult`):

```python
def _probe_with(max_model_len: int | None):
    async def _p(endpoint: str, slug: str, api_key_env: str | None) -> ProbeResult:
        return ProbeResult(True, "ok", max_model_len)
    return _p


def test_probed_context_window_is_stored(client, monkeypatch) -> None:
    """A probe that learns the limit fills the column and says where it came from."""
    monkeypatch.setattr("app.api.admin_routes.probe_endpoint", _probe_with(131072))
    body = {"slug": "qwen", "display_name": "Qwen", "tier": "cheap",
            "endpoint": "http://example.com/v1",
            "price_input": 0.0, "price_output": 0.0}   # no context_window
    r = client.post("/admin/infra/models", json=body, headers=ADMIN)
    assert r.status_code in (200, 201), r.text
    row = next(m for m in client.get("/admin/infra/models", headers=ADMIN).json()
               if m["slug"] == "qwen")
    assert row["context_window"] == 131072
    assert row["context_window_source"] == "probed"


def test_operator_value_beats_the_probe_on_post(client, admin) -> None:
    # stub probe -> max_model_len=131072; POST with context_window=8192
    # -> stored 8192, source "operator"


def test_operator_value_beats_the_probe_on_put(client, admin) -> None:
    # same, via PUT on an existing row -- the console always resubmits the
    # whole body, so this is the common path


def test_reprobe_does_not_overwrite_an_operator_value(client, admin) -> None:
    """The operator's number survives a later edit that re-probes a different
    limit. An explicit value always wins (design §4.2)."""


def test_unknown_context_window_stays_null(client, admin) -> None:
    # probe -> max_model_len=None, body omits context_window
    # -> context_window is None, source is None, and no default appears
```

`ADMIN` above stands for whatever admin-auth headers/fixture `tests/test_onboard_api.py`
already uses — read the file and follow it; do not introduce a second way to authenticate an
admin request. The four tests after the first are the same shape with a different probe stub and
body, so follow the first one's structure exactly.

- [ ] **Step 2: Run to verify they fail**

Run: `LITELLM_LOCAL_MODEL_COST_MAP=True .venv/bin/python -m pytest tests/test_onboard_api.py -q`
Expected: FAIL — `context_window_source` does not exist.

- [ ] **Step 3: Add the column**

In `app/apps/models.py`, next to `context_window` (~:336):

```python
    # Where `context_window` came from: "operator" (a human typed it) or
    # "probed" (read from the endpoint's own /v1/models max_model_len). NULL
    # when context_window is NULL. Same honesty as the P1b data-handling
    # declarations: a number RouteForge inferred must not look like one the
    # operator asserted. (B50)
    context_window_source: Mapped[str | None] = mapped_column(String(16), nullable=True)
```

Add `"context_window_source"` to `_ONBOARDED_FIELDS` in `app/apps/store.py`, and to the
admin serializer at `app/api/admin_routes.py:~72` beside `"context_window"`.

- [ ] **Step 4: Implement the precedence**

Add one helper in `app/api/admin_routes.py`, used by both handlers:

```python
def _resolved_context_window(body: OnboardIn, probed: int | None) -> tuple[int | None, str | None]:
    """`context_window` and its source (B50, design §4.2).

    An explicit value always wins and is never overwritten by a later probe --
    the operator knows things RouteForge cannot read off an endpoint (a proxy
    that truncates, a deliberately conservative budget). Only when they supply
    none does the probed value land. Deliberately NOT omission-preserving like
    `price_cached_input`: context_window is full-replace today, the console
    always resubmits the whole body, and this rule already gives the operator
    the last word on every write that carries their value.
    """
    if body.context_window is not None:
        return body.context_window, "operator"
    if probed is not None:
        return probed, "probed"
    return None, None
```

Call it in both `onboard_model` and `edit_onboarded_model`, passing the probe's
`max_model_len`, and pass both results to `store.upsert_onboarded`.

Note the probe runs **before** the row is written in both handlers — confirm the ordering when
you edit, and keep it.

- [ ] **Step 5: Add the migration**

In `scripts/migrate_app_policies.py`, append to `MODEL_COLUMNS` following the existing
`(name, (pg_type, sqlite_type))` shape:

```python
    ("context_window_source", ("VARCHAR(16)", "VARCHAR(16)")),
```

In `docs/reference/per-app-config.md`'s migration section, add a dated row:

```sql
-- 2026-09-22 (B50): where an onboarded model's context window came from
ALTER TABLE onboarded_models ADD COLUMN context_window_source VARCHAR(16);
```

with the standing warning: **run this before rolling the image**; an unmigrated database fails
every onboarded-model read, which takes on-prem routing down.

- [ ] **Step 6: Run the tests**

Run: `LITELLM_LOCAL_MODEL_COST_MAP=True .venv/bin/python -m pytest tests/test_onboard_api.py tests/test_probe.py -q`
Expected: PASS

- [ ] **Step 7: Run the full suite**

Run: `LITELLM_LOCAL_MODEL_COST_MAP=True .venv/bin/python -m pytest -q --ignore=tests/test_pii_detection_pipeline.py`
Expected: PASS

- [ ] **Step 8: Commit**

```bash
git add -A
git commit -m "feat(onboarding): store the probed context window, operator value wins (B50)"
```

---

### Task 6: Carry it to `GET /v1/models`

**Files:**
- Modify: `app/apps/registry.py` (`_OnboardedRow` ~:15-36, `refresh` ~:54-70, `effective_pool` ~:135-165)
- Modify: `app/config/schema.py` (`PoolCandidate` ~:8-38)
- Modify: `app/api/routes.py` (`list_models` ~:169-184)
- Test: `tests/test_model_registry.py`, `tests/test_v1_models.py`

**Interfaces:**
- Consumes from Task 5: `OnboardedModel.context_window` populated on the row.
- Produces: `PoolCandidate.context_window: int | None`; `GET /v1/models` entries carry
  `max_model_len` when known.

`context_window` is write-only today — it never leaves the DB row. This task adds the four
missing links.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_v1_models.py`:

```python
def test_models_list_publishes_max_model_len(client, ...) -> None:
    """A known context window reaches the client under vLLM's own field name,
    which is the name clients already read (B50)."""
    # seed an onboarded row with context_window=131072
    entry = next(m for m in r.json()["data"] if m["id"] == SLUG)
    assert entry["max_model_len"] == 131072


def test_models_list_omits_unknown_max_model_len(client, ...) -> None:
    """Unknown is absent, never a guess and never zero."""
    # seed an onboarded row with context_window=None
    entry = next(m for m in r.json()["data"] if m["id"] == SLUG)
    assert "max_model_len" not in entry
```

Add to `tests/test_model_registry.py`, using its existing `_reg_with(rows)` helper:

```python
def test_effective_pool_carries_context_window() -> None:
    # a row with context_window=8192 -> candidate.context_window == 8192
    # a row with context_window=None -> candidate.context_window is None
```

- [ ] **Step 2: Run to verify they fail**

Run: `LITELLM_LOCAL_MODEL_COST_MAP=True .venv/bin/python -m pytest tests/test_v1_models.py tests/test_model_registry.py -q`
Expected: FAIL — `KeyError: 'max_model_len'` / `AttributeError: context_window`

- [ ] **Step 3: Add the field to `PoolCandidate`**

In `app/config/schema.py`, inside `PoolCandidate`:

```python
    # The model's real context window in tokens, when known. Populated for
    # onboarded rows from the endpoint's own `max_model_len` or the operator's
    # value (B50); always None for static YAML cloud entries, which declare
    # none. Published on GET /v1/models, never used to gate a request --
    # RouteForge still does not pre-check prompt size (see B36).
    context_window: int | None = None
```

- [ ] **Step 4: Carry it through the registry**

Add `context_window: int | None = None` to `_OnboardedRow`, populate it in `refresh()` from
`r.context_window`, and set it on the onboarded branch of `effective_pool()`.

- [ ] **Step 5: Publish it**

In `app/api/routes.py::list_models`, build each entry so the key is **absent** when unknown:

```python
    data = []
    for c in models:
        entry = {"id": c.name, "object": "model", "created": 0,
                 "owned_by": c.provider}
        # vLLM's field name: the one clients already read. Omitted entirely
        # when unknown -- a guessed context window overflows the request it
        # was meant to size (B50).
        if c.context_window:
            entry["max_model_len"] = c.context_window
        data.append(entry)
    return {"object": "list", "data": data}
```

Update the docstring to mention `max_model_len` and that it is omitted when unknown.

- [ ] **Step 6: Run the tests**

Run: `LITELLM_LOCAL_MODEL_COST_MAP=True .venv/bin/python -m pytest tests/test_v1_models.py tests/test_model_registry.py -q`
Expected: PASS

- [ ] **Step 7: Run the full suite**

Run: `LITELLM_LOCAL_MODEL_COST_MAP=True .venv/bin/python -m pytest -q --ignore=tests/test_pii_detection_pipeline.py`
Expected: PASS

- [ ] **Step 8: Commit**

```bash
git add -A
git commit -m "feat(api): publish max_model_len on GET /v1/models (B50)"
```

---

### Task 7: Console hint, documentation, backlog

**Files:**
- Modify: `ui/src/api.ts` (~:474 `OnboardedModel` type)
- Modify: `ui/src/admin/InfrastructurePanel.tsx` (the context-window input, ~:199-202)
- Modify: `docs/reference/api-reference.md` — **the existing owner of all of this**: the `Errors`
  table (~:287-305), the 400 example (~:230), the SSE error-frame section (~:246-257), and the
  `GET /v1/models` section (~:321)
- Modify: `docs/guides/onboarding-models.md` (field table ~:37-52)
- Modify: `docs/backlog/bugs.md` (B49, B50)

**Plan correction (controller ruling, logged in the ledger).** An earlier draft of this task
created a new `docs/reference/openai-error-format.md`. It must not: `api-reference.md` already
documents the error table, the SSE frame shape and `/v1/models`, and a second reference beside it
would be two owners of one concern — the drift class this repo's config lint exists to prevent.
Every doc change below lands **in `api-reference.md`**.

**Interfaces:**
- Consumes from Tasks 5–6: `context_window_source` on the admin model payload;
  `max_model_len` on `/v1/models`.
- Produces: nothing downstream.

- [ ] **Step 1: Add the source to the UI type**

In `ui/src/api.ts`, add `context_window_source: string | null;` to the `OnboardedModel` type
(beside `context_window`). Do **not** add it to `OnboardIn` — it is server-derived, never sent.

- [ ] **Step 2: Show where the number came from**

In `ui/src/admin/InfrastructurePanel.tsx`, the `context window (optional)` label (~:199-202)
gains a hint when the stored source is `"probed"`, e.g. `context window (from endpoint)` with
help text "read from the endpoint's /v1/models; type a value to override". Keep the input
editable. Follow the panel's existing label/help-text styling — do not invent a new pattern.

A number RouteForge inferred must not look like one the operator asserted.

- [ ] **Step 3: Build the UI**

Run the repo's UI build/lint (check `ui/package.json`; typically `npm run build` in `ui/`).
Expected: clean. If `npm ci` fails in this environment, report that and move on — the type
change is small and reviewable, and a broken UI toolchain is not this task's problem to solve.

- [ ] **Step 4: Update the API reference**

Four edits in `docs/reference/api-reference.md`, all of which are **wrong on `main` after this
branch** and must be corrected together:

1. **The `Errors` table (~:287-305).** Its header is `| Status | `detail` message | Cause |`. Retitle
   the message column and add a `code` column, filling in the code every row now carries (spec
   §3.3). Introduce the table with the envelope shape:
   ```json
   {"error": {"message": "...", "type": "invalid_request_error", "param": null, "code": "context_length_exceeded"}}
   ```
2. **The 400 example (~:230)** still shows `400 {"detail": "streaming is not supported for this
   application"}`. Rewrite it as the envelope with `code: "streaming_not_supported"`.
3. **The SSE error-frame section (~:246-257)** still documents
   `{"error": {"message": "<detail>", "type": "routeforge_error", "code": <http-status>}}`.
   Correct it to the real `type`/`code`, and say plainly that this changed — `type` was the
   constant `routeforge_error` and `code` was the integer status.
4. **The `GET /v1/models` section (~:321)** gains `max_model_len`: what it is, that it is vLLM's
   field name, that it is omitted when unknown and never guessed, and that RouteForge does not
   enforce it (an oversized request still fails upstream — B36).

Add a short client-side example showing
`except openai.BadRequestError as e: e.code == "context_length_exceeded"`.

Also state, once, **which codes are OpenAI's and which are RouteForge's own**, and that
RouteForge never reuses an OpenAI code for a condition OpenAI does not have; and that `/admin/*`
and `/api/*` keep `{"detail": ...}`.

- [ ] **Step 5: Update the onboarding guide**

In `docs/guides/onboarding-models.md`'s field table, document `context_window` and
`context_window_source`: captured from the endpoint's `max_model_len` on onboard and on edit, an
operator value always wins, never guessed, and — stated plainly — **only as fresh as the last
onboard or edit, because there is no re-probe loop**.

- [ ] **Step 6: Close the backlog entries**

In `docs/backlog/bugs.md`, mark B49 and B50 `✅ RESOLVED (2026-09-22, <commit>)` following the
format B48 uses, each with a one-line note on what shipped and a pointer to the new reference
doc. Leave B51 and B45 open and untouched.

- [ ] **Step 7: Run the full suite**

Run: `LITELLM_LOCAL_MODEL_COST_MAP=True .venv/bin/python -m pytest -q --ignore=tests/test_pii_detection_pipeline.py`
Expected: PASS

- [ ] **Step 8: Commit**

```bash
git add -A
git commit -m "docs(api): OpenAI error format reference, context-window capture, close B49/B50"
```
