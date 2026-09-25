"""The minimal conflicting core (P3b design §2.4, §3, §5).

When the feasible set is empty, every available candidate violates at least
one requirement (`app/policy/feasible.py::candidate_violations` /
`app/routing/router.py`'s two budget ids). The **conflicting core** is the
smallest set of requirement ids that meets every candidate's violation set --
a minimum hitting set over that violation matrix, computed by exact search
over the (at most 11-id, so at most 2048-subset) universe, in increasing
size, breaking ties by the fixed `REQUIREMENT_ORDER` (spec §2.1). A minimum
hitting set is automatically irreducible: dropping any of its ids leaves some
candidate violating nothing in the rest.

Endpoint / request **state** (`no_endpoints`, `disabled`, `circuit_open`,
`prior_failure`) is never a requirement and never enters the core -- it is a
fact about an endpoint or this request's history, not policy, and is kept
separate as `ConflictingCore.state`.

Security rule: every requirement whose id is in `SECURITY_IDS` only ever gets
hint text that suggests onboarding or approving an endpoint that satisfies
it. It never suggests relaxing, removing, disabling or otherwise loosening a
security requirement -- `tests/test_conflicting_core.py::test_security_hints_never_relax`
enforces this against a denylist. Nothing here is ever applied automatically.

This module is pure: no I/O, no imports from the router or the gateway. It
consumes only the requirement-id strings (imported from `app/policy/feasible.py`
where that module already defines them, so the alphabet has one source) plus
the caller-supplied violation matrix, state, details and sources.
"""
from __future__ import annotations

import itertools
from dataclasses import dataclass
from typing import Iterable, Mapping, Sequence

from app.policy.feasible import (
    REQ_ALLOWED_MODELS,
    REQ_ALLOWED_PROVIDERS,
    REQ_ALLOWED_REGIONS,
    REQ_ALLOWED_TIERS,
    REQ_ENDPOINT_APP_ALLOWLIST,
    REQ_RAW_SENSITIVE,
    REQ_REQUESTED_MODEL,
    REQ_REQUIRE_NO_RETENTION,
    REQ_RESIDENCY,
    STATE_CIRCUIT_OPEN,
    STATE_DISABLED,
    STATE_NO_ENDPOINTS,
    STATE_PRIOR_FAILURE,
)

# The two budget ids belong to the router's budget stage (design §2.3), not
# to the `feasible_set` seam -- `app/policy/feasible.py` says so explicitly
# and never produces them. Defined here, once, for the core's alphabet.
REQ_MAX_COST_USD_PER_REQUEST = "max_cost_usd_per_request"
REQ_RISK_FLOOR = "risk_floor"

# Spec §2.1 table order -- the alphabet of the core, and the tie-break order
# for `minimum_hitting_set`.
REQUIREMENT_ORDER: tuple[str, ...] = (
    REQ_RESIDENCY,
    REQ_ALLOWED_PROVIDERS,
    REQ_ALLOWED_REGIONS,
    REQ_REQUIRE_NO_RETENTION,
    REQ_RAW_SENSITIVE,
    REQ_ALLOWED_TIERS,
    REQ_ALLOWED_MODELS,
    REQ_ENDPOINT_APP_ALLOWLIST,
    REQ_REQUESTED_MODEL,
    REQ_MAX_COST_USD_PER_REQUEST,
    REQ_RISK_FLOOR,
)

# security: true in spec §2.1's table.
SECURITY_IDS: frozenset[str] = frozenset({
    REQ_RESIDENCY,
    REQ_ALLOWED_PROVIDERS,
    REQ_ALLOWED_REGIONS,
    REQ_REQUIRE_NO_RETENTION,
    REQ_RAW_SENSITIVE,
    REQ_RISK_FLOOR,
})

# Facts about an endpoint or this request's history -- never in the core.
STATE_ORDER: tuple[str, ...] = (
    STATE_NO_ENDPOINTS,
    STATE_DISABLED,
    STATE_CIRCUIT_OPEN,
    STATE_PRIOR_FAILURE,
)

DEFAULT_SOURCE: dict[str, str] = {
    REQ_RESIDENCY: "app_residency",
    REQ_ALLOWED_PROVIDERS: "app_policy",
    REQ_ALLOWED_REGIONS: "app_policy",
    REQ_REQUIRE_NO_RETENTION: "app_policy",
    REQ_RAW_SENSITIVE: "app_policy",
    REQ_ALLOWED_TIERS: "app_policy",
    REQ_ALLOWED_MODELS: "app_policy",
    REQ_ENDPOINT_APP_ALLOWLIST: "endpoint",
    REQ_REQUESTED_MODEL: "request",
    REQ_MAX_COST_USD_PER_REQUEST: "app_policy",
    REQ_RISK_FLOOR: "risk_class",
}


