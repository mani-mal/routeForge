"""Admin-console feedback route: score an existing request by its stored
trace_id, plus a best-effort lazy score-config creator (F1 Phase D task 4).

`POST /api/requests/{request_id}/feedback` is admin-gated (inherited from
`app/api/ui_routes.py`'s router-level `Depends(require_admin)`), looks the
request up in `HistoryStore`, resolves its Langfuse project via the
request's tenant (added: `HistoryStore.get_tenant`), and posts through the
same `post_score` helper the app-token-authenticated `/v1/feedback` route
uses (tests/test_feedback_api.py).
"""
import json

import httpx

from app.obs.scores import post_score  # noqa: F401  (imported for readers' context)
from tests.test_admin_api import _login

_MSG = {"messages": [{"role": "user", "content": "hi"}]}


def _make_app_with_langfuse(client, app_id: str):
    _login(client)
    r = client.post("/admin/applications", json={"id": app_id, "name": app_id})
    assert r.status_code == 200, r.text
    token = r.json()["token"]
    lf = client.put(f"/admin/applications/{app_id}/langfuse", json={
        "host": "http://airgap:3000", "public_key": "pk", "secret_key": "sk",
        "project_id": "proj_air"})
    assert lf.status_code == 200, lf.text
    return token


def _patch_httpx(monkeypatch, handler):
    real_ac = httpx.AsyncClient

    def fake_client(*args, **kwargs):
        kwargs.pop("transport", None)
        return real_ac(*args, transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", fake_client)


def test_admin_feedback_unknown_request_404(client):
    _login(client)
    r = client.post("/api/requests/req-deadbeef/feedback",
                    json={"score_name": "route_correct", "value": 1})
    assert r.status_code == 404


def test_admin_feedback_requires_admin(client):
    # No _login: the router-level Depends(require_admin) must reject.
    r = client.post("/api/requests/req-deadbeef/feedback",
                    json={"score_name": "route_correct", "value": 1})
    assert r.status_code in (401, 403)


def test_admin_feedback_posts_for_known_request(client, monkeypatch):
    token = _make_app_with_langfuse(client, "fbadmin1")

    served = client.post("/v1/chat/completions",
                         headers={"Authorization": f"Bearer {token}"}, json=_MSG)
    assert served.status_code == 200, served.text
    rid = served.json()["id"]
    trace_id = served.json()["trace_id"]

    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append({"url": str(request.url), "auth": request.headers.get("authorization"),
                      "body": json.loads(request.content)})
        return httpx.Response(200, json={"id": "score_1"})

    _patch_httpx(monkeypatch, handler)

    r = client.post(f"/api/requests/{rid}/feedback",
                    json={"score_name": "route_correct", "value": 1})
    assert r.status_code == 200, r.text
    assert r.json()["stored"] is True

    score_calls = [c for c in calls if c["url"].endswith("/api/public/scores")]
    assert len(score_calls) == 1
    call = score_calls[0]
    assert call["body"]["traceId"] == trace_id
    assert call["body"]["name"] == "route_correct"
    import base64
    scheme, _, encoded = call["auth"].partition(" ")
    assert scheme == "Basic"
    assert base64.b64decode(encoded).decode() == "pk:sk"


def test_admin_feedback_corrected_output_posts_second_score(client, monkeypatch):
    token = _make_app_with_langfuse(client, "fbadmin4")

    served = client.post("/v1/chat/completions",
                         headers={"Authorization": f"Bearer {token}"}, json=_MSG)
    assert served.status_code == 200, served.text
    rid = served.json()["id"]

    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append({"url": str(request.url), "body": json.loads(request.content)})
        return httpx.Response(200, json={"id": "score_x"})

    _patch_httpx(monkeypatch, handler)

    r = client.post(f"/api/requests/{rid}/feedback",
                    json={"score_name": "quality", "value": 0.5,
                          "corrected_output": "the fixed answer"})
    assert r.status_code == 200, r.text
    assert r.json()["stored"] is True

    score_calls = [c["body"] for c in calls if c["url"].endswith("/api/public/scores")]
    assert len(score_calls) == 2
    names = {c["name"] for c in score_calls}
    assert names == {"quality", "corrected_output"}
    corrected = next(c for c in score_calls if c["name"] == "corrected_output")
    assert corrected["value"] == 1
    assert corrected["comment"] == "the fixed answer"


def test_admin_feedback_no_trace_id_reports_reason(client, monkeypatch):
    # Same seeding as above, but this app has no Langfuse project, so
    # export_trace never authors a real span context -> resp has no
    # "trace_id" key -> the admin route must report the no-trace reason
    # without ever attempting a score post.
    _login(client)
    r = client.post("/admin/applications", json={"id": "fbadmin2", "name": "fbadmin2"})
    assert r.status_code == 200, r.text
    token = r.json()["token"]

    served = client.post("/v1/chat/completions",
                         headers={"Authorization": f"Bearer {token}"}, json=_MSG)
    assert served.status_code == 200, served.text
    rid = served.json()["id"]
    assert "trace_id" not in served.json()

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("no score post expected when there's no trace_id")

    _patch_httpx(monkeypatch, handler)

    r = client.post(f"/api/requests/{rid}/feedback",
                    json={"score_name": "route_correct", "value": 1})
    assert r.status_code == 200, r.text
    # P2b-2 Task 5: no trace_id still records locally (outcomes.enabled
    # defaults True and the served request wrote a routing_outcomes row).
    assert r.json() == {"stored": False, "reason": "no trace", "recorded": True}


def test_admin_feedback_unknown_score_name_422(client):
    token = _make_app_with_langfuse(client, "fbadmin3")
    served = client.post("/v1/chat/completions",
                         headers={"Authorization": f"Bearer {token}"}, json=_MSG)
    rid = served.json()["id"]

    r = client.post(f"/api/requests/{rid}/feedback",
                    json={"score_name": "bogus", "value": 1})
    assert r.status_code == 422
