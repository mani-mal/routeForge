import os
os.environ.setdefault("SP_PII_DETECTION", "regex")
# There is no `inference_backend` axis any more — every provider egresses
# through the single LiteLLM SDK path, and on-prem availability/endpoints come
# from the ModelRegistry (onboarded rows), never a boot-time backend switch or
# health probe. SP_SEED_TEST_MODELS reproduces the former built-in Qwen models
# (local-8b/local-32b) as onboarded rows (app/apps/bootstrap.py::
# seed_test_models) so the existing pipeline test-suite, which routes to them
# by name, keeps working now that they left the static YAML pool. Production
# never sets this, so the pool boots empty. SP_SKIP_BACKEND_HEALTHCHECK is now
# a harmless no-op (resolve_backend no longer probes at boot) — left set for
# any lingering reference. Generation itself is driven by the autouse
# `_fake_acompletion` fixture below (a monkeypatched litellm.acompletion),
# never a real network call.
os.environ.setdefault("SP_SEED_TEST_MODELS", "1")
os.environ.setdefault("SP_SKIP_BACKEND_HEALTHCHECK", "1")
# Each create_app() gets its own in-memory SQLite (StaticPool → one connection
# per engine → isolated DB), so the application/token/usage store never leaks
# state across tests and no DB server is needed.
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite://")

import socket
import threading
import time
import urllib.request
from dataclasses import dataclass

import pytest
from fastapi.testclient import TestClient
from app.main import create_app
from app.compression.ccr import CCRStore
from app.config.loader import load_settings
from app.gateway.backends import litellm_backend
from app.gateway.gateway import Gateway
from app.pipeline import Pipeline
from app.policy.detectors import make_detector
from app.policy.gate import Gate
from app.routing.router import Router
from tests._litellm_fake import (
    _DeterministicLiteLLM, fake_acompletion, make_test_registry,
)


@pytest.fixture(autouse=True)
def _fake_acompletion(monkeypatch):
    """Autouse: any test that builds the real pipeline/`client` and actually
    generates gets a canned completion instead of a network call. Tests that
    need specific content/confidence/logprobs monkeypatch `litellm_backend.
    acompletion` locally (that local monkeypatch wins — it's applied after
    this fixture's, in the same test body). Tests that use
    `_DeterministicLiteLLM` never call `acompletion` at all, so this fixture
    is a no-op for them.
    """
    monkeypatch.setattr(litellm_backend, "acompletion", fake_acompletion)


@pytest.fixture
def client() -> TestClient:
    return TestClient(create_app())


@pytest.fixture
def compression_on_pipeline() -> Pipeline:
    """Same Pipeline app/main.py builds, but with context_optimization forced
    on (enabled=True, min_tokens=1, bypass_on.user_prompt=False) so direct-
    path requests actually get compressed — see Task 6 (two-lever cost).

    Builds its own registry (not through create_app()/seed_all) via
    `tests._litellm_fake.make_test_registry`, so on-prem routing (local-8b/
    local-32b) still resolves now that those slugs left the static YAML pool.
    """
    settings = load_settings()
    bypass_on = settings.serving.context_optimization.bypass_on.model_copy(
        update={"user_prompt": False})
    co = settings.serving.context_optimization.model_copy(
        update={"enabled": True, "min_tokens": 1, "bypass_on": bypass_on})
    serving = settings.serving.model_copy(update={"context_optimization": co})
    settings = settings.model_copy(update={"serving": serving})

    registry = make_test_registry(settings)
    detector = make_detector(settings.serving.pii_detection)
    gate = Gate(settings, detector, registry=registry)
    backend = _DeterministicLiteLLM(settings)
    model_router = Router(settings, registry=registry)
    gateway = Gateway(settings, backend, model_router, registry)
    ccr = CCRStore(ttl_seconds=co.ccr.ttl_hours * 3600)
    return Pipeline(settings, gate, detector, gateway,
                     co_cfg_provider=lambda: co, ccr=ccr, registry=registry)


def admin_cookie(port: int) -> str:
    """Log in the seeded admin on a live (real-socket) server and return the
    `Cookie` header value (e.g. `rf_admin_session=...`). The console read
    surface (/api/*) is admin-gated, so raw http.client / httpx callers that
    aren't the TestClient need this to reach it."""
    import json as _json

    conn = urllib.request.Request(
        f"http://127.0.0.1:{port}/admin/login",
        data=_json.dumps({"email": "admin@routeforge.local",
                          "password": "changeme"}).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(conn, timeout=5) as resp:
        assert resp.status == 200, f"admin login failed: {resp.status}"
        set_cookie = resp.headers.get("Set-Cookie") or ""
    return set_cookie.split(";", 1)[0]


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@dataclass
class LiveServer:
    port: int


@pytest.fixture
def live_internal_server():
    """Boot the real app under a real uvicorn server on an ephemeral port.

    A bare `TestClient` doesn't accept real socket connections, so any test
    that needs a genuine HTTP round trip -- e.g. the live SSE decision stream
    (`test_decision_stream.py`, `test_b3_console_shared_state.py`) or the
    `/api/traffic` control's own self-call to this same server
    (`test_traffic_endpoint.py`, `app/api/ui_routes.py`) -- needs an actual
    listening server.
    """
    import uvicorn

    os.environ["SP_PII_DETECTION"] = "regex"
    port = _free_port()
    # SP_SERVER_PORT must be set BEFORE create_app() so app/main.py's
    # _self_port() picks up this exact port for app.state.self_port (the
    # /api/traffic control's self-call target -- see app/api/ui_routes.py).
    os.environ["SP_SERVER_PORT"] = str(port)
    config = uvicorn.Config(create_app(), host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=0.5) as resp:
                if resp.status == 200:
                    break
        except Exception:
            time.sleep(0.1)
    else:
        raise RuntimeError("live_internal_server: /health did not come up in time")

    try:
        yield LiveServer(port=port)
    finally:
        server.should_exit = True
        thread.join(timeout=5)
        os.environ.pop("SP_SERVER_PORT", None)
