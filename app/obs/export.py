"""Export a request's decision trace to Langfuse via OTel. Best-effort: never
raises into the request path (telemetry != policy gate)."""
from __future__ import annotations

import json
import logging
import time
from collections import namedtuple

from app.obs.otel import app_trace_url, get_app_tracer, get_tracer, trace_url
from app.obs.redact import redact_value, redacted_copy

log = logging.getLogger("serving.obs")

# `url` is the Langfuse UI deep-link (or None when telemetry is off); `trace_id`
# is the same trace's 32-hex OTel trace id (or None), so an external eval/RLHF
# consumer can link feedback to a trace without parsing the URL (F1 Phase D).
# `capture_io` is the RESOLVED telemetry capture this export actually performed
# (design P1a §2.6) -- not what the caller asked for. The caller needs it
# because the trace is not the only I/O surface: `resp["display"]` is a second
# one, and the two must never disagree about how much of the request may be
# shown (`app/api/routes.py::_finalize`).
TraceExport = namedtuple("TraceExport", ["url", "trace_id", "capture_io"],
                         defaults=("off",))


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
                      "reason": rec.get("reason", ""), "code": None})
    for r in rejected:
        # `code` (P1b design §6): the stable stage identifier behind a
        # data-handling exclusion (e.g. "region_not_allowed"), or None for a
        # rejection with no code of its own (residency/budget/tier/score) --
        # `.get` defaults absent, never fabricates one.
        items.append({"model": r.get("name"), "tier": r.get("tier"),
                      "provider": r.get("provider"), "eligible": False,
                      "reason": r.get("reason", ""), "code": r.get("code")})
    return json.dumps(items)


def resolve_capture_io(capture_io: str, *, has_own_project: bool,
                       detector=None) -> str:
    """The telemetry capture this export may actually perform (design §2.6).

    The ONE place the decision is made, so the trace and the console's
    `display` block can never disagree about it. Two rules, both fail-closed:

    * **`full` needs the app's own Langfuse project.** Raw I/O is CLAUDE.md
      rule 2's single exception and it is tenant-isolated: the shared project
      never receives it. With no own project `full` becomes `redacted` — never
      a fallback to raw on the shared tracer.
    * **`redacted` needs something to redact with.** `redacted` is a promise
      about the content, and the only way to keep it is to build a redacted
      copy from the raw values (`_set_io`), which needs a detector that
      actually ran. With no detector there is no honest redacted copy, so the
      answer is `off` — never raw text under a `redacted` label, which is
      exactly what CE-1 shipped.

    An unrecognised value is `off` for the same reason the compiler denies an
    unparseable axis: a typo must not be answered with content.
    """
    if capture_io not in ("off", "redacted", "full"):
        return "off"
    if capture_io == "full" and not has_own_project:
        capture_io = "redacted"
    if capture_io == "redacted" and detector is None:
        return "off"
    return capture_io


