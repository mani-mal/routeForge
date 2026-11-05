import ast
import pathlib

import httpx

ROOT = pathlib.Path(__file__).resolve().parents[2]
_ADMIN = {"email": "admin@routeforge.local", "password": "changeme"}
_FORBIDDEN_NAMES = {"ExternalSnapshot", "ExternalModelMetric", "ExternalModelMap"}


def test_chat_completion_makes_no_aa_call(client, monkeypatch):
    calls: list[str] = []
    real_send = httpx.AsyncClient.send

    async def spy(self, request, *a, **kw):
        if "artificialanalysis.ai" in request.url.host:
            calls.append(str(request.url))
        return await real_send(self, request, *a, **kw)

    monkeypatch.setattr(httpx.AsyncClient, "send", spy)
    monkeypatch.setenv("AA_API_KEY", "k")
    assert client.post("/admin/login", json=_ADMIN).status_code == 200
    token = client.post("/admin/applications",
                        json={"id": "noaa", "name": "No AA"}).json()["token"]
    r = client.post("/v1/chat/completions", headers={"Authorization": f"Bearer {token}"},
                    json={"messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code == 200
    assert calls == []
    # control: the spy does see an AA call when the refresh endpoint makes one
    client.app.state.aa_transport = httpx.MockTransport(
        lambda req: httpx.Response(200, json=[{"slug": "a", "name": "A"}]))
    assert client.post("/admin/external-metrics/refresh").status_code == 200
    assert len(calls) == 1


def _request_path_files():
    app = ROOT / "app"
    files = []
    for pkg in ("routing", "policy", "gateway", "output", "compression"):
        files += list((app / pkg).rglob("*.py"))
    files += [app / "api" / "routes.py"] + list(app.glob("pipeline*.py"))
    return files


def test_request_path_modules_do_not_import_external():
    offenders = []
    for path in _request_path_files():
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.ImportFrom):
                mod = node.module or ""
                names = {a.name for a in node.names}
                if (mod == "app.metrics.external" or mod.startswith("app.metrics.external.")
                        or (mod == "app.metrics" and "external" in names)
                        or (mod == "app.apps.models" and names & _FORBIDDEN_NAMES)):
                    offenders.append(f"{path}: from {mod}")
                elif mod == "app.apps" and "models" in names:
                    offenders.append(f"{path}: from app.apps import models")
            elif isinstance(node, ast.Import):
                for a in node.names:
                    if a.name.startswith("app.metrics.external"):
                        offenders.append(f"{path}: import {a.name}")
    assert offenders == []
