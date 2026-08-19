"""Task 1 (F1 Phase D): gated pre-verify raw-completion capture into a
`full`-eligible app's OWN residency-local Langfuse project ONLY.

Proves the load-bearing privacy property (CLAUDE.md rules 2/6/8):
1. A `full`-eligible app's export `output.value` carries the RAW (pre-verify)
   completion -- the PII entity the model leaked is present.
2. The client response (`choices[0].message.content`) stays MASKED (step 9
   verification runs unconditionally, regardless of capture_io).
3. `raw_completion` is NEVER a key on the returned client `resp` -- it is
   popped in routes.py before `resp` is serialized to the client.
4. `resp["display"]["output"]` stays MASKED (Phase C honesty -- display never
   shows raw, even for a full-eligible app; routes.py's display block is
   untouched by this task).
5. A `redacted` app's export `output.value` stays MASKED too -- raw never
   crosses into a shared/other project.

Why the backend fake here is NOT `tests/test_request_display.py::_echo_backend`:
by the time a request reaches the backend it has already been through the
step-2 gate (`app/policy/gate.py::Gate.evaluate`), which unconditionally
anonymizes every detected entity into a placeholder BEFORE the sanitized
message is sent onward (confirmed empirically: the gate's `det.anonymize`
turns "please repeat: 482-73-1946" into "please repeat: <US_SSN_1>", and only
that sanitized text ever reaches the backend). An "echo" backend therefore can
never surface a raw SSN literal in its completion -- there is no raw SSN left
to echo by the time the backend is called. This test instead uses a "leaky
model" backend that returns a FIXED completion containing an SSN regardless of
what it was sent -- exactly the CLAUDE.md rule-2 scenario ("model completions
leak PII the input gate never saw") that step 9 verification exists to catch,
and that this task's raw-completion capture exists to preserve pre-verification
for the app's own project.
"""
from __future__ import annotations

import pytest

from tests._litellm_fake import make_response
from tests.test_admin_api import _login

pytest.importorskip("opentelemetry.sdk.trace")
from opentelemetry.sdk.trace import TracerProvider  # noqa: E402
from opentelemetry.sdk.trace.export import SimpleSpanProcessor  # noqa: E402
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (  # noqa: E402
    InMemorySpanExporter,
)

import app.obs.export as export_mod  # noqa: E402
import app.obs.otel as otel  # noqa: E402
from tests.test_capture_io import _wire  # shared-tracer span-capture harness

_SSN = "482-73-1946"


def _leaky_backend(monkeypatch, content: str) -> None:
    """Monkeypatch the litellm backend to return a FIXED completion containing
    a PII entity, regardless of the (already-sanitized) prompt it receives --
    see module docstring for why this, not `_echo_backend`, is the correct
    fake for proving this property."""
    from app.gateway.backends import litellm_backend

    async def _leaky(**kwargs):
        return make_response(content=content)

    monkeypatch.setattr(litellm_backend, "acompletion", _leaky)


def _wire_app_tracer(monkeypatch):
    """Wire an InMemorySpanExporter in place of the REAL per-app Langfuse
    tracer (`app.obs.export.get_app_tracer`), built the same way the real
    per-app provider is (`RedactingSpanExporter(..., skip_io=True)`, per
    `app/obs/otel.py::get_app_tracer`) -- so this exercises the same code path
    a real full-eligible app's own project goes through, without a live
    Langfuse endpoint. Mirrors
    `tests/test_capture_io.py::test_full_per_app_tracer_builds_still_stores_raw`.
    """
    mem = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(
        SimpleSpanProcessor(otel.RedactingSpanExporter(mem, skip_io=True)))
    monkeypatch.setattr(export_mod, "get_app_tracer",
                        lambda *a, **k: provider.get_tracer("t"))
    return mem, provider


def _output_value_for(mem, request_id: str) -> str:
    for span in mem.get_finished_spans():
        if span.name == "serving.request" and \
                span.attributes.get("sp.request_id") == request_id:
            return span.attributes.get("output.value", "")
    raise AssertionError(f"no serving.request span found for request_id={request_id!r}")


def _make_app(client, app_id: str, *, capture_io: str, with_langfuse: bool) -> str:
    """Create an app + token, optionally provision its own Langfuse project
    (via the manual per-app creds endpoint, B15), then set capture_io. Returns
    the raw bearer token."""
    _login(client)
    r = client.post("/admin/applications",
                    json={"id": app_id, "name": app_id, "owner_email": "a@b.c"})
    assert r.status_code == 200, r.text
    token = r.json()["token"]
    if with_langfuse:
        lf = client.put(f"/admin/applications/{app_id}/langfuse",
                        json={"host": "http://lf.test:3000", "public_key": "pub",
                              "secret_key": "sek", "project_id": f"proj-{app_id}"})
        assert lf.status_code == 200, lf.text
    pr = client.put(f"/admin/applications/{app_id}/policy",
                    json={"capture_io": capture_io})
    # Would 422 for capture_io="full" if the app weren't genuinely
    # full-eligible (non-onprem residency + own Langfuse project) --
    # app/api/admin_routes.py::set_policy.
    assert pr.status_code == 200, pr.text
    return token


def test_full_export_stores_pre_verify_raw_completion(client, monkeypatch):
    leaked = f"noted: {_SSN}"
    _leaky_backend(monkeypatch, leaked)
    mem, provider = _wire_app_tracer(monkeypatch)

    token = _make_app(client, "raw-full", capture_io="full", with_langfuse=True)
    r = client.post("/v1/chat/completions",
                    headers={"Authorization": f"Bearer {token}"},
                    json={"model": "auto",
                          "messages": [{"role": "user", "content": "please repeat it back"}]})
    assert r.status_code == 200, r.text
    resp = r.json()

    # (2) client reply is MASKED (step-9 verified) -- raw entity absent.
    assert _SSN not in resp["choices"][0]["message"]["content"]
    # (3) raw_completion is NOT a key on the returned client resp (popped).
    assert "raw_completion" not in resp
    # (4) display stays masked (Phase C honesty) -- display never shows raw.
    assert _SSN not in (resp["display"]["output"] or "")

    # (1) BUT the app's own residency-local export output.value has the RAW
    # (pre-verify) completion.
    provider.force_flush()
    out = _output_value_for(mem, resp["id"])
    assert _SSN in out


def test_redacted_export_never_gets_raw_completion(client, monkeypatch):
    leaked = f"noted: {_SSN}"
    _leaky_backend(monkeypatch, leaked)
    mem, provider = _wire(monkeypatch, redacting=True)  # shared tracer, no app project

    token = _make_app(client, "raw-redacted", capture_io="redacted", with_langfuse=False)
    r = client.post("/v1/chat/completions",
                    headers={"Authorization": f"Bearer {token}"},
                    json={"model": "auto",
                          "messages": [{"role": "user", "content": "please repeat it back"}]})
    assert r.status_code == 200, r.text
    resp = r.json()
    assert _SSN not in resp["choices"][0]["message"]["content"]

    # (5) masked in redacted mode -- raw never crosses into export at all.
    provider.force_flush()
    out = _output_value_for(mem, resp["id"])
    assert _SSN not in out
