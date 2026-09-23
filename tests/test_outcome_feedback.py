"""P2b-2 Task 5: local feedback storage, request-id linkage and app scoping.

`POST /v1/feedback` (app/api/routes.py) and the admin console's
`POST /api/requests/{request_id}/feedback` (app/api/ui_routes.py) both keep
posting to Langfuse exactly as before (`stored`), and now ALSO write a
content-free row to the local `outcome_feedback` table (`recorded`) when the
scored request can be found -- by `request_id` (preferred) or `trace_id` --
scoped to the CALLING app. `comment` and `corrected_output` TEXT are never
persisted locally; only a fixed `value_num=1.0` `corrected_output` row is.

Consumer-contract style, like tests/test_outcome_hooks.py: a real app from
`create_app()`, a real TestClient, and the seeded demo apps/tokens
(`app/apps/bootstrap.py`) rather than mocks of route internals.
"""
from __future__ import annotations

import json
from contextlib import contextmanager

import httpx
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.apps import store as store_mod
from app.apps.models import OutcomeFeedback
from app.main import create_app
from tests.test_admin_api import _login

DEMO = {"Authorization": "Bearer demo-key"}
ACME = {"Authorization": "Bearer acme-key"}
HELLO = {"messages": [{"role": "user", "content": "hello"}]}


@contextmanager
def _client():
    with TestClient(create_app(), raise_server_exceptions=False) as c:
        yield c


def _feedback_rows(c: TestClient) -> list[OutcomeFeedback]:
    async def go():
        async with c.app.state.db.session() as session:
            return list((await session.execute(select(OutcomeFeedback))).scalars())
    return c.portal.call(go)


def _serve(c: TestClient, headers: dict) -> dict:
    r = c.post("/v1/chat/completions", headers=headers, json=HELLO)
    assert r.status_code == 200, r.text
    return r.json()


