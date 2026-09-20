"""End-to-end coverage for cached-prompt-token persistence -- the assertion
that would have caught the fix-round-1 gap: `GenerationResult` carried the
provider-reported cached count correctly (tests/routing/test_cached_tokens.py
and litellm_backend's own tests cover that), but nothing threaded it from
there into `resp["usage"]` / the persisted `UsageRecord` row. See
app/pipeline.py's three `Usage(...)` construction sites and
app/api/routes.py's serving-path `record_usage` call.
"""
from __future__ import annotations

import asyncio
import os

from fastapi.testclient import TestClient

from app.apps import store
from app.apps.db import Database
from app.gateway.backends import litellm_backend
from app.main import create_app
from app.models import ChatRequest, Message
from tests._litellm_fake import FakeResponse, _DeterministicLiteLLM
from tests.test_semantic_cache_pipeline import _base_settings, _pipeline

_CLEAN = {"messages": [{"role": "user", "content": "What is 2 + 2?"}]}


def _cached_response(*, cached_tokens: int, prompt_tokens: int = 20,
                     completion_tokens: int = 5) -> FakeResponse:
    return FakeResponse({
        "choices": [{"message": {"role": "assistant", "content": "ok"}, "logprobs": None}],
        "usage": {"prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens,
                  "prompt_tokens_details": {"cached_tokens": cached_tokens}},
    })


def _served_row(tmp_path, monkeypatch, *, acompletion=None):
    """POST one served /v1/chat/completions request against a file-backed DB
    (so this test's own event loop can read what the app's portal loop wrote
    -- the in-memory StaticPool the `client` fixture defaults to is bound to
    a single loop) and return the persisted `UsageRecord` row. Same pattern as
    tests/test_token_auth.py's test_usage_row_persisted_for_served_request.
    """
    db_url = f"sqlite+aiosqlite:///{tmp_path}/rf.db"
    prev = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = db_url
    try:
        if acompletion is not None:
            monkeypatch.setattr(litellm_backend, "acompletion", acompletion)
        with TestClient(create_app()) as c:
            r = c.post("/v1/chat/completions",
                       headers={"Authorization": "Bearer acme-key"}, json=_CLEAN)
            assert r.status_code == 200

        async def read_rows():
            db = Database(url=db_url)
            try:
                async with db.session() as s:
                    return await store.list_usage(s)
            finally:
                await db.dispose()

        rows = asyncio.run(read_rows())
        assert len(rows) >= 1
        return rows[0]
    finally:
        if prev is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = prev


def test_served_request_persists_provider_reported_cached_tokens(tmp_path, monkeypatch):
    """The assertion that would have caught the gap: a backend response
    carrying usage.prompt_tokens_details.cached_tokens must show up on the
    PERSISTED usage_records row, not just on the in-memory GenerationResult
    (which already worked before this fix round)."""
    async def _acompletion(**kwargs):
        return _cached_response(cached_tokens=7)

    row = _served_row(tmp_path, monkeypatch, acompletion=_acompletion)
    assert row.status == "ok"
    assert row.cached_prompt_tokens == 7


def test_provider_reporting_no_details_persists_zero(tmp_path, monkeypatch):
    """No local acompletion override -- the autouse `_fake_acompletion`
    fixture's canned response carries no `prompt_tokens_details` at all,
    exactly the "provider doesn't report it" case, indistinguishable from a
    genuine cache miss by design."""
    row = _served_row(tmp_path, monkeypatch)
    assert row.status == "ok"
    assert row.cached_prompt_tokens == 0


def test_semantic_cache_hit_records_zero_cached_prompt_tokens():
    """A cache hit makes no provider call at all, so cached_prompt_tokens
    must be the honest 0 (not a missing/omitted value) -- covers the
    app/pipeline.py ~605 semantic-cache-hit `Usage(...)` construction, which
    deliberately never reads a GenerationResult.cached_prompt_tokens because
    there is no GenerationResult on a hit."""
    settings = _base_settings()
    pipe, cache = _pipeline(_DeterministicLiteLLM(settings), settings)
    req = ChatRequest(messages=[Message(role="user", content="What is the capital of France?")])

    out1 = asyncio.run(pipe.run("r1", "acme", req))
    out2 = asyncio.run(pipe.run("r2", "acme", req))

    hit_recs = [r for r in out2["trace"] if r.get("step") == "cache"]
    assert hit_recs and hit_recs[0]["hit"] is True
    assert out2["usage"]["cached_prompt_tokens"] == 0
