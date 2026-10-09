"""Unit tests for `redact_tool_calls` / `redact_tool_calls_with_entities`
(app/policy/gate.py). `function.arguments` is a JSON string every OpenAI
client `json.loads`es, so redaction must never produce invalid JSON: JSON
arguments are walked leaf by leaf, non-JSON arguments fall back to a
whole-string splice, and clean/passthrough arguments come back byte-for-byte."""
import json

import pytest

from app.policy.detectors import NullDetector, RegexDetector, make_detector
from app.policy.effective import FilteredDetector
from app.policy.gate import redact_tool_calls, redact_tool_calls_with_entities

SSN = "482-73-1946"


def _call(arguments, **extra):
    return {"id": "c1", "type": "function",
            "function": {"name": "find_support_ticket", "arguments": arguments}, **extra}


def _args(tool_calls):
    return tool_calls[0]["function"]["arguments"]


def test_numeric_leaf_matching_detector_yields_valid_json():
    # Controller repro: the whole-string splice produced
    # `{"ts": <US_BANK_NUMBER_1>, ...}` -- unparseable.
    out = redact_tool_calls([_call(json.dumps({"ts": 1757980800, "ssn": SSN}))], RegexDetector())
    parsed = json.loads(_args(out))
    assert parsed == {"ts": "REDACTED_US_BANK_NUMBER_1", "ssn": "REDACTED_US_SSN_1"}
    assert SSN not in _args(out)


def test_unmatched_numeric_leaf_stays_a_number():
    out = redact_tool_calls([_call(json.dumps({"n": 42, "ssn": SSN, "f": 1.5, "b": True}))],
                            RegexDetector())
    parsed = json.loads(_args(out))
    assert parsed == {"n": 42, "ssn": "REDACTED_US_SSN_1", "f": 1.5, "b": True}


def test_url_value_yields_valid_json():
    args = json.dumps({"url": "https://example.com/a?b=1", "email": "a.b@example.com"})
    out = redact_tool_calls([_call(args)], RegexDetector())
    parsed = json.loads(_args(out))
    assert parsed == {"url": "https://example.com/a?b=1", "email": "REDACTED_EMAIL_ADDRESS_1"}


def test_nested_dicts_and_lists_are_walked():
    args = json.dumps({"q": {"people": [{"ssn": SSN}, {"ssn": "123-45-6789"}], "ids": [1, 12345678]},
                       "note": None})
    out = redact_tool_calls([_call(args)], RegexDetector())
    parsed = json.loads(_args(out))
    assert parsed == {"q": {"people": [{"ssn": "REDACTED_US_SSN_1"}, {"ssn": "REDACTED_US_SSN_1"}],
                            "ids": [1, "REDACTED_US_BANK_NUMBER_1"]},
                      "note": None}
    assert SSN not in _args(out) and "123-45-6789" not in _args(out)


def test_dict_keys_are_not_redacted():
    args = json.dumps({SSN: "x"})
    out = redact_tool_calls([_call(args)], RegexDetector())
    assert _args(out) == args


def test_escaped_string_is_redacted_and_non_ascii_preserved():
    args = '{"note": "na\\u00efve ssn 482-73-1946", "quote": "say \\"hi\\""}'
    out = redact_tool_calls([_call(args)], RegexDetector())
    parsed = json.loads(_args(out))
    assert parsed == {"note": "naïve ssn REDACTED_US_SSN_1", "quote": 'say "hi"'}
    assert "naïve" in _args(out)  # ensure_ascii=False


def test_top_level_json_scalar_is_redacted_to_valid_json():
    out = redact_tool_calls([_call(json.dumps(SSN))], RegexDetector())
    assert json.loads(_args(out)) == "REDACTED_US_SSN_1"


def test_non_json_arguments_fall_back_to_whole_string_splice():
    args = "ssn=482-73-1946, not json {"
    res = redact_tool_calls_with_entities([_call(args)], RegexDetector())
    assert _args(res.tool_calls) == "ssn=REDACTED_US_SSN_1, not json {"
    assert res.entity_types == frozenset({"US_SSN"})


