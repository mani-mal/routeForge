from __future__ import annotations

from app.models import ChatRequest, ExecutionPolicyContext


def decide(ctx: ExecutionPolicyContext, request: ChatRequest) -> str:
    """Step 4 (Task Mode Router) — always "direct".

    RouteForge runs no agent of its own: tools belong to the calling
    application, which drives its own loop and sends each model call as a
    separate request (every one of them re-enters steps 2-9). The step stays
    in the architecture and in the console step list; as before, it emits no
    DecisionRecord of its own.
    """
    return "direct"
