"""B55: an outcome row records the client's model pin (`requested_model`) and
its output-token cap (`max_tokens`), so the offline policy simulator can
replay a pinned request pinned and price it at the client's own cap.

Driven through the real HTTP boundary, like tests/test_outcome_hooks.py. Both
fields are content-free: a model slug (null = unpinned, the gate's meaning of
`model: "auto"`) and an int (null = no client cap).
"""
from __future__ import annotations

import json

import pytest

from tests.test_admin_api import _login
from tests.test_outcome_hooks import (
    DEMO, HELLO, _client, _failing_backend, _only, _with_azure_enabled,
)
from tests.onboarding.harness import disable_seeded_models, onboard


@pytest.mark.parametrize("extra, pin, cap", [
    ({"model": "local-8b"}, "local-8b", None),
    ({"model": "auto"}, None, None),
    ({}, None, None),
    ({"max_tokens": 64}, None, 64),
    ({"max_tokens": 64, "max_completion_tokens": 32}, None, 32),
    ({"model": "local-8b", "max_completion_tokens": 48}, "local-8b", 48),
])
def test_ok_row_records_pin_and_output_cap(extra, pin, cap):
    with _client() as c:
        r = c.post("/v1/chat/completions", headers=DEMO, json={**HELLO, **extra})
        assert r.status_code == 200, r.text
        row = _only(c)
        assert row.status == "ok"
        assert row.requested_model == pin
        assert row.max_tokens == cap
        if pin is not None:
            assert row.chosen == pin


def test_backend_failed_row_carries_pin_and_cap(monkeypatch):
    _failing_backend(monkeypatch)
    with _client() as c:
        r = c.post("/v1/chat/completions", headers=DEMO,
                   json={**HELLO, "model": "local-8b", "max_tokens": 64})
        assert r.status_code == 502, r.text
        row = _only(c)
        assert row.status == "backend_failed"
        assert row.requested_model == "local-8b"
        assert row.max_tokens == 64


def test_no_eligible_row_carries_cap(monkeypatch):
    _with_azure_enabled(monkeypatch)
    with _client() as c:
        disable_seeded_models(c)
        app = onboard(c, "oc-b55-no-candidate", residency="onprem_only")
        r = app.chat({**HELLO, "max_completion_tokens": 77})
        assert r.status_code == 422, r.text
        row = _only(c)
        assert row.status == "no_eligible"
        assert row.requested_model is None
        assert row.max_tokens == 77


def test_stream_row_records_pin_and_cap(monkeypatch):
    from tests.onboarding.harness import install_dual_fake
    install_dual_fake(monkeypatch)
    with _client() as c:
        app = onboard(c, "oc-b55-stream", stream_mode="stream_unverified")
        with c.stream("POST", "/v1/chat/completions", headers=app.headers,
                      json={**HELLO, "stream": True, "max_tokens": 40}) as r:
            assert r.status_code == 200
            "".join(r.iter_text())
        row = _only(c)
        assert row.status == "ok"
        assert row.requested_model is None
        assert row.max_tokens == 40


def test_export_line_carries_pin_and_cap():
    with _client() as c:
        r = c.post("/v1/chat/completions", headers=DEMO,
                   json={**HELLO, "model": "local-8b", "max_tokens": 64})
        assert r.status_code == 200, r.text
        _login(c)
        r = c.get("/admin/outcomes/export")
        assert r.status_code == 200, r.text
        lines = [json.loads(x) for x in r.text.strip().splitlines() if x]
        assert len(lines) == 1
        outcome = lines[0]["outcome"]
        assert outcome["requested_model"] == "local-8b"
        assert outcome["max_tokens"] == 64
