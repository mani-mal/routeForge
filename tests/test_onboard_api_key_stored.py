"""B68: an onboarded endpoint's API key given at onboarding time -- stored
encrypted (ENCRYPTION_KEY), write-only, used at once, picked up by other
replicas on their next registry refresh, and never leaked.

The probe is the REAL `probe_endpoint` against an `httpx.MockTransport`
upstream that accepts exactly one bearer token, so "re-probe with the new
credentials" is exercised end to end, not stubbed.
"""
from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
from contextlib import contextmanager
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.apps.models import OnboardedModel
from app.apps.registry import NO_AUTH_KEY, ModelRegistry
from app.gateway.backends import litellm_backend
from app.main import create_app
from tests._litellm_fake import CALLS, make_response
from tests.onboarding.harness import disable_seeded_models
from tests.test_onboard_api import _login
from tests.test_outcome_hooks import DEMO, HELLO, _columns, _outcomes

KEY = "sk-live-B68-SECRET-0123456789abcd"      # >= 12 chars -> hint "abcd"
KEY2 = "sk-live-B68-ROTATED-zyxw98765"           # hint "8765"
SHORT = "short-key9"                             # < 12 chars -> no hint
SLUG = "keyed-cheap"
_REAL_CLIENT = httpx.AsyncClient   # captured before any test patches it
BODY = {"slug": SLUG, "display_name": "Keyed", "tier": "cheap",
        "endpoint": "http://keyed/v1", "price_input": 0.01, "price_output": 0.01}


@pytest.fixture(autouse=True)
def _enc_key(monkeypatch):
    monkeypatch.setenv("ENCRYPTION_KEY", "a" * 64)
    CALLS.clear()
    yield
    CALLS.clear()


@contextmanager
def _client():
    with TestClient(create_app(), raise_server_exceptions=False) as c:
        _login(c)
        yield c


class _Upstream:
    """A fake OpenAI-compatible `/v1/models` that serves SLUG only to
    `Authorization: Bearer <accepted>`; anything else is 401."""

    def __init__(self, monkeypatch, accepted: str | None) -> None:
        self.accepted = accepted
        self.seen: list[str | None] = []
        transport = httpx.MockTransport(self._handle)
        monkeypatch.setattr("app.apps.probe.httpx.AsyncClient",
                            lambda **kw: _REAL_CLIENT(transport=transport, **kw))

    def _handle(self, request: httpx.Request) -> httpx.Response:
        auth = request.headers.get("authorization")
        self.seen.append(auth)
        if self.accepted is not None and auth != f"Bearer {self.accepted}":
            return httpx.Response(401, json={"error": "unauthorized"})
        return httpx.Response(200, json={"data": [{"id": SLUG}]})


def _db_row(c) -> OnboardedModel:
    async def go():
        async with c.app.state.db.session() as s:
            return (await s.execute(select(OnboardedModel).where(
                OnboardedModel.slug == SLUG))).scalar_one()
    return c.portal.call(go)


def _listed(c) -> dict:
    r = c.get("/admin/infra/models")
    assert r.status_code == 200
    return next(m for m in r.json() if m["slug"] == SLUG)


def _no_key_in(text: str, *keys: str) -> None:
    for k in keys:
        assert k not in text


# --- store + read -----------------------------------------------------------

def test_onboard_with_api_key_stores_ciphertext_and_reads_write_only(monkeypatch):
    up = _Upstream(monkeypatch, accepted=KEY)
    with _client() as c:
        r = c.post("/admin/infra/models", json={**BODY, "api_key": KEY})
        assert r.status_code == 200, r.text
        assert r.json()["probe_status"] == "healthy" and r.json()["enabled"] is True
        assert r.json()["api_key_source"] == "stored"
        assert up.seen == [f"Bearer {KEY}"]          # probed WITH the given key
        row = _db_row(c)
        assert row.api_key_enc and row.api_key_enc != KEY and KEY not in row.api_key_enc
        assert row.api_key_env is None
        listed = _listed(c)
        assert listed["api_key_source"] == "stored"
        assert listed["api_key_hint"] == "abcd"
        assert listed["api_key_env"] is None
        blob = c.get("/admin/infra/models").text + r.text
        _no_key_in(blob, KEY, row.api_key_enc)
        assert "api_key_enc" not in blob


