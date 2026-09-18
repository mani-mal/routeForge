"""The `model_input` axis at the HTTP boundary (design P1a §2.3-§2.4).

These are deliberately end-to-end, through POST /v1/chat/completions with a
recording backend, not calls into `Pipeline`. The claim being made is about
what the BACKEND receives, and an internal-call test cannot make it: the whole
bug class this feature exists to kill was a consumer quietly reusing another
consumer's view of the request somewhere between the gate and the wire.

The headline configuration is the one that could not be expressed before:
`model_input: raw` + `client_output: redact` -- a trusted model inside the
customer's boundary sees the real value, the client response does not.
"""
import json

from tests._litellm_fake import CALLS, make_recording_acompletion, make_response
from app.gateway.backends import litellm_backend

_ADMIN = {"email": "admin@routeforge.local", "password": "changeme"}
SSN = "482-73-1946"
PROMPT = f"look up the record for ssn {SSN}"


def _app_with_policy(client, app_id: str, policy: dict) -> str:
    """Create an application through the admin API, set its policy, and return
    its API token."""
    login = client.post("/admin/login", json=_ADMIN)
    assert login.status_code == 200, login.text
    created = client.post("/admin/applications", json={"id": app_id, "name": app_id})
    assert created.status_code == 200, created.text
    put = client.put(f"/admin/applications/{app_id}/policy", json=policy)
    assert put.status_code == 200, put.text
    return created.json()["token"]


def _ask(client, token: str, *, completion: str) -> dict:
    CALLS.clear()
    r = client.post("/v1/chat/completions",
                    headers={"Authorization": f"Bearer {token}"},
                    json={"messages": [{"role": "user", "content": PROMPT}]})
    assert r.status_code == 200, r.text
    return r.json()


def test_model_receives_raw_text_under_model_input_raw(client, monkeypatch):
    """`model_input: raw` with `client_output: redact`: the text handed to the
    backend contains the real SSN; the HTTP response does not."""
    token = _app_with_policy(client, "raw-in-redact-out", {
        "residency": "onprem_only", "model_input": "raw",
        "client_output": "redact", "pii_detection": "regex"})
    monkeypatch.setattr(
        litellm_backend, "acompletion",
        make_recording_acompletion(make_response(content=f"found ssn {SSN}")))

    body = _ask(client, token, completion=f"found ssn {SSN}")

    sent = json.dumps(CALLS[-1]["messages"])
    assert SSN in sent, "the model must see the real value under model_input: raw"
    # ...and the client must not, because client_output is redact.
    assert SSN not in json.dumps(body)
    assert "<US_SSN_1>" in body["choices"][0]["message"]["content"]
    # Placement is untouched by either protection axis (CLAUDE.md rule 3).
    assert body["policy"]["onprem_only"] is True
    assert body["policy"]["allowed_providers"] == ["onprem"]
    assert "US_SSN" in body["policy"]["detected_entities"]


def test_model_receives_redacted_text_under_model_input_redact(client, monkeypatch):
    """The inverse, and today's default: the backend sees `<US_SSN_1>`."""
    token = _app_with_policy(client, "redact-in", {
        "residency": "onprem_only", "model_input": "redact",
        "client_output": "redact", "pii_detection": "regex"})
    monkeypatch.setattr(
        litellm_backend, "acompletion",
        make_recording_acompletion(make_response(content="found it")))

    body = _ask(client, token, completion="found it")

    sent = json.dumps(CALLS[-1]["messages"])
    assert SSN not in sent
    assert "<US_SSN_1>" in sent
    assert SSN not in json.dumps(body)
    assert body["policy"]["onprem_only"] is True


def test_router_only_app_sends_raw_and_returns_raw(client, monkeypatch):
    """The other end of the axis pair: a router-only app (`passthrough`, no
    detector) is byte-identical to today -- the model gets the request
    verbatim and the completion comes back untouched."""
    token = _app_with_policy(client, "router-only", {
        "residency": "onprem_only", "policy_mode": "passthrough"})
    monkeypatch.setattr(
        litellm_backend, "acompletion",
        make_recording_acompletion(make_response(content=f"found ssn {SSN}")))

    body = _ask(client, token, completion=f"found ssn {SSN}")

    assert SSN in json.dumps(CALLS[-1]["messages"])
    assert body["choices"][0]["message"]["content"] == f"found ssn {SSN}"
    assert body["policy"]["policy_mode"] == "passthrough"
    assert body["policy"]["onprem_only"] is True


def test_replayed_tool_call_arguments_reach_the_model_raw(client, monkeypatch):
    """The client-driven tool-calling path honours `model_input` too: it
    redacts per message rather than flattening, so it has its own copy of the
    decision and its own way to get it wrong. Under `model_input: raw` a
    replayed tool call's arguments go to the model unredacted."""
    token = _app_with_policy(client, "raw-in-tools", {
        "residency": "onprem_only", "model_input": "raw",
        "client_output": "redact", "pii_detection": "regex"})
    CALLS.clear()
    monkeypatch.setattr(
        litellm_backend, "acompletion",
        make_recording_acompletion(make_response(content="found it")))
    args = json.dumps({"ssn": SSN})
    r = client.post("/v1/chat/completions",
                    headers={"Authorization": f"Bearer {token}"}, json={
                        "messages": [
                            {"role": "user", "content": "find the ticket"},
                            {"role": "assistant", "content": None, "tool_calls": [{
                                "id": "c1", "type": "function",
                                "function": {"name": "find_ticket",
                                             "arguments": args}}]},
                            {"role": "tool", "tool_call_id": "c1",
                             "content": "ticket 42"},
                        ],
                        "tools": [{"type": "function", "function": {
                            "name": "find_ticket", "description": "look up",
                            "parameters": {"type": "object", "properties": {}}}}],
                    })
    assert r.status_code == 200, r.text
    sent = CALLS[-1]["messages"]
    assistant = next(m for m in sent if m.get("role") == "assistant")
    assert assistant["tool_calls"][0]["function"]["arguments"] == args
    assert SSN in json.dumps(sent)
    # The gate still classified those arguments, so placement escalated.
    assert r.json()["policy"]["onprem_only"] is True


