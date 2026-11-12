from __future__ import annotations

import json
import uuid
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from app.config.schema import Settings
from app.models import ChatRequest, Entity, ExecutionPolicyContext, Message
from app.policy.compile import EffectivePolicy, apply_signals, compile_policy
from app.policy.context import build_context, match_sensitivity_rule
from app.policy.detectors import Detector, detection_ran
from app.policy.enums import TOOL_ARGS_BAR_TYPES_DEFAULT
from app.policy.merge import dedupe, exempt_from_score_bar

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
class ToolArgStats:
    """Content-free counts of what the tool-argument bar (B69) did: spans
    rewritten, spans that passed through because they scored below the bar,
    and the entity TYPES of the latter. Counts and types only -- never a
    value -- so they may go on the trace, the outcome row and the response."""
    redacted: int = 0
    skipped: int = 0
    skipped_types: frozenset[str] = frozenset()

    def __add__(self, other: "ToolArgStats") -> "ToolArgStats":
        return ToolArgStats(self.redacted + other.redacted,
                            self.skipped + other.skipped,
                            self.skipped_types | other.skipped_types)


@dataclass(frozen=True)
class ToolCallRedaction:
    """Result of `redact_tool_calls_with_entities`: the redacted COPY of the
    tool calls plus the entity TYPES detected in their arguments (types only,
    never values -- safe for a `VerificationRecord`), and the bar's counts
    (`ToolArgStats`). `entity_types` is what was DETECTED, skipped spans
    included: the bar changes rewriting, never detection."""
    tool_calls: list[dict] | None
    entity_types: frozenset[str]
    redacted: int = 0
    skipped: int = 0
    skipped_types: frozenset[str] = frozenset()

    @property
    def stats(self) -> ToolArgStats:
        return ToolArgStats(self.redacted, self.skipped, self.skipped_types)


def below_tool_arg_bar(e: Entity, min_score: float,
                       bar_types: frozenset[str] = frozenset(TOOL_ARGS_BAR_TYPES_DEFAULT)
                       ) -> bool:
    """True when the tool-argument bar lets `e` pass through unredacted
    inside tool-call arguments (B69).

    **The bar applies to the noisy types only** -- `bar_types`, the
    deployment's `serving.yaml` `policy.tool_args_bar_types` (default
    `TOOL_ARGS_BAR_TYPES_DEFAULT`). A span of any other type is ALWAYS
    rewritten, whatever its score: Presidio scores real PII low too
    (PHONE_NUMBER 0.4 with no context word, US_BANK_NUMBER / US_PASSPORT 0.4
    even with one), so a bar over every type would pass those raw.

    Within those types, only a SCORED match from a built-in recognizer can
    fall below the bar. Never exempted, whatever the score:

    * `PROTECTED_ENTITIES` (US_SSN) -- the same absolute guarantee no per-app
      threshold, denylist or exemption can lift (CLAUDE.md rules 3/8); the
      Presidio SSN backstop scores 1.0 anyway.
    * an unscored match (`score is None`): every `RegexDetector` match, so no
      `pii_detection: regex` match is ever dropped by the bar.
    * an operator-declared custom recognizer (`source == "custom"`): the
      operator wrote that pattern, and `pii_score_threshold` never culls one
      either.
    * a span a detector's own `dedupe` MERGED from a cluster containing any of
      the above (`Entity.bar_exempt`): the merged span takes type and score
      from the highest-scoring member only, so the exemption is carried
      across the cluster explicitly rather than lost in the merge.
    """
    return (min_score > 0.0 and e.type in bar_types
            and e.score is not None and e.score < min_score
            and not exempt_from_score_bar(e))


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
    #: Spans the tool-argument bar let through (B69), for counting only.
    skipped: list[Entity] = field(default_factory=list)

    def add(self, e: Entity, min_score: float, bar_types: frozenset[str]) -> None:
        below = below_tool_arg_bar(e, min_score, bar_types)
        (self.skipped if below else self.spans).append(e)

    def settle(self) -> None:
        """Redact, with the span it overlaps, every below-bar span that
        overlaps a redacted one (M3). Otherwise the kept span's placeholder
        would cover only its own part and the weak span's non-overlapping
        remainder would reach the client raw -- pre-B69 these were one union
        span. Repeated to a fixed point, since a promoted span can overlap
        another skipped one."""
        moved = True
        while moved and self.skipped and self.spans:
            moved = False
            for e in list(self.skipped):
                if any(k.start < e.end and e.start < k.end for k in self.spans):
                    self.skipped.remove(e)
                    self.spans.append(e)
                    moved = True


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


