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

import asyncio
import contextlib
import uuid

from pydantic import BaseModel

from app.agent.internal_endpoint import HOPS
from app.config.loader import load_settings
from app.models import ChatRequest, CompressionRecord, DecisionRecord, ExecutionPolicyContext

NAT_WORKFLOW_CONFIG = "config/nat_workflow.yaml"
DEFAULT_MAX_STEPS = 5


def _gate(run_gate: "asyncio.Semaphore | None"):
    """The concurrency gate as an async context; a no-op when unbounded (None).

    B2: bounds how many multi-step agent runs execute concurrently, so a
    traffic burst can't pile unbounded long-lived inferences onto the single
    onboarded backend (admission control, not inference scaling — CLAUDE.md
    rule 4)."""
    return run_gate if run_gate is not None else contextlib.nullcontext()


def _configure_nat_telemetry(config) -> None:
    """Intentionally does NOT enable NAT's native Langfuse span export.

    NAT's built-in `LangfuseTelemetryExporter` emits the agent's FULL trajectory
    — per-hop prompts, completions, and tool I/O — as RAW text through NAT's own
    OTLP pipeline (`langfuse_telemetry_exporter` -> `OTLPSpanAdapterExporter`),
    which bypasses BOTH:
      - our egress redaction (`app/obs/otel.py` RedactingSpanExporter), so a
        `hospital` multi-step request would push an SSN-bearing tool output or
        completion into Langfuse UNREDACTED — a CLAUDE.md rule 2 violation
        (telemetry redacted on egress); and
      - our per-application project routing (`app/obs/otel.py` get_app_tracer),
        so it lands in the single shared project regardless of tenant.

    It is also redundant: every request — direct AND multi-step — already emits a
    redacted, per-app, per-hop `serving.request` trace via `app/obs/export.py`
    (each `agent_hop` becomes a `serving.agent_hop` child span). NAT's exporter
    was a second, raw copy of the same run.

    NAT provides `OTLPSpanHeaderRedactionAdapterExporter`, but it is not wired to
    any `@register_telemetry_exporter` config type, so it can't be selected via
    `config.general.telemetry.tracing`. Capturing full per-hop I/O for eval is a
    separate, deliberately-gated feature (redacted|full, residency-bounded) — see
    docs/eval-logging-backlog.md — NOT this accidental raw firehose. So we leave
    NAT's native exporter OFF and rely on our own redacted export path.
    """
    return None


class AgentResult(BaseModel):
    text: str
    hops: list[DecisionRecord]
    # Per-hop CompressionRecords (Task 7 / M3): every LLM call the agent
    # makes re-enters compression, so this list is 1:1 with `hops` — see
    # app/agent/internal_endpoint.py's compress() call per hop.
    compressions: list[CompressionRecord] = []


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


async def run(ctx: ExecutionPolicyContext, request: ChatRequest, *, port: int,
              run_gate: "asyncio.Semaphore | None" = None) -> AgentResult:
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
        # NAT's native Langfuse exporter stays OFF (rule 2 + per-app routing):
        # our own redacted per-hop trace covers this. See _configure_nat_telemetry.
        _configure_nat_telemetry(config)

        # B2: hold the concurrency gate only around the actual NAT execution
        # (the expensive, long-lived inference work) — released before the
        # HOPS.pop_all drain below, which must run unconditionally regardless
        # of gate state.
        async with _gate(run_gate):
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
        # pop_all() atomically drains both decision and compression records
        # in the same deletion (see HopCollector.pop_all's docstring).
        hops, compressions = HOPS.pop_all(run_id)

    if _is_cap_exceeded(text):
        raise AgentCapExceeded(
            f"NAT tool_calling_agent exceeded max_iterations without converging "
            f"(run_id={run_id}): {text!r}"
        )

    return AgentResult(text=text, hops=hops, compressions=compressions)
