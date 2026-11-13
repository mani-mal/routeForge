"""B105 identity: `shadow` (and, until B108, `enforce`) changes nothing a
client, the meter or the router sees -- only the trace, the outcome row and
the served DecisionRecord's `advisor` field differ from `off`.

The same fixture traffic is driven through the real `/v1/chat/completions`
stack twice -- once with `routing.switchyard.mode: off`, once with the mode
under test and the REAL Switchyard advisor -- and compared on: what reached
the backend (the litellm call kwargs), the served model, the response bodies
(non-stream JSON and stream SSE frames), the usage rows (metering and cost)
and the outcome rows. Includes an `onprem_only` / high-risk request (SSN), a
medium-risk one, a long-context one, a client tool turn, a live stream and a
stream that falls back to `verified_then_stream`.
"""
from __future__ import annotations

import json

import pytest

from tests import _litellm_fake
from tests.routing.switchyard._shadow_harness import (
    DEMO, SSN, SWITCHYARD_OUTCOME_COLUMNS, client, columns, install_fake, normalize,
    outcomes, root_spans, switchyard_attrs, usage_rows,
)
from tests.test_admin_api import _login

pytest.importorskip("switchyard")

_TOOLS = [{"type": "function", "function": {
    "name": "lookup_order", "description": "Look up an order",
    "parameters": {"type": "object", "properties": {"id": {"type": "string"}},
                   "required": ["id"]}}}]

NON_STREAM = [
    {"messages": [{"role": "user", "content": "What is the capital of France?"}]},
    # US_SSN -> restricted, onprem_only, risk_class high, require_verification.
    {"messages": [{"role": "user",
                   "content": f"Patient SSN {SSN}: summarise the discharge plan."}]},
    # EMAIL -> confidential, medium risk.
    {"messages": [{"role": "user",
                   "content": "Email jane@example.com about the Q3 report."}]},
    # Long context: a higher length tier.
    {"messages": [{"role": "user", "content": "Summarise this log:\n" + "line ok\n" * 1500}]},
    # A client-driven tool turn.
    {"messages": [
        {"role": "user", "content": "Where is order 42?"},
        {"role": "assistant", "content": None, "tool_calls": [{
            "id": "call-1", "type": "function",
            "function": {"name": "lookup_order", "arguments": '{"id": "42"}'}}]},
        {"role": "tool", "tool_call_id": "call-1", "content": '{"status": "shipped"}'}],
     "tools": _TOOLS},
]

STREAM = [
    {"messages": [{"role": "user", "content": "hi there"}], "stream": True},
    # Floored: onprem_only + require_verification -> verified_then_stream.
    {"messages": [{"role": "user", "content": f"my SSN is {SSN}"}], "stream": True},
]

# The only places the advisor may show up.
_ADVISOR_KEYS = frozenset({"advisor"})


def _sse(text: str) -> list:
    frames = []
    for block in text.strip("\n").split("\n\n"):
        payload = block.strip()[len("data: "):]
        frames.append("[DONE]" if payload == "[DONE]" else json.loads(payload))
    return frames


def _drive(monkeypatch, mode: str) -> dict:
    install_fake(monkeypatch)
    _litellm_fake.CALLS.clear()
    with client(monkeypatch, mode) as c:
        _login(c)
        r = c.post("/admin/applications", json={"id": "sy-stream", "name": "sy-stream"})
        assert r.status_code == 200, r.text
        stream_token = r.json()["token"]
        assert c.put("/admin/applications/sy-stream/policy",
                     json={"stream_mode": "stream_unverified"}).status_code == 200

        bodies, served = [], []
        for req in NON_STREAM:
            r = c.post("/v1/chat/completions", headers=DEMO, json=req)
            assert r.status_code == 200, r.text
            bodies.append(r.json())
            served.append(r.json()["model"])
        frames = []
        for req in STREAM:
            r = c.post("/v1/chat/completions",
                       headers={"Authorization": f"Bearer {stream_token}"}, json=req)
            assert r.status_code == 200, r.text
            frames.append(_sse(r.text))
        snapshots = [c.get(f"/api/requests/{f[0]['id']}").json() for f in frames]
        return {
            "calls": [dict(k) for k in _litellm_fake.CALLS],
            "bodies": bodies, "served": served, "frames": frames,
            "snapshots": snapshots,
            "usage": [columns(u) for u in usage_rows(c)],
            "outcomes": [columns(o) for o in outcomes(c)],
            "spans": [switchyard_attrs(s) for s in root_spans(c)],
        }


@pytest.mark.parametrize("mode", ["shadow", "enforce"])
def test_shadow_is_identical_to_off(monkeypatch, mode):
    off = _drive(monkeypatch, "off")
    on = _drive(monkeypatch, mode)

    # Sanity: every request reached the backend, was metered and recorded,
    # and the fixture really exercises onprem_only + high risk.
    n = len(NON_STREAM) + len(STREAM)
    assert len(off["calls"]) == len(off["usage"]) == len(off["outcomes"]) == n
    ssn_body = off["bodies"][1]
    assert ssn_body["policy"]["onprem_only"] is True
    assert ssn_body["policy"]["sensitivity"] == "restricted"

    # 1. What reached the model is identical, call for call.
    assert on["calls"] == off["calls"]
    # 2. Same served model on every request.
    assert on["served"] == off["served"]
    # 3. Response bodies identical, apart from the served hop's `advisor`.
    assert normalize(on["bodies"], drop=_ADVISOR_KEYS) == normalize(off["bodies"])
    assert normalize(on["frames"]) == normalize(off["frames"])
    assert (normalize(on["snapshots"], drop=_ADVISOR_KEYS)
            == normalize(off["snapshots"]))
    # 4. Metering and cost identical.
    assert normalize(on["usage"]) == normalize(off["usage"])
    # 5. Outcome rows identical apart from the switchyard columns.
    assert (normalize(on["outcomes"], drop=SWITCHYARD_OUTCOME_COLUMNS)
            == normalize(off["outcomes"], drop=SWITCHYARD_OUTCOME_COLUMNS))

    # ...and the advisor really ran in the `on` run, and only there.
    assert all(not s for s in off["spans"])
    assert len(on["spans"]) == len(NON_STREAM) + len(STREAM)
    for attrs in on["spans"]:
        assert attrs["sp.switchyard.mode"] == mode
        assert "switchyard" in attrs["langfuse.trace.tags"]
        assert f"switchyard:{mode}" in attrs["langfuse.trace.tags"]
    assert all(o["switchyard_mode"] is None for o in off["outcomes"])
    assert all(o["switchyard_mode"] == mode for o in on["outcomes"])
    for body in off["bodies"]:
        assert all("advisor" not in rec for rec in body["trace"])
    for body in on["bodies"]:
        served = [r for r in body["trace"] if r.get("step") == "router" and r.get("chosen")]
        assert served[-1]["advisor"]["mode"] == mode
