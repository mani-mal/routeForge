"""Eval-capture feature: observability.langfuse.capture_io = off | redacted | full.

Attaches request/response I/O to the `serving.request` trace so Langfuse traces
are usable as an eval dataset (current
capture_io contract in docs/reference/per-app-config.md). Gated so it cannot
break CLAUDE.md rule 2 (redact on egress) / rule 6 (residency):

- off      — capture nothing (demo default; Input column blank).
- redacted — attach prompt/completion with entity VALUES masked to <TYPE>.
- full     — attach RAW I/O; config lint REJECTS it when any tenant is
             onprem_only, and the egress redactor then leaves the I/O keys alone.
"""
import copy
import json

import pytest

from app.config.lint import ConfigLintError, enforce
from app.config.loader import load_settings

_SSN = "482-73-1946"


# --------------------------------------------------------------------------- #
# Config: loader + lint guardrail
# --------------------------------------------------------------------------- #
def test_capture_io_defaults_off():
    assert load_settings().serving.observability.langfuse.capture_io == "off"


def test_capture_io_env_override(monkeypatch):
    monkeypatch.setenv("SP_CAPTURE_IO", "REDACTED")  # case-folded by the loader
    assert load_settings().serving.observability.langfuse.capture_io == "redacted"


def test_lint_rejects_unknown_capture_io():
    s = copy.deepcopy(load_settings())
    s.serving.observability.langfuse.capture_io = "raw"  # typo, not a valid mode
    with pytest.raises(ConfigLintError, match="capture_io"):
        enforce(s)


def test_lint_rejects_full_with_onprem_tenant():
    # Default config has the `hospital` tenant as onprem_only, so `full` (raw
    # I/O into a shared Langfuse) must be a boot failure — residency beats eval.
    s = copy.deepcopy(load_settings())
    assert any(t.default_residency == "onprem_only"
               for t in s.policy.tenants.values())  # premise
    s.serving.observability.langfuse.capture_io = "full"
    with pytest.raises(ConfigLintError, match="onprem_only"):
        enforce(s)


def test_lint_allows_full_when_no_onprem_tenant():
    s = copy.deepcopy(load_settings())
    for t in s.policy.tenants.values():
        if t.default_residency == "onprem_only":
            t.default_residency = "private_cloud_ok"
    s.serving.observability.langfuse.capture_io = "full"
    enforce(s)  # must not raise


def test_lint_allows_redacted_with_onprem_tenant():
    s = copy.deepcopy(load_settings())
    s.serving.observability.langfuse.capture_io = "redacted"
    enforce(s)  # redacted is eval-safe for a shared project — never blocked


# --------------------------------------------------------------------------- #
# export_trace: what actually lands on the root span
# --------------------------------------------------------------------------- #
pytest.importorskip("opentelemetry.sdk.trace")
from opentelemetry.sdk.trace import TracerProvider  # noqa: E402
from opentelemetry.sdk.trace.export import SimpleSpanProcessor  # noqa: E402
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (  # noqa: E402
    InMemorySpanExporter,
)

import app.obs.export as export_mod  # noqa: E402
import app.obs.otel as otel  # noqa: E402
from app.policy.detectors import RegexDetector  # noqa: E402


def _wire(monkeypatch, *, redacting: bool):
    mem = InMemorySpanExporter()
    provider = TracerProvider()
    exporter = otel.RedactingSpanExporter(mem) if redacting else mem
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    monkeypatch.setattr(otel, "_provider", provider)
    return mem, provider


def _app_lf():
    """Stand-in for an application's own provisioned Langfuse project -- the
    only place raw I/O may ever land (CLAUDE.md rule 2)."""
    from types import SimpleNamespace
    return SimpleNamespace(host="http://lf", public_key="pk", secret_key="sk",
                           project_id="proj-1")


