"""Feature 2 — derive a RAISE-ONLY tier bias from agent-loop state.

Pure function of a hop's observable message history. Returns a suggested
min_tier ('mid' | 'frontier') that the caller feeds into Router.select as the
min_tier arg, where it only ever JOINS the max() -- the residency clamp still
caps it, so an onprem_only request can never be lifted to cloud (CLAUDE.md
rules 3 & 8). Role is a heuristic and the reason string says so ('inferred').

Task 1 extends the single "plan" heuristic to full role tagging (plan /
verify / execute / format), inferred from observable message state only (no
LLM classifier call -- rules-first). The role -> min_tier raise is
config-driven via StageSignalsCfg.role_tiers so operators can tune it. All
signals (role bias + the two grounded stage signals below) are evaluated and
combined by STRONGEST raise, not first-match.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from app.config.schema import StageSignalsCfg
from app.routing.router import _ORDER as _TIER_ORDER

_ERROR_MARKERS = (r"\berrors?\b", r"\bexceptions?\b", r"\btraceback\b",
                  r"\bfailed\b", r"\bfailures?\b")
_NEGATED = ("no error", "without error", "no failure", "error-free", "no errors")

# Role cues (evaluate in this order; first match wins for the role label).
# Matched case-insensitively, word-boundary, against the most recent
# non-tool (user/assistant) message content.
_VERIFY_MARKERS = (r"\bverify\b", r"\bvalidate\b", r"\bcheck\b",
                   r"\bdouble-check\b", r"\bis this correct\b")
_FORMAT_MARKERS = (r"\bformat\b", r"\bsummarize\b", r"\bsummarise\b",
                   r"\bfinal answer\b", r"\brephrase\b", r"\bmake it\b")


@dataclass(frozen=True)
class SignalResult:
    min_tier: str | None
    reason: str | None


def _looks_like_error(content: str) -> bool:
    low = content.lower()
    if any(neg in low for neg in _NEGATED):
        return False
    return any(re.search(m, low) for m in _ERROR_MARKERS)


def _latest_non_tool_content(messages: list[dict]) -> str:
    for m in reversed(messages):
        if m.get("role") != "tool":
            return str(m.get("content", ""))
    return ""


def _infer_role(messages: list[dict], hop_index: int, tool_msgs: list[dict]) -> str | None:
    # plan -- an early reasoning/decomposition turn: no tool activity yet.
    # (Generalizes the original planner heuristic; same trigger.)
    if hop_index <= 1 and not tool_msgs:
        return "plan"

    low = _latest_non_tool_content(messages).lower()
    if any(re.search(m, low) for m in _VERIFY_MARKERS):
        return "verify"
    if any(re.search(m, low) for m in _FORMAT_MARKERS):
        return "format"

    # execute -- default when tool activity is present and no cue fired
    # (tool calls/results being consumed).
    if tool_msgs:
        return "execute"

    return None


def _role_signal(role: str | None, cfg: StageSignalsCfg) -> tuple[str, str] | None:
    if role is None:
        return None
    if role == "plan" and not cfg.planner_role_raise:
        return None
    tier = cfg.role_tiers.get(role)
    if tier is None:
        return None
    return tier, f"inferred role: {role} -> {tier}"


def derive_signal_tier(messages: list[dict], hop_index: int,
                       cfg: StageSignalsCfg) -> SignalResult:
    if not cfg.enabled:
        return SignalResult(None, None)

    tool_msgs = [m for m in messages if m.get("role") == "tool"]

    firing: list[tuple[str, str]] = []

    role = _infer_role(messages, hop_index, tool_msgs)
    role_sig = _role_signal(role, cfg)
    if role_sig:
        firing.append(role_sig)

    # Stage signal (grounded): the latest tool result reports an error.
    if cfg.tool_error_raise and tool_msgs and _looks_like_error(
            str(tool_msgs[-1].get("content", ""))):
        firing.append(("mid", "stage: tool-error -> raised to mid"))

    # Stage signal (grounded): the same tool has been called >= N times ->
    # the agent is stuck exploring.
    if cfg.exploration_repeat and tool_msgs:
        names = [m.get("name") for m in tool_msgs if m.get("name")]
        for n in sorted(set(names)):
            if names.count(n) >= cfg.exploration_repeat:
                firing.append(
                    ("mid", f"stage: exploration ({n} x{names.count(n)}) -> raised to mid"))
                break

    if not firing:
        return SignalResult(None, None)

    best_tier = max(firing, key=lambda f: _TIER_ORDER[f[0]])[0]
    reason = "; ".join(r for _, r in firing)
    return SignalResult(best_tier, reason)
