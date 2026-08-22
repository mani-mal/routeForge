"""Feature 3 (DSPy prompt-optimization) Task 2: optional LLM task-mode
classifier for step 4, wired ONLY through `Router.select` so residency holds
by construction (CLAUDE.md rules 3 & 8). Kept in its own module (rather than
`app/routing/task_mode.py`) so the rules-only fallback stays a tiny,
dependency-free stub -- this module pulls in `Router`, the backend protocol,
and the managed-prompt substrate, none of which the always-on rules path
needs.

**Residency argument (traced, not asserted):** this module never constructs a
backend, a provider client, or a candidate. The ONLY way it reaches a model is
`router.select(ctx, ...)` (identical call shape to every other routed call --
`Gateway.run`, `Gateway.run_stream`) which, as its very first pass (see
`Router.select` step 1), drops every candidate whose `provider not in
ctx.allowed_providers` BEFORE any scoring happens. Under `onprem_only`,
`ctx.allowed_providers` excludes every cloud provider, so `router.select` can
only ever return an on-prem candidate here, or raise `NoEligibleCandidate`
when none is healthy -- there is no third outcome and no code path in this
module that bypasses `router.select` to reach a provider directly. On
`NoEligibleCandidate`, on a backend error, or on an unparseable completion,
`classify_task_mode` falls back to the deterministic `decide()` rules verdict
-- which makes no model call at all -- so a classifier failure of any kind
degrades to the safe rules default and never crashes, rejects, or reaches
cloud under `onprem_only`.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from app.config.schema import Settings
from app.errors import NoEligibleCandidate
from app.gateway.backends.base import InferenceBackend, count_tokens
from app.models import ChatRequest, DecisionRecord, ExecutionPolicyContext, Message
from app.prompts.store import PromptArtifact
from app.routing.router import Router
from app.routing.task_mode import decide

if TYPE_CHECKING:
    from app.apps.registry import ModelRegistry

_STEP = "task_mode_classifier"
_LABELS = ("multi_step", "direct")


def _parse_verdict(text: str | None) -> str | None:
    """Tolerant parse: lowercase/strip, accept the label as a substring
    anywhere in the completion (e.g. "Multi_Step." or "The answer is
    direct")."""
    lowered = (text or "").strip().lower()
    for label in _LABELS:
        if label in lowered:
            return label
    return None


async def classify_task_mode(
    ctx: ExecutionPolicyContext,
    sanitized_text: str,
    artifact: PromptArtifact,
    request: ChatRequest,
    router: Router,
    backend: InferenceBackend,
    settings: Settings,
    registry: "ModelRegistry | None" = None,
) -> tuple[str, DecisionRecord]:
    """Classify `direct` vs `multi_step` via a routed LLM call, falling back
    to the deterministic rules `decide()` on ANY trouble (CLAUDE.md rule 5 --
    fail closed shape for this post-gate step: never a crash, never a reject,
    never a residency bypass). Returns `(mode, DecisionRecord)` -- the record
    always carries `step="task_mode_classifier"` and a `reason` stating the
    verdict and whether the LLM classifier or the rules fallback produced it,
    so its cost (when a call was actually made) reaches the same trace the
    UI/Langfuse consume.

    `sanitized_text` MUST already be gate-sanitized (rule 1 -- nothing
    downstream of the gate sees raw request content); the classifier prompt
    is built ONLY from `artifact.text` (the managed instruction) + this
    sanitized text, never the raw `request`. `request` itself is used only as
    the input to the rules `decide()` fallback, never sent to the model.

    `router`/`backend`/`settings`/`registry` are the SAME instances the
    pipeline already constructed for this process (Gateway's `router` and
    `backend` properties) -- this function never builds a new backend or
    provider client, so there is no seam for it to reach a provider `Router.
    select` didn't approve.
    """
    messages = [
        Message(role="system", content=artifact.text),
        Message(role="user", content=sanitized_text),
    ]
    tokens_after = sum(count_tokens(m.content) for m in messages)
    # A classifier call is cheap; min_tier=None lets scoring pick the
    # cheapest allowed candidate rather than forcing a tier up.
    complexity = min(1.0, tokens_after / 2000)

    def _fallback(reason: str, *, cand=None, decision=None,
                  result=None) -> tuple[str, DecisionRecord]:
        rules_mode = decide(ctx, request)
        record = DecisionRecord(
            step=_STEP,
            chosen=cand.name if cand else None,
            chosen_display_name=cand.display_name if cand else None,
            chosen_tier=cand.tier if cand else None,
            chosen_provider=cand.provider if cand else None,
            reason=f"rules fallback (mode={rules_mode}): {reason}",
            rejected=decision.rejected if decision else [],
            est_cost_usd=decision.est_cost_usd if decision else 0.0,
            actual_cost_usd=result.cost_usd if result else None,
            latency_ms=result.latency_ms if result else 0.0)
        return rules_mode, record

    try:
        # The ONLY seam this module ever reaches a provider through --
        # residency-filtered before anything is scored (see module docstring).
        decision = router.select(ctx, tokens_after, complexity, min_tier=None)
    except NoEligibleCandidate as exc:
        return _fallback(f"no eligible candidate ({exc})")

    cand = decision.chosen
    price = registry.price_for(cand.name) if registry else settings.pricing.models[cand.name]
    try:
        result = await backend.generate(cand.name, messages, price, cand.tier, {})
    except Exception as exc:  # backend/provider trouble of any kind
        return _fallback(f"backend call failed: {exc}", cand=cand, decision=decision)

    mode = _parse_verdict(result.text)
    if mode is None:
        return _fallback(f"unparseable completion {result.text!r}",
                         cand=cand, decision=decision, result=result)

    record = DecisionRecord(
        step=_STEP, chosen=cand.name, chosen_display_name=cand.display_name,
        chosen_tier=cand.tier, chosen_provider=cand.provider,
        reason=f"llm classifier verdict={mode}",
        rejected=decision.rejected, est_cost_usd=decision.est_cost_usd,
        actual_cost_usd=result.cost_usd, latency_ms=result.latency_ms)
    return mode, record
