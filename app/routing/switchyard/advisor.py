"""The Switchyard advisor adapter (B103; design docs/architecture/switchyard-advisor-design.md).

`SwitchyardAdvisor.advise(messages)` asks Switchyard's classifier-less
`stage_router` one question: is this turn *efficient* or *capable* work? It
answers with a frozen, content-free `AdvisorResult`, either a
`SwitchyardProposal` (role, tier, confidence, allowlisted evidence) or a
fallback cause. Nothing is wired into the request path here: B105 (shadow) and
B108 (enforce) consume this.

Guarantees, each pinned by a test in tests/routing/switchyard/:

* **Role placeholders only (§3).** Switchyard is handed `ROLE_MODELS`: the
  categories `efficient` / `capable` / `any`, whose only members are the
  literal ids "efficient" and "capable". It never sees a model id, a provider
  or a price, so the only thing to validate is that it picked one of the two.
* **Built from scratch (§8).** The Switchyard request holds only the converted
  instructions and messages, and `run_stream` is called with `headers=None`.
  A client's session header (`x-switchyard-session-id`, ...) can therefore
  never reach Switchyard or engage its per-session holds.
* **No routing-time calls (§4, B111).** Every `CallModel` / `CallDecision`
  step is refused with `call.fail(...)`. A run that asked for one is a
  fallback (`routing_call_refused`), even if Switchyard then falls open.
* **Bounded and total (§5).** The whole run sits under
  `asyncio.wait_for(timeout_ms / 1000)`. `advise` never raises: every failure
  becomes a fallback with a cause. (Cancellation of the *calling* task still
  propagates, as it must.)
* **Lazy import.** `switchyard` is imported only when an advisor is
  constructed, so this module imports cleanly without the extra.

Upstream logging (nemo-switchyard 0.4.0a1). The Python package defines no
loggers of its own. The Rust core logs with the `tracing` crate, and the PyO3
binding (`crates/switchyard-py`) neither depends on `pyo3-log` nor installs a
`tracing` subscriber; only `switchyard_rust.Server` initialises observability,
and RouteForge never constructs it. So on the libsy path Rust log events reach
neither Python `logging` nor stderr: they are dropped. Verified by running an
`override` decision (which emits a `tracing::info!` "stage router override"
event) with stderr captured: zero bytes. Its metrics go to the Rust
`opentelemetry` global meter, which is a no-op unless that Server sets it, and
which is separate from any Python OTel provider. We still pin the
`switchyard` and `switchyard_rust` Python loggers at WARNING, so a future
release that bridges into `logging` stays quiet below WARN (B104).
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal, get_args

from app.models import Message

from . import installed_version
from .convert import _Unconvertible, to_switchyard_request
from .evidence import filter_evidence

if TYPE_CHECKING:
    from app.config.schema import SwitchyardCfg

Role = Literal["efficient", "capable"]
FallbackCause = Literal[
    "timeout", "error", "unconvertible", "invalid_role", "routing_call_refused"
]

ROLES: frozenset[str] = frozenset(get_args(Role))

# The per-request model pool handed to Switchyard: role placeholders, never
# real model ids (design §3).
ROLE_MODELS: dict[str, list[str]] = {
    "efficient": ["efficient"],
    "capable": ["capable"],
    "any": ["efficient", "capable"],
}

# Upstream Python loggers held at WARNING (see module docstring).
UPSTREAM_LOGGERS = ("switchyard", "switchyard_rust")

_REFUSAL = "routing-time model calls are disabled in RouteForge (B111)"


class SwitchyardUnavailable(RuntimeError):
    """The `nemo-switchyard` extra is not installed."""


@dataclass(frozen=True)
class SwitchyardProposal:
    """What the advisor proposes. Content-free: ids, enums and numbers only."""

    role: Role
    tier: str
    confidence: float | None
    evidence: dict[str, Any]
    latency_ms: float
    version: str
    algorithm: str


@dataclass(frozen=True)
class AdvisorResult:
    """Exactly one of `proposal` or `fallback_cause` is set."""

    latency_ms: float
    proposal: SwitchyardProposal | None = None
    fallback_cause: FallbackCause | None = None

    def __post_init__(self) -> None:
        if (self.proposal is None) == (self.fallback_cause is None):
            raise ValueError("AdvisorResult needs exactly one of proposal / fallback_cause")

    @classmethod
    def fallback(cls, cause: FallbackCause, latency_ms: float) -> AdvisorResult:
        return cls(latency_ms=latency_ms, fallback_cause=cause)

    @property
    def is_fallback(self) -> bool:
        return self.fallback_cause is not None

    def to_record(self) -> dict[str, Any]:
        """Flat record for traces and outcome rows: ids, enums and numbers only."""
        p = self.proposal
        return {
            "role": p.role if p else None,
            "tier": p.tier if p else None,
            "confidence": p.confidence if p else None,
            "evidence": dict(p.evidence) if p else {},
            "version": p.version if p else None,
            "algorithm": p.algorithm if p else None,
            "fallback_cause": self.fallback_cause,
            "latency_ms": self.latency_ms,
        }


def _import_libsy() -> Any:
    try:
        from switchyard import libsy
    except ImportError as exc:
        raise SwitchyardUnavailable(
            "nemo-switchyard is not installed; install the `switchyard` extra "
            "(uv sync --extra switchyard) or set routing.switchyard.mode to off"
        ) from exc
    return libsy


def _quiet_upstream_logging() -> None:
    for name in UPSTREAM_LOGGERS:
        logger = logging.getLogger(name)
        if logger.getEffectiveLevel() < logging.WARNING:
            logger.setLevel(logging.WARNING)


@dataclass
class _RunResult:
    outcome: Any = None
    refused_calls: int = 0


class SwitchyardAdvisor:
    """Classifier-less `stage_router`, built once and reused for every request."""

    def __init__(self, cfg: SwitchyardCfg) -> None:
        if cfg.algorithm != "stage_router" or cfg.classifier is not None:
            # The config lint already refuses these when mode != off (B100).
            raise ValueError("only a classifier-less stage_router is supported before B111")
        libsy = _import_libsy()
        _quiet_upstream_logging()
        self._step = libsy.Step
        self._algorithm = libsy.algorithms.stage_router(
            picker=cfg.picker,
            confidence_threshold=cfg.confidence_threshold,
        )
        self._algorithm_name = cfg.algorithm
        self._timeout_s = cfg.timeout_ms / 1000
        self._tier_for_role: dict[str, str] = {
            "efficient": "cheap",
            "capable": cfg.capable_tier,
        }
        self._version = installed_version() or "unknown"

    async def advise(self, messages: Sequence[Message]) -> AdvisorResult:
        """Propose a role for this turn, or say why not. Never raises."""
        started = time.perf_counter()

        def elapsed_ms() -> float:
            return (time.perf_counter() - started) * 1000

        try:
            request = to_switchyard_request(messages)
        except _Unconvertible:
            return AdvisorResult.fallback("unconvertible", elapsed_ms())
        except Exception:
            return AdvisorResult.fallback("error", elapsed_ms())

        try:
            run = await asyncio.wait_for(self._run(request), self._timeout_s)
        except TimeoutError:
            return AdvisorResult.fallback("timeout", elapsed_ms())
        except Exception:
            return AdvisorResult.fallback("error", elapsed_ms())

        try:
            return self._result_from(run, elapsed_ms())
        except Exception:
            return AdvisorResult.fallback("error", elapsed_ms())

    async def _run(self, request: dict[str, Any]) -> _RunResult:
        """Drive the step stream to its terminal `Done`, refusing every call."""
        result = _RunResult()
        stream = self._algorithm.run_stream(request, ROLE_MODELS, headers=None)
        async for step in stream:
            if isinstance(step, (self._step.CallModel, self._step.CallDecision)):
                result.refused_calls += 1
                await step.call.fail(RuntimeError(_REFUSAL))
            elif isinstance(step, self._step.Done):
                result.outcome = step.outcome
                break
        return result

    def _result_from(self, run: _RunResult, latency_ms: float) -> AdvisorResult:
        # Design §5: a run that asked for a routing-time call is a fallback,
        # whatever Switchyard decided after the refusal.
        if run.refused_calls:
            return AdvisorResult.fallback("routing_call_refused", latency_ms)
        if run.outcome is None:
            return AdvisorResult.fallback("error", latency_ms)

        selected = list(run.outcome.selected_model_ids or [])
        role = selected[0] if selected else None
        if role not in ROLES:
            return AdvisorResult.fallback("invalid_role", latency_ms)

        metadata = run.outcome.metadata
        evidence = filter_evidence(metadata.evidence if metadata is not None else None)
        # Upstream reports a confidence only for a signal decision; holds,
        # overrides and the picker default carry none.
        confidence = evidence.get("confidence")
        if confidence is not None and not 0.0 <= confidence <= 1.0:
            confidence = None

        proposal = SwitchyardProposal(
            role=role,
            tier=self._tier_for_role[role],
            confidence=confidence,
            evidence=evidence,
            latency_ms=latency_ms,
            version=self._version,
            algorithm=self._algorithm_name,
        )
        return AdvisorResult(latency_ms=latency_ms, proposal=proposal)
