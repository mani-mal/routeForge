import os
os.environ.setdefault("SP_PII_DETECTION", "regex")
# config/serving.yaml's shipped default is `vllm` (LiteLLM gateway swap,
# Task 2) so the demo boots against a real backend. Tests must stay offline
# and fast: keep onprem enabled (SP_INFERENCE_BACKEND=vllm, same as
# production) so serving tests still route to onprem candidates, but skip
# the real network health probe that `create_app()` would otherwise run at
# import time (every `client`/`live_internal_server` fixture) — mock is gone,
# so there is no fallback, and an unprobed real vllm host would hang or
# sys.exit(1) at test collection. See app/gateway/backends/factory.py's
# resolve_backend docstring for the flag's contract. Generation itself is
# driven by the autouse `_fake_acompletion` fixture below (a monkeypatched
# litellm.acompletion), never a real network call.
os.environ.setdefault("SP_INFERENCE_BACKEND", "vllm")
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
from tests._litellm_fake import _DeterministicLiteLLM, fake_acompletion


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
    path requests actually get compressed — see Task 6 (two-lever cost)."""
    settings = load_settings()
    bypass_on = settings.serving.context_optimization.bypass_on.model_copy(
        update={"user_prompt": False})
    co = settings.serving.context_optimization.model_copy(
        update={"enabled": True, "min_tokens": 1, "bypass_on": bypass_on})
    serving = settings.serving.model_copy(update={"context_optimization": co})
    settings = settings.model_copy(update={"serving": serving})

    detector = make_detector(settings.serving.pii_detection)
    gate = Gate(settings, detector)
    backend = _DeterministicLiteLLM(settings)
    model_router = Router(settings)
    gateway = Gateway(settings, backend, model_router)
    ccr = CCRStore(ttl_seconds=co.ccr.ttl_hours * 3600)
    return Pipeline(settings, gate, detector, gateway, self_port=8080,
                     co_cfg_provider=lambda: co, ccr=ccr)


@pytest.fixture
def internal_client_with_compression_on():
    """TestClient for the internal per-hop endpoint (Task 7 / M3) with
    context_optimization forced on (enabled=True, min_tokens=1,
    bypass_on.user_prompt=False) via app.state.co_cfg — mirrors
    `compression_on_pipeline` above but exercises
    `/internal/v1/{run_id}/chat/completions` directly instead of the direct
    pipeline path, so a hop's compression can be asserted in isolation.

    Starts a HOPS run with a non-onprem ExecutionPolicyContext and yields
    `(client, run_id)`; the TestClient's ASGI-transport host ("testclient")
    is on the internal endpoint's loopback allowlist, same as `client`.
    """
    from app.agent.internal_endpoint import HOPS
    from app.models import ExecutionPolicyContext

    app = create_app()
    bypass_on = app.state.co_cfg.bypass_on.model_copy(update={"user_prompt": False})
    app.state.co_cfg = app.state.co_cfg.model_copy(
        update={"enabled": True, "min_tokens": 1, "bypass_on": bypass_on})

    tc = TestClient(app)
    run_id = "run-compress-hop"
    ctx = ExecutionPolicyContext(
        request_id="req-compress", tenant_id="acme", sensitivity="internal",
        onprem_only=False, allowed_providers=frozenset({"onprem", "azure", "bedrock"}),
        allowed_models=frozenset(), max_cost_usd=1.0, max_latency_ms=10000,
        risk_class="low", require_verification=False, detected_entities=(),
        redaction_map_id=None)
    HOPS.start(run_id, ctx)
    try:
        yield tc, run_id
    finally:
        if HOPS.has(run_id):
            HOPS.pop(run_id)  # avoid leaking run state if a test fails early


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

    NAT's `tool_calling_agent` drives itself via a real OpenAI-SDK HTTP
    client (LangChain `ChatOpenAI`) that POSTs to our internal endpoint —
    a bare `TestClient` doesn't accept real socket connections, so the
    agent runtime tests need an actual listening server.
    """
    import uvicorn

    os.environ["SP_PII_DETECTION"] = "regex"
    port = _free_port()
    # The agent runtime (multi-step pipeline path) makes real HTTP self-calls
    # to this app's own internal endpoint at 127.0.0.1:{port} — SP_SERVER_PORT
    # must be set BEFORE create_app() so app/main.py wires that exact port
    # into Pipeline. See app/main.py's _self_port() and app/pipeline.py.
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
