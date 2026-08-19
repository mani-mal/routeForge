"""Task 1 (F1 Phase C): mode-aware `display` block on the request record.

`resp["display"]` is additive console metadata attached before
`history.record(...)` in the `/v1/chat/completions` handler — see
`app/api/routes.py:~248`. It is NOT a telemetry egress; `export_trace` is
unchanged. `choices[]` stays raw for the calling client.
"""
from tests.test_admin_api import _login
from tests._litellm_fake import make_response


def _make_app_with_mode(client, mode):
    """Create an app, set capture_io=mode, return (app_id, token)."""
    _login(client)
    app_id = f"disp-{mode}"
    r = client.post("/admin/applications",
                    json={"id": app_id, "name": f"Disp {mode}",
                          "owner_email": "a@b.c"})
    assert r.status_code == 200, r.text
    token = r.json()["token"]
    pr = client.put(f"/admin/applications/{app_id}/policy",
                    json={"capture_io": mode})
    assert pr.status_code == 200, pr.text
    return app_id, token


def _chat(client, token, text):
    r = client.post("/v1/chat/completions",
                    headers={"Authorization": f"Bearer {token}"},
                    json={"model": "auto", "messages": [{"role": "user", "content": text}]})
    assert r.status_code == 200, r.text
    return r.json()


def test_display_redacted_masks_both_bodies(client, monkeypatch):
    # The autouse fake backend always returns a fixed "ok" completion, which
    # would trivially lack the SSN regardless of redaction -- that wouldn't
    # prove masking. Echo the raw prompt (SSN included) back as the
    # completion so `display.output` only lacks the raw digits if `redact()`
    # actually did something.
    from app.gateway.backends import litellm_backend

    async def _echo(**kwargs):
        last = kwargs["messages"][-1]["content"]
        return make_response(content=f"noted: {last}")

    monkeypatch.setattr(litellm_backend, "acompletion", _echo)

    app_id, token = _make_app_with_mode(client, "redacted")
    resp = _chat(client, token, "my ssn is 123-45-6789 please help")
    rid = resp["id"]
    _login(client)
    detail = client.get(f"/api/requests/{rid}").json()
    disp = detail["display"]
    assert disp["mode"] == "redacted"
    assert disp["input"] is not None and disp["output"] is not None
    assert "123-45-6789" not in disp["input"]   # raw entity never in the display input
    assert "123-45-6789" not in disp["output"]  # completion masked too
    # Prove masking actually happened rather than the entity being absent by
    # construction: the echoed completion carried the SSN, so a redaction
    # placeholder must be present in its place. `input` comes from the gate's
    # anonymize() (placeholder form `<US_SSN_1>`); `output` comes from
    # `redact()` (placeholder form `<US_SSN>`) -- both are the US_SSN type,
    # just different placeholder shapes from two different redaction paths.
    assert "US_SSN" in disp["output"]
    assert "US_SSN" in disp["input"]


def test_display_off_shows_no_bodies(client):
    app_id, token = _make_app_with_mode(client, "off")
    resp = _chat(client, token, "hello there")
    rid = resp["id"]
    _login(client)
    detail = client.get(f"/api/requests/{rid}").json()
    assert detail["display"]["mode"] == "off"
    assert detail["display"]["input"] is None
    assert detail["display"]["output"] is None
