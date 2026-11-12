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


# --- exemptions survive a detector's own dedupe merge (security review) -------

class _MergingDetector:
    """Returns fixed overlapping entities run through `dedupe`, exactly as
    PresidioDetector / EffectiveDetector hand them to the redactor: one merged
    span per overlap cluster, typed and scored by its best member only."""

    def __init__(self, needle: str, members: list[tuple[str, int, int, float | None, str | None]]):
        self._needle = needle
        self._members = members

    def analyze(self, text: str) -> list[Entity]:
        from app.policy.merge import dedupe
        out: list[Entity] = []
        i = text.find(self._needle)
        while i != -1:
            for etype, s, t, score, source in self._members:
                out.append(Entity(type=etype, start=i + s, end=i + t,
                                  value=text[i + s:i + t], score=score, source=source))
            i = text.find(self._needle, i + 1)
        return dedupe(out)

    def anonymize(self, text, entities):
        return RegexDetector().anonymize(text, entities)

    def warmup(self) -> None:
        return None


@pytest.mark.parametrize("label,members,bar", [
    # A custom match loses the tie-break to a longer, equally weak Presidio span.
    ("custom", [("US_DRIVER_LICENSE", 0, 9, 0.3, None),
                ("EMPLOYEE_ID", 1, 8, 0.3, "custom")], 0.5),
    # An unscored (regex-style) match inside a weak scored one.
    ("unscored", [("US_DRIVER_LICENSE", 0, 9, 0.3, None),
                  ("US_BANK_NUMBER", 1, 9, None, None)], 0.5),
    # A context-scored SSN overlapped by a higher-scoring type, under a bar
    # raised above both: the SSN guarantee must survive the merge.
    ("ssn", [("PHONE_NUMBER", 0, 9, 0.75, None),
             ("US_SSN", 0, 9, 0.4, None)], 0.9),
])
@pytest.mark.parametrize("as_json", [True, False])
def test_exempt_member_of_a_merged_span_keeps_it_redacted(label, members, bar, as_json):
    needle = "X48273194"
    args = json.dumps({"v": needle}) if as_json else f"lookup {needle} now"
    # PHONE_NUMBER is added to the bar's types so the SSN case is a real test
    # of the merge (by default PHONE_NUMBER is never subject to the bar).
    out = redact_tool_calls_with_entities([_call(args)], _MergingDetector(needle, members),
                                          min_score=bar,
                                          bar_types={"US_DRIVER_LICENSE", "PHONE_NUMBER"})
    assert needle not in _args(out.tool_calls), label
    assert out.skipped == 0 and out.redacted >= 1


def test_dedupe_carries_the_exemption_and_changes_nothing_else():
    from app.policy.merge import dedupe
    weak = Entity(type="US_DRIVER_LICENSE", start=0, end=9, value="X48273194", score=0.3)
    custom = Entity(type="EMPLOYEE_ID", start=1, end=8, value="4827319", score=0.3,
                    source="custom")
    (merged,) = dedupe([weak, custom])
    assert (merged.type, merged.start, merged.end, merged.score) == ("US_DRIVER_LICENSE", 0, 9, 0.3)
    assert merged.bar_exempt is True
    # A cluster with no exempt member stays bar-eligible.
    other = Entity(type="DATE_TIME", start=2, end=6, value="8273", score=0.6)
    (plain,) = dedupe([weak, other])
    assert plain.type == "DATE_TIME" and plain.bar_exempt is False
    # The B4/B20 SSN backstop still wins its same-span tie-break.
    ssn = Entity(type="US_SSN", start=0, end=11, value="818-30-4221", score=1.0)
    dt = Entity(type="DATE_TIME", start=0, end=11, value="818-30-4221", score=0.85)
    (won,) = dedupe([dt, ssn])
    assert won.type == "US_SSN" and won.score == 1.0


# --- fix round 1: the bar applies to the listed (noisy) types only ----------

