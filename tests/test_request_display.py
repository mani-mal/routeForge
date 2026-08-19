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


def _echo_backend(monkeypatch, prefix="noted"):
    """Monkeypatch the litellm backend to echo the last user message back as
    the completion. The autouse fake backend always returns a fixed "ok",
    which would trivially lack any entity regardless of redaction -- that
    wouldn't prove masking. Echoing the raw prompt back as the completion
    means `display.output` only lacks the raw value if redaction actually
    did something."""
    from app.gateway.backends import litellm_backend

    async def _echo(**kwargs):
        last = kwargs["messages"][-1]["content"]
        return make_response(content=f"{prefix}: {last}")

    monkeypatch.setattr(litellm_backend, "acompletion", _echo)


def test_display_redacted_masks_both_bodies(client, monkeypatch):
    _echo_backend(monkeypatch)

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


def test_display_redacted_threads_app_recognizers_to_redact(client, monkeypatch):
    """Wiring proof, mirroring test_egress_custom_redaction.py's end-to-end
    guard for export_trace: the display block's redact() calls must thread
    `profile.recognizers` through (CLAUDE.md rule 2) -- export_trace already
    gets this right; this proves the display block does too, closing the gap
    a custom entity type (e.g. a hospital's MRN pattern) would otherwise
    leave RAW in the console drilldown while Langfuse correctly masks it.

    A pure content assertion ("raw MRN absent from display.output") does NOT
    discriminate this fix and would be dishonest to rely on alone: step 9
    output verification (app/output/verify.py) unconditionally anonymizes
    the completion, and the privacy gate unconditionally anonymizes
    sanitized_prompt, both using the SAME effective detector (base +
    profile.recognizers) BEFORE either of the display block's redact() calls
    ever runs -- so the raw value never survives to reach them whether or
    not `recognizers=` is threaded there (confirmed empirically: the content
    assertions below still pass with the routes.py fix reverted). Spy on
    `redact()` itself instead, the same way tests/_litellm_fake.py's CALLS
    list proves kwargs reached the backend -- that's the part that actually
    catches a regression.
    """
    import app.api.routes as routes_mod

    calls: list = []
    real_redact = routes_mod.redact

    def _spy(text, recognizers=None):
        calls.append(recognizers)
        return real_redact(text, recognizers=recognizers)

    monkeypatch.setattr(routes_mod, "redact", _spy)
    _echo_backend(monkeypatch)

    _login(client)
    app_id = "disp-redacted-mrn"
    r = client.post("/admin/applications",
                    json={"id": app_id, "name": "Disp MRN", "owner_email": "a@b.c"})
    assert r.status_code == 200, r.text
    token = r.json()["token"]
    assert client.put(f"/admin/applications/{app_id}/policy",
                      json={"capture_io": "redacted"}).status_code == 200
    prof = client.put(f"/admin/applications/{app_id}/privacy",
                      json={"recognizers": [{"name": "MRN",
                                             "regex": r"\bMRN-\d{7}\b",
                                             "score": 0.9}],
                            "rules": []})
    assert prof.status_code == 200, prof.text

    resp = _chat(client, token, "patient MRN-1234567 discharged")
    rid = resp["id"]
    _login(client)
    detail = client.get(f"/api/requests/{rid}").json()
    disp = detail["display"]
    assert disp["mode"] == "redacted"
    # Belt-and-suspenders content check (not the discriminating assertion --
    # see docstring): raw custom entity value absent from both bodies.
    assert "MRN-1234567" not in disp["input"]
    assert "MRN-1234567" not in disp["output"]

    # The discriminating assertion: at least one redact() call the handler
    # made carried the app's MRN recognizer -- proves profile.recognizers
    # reached the display block's redact() calls, not just export_trace's.
    assert any(recs and any(rc.name == "MRN" for rc in recs) for recs in calls), calls


def test_display_full_mode_shows_raw_bodies(monkeypatch, tmp_path):
    # `full` is only reachable for a non-onprem app with its OWN Langfuse
    # project (routes.py's fail-closed clamp) -- neither condition holds for
    # any seeded demo app, so build a dedicated one directly against the DB,
    # same cross-loop-safe pattern as tests/test_capture_io.py's
    # test_redacted_capture_end_to_end / test_egress_custom_redaction.py's
    # end-to-end test: a file-backed DATABASE_URL (not the default in-memory
    # StaticPool, which is bound to a single event loop), seeded via its own
    # asyncio.run() before the TestClient (and its own loop) is created.
    import asyncio

    from fastapi.testclient import TestClient

    from app.apps import store
    from app.apps.langfuse_provision import encrypt_secret
    from app.main import create_app

    monkeypatch.setenv("LANGFUSE_HOST", "http://lf.test:3000")
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path}/rf.db")
    app = create_app()

    app_id, raw_token = "disp-full", "disp-full-token"

    async def _seed():
        async with app.state.db.session() as s:
            await store.create_application(s, app_id=app_id, name="Disp Full")
            await store.add_token(s, application_id=app_id, raw_token=raw_token)
            await store.update_application(
                s, app_id,
                langfuse_project_id="proj-full",
                langfuse_public_key="pub-full",
                langfuse_secret_key_enc=encrypt_secret("sek-full"))
            await s.commit()

    asyncio.run(_seed())

    client = TestClient(app)
    # A raw SSN in `display.output` specifically is NOT achievable through
    # any real completion: step 9 output verification
    # (app/output/verify.py::verify) unconditionally anonymizes the
    # completion text (via detector.anonymize()) before it is ever placed in
    # resp["choices"], independent of capture_io -- confirmed empirically
    # (an echo/leaky backend that emits a raw SSN still comes back through
    # this request as "<US_SSN_1>"). That's correct, load-bearing behaviour
    # (defense in depth for CLAUDE.md rule 2), not a test-harness limitation
    # to route around. What IS honestly provable for `full` mode: (1) the
    # DISPLAY INPUT is the true raw submission (prompt is never touched for
    # storage -- SSN present, unsanitized), and (2) the display block never
    # calls redact() at all on the `full` branch, so `display.output` is an
    # exact pass-through of whatever the pipeline already decided to return
    # -- never re-processed a second time the way `redacted` mode is. Spy on
    # redact() (same technique as the wiring test above) to prove (2)
    # directly rather than via an unreachable content assertion.
    import app.api.routes as routes_mod

    calls: list = []
    real_redact = routes_mod.redact

    def _spy(text, recognizers=None):
        calls.append(text)
        return real_redact(text, recognizers=recognizers)

    monkeypatch.setattr(routes_mod, "redact", _spy)
    _echo_backend(monkeypatch)

    _login(client)
    pr = client.put(f"/admin/applications/{app_id}/policy", json={"capture_io": "full"})
    assert pr.status_code == 200, pr.text  # would 422 if not genuinely full-eligible

    text = "my ssn is 123-45-6789 please help"
    resp = _chat(client, raw_token, text)
    rid = resp["id"]
    _login(client)
    detail = client.get(f"/api/requests/{rid}").json()
    disp = detail["display"]
    assert disp["mode"] == "full"
    # (1) raw input, exactly as submitted -- SSN present, unsanitized.
    assert disp["input"] == text
    assert "123-45-6789" in disp["input"]
    # (2) exact pass-through of the served completion, never redact()-ed.
    assert disp["output"] == resp["choices"][0]["message"]["content"]
    assert calls == []  # redact() must never be invoked on the `full` branch
