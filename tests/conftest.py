import os
os.environ.setdefault("SP_PII_DETECTION", "regex")

import socket
import threading
import time
import urllib.request
from dataclasses import dataclass

import pytest
from fastapi.testclient import TestClient
from app.main import create_app


@pytest.fixture
def client() -> TestClient:
    return TestClient(create_app())


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