class _TypedDetector:
    """One fixed (regex, type, score) per recognizer, no dedupe."""

    def __init__(self, *rules: tuple[str, str, float]):
        self._rules = [(re.compile(p), t, sc) for p, t, sc in rules]

    def analyze(self, text: str) -> list[Entity]:
        return [Entity(type=t, start=m.start(), end=m.end(), value=m.group(), score=sc)
                for rx, t, sc in self._rules for m in rx.finditer(text)]

    def anonymize(self, text, entities):
        return RegexDetector().anonymize(text, entities)

    def warmup(self) -> None:
        return None


@pytest.mark.parametrize("etype,value,score", [
    ("PHONE_NUMBER", "212-555-0187", 0.4),
    ("US_BANK_NUMBER", "000123456789", 0.4),
    ("US_PASSPORT", "912803456", 0.4),
    ("EMAIL_ADDRESS", "jane.doe@example.com", 0.3),
])
@pytest.mark.parametrize("as_json", [True, False])
def test_unlisted_types_are_always_redacted_whatever_the_score(etype, value, score, as_json):
    det = _TypedDetector((re.escape(value), etype, score),
                         (r"(?<![A-Za-z0-9])[A-Za-z]\d{1,5}\b", "US_DRIVER_LICENSE", 0.3))
    args = (json.dumps({"cmd": "xargs -0 -n1 printf", "v": value}) if as_json
            else f"xargs -0 -n1 printf {value}")
    out = redact_tool_calls_with_entities([_call(args)], det, min_score=0.5)
    got = _args(out.tool_calls)
    assert value not in got and f"REDACTED_{etype}_1" in got
    assert "-n1" in got                                   # the listed weak type is skipped
    assert out.skipped_types == frozenset({"US_DRIVER_LICENSE"})
    assert out.redacted == 1


def test_an_operator_added_type_is_subject_to_the_bar():
    det = _TypedDetector(("212-555-0187", "PHONE_NUMBER", 0.4))
    args = "call 212-555-0187"
    out = redact_tool_calls_with_entities(
        [_call(args)], det, min_score=0.5,
        bar_types={"US_DRIVER_LICENSE", "PHONE_NUMBER"})
    assert _args(out.tool_calls) == args
    assert out.skipped_types == frozenset({"PHONE_NUMBER"})


def test_a_weak_span_overlapping_a_kept_one_is_redacted_with_it():
    # M3: kept PERSON "John Smith" 0.85 overlaps weak DL "Smith Q12345" 0.3.
    # Leaving the weak span out would leak "Q12345" raw next to the placeholder.
    det = _TypedDetector((r"John Smith", "PERSON", 0.85),
                         (r"Smith Q12345", "US_DRIVER_LICENSE", 0.3))
    for args in ("echo John Smith Q12345", json.dumps({"c": "echo John Smith Q12345"})):
        out = redact_tool_calls_with_entities([_call(args)], det, min_score=0.5)
        got = _args(out.tool_calls)
        assert "Q12345" not in got and "Smith" not in got
        assert "echo REDACTED_PERSON_1" in got
        assert out.skipped == 0 and out.skipped_types == frozenset()


def test_gate_uses_the_deployment_bar_types():
    s = copy.deepcopy(load_settings())
    s.serving.tool_args_bar_types = ["PERSON"]             # DL no longer listed
    det = StubDetector()
    req = ChatRequest(messages=[
        Message(role="user", content="list"),
        Message(role="assistant", content=None, tool_calls=[_call("xargs -0 -n1 printf")]),
        Message(role="tool", tool_call_id="c0", content="ok"),
    ])
    _, reps, eff = Gate(s, det).evaluate("r1", "acme", req, detector=det)
    assert eff.protection.tool_args_bar_types == frozenset({"PERSON"})
    assert "-n1" not in _args(reps.redacted[1].tool_calls)
    assert reps.tool_arg_stats().skipped == 0


