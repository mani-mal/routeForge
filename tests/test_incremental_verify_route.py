"""Task 7: `incremental_verify` route wiring (real SSE off a TestClient).

A benign PROMPT (so the request is NOT floored -- an onprem_only/verify-required
request would StreamFallback to verified_then_stream) whose streamed COMPLETION
contains a US SSN split across backend deltas. The hold-back StreamingVerifier
must ensure the released SSE bytes carry `<US_SSN_1>` and NEVER the raw entity,
and the durable trace records step 9 as performed=True / reason="incremental".

Detector backend under test: `RegexDetector` (conftest sets
`SP_PII_DETECTION=regex`; no Presidio needed -- US_SSN is a regex pattern).
"""
from __future__ import annotations

from app.gateway.backends import litellm_backend
from tests._litellm_fake import make_streaming_acompletion
from tests.test_admin_api import _login
from tests.test_stream_unverified import _make_app, _parse_sse, _verify_record

# SSN "123-45-6789" arrives split across the 3rd/4th deltas; padding on either
# side exceeds the hold-back window so real incremental release is exercised.
_DELTAS = ["Sure thing! ", "The demo record number is ", "123-45-",
           "6789", ", which is stored safely for the walkthrough later on."]


def test_incremental_verify_redacts_split_ssn_and_records_incremental(client, monkeypatch):
    monkeypatch.setattr(litellm_backend, "acompletion",
                        make_streaming_acompletion(_DELTAS))

    token = _make_app(client, "iv-ssn", stream_mode="incremental_verify")
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

    # Concatenate everything the client actually received across content frames.
    released = "".join(f["choices"][0]["delta"].get("content", "")
                       for f in frames[1:-2])
    assert "123-45-6789" not in released      # raw entity never released
    assert "123-45-" not in released          # not even the raw prefix
    assert "<US_SSN_1>" in released           # surfaced redacted, intake-style
    # The benign surrounding text still came through.
    assert "demo record number" in released

    # Durable trace: step 9 recorded as performed incrementally (NOT waived,
    # NOT a complete-response verdict).
    request_id = frames[0]["id"]
    _login(client)
    snap = client.get(f"/api/requests/{request_id}")
    assert snap.status_code == 200, snap.text
    vrec = _verify_record(snap.json()["trace"])
    assert vrec["performed"] is True
    assert vrec["reason"] == "incremental"

    # A durable usage row was recorded for the streamed request.
    usage = client.get("/admin/usage?application_id=iv-ssn").json()
    assert usage["count"] == 1


def test_incremental_verify_leaves_a_raw_client_output_app_untouched(client, monkeypatch):
    """The `client_output: raw` direction of the same wiring (design P1a §2.5).

    The app NAMES a detector, so one really runs at intake -- the hold-back
    verifier therefore has everything it needs to over-redact, and must not.
    Over-redaction is this repo's named recurring defect class: a "simplified"
    `run_stream` that handed the verifier `det` unconditionally would turn a
    passthrough customer's own load-bearing values into `<US_SSN_1>` with the
    suite still green. The released bytes must equal the backend deltas exactly.
    """
    monkeypatch.setattr(litellm_backend, "acompletion",
                        make_streaming_acompletion(_DELTAS))

    _login(client)
    created = client.post("/admin/applications",
                          json={"id": "iv-raw-out", "name": "iv-raw-out"})
    assert created.status_code == 200, created.text
    token = created.json()["token"]
    pr = client.put("/admin/applications/iv-raw-out/policy",
                    json={"stream_mode": "incremental_verify",
                          "client_output": "raw", "pii_detection": "regex"})
    assert pr.status_code == 200, pr.text

    r = client.post("/v1/chat/completions",
                    headers={"Authorization": f"Bearer {token}"},
                    json={"model": "auto",
                          "messages": [{"role": "user", "content": "hi there"}],
                          "stream": True})
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("text/event-stream")

    frames = _parse_sse(r.text)
    assert frames[-1] == "[DONE]"
    released = "".join(f["choices"][0]["delta"].get("content", "")
                       for f in frames[1:-2])
    assert released == "".join(_DELTAS)       # byte-identical, hold-back included
    assert "123-45-6789" in released          # the real value reached the client
    assert "<US_SSN" not in released          # ...and nothing was masked
