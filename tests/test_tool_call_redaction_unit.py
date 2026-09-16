"""Unit tests for `redact_tool_calls` / `redact_tool_calls_with_entities`
(app/policy/gate.py). `function.arguments` is a JSON string every OpenAI
client `json.loads`es, so redaction must never produce invalid JSON: JSON
arguments are walked leaf by leaf, non-JSON arguments fall back to a
whole-string splice, and clean/passthrough arguments come back byte-for-byte."""
import json

import pytest

from app.policy.detectors import NullDetector, RegexDetector, make_detector
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
    assert parsed == {"ts": "<US_BANK_NUMBER_1>", "ssn": "<US_SSN_1>"}
    assert SSN not in _args(out)


def test_unmatched_numeric_leaf_stays_a_number():
    out = redact_tool_calls([_call(json.dumps({"n": 42, "ssn": SSN, "f": 1.5, "b": True}))],
                            RegexDetector())
    parsed = json.loads(_args(out))
    assert parsed == {"n": 42, "ssn": "<US_SSN_1>", "f": 1.5, "b": True}


def test_url_value_yields_valid_json():
    args = json.dumps({"url": "https://example.com/a?b=1", "email": "a.b@example.com"})
    out = redact_tool_calls([_call(args)], RegexDetector())
    parsed = json.loads(_args(out))
    assert parsed == {"url": "https://example.com/a?b=1", "email": "<EMAIL_ADDRESS_1>"}


def test_nested_dicts_and_lists_are_walked():
    args = json.dumps({"q": {"people": [{"ssn": SSN}, {"ssn": "123-45-6789"}], "ids": [1, 12345678]},
                       "note": None})
    out = redact_tool_calls([_call(args)], RegexDetector())
    parsed = json.loads(_args(out))
    assert parsed == {"q": {"people": [{"ssn": "<US_SSN_1>"}, {"ssn": "<US_SSN_1>"}],
                            "ids": [1, "<US_BANK_NUMBER_1>"]},
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
    assert parsed == {"note": "naïve ssn <US_SSN_1>", "quote": 'say "hi"'}
    assert "naïve" in _args(out)  # ensure_ascii=False


def test_top_level_json_scalar_is_redacted_to_valid_json():
    out = redact_tool_calls([_call(json.dumps(SSN))], RegexDetector())
    assert json.loads(_args(out)) == "<US_SSN_1>"


def test_non_json_arguments_fall_back_to_whole_string_splice():
    args = "ssn=482-73-1946, not json {"
    res = redact_tool_calls_with_entities([_call(args)], RegexDetector())
    assert _args(res.tool_calls) == "ssn=<US_SSN_1>, not json {"
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
    assert json.loads(got["function"]["arguments"]) == {"ssn": "<US_SSN_1>"}


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
