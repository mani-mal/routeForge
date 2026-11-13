"""B103 contract tests: pin nemo-switchyard 0.4.0a1's behaviour through the adapter.

Needs the `switchyard` extra; skipped without it.
"""

import asyncio
import json

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

pytest.importorskip("switchyard")

from switchyard.libsy import LlmFallback, TaskClassifierConfig, algorithms  # noqa: E402

from app.config.schema import SwitchyardCfg  # noqa: E402
from app.models import Message  # noqa: E402
from app.routing.switchyard import ROLE_MODELS, SwitchyardAdvisor  # noqa: E402


def _advise(advisor, messages):
    return asyncio.run(advisor.advise(messages))


def _advisor(**cfg) -> SwitchyardAdvisor:
    return SwitchyardAdvisor(SwitchyardCfg(mode="shadow", **cfg))


def _tool_turn(i: int, name: str, args: dict, result: str) -> list[Message]:
    call = {"id": f"call_{i}", "type": "function",
            "function": {"name": name, "arguments": json.dumps(args)}}
    return [
        Message(role="assistant", content=None, tool_calls=[call]),
        Message(role="tool", content=result, tool_call_id=f"call_{i}", name=name),
    ]


def clean_production() -> list[Message]:
    msgs = [Message(role="system", content="You are a coding agent."),
            Message(role="user", content="Add the helper module.")]
    for i in range(4):
        msgs += _tool_turn(i, "Write", {"file_path": f"src/m{i}.py", "content": "x = 1"},
                           f"File created successfully at: src/m{i}.py")
    return msgs


def failing_tests() -> list[Message]:
    msgs = [Message(role="user", content="Make the tests pass.")]
    out = ("Traceback (most recent call last):\n  File \"tests/test_a.py\", line 3\n"
           "AssertionError: expected 2\nFAILED tests/test_a.py::test_x - AssertionError\n1 failed")
    for i in range(4):
        msgs += _tool_turn(i, "Bash", {"command": "pytest -q"}, out)
    return msgs


def no_tools() -> list[Message]:
    return [Message(role="user", content="What is a monad?")]


# --- contract: the three pinned behaviours -----------------------------------

def test_clean_production_history_proposes_efficient():
    r = _advise(_advisor(), clean_production())
    assert r.fallback_cause is None
    p = r.proposal
    assert (p.role, p.tier) == ("efficient", "cheap")
    assert p.evidence["source"] == "dimensions"
    assert p.confidence == pytest.approx(0.874, abs=0.01)  # upstream's documented ceiling
    assert p.evidence["threshold"] == 0.5
    assert p.algorithm == "stage_router" and p.version == "0.4.0a1"


def test_repeated_failing_tool_results_propose_capable():
    p = _advise(_advisor(), failing_tests()).proposal
    assert (p.role, p.tier) == ("capable", "frontier")
    assert p.evidence == {"source": "override"}
    assert p.confidence is None  # upstream reports none for an override


def test_capable_tier_can_be_capped_at_mid():
    p = _advise(_advisor(capable_tier="mid"), failing_tests()).proposal
    assert (p.role, p.tier) == ("capable", "mid")


@pytest.mark.parametrize("picker, role", [("efficient_first", "efficient"), ("capable_first", "capable")])
def test_no_tool_history_takes_the_picker_default(picker, role):
    p = _advise(_advisor(picker=picker), no_tools()).proposal
    assert p.role == role
    assert p.evidence == {"source": "fall_open"}


def test_algorithm_is_built_once_and_reused():
    advisor = _advisor()
    handle = advisor._algorithm
    _advise(advisor, no_tools())
    _advise(advisor, failing_tests())
    assert advisor._algorithm is handle


# --- property: role always valid, advise never raises -------------------------

_text = st.one_of(st.none(), st.text(max_size=40))
_junk_call = st.one_of(
    st.fixed_dictionaries({"id": st.text(max_size=5), "type": st.just("function"),
                           "function": st.fixed_dictionaries({"name": st.text(max_size=8),
                                                              "arguments": _text})}),
    st.dictionaries(st.text(max_size=5), st.text(max_size=5), max_size=3),
    st.text(max_size=5),
)
# model_construct skips validation, so junk the API would reject (a string
# tool call, a non-list) still reaches the adapter.
_message = st.builds(
    Message.model_construct,
    role=st.one_of(st.sampled_from(["system", "developer", "user", "assistant", "tool"]),
                   st.text(max_size=10)),
    content=st.one_of(_text, st.integers()),
    tool_calls=st.one_of(st.none(), st.lists(_junk_call, max_size=3), st.text(max_size=3)),
    tool_call_id=st.one_of(st.none(), st.text(max_size=6)),
    name=st.one_of(st.none(), st.text(max_size=6)),
)

_PROPERTY_ADVISOR = None


def _shared_advisor():
    global _PROPERTY_ADVISOR
    if _PROPERTY_ADVISOR is None:
        _PROPERTY_ADVISOR = _advisor(timeout_ms=1000)
    return _PROPERTY_ADVISOR


