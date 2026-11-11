"""Typed serving errors mapped to honest HTTP statuses at the API route
(app/api/routes.py). Replaces the old catch-all 503 "policy evaluation
unavailable" (B1). Subclasses are chosen so existing pytest.raises(ValueError/
RuntimeError) callers keep passing."""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.policy.core import ConflictingCore


class PolicyGateError(Exception):
    """The identity/privacy/policy gate (or its detector) failed to evaluate.
    Fail-closed: the request is rejected. Route maps -> 503."""


class NoEligibleCandidate(ValueError):
    """No model survived the residency/budget/scope filter -- e.g. an
    onprem_only request with no healthy on-prem model (CLAUDE.md rule 3).
    Route maps -> 422. Subclasses ValueError for back-compat with existing
    pytest.raises(ValueError) router callers.

    `rejected` is the candidate set that caused this rejection, as
    `(name, code)` tuples -- content-free (P2b-2 outcome records read it via
    `app/apps/outcomes.py`'s failure builder; `code` is `None` where the
    raising stage has no stable code of its own, e.g. the router's own
    budget filter). Keyword-only, defaulted, so every existing
    `NoEligibleCandidate("msg")` construction keeps working unchanged.

    `core` (P3b) is the minimal conflicting core (`app/policy/core.py::
    ConflictingCore`): the smallest set of requirement ids no candidate
    satisfies together, plus endpoint state. Content-free. Every
    `NoEligibleCandidate` the Router raises carries one; `None` for other
    raisers, and for the Router only if computing the core itself failed --
    the core explains a rejection, it never decides one.

    `below_floor` (B70) is set, to the request's hard tier floor, only when
    the Router refused because every candidate at or above that floor has
    already failed on this request and the survivors are all below it --
    failover never lowers a request below its risk/escalation floor. That
    rejection is endpoint state, not policy, so it carries no core; it is
    only ever raised with a non-empty `exclude`, which the gateway rewraps
    into `BackendUnavailable` (502)."""

    def __init__(self, *args, rejected: tuple = (),
                 core: "ConflictingCore | None" = None,
                 below_floor: str | None = None) -> None:
        super().__init__(*args)
        self.rejected = tuple(rejected)
        self.core = core
        self.below_floor = below_floor


class BackendUnavailable(RuntimeError):
    """Every reachable tier's backend failed (timeout/connection/provider
    unreachable). Route maps -> 502. Subclasses RuntimeError for back-compat
    with existing pytest.raises(RuntimeError) gateway callers.

    `attempted_models` lists the candidates the gateway tried, in order
    (signal hardening, spec §4) -- the route meters the last one on its
    `error` usage row.

    `routing_trace` (B70 item 2) is the content-free dump of the gateway's
    decision records up to the failure (`app/apps/outcomes.py::
    decision_trace`), so the `backend_failed` outcome row keeps its eligible
    set, tier target and attempt chain. `no_failover` (B70 item 1) is True
    when exactly one endpoint was eligible (at or above the request's
    floor, within policy and budget) and it failed -- a feasible set of one,
    not a generic outage."""

    def __init__(self, *args, attempted_models: tuple[str, ...] = (),
                 routing_trace: list[dict] | None = None,
                 no_failover: bool = False) -> None:
        super().__init__(*args)
        self.attempted_models = tuple(attempted_models)
        self.routing_trace = routing_trace
        self.no_failover = no_failover


class ContextWindowExceeded(ValueError):
    """The request (prompt + requested output tokens) is larger than the model's
    context window, so the upstream refused it as a bad request. This is the
    client's error, not an outage: route maps -> 400 with the upstream reason,
    so the client compacts its history instead of retrying a 502. The gateway
    only surfaces it when EVERY model it tried refused for this reason; any
    other failure in the mix keeps the request a 502 (BackendUnavailable).

    `attempted_models` (like `BackendUnavailable`'s) lists the candidates the
    gateway tried, in order, so the request's `context_overflow` routing
    outcome names them. Empty when raised by a single backend call.
    `routing_trace` is as on `BackendUnavailable` (None when raised by a
    single backend call)."""

    def __init__(self, *args, attempted_models: tuple[str, ...] = (),
                 routing_trace: list[dict] | None = None) -> None:
        super().__init__(*args)
        self.attempted_models = tuple(attempted_models)
        self.routing_trace = routing_trace