def test_short_key_gets_no_hint(monkeypatch):
    _Upstream(monkeypatch, accepted=SHORT)
    with _client() as c:
        assert c.post("/admin/infra/models",
                      json={**BODY, "api_key": SHORT}).status_code == 200
        listed = _listed(c)
        assert listed["api_key_source"] == "stored" and listed["api_key_hint"] is None


def test_gateway_sends_the_stored_key(monkeypatch):
    _Upstream(monkeypatch, accepted=KEY)

    async def _call(**kwargs):
        CALLS.append(kwargs)
        return make_response(content="ok")
    monkeypatch.setattr(litellm_backend, "acompletion", _call)
    with _client() as c:
        assert c.post("/admin/infra/models", json={**BODY, "api_key": KEY}).status_code == 200
        disable_seeded_models(c)
        r = c.post("/v1/chat/completions", headers=DEMO, json=HELLO)
        assert r.status_code == 200, r.text
        assert CALLS and CALLS[-1]["model"] == f"openai/{SLUG}"
        assert CALLS[-1]["api_key"] == KEY
        assert CALLS[-1]["api_base"] == "http://keyed/v1"


# --- validation -------------------------------------------------------------

@pytest.mark.parametrize("extra,needle", [
    ({"api_key": KEY, "api_key_env": "SOME_ENV"}, "not both"),
    ({"api_key": ""}, "must not be empty"),
    ({"api_key": "   \t "}, "must not be empty"),
])
def test_bad_key_bodies_are_422_and_store_nothing(monkeypatch, extra, needle):
    _Upstream(monkeypatch, accepted=None)
    with _client() as c:
        r = c.post("/admin/infra/models", json={**BODY, **extra})
        assert r.status_code == 422 and needle in r.json()["detail"]
        r = c.put(f"/admin/infra/models/{SLUG}", json={**BODY, **extra})
        assert r.status_code == 422
        assert not any(m["slug"] == SLUG for m in c.get("/admin/infra/models").json())


def test_no_encryption_key_refuses_and_stores_nothing(monkeypatch):
    monkeypatch.delenv("ENCRYPTION_KEY", raising=False)
    up = _Upstream(monkeypatch, accepted=None)
    with _client() as c:
        r = c.post("/admin/infra/models", json={**BODY, "api_key": KEY})
        assert r.status_code == 422
        assert r.json()["detail"] == (
            "set ENCRYPTION_KEY to store endpoint keys, or use api_key_env")
        assert up.seen == []                       # never probed either
        assert not any(m["slug"] == SLUG for m in c.get("/admin/infra/models").json())
        _no_key_in(r.text, KEY)


def test_validation_error_does_not_echo_the_key(monkeypatch):
    _Upstream(monkeypatch, accepted=None)
    with _client() as c:
        body = {k: v for k, v in BODY.items() if k != "tier"}    # missing field
        r = c.post("/admin/infra/models", json={**body, "api_key": KEY})
        assert r.status_code == 422
        _no_key_in(r.text, KEY)


# --- edit: switch modes, clear, preserve ------------------------------------

