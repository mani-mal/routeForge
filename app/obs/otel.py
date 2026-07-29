"""OTel wiring: pipeline spans -> OTLP -> Langfuse, with egress redaction.

Redaction happens twice: authored spans are redacted before set_attribute in
export.py (the guarantee), and RedactingSpanExporter re-redacts on the way out
(defence in depth, and the mechanism NAT's Phase-5 spans reuse).
"""
from __future__ import annotations

import base64
import logging

from app.config.schema import Settings
from app.obs.redact import redact_value

log = logging.getLogger("serving.obs")

# Attribute keys that may carry free text. Includes OTel-GenAI / NAT span keys so
# Phase-5 agent spans are covered by the same exporter.
REDACT_KEYS: tuple[str, ...] = (
    "sp.reason", "sp.input", "sp.output",
    "input.value", "output.value", "tool.output", "gen_ai.prompt", "gen_ai.completion",
)

# The Langfuse Input/Output attribute keys. Normally redacted on egress like any
# other free-text key (defence in depth). When capture_io == "full" the operator
# has DELIBERATELY opted into raw eval capture and the config lint has already
# proven no onprem_only tenant is exposed (app/config/lint.py), so the egress
# redactor leaves these two keys alone — otherwise `full` mode would be identical
# to `redacted`. Every other key in REDACT_KEYS is still redacted.
_IO_KEYS: frozenset[str] = frozenset({"input.value", "output.value"})
_capture_io_full: bool = False  # set once at init_tracing from serving.yaml

_provider = None  # module-level singleton
_trace_base: str | None = None  # "{host}/project/{project_id}/traces/" for deep-links
_deeplink_host: str | None = None  # browser-reachable Langfuse host (public_host)
# Per-application TracerProviders keyed by the app's Langfuse public key, so each
# application's decision traces export to ITS OWN project (spec 2026-07-25,
# Phase 5). None means "tried and failed / SDK missing" — don't retry.
_app_providers: dict[str, object] = {}


def trace_url(trace_id: int) -> str | None:
    """Build the Langfuse UI URL for a trace id (the UI's live "Langfuse trace"
    deep-link). Returns None when telemetry is disabled/unconfigured, so the UI
    hides the link rather than pointing at a dead page."""
    if _provider is None or _trace_base is None:
        return None
    return f"{_trace_base}{trace_id:032x}"


def app_trace_url(project_id: str, trace_id: int) -> str | None:
    """Deep-link for a trace in a specific application's Langfuse project. Uses
    the same browser-reachable host as the shared link, only the project differs."""
    if _deeplink_host is None or not project_id:
        return None
    return f"{_deeplink_host}/project/{project_id}/traces/{trace_id:032x}"


def _build_exporter_kv(host: str, public_key: str, secret_key: str):
    """OTLP->Langfuse exporter for an explicit (host, project key) pair."""
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    auth = base64.b64encode(f"{public_key}:{secret_key}".encode()).decode()
    return OTLPSpanExporter(
        endpoint=f"{host.rstrip('/')}/api/public/otel/v1/traces",
        headers={
            "Authorization": f"Basic {auth}",
            "x-langfuse-ingestion-version": "4",
        },
    )


def _build_exporter(lf):
    """Deferred import: only reached from inside init_tracing's guarded try/except."""
    return _build_exporter_kv(lf.host, lf.public_key, lf.secret_key)


def get_app_tracer(host: str, public_key: str, secret_key: str):
    """Return a tracer that exports to one application's Langfuse project,
    building (and caching) a dedicated TracerProvider the first time. Returns
    None if the observability SDK isn't installed. Reuses the same redacting
    exporter as the shared pipeline, so per-app traces are redacted too."""
    if public_key in _app_providers:
        p = _app_providers[public_key]
        return p.get_tracer("serving-plane") if p is not None else None
    try:
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
        base = _build_exporter_kv(host, public_key, secret_key)
        exporter = RedactingSpanExporter(base) if RedactingSpanExporter else base
        provider = TracerProvider(
            resource=Resource.create({"service.name": "serving-plane"}))
        provider.add_span_processor(BatchSpanProcessor(exporter))
    except Exception:  # SDK missing or misconfigured — never break serving
        log.warning("per-app tracer build failed for a Langfuse project (non-fatal)")
        _app_providers[public_key] = None
        return None
    _app_providers[public_key] = provider
    return provider.get_tracer("serving-plane")


