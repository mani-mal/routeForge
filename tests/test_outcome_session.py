"""A routing outcome row carries the CLIENT's `x-routeforge-session` label,
through the real HTTP boundary, on every status.

The value is the validated client header only. The server-derived session id
(`server.derive_session_id`) groups traces and is never stored on the row; an
absent or invalid header gives a null column and the request is served anyway.
"""
from __future__ import annotations

import json

from tests.onboarding.harness import disable_seeded_models, install_dual_fake, onboard
from tests.test_admin_api import _login
from tests.test_outcome_hooks import (
    DEMO, HELLO, _client, _failing_backend, _only, _with_azure_enabled,
)

SESSION = "wf-run_42.a:b"


def test_header_value_is_stored_on_an_ok_row():
    with _client() as c:
        r = c.post("/v1/chat/completions", headers={**DEMO, "x-routeforge-session": SESSION},
                   json=HELLO)
        assert r.status_code == 200, r.text
        row = _only(c)
        assert row.status == "ok"
        assert row.session == SESSION


def test_header_value_is_stored_on_a_no_eligible_row(monkeypatch):
    _with_azure_enabled(monkeypatch)
    with _client() as c:
        disable_seeded_models(c)
        app = onboard(c, "sess-no-candidate", residency="onprem_only")
        r = c.post("/v1/chat/completions",
                   headers={**app.headers, "x-routeforge-session": SESSION}, json=HELLO)
        assert r.status_code == 422, r.text
        row = _only(c)
        assert row.status == "no_eligible"
        assert row.session == SESSION


def test_header_value_is_stored_on_a_backend_failed_row(monkeypatch):
    _failing_backend(monkeypatch)
    with _client() as c:
        r = c.post("/v1/chat/completions", headers={**DEMO, "x-routeforge-session": SESSION},
                   json=HELLO)
        assert r.status_code == 502, r.text
        row = _only(c)
        assert row.status == "backend_failed"
        assert row.session == SESSION


def test_header_value_is_stored_on_a_real_token_stream(monkeypatch):
    install_dual_fake(monkeypatch)
    with _client() as c:
        app = onboard(c, "sess-stream", stream_mode="stream_unverified")
        with c.stream("POST", "/v1/chat/completions",
                      headers={**app.headers, "x-routeforge-session": SESSION},
                      json={**HELLO, "stream": True}) as r:
            assert r.status_code == 200
            "".join(r.iter_text())
        row = _only(c)
        assert row.status == "ok"
        assert row.session == SESSION


def test_invalid_header_is_null_and_the_request_is_still_served():
    for bad in ("x" * 129, "has space", "bad/char"):
        with _client() as c:
            r = c.post("/v1/chat/completions", headers={**DEMO, "x-routeforge-session": bad},
                       json=HELLO)
            assert r.status_code == 200, (bad, r.text)
            assert _only(c).session is None, bad


def test_derived_session_id_is_never_stored():
    with _client() as c:
        assert c.app.state.settings.serving.derive_session_id is True
        r = c.post("/v1/chat/completions", headers=DEMO, json=HELLO)
        assert r.status_code == 200, r.text
        assert _only(c).session is None


def test_export_line_carries_the_session():
    with _client() as c:
        r = c.post("/v1/chat/completions", headers={**DEMO, "x-routeforge-session": SESSION},
                   json=HELLO)
        assert r.status_code == 200, r.text
        _login(c)
        out = c.get("/admin/outcomes/export")
        assert out.status_code == 200, out.text
        lines = [json.loads(x) for x in out.text.strip().splitlines()]
        assert [x["outcome"]["session"] for x in lines] == [SESSION]