def test_clean_json_arguments_are_returned_byte_identical():
    # Non-canonical spacing/escapes/number formats would all change under a
    # re-serialize -- nothing detected means the ORIGINAL string comes back.
    args = '{ "city":"Paris",  "n": 1.50, "u": "\\u00e9", "list":[1,2] }'
    tc = _call(args)
    res = redact_tool_calls_with_entities([tc], RegexDetector())
    assert _args(res.tool_calls) is args
    assert res.entity_types == frozenset()


def test_null_detector_is_byte_for_byte_noop():
    args = json.dumps({"ts": 1757980800, "ssn": SSN})
    tcs = [_call(args)]
    res = redact_tool_calls_with_entities(tcs, NullDetector())
    assert res.tool_calls == tcs
    assert _args(res.tool_calls) is args
    assert res.entity_types == frozenset()


@pytest.mark.parametrize("tool_calls", [None, []])
def test_empty_tool_calls_pass_through(tool_calls):
    assert redact_tool_calls(tool_calls, RegexDetector()) is tool_calls
    res = redact_tool_calls_with_entities(tool_calls, RegexDetector())
    assert res.tool_calls is tool_calls
    assert res.entity_types == frozenset()


def test_malformed_calls_pass_through_untouched():
    weird = ["not-a-dict", {"id": "c2"}, {"id": "c3", "function": "nope"},
             {"id": "c4", "function": {"name": "f", "arguments": {"ssn": SSN}}},
             {"id": "c5", "function": {"name": "f"}}]
    out = redact_tool_calls(weird, RegexDetector())
    assert out == weird


def test_extra_keys_preserved_and_input_not_mutated():
    tc = _call(json.dumps({"ssn": SSN}), index=0, custom={"k": 1})
    tc["function"]["strict"] = True
    before = json.dumps(tc)
    out = redact_tool_calls([tc], RegexDetector())
    assert json.dumps(tc) == before  # caller's dict untouched
    got = out[0]
    assert got["id"] == "c1" and got["type"] == "function"
    assert got["index"] == 0 and got["custom"] == {"k": 1}
    assert got["function"]["name"] == "find_support_ticket"
    assert got["function"]["strict"] is True
    assert json.loads(got["function"]["arguments"]) == {"ssn": "REDACTED_US_SSN_1"}


def test_entity_types_are_collected_across_calls():
    tcs = [_call(json.dumps({"ssn": SSN})), _call(json.dumps({"e": "x@y.io"}))]
    res = redact_tool_calls_with_entities(tcs, RegexDetector())
    assert res.entity_types == frozenset({"US_SSN", "EMAIL_ADDRESS"})
    for tc in res.tool_calls:
        json.loads(tc["function"]["arguments"])


@pytest.fixture(scope="module")
def presidio():
    pytest.importorskip("presidio_analyzer")
    return make_detector("presidio")


@pytest.mark.parametrize("value", [
    {"ts": 1757980800},
    {"url": "https://example.com/a?b=1"},
    {"ts": 1757980800, "ssn": SSN, "url": "https://example.com/a?b=1"},
])
def test_presidio_numeric_and_url_leaves_yield_valid_json(presidio, value):
    args = json.dumps(value)
    out = _args(redact_tool_calls([_call(args)], presidio))
    parsed = json.loads(out)  # must never raise
    assert set(parsed) == set(value)
    assert SSN not in out


class _CountingDetector(RegexDetector):
    def __init__(self):
        self.calls = 0

    def analyze(self, text):
        self.calls += 1
        return super().analyze(text)


def test_detection_runs_at_most_twice_regardless_of_calls_and_values():
    det = _CountingDetector()
    tcs = [_call(json.dumps({"a": SSN, "b": [1, 2, 3], "c": {"d": "x", "e": "y"}})),
           _call(json.dumps({"f": "123-45-6789", "g": "z"})), _call("not json 482-73-1946")]
    res = redact_tool_calls_with_entities(tcs, det)
    assert det.calls == 2
    for tc in res.tool_calls[:2]:
        json.loads(tc["function"]["arguments"])
    assert SSN not in json.dumps(res.tool_calls)


