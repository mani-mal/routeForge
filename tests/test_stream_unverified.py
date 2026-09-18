"""Task 6: `stream_unverified` route wiring + runtime floor fallback.

`stream_unverified` streams REAL backend token deltas (litellm `stream=True`)
and WAIVES step-9 verification -- but only when `pipeline.run_stream` returns
normally, which it does exactly when the request is NOT onprem_only and NOT
require_verification. A request that trips the runtime floor (Global
Constraint 1) raises `StreamFallback` inside the pipeline and the route
transparently falls back to `verified_then_stream` (the COMPLETE response is
verified, then chunked) -- indistinguishable to the privacy guarantee.

Every streamed request still emits a DecisionRecord + durable usage row via
`_finalize` (Global Constraint 2), including on a mid-stream client disconnect.

Assertions are made against REAL SSE bytes off a real `TestClient`, never
mocks of the route internals.
"""
from __future__ import annotations

import json

import pytest

from app.gateway.backends import litellm_backend
from tests._litellm_fake import make_streaming_acompletion
from tests.test_admin_api import _login

_DELTAS = ["Hello", ", ", "streamed", " ", "world", "!"]


def _make_app(client, app_id: str, *, stream_mode: str) -> str:
    _login(client)
    r = client.post("/admin/applications", json={"id": app_id, "name": app_id})
    assert r.status_code == 200, r.text
    token = r.json()["token"]
    pr = client.put(f"/admin/applications/{app_id}/policy",
                    json={"stream_mode": stream_mode})
    assert pr.status_code == 200, pr.text
    return token


def _parse_sse(text: str) -> list[dict | str]:
    frames: list[dict | str] = []
    for block in text.strip("\n").split("\n\n"):
        block = block.strip()
        if not block:
            continue
        assert block.startswith("data: "), block
        payload = block[len("data: "):]
        frames.append("[DONE]" if payload == "[DONE]" else json.loads(payload))
    return frames


def _content(frames: list) -> str:
    return "".join(f["choices"][0]["delta"]["content"]
                   for f in frames[1:-2])  # drop role, finish, [DONE]


def _verify_record(trace: list[dict]) -> dict:
    recs = [r for r in trace if r.get("step") == "verify"]
    assert len(recs) == 1, trace
    return recs[0]


# --------------------------------------------------------------------------- #
# (a) benign prompt -> REAL streamed deltas, verify WAIVED, usage recorded
# --------------------------------------------------------------------------- #
def test_stream_unverified_real_deltas_waived_and_usage(client, monkeypatch):
    from tests import _litellm_fake

    monkeypatch.setattr(litellm_backend, "acompletion",
                        make_streaming_acompletion(_DELTAS))
    _litellm_fake.CALLS.clear()

    token = _make_app(client, "su-benign", stream_mode="stream_unverified")
    r = client.post("/v1/chat/completions",
                    headers={"Authorization": f"Bearer {token}"},
                    json={"model": "auto",
                          "messages": [{"role": "user", "content": "hi there"}],
                          "stream": True})
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("text/event-stream")

    frames = _parse_sse(r.text)
    assert frames[0]["choices"][0]["delta"] == {"role": "assistant"}
    assert frames[-1] == "[DONE]"
    assert frames[-2]["choices"][0]["finish_reason"] == "stop"
    # REAL streaming forwards the backend deltas one-for-one (NOT post-hoc
    # word-boundary chunking of a finished string) -- the strongest signal
    # this took the live path, not the verified_then_stream fallback.
    content_deltas = [f["choices"][0]["delta"]["content"] for f in frames[1:-2]]
    assert content_deltas == _DELTAS
    assert _content(frames) == "".join(_DELTAS)

    # The backend was really asked to stream.
    assert _litellm_fake.CALLS and _litellm_fake.CALLS[-1]["stream"] is True

    # Decision trace (durable snapshot) shows verification WAIVED for this mode.
    request_id = frames[0]["id"]
    _login(client)
    snap = client.get(f"/api/requests/{request_id}")
    assert snap.status_code == 200, snap.text
    vrec = _verify_record(snap.json()["trace"])
    assert vrec["performed"] is False
    assert vrec["passed"] is True
    assert vrec["reason"] == "waived: stream_unverified"

    # A durable usage row was recorded for the streamed request.
    usage = client.get("/admin/usage?application_id=su-benign").json()
    assert usage["count"] == 1


