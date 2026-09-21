"""Tests for `app.eval.graders` and `app.eval.cases` (Task 1, FeasibleRouteBench).

Covers both the grader kinds and the JSONL case-suite loader's §4.1 load-time
validation. See docs/superpowers/specs/2026-09-21-feasible-route-bench-design.md §4.1.
"""
from __future__ import annotations

import json

import pytest

from app.apps.policy import AppPolicyView
from app.eval.cases import Case, SuiteError, load_suite
from app.eval.graders import GraderSpec, compile_grader, grade


# --------------------------------------------------------------------------
# Graders
# --------------------------------------------------------------------------

def _spec(**kw) -> GraderSpec:
    spec = GraderSpec(**kw)
    compile_grader(spec)
    return spec


class TestExactGrader:
    def test_matches_after_stripping_whitespace(self):
        spec = _spec(kind="exact", expected="approved")
        assert grade(spec, "  approved  \n") == 1.0

    def test_mismatch_scores_zero(self):
        spec = _spec(kind="exact", expected="approved")
        assert grade(spec, "denied") == 0.0


class TestContainsGrader:
    def test_case_insensitive_substring_matches(self):
        spec = _spec(kind="contains", expected="Approved")
        assert grade(spec, "the claim was approved today") == 1.0

    def test_missing_substring_scores_zero(self):
        spec = _spec(kind="contains", expected="approved")
        assert grade(spec, "the claim was denied") == 0.0


class TestRegexGrader:
    def test_full_match_on_stripped_text(self):
        spec = _spec(kind="regex", expected=r"\d{3}-\d{2}")
        assert grade(spec, "  123-45  ") == 1.0

    def test_partial_match_does_not_pass_fullmatch(self):
        spec = _spec(kind="regex", expected=r"\d{3}-\d{2}")
        assert grade(spec, "123-45 extra") == 0.0

    def test_invalid_regex_rejected_at_load_not_at_grade(self):
        spec = GraderSpec(kind="regex", expected="(unclosed")
        with pytest.raises(ValueError):
            compile_grader(spec)


class TestJsonSchemaGrader:
    _schema = {"type": "object", "properties": {"ok": {"type": "boolean"}},
               "required": ["ok"]}

    def test_valid_instance_scores_one(self):
        spec = _spec(kind="json_schema", expected=self._schema)
        assert grade(spec, json.dumps({"ok": True})) == 1.0

    def test_invalid_instance_scores_zero(self):
        spec = _spec(kind="json_schema", expected=self._schema)
        assert grade(spec, json.dumps({"ok": "not-a-bool"})) == 0.0

    def test_non_json_text_scores_zero(self):
        spec = _spec(kind="json_schema", expected=self._schema)
        assert grade(spec, "not json at all") == 0.0

    def test_invalid_schema_rejected_at_load(self):
        spec = GraderSpec(kind="json_schema", expected={"type": "not-a-real-type"})
        with pytest.raises(ValueError):
            compile_grader(spec)


class TestNumericGrader:
    def test_first_number_within_tolerance_passes(self):
        spec = _spec(kind="numeric", expected=42, tol=0.2)
        assert grade(spec, "about 41.9 units") == 1.0

    def test_first_number_outside_tight_tolerance_fails(self):
        spec = _spec(kind="numeric", expected=42, tol=0.05)
        assert grade(spec, "about 41.9 units") == 0.0

    def test_no_number_in_text_scores_zero(self):
        spec = _spec(kind="numeric", expected=42, tol=0.2)
        assert grade(spec, "no digits here") == 0.0


# --------------------------------------------------------------------------
# Case.policy_view()
# --------------------------------------------------------------------------

class TestCasePolicyView:
    def test_list_valued_fields_become_frozensets(self):
        case = Case(
            id="c1",
            messages=[{"role": "user", "content": "hi"}],
            app_policy={"allowed_tiers": ["cheap", "mid"], "residency": "onprem_only"},
            grader=GraderSpec(kind="exact", expected="ok"),
        )
        view = case.policy_view()
        assert isinstance(view, AppPolicyView)
        assert view.allowed_tiers == frozenset({"cheap", "mid"})
        assert view.residency == "onprem_only"

    def test_unknown_app_policy_key_raises(self):
        case = Case(
            id="c1",
            messages=[{"role": "user", "content": "hi"}],
            app_policy={"not_a_real_field": True},
            grader=GraderSpec(kind="exact", expected="ok"),
        )
        with pytest.raises(ValueError):
            case.policy_view()