def _wire_app_tracer(monkeypatch):
    """An InMemorySpanExporter standing in for the app's OWN Langfuse project,
    built exactly as `app/obs/otel.py::get_app_tracer` builds a full-eligible
    one (skip_io=True)."""
    mem = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(
        SimpleSpanProcessor(otel.RedactingSpanExporter(mem, skip_io=True)))
    monkeypatch.setattr(export_mod, "get_app_tracer",
                        lambda *a, **k: provider.get_tracer("t"))
    return mem, provider


def _export(**over):
    # `detector` is the request's own detector (Representations.detector).
    # Supplied by default because `capture_io: redacted` is only legal for an
    # app that actually runs one -- with None, the exporter now fails closed to
    # `off` rather than authoring raw under a redacted label (design §2.6).
    kw = dict(
        request_id="req-1", tenant_id="hospital", model="local-32b",
        cost={"total_usd": 0.0004, "baseline_usd": 0.012},
        policy={"sensitivity": "restricted", "onprem_only": True,
                "allowed_providers": ["onprem"], "detected_entities": ["US_SSN"]},
        trace=[{"step": "route", "chosen": "local-32b", "hop_index": 0}],
        prompt=f"my SSN is {_SSN}", completion=f"noted: {_SSN}",
        detector=RegexDetector(),
    )
    kw.update(over)
    return export_mod.export_trace(**kw)


def _root_attrs(mem):
    root = next(s for s in mem.get_finished_spans() if s.name == "serving.request")
    return dict(root.attributes)


def test_off_captures_no_io(monkeypatch):
    mem, provider = _wire(monkeypatch, redacting=True)
    _export(capture_io="off")
    provider.force_flush()
    attrs = _root_attrs(mem)
    assert "input.value" not in attrs and "output.value" not in attrs


def test_redacted_masks_pii(monkeypatch):
    # Bare exporter (no RedactingSpanExporter) isolates export_trace's OWN
    # redaction — the value must already be masked before egress defence.
    mem, provider = _wire(monkeypatch, redacting=False)
    _export(capture_io="redacted")
    provider.force_flush()
    attrs = _root_attrs(mem)
    assert _SSN not in attrs["input.value"]
    # The app's own detector produces numbered placeholders (`<US_SSN_1>`);
    # the regex egress pass that follows it produces bare `<US_SSN>`. Either
    # shape proves the value was masked, and which one appears depends only on
    # which pass caught it first.
    assert "<US_SSN" in attrs["input.value"]
    assert _SSN not in attrs["output.value"]


def test_full_keeps_raw_io_but_still_redacts_other_keys(monkeypatch):
    # `full` is the residency-local escape hatch: I/O keys survive the egress
    # redactor, but every other free-text key (sp.reason) is still scrubbed.
    # It runs in the app's OWN project -- the only place raw I/O may land
    # (CLAUDE.md rule 2), which is why this wires a per-app tracer.
    mem, provider = _wire_app_tracer(monkeypatch)
    _export(capture_io="full", app_langfuse=_app_lf(),
            trace=[{"step": "route", "chosen": "local-32b",
                    "reason": f"leaked {_SSN} here", "hop_index": 0}])
    provider.force_flush()
    spans = mem.get_finished_spans()
    root = next(s for s in spans if s.name == "serving.request")
    assert _SSN in root.attributes["input.value"]      # raw, on purpose
    assert _SSN in root.attributes["output.value"]
    reason = next(s for s in spans if "sp.reason" in s.attributes)
    assert _SSN not in reason.attributes["sp.reason"]   # other keys still redacted


def test_full_without_an_own_project_never_reaches_the_shared_tracer(monkeypatch):
    # CLAUDE.md rule 2: the shared project NEVER receives raw I/O, whatever the
    # deployment-wide flag says. `full` with no per-app project falls to
    # `redacted` -- and here a detector exists, so a genuinely redacted copy is
    # what lands, built by the exporter from the raw values.
    monkeypatch.setattr(otel, "_capture_io_full", True)  # even if globally armed
    mem, provider = _wire(monkeypatch, redacting=True)
    te = _export(capture_io="full", app_langfuse=None)
    provider.force_flush()
    attrs = _root_attrs(mem)
    assert te.capture_io == "redacted"
    assert attrs["sp.capture_io"] == "redacted"
    assert _SSN not in attrs["input.value"]
    assert "<US_SSN" in attrs["input.value"]


