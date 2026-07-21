from __future__ import annotations

from app.models import ChatRequest, ExecutionPolicyContext


def decide(ctx: ExecutionPolicyContext, request: ChatRequest) -> str:
    """Rules-first task mode. Classifier is OFF this pass (design §10.3)."""
    return "direct"
