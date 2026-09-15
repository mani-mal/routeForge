"""Onboard an application the way an operator does (admin API: create app and
token), then shape its policy and model pool for a conformance case."""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass

from fastapi.testclient import TestClient

from app.apps import store
from app.gateway.backends import litellm_backend
from app.main import create_app
from tests._litellm_fake import CALLS, make_response, _FakeStreamChunk
from tests.test_admin_api import _login


@dataclass
class OnboardedApp:
    client: TestClient
    app_id: str
    headers: dict

    def chat(self, payload: dict):
        return self.client.post("/v1/chat/completions", headers=self.headers, json=payload)


@contextmanager
def conformance_client():
    # Context-managed so client.portal exists (DB writes run on the app's loop).
    with TestClient(create_app()) as c:
        yield c


def _run(client: TestClient, coro_fn) -> None:
    client.portal.call(coro_fn)


def add_model(client: TestClient, slug: str, *, tier: str = "cheap",
              price_input: float = 0.0, price_output: float = 0.0,
              allowed_app_ids: list[str] | None = None, enabled: bool = True) -> None:
    app = client.app

    async def go():
        async with app.state.db.session() as session:
            await store.upsert_onboarded(
                session, slug, display_name=slug, provider="onprem", tier=tier,
                endpoint="http://test-local/v1", api_key_env=None,
                price_input=price_input, price_output=price_output, quality=None,
                context_window=None, allowed_app_ids=allowed_app_ids, notes=None,
                enabled=enabled, probe_status="healthy" if enabled else "unreachable",
                probe_error=None)
            await session.commit()
        await app.state.registry.refresh(app.state.db)

    _run(client, go)


def disable_seeded_models(client: TestClient) -> None:
    """Take the test-seeded local-8b/local-32b out of the pool, so the case's own
    model is the only on-prem candidate."""
    add_model(client, "local-8b", tier="cheap", price_input=0.05, price_output=0.05,
              enabled=False)
    add_model(client, "local-32b", tier="mid", price_input=0.10, price_output=0.10,
              enabled=False)


def set_policy_direct(client: TestClient, app_id: str, **fields) -> None:
    """Write policy columns straight to the store (no admin clamps): the
    conformance matrix tests request-time behaviour of a stored policy."""
    app = client.app

    async def go():
        async with app.state.db.session() as session:
            await store.upsert_policy(session, app_id, **fields)
            await session.commit()

    _run(client, go)


def onboard(client: TestClient, app_id: str, **policy) -> OnboardedApp:
    _login(client)
    r = client.post("/admin/applications", json={"id": app_id, "name": app_id})
    assert r.status_code in (200, 201), r.text
    r = client.post(f"/admin/applications/{app_id}/tokens", json={"name": "conformance"})
    assert r.status_code in (200, 201), r.text
    if policy:
        set_policy_direct(client, app_id, **policy)
    return OnboardedApp(client, app_id, {"Authorization": f"Bearer {r.json()['token']}"})


def install_dual_fake(monkeypatch) -> None:
    """A litellm.acompletion fake that streams when asked to, else returns a
    canned completion. Records kwargs in tests._litellm_fake.CALLS."""
    async def _call(**kwargs):
        CALLS.append(kwargs)
        if kwargs.get("stream"):
            async def _gen():
                yield _FakeStreamChunk(content="ROUTEFORGE_OK")
                yield _FakeStreamChunk(empty_choices=True,
                                       usage={"prompt_tokens": 12, "completion_tokens": 3})
            return _gen()
        return make_response(content="ROUTEFORGE_OK")

    monkeypatch.setattr(litellm_backend, "acompletion", _call)
