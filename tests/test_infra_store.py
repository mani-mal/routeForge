import asyncio

from app.apps import store
from app.apps.db import Database


def _run(coro):
    return asyncio.run(coro)


def test_infra_settings_singleton_roundtrip():
    db = Database(url="sqlite+aiosqlite:///:memory:")

    async def go():
        async with db.session() as s:
            await store.upsert_infra_settings(
                s, runai_licensed=True, runai_project="runai-routeforge",
                default_engine="trtllm")
            await s.commit()
        async with db.session() as s:
            row = await store.get_infra_settings(s)
            assert row is not None
            assert row.id == "cluster"
            assert row.runai_licensed is True
            assert row.runai_project == "runai-routeforge"
            assert row.default_engine == "trtllm"
        # second upsert updates the same singleton row
        async with db.session() as s:
            await store.upsert_infra_settings(s, runai_licensed=False)
            await s.commit()
        async with db.session() as s:
            row = await store.get_infra_settings(s)
            assert row.runai_licensed is False
            assert row.default_engine == "trtllm"  # untouched field preserved

    _run(go())


def test_infra_model_upsert_and_list():
    db = Database(url="sqlite+aiosqlite:///:memory:")

    async def go():
        async with db.session() as s:
            await store.upsert_infra_model(
                s, "local-8b", engine="vllm", enabled=True, gpu_type="NVIDIA-H200",
                replicas=2, tensor_parallel=1)
            await store.upsert_infra_model(s, "local-32b", engine="trtllm", enabled=False)
            await s.commit()
        async with db.session() as s:
            rows = await store.list_infra_models(s)
            by_name = {r.model_name: r for r in rows}
            assert by_name["local-8b"].engine == "vllm"
            assert by_name["local-8b"].enabled is True
            assert by_name["local-8b"].replicas == 2
            assert by_name["local-32b"].enabled is False
        # upsert existing patches only given fields
        async with db.session() as s:
            await store.upsert_infra_model(s, "local-32b", enabled=True)
            await s.commit()
        async with db.session() as s:
            row = await store.get_infra_model(s, "local-32b")
            assert row.enabled is True
            assert row.engine == "trtllm"  # preserved

    _run(go())