@pytest.mark.parametrize("bar_types,code", [
    (["US_DRIVER_LICENSE", "US_SSN"], "TOOL_ARGS_BAR_TYPE_PROTECTED"),
    (["US_DRIVER_LICENSE", ""], "TOOL_ARGS_BAR_TYPES_INVALID"),
    (["US_DRIVER_LICENSE", 3], "TOOL_ARGS_BAR_TYPES_INVALID"),
])
def test_bar_types_lint(bar_types, code):
    from app.config.lint import lint_settings
    s = copy.deepcopy(load_settings())
    s.serving.tool_args_bar_types = bar_types
    assert any(code in e for e in lint_settings(s))


def test_default_bar_types_lint_clean_and_default_when_missing():
    from app.config.lint import lint_settings
    from app.policy.enums import TOOL_ARGS_BAR_TYPES_DEFAULT
    s = load_settings()
    assert s.serving.tool_args_bar_types == list(TOOL_ARGS_BAR_TYPES_DEFAULT)
    assert not [e for e in lint_settings(s) if "TOOL_ARGS_BAR" in e]


# --- FilteredDetector: a merged cluster containing US_SSN is never dropped ---

class _FixedMerged:
    def __init__(self, ents):
        self._ents = ents

    def analyze(self, text):
        from app.policy.merge import dedupe
        return dedupe(list(self._ents))

    def anonymize(self, text, entities):
        return RegexDetector().anonymize(text, entities)

    def warmup(self):
        return None


@pytest.mark.parametrize("kwargs", [
    {"denylist": ["PHONE_NUMBER"]},
    {"score_threshold": 0.9},
    {"exempt_patterns": [r"\d{9}"]},
])
def test_filtered_detector_keeps_a_merged_span_that_contains_an_ssn(kwargs):
    from app.policy.effective import FilteredDetector
    phone = Entity(type="PHONE_NUMBER", start=0, end=9, value="482731946", score=0.75)
    ssn = Entity(type="US_SSN", start=0, end=9, value="482731946", score=0.4)
    (merged,) = _FixedMerged([phone, ssn]).analyze("482731946")
    assert merged.type == "PHONE_NUMBER" and merged.contains_protected is True
    kept = FilteredDetector(_FixedMerged([phone, ssn]), **kwargs).analyze("482731946")
    assert len(kept) == 1 and kept[0].start == 0 and kept[0].end == 9
    # Without an SSN member the same filters still drop it (behaviour unchanged).
    other = Entity(type="DATE_TIME", start=0, end=9, value="482731946", score=0.4)
    assert FilteredDetector(_FixedMerged([phone, other]), **kwargs).analyze("482731946") == []


# --- compile: provenance and the save-time invariant -----------------------

def test_threshold_raised_bar_is_attributed_and_axis_kept_raw():
    from app.apps.policy import AppPolicyView
    from app.policy.compile import check_invariants, compile_policy
    s = load_settings()
    eff = compile_policy(AppPolicyView(pii_score_threshold=0.7), s)
    assert eff.protection.tool_args_min_score == 0.7
    assert eff.protection.tool_args_min_score_axis == 0.5
    assert eff.protection.sources["tool_args_min_score"] == "pii_score_threshold"
    eff = compile_policy(AppPolicyView(pii_score_threshold=0.2, tool_args_min_score=0.6), s)
    assert eff.protection.tool_args_min_score == 0.6
    assert eff.protection.sources["tool_args_min_score"] == "app_policy"
    check_invariants(AppPolicyView(pii_score_threshold=0.7), s)   # does not raise


def test_invariant_catches_a_non_monotonic_bar(monkeypatch):
    import app.policy.compile as c
    from app.apps.policy import AppPolicyView
    real = c.resolve_tool_args_min_score

    def inverted(value, default):        # a broken resolver: 0.0 -> 1.0
        v, ok = real(value, default)
        return (1.0 - v if value is not None else v), ok

    monkeypatch.setattr(c, "resolve_tool_args_min_score", inverted)
    with pytest.raises(c.PolicyViolation) as exc:
        c.check_invariants(AppPolicyView(pii_score_threshold=0.7), load_settings())
    assert exc.value.code == "INVARIANT_PROTECTION_AXIS_NOT_MONOTONIC"