def test_exporter_never_skips_io_redaction_for_a_non_full_span(monkeypatch):
    # The egress net's own guard (app/obs/otel.py). Both of its skip conditions
    # -- the process-global `capture_io: full` and a per-app full exporter --
    # are properties of the EXPORTER, not of the request passing through it. A
    # span that names its own resolved capture can only tighten that, never
    # loosen it, so a `redacted` request can never ride a full exporter's skip.
    monkeypatch.setattr(otel, "_capture_io_full", True)   # globally armed
    for skip_io in (True, False):                          # and per-app armed
        mem = InMemorySpanExporter()
        prov = TracerProvider()
        prov.add_span_processor(
            SimpleSpanProcessor(otel.RedactingSpanExporter(mem, skip_io=skip_io)))
        with prov.get_tracer("t").start_as_current_span("serving.request") as s:
            s.set_attribute("sp.capture_io", "redacted")   # THIS request is not full
            s.set_attribute("input.value", f"raw {_SSN}")
        prov.force_flush()
        got = next(iter(mem.get_finished_spans())).attributes["input.value"]
        assert _SSN not in got, f"skip_io={skip_io}"


# --------------------------------------------------------------------------- #
# End-to-end: a real request through the app exports redacted I/O
# --------------------------------------------------------------------------- #
def test_redacted_capture_end_to_end(monkeypatch):
    from fastapi.testclient import TestClient

    monkeypatch.setenv("SP_PII_DETECTION", "regex")
    monkeypatch.setenv("SP_CAPTURE_IO", "redacted")
    from app.main import create_app
    app = create_app()  # init_tracing runs with capture_io=redacted (flag stays off)
    client = TestClient(app)

    mem, provider = _wire(monkeypatch, redacting=True)
    r = client.post("/v1/chat/completions",
                    headers={"authorization": "Bearer hospital-key"},
                    json={"model": "auto",
                          "messages": [{"role": "user",
                                        "content": f"my SSN is {_SSN}"}]})
    assert r.status_code == 200
    provider.force_flush()
    attrs = _root_attrs(mem)
    assert "input.value" in attrs                 # I/O now captured (was blank)
    assert _SSN not in str(attrs)                 # but redacted — rule 2 holds
    # Telemetry built this copy itself, from the raw prompt, with the app's own
    # detector (design §2.6) -- so it carries that detector's numbered
    # placeholder scheme (<TYPE_N>), not the bare-<TYPE> shape the regex egress
    # pass produces.
    assert "<US_SSN_1>" in attrs["input.value"]


def test_redacted_redacts_the_whole_raw_prompt_not_just_the_last_message(monkeypatch):
    # `raw_prompt` is the gate's FLATTENED raw scan text (every message), and
    # redacted mode redacts THAT rather than the single-message `prompt` the
    # caller also passes -- telemetry reflects the whole request, redacted.
    mem, provider = _wire(monkeypatch, redacting=False)
    _export(capture_io="redacted",
            prompt="please summarize",
            raw_prompt=f"my SSN is {_SSN}\nplease summarize")
    provider.force_flush()
    attrs = _root_attrs(mem)
    assert _SSN not in attrs["input.value"]
    assert "<US_SSN" in attrs["input.value"]
    assert "please summarize" in attrs["input.value"]


