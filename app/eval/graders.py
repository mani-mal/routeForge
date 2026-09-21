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

    # Compiled by `compile_grader` at suite-load time so `grade()` never
    # recompiles per case. Never set directly by callers.
    _compiled_regex: re.Pattern[str] | None = PrivateAttr(default=None)


def compile_grader(spec: GraderSpec) -> None:
    """Validate `spec` and prepare its load-time state, in place.

    Raises `ValueError` -- the case loader wraps this into `SuiteError` with
    the offending case's id -- on:
    - `expected` of the wrong shape for `kind`;
    - an unparseable `regex` pattern;
    - an invalid `json_schema` document.
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
