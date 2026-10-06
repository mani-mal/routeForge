"""P3c-2: alternative scenarios, compared side by side (offline, operator-run).

An administrator writes several candidate policy changes into one
`routeforge.policy-scenarios/v1` file. Each scenario is a named
`routeforge.policy-proposal/v1` BODY; every one is replayed over the same
recorded traffic against the same `current` arm (`app/eval/simulate.py`).

RouteForge never picks, ranks or recommends a scenario: they are reported in
file order, and nothing is applied (handoff §28).

Never imported by serving code (`tests/eval/test_isolation.py`).
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass

from app.config.schema import Settings
from app.eval.proposal import (SCHEMA as PROPOSAL_SCHEMA, ProposalError, _validate_keys,
                               _validate_schema, apply_proposal)

SCHEMA = "routeforge.policy-scenarios/v1"
_SCHEMA_PREFIX = "routeforge.policy-scenarios/"
_SCHEMA_MAJOR = "v1"

_TOP_KEYS = frozenset({"schema", "title", "scenarios"})
_SCENARIO_KEYS = frozenset({"name", "proposal"})
MAX_SCENARIOS = 20
MAX_NAME_LEN = 64
# Matched with `fullmatch`, never `match` + `$`: `$` also matches before a
# trailing newline, which would let "name\n" through.
_NAME = re.compile(r"[A-Za-z0-9._+-]+")


class ScenarioError(ValueError):
    """A scenarios-file load/validate error. Always names the offending
    location -- `scenarios[<i>](<name>).proposal.<dotted path>` for an error
    inside a scenario's proposal -- mirroring `ProposalError`."""

    def __init__(self, where: str, message: str) -> None:
        self.where = where
        self.message = message
        super().__init__(f"{where}: {message}")


@dataclass(frozen=True)
class AppliedScenario:
    name: str
    proposal: dict      # the scenario's proposal body as a full v1 proposal
    doc: dict           # the snapshot with that proposal applied


def _scenario_where(index: int, name: str) -> str:
    return f"scenarios[{index}]({name})"


def validate_scenarios(doc) -> None:
    """Shape-only checks (no snapshot needed): the schema, the closed key
    sets, 1..20 scenarios, legal unique names, and every proposal body's own
    keys. Values are checked against the snapshot by `apply_scenarios`."""
    if not isinstance(doc, dict):
        raise ScenarioError("<root>", "must be an object")
    schema = doc.get("schema")
    if not isinstance(schema, str) or not schema.startswith(_SCHEMA_PREFIX):
        raise ScenarioError("schema", f"must start with {_SCHEMA_PREFIX!r}; got {schema!r}")
    major = schema[len(_SCHEMA_PREFIX):].split(".", 1)[0]
    if major != _SCHEMA_MAJOR:
        raise ScenarioError("schema", f"major version must be {_SCHEMA_MAJOR!r}; got {major!r}")
    for key in doc:
        if key not in _TOP_KEYS:
            raise ScenarioError(key, "unknown key")
    title = doc.get("title")
    if title is not None and not isinstance(title, str):
        raise ScenarioError("title", "must be a string")

    scenarios = doc.get("scenarios")
    if not isinstance(scenarios, list):
        raise ScenarioError("scenarios", "must be a list")
    if not 1 <= len(scenarios) <= MAX_SCENARIOS:
        raise ScenarioError(
            "scenarios", f"must list 1 to {MAX_SCENARIOS} scenarios; got {len(scenarios)}")

    seen: set[str] = set()
    for i, entry in enumerate(scenarios):
        if not isinstance(entry, dict):
            raise ScenarioError(f"scenarios[{i}]", "must be an object")
        for key in entry:
            if key not in _SCENARIO_KEYS:
                raise ScenarioError(f"scenarios[{i}].{key}", "unknown key")
        name = entry.get("name")
        if (not isinstance(name, str) or not _NAME.fullmatch(name)
                or len(name) > MAX_NAME_LEN):
            raise ScenarioError(
                f"scenarios[{i}].name",
                f"must be 1 to {MAX_NAME_LEN} characters of [A-Za-z0-9._+-]; got {name!r}")
        if name in seen:
            raise ScenarioError(f"scenarios[{i}].name", f"duplicate scenario name {name!r}")
        seen.add(name)

        where = f"{_scenario_where(i, name)}.proposal"
        body = entry.get("proposal")
        if not isinstance(body, dict):
            raise ScenarioError(where, "must be an object")
        try:
            # `schema` is optional on a body; when present it must be a v1
            # proposal schema, checked exactly as a proposal file's is.
            if "schema" in body:
                _validate_schema(body)
            _validate_keys(body)
        except ProposalError as exc:
            raise ScenarioError(f"{where}.{exc.where}", exc.message) from exc


def load_scenarios(path: str) -> tuple[dict, str]:
    """Read a scenarios JSON file. Returns `(doc, sha256 hex)` -- the hash is
    over the raw file bytes. Raises `ScenarioError` on invalid JSON, a bad
    schema, an unknown key anywhere, or a bad scenario name."""
    with open(path, "rb") as f:
        raw = f.read()
    digest = hashlib.sha256(raw).hexdigest()
    try:
        doc = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ScenarioError("file", f"invalid JSON: {exc}") from exc
    validate_scenarios(doc)
    return doc, digest


def apply_scenarios(snapshot: dict, doc: dict, *, base: Settings) -> list[AppliedScenario]:
    """Validate EVERY scenario's proposal against `snapshot` and apply it,
    in file order. All-or-nothing: the first invalid scenario raises
    `ScenarioError` naming its index, name and dotted path, before any
    replay starts. Never mutates `snapshot` or `doc`."""
    validate_scenarios(doc)
    applied: list[AppliedScenario] = []
    for i, entry in enumerate(doc["scenarios"]):
        proposal = {"schema": PROPOSAL_SCHEMA, **entry["proposal"]}
        try:
            applied_doc = apply_proposal(snapshot, proposal, base=base)
        except ProposalError as exc:
            raise ScenarioError(
                f"{_scenario_where(i, entry['name'])}.proposal.{exc.where}",
                exc.message) from exc
        applied.append(AppliedScenario(entry["name"], proposal, applied_doc))
    return applied
