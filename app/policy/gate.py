from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from app.config.schema import Settings
from app.models import ChatRequest, ExecutionPolicyContext, Message
from app.policy.context import build_context
from app.policy.detectors import Detector

if TYPE_CHECKING:
    from app.apps.policy import AppPolicyView
    from app.apps.registry import ModelRegistry


def tool_call_arg_text(m: Message) -> str:
    """Concatenated text of one message's tool-call arguments
    (`tool_calls[*].function.arguments`), for scanning alongside `content` so
    an SSN reachable only through a replayed tool call is still classified
    (CLAUDE.md rule 1/3). Skips a call with a missing `function`/`arguments`
    or a non-string `arguments` -- a malformed value must never crash the
    gate. `tool_calls` is `None`/absent on every ordinary (non-tool)
    message, so this returns "" for them and the flattened scan text is
    unchanged."""
    if not m.tool_calls:
        return ""
    parts: list[str] = []
    for tc in m.tool_calls:
        fn = tc.get("function") if isinstance(tc, dict) else None
        args = fn.get("arguments") if isinstance(fn, dict) else None
        if isinstance(args, str):
            parts.append(args)
    return "\n".join(parts)


def redact_tool_calls(tool_calls: list[dict] | None, det: Detector) -> list[dict] | None:
    """Return a COPY of `tool_calls` with each `function.arguments` STRING
    redacted in place by `det` (`det.anonymize(text, det.analyze(text))` --
    no JSON parse/re-serialize per the plan's constraint). `id`, `type`,
    `function.name`, and every other key are preserved exactly; a missing
    `function`/non-string `arguments` passes through untouched. Never
    mutates the caller's dicts. Shared by ingress (`Pipeline._sanitize_msg`)
    and egress (Task 2, output verification) so both directions redact
    tool-call argument text the same way."""
    if not tool_calls:
        return tool_calls
    out: list[dict] = []
    for tc in tool_calls:
        fn = tc.get("function") if isinstance(tc, dict) else None
        args = fn.get("arguments") if isinstance(fn, dict) else None
        if isinstance(fn, dict) and isinstance(args, str):
            redacted, _ = det.anonymize(args, det.analyze(args))
            tc = {**tc, "function": {**fn, "arguments": redacted}}
        out.append(tc)
    return out


def _scan_text(m: Message) -> str:
    """One message's scan text: `content` plus, when present, its tool-call
    argument text -- so a message that only carries `tool_calls` (content is
    `None`) still contributes its arguments to classification."""
    args = tool_call_arg_text(m)
    if not args:
        return m.content or ""
    return (m.content or "") + "\n" + args


class Gate:
    def __init__(self, settings: Settings, detector: Detector,
                 registry: "ModelRegistry | None" = None) -> None:
        self._settings = settings
        self._detector = detector
        self._registry = registry

    def evaluate(self, request_id: str, tenant_id: str, request: ChatRequest,
                 app_policy: "AppPolicyView | None" = None,
                 detector: Detector | None = None,
                 rules: list | None = None,
                 ) -> tuple[ExecutionPolicyContext, str, dict[str, str]]:
        # `detector`/`rules` let a caller supply the app's EffectiveDetector +
        # effective_rules (per-app custom recognizers/classification) instead
        # of the process-wide default -- see app/pipeline.py.
        det = detector or self._detector
        # scan the latest user message (the live prompt) + prior content.
        # `content or ""`: a tool-calling round-trip includes an assistant
        # message whose content is null (it only carries tool_calls) — joining
        # None would crash the gate before any request is served. Each
        # message's tool-call argument text (if any) is folded in too, so an
        # SSN reachable only through arguments sets the same sensitivity/
        # onprem_only an SSN in content would (`_scan_text`/`tool_call_arg_text`
        # above) -- ordinary messages carry no tool_calls, so this is a no-op
        # for them and the flattened scan text is unchanged.
        text = "\n".join(_scan_text(m) for m in request.messages)
        entities = det.analyze(text)
        redacted, mapping = det.anonymize(text, entities)
        map_id = f"map-{uuid.uuid4().hex[:8]}" if mapping else None
        ctx = build_context(self._settings, request_id, tenant_id, entities, map_id,
                            app_policy=app_policy, app_rules=rules,
                            registry=self._registry)
        ctx = ctx.model_copy(update={
            "requested_model": None if request.model == "auto" else request.model,
        })
        return ctx, redacted, mapping
