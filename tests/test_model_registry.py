import asyncio

from app.config.loader import load_settings
from app.apps.db import Database
from app.apps import store
from app.apps.registry import ModelRegistry


def _run(c):
    return asyncio.run(c)


S = load_settings()


async def _reg_with(rows):
    db = Database(url="sqlite+aiosqlite://")
    await db.ensure_ready()
    async with db.session() as s:
        for slug, f in rows.items():
            await store.upsert_onboarded(s, slug, **f)
        await s.commit()
    reg = ModelRegistry(S)
    await reg.refresh(db)
    return reg


def test_effective_pool_merges_enabled_onboarded():
    reg = _run(_reg_with({
        "nemotron-mid": dict(display_name="Nemotron", provider="onprem",
            tier="mid", price_input=0.2, price_output=0.2,
            endpoint="http://nem/v1", api_key_env="NEM", enabled=True),
        "off-model": dict(display_name="Off", provider="onprem", tier="cheap",
            price_input=0.1, price_output=0.1, endpoint="http://o/v1",
            enabled=False)}))
    names = {c.name for c in reg.effective_pool()}
    assert "nemotron-mid" in names          # enabled onboarded merged in
    assert "off-model" not in names         # disabled excluded
    assert reg.price_for("nemotron-mid").input == 0.2
    assert reg.endpoint_for("nemotron-mid") == ("http://nem/v1", "NEM")
    assert reg.has_healthy_onprem() is True


def test_app_scope():
    reg = _run(_reg_with({
        "scoped": dict(display_name="S", provider="onprem", tier="mid",
            price_input=0.2, price_output=0.2, endpoint="http://s/v1",
            enabled=True, allowed_app_ids=["acme"])}))
    assert reg.is_allowed_for_app("scoped", "acme") is True
    assert reg.is_allowed_for_app("scoped", "hospital") is False


def test_empty_onboarded_has_no_healthy_onprem():
    reg = _run(_reg_with({}))
    assert reg.has_healthy_onprem() is False
