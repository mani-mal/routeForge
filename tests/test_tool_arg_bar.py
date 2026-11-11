"""B69: the tool-argument bar -- a minimum detector score for redaction inside
tool-call `function.arguments`.

Tool-call arguments are a command channel the client EXECUTES. A weak,
context-free pattern match (Presidio's 0.3 `US_DRIVER_LICENSE` on `n1`,
`n20`, `c1500`) must pass through unredacted there; a context-boosted or
format-backed match is still redacted; US_SSN is redacted whatever its score;
an unscored (regex) or operator-declared (custom recognizer) match is never
dropped. Detection itself is unchanged, and message text is untouched by the
bar. Every skipped match is counted and named (types only)."""
from __future__ import annotations

import copy
import json
import re

import pytest

from app.config.loader import load_settings
from app.config.schema import SensitivityRule
from app.models import ChatRequest, Entity, Message
from app.policy.detectors import RegexDetector
from app.policy.effective import EffectiveDetector
from app.policy.gate import Gate, redact_message, redact_tool_calls_with_entities
from app.policy.recognizers import CustomRecognizer

COMMANDS = ["xargs -0 -n1 printf '%q\\n'", "tail -n20 log", "head -c1500 f"]

_WEAK = re.compile(r"(?<![A-Za-z0-9])[A-Za-z]\d{1,5}\b")
_PERSON = re.compile(r"\bJohn Smith\b")
_SSN9 = re.compile(r"\b\d{9}\b")


class StubDetector:
    """Presidio-shaped scores without Presidio: a 0.3 `US_DRIVER_LICENSE` on
    any letter-then-digits token (`n1`, `n20`, `c1500`), a 0.85 `PERSON` on
    "John Smith", and a 0.3 `US_SSN` on a bare 9-digit run."""

    def analyze(self, text: str) -> list[Entity]:
        out = [Entity(type="US_DRIVER_LICENSE", start=m.start(), end=m.end(),
                      value=m.group(), score=0.3) for m in _WEAK.finditer(text)]
        out += [Entity(type="PERSON", start=m.start(), end=m.end(),
                       value=m.group(), score=0.85) for m in _PERSON.finditer(text)]
        out += [Entity(type="US_SSN", start=m.start(), end=m.end(),
                       value=m.group(), score=0.3) for m in _SSN9.finditer(text)]
        return out

    def anonymize(self, text, entities):
        return RegexDetector().anonymize(text, entities)

    def warmup(self) -> None:
        return None


def _call(arguments: str, i: int = 0) -> dict:
    return {"id": f"c{i}", "type": "function",
            "function": {"name": "bash", "arguments": arguments}}


def _args(tool_calls, i: int = 0) -> str:
    return tool_calls[i]["function"]["arguments"]


@pytest.mark.parametrize("command", COMMANDS)
def test_weak_match_in_json_arguments_passes_byte_identical(command):
    args = json.dumps({"command": command})
    out = redact_tool_calls_with_entities([_call(args)], StubDetector(), min_score=0.5)
    assert _args(out.tool_calls) == args
    assert out.redacted == 0
    assert out.skipped >= 1
    assert out.skipped_types == frozenset({"US_DRIVER_LICENSE"})
    # Detection is unchanged: the type is still reported as found.
    assert "US_DRIVER_LICENSE" in out.entity_types


@pytest.mark.parametrize("command", COMMANDS)
def test_weak_match_in_non_json_arguments_passes_byte_identical(command):
    out = redact_tool_calls_with_entities([_call(command)], StubDetector(), min_score=0.5)
    assert _args(out.tool_calls) == command
    assert out.skipped >= 1 and out.redacted == 0
    assert out.skipped_types == frozenset({"US_DRIVER_LICENSE"})


def test_strong_match_is_still_redacted_with_an_inert_placeholder():
    args = json.dumps({"command": "xargs -0 -n1 printf", "user": "John Smith"})
    out = redact_tool_calls_with_entities([_call(args)], StubDetector(), min_score=0.5)
    parsed = json.loads(_args(out.tool_calls))
    assert parsed == {"command": "xargs -0 -n1 printf", "user": "REDACTED_PERSON_1"}
    assert out.redacted == 1
    assert out.skipped == 1
    assert out.skipped_types == frozenset({"US_DRIVER_LICENSE"})


def test_non_json_strong_match_redacted_weak_kept():
    out = redact_tool_calls_with_entities(
        [_call("tail -n20 /home/John Smith/log")], StubDetector(), min_score=0.5)
    assert _args(out.tool_calls) == "tail -n20 /home/REDACTED_PERSON_1/log"
    assert (out.redacted, out.skipped) == (1, 1)


