"""POST /v1/feedback + post_score Langfuse-REST helper (F1 Phase D / B16).

Feedback must post straight to the calling app's OWN residency-local
Langfuse project (never a shared one), and must fail closed — dropped, not
errored — when the app has no provisioned project.
"""
import asyncio
import json

import httpx

from app.obs.scores import post_score
from tests.test_admin_api import _login  # noqa: F401


def _make_app(client, app_id: str = "fbapp", *, with_langfuse: bool = False):
    _login(client)
    r = client.post("/admin/applications", json={"id": app_id, "name": app_id})
    assert r.status_code == 200, r.text
    body = r.json()
    token = body["token"]
    if with_langfuse:
        lf = client.put(f"/admin/applications/{app_id}/langfuse", json={
            "host": "http://airgap:3000", "public_key": "pk", "secret_key": "sk",
            "project_id": "proj_air"})
        assert lf.status_code == 200, lf.text
    return app_id, token


def test_feedback_posts_score_to_app_project(client, monkeypatch):
    app_id, token = _make_app(client, "fbapp1", with_langfuse=True)

    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["auth"] = request.headers.get("authorization")
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"id": "score_1"})

    real_ac = httpx.AsyncClient

    def fake_client(*args, **kwargs):
        kwargs.pop("transport", None)
        return real_ac(*args, transport=httpx.MockTransport(handler), **kwargs)

    # `post_score` does `import httpx` locally, which binds the same module
    # object as this top-level `import httpx` -- patching the real module's
    # AsyncClient here is what the function sees too.
    monkeypatch.setattr(httpx, "AsyncClient", fake_client)

    trace_id = "a" * 32
    r = client.post("/v1/feedback", headers={"Authorization": f"Bearer {token}"},
                    json={"trace_id": trace_id, "score_name": "route_correct",
                          "value": 1})
    assert r.status_code == 200, r.text
    assert r.json()["stored"] is True

    assert captured["url"].endswith("/api/public/scores")
    import base64
    scheme, _, encoded = captured["auth"].partition(" ")
    assert scheme == "Basic"
    assert base64.b64decode(encoded).decode() == "pk:sk"
    assert captured["body"] == {
        "traceId": trace_id, "name": "route_correct",
        "value": 1, "dataType": "NUMERIC",
    }


def test_feedback_unknown_score_name_422(client):
    app_id, token = _make_app(client, "fbapp2")
    r = client.post("/v1/feedback", headers={"Authorization": f"Bearer {token}"},
                    json={"trace_id": "a" * 32, "score_name": "bogus", "value": 1})
    assert r.status_code == 422


def test_feedback_no_project_fails_closed(client):
    app_id, token = _make_app(client, "fbapp3", with_langfuse=False)
    r = client.post("/v1/feedback", headers={"Authorization": f"Bearer {token}"},
                    json={"trace_id": "a" * 32, "score_name": "route_correct",
                          "value": 1})
    assert r.status_code == 200
    assert r.json()["stored"] is False


def test_feedback_unauthenticated_rejected(client):
    r = client.post("/v1/feedback", headers={"Authorization": "Bearer nope"},
                    json={"trace_id": "a" * 32, "score_name": "route_correct",
                          "value": 1})
    assert r.status_code == 401


def test_feedback_corrected_output_posts_second_score(client, monkeypatch):
    app_id, token = _make_app(client, "fbapp4", with_langfuse=True)

    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append({"url": str(request.url), "body": json.loads(request.content)})
        return httpx.Response(200, json={"id": "score_x"})

    real_ac = httpx.AsyncClient

    def fake_client(*args, **kwargs):
        kwargs.pop("transport", None)
        return real_ac(*args, transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", fake_client)

    trace_id = "b" * 32
    r = client.post("/v1/feedback", headers={"Authorization": f"Bearer {token}"},
                    json={"trace_id": trace_id, "score_name": "quality",
                          "value": 0.5, "corrected_output": "the fixed answer"})
    assert r.status_code == 200
    assert r.json()["stored"] is True
    # Two /api/public/score-configs POSTs (best-effort, ensure_score_config)
    # also fire here now (one per score name) -- filter down to the actual
    # /api/public/scores posts, which is what this test asserts on.
    score_calls = [c["body"] for c in calls if c["url"].endswith("/api/public/scores")]
    assert len(score_calls) == 2
    names = {c["name"] for c in score_calls}
    assert names == {"quality", "corrected_output"}
    corrected = next(c for c in score_calls if c["name"] == "corrected_output")
    assert corrected["comment"] == "the fixed answer"
    assert corrected["value"] == 1
    assert corrected["dataType"] == "NUMERIC"


# --- unit tests of post_score directly (no HTTP client, no event loop needed
# from pytest-asyncio, which isn't installed here) -- mirrors the asyncio.run
# pattern already used in tests/test_langfuse_provision.py ------------------

def test_post_score_returns_false_with_no_project():
    assert asyncio.run(post_score(None, "a" * 32, "route_correct", 1)) is False


def test_post_score_uses_injected_client_and_categorical_dtype():
    from app.apps.langfuse_provision import AppLangfuse

    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"id": "s1"})

    async def go():
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        try:
            app_lf = AppLangfuse(project_id="p1", public_key="pk1",
                                 secret_key="sk1", host="http://lf.test:3000")
            return await post_score(app_lf, "c" * 32, "failure_type", "timeout",
                                    comment="ran long", client=client)
        finally:
            await client.aclose()

    result = asyncio.run(go())
    assert result is True
    assert captured["body"] == {
        "traceId": "c" * 32, "name": "failure_type",
        "value": "timeout", "dataType": "CATEGORICAL", "comment": "ran long",
    }


def test_post_score_returns_false_on_non_2xx():
    from app.apps.langfuse_provision import AppLangfuse

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    async def go():
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        try:
            app_lf = AppLangfuse(project_id="p1", public_key="pk1",
                                 secret_key="sk1", host="http://lf.test:3000")
            return await post_score(app_lf, "d" * 32, "route_correct", 1,
                                    client=client)
        finally:
            await client.aclose()

    assert asyncio.run(go()) is False


# --- non-finite numeric values are refused at the boundary -------------------
# Python's json (and so Starlette's request.json()) accepts the non-standard
# `NaN`/`Infinity` literals, so the body is built by hand: a NaN/inf score must
# be a 422 before anything reaches Langfuse or the local outcome_feedback table.

import pytest  # noqa: E402

from tests.conftest import v1_error  # noqa: E402


@pytest.mark.parametrize("literal", ["NaN", "Infinity", "-Infinity"])
def test_feedback_rejects_non_finite_value(client, monkeypatch, literal):
    import app.api.routes as routes_mod

    app_id, token = _make_app(client, f"fbnan{literal.lower().strip('-')}"
                              f"{'n' if literal.startswith('-') else ''}",
                              with_langfuse=True)
    posted, written = [], []

    async def _post(*a, **kw):
        posted.append(a)
        return True

    async def _write(*a, **kw):
        written.append(kw)
        return True

    monkeypatch.setattr(routes_mod, "post_score", _post)
    monkeypatch.setattr(routes_mod, "_write_feedback_safe", _write)
    body = ('{"trace_id": "' + "e" * 32 + '", "request_id": "req-x", '
            '"score_name": "route_correct", "value": ' + literal + '}')
    r = client.post("/v1/feedback", content=body,
                    headers={"Authorization": f"Bearer {token}",
                             "Content-Type": "application/json"})
    assert r.status_code == 422, r.text
    assert v1_error(r)["code"] == "invalid_value"
    assert posted == [] and written == []