def test_redacted_builds_its_own_copy_and_never_authors_raw(monkeypatch):
    # CE-1 at the export boundary. `redacted` is a promise about the content,
    # and the only way to keep it is a copy the exporter built itself from the
    # raw values. With no detector there is no such copy -- and no I/O at all,
    # rather than raw text under a `redacted` label.
    mem, provider = _wire(monkeypatch, redacting=False)
    te = _export(capture_io="redacted", detector=None,
                 raw_prompt=f"my SSN is {_SSN}")
    provider.force_flush()
    attrs = _root_attrs(mem)
    assert te.capture_io == "off"
    assert "input.value" not in attrs and "output.value" not in attrs
    assert _SSN not in str(attrs)


def test_full_keeps_raw_in_its_own_project(monkeypatch):
    # full stores RAW for distillation, in the app's own project only.
    mem, provider = _wire_app_tracer(monkeypatch)
    te = _export(capture_io="full", app_langfuse=_app_lf(),
                 prompt=f"my SSN is {_SSN}")
    provider.force_flush()
    attrs = _root_attrs(mem)
    assert te.capture_io == "full"
    assert _SSN in attrs["input.value"]                  # raw, on purpose


def test_full_downgrades_when_per_app_tracer_build_fails(monkeypatch):
    # app_langfuse IS configured (the app has its own Langfuse project), but
    # get_app_tracer fails to build a tracer for it (SDK build failure). Must
    # NOT fall through to authoring raw I/O on the SHARED tracer -- downgrade
    # to redacted before any span is authored (B14 final-review).
    from types import SimpleNamespace

    monkeypatch.setattr(export_mod, "get_app_tracer", lambda *a, **k: None)
    monkeypatch.setattr(otel, "_capture_io_full", True)  # even if globally armed
    mem, provider = _wire(monkeypatch, redacting=True)
    app_lf = SimpleNamespace(host="http://lf", public_key="pk", secret_key="sk",
                             project_id="proj-1")
    _export(capture_io="full", app_langfuse=app_lf)
    provider.force_flush()
    attrs = _root_attrs(mem)
    assert _SSN not in attrs["input.value"]
    assert _SSN not in attrs["output.value"]
    assert "<US_SSN" in attrs["input.value"]  # redacted, not merely absent


def test_full_per_app_tracer_builds_still_stores_raw(monkeypatch):
    # Sanity check for the normal per-app-full path: when the per-app tracer
    # DOES build, raw I/O still lands -- only in that app's own project.
    from types import SimpleNamespace

    app_mem = InMemorySpanExporter()
    app_provider = TracerProvider()
    app_provider.add_span_processor(
        SimpleSpanProcessor(otel.RedactingSpanExporter(app_mem, skip_io=True)))
    monkeypatch.setattr(export_mod, "get_app_tracer",
                        lambda *a, **k: app_provider.get_tracer("t"))
    app_lf = SimpleNamespace(host="http://lf", public_key="pk", secret_key="sk",
                             project_id="proj-1")
    _export(capture_io="full", app_langfuse=app_lf)
    app_provider.force_flush()
    root = next(s for s in app_mem.get_finished_spans() if s.name == "serving.request")
    assert _SSN in dict(root.attributes)["input.value"]


def test_redacting_exporter_skip_io_keeps_raw_only_when_armed(monkeypatch):
    # Per-app full exporters are built with skip_io=True so an eligible app's
    # OWN project keeps raw I/O even while the process-global flag is off; the
    # default exporter (skip_io=False) still scrubs (defence in depth).
    #
    # The span carries `sp.capture_io` because every span export.py authors
    # does. Keeping raw now takes BOTH an armed exporter and a span that says
    # this request resolved to `full` -- see the two tests below for each half.
    def _run(skip_io):
        mem = InMemorySpanExporter()
        prov = TracerProvider()
        prov.add_span_processor(SimpleSpanProcessor(otel.RedactingSpanExporter(mem, skip_io=skip_io)))
        monkeypatch.setattr(otel, "_capture_io_full", False)  # global OFF
        with prov.get_tracer("t").start_as_current_span("serving.request") as s:
            s.set_attribute("sp.capture_io", "full")
            s.set_attribute("input.value", f"raw {_SSN}")
        prov.force_flush()
        return next(iter(mem.get_finished_spans())).attributes["input.value"]

    assert _SSN in _run(skip_io=True)       # per-app full exporter: raw kept
    assert _SSN not in _run(skip_io=False)  # default exporter: raw scrubbed (defence in depth)