def test_counts_are_per_span_not_per_detector_pass():
    # The weak span is seen by both the context pass and the raw pass: it is
    # still ONE span that passed through.
    args = json.dumps({"a": "tail -n20 x", "b": "head -c1500 y"})
    out = redact_tool_calls_with_entities([_call(args)], StubDetector(), min_score=0.5)
    assert out.skipped == 2


def test_default_bar_is_zero_so_the_helper_alone_changes_nothing():
    out = redact_tool_calls_with_entities([_call("xargs -0 -n1 printf")], StubDetector())
    assert _args(out.tool_calls) == "xargs -0 -REDACTED_US_DRIVER_LICENSE_1 printf"
    assert (out.redacted, out.skipped) == (1, 0)
    assert out.skipped_types == frozenset()


def test_bar_lowered_below_the_weak_score_redacts_it_again():
    out = redact_tool_calls_with_entities([_call("xargs -0 -n1 printf")], StubDetector(),
                                          min_score=0.2)
    assert "n1" not in _args(out.tool_calls)
    assert out.skipped == 0


def test_bar_raised_above_the_strong_score_lets_it_pass():
    args = json.dumps({"user": "John Smith"})
    out = redact_tool_calls_with_entities([_call(args)], StubDetector(), min_score=0.9)
    assert _args(out.tool_calls) == args
    assert out.skipped_types == frozenset({"PERSON"})


@pytest.mark.parametrize("args", [json.dumps({"id": "482731946"}), "lookup 482731946"])
def test_ssn_is_never_exempted_by_the_bar(args):
    out = redact_tool_calls_with_entities([_call(args)], StubDetector(), min_score=1.0)
    assert "482731946" not in _args(out.tool_calls)
    assert "REDACTED_US_SSN_1" in _args(out.tool_calls)
    assert out.skipped == 0


def test_unscored_regex_matches_are_never_dropped():
    args = json.dumps({"ssn": "482-73-1946", "email": "a.b@example.com"})
    out = redact_tool_calls_with_entities([_call(args)], RegexDetector(), min_score=1.0)
    assert json.loads(_args(out.tool_calls)) == {
        "ssn": "REDACTED_US_SSN_1", "email": "REDACTED_EMAIL_ADDRESS_1"}
    assert out.skipped == 0


def test_custom_recognizer_matches_are_never_dropped():
    det = EffectiveDetector(RegexDetector(), [CustomRecognizer("EMPLOYEE_ID", r"EMP-\d{4}",
                                                               score=0.3)])
    out = redact_tool_calls_with_entities([_call("grep EMP-1234 roster")], det, min_score=0.9)
    assert _args(out.tool_calls) == "grep REDACTED_EMPLOYEE_ID_1 roster"
    assert out.skipped == 0


def test_message_content_is_untouched_by_the_bar():
    m = Message(role="assistant", content="ran xargs -0 -n1 printf",
                tool_calls=[_call("xargs -0 -n1 printf")])
    out = redact_message(StubDetector(), m, min_score=0.5)
    assert out.content == "ran xargs -0 -<US_DRIVER_LICENSE_1> printf"
    assert _args(out.tool_calls) == "xargs -0 -n1 printf"


def _settings_with_dl_rule():
    s = copy.deepcopy(load_settings())
    s.policy.sensitivity_rules.insert(0, SensitivityRule(
        entities=["US_DRIVER_LICENSE"], sensitivity="restricted", residency="onprem_only",
        risk_class="high", require_verification=True))
    return s


def test_detection_unchanged_skipped_type_still_classifies():
    s = _settings_with_dl_rule()
    det = StubDetector()
    req = ChatRequest(messages=[
        Message(role="user", content="list the files"),
        Message(role="assistant", content=None, tool_calls=[_call("xargs -0 -n1 printf")]),
        Message(role="tool", tool_call_id="c0", content="ok"),
    ])
    ctx, reps, eff = Gate(s, det).evaluate("r1", "acme", req, detector=det)
    assert "US_DRIVER_LICENSE" in ctx.detected_entities
    assert "rule:restricted" in eff.cross_axis_rules_fired
    assert ctx.onprem_only is True
    # The gate's per-message redacted view applies the deployment bar (0.5).
    assert eff.protection.tool_args_min_score == 0.5
    redacted = reps.redacted
    assert _args(redacted[1].tool_calls) == "xargs -0 -n1 printf"
    stats = reps.tool_arg_stats()
    assert (stats.redacted, stats.skipped) == (0, 1)
    assert stats.skipped_types == frozenset({"US_DRIVER_LICENSE"})
