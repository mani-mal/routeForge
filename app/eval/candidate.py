"""A candidate router configuration for the offline router-version check
(P2 evaluation metrics, spec §7.1).

A candidate is a YAML file: the `routing.yaml` keys an operator is thinking
of changing, and nothing else. It is laid over the deployment's own routing
config IN MEMORY, for one bench run. Nothing here writes a config file,
calls an admin route, or is read by serving code: the file an operator
finally edits is `config/routing.yaml`, by hand, after reading the report.

    schema: routeforge.router-candidate/v1
    name: latency-heavy            # optional, a label for the report
    routing:
      weights: {latency: 0.6}      # merged over the base weights
      complexity_thresholds: {cheap_max: 0.25}

Only the keys in `ALLOWED_KEYS` are accepted. A key the harness cannot
evaluate is refused with the reason (`NOT_EVALUABLE`), never ignored: a
candidate that silently changed nothing would read as "no regression".

Never imported by serving code (`tests/eval/test_isolation.py`).
"""
from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

import yaml

from app.config.lint import lint_settings
from app.config.schema import RoutingCfg, Settings
from app.eval.strategies import _REQUIRED_WEIGHT_KEYS
from app.routing.strategy import build_strategy

SCHEMA = "routeforge.router-candidate/v1"
#: The strategy-name suffix of the candidate's arms (`router_online:candidate`).
ARM = "candidate"

#: `routing.yaml` keys a candidate may state: the ones the bench's router
#: arms read when they choose.
ALLOWED_KEYS = ("weights", "tier_quality", "complexity_thresholds", "risk_floors",
                "strategy", "signals", "breaker")

#: Known `routing.yaml` keys a candidate may NOT state, and why.
NOT_EVALUABLE = {
    "confidence_escalation_threshold":
        "the bench sweep makes one call per case and endpoint with escalation off, "
        "so a change to it moves no bench figure",
    "confidence_logprob_temperature":
        "it shapes a live backend's confidence, which the bench does not score",
    "max_escalations": "the harness sets it to 0 for the sweep",
    "max_failover_attempts":
        "the attempt cap is read from the deployment's own config for every arm",
    "tiers": "the tier list is shared with the endpoint file and the policy config",
    "model_pool": "the endpoint file is the whole endpoint universe of a bench run",
    "score_on": "the bench always scores post-compression tokens",
    "fallback": "the bench does not model same-provider retries",
    "prompt_opt": "managed prompts are not part of a bench run",
    "quality_refresh_s": "it is a serving-process refresh interval",
}


class CandidateError(ValueError):
    """A candidate file failed to load or validate. Names the location."""

    def __init__(self, where: str, message: str) -> None:
        self.where = where
        self.message = message
        super().__init__(f"{where}: {message}")


@dataclass(frozen=True)
class Candidate:
    """A loaded candidate. `routing` is the overlay exactly as stated; `id`
    is the first 12 hex of sha256 over it (sorted keys), so two files that
    state the same overlay have the same id whatever their formatting."""

    name: str | None
    routing: dict
    id: str


def candidate_id(routing: dict) -> str:
    blob = json.dumps(routing, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:12]


def load_candidate(path: str) -> Candidate:
    """Read and validate a `routeforge.router-candidate/v1` file. Raises
    `CandidateError` naming the file and the offending key; `OSError` when
    the file cannot be read."""
    try:
        doc = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise CandidateError(path, f"invalid YAML: {exc}") from exc
    if not isinstance(doc, dict):
        raise CandidateError(path, "must be a mapping")
    unknown = sorted(set(doc) - {"schema", "name", "routing"})
    if unknown:
        raise CandidateError(path, f"unknown key(s) {unknown}; "
                                   "expected schema, name, routing")
    if doc.get("schema") != SCHEMA:
        raise CandidateError(f"{path}: schema",
                             f"must be {SCHEMA!r}; got {doc.get('schema')!r}")
    name = doc.get("name")
    if name is not None and (not isinstance(name, str) or not name.strip()):
        raise CandidateError(f"{path}: name", "must be a non-empty string when given")
    routing = doc.get("routing")
    if not isinstance(routing, dict) or not routing:
        raise CandidateError(f"{path}: routing",
                             "must be a non-empty mapping of routing.yaml keys")
    for key in sorted(routing, key=str):
        if key in NOT_EVALUABLE:
            raise CandidateError(f"{path}: routing.{key}",
                                 f"cannot be evaluated by this check: {NOT_EVALUABLE[key]}")
        if key not in ALLOWED_KEYS:
            raise CandidateError(f"{path}: routing.{key}",
                                 f"is not a key a candidate may state "
                                 f"(allowed: {list(ALLOWED_KEYS)})")
    return Candidate(name=name, routing=copy.deepcopy(routing), id=candidate_id(routing))


def candidate_settings(harness: Settings, candidate: Candidate) -> Settings:
    """A deep copy of `harness` (the bench's own settings, never mutated)
    with the candidate's routing keys laid over it.

    A mapping-valued key is merged ONE level deep over the base value, so
    `weights: {latency: 0.6}` keeps the other three weights; a scalar
    replaces. The result goes through the real `RoutingCfg` validation, the
    required-weights check the weight variants use, the strategy registry
    and the config lint: a candidate the deployment could not boot with is
    refused here.

    Raises `CandidateError`."""
    merged = harness.routing.model_dump()
    for key, value in candidate.routing.items():
        if isinstance(merged.get(key), dict):
            if not isinstance(value, dict):
                raise CandidateError(f"routing.{key}", "must be a mapping")
            merged[key] = {**merged[key], **value}
        else:
            merged[key] = value
    settings = copy.deepcopy(harness)
    try:
        settings.routing = RoutingCfg.model_validate(merged)
    except ValueError as exc:
        raise CandidateError("routing", str(exc)) from exc
    missing = [k for k in _REQUIRED_WEIGHT_KEYS if k not in settings.routing.weights]
    if missing:
        raise CandidateError("routing.weights", f"is missing required key(s) {missing}")
    try:
        # The boot check for this key is in `lint.enforce`, beside checks of
        # telemetry and secrets that are no business of a candidate; this is
        # the same registry lookup the Router does when it is built.
        build_strategy(settings.routing.strategy)
    except ValueError as exc:
        raise CandidateError("routing.strategy", str(exc)) from exc
    # Only what the overlay ADDED: a lint error the base config already has
    # is the deployment's, and the bench has always run despite it.
    before = set(lint_settings(harness))
    errors = [e for e in lint_settings(settings) if e not in before]
    if errors:
        raise CandidateError("routing", "config lint: " + "; ".join(errors))
    return settings