def test_edit_switches_env_to_stored_and_back_and_clears(monkeypatch):
    monkeypatch.setenv("RF_B68_ENV", KEY)
    up = _Upstream(monkeypatch, accepted=KEY)
    with _client() as c:
        assert c.post("/admin/infra/models",
                      json={**BODY, "api_key_env": "RF_B68_ENV"}).status_code == 200
        assert _listed(c)["api_key_source"] == "env"

        # env -> stored: one call; api_key_env cleared
        r = c.put(f"/admin/infra/models/{SLUG}", json={**BODY, "api_key": KEY})
        assert r.status_code == 200 and r.json()["probe_status"] == "healthy"
        row = _db_row(c)
        assert row.api_key_enc and row.api_key_env is None
        assert _listed(c)["api_key_source"] == "stored"

        # omitted api_key on an unrelated edit PRESERVES the stored key and
        # re-probes with it
        up.seen.clear()
        r = c.put(f"/admin/infra/models/{SLUG}", json={**BODY, "display_name": "K2"})
        assert r.status_code == 200 and r.json()["probe_status"] == "healthy"
        assert up.seen == [f"Bearer {KEY}"]
        assert _db_row(c).api_key_enc == row.api_key_enc

        # stored -> env: stored key cleared
        r = c.put(f"/admin/infra/models/{SLUG}", json={**BODY, "api_key_env": "RF_B68_ENV"})
        assert r.status_code == 200
        row = _db_row(c)
        assert row.api_key_enc is None and row.api_key_env == "RF_B68_ENV"
        assert _listed(c)["api_key_source"] == "env"
        assert _listed(c)["api_key_hint"] is None

        # back to stored, then an explicit null clears it
        assert c.put(f"/admin/infra/models/{SLUG}",
                     json={**BODY, "api_key": KEY}).status_code == 200
        r = c.put(f"/admin/infra/models/{SLUG}", json={**BODY, "api_key": None})
        assert r.status_code == 200
        assert r.json()["probe_status"] == "unauthorized"     # no key any more
        row = _db_row(c)
        assert row.api_key_enc is None and row.api_key_env is None
        assert _listed(c)["api_key_source"] is None


# --- rotation across replicas -----------------------------------------------

def test_rotation_reprobes_and_other_replica_picks_it_up_on_refresh(monkeypatch):
    up = _Upstream(monkeypatch, accepted=KEY)
    with _client() as c:
        assert c.post("/admin/infra/models", json={**BODY, "api_key": KEY}).status_code == 200
        other = ModelRegistry(c.app.state.settings)          # a second replica
        c.portal.call(other.refresh, c.app.state.db)
        assert other.api_key_for(SLUG) == KEY

        up.accepted = KEY2
        up.seen.clear()
        r = c.put(f"/admin/infra/models/{SLUG}", json={**BODY, "api_key": KEY2})
        assert r.status_code == 200 and r.json()["probe_status"] == "healthy"
        assert up.seen == [f"Bearer {KEY2}"]                   # re-probed, new key
        assert c.app.state.registry.api_key_for(SLUG) == KEY2  # this replica: at once
        assert other.api_key_for(SLUG) == KEY                  # other: until it reloads
        c.portal.call(other.refresh, c.app.state.db)
        assert other.api_key_for(SLUG) == KEY2                 # no restart
        assert _listed(c)["api_key_hint"] == "8765"


def test_quality_loop_also_reloads_the_registry(monkeypatch):
    """The periodic loop is what carries another replica's write here."""
    import app.main as main

    refreshed: list[str] = []
    stop = asyncio.Event()

    class _Reg:
        async def refresh(self, db):
            refreshed.append("registry")

    async def _quality(app):
        refreshed.append("quality")
        stop.set()
        return True

    monkeypatch.setattr(main, "refresh_quality", _quality)
    app = SimpleNamespace(state=SimpleNamespace(
        quality_stop=stop, registry=_Reg(), db=object(),
        settings=SimpleNamespace(routing=SimpleNamespace(quality_refresh_s=0.01))))
    asyncio.run(main._quality_loop(app))
    assert refreshed == ["registry", "quality"]


# --- undecryptable ciphertext -----------------------------------------------

def test_undecryptable_key_is_no_key_warns_without_value_and_probes_unauthorized(
        monkeypatch, caplog):
    up = _Upstream(monkeypatch, accepted=KEY)
    with _client() as c:
        assert c.post("/admin/infra/models", json={**BODY, "api_key": KEY}).status_code == 200
        monkeypatch.setenv("ENCRYPTION_KEY", "b" * 64)      # key rotated away
        caplog.set_level(logging.WARNING)
        reg = ModelRegistry(c.app.state.settings)
        c.portal.call(reg.refresh, c.app.state.db)
        assert reg.api_key_for(SLUG) == NO_AUTH_KEY           # never the ciphertext
        assert any(SLUG in rec.getMessage() and "cannot be decrypted" in rec.getMessage()
                   for rec in caplog.records)
        c.portal.call(reg.refresh, c.app.state.db)            # warned once, not per refresh
        assert sum("cannot be decrypted" in rec.getMessage()
                   for rec in caplog.records) == 1
        listed = _listed(c)
        assert listed["api_key_source"] == "stored" and listed["api_key_hint"] is None

        up.seen.clear()
        r = c.put(f"/admin/infra/models/{SLUG}", json=BODY)   # preserve -> re-probe
        assert r.status_code == 200
        assert up.seen == [None]                              # no Authorization sent
        assert r.json()["probe_status"] == "unauthorized" and r.json()["enabled"] is False
        _no_key_in(caplog.text, KEY)


