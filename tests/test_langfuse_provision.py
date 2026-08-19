"""Per-app Langfuse provisioning + secret encryption (Phase 5).

Provisioning against a live Langfuse needs an org key + a running instance, so
here we test the offline-verifiable parts: encryption round-trip, the org-API
request flow (via httpx MockTransport), the deep-link builder, and the loader.
"""
import asyncio
from types import SimpleNamespace

import httpx

from app.apps import langfuse_provision as lp
from app.obs import otel


def test_encrypt_decrypt_round_trip(monkeypatch):
    monkeypatch.setenv("ENCRYPTION_KEY", "a" * 64)  # 64 hex -> 32 bytes
    blob = lp.encrypt_secret("sk-lf-supersecret")
    assert blob != b"sk-lf-supersecret"           # actually encrypted
    assert lp.decrypt_secret(blob) == "sk-lf-supersecret"


def test_provisioner_not_configured_returns_none(monkeypatch):
    monkeypatch.delenv("LANGFUSE_ORG_PUBLIC_KEY", raising=False)
    monkeypatch.delenv("LANGFUSE_ORG_SECRET_KEY", raising=False)
    prov = lp.LangfuseProvisioner(host="http://lf", org_public=None, org_secret=None)
    assert prov.is_configured() is False
    assert asyncio.run(prov.provision("acme", "Acme")) is None


def test_provisioner_creates_project_and_keys():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path.endswith("/api/public/projects"):
            return httpx.Response(200, json={"id": "proj-123"})
        if request.url.path.endswith("/apiKeys"):
            return httpx.Response(201, json={"publicKey": "pk-x", "secretKey": "sk-x"})
        return httpx.Response(404)

    async def go():
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        try:
            prov = lp.LangfuseProvisioner(
                host="http://lf", org_public="opk", org_secret="osk", client=client)
            return await prov.provision("acme", "Acme Corp")
        finally:
            await client.aclose()

    result = asyncio.run(go())
    assert result is not None
    assert result.project_id == "proj-123"
    assert result.public_key == "pk-x" and result.secret_key == "sk-x"
    assert any(p.endswith("/api/public/projects") for p in calls)
    assert any(p.endswith("/apiKeys") for p in calls)


def test_app_trace_url_uses_app_project(monkeypatch):
    monkeypatch.setattr(otel, "_deeplink_host", "http://lf-public:30300")
    url = otel.app_trace_url("proj-123", 0x1a2b)
    assert url == "http://lf-public:30300/project/proj-123/traces/" + f"{0x1a2b:032x}"
    # no host configured -> None (UI hides the link)
    monkeypatch.setattr(otel, "_deeplink_host", None)
    assert otel.app_trace_url("proj-123", 1) is None


def test_load_app_langfuse(monkeypatch):
    monkeypatch.setenv("ENCRYPTION_KEY", "b" * 64)
    monkeypatch.setenv("LANGFUSE_HOST", "http://langfuse-web:3000")
    enc = lp.encrypt_secret("sk-app")
    app = SimpleNamespace(langfuse_project_id="proj-9",
                          langfuse_public_key="pk-9",
                          langfuse_secret_key_enc=enc)
    got = lp.load_app_langfuse(app)
    assert got is not None
    assert got.project_id == "proj-9" and got.secret_key == "sk-app"
    assert got.host == "http://langfuse-web:3000"

    # an app with no provisioned project -> None
    bare = SimpleNamespace(langfuse_project_id=None, langfuse_public_key=None,
                           langfuse_secret_key_enc=None)
    assert lp.load_app_langfuse(bare) is None


def test_load_app_langfuse_prefers_per_app_host(monkeypatch):
    from types import SimpleNamespace
    from app.apps.langfuse_provision import load_app_langfuse, encrypt_secret
    monkeypatch.setenv("LANGFUSE_HOST", "http://env-host:3000")
    monkeypatch.delenv("ENCRYPTION_KEY", raising=False)
    app = SimpleNamespace(
        langfuse_project_id="proj_x", langfuse_public_key="pk_x",
        langfuse_secret_key_enc=encrypt_secret("sk_x"),
        langfuse_host="http://airgap-host:3000",
    )
    creds = load_app_langfuse(app)
    assert creds is not None
    assert creds.host == "http://airgap-host:3000"   # per-app wins over env
    assert creds.project_id == "proj_x"