def _make_app_with_langfuse(c: TestClient, app_id: str) -> str:
    _login(c)
    r = c.post("/admin/applications", json={"id": app_id, "name": app_id})
    assert r.status_code == 200, r.text
    token = r.json()["token"]
    lf = c.put(f"/admin/applications/{app_id}/langfuse", json={
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


# --------------------------------------------------------------------------- #
# 1. feedback by request_id is recorded
# --------------------------------------------------------------------------- #
def test_feedback_by_request_id_is_recorded():
    with _client() as c:
        body = _serve(c, DEMO)
        rid = body["id"]

        r = c.post("/v1/feedback", headers=DEMO,
                   json={"request_id": rid, "score_name": "route_correct", "value": 1})
        assert r.status_code == 200, r.text
        assert r.json()["recorded"] is True

        rows = _feedback_rows(c)
        assert len(rows) == 1
        assert rows[0].request_id == rid
        assert rows[0].application_id == "default"
        assert rows[0].score_name == "route_correct"
        assert rows[0].value_num == 1.0
        assert rows[0].source == "app"


# --------------------------------------------------------------------------- #
# 2. feedback by trace_id is recorded, using an outcome row with a known
#    trace_id
# --------------------------------------------------------------------------- #
def test_feedback_by_trace_id_is_recorded():
    with _client() as c:
        token = _make_app_with_langfuse(c, "fbrec2")
        body = _serve(c, {"Authorization": f"Bearer {token}"})
        trace_id = body.get("trace_id")
        assert trace_id, "expected a trace_id once Langfuse is provisioned for the app"

        r = c.post("/v1/feedback", headers={"Authorization": f"Bearer {token}"},
                   json={"trace_id": trace_id, "score_name": "route_correct", "value": 1})
        assert r.status_code == 200, r.text
        assert r.json()["recorded"] is True

        rows = _feedback_rows(c)
        assert len(rows) == 1
        assert rows[0].request_id == body["id"]
        assert rows[0].application_id == "fbrec2"


# --------------------------------------------------------------------------- #
# 3. app B scoring app A's request -> recorded: false, no row written
# --------------------------------------------------------------------------- #
def test_cross_app_feedback_is_refused_and_writes_no_row():
    with _client() as c:
        body = _serve(c, DEMO)
        rid = body["id"]

        r = c.post("/v1/feedback", headers=ACME,
                   json={"request_id": rid, "score_name": "route_correct", "value": 1})
        assert r.status_code == 200, r.text
        assert r.json()["recorded"] is False

        assert _feedback_rows(c) == []


# --------------------------------------------------------------------------- #
# 4. a bad categorical value -> recorded: false
# --------------------------------------------------------------------------- #
def test_bad_categorical_value_is_refused():
    with _client() as c:
        body = _serve(c, DEMO)
        rid = body["id"]

        r = c.post("/v1/feedback", headers=DEMO,
                   json={"request_id": rid, "score_name": "failure_type",
                         "value": "bad value!"})
        assert r.status_code == 200, r.text
        assert r.json()["recorded"] is False

        assert _feedback_rows(c) == []


def test_slashed_model_slug_is_recorded_as_a_categorical_value():
    """Final review #3: a `preferred_model` score names a real model slug,
    and those carry `/`."""
    slug = "nvidia/llama-3.1-nemotron-nano-8b-v1"
    with _client() as c:
        rid = _serve(c, DEMO)["id"]
        r = c.post("/v1/feedback", headers=DEMO,
                   json={"request_id": rid, "score_name": "preferred_model",
                         "value": slug})
        assert r.status_code == 200, r.text
        assert r.json()["recorded"] is True
        rows = _feedback_rows(c)
        assert [(x.value_cat, x.value_num) for x in rows] == [(slug, None)]


# --------------------------------------------------------------------------- #
# 5. neither id given -> 422
# --------------------------------------------------------------------------- #
def test_feedback_neither_id_given_is_422():
    with _client() as c:
        r = c.post("/v1/feedback", headers=DEMO,
                   json={"score_name": "route_correct", "value": 1})
        assert r.status_code == 422, r.text
        assert r.json()["error"]["code"] == "invalid_value"


# --------------------------------------------------------------------------- #
# 6. comment and corrected_output TEXT never land in outcome_feedback
# --------------------------------------------------------------------------- #
def test_comment_and_corrected_output_text_never_stored_locally():
    with _client() as c:
        body = _serve(c, DEMO)
        rid = body["id"]

        secret_comment = "this-comment-must-never-be-stored-locally"
        secret_corrected = "this-corrected-output-must-never-be-stored-locally"

        r = c.post("/v1/feedback", headers=DEMO,
                   json={"request_id": rid, "score_name": "quality", "value": 0.5,
                         "comment": secret_comment,
                         "corrected_output": secret_corrected})
        assert r.status_code == 200, r.text
        assert r.json()["recorded"] is True

        rows = _feedback_rows(c)
        # One row for the primary score, one for corrected_output.
        assert len(rows) == 2
        names = {row.score_name for row in rows}
        assert names == {"quality", "corrected_output"}
        corrected = next(row for row in rows if row.score_name == "corrected_output")
        assert corrected.value_num == 1.0
        assert corrected.value_cat is None

        dumped = json.dumps([{
            "request_id": row.request_id, "application_id": row.application_id,
            "score_name": row.score_name, "value_num": row.value_num,
            "value_cat": row.value_cat, "source": row.source,
        } for row in rows])
        assert secret_comment not in dumped
        assert secret_corrected not in dumped


# --------------------------------------------------------------------------- #
# 7. admin feedback with no trace still records locally, source == "admin"
# --------------------------------------------------------------------------- #
def test_admin_feedback_no_trace_still_records_locally():
    with _client() as c:
        _login(c)
        r = c.post("/admin/applications", json={"id": "fbadmin_notrace",
                                                 "name": "fbadmin_notrace"})
        assert r.status_code == 200, r.text
        token = r.json()["token"]

        body = _serve(c, {"Authorization": f"Bearer {token}"})
        rid = body["id"]
        assert "trace_id" not in body  # no Langfuse project provisioned

        r = c.post(f"/api/requests/{rid}/feedback",
                   json={"score_name": "route_correct", "value": 1})
        assert r.status_code == 200, r.text
        assert r.json() == {"stored": False, "reason": "no trace", "recorded": True}

        rows = _feedback_rows(c)
        assert len(rows) == 1
        assert rows[0].request_id == rid
        assert rows[0].application_id == "fbadmin_notrace"
        assert rows[0].source == "admin"


# --------------------------------------------------------------------------- #
# 8. local recording is gated by outcomes.enabled
# --------------------------------------------------------------------------- #
def test_feedback_recording_disabled_when_outcomes_disabled():
    with _client() as c:
        # Outcomes stay enabled (the default) for the served request, so a
        # routing_outcomes row for it genuinely exists -- this isolates the
        # gate itself from the "no row to find" case tested above.
        body = _serve(c, DEMO)
        rid = body["id"]

        s = c.app.state.settings
        outcomes = s.serving.outcomes.model_copy(update={"enabled": False})
        c.app.state.settings = s.model_copy(update={
            "serving": s.serving.model_copy(update={"outcomes": outcomes})})

        r = c.post("/v1/feedback", headers=DEMO,
                   json={"request_id": rid, "score_name": "route_correct", "value": 1})
        assert r.status_code == 200, r.text
        assert r.json()["recorded"] is False

        assert _feedback_rows(c) == []


# --------------------------------------------------------------------------- #
# Fix round 1: local DB failures must be swallowed -- never a 500, never
# block a Langfuse post that doesn't depend on the failed call.
# --------------------------------------------------------------------------- #
def test_feedback_find_outcome_failure_does_not_block_langfuse_post(monkeypatch):
    with _client() as c:
        token = _make_app_with_langfuse(c, "fbfail1")
        body = _serve(c, {"Authorization": f"Bearer {token}"})
        trace_id = body.get("trace_id")
        assert trace_id, "expected a trace_id once Langfuse is provisioned for the app"

        calls = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append({"url": str(request.url), "body": json.loads(request.content)})
            return httpx.Response(200, json={"id": "score_1"})

        _patch_httpx(monkeypatch, handler)

        async def _boom(*args, **kwargs):
            raise RuntimeError("db down")

        monkeypatch.setattr(store_mod, "find_outcome", _boom)

        r = c.post("/v1/feedback", headers={"Authorization": f"Bearer {token}"},
                   json={"trace_id": trace_id, "score_name": "route_correct", "value": 1})
        assert r.status_code == 200, r.text
        assert r.json()["recorded"] is False
        assert r.json()["stored"] is True  # unchanged vs. a healthy lookup: trace_id
                                            # was given directly, so a failed local
                                            # lookup never touches the Langfuse post.

        score_calls = [call["url"] for call in calls if call["url"].endswith("/api/public/scores")]
        assert len(score_calls) == 1

        assert _feedback_rows(c) == []


def test_feedback_write_feedback_failure_on_corrected_output_does_not_block_second_post(
        monkeypatch):
    with _client() as c:
        token = _make_app_with_langfuse(c, "fbfail2")
        body = _serve(c, {"Authorization": f"Bearer {token}"})
        trace_id = body.get("trace_id")
        assert trace_id

        calls = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append({"url": str(request.url), "body": json.loads(request.content)})
            return httpx.Response(200, json={"id": "score_x"})

        _patch_httpx(monkeypatch, handler)

        orig_write_feedback = store_mod.write_feedback

        async def _boom_on_corrected(*args, **kwargs):
            if kwargs.get("score_name") == "corrected_output":
                raise RuntimeError("db down")
            return await orig_write_feedback(*args, **kwargs)

        monkeypatch.setattr(store_mod, "write_feedback", _boom_on_corrected)

        r = c.post("/v1/feedback", headers={"Authorization": f"Bearer {token}"},
                   json={"trace_id": trace_id, "score_name": "quality", "value": 0.5,
                         "corrected_output": "the fixed answer"})
        assert r.status_code == 200, r.text
        assert r.json()["stored"] is True
        assert r.json()["recorded"] is True  # primary write succeeded

        score_calls = [call["body"] for call in calls if call["url"].endswith("/api/public/scores")]
        assert len(score_calls) == 2
        assert {sc["name"] for sc in score_calls} == {"quality", "corrected_output"}

        # Only the primary score made it to outcome_feedback -- the
        # corrected_output write raised and was swallowed.
        rows = _feedback_rows(c)
        assert len(rows) == 1
        assert rows[0].score_name == "quality"


def test_admin_feedback_find_outcome_failure_does_not_block_langfuse_post(monkeypatch):
    with _client() as c:
        token = _make_app_with_langfuse(c, "fbadminfail1")
        body = _serve(c, {"Authorization": f"Bearer {token}"})
        rid = body["id"]
        assert body.get("trace_id")

        calls = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append({"url": str(request.url), "body": json.loads(request.content)})
            return httpx.Response(200, json={"id": "score_1"})

        _patch_httpx(monkeypatch, handler)

        async def _boom(*args, **kwargs):
            raise RuntimeError("db down")

        monkeypatch.setattr(store_mod, "find_outcome", _boom)

        r = c.post(f"/api/requests/{rid}/feedback",
                   json={"score_name": "route_correct", "value": 1})
        assert r.status_code == 200, r.text
        assert r.json()["recorded"] is False
        assert r.json()["stored"] is True

        score_calls = [call["url"] for call in calls if call["url"].endswith("/api/public/scores")]
        assert len(score_calls) == 1

        assert _feedback_rows(c) == []


def test_admin_feedback_write_feedback_failure_on_corrected_output_does_not_block_second_post(
        monkeypatch):
    with _client() as c:
        token = _make_app_with_langfuse(c, "fbadminfail2")
        body = _serve(c, {"Authorization": f"Bearer {token}"})
        rid = body["id"]
        assert body.get("trace_id")

        calls = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append({"url": str(request.url), "body": json.loads(request.content)})
            return httpx.Response(200, json={"id": "score_x"})

        _patch_httpx(monkeypatch, handler)

        orig_write_feedback = store_mod.write_feedback

        async def _boom_on_corrected(*args, **kwargs):
            if kwargs.get("score_name") == "corrected_output":
                raise RuntimeError("db down")
            return await orig_write_feedback(*args, **kwargs)

        monkeypatch.setattr(store_mod, "write_feedback", _boom_on_corrected)

        r = c.post(f"/api/requests/{rid}/feedback",
                   json={"score_name": "quality", "value": 0.5,
                         "corrected_output": "the fixed answer"})
        assert r.status_code == 200, r.text
        assert r.json()["stored"] is True
        assert r.json()["recorded"] is True

        score_calls = [call["body"] for call in calls if call["url"].endswith("/api/public/scores")]
        assert len(score_calls) == 2
        assert {sc["name"] for sc in score_calls} == {"quality", "corrected_output"}

        rows = _feedback_rows(c)
        assert len(rows) == 1
        assert rows[0].score_name == "quality"
        assert rows[0].source == "admin"
