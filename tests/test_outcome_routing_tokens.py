"""B60: every outcome row records `routing_tokens` -- the token count the
router was given (`tokens_after`: RouteForge's own post-gate, post-compression
count) -- whether the request was served or rejected, so the offline policy
simulator can replay a rejected request at its real size.

Driven through the real HTTP boundary, like tests/test_outcome_hooks.py: a
real app from `create_app()`, a real `TestClient`, the litellm fakes. The
recorded value is checked against what the router was ACTUALLY called with
(a spy on `Router.select`), never against a re-derivation of it.

`prompt_tokens` is a different number and is untouched: it stays the
backend's billed count (the fakes always report 10), and 0 on a row no
backend answered.
"""
from __future__ import annotations

import json
import logging
import sqlite3

import pytest

from app.errors import ContextWindowExceeded
from app.gateway.backends import litellm_backend
from app.routing.router import Router
from tests.conftest import v1_error
from tests.onboarding.harness import install_dual_fake, onboard
from tests.test_admin_api import _login
from tests.test_outcome_hooks import (
    DEMO, EMAIL, HELLO, SSN, _client, _columns, _failing_backend, _failing_verify, _only,
    _outcomes,
)

MARKER = "ZZMARKER-b60"
# 12 000 characters -> 3000 routing tokens (`count_tokens` is len // 4): above
# the 2000-token complexity saturation point, so `complexity` alone (1.0)
# cannot tell this request's size -- only `routing_tokens` can.
LONG_PROMPT = "word " * 2400
LONG = {"messages": [{"role": "user", "content": LONG_PROMPT}], "max_tokens": 16}
# Seeded prices are $0.05 (local-8b) and $0.10 (local-32b) per 1M tokens, so a
# 3000-token prompt + 16 output tokens is estimated at $0.00015 / $0.0003:
# over this cap on BOTH tiers. A zero-token prompt would be $0.0000008 /
# $0.0000016 -- under it. The rejection is caused by the prompt's size.
TIGHT_CAP = 0.0001


def _spy_router(monkeypatch) -> list[int]:
    """Record the `tokens_after` every `Router.select` call is given."""
    seen: list[int] = []
    real = Router.select

    def select(self, ctx, tokens_after, *args, **kwargs):
        seen.append(tokens_after)
        return real(self, ctx, tokens_after, *args, **kwargs)

    monkeypatch.setattr(Router, "select", select)
    return seen


# --------------------------------------------------------------------------- #
# R10: the recorded value is the router's input, on every status
# --------------------------------------------------------------------------- #
def test_ok_row_records_the_tokens_the_router_was_given(monkeypatch):
    seen = _spy_router(monkeypatch)
    with _client() as c:
        r = c.post("/v1/chat/completions", headers=DEMO, json=LONG)
        assert r.status_code == 200, r.text
        row = _only(c)
        assert row.status == "ok"
        assert row.routing_tokens == seen[0] == 3000
        assert row.complexity == 1.0                 # saturated: it hides the size
        assert row.prompt_tokens == r.json()["usage"]["prompt_tokens"] == 10


def test_budget_rejected_row_records_the_tokens_the_router_was_given(monkeypatch):
    """The B60 row itself: rejected for `max_cost_usd_per_request`, because of
    its size. Before the fix nothing on the row said how large it was."""
    seen = _spy_router(monkeypatch)
    with _client() as c:
        app = onboard(c, "rt-budget", max_cost_usd_per_request=TIGHT_CAP)
        r = app.chat(LONG)
        assert r.status_code == 422, r.text
        assert v1_error(r)["code"] == "no_eligible_model"
        row = _only(c)
        assert row.status == "no_eligible"
        assert [q["id"] for q in row.conflicting_core["requirements"]] == \
            ["max_cost_usd_per_request"]
        assert row.routing_tokens == seen[0] == 3000
        assert row.max_tokens == 16
        assert row.prompt_tokens == 0                # never billed: unchanged


