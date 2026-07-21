def test_ssn_request_is_onprem_only(client):
    r = client.post("/v1/chat/completions", json={
        "messages": [{"role": "user", "content": "verify SSN 123-45-6789"}]})
    body = r.json()
    assert body["onprem_only"] is True
    assert body["allowed_providers"] == ["onprem"]
    assert "US_SSN" in body["detected_entities"]


def test_clean_request_full_pool(client):
    r = client.post("/v1/chat/completions", json={
        "messages": [{"role": "user", "content": "capital of France?"}]})
    body = r.json()
    assert body["onprem_only"] is False
    assert body["sensitivity"] == "public"


def test_stream_rejected(client):
    r = client.post("/v1/chat/completions", json={
        "messages": [{"role": "user", "content": "hi"}], "stream": True})
    assert r.status_code == 400


def test_no_ssn_value_in_response(client):
    r = client.post("/v1/chat/completions", json={
        "messages": [{"role": "user", "content": "SSN 123-45-6789"}]})
    assert "123-45-6789" not in r.text
