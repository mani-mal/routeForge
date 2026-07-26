"""Export a request's decision trace to Langfuse via OTel. Best-effort: never
raises into the request path (telemetry != policy gate)."""
from __future__ import annotations

import logging

from app.obs.otel import app_trace_url, get_app_tracer, get_tracer, trace_url
from app.obs.redact import redact_value

log = logging.getLogger("serving.obs")


def export_trace(*, request_id: str, tenant_id: str, model: str,
                 cost: dict, policy: dict, trace: list[dict],
                 application_id: str = "", app_langfuse=None) -> str | None:
    """Export the request's decision trace as spans and return the Langfuse UI
    URL for the resulting trace (or None when telemetry is disabled), so the
    caller can hand the UI a live "Langfuse trace" deep-link.

    When `app_langfuse` (the application's own Langfuse project) is provided,
    the trace is exported to THAT project and the deep-link points there — each
    application owner sees only their own traces (Phase 5). Otherwise it falls
    back to the shared project."""
    try:
        per_app = None
        if app_langfuse is not None:
            per_app = get_app_tracer(app_langfuse.host, app_langfuse.public_key,
                                     app_langfuse.secret_key)
        tracer = per_app if per_app is not None else get_tracer()
        with tracer.start_as_current_span("serving.request") as root:
            root.set_attribute("sp.request_id", request_id)
            root.set_attribute("sp.tenant_id", tenant_id)
            root.set_attribute("sp.application_id", application_id or tenant_id)
            root.set_attribute("sp.model", model)
            root.set_attribute("sp.cost_total_usd", float(cost.get("total_usd", 0.0)))
            root.set_attribute("sp.cost_baseline_usd", float(cost.get("baseline_usd", 0.0)))
            root.set_attribute("sp.sensitivity", str(policy.get("sensitivity", "")))
            root.set_attribute("sp.onprem_only", bool(policy.get("onprem_only", False)))
            root.set_attribute("sp.detected_entities",
                               ",".join(policy.get("detected_entities", [])))  # TYPES only
            for i, rec in enumerate(trace):
                step = str(rec.get("step", f"step-{i}"))
                with tracer.start_as_current_span(f"serving.{step}") as sp:
                    sp.set_attribute("sp.step", step)
                    if rec.get("chosen") is not None:
                        # chosen is a model id / enum, never free text — redact
                        # defensively anyway (idempotent on clean text) so this
                        # attribute can't leak if a future record widens it.
                        sp.set_attribute("sp.chosen", redact_value(str(rec["chosen"])))
                    if "reason" in rec:
                        sp.set_attribute("sp.reason", redact_value(str(rec["reason"])))
                    for k in ("hop_index", "actual_cost_usd", "est_cost_usd", "latency_ms"):
                        if rec.get(k) is not None:
                            sp.set_attribute(f"sp.{k}", float(rec[k]))
            # Capture the trace id for the UI deep-link. The NoOp span (telemetry
            # off) has no get_span_context, so this stays None and the UI hides
            # the link.
            get_ctx = getattr(root, "get_span_context", None)
            if get_ctx is not None:
                trace_id = get_ctx().trace_id
                if app_langfuse is not None:
                    return app_trace_url(app_langfuse.project_id, trace_id)
                return trace_url(trace_id)
    except Exception:  # telemetry must never break serving
        log.exception("trace export failed for %s (swallowed)", request_id)
    return None