def test_backend_failed_row_records_routing_tokens(monkeypatch):
    seen = _spy_router(monkeypatch)
    _failing_backend(monkeypatch)
    with _client() as c:
        r = c.post("/v1/chat/completions", headers=DEMO, json=LONG)
        assert r.status_code == 502, r.text
        row = _only(c)
        assert row.status == "backend_failed"
        assert row.routing_tokens == seen[0] == 3000
        assert row.prompt_tokens == 0


def test_context_overflow_row_records_routing_tokens(monkeypatch):
    seen = _spy_router(monkeypatch)

    async def _overflow(model, call):
        raise ContextWindowExceeded(f"{model}: maximum context length exceeded")

    monkeypatch.setattr(litellm_backend, "_acompletion", _overflow)
    with _client() as c:
        r = c.post("/v1/chat/completions", headers=DEMO, json=LONG)
        assert r.status_code == 400, r.text
        row = _only(c)
        assert row.status == "context_overflow"
        assert row.routing_tokens == seen[0] == 3000
        assert row.prompt_tokens == 0


def test_verify_failed_row_records_both_counts(monkeypatch):
    seen = _spy_router(monkeypatch)
    _failing_verify(monkeypatch)
    with _client() as c:
        r = c.post("/v1/chat/completions", headers=DEMO, json=LONG)
        assert r.status_code == 422, r.text
        row = _only(c)
        assert row.status == "verify_failed"
        assert row.routing_tokens == seen[0] == 3000
        assert row.prompt_tokens == 10               # the served attempt's, from the backend


@pytest.mark.parametrize("mode", ["stream_unverified", "incremental_verify",
                                  "verified_then_stream"])
def test_streamed_row_records_routing_tokens(monkeypatch, mode):
    seen = _spy_router(monkeypatch)
    install_dual_fake(monkeypatch)
    with _client() as c:
        app = onboard(c, f"rt-stream-{mode}", stream_mode=mode)
        with c.stream("POST", "/v1/chat/completions", headers=app.headers,
                      json={**LONG, "stream": True}) as r:
            assert r.status_code == 200
            "".join(r.iter_text())
        row = _only(c)
        assert row.status == "ok"
        assert row.routing_tokens == seen[0] == 3000


def test_tool_conversation_row_counts_what_the_router_priced(monkeypatch):
    """A tool-bearing request is priced on its messages PLUS its tool schemas
    (`app/pipeline.py`); the recorded count is that same number."""
    seen = _spy_router(monkeypatch)
    tools = [{"type": "function", "function": {
        "name": "read_file", "description": "Read a file from the workspace",
        "parameters": {"type": "object", "properties": {"path": {"type": "string"}},
                       "required": ["path"]}}}]
    with _client() as c:
        r = c.post("/v1/chat/completions", headers=DEMO, json={**LONG, "tools": tools})
        assert r.status_code == 200, r.text
        row = _only(c)
        assert row.routing_tokens == seen[0]
        assert row.routing_tokens > 3000             # the schemas are counted too


def test_semantic_cache_hit_row_has_null_routing_tokens(monkeypatch):
    """A cache hit returns before compression: no router ran, so there is no
    count to record -- null, like `complexity` on the same row."""
    monkeypatch.setenv("SP_SEMANTIC_CACHE", "on")
    with _client() as c:
        first = c.post("/v1/chat/completions", headers=DEMO, json=HELLO)
        second = c.post("/v1/chat/completions", headers=DEMO, json=HELLO)
        assert first.status_code == 200 and second.status_code == 200
        rows = {r.request_id: r for r in _outcomes(c)}
        miss, hit = rows[first.json()["id"]], rows[second.json()["id"]]
        assert miss.routing_tokens == 1              # "hello": max(1, 5 // 4)
        assert hit.cache_hit is True
        assert hit.routing_tokens is None and hit.complexity is None


