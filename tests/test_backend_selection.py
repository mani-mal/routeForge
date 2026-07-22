"""Phase 9 Task 4 — make_backend factory + startup health-validation/fallback.

No pytest-asyncio/anyio plugin is installed (see conftest.py / other test
files) -- async paths are driven with asyncio.run(...) inside ordinary sync
`def test_...`, matching the rest of the suite.
"""
import asyncio
import pytest

from app.config.loader import load_settings
from app.gateway.backends.factory import make_backend, resolve_backend
from app.gateway.backends.mock import MockBackend
from app.gateway.backends.vllm import VllmBackend


def _s(backend, fallback=True):
    s = load_settings()
    return s.model_copy(update={"serving": s.serving.model_copy(
        update={"inference_backend": backend, "allow_backend_fallback": fallback})})


def test_factory_selects_by_config():
    assert isinstance(make_backend(_s("mock")), MockBackend)
    assert isinstance(make_backend(_s("vllm")), VllmBackend)


def test_factory_dynamo_and_none():
    from app.gateway.backends.dynamo import DynamoBackend
    assert isinstance(make_backend(_s("dynamo")), DynamoBackend)
    # 'none' still returns a working backend object -- the REJECTION of
    # onprem_only requests is enforced in context.py (Task 1), not here.
    assert isinstance(make_backend(_s("none")), MockBackend)


def test_unreachable_vllm_with_fallback_returns_mock(monkeypatch):
    s = _s("vllm", fallback=True)

    # force health() unhealthy
    async def bad(self):
        from app.gateway.backends.base import BackendHealth
        return BackendHealth(ok=False, detail="refused")
    monkeypatch.setattr(VllmBackend, "health", bad)
    b = asyncio.run(resolve_backend(s))
    assert isinstance(b, MockBackend)


def test_unreachable_vllm_no_fallback_exits(monkeypatch):
    s = _s("vllm", fallback=False)

    async def bad(self):
        from app.gateway.backends.base import BackendHealth
        return BackendHealth(ok=False, detail="refused")
    monkeypatch.setattr(VllmBackend, "health", bad)
    with pytest.raises(SystemExit):
        asyncio.run(resolve_backend(s))


def test_healthy_vllm_resolves_to_vllm_backend(monkeypatch):
    s = _s("vllm", fallback=True)

    async def ok(self):
        from app.gateway.backends.base import BackendHealth
        return BackendHealth(ok=True, detail="ok", models=["local-8b"])
    monkeypatch.setattr(VllmBackend, "health", ok)
    b = asyncio.run(resolve_backend(s))
    assert isinstance(b, VllmBackend)


def test_mock_and_none_never_unhealthy():
    # mock/none health is always ok -> resolve_backend never falls back for them.
    b_mock = asyncio.run(resolve_backend(_s("mock", fallback=False)))
    assert isinstance(b_mock, MockBackend)
    b_none = asyncio.run(resolve_backend(_s("none", fallback=False)))
    assert isinstance(b_none, MockBackend)
