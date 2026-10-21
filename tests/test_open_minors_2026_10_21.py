"""Three minors left open by B75/B78 (2026-10-21): a cancelled request's
outcome row names the endpoint that was in flight, the Overview's fleet cache
is bounded, and Langfuse secrets stored in cleartext get encrypted at boot."""
from __future__ import annotations

import asyncio

from sqlalchemy import select

from app.apps.models import Application, RoutingOutcome
from app.apps.privacy import AppPrivacyProfile
from app.apps.policy import AppPolicyView
from app.api import routes
from app.models import ChatRequest, Message
from tests.test_error_metering import _client, _failing_backend


def test_cancelled_mid_call_outcome_names_the_in_flight_endpoint(monkeypatch):
    attempted = _failing_backend(monkeypatch, delay_s=10.0)
    monkeypatch.setattr(routes, "export_failure_trace", lambda **kw: None)
    with _client() as c:
        async def go():
            await c.app.state.registry.ensure_loaded(c.app.state.db)
            internals: dict = {}
            body = ChatRequest(model="auto", messages=[Message(role="user", content="hi")])
            task = asyncio.create_task(c.app.state.pipeline.run(
                "req-inflight", "default", body, app_policy=AppPolicyView(),
                profile=AppPrivacyProfile(recognizers=(), rules=()),
                internals=internals))
            for _ in range(200):           # wait until the backend call starts
                if attempted:
                    break
                await asyncio.sleep(0.01)
            task.cancel()
            done = await asyncio.gather(task, return_exceptions=True)
            assert attempted, f"backend call never started: {done!r}"
            await routes._record_cancelled(
                _Req(c.app), tenant="demo", request_id="req-inflight",
                started=0.0, s=c.app.state.settings, internals=internals)
            async with c.app.state.db.session() as session:
                return list((await session.execute(select(RoutingOutcome))).scalars())
        rows = c.portal.call(go)
    assert attempted, "backend call never started"
    assert [r.status for r in rows] == ["cancelled"]
    assert rows[0].attempts == attempted[:1]


class _Req:
    def __init__(self, app):
        self.app = app


def test_fleet_cache_is_bounded(client, monkeypatch):
    from app.api import ui_routes
    from tests.test_ui_routes import _login
    _login(client)
    monkeypatch.setattr(ui_routes, "_fleet_cache", {})
    monkeypatch.setattr(ui_routes, "_FLEET_CACHE_TTL_S", 10.0)
    for h in range(1, 3 * ui_routes._FLEET_CACHE_MAX):
        assert client.get(f"/api/fleet?hours={h}").status_code == 200
    assert len(ui_routes._fleet_cache) <= ui_routes._FLEET_CACHE_MAX


def _app_row(app_id: str, blob: bytes | None) -> Application:
    return Application(id=app_id, name=app_id, langfuse_project_id="p",
                       langfuse_public_key="pk-lf-x", langfuse_secret_key_enc=blob)


def test_boot_encrypts_cleartext_langfuse_secrets(monkeypatch):
    monkeypatch.setenv("ENCRYPTION_KEY", "a" * 64)
    from app.apps.langfuse_provision import (decrypt_secret, encrypt_plaintext_secrets,
                                             encrypt_secret)
    from app.apps.db import Database

    async def go():
        db = Database("sqlite+aiosqlite:///:memory:")
        async with db.session() as s:
            s.add_all([_app_row("plain", b"sk-lf-cleartext"),
                       _app_row("enc", encrypt_secret("sk-lf-already")),
                       _app_row("none", None)])
            await s.commit()
        async with db.session() as s:
            assert await encrypt_plaintext_secrets(s) == 1
            await s.commit()
        async with db.session() as s:
            assert await encrypt_plaintext_secrets(s) == 0   # idempotent
            rows = {a.id: a.langfuse_secret_key_enc
                    for a in (await s.execute(select(Application))).scalars()}
        await db.engine.dispose()
        return rows
    rows = asyncio.run(go())
    assert not bytes(rows["plain"]).startswith(b"sk-lf-")
    assert decrypt_secret(rows["plain"]) == "sk-lf-cleartext"
    assert decrypt_secret(rows["enc"]) == "sk-lf-already"
    assert rows["none"] is None


def test_needs_encryption_leaves_foreign_fernet_tokens_alone(monkeypatch):
    from app.apps import langfuse_provision as lp
    monkeypatch.setenv("ENCRYPTION_KEY", "b" * 64)
    other = lp.encrypt_secret("sk-lf-x")
    monkeypatch.setenv("ENCRYPTION_KEY", "c" * 64)   # key changed since
    assert lp.needs_encryption(other) is False
    assert lp.needs_encryption(b"sk-lf-plain") is True
    monkeypatch.delenv("ENCRYPTION_KEY")
    assert lp.needs_encryption(b"sk-lf-plain") is False   # nothing to encrypt with


def test_boot_warns_when_secrets_exist_but_no_key(monkeypatch, caplog):
    monkeypatch.delenv("ENCRYPTION_KEY", raising=False)
    from app.apps.langfuse_provision import encrypt_plaintext_secrets
    from app.apps.db import Database

    async def go():
        db = Database("sqlite+aiosqlite:///:memory:")
        async with db.session() as s:
            s.add(_app_row("plain", b"sk-lf-cleartext"))
            await s.commit()
        async with db.session() as s:
            n = await encrypt_plaintext_secrets(s)
        await db.engine.dispose()
        return n
    with caplog.at_level("WARNING", logger="serving.obs"):
        assert asyncio.run(go()) == 0
    assert "ENCRYPTION_KEY is unset" in caplog.text
    assert "sk-lf-cleartext" not in caplog.text


def test_compose_files_pass_encryption_key_to_the_api():
    import yaml
    for path in ("deploy/spark/docker-compose.yml", "deploy/compose/docker-compose.yml"):
        env = yaml.safe_load(open(path))["services"]["api"]["environment"]
        assert "ENCRYPTION_KEY" in env, path