# --------------------------------------------------------------------------- #
# R1: still content-free -- a count, on a rejected request carrying PII
# --------------------------------------------------------------------------- #
def test_r1_rejected_row_with_pii_carries_a_count_and_no_content():
    with _client() as c:
        app = onboard(c, "rt-pii", max_cost_usd_per_request=TIGHT_CAP)
        r = app.chat({"max_tokens": 16, "messages": [{"role": "user", "content": (
            f"{MARKER} my SSN is {SSN}, email {EMAIL}. " + LONG_PROMPT)}]})
        assert r.status_code == 422, r.text
        row = _only(c)
        assert row.status == "no_eligible"
        assert type(row.routing_tokens) is int and row.routing_tokens > 3000
        dumped = json.dumps(_columns(row), default=str)
        for secret in (MARKER, SSN, EMAIL, "my SSN", "word word"):
            assert secret not in dumped
        # The entity TYPES are the content-free signal that remains.
        assert "US_SSN" in row.detected_entity_types


# --------------------------------------------------------------------------- #
# the export carries the field (no exporter change: it dumps every column)
# --------------------------------------------------------------------------- #
def test_export_line_carries_routing_tokens():
    with _client() as c:
        app = onboard(c, "rt-export", max_cost_usd_per_request=TIGHT_CAP)
        assert app.chat(LONG).status_code == 422
        assert c.post("/v1/chat/completions", headers=DEMO, json=HELLO).status_code == 200
        _login(c)
        r = c.get("/admin/outcomes/export")
        assert r.status_code == 200, r.text
        by_status = {x["outcome"]["status"]: x["outcome"]
                     for x in map(json.loads, r.text.strip().splitlines())}
        assert by_status["no_eligible"]["routing_tokens"] == 3000
        assert by_status["no_eligible"]["prompt_tokens"] == 0
        assert by_status["ok"]["routing_tokens"] == 1


# --------------------------------------------------------------------------- #
# R13: an unmigrated table never breaks serving; the documented ALTER repairs it
# --------------------------------------------------------------------------- #
def test_r13_unmigrated_table_serving_continues_and_the_documented_alter_repairs_it(
        tmp_path, monkeypatch, caplog):
    """`create_all` never adds a column to an existing table, so a deployed
    database needs the ALTER in docs/reference/routing-outcomes.md. Against a
    table that lacks the column the app still BOOTS and still SERVES; every
    outcome write fails and is swallowed, so no row is recorded. After the
    documented statement, rows are recorded again, with the new column.

    A real file database and three real boots: the upgraded image against an
    old table is a fresh process, not a table altered under a running one."""
    caplog.set_level(logging.WARNING, logger="serving.obs")
    path = tmp_path / "routeforge.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{path}")

    def _db(statement: str) -> list:
        con = sqlite3.connect(path)
        try:
            rows = con.execute(statement).fetchall()
            con.commit()
            return rows
        finally:
            con.close()

    with _client() as c:                         # boot 1: create_all builds the table
        assert c.post("/v1/chat/completions", headers=DEMO, json=HELLO).status_code == 200
    # Put the table in the shape a deployed database has before the upgrade.
    _db("DELETE FROM routing_outcomes")
    _db("ALTER TABLE routing_outcomes DROP COLUMN routing_tokens")

    with _client() as c:                         # boot 2: this code, the old table
        assert c.get("/health").status_code == 200                # it boots
        r = c.post("/v1/chat/completions", headers=DEMO, json=HELLO)
        assert r.status_code == 200, r.text                       # it serves
    assert _db("SELECT COUNT(*) FROM routing_outcomes") == [(0,)]  # nothing recorded
    assert any("outcome record failed" in rec.getMessage() for rec in caplog.records)

    # The SQLite form of the documented statement, verbatim.
    _db("ALTER TABLE routing_outcomes ADD COLUMN routing_tokens INTEGER")
    with pytest.raises(sqlite3.OperationalError, match="duplicate column name"):
        _db("ALTER TABLE routing_outcomes ADD COLUMN routing_tokens INTEGER")

    with _client() as c:                         # boot 3: migrated
        r = c.post("/v1/chat/completions", headers=DEMO, json=HELLO)
        assert r.status_code == 200, r.text
        row = _only(c)
        assert row.request_id == r.json()["id"] and row.routing_tokens == 1
