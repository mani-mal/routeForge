"""Contract: an agent client that sends no session header still gets one
session per conversation, derived from the conversation's opening turns
(stable as the history grows), and an explicit header always wins."""
import hashlib

from app.api import routes
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

def test_malformed_header_with_derivation_on_still_derives(client):
    r = client.post(
        "/v1/chat/completions",
        headers={"Authorization": "Bearer demo-key",
                 "x-routeforge-session": "has spaces & @!"},
        json={"messages": [U1]},
    )
    assert r.status_code == 200
    assert r.json()["session_id"].startswith("auto-")

def test_derived_session_id_never_becomes_the_memory_partition_key(client, monkeypatch):
    """Fix round 1 (Important): the derived id is for trace/console grouping
    ONLY. `pipeline.run`'s `session_id=` kwarg -- the memory partition key --
    must keep seeing exactly what the client sent (None when there is no
    header), never the server-derived `auto-...` id, even though the HTTP
    response and Langfuse trace do carry the derived id."""
    calls: list[str | None] = []
    real = client.app.state.pipeline.run

    async def recording(*a, **kw):
        calls.append(kw.get("session_id"))
        return await real(*a, **kw)

    monkeypatch.setattr(client.app.state.pipeline, "run", recording)

    r = client.post("/v1/chat/completions", headers={"Authorization": "Bearer demo-key"},
                    json={"messages": [U1]})
    assert r.status_code == 200
    assert calls[-1] is None                          # memory partition key: untouched
    assert r.json()["session_id"].startswith("auto-")  # response/trace: derived

    calls.clear()
    r2 = client.post(
        "/v1/chat/completions",
        headers={"Authorization": "Bearer demo-key", "x-routeforge-session": "wf-999"},
        json={"messages": [U1]},
    )
    assert r2.status_code == 200
    assert calls[-1] == "wf-999"                # memory partition key: the header value
    assert r2.json()["session_id"] == "wf-999"  # response/trace: same, explicit wins


# --- C1: the derivation hash must be keyed (HMAC), not a raw unkeyed sha256 --
# A telemetry viewer who sees the redacted prompt could otherwise brute-force
# low-entropy redacted values (e.g. SSNs) against the exported session id.

def _unkeyed_sha256_id(tenant: str, system: str, first_user: str) -> str:
    digest = hashlib.sha256(
        f"{tenant}\x00{system}\x00{first_user}".encode()).hexdigest()[:24]
    return f"auto-{digest}"


def test_derived_id_is_not_the_old_unkeyed_hash(monkeypatch):
    monkeypatch.setenv("SP_SESSION_ID_KEY", "a-fixed-test-key")
    routes._reset_session_id_key_cache()
    got = _derive_session_id(_req(SYS, U1), "app-a")
    old = _unkeyed_sha256_id("app-a", SYS["content"], U1["content"])
    assert got != old
    assert got.startswith("auto-") and len(got) == 5 + 24


def test_same_key_gives_a_stable_id(monkeypatch):
    monkeypatch.setenv("SP_SESSION_ID_KEY", "shared-key-1")
    routes._reset_session_id_key_cache()
    a = _derive_session_id(_req(SYS, U1), "app-a")
    routes._reset_session_id_key_cache()  # simulate a fresh resolution of the same env key
    b = _derive_session_id(_req(SYS, U1), "app-a")
    assert a == b


def test_different_key_gives_a_different_id(monkeypatch):
    monkeypatch.setenv("SP_SESSION_ID_KEY", "key-one")
    routes._reset_session_id_key_cache()
    a = _derive_session_id(_req(SYS, U1), "app-a")
    monkeypatch.setenv("SP_SESSION_ID_KEY", "key-two")
    routes._reset_session_id_key_cache()
    b = _derive_session_id(_req(SYS, U1), "app-a")
    assert a != b


def test_unset_key_env_still_derives_and_is_stable_within_the_process(monkeypatch):
    monkeypatch.delenv("SP_SESSION_ID_KEY", raising=False)
    routes._reset_session_id_key_cache()
    a = _derive_session_id(_req(SYS, U1), "app-a")
    b = _derive_session_id(_req(SYS, U1), "app-a")
    assert a is not None
    assert a == b
    routes._reset_session_id_key_cache()
