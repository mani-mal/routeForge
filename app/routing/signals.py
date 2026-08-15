"""Feature 2 — derive a RAISE-ONLY tier bias from agent-loop state.

Pure function of a hop's observable message history. Returns a suggested
min_tier ('mid' | 'frontier') that the caller feeds into Router.select as the
min_tier arg, where it only ever JOINS the max() -- the residency clamp still
caps it, so an onprem_only request can never be lifted to cloud (CLAUDE.md
rules 3 & 8). Role is a heuristic and the reason string says so ('inferred').
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from app.config.schema import StageSignalsCfg

_ERROR_MARKERS = (r"\berror\b", r"\bexception\b", r"\btraceback\b",
                  r"\bfailed\b", r"\bfailure\b")
_NEGATED = ("no error", "without error", "no failure", "error-free", "no errors")


@dataclass(frozen=True)
class SignalResult:
    min_tier: str | None
    reason: str | None


def _looks_like_error(content: str) -> bool:
    low = content.lower()
    if any(neg in low for neg in _NEGATED):
        return False
    return any(re.search(m, low) for m in _ERROR_MARKERS)


def derive_signal_tier(messages: list[dict], hop_index: int,
                       cfg: StageSignalsCfg) -> SignalResult:
    if not cfg.enabled:
        return SignalResult(None, None)

    tool_msgs = [m for m in messages if m.get("role") == "tool"]

    # Role signal (heuristic): a first hop with no tool activity yet is a
    # planning turn -> bias to frontier.
    if cfg.planner_role_raise and hop_index <= 1 and not tool_msgs:
        return SignalResult("frontier",
                            f"inferred role: plan (hop {hop_index}) -> frontier")

    # Stage signal (grounded): the latest tool result reports an error.
    if cfg.tool_error_raise and tool_msgs and _looks_like_error(
            str(tool_msgs[-1].get("content", ""))):
        return SignalResult("mid", "stage: tool-error -> raised to mid")

    # Stage signal (grounded): the same tool has been called >= N times ->
    # the agent is stuck exploring.
    if cfg.exploration_repeat and tool_msgs:
        names = [m.get("name") for m in tool_msgs if m.get("name")]
        for n in set(names):
            if names.count(n) >= cfg.exploration_repeat:
                return SignalResult(
                    "mid", f"stage: exploration ({n} x{names.count(n)}) -> raised to mid")

    return SignalResult(None, None)