# --- leak tests -------------------------------------------------------------

def test_repr_of_registry_row_hides_the_key(monkeypatch):
    _Upstream(monkeypatch, accepted=KEY)
    with _client() as c:
        assert c.post("/admin/infra/models", json={**BODY, "api_key": KEY}).status_code == 200
        row = c.app.state.registry._onboarded()[SLUG]
        _no_key_in(repr(row) + str(row) + str(dataclasses.asdict(row)), KEY)
        _no_key_in(repr(_db_row(c)), KEY)


def test_key_never_in_logs_snapshot_traces_or_outcomes(monkeypatch, caplog):
    pytest.importorskip("opentelemetry.sdk.trace")
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
    import app.obs.otel as otel

    mem = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(otel.RedactingSpanExporter(mem)))
    monkeypatch.setattr(otel, "_provider", provider)
    caplog.set_level(logging.DEBUG)

    up = _Upstream(monkeypatch, accepted=KEY)
    fail = {"on": False}

    async def _call(**kwargs):
        CALLS.append(kwargs)
        if fail["on"]:
            # An upstream whose error text echoes the request kwargs (key and all).
            # Key first, so the 200-char truncation cannot hide a leak.
            raise RuntimeError(f"key {kwargs['api_key']} rejected for request {kwargs!r}")
        return make_response(content="ok")
    monkeypatch.setattr(litellm_backend, "acompletion", _call)

    with _client() as c:
        # onboard + probe (success), then a probe failure whose text echoes the key
        r1 = c.post("/admin/infra/models", json={**BODY, "api_key": KEY})
        assert r1.status_code == 200

        def _boom(request):
            raise httpx.ConnectError(f"refused {request.headers.get('authorization')}")
        monkeypatch.setattr(
            "app.apps.probe.httpx.AsyncClient",
            lambda **kw: _REAL_CLIENT(transport=httpx.MockTransport(_boom), **kw))
        r2 = c.put(f"/admin/infra/models/{SLUG}", json={**BODY, "api_key": KEY})
        assert r2.json()["probe_status"] == "unreachable"
        assert r2.json()["probe_error"]
        _Upstream(monkeypatch, accepted=KEY)
        assert c.put(f"/admin/infra/models/{SLUG}", json=BODY).json()["enabled"] is True

        disable_seeded_models(c)
        ok = c.post("/v1/chat/completions", headers=DEMO, json=HELLO)
        assert ok.status_code == 200, ok.text
        fail["on"] = True
        bad = c.post("/v1/chat/completions", headers=DEMO, json=HELLO)
        assert bad.status_code >= 500
        assert CALLS[-1]["api_key"] == KEY            # the key WAS sent ...

        snapshot = c.get("/admin/config-snapshot")
        assert snapshot.status_code == 200
        listing = c.get("/admin/infra/models").text
        outcomes = _outcomes(c)
        assert len(outcomes) >= 2
        provider.force_flush()
        spans = "".join(s.name + json.dumps(dict(s.attributes), default=str)
                        for s in mem.get_finished_spans())
        enc = _db_row(c).api_key_enc

        # ... and it is nowhere else. Logs are checked at DEBUG for the
        # plaintext; the ciphertext does appear there as a bound SQL parameter
        # in aiosqlite's own DEBUG lines, which is a ciphertext, not the key.
        _no_key_in(caplog.text, KEY)
        for surface in (r1.text, r2.text, ok.text, bad.text,
                        snapshot.text, listing, spans,
                        json.dumps([_columns(o) for o in outcomes], default=str)):
            _no_key_in(surface, KEY, enc)
        assert "api_key" not in snapshot.text
        assert "<redacted-key>" in r2.json()["probe_error"]
        assert "key <redacted-key> rejected" in caplog.text     # scrubbed, not dropped