# --------------------------------------------------------------------------
# load_suite
# --------------------------------------------------------------------------

def _line(**kw) -> str:
    base = {
        "id": "case-1",
        "messages": [{"role": "user", "content": "hello there"}],
        "grader": {"kind": "exact", "expected": "hi"},
    }
    base.update(kw)
    return json.dumps(base)


class TestLoadSuiteValid:
    def test_loads_cases_and_returns_stable_hash(self, tmp_path):
        path = tmp_path / "suite.jsonl"
        path.write_text(_line(id="a") + "\n" + _line(id="b") + "\n")
        cases, digest = load_suite(str(path))
        assert [c.id for c in cases] == ["a", "b"]
        assert isinstance(digest, str) and len(digest) == 64

    def test_hash_stable_under_key_reordering_within_a_line(self, tmp_path):
        obj_a = {"id": "a", "grader": {"kind": "exact", "expected": "hi"},
                  "messages": [{"role": "user", "content": "hello there"}]}
        obj_b = {"messages": [{"role": "user", "content": "hello there"}],
                  "id": "a", "grader": {"expected": "hi", "kind": "exact"}}
        path_a = tmp_path / "a.jsonl"
        path_b = tmp_path / "b.jsonl"
        path_a.write_text(json.dumps(obj_a) + "\n")
        path_b.write_text(json.dumps(obj_b) + "\n")
        _, hash_a = load_suite(str(path_a))
        _, hash_b = load_suite(str(path_b))
        assert hash_a == hash_b

    def test_synthetic_pii_tagged_true_loads(self, tmp_path):
        path = tmp_path / "suite.jsonl"
        path.write_text(_line(
            id="pii-1",
            messages=[{"role": "user", "content": "SSN 078-05-1120 on file"}],
            tags={"synthetic": True},
        ) + "\n")
        cases, _ = load_suite(str(path))
        assert cases[0].id == "pii-1"


class TestLoadSuiteErrors:
    def test_duplicate_id_raises_suite_error(self, tmp_path):
        path = tmp_path / "suite.jsonl"
        path.write_text(_line(id="dup") + "\n" + _line(id="dup") + "\n")
        with pytest.raises(SuiteError):
            load_suite(str(path))

    def test_unknown_grader_kind_raises_suite_error(self, tmp_path):
        path = tmp_path / "suite.jsonl"
        path.write_text(_line(grader={"kind": "bogus", "expected": "hi"}) + "\n")
        with pytest.raises(SuiteError):
            load_suite(str(path))

    def test_missing_expected_raises_suite_error(self, tmp_path):
        path = tmp_path / "suite.jsonl"
        path.write_text(_line(grader={"kind": "exact"}) + "\n")
        with pytest.raises(SuiteError):
            load_suite(str(path))

    def test_invalid_schema_raises_suite_error(self, tmp_path):
        path = tmp_path / "suite.jsonl"
        path.write_text(_line(
            grader={"kind": "json_schema", "expected": {"type": "not-a-real-type"}},
        ) + "\n")
        with pytest.raises(SuiteError):
            load_suite(str(path))

    def test_unknown_app_policy_key_raises_suite_error(self, tmp_path):
        path = tmp_path / "suite.jsonl"
        path.write_text(_line(app_policy={"not_a_real_field": True}) + "\n")
        with pytest.raises(SuiteError):
            load_suite(str(path))

    def test_synthetic_pii_without_tag_raises_suite_error(self, tmp_path):
        path = tmp_path / "suite.jsonl"
        path.write_text(_line(
            id="pii-2",
            messages=[{"role": "user", "content": "SSN 078-05-1120 on file"}],
        ) + "\n")
        with pytest.raises(SuiteError):
            load_suite(str(path))

    def test_invalid_regex_raises_suite_error(self, tmp_path):
        path = tmp_path / "suite.jsonl"
        path.write_text(_line(
            grader={"kind": "regex", "expected": "(unclosed"},
        ) + "\n")
        with pytest.raises(SuiteError):
            load_suite(str(path))
