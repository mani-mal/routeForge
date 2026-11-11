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


def force_full_capture_with_enforced_client(monkeypatch) -> None:
    """Make the EXPORT boundary see `capture_io: full` for an app whose
    compiled policy is otherwise `enforced` with a BUILT-IN detector running.

    Since B31 no stored policy reaches that combination. (A `full` app CAN
    still have its client output redacted: off-prem, `pii_detection: off` +
    `enforced` + a privacy profile's custom recognizers is legal, and those
    recognizers still redact egress -- see
    `test_full_export_with_custom_recognizers_keeps_client_redacted`, which
    drives that real configuration.) This patch keeps a backstop for the
    built-in-detector case: if `full` ever coincides with it, the raw
    pre-verify values reach ONLY the app's own trace, never the client body or
    `display`. Patched on `app.api.routes` alone, so the pipeline still runs
    the app's real `enforced` posture."""
    import dataclasses

    import app.api.routes as routes_mod
    real = routes_mod.compile_policy

    def _full(app_policy, settings):
        eff = real(app_policy, settings)
        return dataclasses.replace(eff, protection=dataclasses.replace(
            eff.protection, telemetry_io="full"))
    monkeypatch.setattr(routes_mod, "compile_policy", _full)


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
        lf = client.put(f"/admin/applications/{app_id}/langfuse?verify=false",
                        json={"host": "http://lf.test:3000", "public_key": "pub",
                              "secret_key": "sek", "project_id": f"proj-{app_id}"})
        assert lf.status_code == 200, lf.text
    pr = client.put(f"/admin/applications/{app_id}/policy",
                    json={"capture_io": capture_io})
    # Would 422 for capture_io="full" if the app weren't genuinely
    # full-eligible (own Langfuse project + declared no-protection posture)
    # -- app/policy/compile.py::validate_effective.
    assert pr.status_code == 200, pr.text
    return token


def test_full_export_stores_pre_verify_raw_completion(client, monkeypatch):
    leaked = f"noted: {_SSN}"
    _leaky_backend(monkeypatch, leaked)
    mem, provider = _wire_app_tracer(monkeypatch)

    force_full_capture_with_enforced_client(monkeypatch)
    token = _make_app(client, "raw-full", capture_io="redacted", with_langfuse=True)
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


def test_full_export_with_custom_recognizers_keeps_client_redacted(client, monkeypatch):
    """The REAL legal configuration in which `full` capture and a redacted
    client reply coexist after B31: off-prem, `pii_detection: off` (a declared
    posture there) + `enforced` + a privacy profile with a custom recognizer.
    The recognizer still runs on egress (`EffectiveDetector`, pipeline), so the
    client never sees the value -- while the app's own trace keeps the raw,
    pre-verify completion. No patching of the policy."""
    acct = "ACCT-482731"
    _leaky_backend(monkeypatch, f"noted: {acct}")
    mem, provider = _wire_app_tracer(monkeypatch)

    _login(client)
    r = client.post("/admin/applications",
                    json={"id": "raw-full-custom", "name": "raw-full-custom",
                          "owner_email": "a@b.c"})
    assert r.status_code == 200, r.text
    token = r.json()["token"]
    assert client.put("/admin/applications/raw-full-custom/langfuse?verify=false",
                      json={"host": "http://lf.test:3000", "public_key": "pub",
                            "secret_key": "sek",
                            "project_id": "proj-raw-full-custom"}).status_code == 200
    assert client.put("/admin/applications/raw-full-custom/privacy", json={
        "recognizers": [{"name": "ACCOUNT_ID", "regex": r"\bACCT-\d{6}\b",
                         "score": 0.9, "enabled": True}],
        "rules": []}).status_code == 200
    pr = client.put("/admin/applications/raw-full-custom/policy",
                    json={"residency": "any", "policy_mode": "enforced",
                          "pii_detection": "off", "capture_io": "full"})
    assert pr.status_code == 200, pr.text

    r = client.post("/v1/chat/completions",
                    headers={"Authorization": f"Bearer {token}"},
                    json={"model": "auto",
                          "messages": [{"role": "user", "content": "please repeat it back"}]})
    assert r.status_code == 200, r.text
    resp = r.json()
    assert acct not in resp["choices"][0]["message"]["content"]   # egress redacted
    assert "raw_completion" not in resp
    assert acct not in (resp["display"]["output"] or "")
    provider.force_flush()
    assert acct in _output_value_for(mem, resp["id"])              # raw, own project


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


def test_full_without_app_langfuse_downgrades_to_redacted(monkeypatch):
    """Defense-in-depth (coordinator review, task 1): export_trace must
    enforce its OWN full+own-project invariant rather than trusting the
    upstream clamp in routes.py alone. Today routes.py already guarantees
    `capture_io == "full"` implies `app_langfuse is not None` for the only
    production caller -- but the export boundary is where the raw completion
    actually gets authored onto a span, so it must fail closed independently:
    a caller that (by a future bug) passes capture_io='full' with no per-app
    Langfuse project must still get 'redacted' behaviour, never a raw leak
    onto the shared project."""
    mem, provider = _wire(monkeypatch, redacting=True)
    leaked = f"noted: {_SSN}"
    export_mod.export_trace(
        request_id="req-guard-1", tenant_id="acme", model="local-32b",
        cost={"total_usd": 0.0001, "baseline_usd": 0.001},
        policy={"sensitivity": "internal", "onprem_only": False,
                "allowed_providers": ["onprem"], "detected_entities": []},
        trace=[],
        capture_io="full", app_langfuse=None,
        prompt="please repeat it back", completion=leaked,
        raw_completion=leaked)
    provider.force_flush()
    out = _output_value_for(mem, "req-guard-1")
    assert _SSN not in out  # downgraded to redacted -- never raw on the shared project
