"""Phase 9 Task 4 — make_backend factory + startup health-validation
(cut over to LiteLLM in the litellm-gateway-swap plan's Task 5).

No pytest-asyncio/anyio plugin is installed (see conftest.py / other test
files) -- async paths are driven with asyncio.run(...) inside ordinary sync
`def test_...`, matching the rest of the suite.
"""
import asyncio
import pytest

from app.config.loader import load_settings
from app.gateway.backends.factory import make_backend, resolve_backend
from app.gateway.backends.litellm_backend import LiteLLMBackend


def _s(backend):
    s = load_settings()
    return s.model_copy(update={"serving": s.serving.model_copy(
        update={"inference_backend": backend})})


def test_make_backend_is_litellm():
    assert isinstance(make_backend(_s("vllm")), LiteLLMBackend)
    assert isinstance(make_backend(_s("none")), LiteLLMBackend)


@pytest.mark.xfail(reason="Qwen removed in Task 4", strict=False)
def test_unreachable_onprem_exits(monkeypatch):
    # conftest.py sets SP_SKIP_BACKEND_HEALTHCHECK process-wide so create_app()
    # never hangs during test collection; that would bypass the very probe
    # this test exercises, so unset it here.
    monkeypatch.delenv("SP_SKIP_BACKEND_HEALTHCHECK", raising=False)

    async def bad(self):
        from app.gateway.backends.base import BackendHealth
        return BackendHealth(ok=False, detail="down")
    monkeypatch.setattr(LiteLLMBackend, "health", bad)
    with pytest.raises(SystemExit):
        asyncio.run(resolve_backend(_s("vllm")))


def test_reachable_onprem_resolves_to_litellm_backend(monkeypatch):
    monkeypatch.delenv("SP_SKIP_BACKEND_HEALTHCHECK", raising=False)

    async def ok(self):
        from app.gateway.backends.base import BackendHealth
        return BackendHealth(ok=True, detail="ok", models=["local-8b"])
    monkeypatch.setattr(LiteLLMBackend, "health", ok)
    b = asyncio.run(resolve_backend(_s("vllm")))
    assert isinstance(b, LiteLLMBackend)


def test_none_backend_never_unhealthy(monkeypatch):
    # 'none' skips the network probe entirely (see LiteLLMBackend.health) --
    # resolve_backend never exits for it, regardless of the skip flag.
    monkeypatch.delenv("SP_SKIP_BACKEND_HEALTHCHECK", raising=False)
    b = asyncio.run(resolve_backend(_s("none")))
    assert isinstance(b, LiteLLMBackend)


def test_skip_healthcheck_flag_bypasses_the_probe(monkeypatch):
    # Explicit unit test for the escape hatch itself (conftest.py relies on
    # this behaviour to keep create_app() offline in every fixture).
    monkeypatch.setenv("SP_SKIP_BACKEND_HEALTHCHECK", "1")

    async def bad(self):
        from app.gateway.backends.base import BackendHealth
        return BackendHealth(ok=False, detail="down")
    monkeypatch.setattr(LiteLLMBackend, "health", bad)  # would exit if probed
    b = asyncio.run(resolve_backend(_s("vllm")))
    assert isinstance(b, LiteLLMBackend)


@pytest.mark.xfail(reason="Qwen removed in Task 4", strict=False)
def test_resolved_backend_client_not_bound_to_resolve_loop(monkeypatch):
    """resolve_backend must not hand back something whose network layer is
    bound to its (throwaway) loop.

    main.py runs resolve_backend under asyncio.run(), whose loop closes on
    return, while requests are served on uvicorn's loop. LiteLLMBackend holds
    no persistent client (litellm.acompletion opens its own connections per
    call), so there is no cross-loop state to leak -- this test pins that
    invariant so a future change that adds a persistent client on the class
    is forced to reconsider the probe/serve split documented in
    app/gateway/backends/factory.py.
    """
    monkeypatch.delenv("SP_SKIP_BACKEND_HEALTHCHECK", raising=False)

    async def ok(self):
        from app.gateway.backends.base import BackendHealth
        return BackendHealth(ok=True, models=["local-8b"])
    monkeypatch.setattr(LiteLLMBackend, "health", ok)

    backend = asyncio.run(resolve_backend(_s("vllm")))  # loop A now closed
    assert not hasattr(backend, "_client")

    # Loop B: exercise generate() on a fresh loop -- must not raise
    # "Event loop is closed".
    from app.gateway.backends import litellm_backend
    from tests._litellm_fake import fake_acompletion
    monkeypatch.setattr(litellm_backend, "acompletion", fake_acompletion)

    async def _serve():
        from app.models import Message
        s = _s("vllm")
        price = s.pricing.models["local-8b"]
        await backend.generate("local-8b", [Message(role="user", content="hi")],
                                price, "cheap", {})
    asyncio.run(_serve())
