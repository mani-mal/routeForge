"""Typed serving errors mapped to honest HTTP statuses at the API route
(app/api/routes.py). Replaces the old catch-all 503 "policy evaluation
unavailable" (B1). Subclasses are chosen so existing pytest.raises(ValueError/
RuntimeError) callers keep passing."""
from __future__ import annotations


class PolicyGateError(Exception):
    """The identity/privacy/policy gate (or its detector) failed to evaluate.
    Fail-closed: the request is rejected. Route maps -> 503."""


class NoEligibleCandidate(ValueError):
    """No model survived the residency/budget/scope filter -- e.g. an
    onprem_only request with no healthy on-prem model (CLAUDE.md rule 3).
    Route maps -> 422. Subclasses ValueError for back-compat with existing
    pytest.raises(ValueError) router callers."""


class BackendUnavailable(RuntimeError):
    """Every reachable tier's backend failed (timeout/connection/provider
    unreachable). Route maps -> 502. Subclasses RuntimeError for back-compat
    with existing pytest.raises(RuntimeError) gateway callers."""
