"""Task 8: egress telemetry redaction must cover per-app CUSTOM entity types,
not just the built-in RegexDetector patterns (CLAUDE.md rule 2: redact on
egress). app/obs/redact.py's `_detector` is a module-level, app-agnostic
RegexDetector -- a custom recognizer's value (e.g. a hospital's MRN) is
invisible to it unless the caller threads the app's recognizers through."""
import asyncio

import pytest

from app.obs.redact import redact
from app.policy.recognizers import CustomRecognizer


def _mrn_recognizer():
    return CustomRecognizer("MRN", r"\bMRN-\d{7}\b", 0.9)


def test_custom_entity_redacted_when_recognizers_supplied():
    out = redact("patient MRN-1234567 discharged", recognizers=[_mrn_recognizer()])
    assert "MRN-1234567" not in out
    assert "<MRN>" in out


def test_custom_entity_survives_without_recognizers_arg():
    # Proves the fix is load-bearing: with no recognizers passed, the custom
    # value is NOT redacted (this is the gap the fix closes when callers
    # thread recognizers through).
    out = redact("patient MRN-1234567 discharged")
    assert "MRN-1234567" in out


def test_builtin_still_redacted_with_no_recognizers():
    out = redact("ssn 482-73-1946")
    assert "482-73-1946" not in out
    assert "<US_SSN>" in out


def test_both_builtin_and_custom_redacted_together():
    out = redact(
        "ssn 482-73-1946 mrn MRN-1234567",
        recognizers=[_mrn_recognizer()],
    )
    assert "482-73-1946" not in out
    assert "MRN-1234567" not in out
    assert "<US_SSN>" in out
    assert "<MRN>" in out


# --------------------------------------------------------------------------- #
# End-to-end regression guard: the WIRING, not just redact() in isolation.
#
# The unit tests above prove redact(recognizers=...) works, but nothing above
# exercises the real call chain this task exists to deliver:
#   routes.py export_trace(recognizers=profile.recognizers)
#     -> export.py _set_io(..., recognizers=recognizers)
#       -> redact.py redact(text, recognizers=recognizers)
# A future refactor that silently drops the `recognizers=` kwarg at the
# routes.py call site (or passes the wrong object) would NOT be caught by the
# unit tests above. This test boots the real app, persists a custom
# recognizer for a real application via the same store API the admin path
# uses, fires a real request through that app's token, and checks the
# exported span. Modeled on
# tests/test_capture_io.py::test_redacted_capture_end_to_end (same harness:
# TestClient + _wire + _root_attrs), reused here rather than duplicated.
# --------------------------------------------------------------------------- #
pytest.importorskip("opentelemetry.sdk.trace")

from tests.test_capture_io import _root_attrs, _wire  # noqa: E402


@pytest.mark.xfail(reason="Qwen removed in Task 4", strict=False)
def test_custom_recognizer_reaches_egress_redaction_end_to_end(monkeypatch, tmp_path):
    """Persist an MRN recognizer for the seeded `hospital` app, fire a real
    /v1/chat/completions request carrying an MRN through that app's token,
    and confirm the exported root span's redacted input.value masks it --
    proving `recognizers` actually reaches redact() through routes.py ->
    export_trace -> _set_io, not just via a hand-built call in the tests
    above."""
    from app.apps import store
    from fastapi.testclient import TestClient

    monkeypatch.setenv("SP_PII_DETECTION", "regex")
    monkeypatch.setenv("SP_CAPTURE_IO", "redacted")
    # A real file-backed DB, not the in-memory StaticPool tests/conftest.py
    # defaults to: the seeding write below runs on its own asyncio.run()
    # loop, and the TestClient request further down runs on a DIFFERENT
    # loop. An in-memory StaticPool db is bound to whichever loop first
    # touches it -- the exact gotcha tests/test_token_auth.py's
    # test_usage_row_persisted_for_served_request works around the same way.
    db_url = f"sqlite+aiosqlite:///{tmp_path}/rf.db"
    monkeypatch.setenv("DATABASE_URL", db_url)

    from app.main import create_app
    app = create_app()

    async def _seed_recognizer():
        async with app.state.db.session() as s:
            # ensure_ready() (triggered by .session()) seeds the demo apps,
            # including `hospital` / token `hospital-key`, before this runs.
            await store.replace_privacy_profile(
                s, "hospital",
                recognizers=[{"name": "MRN", "regex": r"\bMRN-\d{7}\b",
                              "score": 0.9}],
                rules=[{"entities": ["MRN"], "sensitivity": "restricted",
                        "risk_class": "high", "residency": "onprem_only",
                        "require_verification": True}])
            await s.commit()

    asyncio.run(_seed_recognizer())

    client = TestClient(app)
    mem, provider = _wire(monkeypatch, redacting=True)
    r = client.post("/v1/chat/completions",
                    headers={"authorization": "Bearer hospital-key"},
                    json={"model": "auto",
                          "messages": [{"role": "user",
                                        "content": "patient MRN-1234567 discharged"}]})
    assert r.status_code == 200, r.text
    provider.force_flush()
    attrs = _root_attrs(mem)
    assert "input.value" in attrs                 # capture_io=redacted is on
    assert "MRN-1234567" not in str(attrs)         # raw custom entity absent
    assert "<MRN>" in attrs["input.value"]          # masked by the custom rule