def _set_io(root, capture_io: str, prompt: str, completion: str,
            recognizers=None, detector=None, raw_prompt: str = "",
            redacted_prompt: str | None = None,
            raw_completion: str = "") -> None:
    """Attach request/response I/O to the root span per the RESOLVED capture
    mode (`resolve_capture_io`, above; contract in
    docs/reference/per-app-config.md). `input.value`/`output.value` are the
    OTel attributes Langfuse reads as a trace's Input/Output, and they are the
    only two I/O surfaces this function owns -- both get the same treatment.

    - off      → set nothing (the Input column stays blank).
    - redacted → build telemetry's OWN redacted copy of each raw value with
                 the app's detector (`redacted_copy`), then the regex egress
                 pass. `raw_prompt` (the gate's flattened raw scan text) is
                 preferred over `prompt` so telemetry reflects the WHOLE
                 request, not just its last message -- and comes paired with
                 `redacted_prompt`, the gate's redaction OF THAT SAME string
                 with THIS detector, which spares a second Presidio pass. The
                 COMPLETION never went through the gate, so it always gets its
                 own. Never authored without a detector --
                 `resolve_capture_io` has already turned that case into `off`;
                 the guard here is the structural backstop.
    - full     → set raw text. Reachable only for an app with its own Langfuse
                 project (rule 2), whose exporter is built with skip_io=True so
                 the I/O keys survive egress redaction. Prefers the PRE-VERIFY
                 raw completion (F1 Phase D task 1) over the already
                 step-9-masked `completion` the caller passed, so that project
                 captures what the model actually said. Falls back to
                 `completion` when no raw completion was supplied."""
    if capture_io == "off":
        return
    if capture_io == "redacted":
        if detector is None:
            # Unreachable through export_trace; a direct caller that gets here
            # would otherwise author raw under a redacted label (CE-1).
            log.warning("capture_io='redacted' with no detector; exporting no "
                        "I/O rather than unredacted content")
            return
        # `prepared` is paired with `raw_prompt` and ONLY with it -- the gate
        # redacted that exact string, not the last-message `prompt` fallback.
        if raw_prompt:
            prompt = redacted_copy(raw_prompt, detector, recognizers,
                                   prepared=redacted_prompt)
        else:
            prompt = redacted_copy(prompt, detector, recognizers)
        completion = redacted_copy(completion, detector, recognizers)
    elif capture_io == "full":
        completion = raw_completion or completion
    if prompt:
        root.set_attribute("input.value", prompt)
    if completion:
        root.set_attribute("output.value", completion)


def _mark_generation(span, rec: dict, usage: dict | None) -> None:
    """Make a router hop that chose a model a Langfuse generation: model, token
    usage and cost, which Langfuse reads from these OTel attributes. Values are
    a model id and numbers (no free text), so no redaction applies. `usage`
    (prompt_tokens/completion_tokens) is caller-resolved per hop -- see
    `_hop_usage` -- so this function itself has no opinion on WHICH tokens a
    hop gets, only how to stamp them once decided."""
    model = str(rec.get("chosen"))
    span.set_attribute("langfuse.observation.type", "generation")
    span.set_attribute("langfuse.observation.model.name", model)
    span.set_attribute("gen_ai.request.model", model)
    if usage:
        tin = int(usage.get("prompt_tokens") or 0)
        tout = int(usage.get("completion_tokens") or 0)
        span.set_attribute("gen_ai.usage.input_tokens", tin)
        span.set_attribute("gen_ai.usage.output_tokens", tout)
        span.set_attribute("langfuse.observation.usage_details",
                           json.dumps({"input": tin, "output": tout}))
    if rec.get("actual_cost_usd") is not None:
        span.set_attribute("langfuse.observation.cost_details",
                           json.dumps({"total": float(rec["actual_cost_usd"])}))


def _hop_usage(rec: dict, request_usage: dict | None, *, is_served: bool) -> dict | None:
    """Token usage for ONE router generation hop -- never just stamp the
    request-wide aggregate onto every hop, or a multi-step trace massively
    over-reports tokens (every earlier hop double-counts the whole request).

    - Multi-step: each router call re-enters the router for its own LLM call,
      and the DecisionRecord carries that call's own token counts
      (`tokens_priced` in, `tokens_out` out). Use those whenever either is
      present and > 0 -- this hop's usage is fully self-describing.
    - Direct / escalation (no per-hop tokens on the record): only the hop that
      actually served the final answer (the LAST router record with a
      `chosen` model) gets the request-level `usage` aggregate. An escalated-
      past hop (tried, rejected, superseded) gets no usage attribute at all --
      it has no tokens of its own to report and must not borrow the final
      hop's."""
    tin = rec.get("tokens_priced") or 0
    tout = rec.get("tokens_out") or 0
    if tin > 0 or tout > 0:
        return {"prompt_tokens": tin, "completion_tokens": tout}
    return request_usage if is_served else None