def test_replayed_tool_call_arguments_stay_redacted_under_model_input_redact(
        client, monkeypatch):
    """The contrast, so the raw case above cannot be an accident."""
    token = _app_with_policy(client, "redact-in-tools", {
        "residency": "onprem_only", "model_input": "redact",
        "client_output": "redact", "pii_detection": "regex"})
    CALLS.clear()
    monkeypatch.setattr(
        litellm_backend, "acompletion",
        make_recording_acompletion(make_response(content="found it")))
    r = client.post("/v1/chat/completions",
                    headers={"Authorization": f"Bearer {token}"}, json={
                        "messages": [
                            {"role": "user", "content": "find the ticket"},
                            {"role": "assistant", "content": None, "tool_calls": [{
                                "id": "c1", "type": "function",
                                "function": {"name": "find_ticket",
                                             "arguments": json.dumps({"ssn": SSN})}}]},
                            {"role": "tool", "tool_call_id": "c1",
                             "content": "ticket 42"},
                        ],
                        "tools": [{"type": "function", "function": {
                            "name": "find_ticket", "description": "look up",
                            "parameters": {"type": "object", "properties": {}}}}],
                    })
    assert r.status_code == 200, r.text
    assert SSN not in json.dumps(CALLS[-1]["messages"])


# --------------------------------------------------------------------------- #
# The `client_output` axis (design P1a §2.5): egress is independent of ingress.
#
# The two tests below are the ones this whole feature exists for. The first is
# the headline claim -- a TRUSTED model inside the customer's boundary is handed
# the real value while the CLIENT response is redacted -- and it is asserted at
# the HTTP boundary, against what the backend really received and what the
# response body really contains, because that is the only place the claim is
# falsifiable.
# --------------------------------------------------------------------------- #

TICKET_SSN = "123-45-6789"
TICKET_PROMPT = f"Summarize the ticket for Jane Smith. Her SSN is {TICKET_SSN}."


def test_client_output_redact_removes_the_ssn_the_model_saw(client, monkeypatch):
    """`model_input: raw` + `client_output: redact`, end to end: the backend is
    handed the real SSN, and the HTTP body carries a placeholder instead.

    Egress redaction here is NOT a by-product of the model having been sent a
    redacted prompt (it wasn't) -- step 9 re-runs the app's real detector over
    the completion because `client_output` says so. That is the whole point of
    splitting the two axes."""
    token = _app_with_policy(client, "trusted-model-redacted-client", {
        "residency": "onprem_only", "model_input": "raw",
        "client_output": "redact", "pii_detection": "regex"})
    completion = f"The ticket for Jane Smith cites SSN {TICKET_SSN}."
    monkeypatch.setattr(
        litellm_backend, "acompletion",
        make_recording_acompletion(make_response(content=completion)))

    CALLS.clear()
    r = client.post("/v1/chat/completions",
                    headers={"Authorization": f"Bearer {token}"},
                    json={"messages": [{"role": "user", "content": TICKET_PROMPT}]})
    assert r.status_code == 200, r.text

    # (1) the model really saw the real value...
    sent = json.dumps(CALLS[-1]["messages"])
    assert TICKET_SSN in sent, "the model must see the real value under model_input: raw"
    # (2) ...and the client really did not, anywhere in the response.
    body = r.json()
    assert TICKET_SSN not in json.dumps(body)
    assert "<US_SSN_1>" in body["choices"][0]["message"]["content"]
    assert "US_SSN" in body["policy"]["detected_entities"]


def test_client_output_raw_leaves_the_completion_untouched(client, monkeypatch):
    """The inverse pairing, and the proof the two axes are read separately:
    `model_input: redact` + `client_output: raw`. A real detector runs (the
    model is sent a placeholder), yet NOTHING is redacted on egress -- the
    completion comes back byte-for-byte as the backend produced it."""
    token = _app_with_policy(client, "redacted-model-raw-client", {
        "residency": "onprem_only", "model_input": "redact",
        "client_output": "raw", "pii_detection": "regex"})
    completion = f"The ticket for Jane Smith cites SSN {TICKET_SSN}."
    monkeypatch.setattr(
        litellm_backend, "acompletion",
        make_recording_acompletion(make_response(content=completion)))

    CALLS.clear()
    r = client.post("/v1/chat/completions",
                    headers={"Authorization": f"Bearer {token}"},
                    json={"messages": [{"role": "user", "content": TICKET_PROMPT}]})
    assert r.status_code == 200, r.text

    sent = json.dumps(CALLS[-1]["messages"])
    assert TICKET_SSN not in sent
    assert "<US_SSN_1>" in sent
    assert r.json()["choices"][0]["message"]["content"] == completion
