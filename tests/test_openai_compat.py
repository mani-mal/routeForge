"""OpenAI wire-compatibility contract for POST /v1/chat/completions.

These are the regression tests that were missing: nothing asserted the
response envelope, so a response with no `object`/`created` shipped and every
OpenAI-client turn (any client app, LangChain ChatOpenAI, the `openai` SDK) died
before it reached a tool. Likewise nothing exercised a tool-calling round-trip,
so the direct path silently dropped `tools` at the gateway and flattened the
conversation.

The strongest guard here is `_validate_openai`: it parses our response with the
REAL `openai` SDK model, exactly as a client does — so any future drift from
the OpenAI shape fails a test instead of a customer's agent.
"""
import json

import pytest

from tests._litellm_fake import (
    CALLS, make_recording_acompletion, make_response,
)
from tests.conftest import v1_error
from app.gateway.backends import litellm_backend

_AUTH = {"Authorization": "Bearer demo-key"}

_TOOLS = [{
    "type": "function",
    "function": {
        "name": "get_weather",
        "description": "Get the weather for a city",
        "parameters": {
            "type": "object",
            "properties": {"city": {"type": "string"}},
            "required": ["city"],
        },
    },
}]


def _validate_openai(body: dict) -> None:
    """Parse `body` with the actual openai SDK ChatCompletion model — the same
    validation a real OpenAI client runs. Skips cleanly if openai isn't installed."""
    ChatCompletion = pytest.importorskip("openai.types.chat").ChatCompletion
    ChatCompletion.model_validate(body)  # raises on any shape violation


def test_response_has_openai_envelope(client):
    r = client.post("/v1/chat/completions", headers=_AUTH, json={
        "messages": [{"role": "user", "content": "capital of France?"}]})
    assert r.status_code == 200
    body = r.json()
    # The exact two fields that were missing and broke every turn.
    assert body["object"] == "chat.completion"
    assert isinstance(body["created"], int) and body["created"] > 0
    # Core OpenAI fields still present and well-formed.
    assert body["id"]
    assert body["model"]
    choice = body["choices"][0]
    assert choice["message"]["role"] == "assistant"
    assert choice["finish_reason"] == "stop"
    assert {"prompt_tokens", "completion_tokens", "total_tokens"} <= body["usage"].keys()
    # And it parses with the real client model — the definitive regression.
    _validate_openai(body)


def test_tools_are_threaded_and_tool_calls_returned(client, monkeypatch):
    """A request with `tools` must (a) actually reach the backend and (b) come
    back as an OpenAI tool_calls response (content null, finish_reason
    'tool_calls'). Before the fix, gateway.run dropped `tools` entirely."""
    CALLS.clear()
    tool_call = {"id": "call-1", "type": "function",
                 "function": {"name": "get_weather", "arguments": '{"city": "Paris"}'}}
    monkeypatch.setattr(
        litellm_backend, "acompletion",
        make_recording_acompletion(make_response(content=None, tool_calls=[tool_call])))

    r = client.post("/v1/chat/completions", headers=_AUTH, json={
        "messages": [{"role": "user", "content": "weather in Paris?"}],
        "tools": _TOOLS,
        "tool_choice": "auto",
    })
    assert r.status_code == 200
    body = r.json()
    choice = body["choices"][0]
    assert choice["finish_reason"] == "tool_calls"
    assert choice["message"]["content"] is None
    assert choice["message"]["tool_calls"][0]["function"]["name"] == "get_weather"
    _validate_openai(body)

    # The backend actually received the tools and tool_choice (not dropped).
    assert CALLS, "backend was never called"
    last = CALLS[-1]
    assert last["tools"] == _TOOLS
    assert last["tool_choice"] == "auto"


def test_tool_result_roundtrip_parses_and_forwards_structure(client, monkeypatch):
    """The SECOND turn of a tool loop: the client replays an assistant message
    with content:null + tool_calls, plus a role:'tool' result. This must NOT
    422 on request parsing, and the structured messages (roles + tool_call_id)
    must reach the backend rather than being flattened into one blob."""
    CALLS.clear()
    monkeypatch.setattr(
        litellm_backend, "acompletion",
        make_recording_acompletion(make_response(content="It is sunny in Paris.")))

    r = client.post("/v1/chat/completions", headers=_AUTH, json={
        "messages": [
            {"role": "user", "content": "weather in Paris?"},
            {"role": "assistant", "content": None,
             "tool_calls": [{"id": "call-1", "type": "function",
                             "function": {"name": "get_weather",
                                          "arguments": '{"city": "Paris"}'}}]},
            {"role": "tool", "tool_call_id": "call-1", "content": "sunny, 21C"},
        ],
        "tools": _TOOLS,
    })
    assert r.status_code == 200
    body = r.json()
    assert body["choices"][0]["message"]["content"] == "It is sunny in Paris."
    _validate_openai(body)

    # Structure was preserved end-to-end: the tool result reached the backend
    # as its own role:'tool' message correlated by tool_call_id.
    wire = CALLS[-1]["messages"]
    roles = [m["role"] for m in wire]
    assert roles == ["user", "assistant", "tool"], roles
    tool_msg = wire[-1]
    assert tool_msg["tool_call_id"] == "call-1"
    assert tool_msg["content"] == "sunny, 21C"
    # The assistant turn kept its tool_calls.
    assert wire[1]["tool_calls"][0]["id"] == "call-1"


