"""P3b Task 4: the minimal conflicting core on every surface a `no_eligible`
request reaches -- the live 422 body (`error.conflicting_core`), the
`routing_outcomes.conflicting_core` column (and so the NDJSON export), and the
structured `serving.api` log line for the failed request (a `no_eligible`
request exports no Langfuse trace: `export_trace` runs only in `_finalize`,
on a served response).

Driven through the real HTTP boundary (`create_app()` + `TestClient`), with
the seeded apps/models and the onboarding harness, like
tests/test_outcome_hooks.py. Everything checked here must be content-free
(spec §5.12): a marker in the prompt never reaches any of the three surfaces.
"""
from __future__ import annotations

import asyncio
import json
import logging
from contextlib import contextmanager

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.apps.models import RoutingOutcome
from app.main import create_app
from tests.conftest import v1_error
from tests.onboarding.harness import disable_seeded_models, onboard

HELLO = {"messages": [{"role": "user", "content": "hello"}]}
MARKER = "ZZMARKER-7f3a"


@contextmanager
def _client():
    with TestClient(create_app(), raise_server_exceptions=False) as c:
        yield c


def _with_azure_enabled(monkeypatch) -> None:
    """Boot with the Azure provider on, so the static cloud pool has a
    candidate for the residency filter to reject under `onprem_only`."""
    import app.main as main_mod
    from app.config.loader import load_settings

    def _load():
        s = load_settings()
        providers = dict(s.serving.providers)
        providers["azure"] = providers["azure"].model_copy(update={"enabled": True})
        return s.model_copy(update={
            "serving": s.serving.model_copy(update={"providers": providers})})

    monkeypatch.setattr(main_mod, "load_settings", _load)


def _outcomes(c: TestClient) -> list[RoutingOutcome]:
    async def go():
        async with c.app.state.db.session() as session:
            return list((await session.execute(select(RoutingOutcome))).scalars())
    return c.portal.call(go)


def _only(c: TestClient) -> RoutingOutcome:
    rows = _outcomes(c)
    assert len(rows) == 1, [(r.status, r.request_id) for r in rows]
    return rows[0]


def _ids(core: dict) -> list[str]:
    return [r["id"] for r in core["requirements"]]


def _state_ids(core: dict) -> list[str]:
    return [s["id"] for s in core["state"]]


def _core_log_lines(caplog) -> list[dict]:
    out = []
    for rec in caplog.records:
        if rec.name != "serving.api":
            continue
        try:
            obj = json.loads(rec.getMessage())
        except (ValueError, TypeError):
            continue
        if isinstance(obj, dict) and obj.get("event") == "no_eligible_model":
            out.append(obj)
    return out


def _no_candidate_app(c: TestClient, app_id: str):
    """An `onprem_only` app whose only on-prem models are disabled, with a
    cloud model available: the core is `residency`, the state `disabled`."""
    disable_seeded_models(c)
    return onboard(c, app_id, residency="onprem_only")


# --------------------------------------------------------------------------- #
# 1-2. the 422 body carries the core; the B49 envelope is intact
# --------------------------------------------------------------------------- #
def test_422_body_carries_the_conflicting_core(monkeypatch):
    _with_azure_enabled(monkeypatch)
    with _client() as c:
        app = _no_candidate_app(c, "cc-disabled")
        r = app.chat(HELLO)
        assert r.status_code == 422, r.text
        err = v1_error(r)
        assert err["code"] == "no_eligible_model"
        core = err["conflicting_core"]
        assert "residency" in _ids(core)
        assert "disabled" in _state_ids(core)
        # The client-facing shape (spec §3) keeps the label and hints.
        for req in core["requirements"]:
            assert {"id", "security", "source", "detail", "label", "hints"} <= set(req)
        assert isinstance(core["candidates_considered"], int)


def test_422_envelope_keeps_every_b49_field(monkeypatch):
    _with_azure_enabled(monkeypatch)
    with _client() as c:
        app = _no_candidate_app(c, "cc-envelope")
        err = v1_error(app.chat(HELLO))
        assert isinstance(err["message"], str) and err["message"]
        assert err["type"] == "invalid_request_error"
        assert "param" in err and err["param"] is None
        assert err["code"] == "no_eligible_model"


def test_pinned_model_rejection_carries_no_core(monkeypatch):
    """A client pin the app's placement forbids is refused by the pipeline's
    pinned-model check (`model_not_found`) before the router runs -- not a
    `no_eligible_model` rejection, so no core is attached."""
    _with_azure_enabled(monkeypatch)
    with _client() as c:
        app = onboard(c, "cc-pinned", residency="onprem_only")
        r = app.chat({**HELLO, "model": "azure-gpt-mid"})
        assert r.status_code == 422, r.text
        err = v1_error(r)
        assert err["code"] == "model_not_found"
        assert "conflicting_core" not in err


