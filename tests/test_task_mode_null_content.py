from app.models import ChatRequest
from app.routing.task_mode import decide


def test_null_content_last_user_turn_is_direct_not_a_crash():
    req = ChatRequest.model_validate({"messages": [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "hi"},
        {"role": "user", "content": None}]})
    assert decide(None, req) == "direct"


def test_null_content_http_is_served(client):
    r = client.post("/v1/chat/completions", headers={"Authorization": "Bearer demo-key"},
                    json={"messages": [{"role": "user", "content": None}]})
    assert r.status_code == 200, r.text
