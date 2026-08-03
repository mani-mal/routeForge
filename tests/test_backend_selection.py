"""make_backend/resolve_backend factory (model-onboarding plan, Task 4).

There is no `inference_backend` axis and no boot-time health probe any more:
per-model reachability is probed at onboard time (the registry), so the
single LiteLLM egress path is always constructed directly — see
app/gateway/backends/factory.py's module docstring. This file just pins that
contract: both factory functions always return a LiteLLMBackend, with or
without a registry, and never raise/exit.

No pytest-asyncio/anyio plugin is installed (see conftest.py / other test
files) -- async paths are driven with asyncio.run(...) inside ordinary sync
`def test_...`, matching the rest of the suite.
"""
import asyncio

from app.config.loader import load_settings
from app.gateway.backends.factory import make_backend, resolve_backend
from app.gateway.backends.litellm_backend import LiteLLMBackend
from tests._litellm_fake import make_test_registry

S = load_settings()


def test_make_backend_is_litellm():
    assert isinstance(make_backend(S), LiteLLMBackend)


def test_make_backend_injects_registry():
    reg = make_test_registry(S)
    b = make_backend(S, reg)
    assert isinstance(b, LiteLLMBackend)
    assert b._reg is reg


def test_resolve_backend_never_raises_and_needs_no_registry():
    # No boot-time health probe: resolve_backend must succeed even with no
    # registry at all (registry is populated/refreshed later, on first
    # request -- see app/main.py).
    b = asyncio.run(resolve_backend(S))
    assert isinstance(b, LiteLLMBackend)


def test_resolve_backend_with_registry():
    reg = make_test_registry(S)
    b = asyncio.run(resolve_backend(S, reg))
    assert isinstance(b, LiteLLMBackend)
    assert b._reg is reg
