"""/admin/metrics/summary and /admin/metrics/models."""
import json
import os

from tests.test_admin_api import _login
from tests.test_admin_usage_by_app import _chat, _seed

PROMPT = "zebra-unique-prompt-text-91827"


def test_summary_requires_admin(client):
    assert client.get("/admin/metrics/summary").status_code == 401
    assert client.get("/admin/metrics/models").status_code == 401


def test_summary_counts_served_requests(client):
    _, tok = _seed(client, "mx-a", "a")
    for _i in range(3):
        _chat(client, tok)
    _login(client)
    body = client.get("/admin/metrics/summary").json()
    assert body["current"]["completed"]["n"] == 3
    assert body["current"]["completed"]["insufficient"] is True
    assert body["current"]["spend"]["value"] > 0
    assert {"routing", "prompt_cache", "compression", "semantic_cache", "total"} <= set(body["current"]["savings"])
    assert {"window", "previous", "alerts", "baseline", "generated_at"} <= set(body)
    _dump("summary", body)


def test_models_lists_served_model(client):
    _, tok = _seed(client, "mx-b", "b")
    _chat(client, tok); _chat(client, tok)
    _login(client)
    body = client.get("/admin/metrics/models").json()
    assert body["models"]
    row = body["models"][0]
    assert row["requests"] == 2 and row["placement"] in ("onprem", "cloud")
    _dump("models", body)


def test_hours_out_of_range_is_422(client):
    _login(client)
    for h in (0, 9000):
        assert client.get(f"/admin/metrics/summary?hours={h}").status_code == 422
        assert client.get(f"/admin/metrics/models?hours={h}").status_code == 422


def test_bad_placement_is_422(client):
    _login(client)
    assert client.get("/admin/metrics/summary?placement=moon").status_code == 422


def test_application_filter_scopes(client):
    a, ta = _seed(client, "mx-c", "c")
    b, tb = _seed(client, "mx-d", "d")
    _chat(client, ta); _chat(client, ta); _chat(client, tb)
    _login(client)
    assert client.get(f"/admin/metrics/summary?application_id={a}").json()["current"]["spend"]["n"] == 2
    assert client.get(f"/admin/metrics/summary?application_id={b}").json()["current"]["spend"]["n"] == 1


def test_response_is_content_free(client):
    _, tok = _seed(client, "mx-e", "e")
    _chat(client, tok, PROMPT)
    _login(client)
    for path in ("summary", "models"):
        assert PROMPT not in json.dumps(client.get(f"/admin/metrics/{path}").json())


def _dump(name, body):
    d = os.environ.get("METRICS_DUMP_DIR")
    if d:
        open(os.path.join(d, name + ".json"), "w").write(json.dumps(body, indent=2))