# --------------------------------------------------------------------------- #
# (b) require_verification/onprem -> falls back to verified_then_stream:
#     verification IS performed, output still streamed
# --------------------------------------------------------------------------- #
def test_stream_unverified_falls_back_when_floored(client):
    # A US_SSN trips the default `restricted` rule -> onprem_only +
    # require_verification -> run_stream raises StreamFallback -> the route
    # runs the FULL verified pipeline and chunks the finished text. The autouse
    # non-streaming fake (conftest) serves the fallback's pipeline.run() call.
    token = _make_app(client, "su-floored", stream_mode="stream_unverified")
    r = client.post("/v1/chat/completions",
                    headers={"Authorization": f"Bearer {token}"},
                    json={"model": "auto",
                          "messages": [{"role": "user",
                                        "content": "my ssn is 482-73-1946 please help"}],
                          "stream": True})
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("text/event-stream")

    frames = _parse_sse(r.text)
    assert frames[-1] == "[DONE]"
    assert _content(frames)  # streamed a (verified) answer

    request_id = frames[0]["id"]
    _login(client)
    snap = client.get(f"/api/requests/{request_id}").json()
    # Verification was PERFORMED on the complete response -- NOT waived. The
    # floor was never weakened by the streaming mode.
    vrec = _verify_record(snap["trace"])
    assert vrec["performed"] is True
    assert vrec["reason"] != "waived: stream_unverified"
    assert snap["policy"]["onprem_only"] is True


# --------------------------------------------------------------------------- #
# mid-stream disconnect: usage/decision still finalize (best-effort)
# --------------------------------------------------------------------------- #
def test_stream_unverified_finalizes_on_disconnect(client, monkeypatch):
    """Reading only the first SSE frame and closing early simulates a client
    that disconnects mid-stream. `_finalize` must still run (in the generator's
    `finally`, draining the remaining backend deltas) so the usage row is not
    lost -- a Task 5 reviewer note."""
    monkeypatch.setattr(litellm_backend, "acompletion",
                        make_streaming_acompletion(_DELTAS))
    token = _make_app(client, "su-disc", stream_mode="stream_unverified")

    with client.stream("POST", "/v1/chat/completions",
                       headers={"Authorization": f"Bearer {token}"},
                       json={"model": "auto",
                             "messages": [{"role": "user", "content": "hi there"}],
                             "stream": True}) as r:
        assert r.status_code == 200
        for _line in r.iter_lines():
            break  # read one frame, then abandon the stream

    # Despite the early close, accounting closed out.
    _login(client)
    usage = client.get("/admin/usage?application_id=su-disc").json()
    assert usage["count"] == 1


# --------------------------------------------------------------------------- #
# (d) client_output: redact -- refused at WRITE time, floored at RUNTIME
#
# Design P1a §2.5: egress redaction is `client_output`'s call, and
# `stream_unverified` emits backend tokens as they arrive, so no redaction can
# run over the complete response. An app that NAMED `client_output: redact` has
# asked for a guarantee this mode cannot keep. The admin API refuses the write
# (`STREAM_UNVERIFIED_FORBIDDEN_REDACTED_OUTPUT`); the runtime floor is the
# backstop for a row that exists anyway (written before the rule shipped, or
# straight into the DB).
# --------------------------------------------------------------------------- #
def test_stream_unverified_write_refused_when_client_output_is_redact(client):
    _login(client)
    created = client.post("/admin/applications",
                          json={"id": "su-conflict", "name": "su-conflict"})
    assert created.status_code == 200, created.text
    r = client.put("/admin/applications/su-conflict/policy",
                   json={"stream_mode": "stream_unverified",
                         "client_output": "redact"})
    assert r.status_code == 422, r.text
    assert "client_output" in r.json()["detail"]


def test_stream_unverified_floors_when_client_output_is_redact(client, monkeypatch):
    """The runtime backstop, on a request nothing ELSE would floor: residency
    `any`, no PII in the prompt, so neither `onprem_only` nor
    `require_verification` is set. The only reason this must not stream raw
    backend tokens is the app's explicit `client_output: redact`.

    The policy is written past the admin API's refusal on purpose -- the point
    is that even a row that should not exist cannot leak."""
    from app.api import admin_routes

    _login(client)
    created = client.post("/admin/applications",
                          json={"id": "su-redact-out", "name": "su-redact-out"})
    assert created.status_code == 200, created.text
    token = created.json()["token"]
    with monkeypatch.context() as m:
        m.setattr(admin_routes, "validate_effective", lambda *a, **k: None)
        pr = client.put("/admin/applications/su-redact-out/policy",
                        json={"residency": "any", "stream_mode": "stream_unverified",
                              "client_output": "redact"})
        assert pr.status_code == 200, pr.text

    r = client.post("/v1/chat/completions",
                    headers={"Authorization": f"Bearer {token}"},
                    json={"model": "auto",
                          "messages": [{"role": "user", "content": "hi there"}],
                          "stream": True})
    assert r.status_code == 200, r.text
    frames = _parse_sse(r.text)
    assert frames[-1] == "[DONE]"
    assert _content(frames)

    request_id = frames[0]["id"]
    _login(client)
    snap = client.get(f"/api/requests/{request_id}").json()
    # Nothing but `client_output` could have floored this request...
    assert snap["policy"]["onprem_only"] is False
    # ...and step 9 was NOT waived: the full pipeline ran and the finished,
    # egress-checked text was chunked out (verified_then_stream).
    vrec = _verify_record(snap["trace"])
    assert vrec["reason"] != "waived: stream_unverified"
