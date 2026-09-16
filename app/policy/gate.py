from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from app.config.schema import Settings
from app.models import ChatRequest, Entity, ExecutionPolicyContext, Message
from app.policy.context import build_context
from app.policy.detectors import Detector
from app.policy.merge import dedupe

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


# Characters a detector span over RAW JSON can swallow around a value (e.g.
# Presidio's URL recognizer includes the closing quote); stripped before the
# raw-pass value is looked up inside the decoded leaves.
_JSON_EDGE = ' \t\r\n"{}[],:'


@dataclass
class _Leaf:
    """One redactable value inside one call's `arguments`: a JSON string or
    number leaf (written back into `parent[key]`), or -- `parent is None` --
    the whole root (a top-level JSON scalar, or non-JSON arguments text).
    `start`/`end` locate `text` inside the pass-1 scan text."""
    parent: dict | list | None
    key: str | int | None
    text: str
    label: str = ""
    start: int = 0
    end: int = 0
    spans: list[Entity] = field(default_factory=list)


@dataclass
class _Target:
    """One tool call whose `function.arguments` is a string."""
    index: int
    args: str
    is_json: bool
    root: Any = None
    leaves: list[_Leaf] = field(default_factory=list)


def _collect_leaves(value: Any, parent: dict | list | None, key: str | int | None,
                    label: str, out: list[_Leaf]) -> None:
    """Append a `_Leaf` for every string/number leaf. Bools, nulls and dict
    keys are never redacted. `label` is the nearest enclosing dict key -- it
    is rendered next to the value in the pass-1 scan text so a recognizer that
    needs context (Presidio boosts a bare 9-digit number to US_SSN when "ssn"
    is nearby) still gets it."""
    if isinstance(value, bool) or value is None:
        return
    if isinstance(value, str):
        out.append(_Leaf(parent, key, value, label))
    elif isinstance(value, (int, float)):
        out.append(_Leaf(parent, key, str(value), label))
    elif isinstance(value, dict):
        for k, v in value.items():
            _collect_leaves(v, value, k, k, out)
    elif isinstance(value, list):
        for i, v in enumerate(value):
            _collect_leaves(v, value, i, label, out)


def _occurrences(needle: str, haystack: str) -> list[tuple[int, int]]:
    """Every occurrence of `needle` in `haystack` not embedded inside a longer
    alphanumeric run (so a short raw-pass value like a two-letter name does
    not mangle unrelated words)."""
    found: list[tuple[int, int]] = []
    i = haystack.find(needle)
    while i != -1:
        j = i + len(needle)
        if ((i == 0 or not haystack[i - 1].isalnum())
                and (j == len(haystack) or not haystack[j].isalnum())):
            found.append((i, j))
        i = haystack.find(needle, i + 1)
    return found


def redact_tool_calls_with_entities(tool_calls: list[dict] | None,
                                    det: Detector) -> ToolCallRedaction:
    """Return a COPY of `tool_calls` with each `function.arguments` STRING
    redacted by `det`, plus the entity types detected. `id`, `type`,
    `function.name`, and every other key are preserved exactly; a non-dict
    call, a missing `function`, or a non-string `arguments` passes through
    untouched. Never mutates the caller's dicts. Shared by ingress
    (`Pipeline._sanitize_msg`) and egress (output verification) so both
    directions redact tool-call arguments the same way, and egress takes the
    entity types from here instead of running detection again.

    Arguments are machine-consumed JSON, so redaction must never break the
    JSON: valid JSON is redacted value by value (a matched number becomes the
    redacted STRING; keys, bools and nulls untouched) and re-serialized only
    if a value changed -- otherwise the ORIGINAL string is returned byte for
    byte (so NullDetector/passthrough is a no-op). Non-JSON arguments are
    redacted as one plain string.

    At most TWO `det.analyze` calls per invocation, whatever the number of
    calls or values (pass 1 is skipped when there is no string/number value):
      1. Context pass -- every value rendered as `key: value` lines in one
         scan text; entities overlapping a value are clipped to it.
      2. Raw pass -- the raw argument strings joined, i.e. the same text the
         gate classifies (`tool_call_arg_text`). Its types are always
         reported, and any entity value still present verbatim in a value is
         redacted there too. This is the guarantee that redaction never lets
         through what classification saw -- e.g. `{"ssn": "482731946"}`,
         which Presidio only scores as US_SSN with the key in view, so a
         per-app `pii_score_threshold`/denylist (which can never exempt
         US_SSN) cannot un-redact it.
    Placeholders are numbered per value (`<US_SSN_1>` may repeat across
    values); the redaction map is not kept for tool calls."""
    if not tool_calls:
        return ToolCallRedaction(tool_calls, frozenset())

    targets: list[_Target] = []
    for i, tc in enumerate(tool_calls):
        fn = tc.get("function") if isinstance(tc, dict) else None
        args = fn.get("arguments") if isinstance(fn, dict) else None
        if not (isinstance(fn, dict) and isinstance(args, str)):
            continue
        target = _Target(index=i, args=args, is_json=False)
        try:
            target.root = json.loads(args)
            _collect_leaves(target.root, None, None, "", target.leaves)
            target.is_json = True
        except (ValueError, RecursionError):
            target.leaves = [_Leaf(None, None, args)]
        targets.append(target)

    if not targets:
        return ToolCallRedaction(list(tool_calls), frozenset())

    # Pass 1: context scan over decoded values.
    all_leaves = [leaf for t in targets for leaf in t.leaves]
    parts: list[str] = []
    offset = 0
    for leaf in all_leaves:
        prefix = f"{leaf.label}: " if leaf.label else ""
        leaf.start = offset + len(prefix)
        leaf.end = leaf.start + len(leaf.text)
        line = prefix + leaf.text
        parts.append(line)
        offset += len(line) + 1
    found: set[str] = set()
    for e in (det.analyze("\n".join(parts)) if parts else []):
        for leaf in all_leaves:
            s, t = max(e.start, leaf.start), min(e.end, leaf.end)
            if s < t:
                found.add(e.type)
                ls, lt = s - leaf.start, t - leaf.start
                leaf.spans.append(Entity(type=e.type, start=ls, end=lt,
                                         value=leaf.text[ls:lt], score=e.score))

    # Pass 2: raw scan over the argument strings as the gate classifies them.
    raw = "\n".join(t.args for t in targets)
    for e in det.analyze(raw):
        found.add(e.type)
        value = raw[e.start:e.end].strip(_JSON_EDGE)
        if not value:
            continue
        for leaf in all_leaves:
            for s, t in _occurrences(value, leaf.text):
                leaf.spans.append(Entity(type=e.type, start=s, end=t,
                                         value=value, score=e.score))

    out = list(tool_calls)
    for target in targets:
        changed = False
        for leaf in target.leaves:
            if not leaf.spans:
                continue
            redacted, _ = det.anonymize(leaf.text, dedupe(leaf.spans))
            if redacted == leaf.text:
                continue
            changed = True
            if leaf.parent is None:
                target.root = redacted
            else:
                leaf.parent[leaf.key] = redacted  # type: ignore[index]
        if not changed:
            continue
        new_args = (json.dumps(target.root, ensure_ascii=False) if target.is_json
                    else target.root)
        tc = tool_calls[target.index]
        out[target.index] = {**tc, "function": {**tc["function"], "arguments": new_args}}
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