class _KeyContextDetector(RegexDetector):
    """Stands in for Presidio's context boost: a bare 9-digit number is only
    an SSN when the word "ssn" is right before it -- which is true in the raw
    JSON (`"ssn": "482731946"`) but not in a value on its own."""

    def analyze(self, text):
        import re
        from app.models import Entity
        return [Entity(type="US_SSN", start=m.start(1), end=m.end(1), value=m.group(1))
                for m in re.finditer(r'ssn": "(\d{9})"', text)]


def test_raw_pass_redacts_value_only_detected_with_json_context():
    args = json.dumps({"ssn": "482731946", "n": 482731946, "other": "keep"})
    res = redact_tool_calls_with_entities([_call(args)], _KeyContextDetector())
    out = _args(res.tool_calls)
    assert json.loads(out) == {"ssn": "REDACTED_US_SSN_1", "n": "REDACTED_US_SSN_1", "other": "keep"}
    assert "482731946" not in out
    assert res.entity_types == frozenset({"US_SSN"})


def test_raw_pass_value_embedded_in_longer_word_is_not_redacted():
    class _Det(RegexDetector):
        def analyze(self, text):
            from app.models import Entity
            i = text.find('"Al"')
            return [Entity(type="PERSON", start=i, end=i + 4, value='"Al"')] if i >= 0 else []
    args = json.dumps({"who": "Al", "note": "Also fine"})
    out = _args(redact_tool_calls([_call(args)], _Det()))
    assert json.loads(out) == {"who": "REDACTED_PERSON_1", "note": "Also fine"}


_BARE_SSN_ARGS = json.dumps({"ssn": "482731946"})


@pytest.mark.parametrize("make", [
    lambda base: base,
    lambda base: FilteredDetector(base, score_threshold=0.3),
    lambda base: FilteredDetector(base, denylist=["US_PASSPORT"]),
], ids=["default", "score_threshold_0.3", "denylist_US_PASSPORT"])
def test_presidio_hyphenless_ssn_under_ssn_key_is_always_redacted(presidio, make):
    # Re-review finding: per-value detection lost the "ssn" key context, so
    # Presidio saw only a low-score US_PASSPORT and a per-app threshold or
    # denylist let the RAW SSN through -- although US_SSN can never be
    # exempted and the gate's whole-text classification saw it.
    res = redact_tool_calls_with_entities([_call(_BARE_SSN_ARGS)], make(presidio))
    out = _args(res.tool_calls)
    json.loads(out)
    assert "482731946" not in out
    assert "US_SSN" in res.entity_types


# --- B69: a redacted argument is executed by the client ----------------------

_SHELL_ACTIVE = set("<>|&;$`(){}*?!\\\"'")


@pytest.mark.parametrize("command", [
    "grep -r 482-73-1946 /var/log | xargs -0 -n1 printf '%q\\n'",
    "echo 482-73-1946 > /tmp/out.txt",
])
def test_placeholder_in_a_shell_command_is_word_characters_only(command):
    """B69: `<US_SSN_1>` inside a command is a shell redirection. The
    placeholder must add no shell-active character the command did not have."""
    args = json.dumps({"command": command})
    out = json.loads(_args(redact_tool_calls([_call(args)], RegexDetector())))["command"]
    assert SSN not in out
    assert "REDACTED_US_SSN_1" in out
    assert "<" not in out.replace(" > /tmp/out.txt", "")
    assert out == command.replace(SSN, "REDACTED_US_SSN_1")
    assert not (_SHELL_ACTIVE & set("REDACTED_US_SSN_1"))


def test_non_json_command_placeholder_is_word_characters_only():
    res = redact_tool_calls_with_entities([_call(f"cat notes | grep {SSN}")], RegexDetector())
    assert _args(res.tool_calls) == "cat notes | grep REDACTED_US_SSN_1"
    assert res.entity_types == frozenset({"US_SSN"})


def test_message_text_keeps_the_angle_bracket_placeholder():
    """Only tool-call arguments change spelling; prose redaction is untouched."""
    det = RegexDetector()
    text, _ = det.anonymize(f"ssn {SSN}", det.analyze(f"ssn {SSN}"))
    assert text == "ssn <US_SSN_1>"
