"""Real NeMo Agent Toolkit runtime (Phase 5, Task 4).

Drives a real NAT `tool_calling_agent` workflow (`config/nat_workflow.yaml`)
over live HTTP against our internal endpoint
(`app/agent/internal_endpoint.py`), so every LLM call the agent makes
re-enters the router/gateway (steps 5-7) and gets its own `DecisionRecord` —
not just the outer request. This is the integration crux: NAT is not
simulated here, it drives itself.

Invocation pattern verified against the installed nvidia-nat 1.8 build (see
docs/superpowers/specs/2026-07-21-phase5-nat-integration-spike.md section 3,
and this task's manual confirmation):

- `nat.runtime.loader.load_config(path)` returns a mutable pydantic `Config`
  object (not frozen) — `config.llms["sp_router"]` (an `OpenAIModelConfig`,
  `model_config = {"extra": "allow"}`, no `frozen=True`) can be mutated
  directly per-run: `.base_url = "..."` and `.api_key = SecretStr("...")`.
  IMPORTANT: `api_key` must be assigned a `pydantic.SecretStr`, not a plain
  `str` — assignment isn't re-validated (no `validate_assignment=True`), so a
  plain string blows up later with `AttributeError: 'str' object has no
  attribute 'get_secret_value'` deep inside NAT's langchain LLM wrapper.
- `config.workflow.max_iterations` (the `tool_calling_agent`'s step cap) is
  likewise mutable — used here to enforce the real cap from
  `policy.yaml`'s `agent_budgets.max_steps` (the YAML's own `max_iterations`
  is only a fallback for standalone `nat validate`/`nat run`).
- `WorkflowBuilder.from_config(config=config)` -> `SessionManager.create(...)`
  -> `session_manager.session()` -> `session.run(text)` -> `runner.result(to_type=str)`
  is the exact working programmatic-invocation chain (mirrors
  `scratch-nat-spike/programmatic_run.py`), confirmed to round-trip against a
  live instance of this app's own `/internal/v1/{run_id}/chat/completions`.

NAT's OpenAI provider (LangChain `ChatOpenAI`) appends `/chat/completions` to
`base_url` itself, so `base_url` here is set to
`http://127.0.0.1:{port}/internal/v1/{run_id}` — NOT
`.../{run_id}/chat/completions` and NOT with a trailing `/v1` (the endpoint's
route, `internal_router` in `app/agent/internal_endpoint.py`, is
`/internal/v1/{run_id}/chat/completions`, no extra `/v1` segment).

nat is imported lazily (inside `run`), never at module import time, so that
importing `app.agent` doesn't require the (optional, extras-gated) `nat`
package to be installed.
"""
from __future__ import annotations

import uuid

from pydantic import BaseModel

from app.agent.internal_endpoint import HOPS
from app.config.loader import load_settings
from app.models import ChatRequest, DecisionRecord, ExecutionPolicyContext

NAT_WORKFLOW_CONFIG = "config/nat_workflow.yaml"
DEFAULT_MAX_STEPS = 5


class AgentResult(BaseModel):
    text: str
    hops: list[DecisionRecord]


class AgentCapExceeded(RuntimeError):
    """Raised when NAT's `tool_calling_agent` exhausts `max_iterations`
    without converging on a final answer.

    NAT does NOT raise in this case: it internally catches its own
    `GraphRecursionError` and returns a DEGRADED natural-language message
    (e.g. "The tool calling agent could not produce a final answer within
    N iterations...") as an ordinary `runner.result()` string, indistinguishable
    from a real answer unless inspected. Serving that string as a normal
    `AgentResult` would violate the fail-closed rule (CLAUDE.md rule 5) and
    this task's "enforce max_iterations" intent, so `run()` detects it and
    raises this instead, letting the pipeline (Task 5) map it to an error
    response.
    """


# NAT's tool_calling_agent has no structured non-convergence signal (no
# distinct exception, no status field on the result) — the only indicator
# is this substring in the final answer text. It is coupled to nvidia-nat's
# internal `tool_calling_agent` implementation and MUST be re-verified
# whenever the `nat` dependency is upgraded.
_CAP_EXCEEDED_MARKER = "could not produce a final answer within"


def _is_cap_exceeded(text: str) -> bool:
    """True if NAT's final answer is its own non-convergence message.

    This is the PRIMARY and only detection signal — deliberately not
    hop-count based. A legitimately hard question can take exactly
    `max_iterations` hops and still converge to a real answer on the last
    one; keying off hop count alone would false-positive on that case.
    """
    return _CAP_EXCEEDED_MARKER in (text or "").lower()


async def run(ctx: ExecutionPolicyContext, request: ChatRequest, *, port: int) -> AgentResult:
    """Run the NAT `tool_calling_agent` workflow for one chat request.

    Every LLM call the agent makes is a real HTTP POST to our own internal
    endpoint (`.../internal/v1/{run_id}/chat/completions`), which re-enters
    the router (step 6) per hop using `ctx`. On any exception the per-run hop
    state is still drained (`HOPS.pop`) before re-raising — fail closed, the
    caller (pipeline) handles the error; we just must not leak run state.

    Raises `AgentCapExceeded` if NAT's own `tool_calling_agent` exhausts
    `max_iterations` without converging — NAT itself does not raise for
    this, it swallows it internally and returns a DEGRADED text answer, so
    we detect and re-raise it here (fail closed; see `_is_cap_exceeded`).
    """
    # Lazy import: nat is an optional extra (`--extra agent`); importing
    # app.agent must not require it to be installed.
    from pydantic import SecretStr

    from nat.builder.workflow_builder import WorkflowBuilder
    from nat.runtime.loader import load_config
    from nat.runtime.session import SessionManager

    run_id = uuid.uuid4().hex
    user_text = request.messages[-1].content

    HOPS.start(run_id, ctx)
    try:
        settings = load_settings()
        max_steps = int(settings.policy.agent_budgets.get("max_steps", DEFAULT_MAX_STEPS))

        config = load_config(NAT_WORKFLOW_CONFIG)
        config.llms["sp_router"].base_url = f"http://127.0.0.1:{port}/internal/v1/{run_id}"
        config.llms["sp_router"].api_key = SecretStr("sk-internal")
        config.workflow.max_iterations = max_steps

        async with WorkflowBuilder.from_config(config=config) as builder:
            session_manager = await SessionManager.create(config=config, shared_builder=builder)
            try:
                async with session_manager.session() as session:
                    async with session.run(user_text) as runner:
                        text = await runner.result(to_type=str)
            finally:
                await session_manager.shutdown()
    finally:
        # Always drain the per-run hop collector, even when we are about to
        # raise AgentCapExceeded below — fail closed without leaking state.
        hops = HOPS.pop(run_id)

    if _is_cap_exceeded(text):
        raise AgentCapExceeded(
            f"NAT tool_calling_agent exceeded max_iterations without converging "
            f"(run_id={run_id}): {text!r}"
        )

    return AgentResult(text=text, hops=hops)