def test_exporter_redacts_a_span_that_does_not_name_its_capture(monkeypatch):
    # The fail-closed default for the other half. An unstamped span is
    # unreachable through export.py, which always stamps -- which is exactly
    # why the default must be the safe one: the unreachable branch is the one
    # some future instrumentation reaches, and "capture unknown" can never mean
    # "keep the raw value".
    monkeypatch.setattr(otel, "_capture_io_full", True)  # every skip condition armed
    mem = InMemorySpanExporter()
    prov = TracerProvider()
    prov.add_span_processor(
        SimpleSpanProcessor(otel.RedactingSpanExporter(mem, skip_io=True)))
    with prov.get_tracer("t").start_as_current_span("serving.request") as s:
        s.set_attribute("input.value", f"raw {_SSN}")   # no sp.capture_io
    prov.force_flush()
    assert _SSN not in next(iter(mem.get_finished_spans())).attributes["input.value"]


def test_redacted_capture_reflects_full_model_input(monkeypatch):
    from fastapi.testclient import TestClient
    monkeypatch.setenv("SP_PII_DETECTION", "regex")
    monkeypatch.setenv("SP_CAPTURE_IO", "redacted")
    from app.main import create_app
    client = TestClient(create_app())

    mem, provider = _wire(monkeypatch, redacting=True)
    r = client.post("/v1/chat/completions",
                    headers={"authorization": "Bearer hospital-key"},
                    json={"model": "auto", "messages": [
                        {"role": "user", "content": f"patient SSN is {_SSN}"},
                        {"role": "user", "content": "please summarize"}]})
    assert r.status_code == 200
    # Internal plumbing field must NOT leak to the client:
    assert "sanitized_prompt" not in r.json()
    provider.force_flush()
    attrs = _root_attrs(mem)
    # Old code redacted only messages[-1] ("please summarize") — the SSN from the
    # FIRST message was absent from telemetry. Now telemetry redacts the gate's
    # whole flattened raw scan text ITSELF (`raw_prompt`, design §2.6), with the
    # app's own detector — whose anonymize() numbers placeholders (<TYPE_N>),
    # unlike the bare-<TYPE> shape the regex egress pass produces.
    assert "<US_SSN_1>" in attrs["input.value"]
    assert "summarize" in attrs["input.value"]
    assert _SSN not in str(attrs)


# --------------------------------------------------------------------------- #
# CE-1: telemetry projects independently (design P1a §2.6)
#
# These two are written at the HTTP boundary on purpose. The claim is about
# what a real request exports for a real stored policy, and the bug was a
# consumer (the exporter) quietly reusing another consumer's (the model's)
# view of the request -- which no call into `export_trace` with hand-picked
# arguments can falsify.
# --------------------------------------------------------------------------- #
def _seed_full_capture_app(client, app_id: str) -> str:
    """An app the admin API accepts as `onprem_only` + `passthrough` +
    `capture_io: full`: it has its own provisioned Langfuse project (rule 2's
    own-project condition) and has declared the no-protection posture
    (`policy_mode: passthrough` -> model_input/client_output both `raw`).
    Returns its bearer token."""
    from tests.test_admin_api import _login

    _login(client)
    r = client.post("/admin/applications",
                    json={"id": app_id, "name": app_id, "owner_email": "a@b.c"})
    assert r.status_code == 200, r.text
    token = r.json()["token"]
    lf = client.put(f"/admin/applications/{app_id}/langfuse",
                    json={"host": "http://lf.test:3000", "public_key": "pub",
                          "secret_key": "sek", "project_id": f"proj-{app_id}"})
    assert lf.status_code == 200, lf.text
    pr = client.put(f"/admin/applications/{app_id}/policy",
                    json={"residency": "onprem_only", "policy_mode": "passthrough",
                          "capture_io": "full"})
    assert pr.status_code == 200, pr.text
    return token


