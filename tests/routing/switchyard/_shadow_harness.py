"""Shared harness for the B104/B105 shadow-mode tests.

Drives the real `/v1/chat/completions` stack (`create_app()`, a TestClient,
the litellm fakes) with `routing.switchyard.mode` set by replacing
`app.main.load_settings` -- so the routing policy version, and everything else
in the config, is byte-identical between the `off` run and the `shadow` run.
Spans are captured with an in-memory OTel provider behind the real
`RedactingSpanExporter`.
"""
from __future__ import annotations

import copy
from contextlib import contextmanager
from typing import Any, Callable

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

import app.main as app_main
import app.routing.switchyard as switchyard_pkg
from app.apps.models import RoutingOutcome, UsageRecord
from app.config.loader import load_settings
from app.gateway.backends import litellm_backend
from app.routing.switchyard import AdvisorResult, SwitchyardProposal
from tests._litellm_fake import CALLS, _FakeStreamChunk, make_response

DEMO = {"Authorization": "Bearer demo-key"}
SSN = "123-45-6789"


def settings_with(mode: str, *, semantic_cache: bool = False, timeout_ms: int | None = None):
    s = load_settings()
    # A generous default so a loaded parallel run can't turn a decision into a
    # `timeout` fallback; tests of the timeout path pass their own value.
    sy_update: dict[str, Any] = {"mode": mode,
                                 "timeout_ms": 2000 if timeout_ms is None else timeout_ms}
    sy = s.routing.switchyard.model_copy(update=sy_update)
    s = s.model_copy(update={"routing": s.routing.model_copy(update={"switchyard": sy})})
    if semantic_cache:
        sc = s.serving.semantic_cache.model_copy(update={"enabled": True})
        s = s.model_copy(update={"serving": s.serving.model_copy(
            update={"semantic_cache": sc})})
    return s


class SpyAdvisor:
    """Stands in for `SwitchyardAdvisor`: records every call's messages and
    answers with a fixed result (or raises)."""

    instances: list["SpyAdvisor"] = []

    def __init__(self, result: AdvisorResult | Callable[[], Any] | None = None,
                 raises: Exception | None = None) -> None:
        self.result = result
        self.raises = raises
        self.calls: list[list[dict]] = []
        SpyAdvisor.instances.append(self)

    async def advise(self, messages):
        self.calls.append([m.model_dump() for m in messages])
        if self.raises is not None:
            raise self.raises
        return self.result() if callable(self.result) else self.result


def proposal(tier: str = "cheap", role: str = "efficient",
             confidence: float | None = 0.8) -> AdvisorResult:
    p = SwitchyardProposal(role=role, tier=tier, confidence=confidence,
                           evidence={"source": "dimensions"}, latency_ms=0.3,
                           version="0.4.0a1", algorithm="stage_router")
    return AdvisorResult(latency_ms=0.3, proposal=p)


def fallback(cause: str) -> AdvisorResult:
    return AdvisorResult.fallback(cause, 0.2, version="0.4.0a1",
                                  algorithm="stage_router")


@contextmanager
def client(monkeypatch, mode: str, *, advisor: SpyAdvisor | None = None,
           semantic_cache: bool = False, timeout_ms: int | None = None,
           spans: bool = True):
    """A TestClient over an app booted with `routing.switchyard.mode=mode`.
    `advisor`, when given, replaces the real `SwitchyardAdvisor` (so the test
    needs no upstream package); otherwise the real one is built."""
    monkeypatch.setattr(app_main, "load_settings",
                        lambda: settings_with(mode, semantic_cache=semantic_cache,
                                              timeout_ms=timeout_ms))
    if advisor is not None:
        monkeypatch.setattr(switchyard_pkg, "SwitchyardAdvisor", lambda cfg: advisor)
        # The boot lint reads the install from metadata; pretend it is there.
        monkeypatch.setattr(switchyard_pkg, "installed_version", lambda: "0.4.0a1")
    app = app_main.create_app()
    mem = None
    if spans:
        mem = install_span_capture(monkeypatch)
    with TestClient(app, raise_server_exceptions=False) as c:
        c.mem = mem  # type: ignore[attr-defined]
        yield c


def install_span_capture(monkeypatch):
    pytest.importorskip("opentelemetry.sdk.trace")
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    import app.obs.otel as otel
    mem = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(otel.RedactingSpanExporter(mem)))
    monkeypatch.setattr(otel, "_provider", provider)
    return mem


def dispatching_fake(content: str = "ok", deltas: list[str] | None = None):
    """One litellm fake for both shapes: streams when asked to stream,
    answers whole otherwise."""
    deltas = deltas or ["Hello", ", ", "world"]

    async def _call(**kwargs):
        CALLS.append(kwargs)
        if kwargs.get("stream"):
            async def _gen():
                for d in deltas:
                    yield _FakeStreamChunk(content=d)
                yield _FakeStreamChunk(empty_choices=True,
                                       usage={"prompt_tokens": 12, "completion_tokens": 7})
            return _gen()
        return make_response(content=content)

    return _call


def install_fake(monkeypatch, **kw) -> None:
    monkeypatch.setattr(litellm_backend, "acompletion", dispatching_fake(**kw))


def root_spans(c) -> list:
    return [s for s in c.mem.get_finished_spans() if s.name == "serving.request"]


def switchyard_attrs(span) -> dict:
    return {k: v for k, v in span.attributes.items()
            if k.startswith("sp.switchyard.") or k == "langfuse.trace.tags"}


def outcomes(c) -> list[RoutingOutcome]:
    async def go():
        async with c.app.state.db.session() as session:
            return list((await session.execute(
                select(RoutingOutcome).order_by(RoutingOutcome.ts))).scalars())
    return c.portal.call(go)


def usage_rows(c) -> list[UsageRecord]:
    async def go():
        async with c.app.state.db.session() as session:
            return list((await session.execute(
                select(UsageRecord).order_by(UsageRecord.ts))).scalars())
    return c.portal.call(go)


def columns(row) -> dict:
    return {col.name: getattr(row, col.name) for col in row.__table__.columns}


# Fields that differ run-to-run whatever the mode: ids, clocks, timings.
_VOLATILE = {"id", "created", "trace_id", "trace_url", "request_id", "ts",
             "latency_ms", "check_latency_ms"}


def normalize(obj: Any, *, drop: frozenset[str] = frozenset()) -> Any:
    """Strip volatile keys (and `drop`) recursively, so two runs compare equal
    exactly when everything meaningful matches."""
    if isinstance(obj, dict):
        return {k: normalize(v, drop=drop) for k, v in obj.items()
                if k not in _VOLATILE and k not in drop}
    if isinstance(obj, list):
        return [normalize(v, drop=drop) for v in obj]
    return copy.deepcopy(obj)


SWITCHYARD_OUTCOME_COLUMNS = frozenset({
    "switchyard_mode", "switchyard_role", "switchyard_tier",
    "switchyard_confidence", "switchyard_agreed", "switchyard_fallback_cause",
})
