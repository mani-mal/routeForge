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
  `streaming_not_supported`, `backend_unavailable`, `policy_unavailable`,
  `internal_error`, `invalid_request` -- for conditions OpenAI has no
  equivalent for.

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
CODE_INVALID_REQUEST = "invalid_request"
CODE_UNKNOWN_MODEL_ALIAS = "unknown_model_alias"


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
                  type: str | None = None, extra: dict | None = None) -> dict:
    """The response body for a `/v1/*` failure.

    `param` is always present and null: the OpenAI SDK reads it off the inner
    `error` object, and an absent key and a null key are not the same thing to
    every client. RouteForge never attributes a failure to one request field,
    so it is never populated.

    `extra` merges additional content-free keys into the inner `error` object
    (P3b: `conflicting_core` on a `no_eligible_model` 422). It never
    overrides the four standard keys.
    """
    return {"error": {**(extra or {}),
                      "message": message,
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
                 code: str | None = None, type: str | None = None,
                 extra: dict | None = None) -> None:
        super().__init__(status_code, detail)
        self.openai_code = code
        self.openai_type = type
        # Extra keys for the inner `error` object (see `error_payload`).
        self.openai_extra = extra