def test_plain_multiturn_without_tools_still_serves(client):
    """Regression guard for the 100+ non-tool requests: a plain multi-message
    request (no tools) still serves and still carries the OpenAI envelope."""
    r = client.post("/v1/chat/completions", headers=_AUTH, json={
        "messages": [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "hello"},
            {"role": "user", "content": "capital of France?"},
        ]})
    assert r.status_code == 200
    body = r.json()
    assert body["object"] == "chat.completion"
    assert isinstance(body["created"], int) and body["created"] > 0
    _validate_openai(body)


def test_tools_request_is_redacted_before_egress(client, monkeypatch):
    """CLAUDE.md rule 1 on the tool path: each message is per-message redacted
    before it reaches the backend — a PII value in a tool result must not be
    forwarded raw."""
    CALLS.clear()
    monkeypatch.setattr(
        litellm_backend, "acompletion",
        make_recording_acompletion(make_response(content="ok")))

    r = client.post("/v1/chat/completions", headers=_AUTH, json={
        "messages": [
            {"role": "user", "content": "look up this account"},
            {"role": "tool", "tool_call_id": "c1",
             "content": "customer SSN 482-73-1946 on file"},
        ],
        "tools": _TOOLS,
    })
    assert r.status_code == 200
    wire_text = "".join(str(m.get("content") or "") for m in CALLS[-1]["messages"])
    assert "482-73-1946" not in wire_text  # redacted before egress


# --- B49: the OpenAI SDK contract on the error envelope ---
#
# These are the tests that would have caught B49: nothing above drives a
# genuine OpenAI SDK client, so nothing above would have noticed that a
# failure rendered as FastAPI's bare `{"detail": ...}` instead of the shape
# the SDK parses. Qwen Code -- a real OpenAI-SDK client -- showed the user
# only "API error" for exactly this reason.


def _sdk(client, token: str):
    """A real OpenAI SDK client speaking to the in-process app.

    `TestClient` is an `httpx.Client` subclass and `openai.OpenAI` accepts an
    `http_client`, so this is the genuine SDK parse path -- the same code that
    turned RouteForge's 400 into a bare "API error" for Qwen Code (B49).
    """
    openai = pytest.importorskip("openai")
    oc = openai.OpenAI(base_url="http://testserver/v1", api_key=token,
                       http_client=client, max_retries=0)
    oc._client = client  # route SDK traffic through the TestClient transport
    return oc


def test_sdk_reads_context_overflow_code(client, monkeypatch) -> None:
    """The complaint itself (B49): the OpenAI SDK must surface `.code` and
    `.type` off a 400, not a bare "API error" (Qwen Code)."""
    openai = pytest.importorskip("openai")
    from litellm import ContextWindowExceededError

    async def _overflow(**kwargs):
        raise ContextWindowExceededError(
            message=("OpenAIException - This model's maximum context length is "
                     "8192 tokens. Please shorten your messages."),
            model=kwargs.get("model", "m"), llm_provider="openai")

    monkeypatch.setattr(litellm_backend, "acompletion", _overflow)

    with pytest.raises(openai.BadRequestError) as ei:
        _sdk(client, "demo-key").chat.completions.create(
            model="auto", messages=[{"role": "user", "content": "hi"}])
    assert ei.value.code == "context_length_exceeded"
    assert ei.value.type == "invalid_request_error"
    # `.message` is the SDK's own "Error code: 400 - {...}" wrapper; the
    # RouteForge text lives on the parsed body.
    assert "shorten the conversation" in ei.value.body["message"]


def test_sdk_reads_auth_failure_code(client) -> None:
    """A bad token must read as `openai.AuthenticationError` with
    `code == "invalid_api_key"`, not a bare 401."""
    openai = pytest.importorskip("openai")
    with pytest.raises(openai.AuthenticationError) as ei:
        _sdk(client, "rf_live_nope").chat.completions.create(
            model="any", messages=[{"role": "user", "content": "hi"}])
    assert ei.value.code == "invalid_api_key"