def _inert_placeholders(redacted: str, mapping: dict[str, str]) -> str:
    """Rewrite each `<TYPE_N>` placeholder `anonymize` just wrote as
    `REDACTED_TYPE_N`. A tool-call argument is a command channel: the client
    EXECUTES it, and in a shell `<NAME>` is a redirection (B69: a redacted
    `xargs -0 -n1` came back as `xargs -0 -<US_DRIVER_LICENSE_1>` and bash
    tried to read a file of that name). Word characters only are inert in a
    shell, a path, SQL and JSON alike. What is redacted does not change --
    only how the placeholder is spelled."""
    for ph in mapping:
        if ph.startswith("<") and ph.endswith(">"):
            redacted = redacted.replace(ph, "REDACTED_" + ph[1:-1])
    return redacted


def _passed_through(leaf: _Leaf) -> set[tuple[int, int, str]]:
    """The leaf's skipped spans that really passed through, de-duplicated
    across the two detector passes. Call after `_Leaf.settle`, which has
    already moved every skipped span a redacted span overlaps into the
    redacted set."""
    return {(e.start, e.end, e.type) for e in leaf.skipped}


def redact_tool_calls_with_entities(tool_calls: list[dict] | None,
                                    det: Detector,
                                    min_score: float = 0.0,
                                    bar_types: Iterable[str] | None = None
                                    ) -> ToolCallRedaction:
    """Return a COPY of `tool_calls` with each `function.arguments` STRING
    redacted by `det`, plus the entity types detected. `id`, `type`,
    `function.name`, and every other key are preserved exactly; a non-dict
    call, a missing `function`, or a non-string `arguments` passes through
    untouched. Never mutates the caller's dicts. Shared by ingress
    (`redact_message` below) and egress (output verification) so both
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
    Placeholders are numbered per value (`REDACTED_US_SSN_1` may repeat across
    values); the redaction map is not kept for tool calls. They are written
    in word characters only, not as `<US_SSN_1>` -- see `_inert_placeholders`
    (B69).

    **The tool-argument bar (B69).** Arguments are a command channel the
    client executes, so a span is rewritten only when it clears `min_score`
    (the app's effective bar, `Protection.tool_args_min_score`): a weak,
    context-free pattern match such as Presidio's 0.3 `US_DRIVER_LICENSE` on
    `n1` in `xargs -0 -n1` passes through byte for byte. The raw-pass
    guarantee above therefore holds for every span AT OR ABOVE the bar, and
    unconditionally for what `below_tool_arg_bar` never exempts -- US_SSN
    whatever its score, every unscored (regex) match, every custom-recognizer
    match. Detection is unchanged: `entity_types` still reports every type
    found, skipped ones included, so classification and the trace see them.
    What was skipped is never silent: `redacted`/`skipped`/`skipped_types`
    count it, content-free. `min_score=0.0` (the default) skips nothing.
    `bar_types` (default `TOOL_ARGS_BAR_TYPES_DEFAULT`) is the set of entity
    types the bar applies to at all -- every other type is always rewritten
    (see `below_tool_arg_bar`). A below-bar span that overlaps a rewritten one
    is rewritten with it, never left half-raw (`_Leaf.settle`)."""
    types = frozenset(TOOL_ARGS_BAR_TYPES_DEFAULT if bar_types is None else bar_types)
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
                leaf.add(e.model_copy(update={"start": ls, "end": lt,
                                              "value": leaf.text[ls:lt]}),
                         min_score, types)

    # Pass 2: raw scan over the argument strings as the gate classifies them.
    raw = "\n".join(t.args for t in targets)
    for e in det.analyze(raw):
        found.add(e.type)
        value = raw[e.start:e.end].strip(_JSON_EDGE)
        if not value:
            continue
        for leaf in all_leaves:
            for s, t in _occurrences(value, leaf.text):
                leaf.add(e.model_copy(update={"start": s, "end": t, "value": value}),
                         min_score, types)

    out = list(tool_calls)
    n_redacted = 0
    skipped: set[tuple[int, int, int, str]] = set()
    for li, leaf in enumerate(all_leaves):
        leaf.settle()
        skipped |= {(li, *k) for k in _passed_through(leaf)}
    for target in targets:
        changed = False
        for leaf in target.leaves:
            if not leaf.spans:
                continue
            spans = dedupe(leaf.spans)
            redacted, mapping = det.anonymize(leaf.text, spans)
            if redacted == leaf.text:
                continue
            redacted = _inert_placeholders(redacted, mapping)
            n_redacted += len(spans)
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
    return ToolCallRedaction(out, frozenset(found), redacted=n_redacted,
                             skipped=len(skipped),
                             skipped_types=frozenset(k[3] for k in skipped))