@settings(max_examples=150, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(st.lists(_message, max_size=8))
def test_advise_never_raises_and_role_is_always_valid(messages):
    r = _advise(_shared_advisor(), messages)
    if r.proposal is not None:
        assert r.proposal.role in {"efficient", "capable"}
        assert r.proposal.tier in {"cheap", "frontier"}
    else:
        assert r.fallback_cause in {"timeout", "error", "unconvertible",
                                    "invalid_role", "routing_call_refused"}
    json.dumps(r.to_record())  # always serialisable


@pytest.mark.parametrize("junk", [None, 42, "text", [None], [{"role": "user"}]])
def test_advise_never_raises_on_non_message_input(junk):
    r = _advise(_advisor(), junk)
    assert r.fallback_cause in {"unconvertible", "error"}


def test_unconvertible_history_is_a_fallback():
    r = _advise(_advisor(), [Message(role="tool", content="orphan result")])
    assert r.fallback_cause == "unconvertible"


# --- spies over the real algorithm handle ----------------------------------------

class _Spy:
    """Wraps the real algorithm handle and records each run_stream call."""

    def __init__(self, inner, models=None):
        self.inner, self.models, self.calls = inner, models, []

    def run_stream(self, request, models, subagent_models=None, headers=None):
        self.calls.append({"request": request, "models": models, "headers": headers})
        return self.inner.run_stream(request, self.models or models, headers=headers)


def test_switchyard_gets_role_placeholders_and_no_headers():
    advisor = _advisor()
    spy = _Spy(advisor._algorithm)
    advisor._algorithm = spy
    _advise(advisor, clean_production())
    (call,) = spy.calls
    assert call["headers"] is None
    assert call["models"] == ROLE_MODELS
    assert set(call["request"]) == {"instructions", "messages"}


def test_a_session_header_would_engage_holds_but_advise_cannot_send_one():
    """Why headers=None matters: with a session id, an override sets a capable
    hold that sticks to the *next* request. advise() takes no headers, so the
    hold never engages and each request is advised on its own history."""
    session = {"x-switchyard-session-id": "client-chosen"}

    async def raw(alg, messages, headers):
        from app.routing.switchyard.convert import to_switchyard_request
        async for step in alg.run_stream(to_switchyard_request(messages), ROLE_MODELS, headers=headers):
            if hasattr(step, "outcome"):
                return step.outcome.selected_model_ids[0], step.outcome.metadata.evidence["source"]

    direct = algorithms.stage_router(picker="efficient_first", confidence_threshold=0.5)
    asyncio.run(raw(direct, failing_tests(), session))
    assert asyncio.run(raw(direct, no_tools(), session)) == ("capable", "capable_hold")

    advisor = _advisor()
    _advise(advisor, failing_tests())
    p = _advise(advisor, no_tools()).proposal
    assert (p.role, p.evidence["source"]) == ("efficient", "fall_open")


# --- timeout ------------------------------------------------------------------------

class _SlowAlgorithm:
    def run_stream(self, request, models, subagent_models=None, headers=None):
        async def steps():
            await asyncio.sleep(1)
            yield None
        return steps()


def test_timeout_is_a_fallback():
    advisor = _advisor(timeout_ms=5)
    advisor._algorithm = _SlowAlgorithm()
    r = _advise(advisor, no_tools())
    assert r.fallback_cause == "timeout"
    assert r.latency_ms < 500


class _RaisingAlgorithm:
    def run_stream(self, request, models, subagent_models=None, headers=None):
        raise RuntimeError("boom")


def test_upstream_exception_is_a_fallback():
    advisor = _advisor()
    advisor._algorithm = _RaisingAlgorithm()
    assert _advise(advisor, no_tools()).fallback_cause == "error"


class _FakeOutcome:
    def __init__(self, ids):
        self.selected_model_ids = ids
        self.metadata = None


class _FakeDone:
    def __init__(self, ids):
        self.outcome = _FakeOutcome(ids)


class _FakeSteps:
    """Stands in for `Step`: only Done can occur."""
    Done = _FakeDone
    CallModel = CallDecision = type("_NeverAStep", (), {})


@pytest.mark.parametrize("ids", [["gpt-4o"], [], ["any"]])
def test_selected_id_outside_the_roles_is_invalid_role(ids):
    """The real stage_router only ever returns a placeholder; forge one that doesn't."""
    class _Alg:
        def run_stream(self, request, models, subagent_models=None, headers=None):
            async def steps():
                yield _FakeDone(ids)
            return steps()

    advisor = _advisor()
    advisor._step = _FakeSteps
    advisor._algorithm = _Alg()
    assert _advise(advisor, no_tools()).fallback_cause == "invalid_role"


# --- refusal of routing-time calls ------------------------------------------------

class _StepRecorder(_Spy):
    """A spy that also records each step the real stream yields."""

    def __init__(self, inner, models=None):
        super().__init__(inner, models)
        self.steps: list = []

    def run_stream(self, request, models, subagent_models=None, headers=None):
        stream = super().run_stream(request, models, headers=headers)

        async def steps():
            async for step in stream:
                self.steps.append(step)
                yield step
        return steps()


def test_routing_time_model_call_is_refused_and_falls_back():
    """A stage_router with an LLM classifier asks for a judge call on an
    ambiguous turn. It is built here directly, never via config (the lint and
    the constructor refuse a classifier until B111). Upstream only emits the
    call when a `judge` category exists, so the spy adds one.

    Proof that fail() reached upstream: the run still terminates (an unanswered
    call would hang until the timeout), and upstream's own outcome records the
    refusal as a client error and falls open."""
    advisor = _advisor(timeout_ms=2000)
    recorder = _StepRecorder(
        algorithms.stage_router(
            picker="efficient_first",
            confidence_threshold=0.5,
            classifier=LlmFallback(config=TaskClassifierConfig(0.5)),
        ),
        models={**ROLE_MODELS, "judge": ["judge"]},
    )
    advisor._algorithm = recorder

    r = _advise(advisor, no_tools())

    kinds = [type(s).__name__ for s in recorder.steps]
    assert kinds == ["CallModel", "Done"]
    assert recorder.steps[0].call.models == ["judge"]
    assert recorder.steps[1].outcome.metadata.evidence == {
        "source": "fail_open", "reason_code": "client_error"}
    # Design §5: a requested routing-time call is a fallback, even though
    # upstream then fell open to a valid Done.
    assert r.fallback_cause == "routing_call_refused"
    assert r.proposal is None