# --------------------------------------------------------------------------- #
# 3-4. the outcome row and the export line
# --------------------------------------------------------------------------- #
def test_outcome_row_records_the_core(monkeypatch):
    _with_azure_enabled(monkeypatch)
    with _client() as c:
        app = _no_candidate_app(c, "cc-row")
        body_core = v1_error(app.chat(HELLO))["conflicting_core"]
        row = _only(c)
        assert row.status == "no_eligible"
        core = row.conflicting_core
        assert isinstance(core, dict)
        assert _ids(core) == _ids(body_core)
        assert _state_ids(core) == _state_ids(body_core)
        # The record shape drops the client-facing label and hints.
        for req in core["requirements"]:
            assert "label" not in req and "hints" not in req


def test_ok_outcome_row_has_null_core():
    with _client() as c:
        r = c.post("/v1/chat/completions",
                   headers={"Authorization": "Bearer demo-key"}, json=HELLO)
        assert r.status_code == 200, r.text
        assert _only(c).conflicting_core is None


def test_export_line_includes_the_core(monkeypatch):
    _with_azure_enabled(monkeypatch)
    with _client() as c:
        app = _no_candidate_app(c, "cc-export")
        assert app.chat(HELLO).status_code == 422
        r = c.get("/admin/outcomes/export")   # onboard() logged the admin in
        assert r.status_code == 200, r.text
        lines = [json.loads(ln) for ln in r.text.strip().splitlines() if ln]
        mine = [ln for ln in lines if ln["outcome"]["application_id"] == "cc-export"]
        assert len(mine) == 1
        core = mine[0]["outcome"]["conflicting_core"]
        assert "residency" in _ids(core)


# --------------------------------------------------------------------------- #
# the trace surface: a structured log line (no trace is exported on a 422)
# --------------------------------------------------------------------------- #
def test_failed_request_log_line_carries_the_core(monkeypatch, caplog):
    _with_azure_enabled(monkeypatch)
    caplog.set_level(logging.INFO, logger="serving.api")
    with _client() as c:
        app = _no_candidate_app(c, "cc-log")
        r = app.chat(HELLO)
        assert r.status_code == 422
        lines = _core_log_lines(caplog)
        assert len(lines) == 1, lines
        assert lines[0]["request_id"] == _only(c).request_id
        core = lines[0]["conflicting_core"]
        assert "residency" in _ids(core)
        for req in core["requirements"]:
            assert "label" not in req and "hints" not in req


# --------------------------------------------------------------------------- #
# the keepalive stream's SSE error frame carries the core too
# --------------------------------------------------------------------------- #
def test_keepalive_stream_error_frame_carries_the_core(monkeypatch):
    _with_azure_enabled(monkeypatch)
    with _client() as c:
        app = _no_candidate_app(c, "cc-stream")
        s = c.app.state.settings
        c.app.state.settings = s.model_copy(update={
            "serving": s.serving.model_copy(update={"stream_keepalive_s": 0.05})})
        pipeline = c.app.state.pipeline
        real_run = pipeline.run

        async def slow_run(*args, **kwargs):
            await asyncio.sleep(0.15)   # outlast one keepalive: the SSE response opens
            return await real_run(*args, **kwargs)

        pipeline.run = slow_run
        from tests.onboarding.harness import set_policy_direct
        set_policy_direct(c, "cc-stream", stream_mode="verified_then_stream")
        with c.stream("POST", "/v1/chat/completions", headers=app.headers,
                      json={**HELLO, "stream": True}) as r:
            assert r.status_code == 200
            text = "".join(r.iter_text())
        assert text.startswith(": keepalive")
        frames = [json.loads(ln[len("data: "):]) for ln in text.splitlines()
                  if ln.startswith("data: {")]
        errors = [f["error"] for f in frames if "error" in f]
        assert len(errors) == 1, text
        assert errors[0]["code"] == "no_eligible_model"
        assert "residency" in _ids(errors[0]["conflicting_core"])
        assert _only(c).conflicting_core is not None


# --------------------------------------------------------------------------- #
# 5. content-free (spec §5.12): the prompt never reaches any surface
# --------------------------------------------------------------------------- #
def test_conflicting_core_is_content_free(monkeypatch, caplog):
    _with_azure_enabled(monkeypatch)
    caplog.set_level(logging.INFO, logger="serving.api")
    with _client() as c:
        app = _no_candidate_app(c, "cc-marker")
        r = app.chat({"messages": [
            {"role": "user", "content": f"please answer {MARKER} now"}]})
        assert r.status_code == 422, r.text
        body_core = v1_error(r)["conflicting_core"]
        assert MARKER not in json.dumps(body_core)
        row = _only(c)
        assert row.conflicting_core is not None
        assert MARKER not in json.dumps(row.conflicting_core)
        lines = _core_log_lines(caplog)
        assert lines
        assert MARKER not in json.dumps(lines)
