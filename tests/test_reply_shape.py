"""B71 item 6: the content-free shape of a backend reply on the outcome row.

An HTTP 200 with no text and no tool call used to be indistinguishable, on the
record, from an answer. The row now says whether the reply had text, how many
tool calls, the backend's finish reason and its reasoning tokens -- and still
nothing of what was said."""
import json
from types import SimpleNamespace

import pytest

from app.apps.outcomes import reply_shape
from app.gateway.backends.litellm_backend import _finish_reason, _reasoning_tokens

_ADMIN = {"email": "admin@routeforge.local", "password": "changeme"}


@pytest.mark.parametrize("text,tool_calls,expected", [
    ("an answer", None, (True, 0)),
    ("", None, (False, 0)),
    ("   \n", None, (False, 0)),
    ("", [{"id": "c1"}, {"id": "c2"}], (False, 2)),
    ("thinking aloud", [{"id": "c1"}], (True, 1)),
])
def test_reply_shape(text, tool_calls, expected):
    got = reply_shape(SimpleNamespace(text=text, tool_calls=tool_calls,
                                      finish_reason="stop", reasoning_tokens=7))
    assert (got["reply_has_text"], got["reply_tool_calls"]) == expected
    assert got["finish_reason"] == "stop" and got["reasoning_tokens"] == 7
    assert set(got) == {"finish_reason", "reply_has_text", "reply_tool_calls", "reasoning_tokens"}


def test_finish_reason_is_an_enum_never_a_backend_string():
    assert _finish_reason("length") == "length"
    assert _finish_reason("tool_calls") == "tool_calls"
    assert _finish_reason(None) is None
    assert _finish_reason("the user's ssn is 482-73-1946") == "other"


def test_reasoning_tokens_from_either_usage_shape():
    assert _reasoning_tokens({"completion_tokens_details": {"reasoning_tokens": 137}}) == 137
    assert _reasoning_tokens(SimpleNamespace(
        completion_tokens_details=SimpleNamespace(reasoning_tokens=5))) == 5
    assert _reasoning_tokens({}) is None
    assert _reasoning_tokens({"completion_tokens_details": {"reasoning_tokens": "many"}}) is None


def _app(client, app_id):
    assert client.post("/admin/login", json=_ADMIN).status_code == 200
    created = client.post("/admin/applications", json={"id": app_id, "name": app_id})
    assert created.status_code == 200, created.text
    assert client.put(f"/admin/applications/{app_id}/policy",
                      json={"pii_detection": "regex"}).status_code == 200
    return created.json()["token"]


def _rows(client, app_id):
    r = client.get("/admin/outcomes/export", params={"application_id": app_id})
    assert r.status_code == 200, r.text
    return sorted((json.loads(l)["outcome"] for l in r.text.splitlines() if l.strip()),
                  key=lambda o: o["ts"]), r.text


def test_outcome_row_distinguishes_an_empty_reply_from_an_answer(client, monkeypatch):
    from tests._litellm_fake import make_recording_acompletion, make_response
    from app.gateway.backends import litellm_backend
    token = _app(client, "shape-app")
    for content in ("the capital is Paris", ""):
        monkeypatch.setattr(litellm_backend, "acompletion",
                            make_recording_acompletion(make_response(content=content)))
        r = client.post("/v1/chat/completions", headers={"Authorization": f"Bearer {token}"},
                        json={"messages": [{"role": "user", "content": "capital of France?"}]})
        assert r.status_code == 200, r.text
    rows, text = _rows(client, "shape-app")
    assert [o["reply_has_text"] for o in rows] == [True, False]
    assert [o["reply_tool_calls"] for o in rows] == [0, 0]
    assert all(o["status"] == "ok" for o in rows)
    assert "Paris" not in text
