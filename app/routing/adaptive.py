"""B108: the effective Switchyard advisor mode for one application.

Pure: no I/O, no settings mutation, no randomness. `build_context` calls
`resolve_adaptive_routing` once and puts the answer on the policy context; the
pipeline reads only those two ctx fields and never re-derives them. Same shape
as `app/routing/quality_floor.py::resolve_floor` (B80), the other per-app
routing setting with a deployment counterpart.

**Not a policy axis (CLAUDE.md rule 2a).** `adaptive_routing` is neither
placement nor protection: it is a routing preference inside the bounds that
placement and policy have already set. Nothing here reads or writes residency,
providers, or any protection value, and nothing that decides those reads this.

Effective mode = min(deployment ceiling, app setting), ordered
`off < shadow < enforce`:

* The ceiling is `routing.yaml` `switchyard.mode`. No app ever exceeds it.
* An app with NO explicit setting inherits the ceiling capped at `shadow`. A
  deployment in `shadow` therefore keeps shadowing every app (the B105
  behaviour), and `enforce` is only ever reached by an explicit per-app
  `adaptive_routing: enforce`.
* An unparseable stored value denies to `off` (the advisor does not run for
  that app); an unparseable sample rate denies to `0.0` (an `enforce` app is
  then never applied -- it records like `shadow`). The admin API refuses both
  on write, so either can only come from a hand-edited row.
"""
from __future__ import annotations

import hashlib
import math
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.apps.policy import AppPolicyView
    from app.config.schema import Settings

ADAPTIVE_ROUTING_MODES: tuple[str, ...] = ("off", "shadow", "enforce")
_RANK = {m: i for i, m in enumerate(ADAPTIVE_ROUTING_MODES)}
#: What an app with no explicit `adaptive_routing` may reach at most.
INHERITED_CAP = "shadow"


def parse_mode(value) -> tuple[str | None, bool]:
    """`(mode, parsed)`. None = no explicit setting (`(None, True)`).
    Anything unparseable denies to `off` and reports `parsed=False`."""
    if value is None:
        return None, True
    if isinstance(value, str) and value in _RANK:
        return value, True
    return "off", False


def parse_sample_rate(value) -> tuple[float, bool]:
    """`(rate, parsed)`. None = 1.0 (every request). A finite number in
    [0, 1] is itself; anything else denies to 0.0 (never applied)."""
    if value is None:
        return 1.0, True
    if (isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(value) and 0.0 <= value <= 1.0):
        return float(value), True
    return 0.0, False


def _min_mode(a: str, b: str) -> str:
    return a if _RANK[a] <= _RANK[b] else b


def resolve_adaptive_routing(settings: "Settings",
                             app_policy: "AppPolicyView | None") -> tuple[str, float]:
    """`(effective_mode, sample_rate)` for one application (see module doc)."""
    ceiling = settings.routing.switchyard.mode
    if ceiling not in _RANK:      # schema-validated; deny anyway
        ceiling = "off"
    stored = getattr(app_policy, "adaptive_routing", None) if app_policy else None
    app_mode, _ok = parse_mode(stored)
    if app_mode is None:
        app_mode = INHERITED_CAP
    rate, _ok = parse_sample_rate(
        getattr(app_policy, "adaptive_routing_sample_rate", None) if app_policy else None)
    return _min_mode(ceiling, app_mode), rate


def sampled(request_id: str, rate: float) -> bool:
    """Deterministic per-request sample: the same request id always lands on
    the same side of `rate`. sha256 of the id, first 8 bytes, as a fraction
    of 2**64. `rate >= 1` always samples; `rate <= 0` never does."""
    if rate >= 1.0:
        return True
    if rate <= 0.0:
        return False
    digest = hashlib.sha256(request_id.encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big") / 2**64 < rate
