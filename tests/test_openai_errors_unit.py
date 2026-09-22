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
