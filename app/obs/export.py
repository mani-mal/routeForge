"""Export a request's decision trace to Langfuse via OTel. Best-effort: never
raises into the request path (telemetry != policy gate)."""
from __future__ import annotations

import json
import logging

from app.obs.otel import app_trace_url, get_app_tracer, get_tracer, trace_url
from app.obs.redact import redact, redact_value

log = logging.getLogger("serving.obs")


def _candidates_json(rec: dict) -> str | None:
    """Serialize a routing DecisionRecord's chosen + rejected set into an
    always-on `sp.candidates` attribute. Returns None for records that carry no
    routing decision (e.g. the verify step). Values are model ids / enums /
    fixed reason strings -- no PII -- so this is emitted regardless of
    capture_io (rlhf-readiness F2). Reason strings are run through the same
    egress redactor defensively at emit time."""
    chosen = rec.get("chosen")
    rejected = rec.get("rejected") or []
    if not chosen and not rejected:
        return None
    items = []
    if chosen:
        items.append({"model": chosen, "tier": rec.get("chosen_tier"),
                      "provider": rec.get("chosen_provider"), "eligible": True,
                      "reason": rec.get("reason", "")})
    for r in rejected:
        items.append({"model": r.get("name"), "tier": r.get("tier"),
                      "provider": r.get("provider"), "eligible": False,
                      "reason": r.get("reason", "")})
    return json.dumps(items)


def _set_io(root, capture_io: str, prompt: str, completion: str,
            recognizers=None, sanitized_prompt: str = "",
            raw_completion: str = "") -> None:
    """Attach request/response I/O to the root span per the eval-capture mode
    (docs/eval-logging-backlog.md). `input.value`/`output.value` are the OTel
    attributes Langfuse reads as a trace's Input/Output.

    - off      → set nothing (today's behaviour; Input column stays blank).
    - redacted → run the SAME egress redactor as every other attribute, so
                 entity values become <TYPE> — eval-usable, no raw PII. The
                 RedactingSpanExporter also re-redacts these keys (idempotent).
                 `recognizers` (an app's custom recognizers, Task 7) are
                 threaded through so a per-app custom entity type (e.g. an
                 MRN pattern) is masked here too, not just built-ins.
    - full     → set raw text. Only reachable when the config lint has cleared
                 it (no onprem_only tenant); the exporter then skips redacting
                 the I/O keys (app/obs/otel.py _capture_io_full). Prefers the
                 PRE-VERIFY raw completion (F1 Phase D task 1) over the
                 already step-9-masked `completion` the caller passed, so a
                 full-eligible app's own residency-local project captures what
                 the model actually said, not the client-facing masked text.
                 Falls back to `completion` when no raw completion was
                 supplied (backward-compatible)."""
    if capture_io == "off":
        return
    if capture_io == "redacted":
        # Prefer the gate's already-sanitized prompt so telemetry can never be
        # weaker than what the model saw (B13). Fall back to re-redacting raw
        # for callers that don't supply it (backward-compatible).
        prompt = sanitized_prompt or redact(prompt, recognizers=recognizers)
        completion = redact(completion, recognizers=recognizers)
    elif capture_io == "full":
        completion = raw_completion or completion
    # `full`: leave prompt/completion raw (lint-gated, residency-local only).
    if prompt:
        root.set_attribute("input.value", prompt)
    if completion:
        root.set_attribute("output.value", completion)


def export_trace(*, request_id: str, tenant_id: str, model: str,
                 cost: dict, policy: dict, trace: list[dict],
                 application_id: str = "", app_langfuse=None,
                 capture_io: str = "off", prompt: str = "",
                 completion: str = "", sanitized_prompt: str = "", recognizers=None,
                 session_id: str | None = None,
                 routing_policy_version: str = "",
                 raw_completion: str = "") -> str | None:
    """Export the request's decision trace as spans and return the Langfuse UI
    URL for the resulting trace (or None when telemetry is disabled), so the
    caller can hand the UI a live "Langfuse trace" deep-link.

    When `app_langfuse` (the application's own Langfuse project) is provided,
    the trace is exported to THAT project and the deep-link points there — each
    application owner sees only their own traces (Phase 5). Otherwise it falls
    back to the shared project.

    `recognizers` (an app's AppPrivacyProfile.recognizers, Task 7) is threaded
    into the `redacted` capture_io path so a per-app custom entity type is
    masked in telemetry, not just the built-in RegexDetector patterns.

    `raw_completion` (F1 Phase D task 1) is the PRE-VERIFY model output --
    used only on the `capture_io == "full"` path (see `_set_io`), and only
    ever reaches a full-eligible app's OWN residency-local project (the
    fail-closed clamp above and in `app/api/routes.py` guarantee that)."""
    try:
        per_app = None
        if app_langfuse is not None:
            per_app = get_app_tracer(app_langfuse.host, app_langfuse.public_key,
                                     app_langfuse.secret_key,
                                     capture_io_full=(capture_io == "full"))
            if capture_io == "full" and per_app is None:
                # The app has its own Langfuse project, but building its
                # tracer failed (e.g. SDK build failure) -- we cannot isolate
                # raw I/O into that project, and the shared tracer is not a
                # safe fallback for raw text. Downgrade BEFORE any span is
                # authored (rules 2/8: redact on egress, residency beats
                # eval) rather than falling through to raw on the shared
                # project.
                log.warning(
                    "capture_io='full' requested but per-app tracer build "
                    "failed for tenant %s; downgrading to 'redacted' for "
                    "this export (no raw fallback to shared project)",
                    tenant_id)
                capture_io = "redacted"
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
            if routing_policy_version:
                root.set_attribute("sp.routing_policy_version", routing_policy_version)
            if session_id:
                # Langfuse groups traces sharing this attribute into one Session
                # (B17). Opaque, client-supplied; deliberately NOT in REDACT_KEYS
                # so it passes through the RedactingSpanExporter intact.
                root.set_attribute("langfuse.session.id", session_id)
            _set_io(root, capture_io, prompt, completion,
                    recognizers=recognizers, sanitized_prompt=sanitized_prompt,
                    raw_completion=raw_completion)
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
                    cands = _candidates_json(rec)
                    if cands is not None:
                        sp.set_attribute("sp.candidates", redact_value(cands))
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
