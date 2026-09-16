from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

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


@dataclass(frozen=True)
class ToolCallRedaction:
    """Result of `redact_tool_calls_with_entities`: the redacted COPY of the
    tool calls plus the entity TYPES detected in their arguments (types only,
    never values -- safe for a `VerificationRecord`)."""
    tool_calls: list[dict] | None
    entity_types: frozenset[str]


def _redact_text(text: str, det: Detector, found: set[str]) -> str:
    entities = det.analyze(text)
    if not entities:
        return text
    found.update(e.type for e in entities)
    redacted, _ = det.anonymize(text, entities)
    # Same object when anonymize changed nothing, so callers can test "no
    # change" by identity and keep the original bytes.
    return text if redacted == text else redacted


def _redact_json_value(value: Any, det: Detector, found: set[str]) -> Any:
    """Redact one parsed JSON value, leaf by leaf. String leaves are redacted
    in place; a number leaf the detector matches (e.g. a timestamp read as
    US_BANK_NUMBER/PHONE_NUMBER) becomes the redacted STRING -- a bare
    placeholder where a number was is not valid JSON. Dict keys, bools and
    nulls are left alone. Returns the SAME object when nothing changed, so the
    caller can detect "no change" by identity."""
    if isinstance(value, str):
        return _redact_text(value, det, found)
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, (int, float)):
        as_text = str(value)
        redacted = _redact_text(as_text, det, found)
        return value if redacted == as_text else redacted
    if isinstance(value, dict):
        items = {k: _redact_json_value(v, det, found) for k, v in value.items()}
        if all(items[k] is value[k] for k in value):
            return value
        return items
    if isinstance(value, list):
        elems = [_redact_json_value(v, det, found) for v in value]
        if all(a is b for a, b in zip(elems, value)):
            return value
        return elems
    return value


def _redact_arguments(args: str, det: Detector, found: set[str]) -> str:
    """Redact one `function.arguments` string without ever breaking its JSON.
    Valid JSON is walked leaf by leaf (`_redact_json_value`) and re-serialized
    only if a leaf changed -- clean arguments (and every NullDetector/
    passthrough call) come back as the ORIGINAL string, byte for byte. Text
    that is not valid JSON falls back to a whole-string splice, exactly as
    message `content` is redacted. Placeholders are numbered per leaf (each
    leaf is its own `anonymize` call), so two SSNs in different leaves both
    read `<US_SSN_1>`; the redaction map is not kept for tool calls, so the
    numbering carries no meaning beyond "an entity was here"."""
    try:
        parsed = json.loads(args)
    except (ValueError, RecursionError):
        return _redact_text(args, det, found)
    try:
        redacted = _redact_json_value(parsed, det, found)
    except RecursionError:
        return _redact_text(args, det, found)
    if redacted is parsed:
        return args
    return json.dumps(redacted, ensure_ascii=False)


def redact_tool_calls_with_entities(tool_calls: list[dict] | None,
                                    det: Detector) -> ToolCallRedaction:
    """Return a COPY of `tool_calls` with each `function.arguments` STRING
    redacted by `det` (see `_redact_arguments`: JSON-aware, output always
    parses when the input did), plus the entity types detected. `id`, `type`,
    `function.name`, and every other key are preserved exactly; a non-dict
    call, a missing `function`, or a non-string `arguments` passes through
    untouched. Never mutates the caller's dicts. Shared by ingress
    (`Pipeline._sanitize_msg`) and egress (output verification) so both
    directions redact tool-call arguments the same way, and egress takes the
    entity types from here instead of running detection a second time."""
    found: set[str] = set()
    if not tool_calls:
        return ToolCallRedaction(tool_calls, frozenset())
    out: list[dict] = []
    for tc in tool_calls:
        fn = tc.get("function") if isinstance(tc, dict) else None
        args = fn.get("arguments") if isinstance(fn, dict) else None
        if isinstance(fn, dict) and isinstance(args, str):
            redacted = _redact_arguments(args, det, found)
            if redacted is not args:
                tc = {**tc, "function": {**fn, "arguments": redacted}}
        out.append(tc)
    return ToolCallRedaction(out, frozenset(found))


def redact_tool_calls(tool_calls: list[dict] | None, det: Detector) -> list[dict] | None:
    """`redact_tool_calls_with_entities` without the entity types."""
    return redact_tool_calls_with_entities(tool_calls, det).tool_calls


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
