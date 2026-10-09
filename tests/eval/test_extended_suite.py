"""The generated extended suite (eval/suites/build_extended.py): it loads, the
checked-in file is what the generator writes, and every group the report
prints is large enough to print."""
import collections
import importlib.util
from pathlib import Path

from app.eval.cases import load_suite
from app.eval.graders import grade

SUITE = "eval/suites/extended.jsonl"


def _generator():
    spec = importlib.util.spec_from_file_location(
        "build_extended", "eval/suites/build_extended.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_checked_in_suite_is_what_the_generator_writes():
    assert Path(SUITE).read_text() == _generator().render()


def test_generator_is_deterministic():
    g = _generator()
    assert g.render() == g.render()


def test_suite_loads_with_unique_ids_and_balanced_groups():
    cases, _ = load_suite(SUITE)
    assert len(cases) == 108 and len({c.id for c in cases}) == 108
    for dim, minimum in (("placement", 18), ("protection", 18), ("workload", 18), ("length", 36)):
        sizes = collections.Counter(c.tags[dim] for c in cases)
        assert min(sizes.values()) >= minimum, (dim, sizes)


def test_every_case_is_answerable_by_its_own_literal_answer():
    """The grader's own sample (or expected value) must score 1.0, and an
    obviously wrong answer must not -- a case no answer can pass, or any
    answer can, measures nothing."""
    cases, _ = load_suite(SUITE)
    for c in cases:
        g = c.grader
        right = g.sample if g.sample is not None else (
            str(int(g.expected)) if g.kind == "numeric" else str(g.expected))
        assert grade(g, right) == 1.0, c.id
        assert grade(g, "I cannot determine that.") == 0.0, c.id


def test_only_the_escalated_cases_carry_a_planted_identifier():
    import re
    cases, _ = load_suite(SUITE)
    ssn = re.compile(r"\b\d{3}-\d{2}-\d{4}\b")
    for c in cases:
        has = bool(ssn.search(c.messages[0]["content"]))
        assert has is (c.tags["placement"] == "escalated"), c.id
