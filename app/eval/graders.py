"""Graders for FeasibleRouteBench (spec §4.1).

A grader scores a completion's text against an expected answer, in `[0, 1]`.
This slice's five kinds are all 0/1 (no partial credit): `exact` (string
equality after stripping whitespace), `contains` (case-insensitive
substring), `regex` (full-match on stripped text), `json_schema` (the text
parses as JSON and validates against the schema), `numeric` (the first
number found in the text is within `tol` of `expected`).

Offline harness code only -- never imported by the serving path.
"""
from __future__ import annotations

import json
import re
from typing import Literal

import jsonschema
from pydantic import BaseModel, PrivateAttr

_NUMBER_RE = re.compile(r"[-+]?\d+(?:\.\d+)?")


class GraderSpec(BaseModel):
    """How one case is graded. `expected`'s shape depends on `kind`: a
    string for exact/contains/regex, a JSON Schema dict for json_schema, a
    number for numeric."""

    kind: Literal["exact", "contains", "regex", "json_schema", "numeric"]
    expected: str | dict | float
    tol: float = 0.0
    # A literal answer known to score 1.0 against this grader. Required for
    # `regex`/`json_schema` (spec §5.1): those graders match a pattern or
    # schema, not a literal value, so the simulated backend (Task 3) has
    # nothing to return for `u < q` without one. `compile_grader` validates
    # it actually passes. Unused (optional) for exact/contains/numeric,
    # where `str(expected)` always passes and doubles as the simulated
    # answer.
    sample: str | None = None

    # Compiled by `compile_grader` at suite-load time so `grade()` never
    # recompiles per case. Never set directly by callers.
    _compiled_regex: re.Pattern[str] | None = PrivateAttr(default=None)


def compile_grader(spec: GraderSpec) -> None:
    """Validate `spec` and prepare its load-time state, in place.

    Raises `ValueError` -- the case loader wraps this into `SuiteError` with
    the offending case's id -- on:
    - `expected` of the wrong shape for `kind`;
    - an unparseable `regex` pattern;
    - an invalid `json_schema` document;
    - a `regex`/`json_schema` grader with no `sample`, or a `sample` that
      does not itself score 1.0 against the grader (spec §5.1: the
      simulated backend needs a literal answer it can hand back).
    """
    if spec.kind in ("exact", "contains", "regex"):
        if not isinstance(spec.expected, str):
            raise ValueError(
                f"grader kind {spec.kind!r} requires a string expected value")
    if spec.kind == "regex":
        try:
            spec._compiled_regex = re.compile(spec.expected)
        except re.error as exc:
            raise ValueError(f"invalid regex: {exc}") from exc
    elif spec.kind == "json_schema":
        if not isinstance(spec.expected, dict):
            raise ValueError(
                "grader kind 'json_schema' requires a dict (JSON Schema) expected value")
        try:
            jsonschema.Draft202012Validator.check_schema(spec.expected)
        except jsonschema.exceptions.SchemaError as exc:
            raise ValueError(f"invalid json schema: {exc}") from exc
    elif spec.kind == "numeric":
        if isinstance(spec.expected, bool) or not isinstance(spec.expected, (int, float)):
            raise ValueError("grader kind 'numeric' requires a numeric expected value")

    if spec.kind in ("regex", "json_schema"):
        if spec.sample is None:
            raise ValueError(
                f"grader kind {spec.kind!r} requires a 'sample' answer that "
                "passes its own grader")
        if grade(spec, spec.sample) < 1.0:
            raise ValueError(
                f"grader kind {spec.kind!r} 'sample' does not pass its own grader")


def grade(spec: GraderSpec, text: str) -> float:
    """Score `text` against `spec`. `spec` must already have gone through
    `compile_grader` (the loader does this for every case)."""
    if spec.kind == "exact":
        return 1.0 if text.strip() == spec.expected.strip() else 0.0
    if spec.kind == "contains":
        return 1.0 if spec.expected.lower() in text.lower() else 0.0
    if spec.kind == "regex":
        pattern = spec._compiled_regex or re.compile(spec.expected)
        return 1.0 if pattern.fullmatch(text.strip()) else 0.0
    if spec.kind == "json_schema":
        try:
            instance = json.loads(text)
        except json.JSONDecodeError:
            return 0.0
        try:
            jsonschema.validate(instance=instance, schema=spec.expected)
        except jsonschema.exceptions.ValidationError:
            return 0.0
        return 1.0
    if spec.kind == "numeric":
        match = _NUMBER_RE.search(text)
        if match is None:
            return 0.0
        value = float(match.group())
        return 1.0 if abs(value - float(spec.expected)) <= spec.tol else 0.0
    raise ValueError(f"unknown grader kind: {spec.kind}")
