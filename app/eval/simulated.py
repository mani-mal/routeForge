"""Deterministic simulated backend for FeasibleRouteBench (spec §5.1, §3).

Implements `InferenceBackend` (`app/gateway/backends/base.py`) without any
network call. For a fixed `--seed`, quality and latency for a given
`(case.id, model)` are a pure function of that seed -- draw the same seed
twice and every text, latency, and cost is byte-identical (global
constraint: "Simulated mode is deterministic for a given `--seed`").

Also injects two kinds of failure (spec §3), both raised as
`SimulatedEndpointFailure` -- shaped so `app.gateway.gateway
.counts_as_endpoint_unhealth` reads them exactly as it would a real backend
failure:
- an **outage**: every call in an `OutageSpan` (a case-index range) fails,
  connection-refused shape (`status_code=None`);
- a **flake**: an independent per-call draw against `error_rate`, upstream
  4xx-refusal shape (`status_code=503`).
Outage is checked before the flake draw. A failed call still records its
messages in `received` -- the model was contacted before it failed.

Offline harness code only -- never imported by the serving path.
"""
from __future__ import annotations

import random

from app.config.schema import PriceCfg
from app.eval.cases import Case
from app.eval.endpoints import EndpointSpec, SimProfile
from app.gateway.backends.base import BackendHealth, count_tokens
from app.models import GenerationResult, Message
from app.routing.pricing import price_call

_WRONG_ANSWER = "SIMULATED-WRONG"


class SimulatedEndpointFailure(Exception):
    """An injected simulated-backend failure (spec §3).

    `status_code` follows the same shape a real backend exception carries --
    `counts_as_endpoint_unhealth` reads it via `getattr(exc, "status_code",
    None)` -- so an injected failure is indistinguishable, to every
    production consumer of that check, from a real one. `None` (an outage)
    reads as connection-refused; `503` (a flake) reads as an upstream
    refusal. Both count as endpoint unhealth.
    """

    def __init__(self, message: str, *, status_code: int | None,
                 failure_latency_ms: float) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.failure_latency_ms = failure_latency_ms


def _expected_answer(case: Case) -> str:
    """The text that, returned verbatim, scores 1.0 against `case.grader`.

    `exact`/`contains`/`numeric` graders compare against `expected`
    directly, so `str(expected)` always passes. `regex`/`json_schema`
    graders match a pattern or schema rather than a literal value, so
    simulation needs the case author's own known-passing answer --
    `grader.sample`, which `app/eval/cases.py::load_suite` requires and
    validates (via `compile_grader`) for exactly these two kinds before a
    suite can load at all.
    """
    grader = case.grader
    if grader.kind in ("exact", "contains", "numeric"):
        return str(grader.expected)
    assert grader.sample is not None, (
        f"case {case.id!r}: grader kind {grader.kind!r} has no sample "
        "(the loader should have rejected this suite)")
    return grader.sample


class SimulatedBackend:
    """A deterministic stand-in for the real LiteLLM backend (spec §5.1).

    Never sleeps, never calls out. The runner calls `set_case` with the case
    under sweep -- and that case's position in the suite (spec §3: outages
    are indexed by suite order, not wall time) -- before sweeping that
    case's eligible endpoints, so `generate` always knows which case it is
    answering for and whether that case index falls in an outage span.
    """

    name = "simulated"

    def __init__(self, specs: list[EndpointSpec], seed: int) -> None:
        self._profiles: dict[str, SimProfile] = {
            spec.slug: spec.simulated
            for spec in specs if spec.simulated is not None
        }
        self._seed = seed
        self._case: Case | None = None
        self._index: int | None = None
        # (case.id, model) -> the messages `generate` received for that call.
        # Not the harness's own persistence (the runner keeps `--keep-text`
        # discipline for completion text); this is here for tests -- e.g. the
        # redaction test -- to assert what the model-facing view looked like.
        self.received: dict[tuple[str, str], list[Message]] = {}

    def set_case(self, case: Case, index: int) -> None:
        """`index` is the case's 0-based position in the suite (the runner
        passes `enumerate` order) -- required, no default, so a caller can
        never silently sweep against the wrong outage window."""
        self._case = case
        self._index = index

    async def generate(self, model: str, messages: list[Message], price: PriceCfg,
                        tier: str, hint: dict,
                        tools: list[dict] | None = None,
                        tool_choice: str | dict | None = None,
                        params: dict | None = None) -> GenerationResult:
        if self._case is None:
            raise RuntimeError(
                "SimulatedBackend.set_case must be called before generate()")
        case = self._case
        profile = self._profiles.get(model)
        if profile is None:
            raise ValueError(
                f"simulated backend has no 'simulated' profile for endpoint {model!r}")

        self.received[(case.id, model)] = list(messages)

        assert self._index is not None  # set_case always sets both together
        failure_latency_ms = profile.failure_latency_ms or profile.latency_ms.mean
        if any(span.from_ <= self._index <= span.to for span in profile.outages):
            raise SimulatedEndpointFailure(
                f"simulated outage: {model!r} at case index {self._index}",
                status_code=None, failure_latency_ms=failure_latency_ms)
        err_draw = random.Random(f"{self._seed}:{case.id}:{model}:err").random()
        if err_draw < profile.error_rate:
            raise SimulatedEndpointFailure(
                f"simulated flake: {model!r} for case {case.id!r}",
                status_code=503, failure_latency_ms=failure_latency_ms)

        u = random.Random(f"{self._seed}:{case.id}:{model}").random()
        q = profile.quality.get(str(case.tags.get("workload")), profile.quality["default"])
        text = _expected_answer(case) if u < q else _WRONG_ANSWER

        u2 = random.Random(f"{self._seed}:{case.id}:{model}:lat").random()
        latency_ms = max(
            1.0, profile.latency_ms.mean + profile.latency_ms.jitter * (2 * u2 - 1))

        tokens_in = sum(count_tokens(m.content) for m in messages)
        tokens_out = count_tokens(text)
        cost_usd = price_call(price, prompt_tokens=tokens_in, completion_tokens=tokens_out)

        return GenerationResult(
            model=model,
            text=text,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            latency_ms=latency_ms,
            cost_usd=cost_usd,
            # Fixed at 1.0: simulation has no logprobs to derive a real
            # confidence from, and the gateway's confidence-based escalation
            # loop (step 7) must never fire on a simulated call.
            confidence=1.0,
        )

    def generate_stream(self, model: str, messages: list[Message], price: PriceCfg,
                         tier: str, hint: dict,
                         tools: list[dict] | None = None,
                         tool_choice: str | dict | None = None,
                         params: dict | None = None):
        raise NotImplementedError("simulated backend does not stream")

    async def health(self) -> BackendHealth:
        return BackendHealth(ok=True, detail="simulated", models=sorted(self._profiles))
