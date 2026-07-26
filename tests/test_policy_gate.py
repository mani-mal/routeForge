# A valid seeded token — auth is fail-closed now, so every served request needs one.
_AUTH = {"Authorization": "Bearer demo-key"}


def test_ssn_request_is_onprem_only(client):
    r = client.post("/v1/chat/completions", headers=_AUTH, json={
        "messages": [{"role": "user", "content": "verify SSN 482-73-1946"}]})
    body = r.json()
    assert body["policy"]["onprem_only"] is True
    assert body["policy"]["allowed_providers"] == ["onprem"]
    assert "US_SSN" in body["policy"]["detected_entities"]


def test_clean_request_full_pool(client):
    r = client.post("/v1/chat/completions", headers=_AUTH, json={
        "messages": [{"role": "user", "content": "capital of France?"}]})
    body = r.json()
    assert body["policy"]["onprem_only"] is False
    assert body["policy"]["sensitivity"] == "public"


def test_stream_rejected(client):
    r = client.post("/v1/chat/completions", headers=_AUTH, json={
        "messages": [{"role": "user", "content": "hi"}], "stream": True})
    assert r.status_code == 400


def test_no_ssn_value_in_response(client):
    r = client.post("/v1/chat/completions", headers=_AUTH, json={
        "messages": [{"role": "user", "content": "SSN 482-73-1946"}]})
    assert "482-73-1946" not in r.text
