"""Case-suite loading for FeasibleRouteBench (spec §4.1).

A suite is JSONL: one JSON object per line, each parseable as a `Case`.
**Load-time validation fails the whole run, never mid-run** -- every case is
validated (and, for a `regex` grader, compiled) before any case is used.

Offline harness code only -- never imported by the serving path.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import fields as _dataclass_fields

from pydantic import BaseModel, ValidationError

from app.apps.policy import AppPolicyView
from app.eval.graders import GraderSpec, compile_grader
from app.policy.detectors import make_detector

# AppPolicyView fields whose value is `frozenset[str] | None` -- a case's
# app_policy dict spells these as JSON lists; policy_view() converts them.
_SET_FIELDS = frozenset({
    "allowed_tiers", "allowed_providers", "allowed_models",
    "pii_entity_denylist", "pii_exempt_patterns", "allowed_regions",
})
_APP_POLICY_FIELDS = frozenset(f.name for f in _dataclass_fields(AppPolicyView))


class SuiteError(ValueError):
    """A case-suite load-time error (§4.1). Always names the offending
    case's id, so a load failure is actionable without re-reading the file."""

    def __init__(self, case_id: str, message: str) -> None:
        self.case_id = case_id
        super().__init__(f"{case_id}: {message}")


class Case(BaseModel):
    id: str
    messages: list[dict]
    app_policy: dict = {}
    grader: GraderSpec
    quality_floor: float = 1.0
    tags: dict[str, str | bool] = {}
    expect_empty: bool = False

    def policy_view(self) -> AppPolicyView:
        """Build the `AppPolicyView` the pipeline consumes -- via the same
        constructor production uses, never a hand-assembled context.

        Raises `ValueError` on a key `AppPolicyView` doesn't have."""
        converted: dict = {}
        for key, value in self.app_policy.items():
            if key not in _APP_POLICY_FIELDS:
                raise ValueError(f"unknown app_policy key: {key}")
            if key in _SET_FIELDS and isinstance(value, list):
                converted[key] = frozenset(value)
            else:
                converted[key] = value
        return AppPolicyView(**converted)


def load_suite(path: str) -> tuple[list[Case], str]:
    """Load a JSONL case suite.

    Returns `(cases, digest)` where `digest` is the sha256 of the file's
    normalised content: each non-blank line JSON-parsed and re-dumped with
    `sort_keys=True`, joined by `\\n` -- stable under key reordering within a
    line.

    Raises `SuiteError` for every §4.1 load error: invalid JSON, a case that
    fails `Case` validation (unknown grader kind, missing `expected`, ...), a
    duplicate id, an invalid regex or JSON Schema, an unknown `app_policy`
    key, or message content the synthetic-PII check flags without
    `tags.synthetic: true`.
    """
    detector = make_detector("regex")
    cases: list[Case] = []
    seen_ids: set[str] = set()
    normalised_lines: list[str] = []

    with open(path, encoding="utf-8") as f:
        raw_lines = [line for line in f if line.strip()]

    for lineno, raw in enumerate(raw_lines, start=1):
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise SuiteError(f"<line {lineno}>", f"invalid JSON: {exc}") from exc

        raw_id = obj.get("id") if isinstance(obj, dict) else None
        label = raw_id if isinstance(raw_id, str) else f"<line {lineno}>"

        try:
            case = Case(**obj)
        except ValidationError as exc:
            raise SuiteError(label, str(exc)) from exc

        if case.id in seen_ids:
            raise SuiteError(case.id, "duplicate id")
        seen_ids.add(case.id)

        try:
            compile_grader(case.grader)
        except ValueError as exc:
            raise SuiteError(case.id, str(exc)) from exc

        try:
            case.policy_view()
        except ValueError as exc:
            raise SuiteError(case.id, str(exc)) from exc

        if case.tags.get("synthetic") is not True:
            for message in case.messages:
                content = message.get("content")
                if not isinstance(content, str):
                    continue
                if detector.analyze(content):
                    raise SuiteError(
                        case.id,
                        "message content is flagged as containing PII; tag "
                        "the case tags.synthetic: true if it is synthetic",
                    )

        cases.append(case)
        normalised_lines.append(json.dumps(obj, sort_keys=True))

    digest = hashlib.sha256("\n".join(normalised_lines).encode("utf-8")).hexdigest()
    return cases, digest
