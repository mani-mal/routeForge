"""Evidence allowlist for Switchyard outcomes (B103; containment rule from B104).

`RoutingOutcome.metadata.evidence` is an open JSON object upstream. Only keys
named here survive, and only when the value has the declared type: a finite
number, or a string from a closed enum. Everything else is dropped, including a
free-text string under an allowed key. Nothing here ever carries message text.

Derived from nemo-switchyard 0.4.0a1, by reading the stage-router source and by
running it (see the B103 report):

* `crates/libsy/src/algorithms/util/stage.rs`, `StageClassifier::score`, sets
  `{"source"}` for holds and overrides, and
  `{"source", "confidence", "threshold"}` for a signal (`dimensions`) decision.
* `crates/libsy/src/algorithms/stage.rs` (`SourceStamp`, `FallOpen`) sets
  `{"source": "fall_open"}` when the picker default decides.
* `DecisionSource::as_str` is the closed set of `source` labels. A refused
  classifier call yields `source: "fail_open"` plus a `reason_code`; that path
  is an advisor fallback anyway, so `reason_code` is not allowlisted.

Observed values: clean production -> `{"source": "dimensions", "confidence":
0.874..., "threshold": 0.5}`; repeated failing tests -> `{"source":
"override"}`; no tool history -> `{"source": "fall_open"}`.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Any

# `DecisionSource::as_str` in 0.4.0a1, plus the classifier's `fail_open` label.
DECISION_SOURCES = frozenset(
    {
        "override",
        "capable_hold",
        "efficient_hold",
        "dimensions",
        "ambiguous",
        "llm-classifier",
        "fall_open",
        "fail_open",
    }
)

_NUMBER = "number"

# key -> either _NUMBER or the closed set of strings the value may take.
EVIDENCE_ALLOWLIST: dict[str, str | frozenset[str]] = {
    "source": DECISION_SOURCES,
    "confidence": _NUMBER,
    "threshold": _NUMBER,
}


def _is_number(value: Any) -> bool:
    # bool is an int subclass in Python; it is not a number here.
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    return math.isfinite(value)


def filter_evidence(evidence: Any) -> dict[str, Any]:
    """Return only the allowlisted, correctly typed evidence entries."""
    if not isinstance(evidence, Mapping):
        return {}
    kept: dict[str, Any] = {}
    for key, rule in EVIDENCE_ALLOWLIST.items():
        if key not in evidence:
            continue
        value = evidence[key]
        if rule == _NUMBER:
            if _is_number(value):
                kept[key] = float(value)
        elif isinstance(value, str) and value in rule:
            kept[key] = value
    return kept
