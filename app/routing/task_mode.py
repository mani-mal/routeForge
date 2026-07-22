from __future__ import annotations

from app.models import ChatRequest, ExecutionPolicyContext

# Multi-step trigger phrases (rules-only; classifier is OFF this pass — §10.3).
_TRIGGER_PHRASES = ("step by step", "look up")


def decide(ctx: ExecutionPolicyContext, request: ChatRequest) -> str:
    """Rules-first task mode. Classifier is OFF this pass (design §10.3).

    Returns "multi_step" when the request opts in explicitly
    (`mock_multistep: true`, a demo-only hint) or the last user message
    contains a multi-step trigger phrase; else "direct".
    """
    if getattr(request, "mock_multistep", False):
        return "multi_step"

    last_user = ""
    for m in reversed(request.messages):
        if m.role == "user":
            last_user = m.content
            break

    lowered = last_user.lower()
    if any(phrase in lowered for phrase in _TRIGGER_PHRASES):
        return "multi_step"

    return "direct"
