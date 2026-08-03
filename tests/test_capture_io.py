"""Eval-capture feature: observability.langfuse.capture_io = off | redacted | full.

Attaches request/response I/O to the `serving.request` trace so Langfuse traces
are usable as an eval dataset (docs/eval-logging-backlog.md). Gated so it cannot
break CLAUDE.md rule 2 (redact on egress) / rule 6 (residency):

- off      — capture nothing (demo default; Input column blank).
- redacted — attach prompt/completion with entity VALUES masked to <TYPE>.
- full     — attach RAW I/O; config lint REJECTS it when any tenant is
             onprem_only, and the egress redactor then leaves the I/O keys alone.
"""
import copy

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


def _wire(monkeypatch, *, redacting: bool):
    mem = InMemorySpanExporter()
    provider = TracerProvider()
    exporter = otel.RedactingSpanExporter(mem) if redacting else mem
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    monkeypatch.setattr(otel, "_provider", provider)
    return mem, provider


def _export(**over):
    kw = dict(
        request_id="req-1", tenant_id="hospital", model="local-32b",
        cost={"total_usd": 0.0004, "baseline_usd": 0.012},
        policy={"sensitivity": "restricted", "onprem_only": True,
                "allowed_providers": ["onprem"], "detected_entities": ["US_SSN"]},
        trace=[{"step": "route", "chosen": "local-32b", "hop_index": 0}],
        prompt=f"my SSN is {_SSN}", completion=f"noted: {_SSN}",
    )
    kw.update(over)
    export_mod.export_trace(**kw)


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
    assert "<US_SSN>" in attrs["input.value"]
    assert _SSN not in attrs["output.value"]


def test_full_keeps_raw_io_but_still_redacts_other_keys(monkeypatch):
    # `full` is the residency-local escape hatch: I/O keys survive the egress
    # redactor, but every other free-text key (sp.reason) is still scrubbed.
    monkeypatch.setattr(otel, "_capture_io_full", True)
    mem, provider = _wire(monkeypatch, redacting=True)
    _export(capture_io="full",
            trace=[{"step": "route", "chosen": "local-32b",
                    "reason": f"leaked {_SSN} here", "hop_index": 0}])
    provider.force_flush()
    spans = mem.get_finished_spans()
    root = next(s for s in spans if s.name == "serving.request")
    assert _SSN in root.attributes["input.value"]      # raw, on purpose
    assert _SSN in root.attributes["output.value"]
    reason = next(s for s in spans if "sp.reason" in s.attributes)
    assert _SSN not in reason.attributes["sp.reason"]   # other keys still redacted


def test_full_authored_raw_is_still_scrubbed_when_flag_off(monkeypatch):
    # Defence in depth: if capture_io='full' authored raw text but the exporter
    # flag is NOT set (misconfig / flag never flipped), the egress redactor must
    # still scrub the I/O keys — full mode is unsafe only when deliberately armed.
    monkeypatch.setattr(otel, "_capture_io_full", False)
    mem, provider = _wire(monkeypatch, redacting=True)
    _export(capture_io="full")
    provider.force_flush()
    attrs = _root_attrs(mem)
    assert _SSN not in attrs["input.value"]


# --------------------------------------------------------------------------- #
# End-to-end: a real request through the app exports redacted I/O
# --------------------------------------------------------------------------- #
@pytest.mark.xfail(reason="Qwen removed in Task 4", strict=False)
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
    assert "<US_SSN>" in attrs["input.value"]