def _fmt(template: str, detail: dict) -> str:
    """`template.format(**detail)`, falling back to the raw template text
    when a referenced key is missing rather than raising -- a `detail` the
    caller under-populated must still produce a readable label. List/tuple
    values are joined with ", " in their given order (e.g. `providers`,
    `regions`, `tiers`, `models`); set/frozenset values are joined sorted,
    since a set has no order of its own -- otherwise a list-valued detail
    would render as its Python repr (`['onprem', 'azure']`) instead of
    operator-facing text ("onprem, azure")."""
    try:
        joined = {k: _joined(v) for k, v in detail.items()}
        return template.format(**joined)
    except (KeyError, IndexError):
        return template


def _joined(value: object) -> object:
    if isinstance(value, (list, tuple)):
        return ", ".join(str(v) for v in value)
    if isinstance(value, (set, frozenset)):
        return ", ".join(sorted(str(v) for v in value))
    return value


def _residency_label(detail: dict, source: str) -> str:
    label = "must run on-premises"
    if source == "sensitivity_rule":
        label += _fmt(" (raised by sensitivity rule on {entities})", detail)
    return label


_LABEL_TEMPLATES: dict[str, object] = {
    REQ_RESIDENCY: _residency_label,
    REQ_ALLOWED_PROVIDERS: "provider must be one of {providers}",
    REQ_ALLOWED_REGIONS: "endpoint region must be one of {regions}",
    REQ_REQUIRE_NO_RETENTION: "endpoint must declare no prompt retention",
    REQ_RAW_SENSITIVE: "endpoint must accept raw sensitive content (the app sends it unredacted)",
    REQ_ALLOWED_TIERS: "tier must be one of {tiers}",
    REQ_ALLOWED_MODELS: "model must be one of {models}",
    REQ_ENDPOINT_APP_ALLOWLIST: "endpoint must allow this application",
    REQ_REQUESTED_MODEL: "request pins model {value}",
    REQ_MAX_COST_USD_PER_REQUEST: "estimated cost must be at most ${cap:.4f} per request",
    REQ_RISK_FLOOR: "tier must be at least {floor} for risk class {risk_class}",
}


def _label(req_id: str, detail: dict, source: str) -> str:
    template = _LABEL_TEMPLATES[req_id]
    if callable(template):
        return template(detail, source)
    return _fmt(template, detail)


# Security ids only ever suggest onboarding/approving an endpoint -- never
# relaxing the requirement (spec §3, §5.7).
_HINT_TEMPLATES: dict[str, tuple[str, ...]] = {
    REQ_RESIDENCY: (
        "Onboard an on-premises endpoint that also satisfies the other requirements listed here.",
    ),
    REQ_ALLOWED_PROVIDERS: (
        "Onboard or approve an endpoint from an allowed provider that also satisfies the other requirements listed here.",
    ),
    REQ_ALLOWED_REGIONS: (
        "Onboard or approve an endpoint declared in an allowed region.",
    ),
    REQ_REQUIRE_NO_RETENTION: (
        "Onboard or approve an endpoint that declares no prompt retention.",
    ),
    REQ_RAW_SENSITIVE: (
        "Onboard or approve an endpoint declared to accept raw sensitive content.",
    ),
    REQ_RISK_FLOOR: (
        "Onboard an endpoint at or above the required tier that fits the other requirements.",
    ),
    REQ_ALLOWED_TIERS: (
        "Onboard an endpoint in an allowed tier, or ask an administrator to review the application's allowed tiers.",
    ),
    REQ_ALLOWED_MODELS: (
        "Onboard or permit an allowed model, or ask an administrator to review the application's allowed models.",
    ),
    REQ_ENDPOINT_APP_ALLOWLIST: (
        "Ask an administrator to allow this application on a suitable endpoint.",
    ),
    REQ_REQUESTED_MODEL: (
        'Send model "auto", or pin a model that satisfies the other requirements.',
    ),
    REQ_MAX_COST_USD_PER_REQUEST: (
        "Lower max_tokens or shorten the prompt, or ask an administrator to review max_cost_usd_per_request.",
    ),
}


# The detail key that carries a requirement's "what would satisfy this"
# list, for ids whose detail has no single `value` -- used by `summary()`.
_PRIMARY_LIST_KEY: dict[str, str] = {
    REQ_ALLOWED_PROVIDERS: "providers",
    REQ_ALLOWED_REGIONS: "regions",
    REQ_ALLOWED_TIERS: "tiers",
    REQ_ALLOWED_MODELS: "models",
}