def _ask(client, token: str, text: str) -> dict:
    r = client.post("/v1/chat/completions",
                    headers={"Authorization": f"Bearer {token}"},
                    json={"model": "auto",
                          "messages": [{"role": "user", "content": text}]})
    assert r.status_code == 200, r.text
    return r.json()


def test_onprem_passthrough_full_does_not_export_raw_under_a_redacted_label(
        client, monkeypatch):
    """CE-1. Before P1a, routes.py:599 clamped full -> redacted for ANY
    onprem_only app regardless of policy_mode, and the 'redacted' exporter then
    reused the gate's sanitized prompt -- which under passthrough IS the raw
    text. The app was accepted at write time as onprem_only + passthrough +
    full, so the operator believed raw capture was going to their own project;
    what shipped was raw content labelled redacted, minus a regex pass.

    Here the app has no own project at request time (credentials removed, or a
    key rotation left ciphertext `load_app_langfuse` can no longer decrypt --
    it returns None for both). `full` may not fall back to the shared project
    (CLAUDE.md rule 2), and `redacted` is not available either: this app runs
    no detector, so no genuinely redacted copy exists. The only honest answer
    is to export no I/O at all.

    The span exporter is deliberately NOT the redacting one: its regex pass is
    a partial net over *some* built-in entity shapes, not the guarantee, and
    the guarantee is that export_trace never authors raw under a redacted
    label in the first place (see `test_redacted_masks_pii`).
    """
    import app.api.routes as routes_mod

    mem, provider = _wire(monkeypatch, redacting=False)
    token = _seed_full_capture_app(client, "ce1-onprem-passthrough-full")
    # The app's own Langfuse project is gone by the time the request runs.
    monkeypatch.setattr(routes_mod, "load_app_langfuse", lambda app: None)

    resp = _ask(client, token, f"my SSN is {_SSN} please help")

    provider.force_flush()
    attrs = _root_attrs(mem)
    # (1) No I/O surface on the trace at all -- not a "redacted" label over raw.
    assert "input.value" not in attrs, attrs.get("input.value")
    assert "output.value" not in attrs, attrs.get("output.value")
    assert _SSN not in str(attrs)
    # (2) The display block is the second I/O surface and obeys the same rule.
    assert resp["display"]["mode"] == "off"
    assert resp["display"]["input"] is None
    assert resp["display"]["output"] is None
    assert _SSN not in json.dumps(resp["display"])


def test_full_with_own_project_still_exports_raw_to_that_project_only(
        client, monkeypatch):
    """The positive half of CE-1, so the fix above cannot be "export nothing,
    ever": the SAME app, with its own project actually reachable, still gets
    raw I/O -- in ITS project. The shared project gets no span at all, so it
    can never see the raw prompt (CLAUDE.md rule 2)."""
    shared_mem, shared_provider = _wire(monkeypatch, redacting=False)
    app_mem = InMemorySpanExporter()
    app_provider = TracerProvider()
    app_provider.add_span_processor(
        SimpleSpanProcessor(otel.RedactingSpanExporter(app_mem, skip_io=True)))
    monkeypatch.setattr(export_mod, "get_app_tracer",
                        lambda *a, **k: app_provider.get_tracer("t"))

    token = _seed_full_capture_app(client, "ce1-onprem-passthrough-full-ok")
    _ask(client, token, f"my SSN is {_SSN} please help")

    app_provider.force_flush()
    shared_provider.force_flush()
    own = next(s for s in app_mem.get_finished_spans() if s.name == "serving.request")
    assert _SSN in dict(own.attributes)["input.value"]      # raw, in its OWN project
    assert not [s for s in shared_mem.get_finished_spans()
                if s.name == "serving.request"]             # shared project: nothing
