"""E1: the multi-step (NAT) path must not emit a RAW, unredacted Langfuse trace.

NAT's native `LangfuseTelemetryExporter` captures the agent's full trajectory
(per-hop prompts, completions, tool I/O) as raw text through its own OTLP
pipeline, bypassing our egress redaction (`app/obs/otel.RedactingSpanExporter`)
and per-application project routing (`get_app_tracer`). For a `hospital`
multi-step request that would push an SSN-bearing tool output/completion into
Langfuse unredacted — a CLAUDE.md rule 2 violation. Every request already emits
a redacted, per-app, per-hop `serving.request` trace via `app/obs/export.py`, so
`runtime._configure_nat_telemetry` must leave NAT's native exporter OFF.

These guards need no `nat` install — they assert the wiring decision directly.
"""
from types import SimpleNamespace

from app.agent import runtime


def test_nat_native_langfuse_exporter_stays_off():
    tracing: dict = {}
    config = SimpleNamespace(
        general=SimpleNamespace(telemetry=SimpleNamespace(tracing=tracing)))
    runtime._configure_nat_telemetry(config)
    assert tracing == {}, (
        "NAT's raw (unredacted) Langfuse exporter must not be configured — it "
        "bypasses egress redaction and per-app routing (CLAUDE.md rule 2)")


def test_old_raw_export_wiring_is_gone():
    # Catches an accidental re-introduction of the raw NAT langfuse exporter.
    assert not hasattr(runtime, "_maybe_enable_langfuse")