def _summary_detail(req_id: str, detail: dict) -> str | None:
    """The short parenthetical for one requirement in `summary()`, or `None`
    for a bare id (empty/irrelevant detail). `value` (e.g. `residency`,
    `requested_model`) always wins when present, even alongside other keys
    such as `residency`'s `entities` -- the summary line names what must
    hold, not why."""
    if "value" in detail:
        return str(detail["value"])
    list_key = _PRIMARY_LIST_KEY.get(req_id)
    if list_key and list_key in detail:
        return _joined(detail[list_key])
    if req_id == REQ_MAX_COST_USD_PER_REQUEST and "cap" in detail:
        return f"cap ${detail['cap']:.4f}"
    if req_id == REQ_RISK_FLOOR and "floor" in detail:
        return f">= {detail['floor']}"
    return None


@dataclass(frozen=True)
class Requirement:
    id: str
    security: bool
    source: str
    detail: dict
    label: str
    hints: tuple[str, ...]


@dataclass(frozen=True)
class ConflictingCore:
    requirements: tuple[Requirement, ...]
    state: tuple[tuple[str, tuple[str, ...]], ...]
    candidates_considered: int

    def ids(self) -> tuple[str, ...]:
        return tuple(r.id for r in self.requirements)

    def state_ids(self) -> tuple[str, ...]:
        return tuple(s for s, _models in self.state)

    def to_dict(self) -> dict:
        return {
            "requirements": [
                {
                    "id": r.id,
                    "security": r.security,
                    "source": r.source,
                    "detail": r.detail,
                    "label": r.label,
                    "hints": list(r.hints),
                }
                for r in self.requirements
            ],
            "state": [{"id": s, "models": list(models)} for s, models in self.state],
            "candidates_considered": self.candidates_considered,
        }

    def to_record(self) -> dict:
        record = self.to_dict()
        for r in record["requirements"]:
            r.pop("label", None)
            r.pop("hints", None)
        return record

    def summary(self) -> str:
        parts = []
        for r in self.requirements:
            detail = _summary_detail(r.id, r.detail)
            parts.append(f"{r.id} ({detail})" if detail is not None else r.id)
        return "no endpoint satisfies all of: " + ", ".join(parts)


def minimum_hitting_set(violations: Iterable[frozenset[str]]) -> tuple[str, ...]:
    """The smallest set of ids that intersects every set in `violations`,
    ids ordered by `REQUIREMENT_ORDER`. `()` when there are no violation
    sets. Raises `ValueError` for an unknown id, or for any empty violation
    set (a candidate violating nothing means the caller's set was not
    actually a rejection)."""
    vsets = list(violations)
    if not vsets:
        return ()
    known = set(REQUIREMENT_ORDER)
    for v in vsets:
        if not v:
            raise ValueError("empty violation set: a candidate violating nothing is not a rejection")
        unknown = v - known
        if unknown:
            raise ValueError(f"unknown requirement id(s): {sorted(unknown)}")

    universe = [i for i in REQUIREMENT_ORDER if any(i in v for v in vsets)]

    def covers(combo: tuple[str, ...]) -> bool:
        combo_set = set(combo)
        return all(combo_set & v for v in vsets)

    for k in range(1, len(universe) + 1):
        for combo in itertools.combinations(universe, k):
            if covers(combo):
                return combo
    # Unreachable: `universe` itself always covers (k == len(universe)).
    raise AssertionError("no covering combination found")


def build_core(violations: Mapping[str, frozenset[str]], *,
               state: Mapping[str, Sequence[str]],
               details: Mapping[str, dict],
               sources: Mapping[str, str]) -> ConflictingCore:
    core_ids = minimum_hitting_set(violations.values())

    requirements = []
    for req_id in core_ids:
        detail = dict(details.get(req_id, {}))
        source = sources.get(req_id, DEFAULT_SOURCE[req_id])
        requirements.append(Requirement(
            id=req_id,
            security=req_id in SECURITY_IDS,
            source=source,
            detail=detail,
            label=_label(req_id, detail, source),
            hints=_HINT_TEMPLATES[req_id],
        ))

    state_out = []
    for state_id in STATE_ORDER:
        if state_id not in state:
            continue
        models = tuple(sorted(state[state_id]))
        if models or state_id == STATE_NO_ENDPOINTS:
            state_out.append((state_id, models))

    return ConflictingCore(
        requirements=tuple(requirements),
        state=tuple(state_out),
        candidates_considered=len(violations),
    )