def redact_tool_calls(tool_calls: list[dict] | None, det: Detector,
                      min_score: float = 0.0,
                      bar_types: Iterable[str] | None = None) -> list[dict] | None:
    """`redact_tool_calls_with_entities` without the entity types."""
    return redact_tool_calls_with_entities(tool_calls, det, min_score,
                                           bar_types).tool_calls


def redact_message(det: Detector, m: Message, min_score: float = 0.0,
                   bar_types: Iterable[str] | None = None) -> Message:
    """Redact one message's text with the request's effective detector,
    preserving role + tool-calling structure. Used on the tool-calling path so
    the full structured conversation reaches the model with every message's PII
    masked (CLAUDE.md rule 1) -- the per-message analogue of the flattened
    path's single sanitized blob. A None content (an assistant turn that only
    carries tool_calls) passes through untouched. `tool_calls` arguments are
    redacted the same way via `redact_tool_calls` -- a replayed assistant tool
    call whose arguments carry an SSN must not forward it to the model
    untouched.

    `min_score` is the tool-argument bar (B69) and applies to `tool_calls`
    arguments ONLY: message `content` is prose, redacted exactly as before
    whatever the score.

    Lives here, next to `redact_tool_calls`, rather than on `Pipeline`: it is
    how `Representations.redacted` below is built, and the gate owns every
    representation of a request.
    """
    return _redact_message(det, m, min_score, bar_types)[0]


def _redact_message(det: Detector, m: Message, min_score: float,
                    bar_types: Iterable[str] | None = None
                    ) -> tuple[Message, ToolArgStats]:
    """`redact_message` plus the tool-argument bar's counts for `m`."""
    content = m.content
    if content:
        content, _ = det.anonymize(content, det.analyze(content))
    tcr = redact_tool_calls_with_entities(m.tool_calls, det, min_score, bar_types)
    return (Message(role=m.role, content=content, tool_calls=tcr.tool_calls,
                    tool_call_id=m.tool_call_id, name=m.name), tcr.stats)