def test_sdk_reads_rate_limit_code() -> None:
    """max_rpm exceeded -> `openai.RateLimitError` with `code ==
    "rate_limit_exceeded"` and `type == "rate_limit_error"`. Setup mirrors
    tests/onboarding/test_rejection_points.py::test_rate_limit_is_429_after_max_rpm."""
    openai = pytest.importorskip("openai")
    from tests.onboarding.harness import conformance_client, onboard

    with conformance_client() as c:
        app = onboard(c, "sdk-rate-limit", max_rpm=1)
        token = app.headers["Authorization"].removeprefix("Bearer ")
        oc = _sdk(c, token)
        oc.chat.completions.create(model="auto", messages=[{"role": "user", "content": "hi"}])
        with pytest.raises(openai.RateLimitError) as ei:
            oc.chat.completions.create(model="auto", messages=[{"role": "user", "content": "hi"}])
    assert ei.value.code == "rate_limit_exceeded"
    assert ei.value.type == "rate_limit_error"


def test_sdk_reads_daily_budget_code() -> None:
    """Daily budget exhausted is also a 429, but a DIFFERENT `type` --
    "insufficient_quota", not "rate_limit_error" -- the one pair in the whole
    mapping that shares a status and deliberately differs on `type`, so a
    single 429 test would let the two collapse into each other unnoticed.
    Setup mirrors tests/onboarding/test_rejection_points.py::
    test_daily_budget_exhausted_is_429_and_named."""
    openai = pytest.importorskip("openai")
    from app.apps import store
    from tests.onboarding.harness import conformance_client, onboard

    with conformance_client() as c:
        app = onboard(c, "sdk-daily-budget", max_spend_usd_per_day=0.5)

        async def spend():
            async with c.app.state.db.session() as session:
                await store.record_usage(
                    session, application_id="sdk-daily-budget", request_id="seed",
                    model="local-8b", provider="onprem", tier="cheap", prompt_tokens=1,
                    completion_tokens=1, total_tokens=2, cost_usd=1.0, baseline_usd=1.0,
                    latency_ms=1.0, sensitivity="public", onprem_only=False, status="ok")
                await session.commit()

        c.portal.call(spend)
        token = app.headers["Authorization"].removeprefix("Bearer ")
        with pytest.raises(openai.RateLimitError) as ei:
            _sdk(c, token).chat.completions.create(
                model="auto", messages=[{"role": "user", "content": "hi"}])
    assert ei.value.code == "insufficient_quota"
    assert ei.value.type == "insufficient_quota"


def test_streamed_and_plain_errors_agree(client, monkeypatch) -> None:
    """A failure describes itself identically whether or not the response had
    already started (B49 design §3.4)."""
    from app.errors import NoEligibleCandidate
    from tests.onboarding.harness import conformance_client, onboard
    from tests.test_stream_keepalive import _fast_keepalive, _slow_run

    with conformance_client() as c:
        plain_app = onboard(c, "parity-plain")
        stream_app = onboard(c, "parity-stream", stream_mode="verified_then_stream")

        _slow_run(monkeypatch, c, delay=0.0,
                  exc=NoEligibleCandidate("no candidates after residency filter: test"))
        plain = plain_app.chat({"messages": [{"role": "user", "content": "hi"}]})
        assert plain.status_code == 422
        plain_err = v1_error(plain)

        _fast_keepalive(c)
        _slow_run(monkeypatch, c, delay=0.2,
                  exc=NoEligibleCandidate("no candidates after residency filter: test"))
        with c.stream("POST", "/v1/chat/completions", headers=stream_app.headers,
                      json={"stream": True,
                            "messages": [{"role": "user", "content": "hi"}]}) as r:
            assert r.status_code == 200
            body = "".join(r.iter_text())

    data = [e for e in body.split("\n\n") if e.startswith("data: ")]
    assert data[-1] == "data: [DONE]"
    stream_err = json.loads(data[-2][6:])["error"]

    assert plain_err["type"] == stream_err["type"] == "invalid_request_error"
    assert plain_err["code"] == stream_err["code"] == "no_eligible_model"


def test_backend_error_message_still_scrubs_endpoints(client, monkeypatch) -> None:
    """The envelope adds no text and reads no new source, so it cannot widen a
    leak -- pinned, because it renders a message that used to be opaque."""
    async def refuse(**kwargs):
        raise RuntimeError("Cannot connect to http://10.1.2.3:8000/v1")

    monkeypatch.setattr(litellm_backend, "acompletion", refuse)
    r = client.post("/v1/chat/completions", headers=_AUTH,
                    json={"messages": [{"role": "user", "content": "hello"}]})
    assert r.status_code == 502
    message = v1_error(r)["message"]
    assert "<endpoint>" in message
    assert "10.1.2.3" not in message
    assert "http://" not in message


