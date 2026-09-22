"""GET /v1/models — the OpenAI model list, scoped to the calling application.

OpenAI clients (Qwen Code, the `openai` SDK, LangChain, open-webui) call
`/v1/models` to discover or validate model ids before a chat turn. The list is
exactly the set of ids this app may send as `model` on /v1/chat/completions:
the same placement and allowlist rules the pipeline enforces on a pinned
model, so a listed id is never rejected for policy and an unlisted one is.
"""
import asyncio

import pytest
from fastapi.testclient import TestClient

from app.apps import store
from app.apps.policy import AppPolicyView
from app.config.loader import load_settings
from app.main import create_app
from app.policy.context import requestable_models
from tests._litellm_fake import make_test_registry
from tests.test_app_policy import _seed_policy, _with_db

_DEMO = {"Authorization": "Bearer demo-key"}


def _ids(body: dict) -> set[str]:
    return {m["id"] for m in body["data"]}


def test_models_requires_a_token(client):
    assert client.get("/v1/models").status_code == 401
    assert client.get("/v1/models",
                      headers={"Authorization": "Bearer nope"}).status_code == 401


def test_models_returns_openai_list_of_onboarded_models(client):
    r = client.get("/v1/models", headers=_DEMO)
    assert r.status_code == 200
    body = r.json()
    assert body["object"] == "list"
    assert _ids(body) == {"local-8b", "local-32b"}
    Model = pytest.importorskip("openai.types").Model
    for m in body["data"]:
        Model.model_validate(m)  # the same parse a real OpenAI client runs
        assert m["owned_by"] == "onprem"


def test_models_honors_app_allowed_models(tmp_path):
    db_url, restore = _with_db(tmp_path)
    try:
        _seed_policy(db_url, "acme", allowed_models=["local-32b"])
        with TestClient(create_app()) as c:
            r = c.get("/v1/models", headers={"Authorization": "Bearer acme-key"})
            assert r.status_code == 200
            assert _ids(r.json()) == {"local-32b"}
    finally:
        restore()


def test_models_honors_app_allowed_tiers(tmp_path):
    db_url, restore = _with_db(tmp_path)
    try:
        _seed_policy(db_url, "acme", allowed_tiers=["cheap"])
        with TestClient(create_app()) as c:
            r = c.get("/v1/models", headers={"Authorization": "Bearer acme-key"})
            assert _ids(r.json()) == {"local-8b"}
    finally:
        restore()


def test_every_listed_model_is_accepted_as_a_pinned_model(client):
    ids = _ids(client.get("/v1/models", headers=_DEMO).json())
    assert ids
    for model_id in ids:
        r = client.post("/v1/chat/completions", headers=_DEMO, json={
            "model": model_id,
            "messages": [{"role": "user", "content": "capital of France?"}]})
        assert r.status_code == 200, (model_id, r.text)


def _settings_with_azure():
    settings = load_settings()
    providers = dict(settings.serving.providers)
    providers["azure"] = providers["azure"].model_copy(update={"enabled": True})
    serving = settings.serving.model_copy(update={"providers": providers})
    return settings.model_copy(update={"serving": serving})


def test_cloud_models_listed_only_when_residency_allows():
    settings = _settings_with_azure()
    registry = make_test_registry(settings)

    def names(policy):
        return {c.name for c in requestable_models(settings, registry, "acme", policy)}

    assert "azure-gpt-mid" in names(AppPolicyView(residency="any"))
    assert names(AppPolicyView(residency="onprem_only")) == {"local-8b", "local-32b"}
    assert names(AppPolicyView(residency="any",
                               allowed_providers=frozenset({"azure"}))) == {"azure-gpt-mid"}


def _set_context_window(client: TestClient, slug: str, context_window: int | None) -> None:
    """Update one onboarded row's context_window and refresh the app's live
    registry, the same way `POST/PUT /admin/infra/models` does."""
    async def go():
        async with client.app.state.db.session() as session:
            await store.upsert_onboarded(session, slug, context_window=context_window)
            await session.commit()
        await client.app.state.registry.refresh(client.app.state.db)
    asyncio.run(go())


def test_models_list_publishes_max_model_len(client):
    """A known context window reaches the client under vLLM's own field name,
    which is the name clients already read (B50)."""
    _set_context_window(client, "local-8b", 131072)
    r = client.get("/v1/models", headers=_DEMO)
    entry = next(m for m in r.json()["data"] if m["id"] == "local-8b")
    assert entry["max_model_len"] == 131072


def test_models_list_omits_unknown_max_model_len(client):
    """Unknown is absent, never a guess and never zero."""
    _set_context_window(client, "local-32b", None)
    r = client.get("/v1/models", headers=_DEMO)
    entry = next(m for m in r.json()["data"] if m["id"] == "local-32b")
    assert "max_model_len" not in entry