def export_trace(*, request_id: str, tenant_id: str, model: str,
                 cost: dict, policy: dict, trace: list[dict],
                 application_id: str = "", app_langfuse=None,
                 capture_io: str = "off", prompt: str = "",
                 completion: str = "", raw_prompt: str = "",
                 redacted_prompt: str | None = None, detector=None,
                 recognizers=None,
                 session_id: str | None = None,
                 routing_policy_version: str = "",
                 prompt_version: str = "",
                 raw_completion: str = "",
                 started_ns: int | None = None,
                 usage: dict | None = None) -> TraceExport:
    """Export the request's decision trace as spans and return a `TraceExport`
    (url, trace_id, capture_io) for the resulting trace — `url` the Langfuse UI
    deep-link, `trace_id` the same trace's 32-hex id, `capture_io` the capture
    this export actually performed — or `TraceExport(None, None, <resolved>)`
    when telemetry is disabled, so the caller can hand the UI a live "Langfuse
    trace" deep-link, hand an eval/RLHF consumer the raw trace id, and keep its
    OWN I/O surface (`resp["display"]`) in step with the trace.

    When `app_langfuse` (the application's own Langfuse project) is provided,
    the trace is exported to THAT project and the deep-link points there — each
    application owner sees only their own traces (Phase 5). Otherwise it falls
    back to the shared project — and `capture_io: full` never does, because raw
    I/O may only reach the app's own project (CLAUDE.md rule 2).

    **Telemetry projects independently (design §2.6).** `detector` is the
    request's own detector (`Representations.detector`, None exactly when none
    ran) and `raw_prompt` the gate's flattened RAW scan text. `redacted` mode
    builds its own redacted copy from those raw values rather than reusing the
    gate's model-facing view, and falls closed to `off` when no detector ran.
    Nothing here reads a view another consumer selected.

    `redacted_prompt` is an optimisation, not a view: `detector`'s own
    redaction of `raw_prompt`, which the gate already computed
    (`Representations.text_redacted`). It must come from the SAME frozen
    `Representations` as `raw_prompt` -- see `redacted_copy`.

    `recognizers` (an app's AppPrivacyProfile.recognizers) is threaded into the
    `redacted` path too, so a per-app custom entity type is masked in
    telemetry, not just the built-in RegexDetector patterns.

    `raw_completion` (F1 Phase D task 1) is the PRE-VERIFY model output --
    used only on the `capture_io == "full"` path (see `_set_io`), and only
    ever reaches a full-eligible app's OWN residency-local project.

    `prompt_version` (Feature 3 / DSPy prompt-optimization Task 1) is
    `Settings.prompt_version` -- stamped as `sp.prompt_version` when
    non-empty, mirroring `routing_policy_version` exactly."""
    resolved = "off"
    try:
        # Resolved BEFORE any span is authored, and before any tracer is built:
        # every downgrade this function can make has to happen while there is
        # still nothing on a span to leak. `full` with no own project becomes
        # `redacted` (rule 2 -- the shared project never receives raw I/O,
        # whether or not a `raw_completion` was supplied), and `redacted` with
        # no detector becomes `off`.
        resolved = resolve_capture_io(capture_io, detector=detector,
                                      has_own_project=app_langfuse is not None)
        if resolved != capture_io:
            log.warning("capture_io=%r resolved to %r for tenant %s "
                        "(own Langfuse project: %s; detector: %s)",
                        capture_io, resolved, tenant_id,
                        app_langfuse is not None, detector is not None)
        per_app = None
        if app_langfuse is not None:
            per_app = get_app_tracer(app_langfuse.host, app_langfuse.public_key,
                                     app_langfuse.secret_key,
                                     capture_io_full=(resolved == "full"))
            if resolved == "full" and per_app is None:
                # The app has its own Langfuse project, but building its
                # tracer failed (e.g. SDK build failure) -- we cannot isolate
                # raw I/O into that project, and the shared tracer is not a
                # safe fallback for raw text. Downgrade BEFORE any span is
                # authored (rules 2/8: redact on egress, residency beats
                # eval) rather than falling through to raw on the shared
                # project.
                log.warning(
                    "capture_io='full' requested but per-app tracer build "
                    "failed for tenant %s; downgrading for this export (no "
                    "raw fallback to shared project)", tenant_id)
                # Re-resolve rather than assigning "redacted" directly: the
                # downgrade lands on the SHARED tracer, so it has to clear the
                # same detector bar every other redacted export does, or this
                # failure path becomes the one that writes raw under a
                # redacted label.
                resolved = resolve_capture_io("redacted", detector=detector,
                                              has_own_project=False)
        tracer = per_app if per_app is not None else get_tracer()
        root_kwargs = {"start_time": started_ns} if started_ns else {}
        with tracer.start_as_current_span("serving.request", **root_kwargs) as root:
            root.set_attribute("sp.request_id", request_id)
            root.set_attribute("sp.tenant_id", tenant_id)
            root.set_attribute("sp.application_id", application_id or tenant_id)
            root.set_attribute("sp.model", model)
            root.set_attribute("sp.cost_total_usd", float(cost.get("total_usd", 0.0)))
            root.set_attribute("sp.cost_baseline_usd", float(cost.get("baseline_usd", 0.0)))
            if "cache_saved_usd" in cost:  # only present when semantic_cache is enabled
                root.set_attribute("sp.cost_cache_saved_usd", float(cost["cache_saved_usd"]))
            root.set_attribute("sp.sensitivity", str(policy.get("sensitivity", "")))
            root.set_attribute("sp.onprem_only", bool(policy.get("onprem_only", False)))
            root.set_attribute("sp.detected_entities",
                               ",".join(policy.get("detected_entities", [])))  # TYPES only
            if routing_policy_version:
                root.set_attribute("sp.routing_policy_version", routing_policy_version)
            if policy.get("app_policy_version"):
                # Per-app policy content hash (residency/pii/denylist/allowlists);
                # lets a trace consumer correlate a decision to the exact app
                # policy it ran under, and spot a stale conformance probe.
                root.set_attribute("sp.app_policy_version", policy["app_policy_version"])
            if policy.get("endpoint_registry_version"):
                # P1b Task 6 (design §6): the endpoint registry this decision
                # was made against -- `ModelRegistry.registry_version()`,
                # same stamp-when-non-empty pattern as
                # `sp.routing_policy_version`/`sp.app_policy_version` above.
                # Lets a replay consumer tell whether an operator changed an
                # endpoint's declared region/retention/raw-sensitive between
                # this decision and now, instead of that drift being silent.
                root.set_attribute("sp.endpoint_registry_version",
                                   policy["endpoint_registry_version"])
            if policy.get("policy_mode"):
                # Effective protection posture ("enforced" | "passthrough") this
                # request actually ran under. Emitted alongside the policy
                # version so a trace consumer can see -- never having to infer
                # from an absence of redaction -- that the gate was intentionally
                # bypassed for this app. An unprotected app must never be
                # unprotected invisibly (the plan's "Never silent" guard).
                root.set_attribute("sp.policy_mode", str(policy["policy_mode"]))
            if policy.get("content_integrity_mode"):
                # Data-fidelity mode this request ran under ("exact" |
                # "transformations_allowed") -- see
                # `ExecutionPolicyContext.content_integrity`. A DATA control,
                # separate from `sp.policy_mode` (protection posture).
                root.set_attribute("sp.content_integrity_mode",
                                   str(policy["content_integrity_mode"]))
            # Task 9 -- the rest of the decision certificate (design §6).
            # `sp.policy_mode`/`sp.content_integrity_mode` above are the
            # legacy compatibility surface (a client already reads them);
            # everything below is the full per-axis breakdown that makes the
            # certificate inspectable without having to infer one axis from
            # another. All read from the SAME `policy` dict `Pipeline`
            # assembled once, so the response the client sees and the trace
            # a consumer reads can never disagree (Task 9 brief: "follow that
            # exact pattern rather than inventing a new channel").
            if policy.get("placement"):
                # PLACEMENT: where compute may run, and what decided it.
                # `sources` names the rung (app policy / preset / ceiling /
                # deployment default) and any cross-axis rule that tightened
                # it -- see app/policy/compile.py::Placement.
                root.set_attribute("sp.placement", str(policy["placement"]))
            if policy.get("placement_sources"):
                root.set_attribute("sp.placement_sources",
                                   ",".join(policy["placement_sources"]))
            # PROTECTION: five independent axes, named individually so a
            # trace reader never has to infer one from another (CLAUDE.md
            # rule 2a -- placement and protection are separate axes).
            for key, attr in (
                ("model_input_mode", "sp.model_input_mode"),
                ("client_output_mode", "sp.client_output_mode"),
                ("telemetry_io_mode", "sp.telemetry_io_mode"),
                ("detection_mode", "sp.detection_mode"),
                ("stream_mode", "sp.stream_mode"),
            ):
                if policy.get(key):
                    root.set_attribute(attr, str(policy[key]))
            if policy.get("cross_axis_rules_fired"):
                # Every declared rule (design §5) that fired for this
                # request, whether or not it was allowed to move placement --
                # see `sp.placement_sources` for the ones that actually did.
                root.set_attribute("sp.cross_axis_rules_fired",
                                   ",".join(policy["cross_axis_rules_fired"]))
            if "detected_entity_types" in policy:
                # Certificate-named twin of `sp.detected_entities` above --
                # same TYPES-only list, under the design §6 field name.
                root.set_attribute(
                    "sp.detected_entity_types",
                    ",".join(policy.get("detected_entity_types", [])))
            checks = policy.get("checks")
            if checks:
                # sp.checks (design §6): the one per-request proof the
                # certificate offers, JSON-encoded like `sp.candidates` --
                # small structured data, not free text, so nothing here
                # needs redaction. A `None` value (not applicable this
                # request) survives the round-trip as JSON `null`, never a
                # fabricated boolean.
                #
                # `fallback_in_eligible_set` covers every router hop the
                # gateway moved PAST before the one that was served -- a
                # failed-over backend retry (Gateway.run's failover loop) AND
                # a confidence escalation to a higher tier both count, since
                # both are cases where the gateway used a candidate other than
                # its first pick. It is `null` when the served model was
                # chosen on the first hop (nothing to check). See
                # `Pipeline._eligibility_checks`.
                root.set_attribute("sp.checks", json.dumps(checks))
            if prompt_version:
                # Feature 3 (DSPy prompt-optimization) Task 1: sha256[:12]
                # over the active managed-prompt texts (Settings.prompt_version).
                # Empty (nothing stamped) whenever no managed prompt is
                # pinned active -- true for every deploy this task ships.
                root.set_attribute("sp.prompt_version", prompt_version)
            if session_id:
                # Langfuse groups traces sharing this attribute into one Session
                # (B17). Opaque, client-supplied; deliberately NOT in REDACT_KEYS
                # so it passes through the RedactingSpanExporter intact.
                root.set_attribute("langfuse.session.id", session_id)
            # The RESOLVED capture, stamped on the span itself. Two consumers:
            # an operator reading the trace (why is this app's Input blank?),
            # and `RedactingSpanExporter`, which uses it to refuse to skip I/O
            # redaction for a request whose capture is not `full` — a span
            # says what it is, rather than the exporter inferring it from a
            # process-global flag. Bounded cardinality, no PII.
            root.set_attribute("sp.capture_io", resolved)
            _set_io(root, resolved, prompt, completion,
                    recognizers=recognizers, detector=detector,
                    raw_prompt=raw_prompt, redacted_prompt=redacted_prompt,
                    raw_completion=raw_completion)
            # Lay each step out on the real clock: back to back from the request
            # start, each as long as its recorded latency, never past "now".
            # Without explicit times every span is created after the fact and
            # shows as 0 s in Langfuse.
            now_ns = time.time_ns()
            cursor = started_ns or now_ns
            router_hop_idxs = [i for i, r in enumerate(trace)
                               if r.get("step") == "router" and r.get("chosen")]
            served_idx = router_hop_idxs[-1] if router_hop_idxs else None
            for i, rec in enumerate(trace):
                step = str(rec.get("step", f"step-{i}"))
                dur_ns = int(float(rec.get("latency_ms") or 0.0) * 1_000_000)
                span_start = min(cursor, now_ns)
                span_end = min(span_start + dur_ns, now_ns)
                cursor = span_end
                timed = {"start_time": span_start, "end_on_exit": False} if started_ns else {}
                with tracer.start_as_current_span(f"serving.{step}", **timed) as sp:
                    # Attribute-setting can raise (e.g. a malformed cost value
                    # hitting float()). With end_on_exit=False the SDK does NOT
                    # end a span whose body raised -- it would be orphaned
                    # (never exported, never in Langfuse) even though the outer
                    # try/except still swallows the exception and returns
                    # TraceExport(None, None). `finally` guarantees end() runs
                    # exactly once on every path, success or exception.
                    try:
                        sp.set_attribute("sp.step", step)
                        if rec.get("chosen") is not None:
                            # chosen is a model id / enum, never free text —
                            # redact defensively anyway (idempotent on clean
                            # text) so this attribute can't leak if a future
                            # record widens it.
                            sp.set_attribute("sp.chosen", redact_value(str(rec["chosen"])))
                        if "reason" in rec:
                            sp.set_attribute("sp.reason", redact_value(str(rec["reason"])))
                        cands = _candidates_json(rec)
                        if cands is not None:
                            sp.set_attribute("sp.candidates", redact_value(cands))
                        for k in ("hop_index", "actual_cost_usd", "est_cost_usd", "latency_ms"):
                            if rec.get(k) is not None:
                                sp.set_attribute(f"sp.{k}", float(rec[k]))
                        if i in router_hop_idxs:
                            # EVERY router hop that chose a model is a
                            # generation, not just the one that served the
                            # final answer -- an escalated-past hop still
                            # called a model and spent money. Token usage is
                            # resolved per hop (see `_hop_usage`): a
                            # multi-step hop's own tokens, or the request
                            # aggregate ONLY on the hop that served the
                            # answer, or nothing.
                            _mark_generation(sp, rec, _hop_usage(
                                rec, usage, is_served=(i == served_idx)))
                    finally:
                        if started_ns:
                            sp.end(end_time=span_end)
            # Capture the trace id for the UI deep-link. The NoOp span (telemetry
            # off) has no get_span_context, so this stays None and the UI hides
            # the link.
            get_ctx = getattr(root, "get_span_context", None)
            if get_ctx is not None:
                trace_id = get_ctx().trace_id
                hex_id = f"{trace_id:032x}"
                if app_langfuse is not None:
                    return TraceExport(
                        app_trace_url(app_langfuse.project_id, trace_id), hex_id,
                        resolved)
                return TraceExport(trace_url(trace_id), hex_id, resolved)
    except Exception:  # telemetry must never break serving
        log.exception("trace export failed for %s (swallowed)", request_id)
    # `resolved` is computed first thing inside the try, so it is correct here
    # whether we fell out of the `with` (telemetry off — no span context) or
    # raised afterwards. It stays "off" if we never got that far.
    return TraceExport(None, None, resolved)