def test_plain_http_exception_on_v1_still_renders_an_envelope(client) -> None:
    """A raise that names no code gets a status-derived type and a null code.

    Uses GET on the POST-only /v1/chat/completions route (405), which is
    raised by Starlette before any RouteForge code runs -- nothing on this
    path could have remembered to opt in to the mapping."""
    r = client.get("/v1/chat/completions")
    assert r.status_code == 405
    err = v1_error(r)
    assert set(err) == {"message", "type", "param", "code"}
    assert err["code"] is None
    assert err["type"] == "invalid_request_error"


def test_request_validation_error_renders_openai_envelope_and_keeps_422(client) -> None:
    """The `RequestValidationError` half of B49: a malformed body must still
    render the OpenAI envelope shape, and the status must stay FastAPI's
    422 -- spec §3.2 is explicit that this is *not* changed to OpenAI's 400,
    which would be a behaviour change beyond rendering."""
    r = client.post("/v1/chat/completions", headers=_AUTH,
                    json={"messages": "not-a-list"})
    assert r.status_code == 422
    err = v1_error(r)
    assert set(err) == {"message", "type", "param", "code"}
    assert err["code"] == "invalid_request"
    assert err["type"] == "invalid_request_error"
    assert err["param"] is None
    assert "messages" in err["message"]


def test_request_validation_error_does_not_echo_the_rejected_value(client) -> None:
    """FastAPI's default handler echoes the rejected input (`exc.errors()`
    includes `input`); ours emits only `loc` + `msg`. That is a real privacy
    improvement -- an invalid field can carry sensitive text -- and nothing
    currently stops a future edit from regressing it back to leaking the
    value, so pin it with a distinctive marker."""
    r = client.post("/v1/chat/completions", headers=_AUTH,
                    json={"messages": [{"role": "user", "content": "hi"}],
                          "max_tokens": "supersecretvalue"})
    assert r.status_code == 422
    assert "supersecretvalue" not in r.text


# --- B60: recording the routing token count adds nothing to the wire ----------

_SUPPORT_TICKET = (
    "Ticket 48213 (priority: high). Customer reports that order ORD-2026-0917 "
    "shipped to the wrong depot on 2026-09-30; the carrier scan shows it at "
    "the Leeds hub. Summarise the ticket in two sentences and propose the "
    "next action for the support agent. ") * 40      # ~2500 routing tokens


def test_outcome_recording_adds_nothing_to_the_success_wire(client, monkeypatch):
    """Serving-path change (B60: `app/pipeline.py` now publishes
    `routing_tokens` for the outcome row). The consumer contract: the
    response is still a valid OpenAI `ChatCompletion`, the new field appears
    nowhere in it, and the model was sent exactly the client's message."""
    monkeypatch.setattr(litellm_backend, "acompletion",
                        make_recording_acompletion(make_response(content="Two sentences.")))
    r = client.post("/v1/chat/completions", headers=_AUTH, json={
        "messages": [{"role": "user", "content": _SUPPORT_TICKET}], "max_tokens": 64})
    assert r.status_code == 200, r.text
    body = r.json()
    _validate_openai(body)
    assert body["choices"][0]["message"]["content"] == "Two sentences."
    assert "routing_tokens" not in r.text
    wire = "".join(str(m.get("content") or "") for m in CALLS[-1]["messages"])
    assert wire == _SUPPORT_TICKET                   # nothing redacted, nothing added


def test_outcome_recording_adds_nothing_to_the_rejection_wire() -> None:
    """The other direction: a request rejected for its size (per-request
    budget) still returns the B49 envelope with exactly its existing keys --
    the count that the outcome row now records is not echoed to the client."""
    from tests.onboarding.harness import conformance_client, onboard

    with conformance_client() as c:
        app = onboard(c, "wire-budget", max_cost_usd_per_request=0.00001)
        r = app.chat({"messages": [{"role": "user", "content": _SUPPORT_TICKET}],
                      "max_tokens": 64})
    assert r.status_code == 422, r.text
    assert set(r.json()) == {"error"}
    err = v1_error(r)
    assert set(err) == {"message", "type", "param", "code", "conflicting_core"}
    assert err["type"] == "invalid_request_error" and err["param"] is None
    assert err["code"] == "no_eligible_model"
    assert set(err["conflicting_core"]) == {"requirements", "state", "candidates_considered"}
    assert "routing_tokens" not in r.text