@dataclass(frozen=True)
class Representations:
    """The gate's explicit views of one request (design §2.4).

    There used to be one `sanitized_prompt` string, and every consumer read
    whatever the MODEL had been given — which is how a raw-input app came to
    export content labelled `redacted`. Now the gate publishes both views and
    each consumer selects its own: the model by `model_input`, the client by
    `client_output`, the exporter by `telemetry_io`. No consumer reads
    another's view.

    Two shapes of each view, because the pipeline genuinely needs both:

    * `text_raw` / `text_redacted` — the FLATTENED scan text (`_scan_text`
      joined over every message, tool-call arguments included) and its
      redaction. This is what the gate classified, and what the ordinary
      single-prompt path sends.
    * `raw` / `redacted` — the per-message views, which the client-driven
      tool-calling path needs because it must preserve roles and tool
      structure.

    `text_redacted` and `redacted` are **None exactly when no detector ran**
    (design §2.4) — a router-only app scans nothing, so there is no redacted
    copy to offer and no consumer may silently substitute the raw one.

    The redaction map is deliberately NOT here. It stays inside the gate and is
    discarded (tokenization is out of scope for P1a); `ctx.redaction_map_id`
    remains the only trace of it.
    """

    #: The request's messages, untouched.
    raw: tuple[Message, ...]
    #: The flattened text the gate classified.
    text_raw: str
    #: Its redaction; None when no detector ran.
    text_redacted: str | None
    #: Entity TYPES only — never values.
    detected_entity_types: tuple[str, ...]
    #: The detector that produced the redacted views; None when none ran.
    detector: Detector | None = None
    #: The effective tool-argument bar (B69) the per-message view applies to
    #: replayed tool-call arguments; `Protection.tool_args_min_score`.
    tool_args_min_score: float = 0.0
    #: The entity types that bar applies to (`Protection.tool_args_bar_types`).
    tool_args_bar_types: frozenset[str] = frozenset(TOOL_ARGS_BAR_TYPES_DEFAULT)
    #: Memo for the lazily built per-message redacted view (see `redacted`).
    _redacted_msgs: dict = field(default_factory=dict, compare=False, repr=False)

    @property
    def redacted(self) -> tuple[Message, ...] | None:
        """The per-message redacted view; None when no detector ran.

        Built ON DEMAND and memoized. Per-message redaction costs one extra
        `analyze` per message (plus two per message carrying tool_calls), and
        its only consumer is the tool-calling path — computing it eagerly for
        every request would roughly double the gate's detector work on the
        ordinary path for nothing.
        """
        if self.text_redacted is None or self.detector is None:
            return None
        if "v" not in self._redacted_msgs:
            pairs = [_redact_message(self.detector, m, self.tool_args_min_score,
                                     self.tool_args_bar_types)
                     for m in self.raw]
            self._redacted_msgs["v"] = tuple(m for m, _ in pairs)
            self._redacted_msgs["stats"] = sum((st for _, st in pairs), ToolArgStats())
        return self._redacted_msgs["v"]

    def tool_arg_stats(self) -> ToolArgStats:
        """The tool-argument bar's counts (B69) over the per-message redacted
        view -- zero when that view was never built (no tool conversation, or
        the model was sent the raw view), because then nothing was rewritten
        or skipped on the way in."""
        return self._redacted_msgs.get("stats", ToolArgStats())


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
                 eff: EffectivePolicy | None = None,
                 workload: str | None = None,
                 inherited_entity_types: frozenset[str] = frozenset(),
                 ) -> tuple[ExecutionPolicyContext, Representations, EffectivePolicy]:
        """Step 2. Returns `(ctx, reps, eff)`:

        * `ctx` — the immutable ExecutionPolicyContext (step 3),
        * `reps` — the explicit `Representations` of the request (design §2.4);
          each consumer selects its own view, none reuses another's,
        * `eff` — the compiled effective policy with the request's signals
          applied (`apply_signals`): the cross-axis rules that fired, the
          resulting placement sources, sensitivity and `require_verification`.

        `detector`/`rules` let a caller supply the app's EffectiveDetector +
        effective_rules (per-app custom recognizers/classification) instead of
        the process-wide default, and `eff` the already-compiled policy those
        were composed from -- see `app/pipeline.py::_gate_inputs`, the one
        place that composition happens. `eff` is compiled here when a caller
        (a test, a script) does not supply one, so the gate is never without
        an effective policy to read.

        `workload` is the client's already-validated, content-free workload
        slug (P2b-3), threaded onto the context for the router's quality term;
        nothing in the gate reads it.
        """
        det = detector or self._detector
        if eff is None:
            eff = compile_policy(app_policy, self._settings)
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
        # "No detector ran" is the NullDetector and nothing else: an app with
        # `pii_detection: off` but custom recognizers layered on top
        # (EffectiveDetector) HAS scanned, and must get a redacted view.
        # Shared with the pipeline's decision-certificate builder via
        # `detection_ran()` (app/policy/detectors.py) so the two structurally
        # cannot disagree.
        ran = detection_ran(det)
        redacted: str | None = None
        mapping: dict[str, str] = {}
        if ran:
            redacted, mapping = det.anonymize(text, entities)
        # The redaction map holds the ORIGINAL values -- a second copy of
        # sensitive data. It never leaves this method (only its id does), and
        # nothing detokenizes anywhere (design §9).
        map_id = f"map-{uuid.uuid4().hex[:8]}" if mapping else None
        # B72: types inherited from the trajectory envelope join the rule
        # match (placement, sensitivity, verification). They never join
        # redaction: there is no text of theirs in this request to redact.
        etypes = {e.type for e in entities} | set(inherited_entity_types)
        ctx = build_context(self._settings, request_id, tenant_id, entities, map_id,
                            app_policy=app_policy, app_rules=rules,
                            registry=self._registry,
                            escalate_residency=eff.escalate_residency_on_detection,
                            content_integrity=eff.protection.content_integrity,
                            # The compiled egress-to-the-MODEL axis, threaded
                            # from the same EffectivePolicy: `build_context`
                            # needs it for P1b's raw-sensitive eligibility rule
                            # (design §4) and must not re-derive it.
                            model_input=eff.protection.model_input,
                            workload=workload,
                            inherited_entity_types=inherited_entity_types)
        ctx = ctx.model_copy(update={
            "requested_model": None if request.model == "auto" else request.model,
        })
        # The SAME matched rule build_context used, so the compiled policy's
        # placement sources and `cross_axis_rules_fired` describe the placement
        # the context actually got.
        rule = match_sensitivity_rule(self._settings, rules, etypes)
        eff = apply_signals(eff, tuple(sorted(etypes)), rule)
        reps = Representations(
            raw=tuple(request.messages),
            text_raw=text,
            text_redacted=redacted,
            detected_entity_types=tuple(sorted(etypes)),
            detector=det if ran else None,
            tool_args_min_score=eff.protection.tool_args_min_score,
            tool_args_bar_types=eff.protection.tool_args_bar_types,
        )
        return ctx, reps, eff