def init_tracing(settings: Settings):
    """Idempotent. Returns the provider, or None when disabled / extra missing."""
    global _provider, _capture_io_full
    lf = settings.serving.observability.langfuse
    # Record the eval-capture mode for the exporter BEFORE the guards below can
    # early-return — the flag must reflect config even when export is off, and
    # the per-app exporters (get_app_tracer) read this same module flag.
    _capture_io_full = (lf.capture_io == "full")
    if _capture_io_full:
        log.warning("capture_io='full': RAW request/response I/O will be exported "
                    "to Langfuse UNREDACTED (lint-gated to residency-local; see "
                    "app/config/lint.py)")
    if _provider is not None:
        return _provider
    if not lf.enabled:
        return None
    if not (lf.public_key and lf.secret_key):
        log.warning("langfuse enabled but LANGFUSE_PUBLIC_KEY/SECRET_KEY unset; "
                    "telemetry export disabled")
        return None
    try:
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
        exporter = RedactingSpanExporter(_build_exporter(lf)) if lf.redact_on_export \
            else _build_exporter(lf)
    except ImportError:
        log.warning("observability extra not installed; telemetry export disabled")
        return None
    provider = TracerProvider(resource=Resource.create({"service.name": "serving-plane"}))
    provider.add_span_processor(BatchSpanProcessor(exporter))
    _provider = provider
    global _trace_base, _deeplink_host
    # Deep-links open in the user's browser, so prefer the browser-reachable
    # public_host over the in-cluster OTLP host.
    deeplink_host = (lf.public_host or lf.host).rstrip("/")
    _deeplink_host = deeplink_host
    _trace_base = f"{deeplink_host}/project/{lf.project_id}/traces/"
    # Register as the process-global tracer provider so any OTel-instrumented
    # library in-process (e.g. the NAT agent runtime's own spans) exports through
    # the SAME redacting pipeline and lands in the same Langfuse project. Guarded:
    # never let a telemetry setup error break boot.
    try:
        from opentelemetry import trace as _ot_trace
        _ot_trace.set_tracer_provider(provider)
    except Exception:  # pragma: no cover - defensive
        log.warning("could not register global tracer provider (non-fatal)")
    return provider


class _NoOpSpan:
    """Zero-dependency stand-in for an OTel span when telemetry is off."""

    def set_attribute(self, *a, **k):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _NoOpTracer:
    """Zero-dependency stand-in for an OTel tracer; never imports opentelemetry."""

    def start_as_current_span(self, *a, **k):
        return _NoOpSpan()


def get_tracer():
    if _provider is None:
        return _NoOpTracer()
    return _provider.get_tracer("serving-plane")


def _make_redacting_exporter():
    """Deferred so importing app.obs.otel never requires the extra."""
    from opentelemetry.sdk.trace.export import SpanExporter

    class RedactingSpanExporter(SpanExporter):
        def __init__(self, delegate):
            self._delegate = delegate

        def export(self, spans):
            for span in spans:
                self._redact(span)
            return self._delegate.export(spans)

        @staticmethod
        def _redact(span):
            # ReadableSpan._attributes is a BoundedAttributes (private OTel field).
            # Verified against opentelemetry-sdk 1.44.0: once a span is finished it
            # is constructed with immutable=True, so __setitem__ raises TypeError
            # unless we flip the private `_immutable` flag for the duration of the
            # in-place redaction. The backing store (`_dict`) is the same object
            # InMemorySpanExporter/other exporters read from, so mutating here is
            # visible to whatever `spans` is handed to next.
            attrs = getattr(span, "_attributes", None)
            if not attrs:
                return
            # In capture_io='full' the I/O keys are intentionally left raw
            # (lint-gated, residency-local); everything else still redacts.
            skip = _IO_KEYS if _capture_io_full else frozenset()
            keys_to_redact = [k for k in attrs.keys()
                              if k in REDACT_KEYS and k not in skip]
            if not keys_to_redact:
                return
            was_immutable = getattr(attrs, "_immutable", False)
            if was_immutable:
                attrs._immutable = False
            try:
                for k in keys_to_redact:
                    attrs[k] = redact_value(attrs[k])
            finally:
                if was_immutable:
                    attrs._immutable = True

        def shutdown(self):
            return self._delegate.shutdown()

        def force_flush(self, timeout_millis: int = 30000):
            return self._delegate.force_flush(timeout_millis)

    return RedactingSpanExporter


# Bind the class at import time only if the SDK is present; tests importorskip.
try:  # pragma: no cover - exercised via the extra
    RedactingSpanExporter = _make_redacting_exporter()
except Exception:  # SDK not installed
    RedactingSpanExporter = None  # type: ignore
