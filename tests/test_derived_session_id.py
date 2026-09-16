"""Contract: an agent client that sends no session header still gets one
session per conversation, derived from the conversation's opening turns
(stable as the history grows), and an explicit header always wins."""
from app.api.routes import _derive_session_id
from app.models import ChatRequest

def _req(*msgs):
    return ChatRequest.model_validate({"messages": [dict(m) for m in msgs]})

SYS = {"role": "system", "content": "You are a coding agent."}
U1 = {"role": "user", "content": "Refactor the billing module."}

def test_same_opening_turns_share_a_session_as_history_grows():
    first = _derive_session_id(_req(SYS, U1), "app-a")
    later = _derive_session_id(_req(SYS, U1,
                                    {"role": "assistant", "content": "ok"},
                                    {"role": "user", "content": "now add tests"}), "app-a")
    assert first == later
    assert first.startswith("auto-") and len(first) == 5 + 24

def test_different_conversation_or_app_gets_a_different_session():
    base = _derive_session_id(_req(SYS, U1), "app-a")
    assert _derive_session_id(_req(SYS, {"role": "user", "content": "Fix the login bug."}),
                              "app-a") != base
    assert _derive_session_id(_req(SYS, U1), "app-b") != base

def test_no_user_turn_means_no_session():
    assert _derive_session_id(_req(SYS), "app-a") is None

def test_content_parts_are_handled():
    parts = {"role": "user", "content": [{"type": "text", "text": "Refactor the billing module."}]}
    assert _derive_session_id(_req(SYS, parts), "app-a") == _derive_session_id(_req(SYS, U1), "app-a")

def test_http_response_carries_derived_session_and_header_wins(client):
    h = {"Authorization": "Bearer demo-key"}
    body = {"messages": [U1]}
    derived = client.post("/v1/chat/completions", headers=h, json=body).json()["session_id"]
    assert derived.startswith("auto-")
    explicit = client.post("/v1/chat/completions",
                           headers={**h, "x-routeforge-session": "wf-123"}, json=body).json()
    assert explicit["session_id"] == "wf-123"

def test_switch_off_keeps_old_behaviour(client):
    s = client.app.state.settings
    client.app.state.settings = s.model_copy(update={
        "serving": s.serving.model_copy(update={"derive_session_id": False})})
    r = client.post("/v1/chat/completions", headers={"Authorization": "Bearer demo-key"},
                    json={"messages": [U1]})
    assert not r.json().get("session_id")
