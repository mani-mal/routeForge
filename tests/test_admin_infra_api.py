_ADMIN = {"email": "admin@routeforge.local", "password": "changeme"}


def _login(client):
    r = client.post("/admin/login", json=_ADMIN)
    assert r.status_code == 200


def test_infra_requires_admin(client):
    assert client.get("/admin/infra").status_code in (401, 403)


def test_get_infra_defaults_and_backend_readback(client):
    _login(client)
    r = client.get("/admin/infra")
    assert r.status_code == 200
    body = r.json()
    # boot serving-layer echoed read-only (conftest sets SP_INFERENCE_BACKEND=vllm)
    assert body["inference_backend"] == "vllm"
    # cluster defaults when no row yet
    assert body["runai_licensed"] is False
    assert body["default_engine"] == "vllm"


def test_put_infra_then_get_roundtrip(client):
    _login(client)
    put = client.put("/admin/infra", json={
        "runai_licensed": True, "runai_project": "runai-routeforge",
        "default_engine": "trtllm"})
    assert put.status_code == 200
    got = client.get("/admin/infra").json()
    assert got["runai_licensed"] is True
    assert got["runai_project"] == "runai-routeforge"
    assert got["default_engine"] == "trtllm"


def test_put_infra_bad_engine_422(client):
    _login(client)
    r = client.put("/admin/infra", json={"default_engine": "sglang"})
    assert r.status_code == 422


def test_get_infra_models_lists_onprem_pool(client):
    _login(client)
    rows = client.get("/admin/infra/models").json()
    names = {r["model_name"] for r in rows}
    # on-prem pool members from routing.yaml appear; cloud models do not
    assert "local-8b" in names
    assert "azure-gpt-mid" not in names
    # unmanaged members are flagged
    assert all("managed" in r and "enabled" in r for r in rows)


def test_put_model_then_get_reflects_and_gates(client):
    _login(client)
    put = client.put("/admin/infra/models/local-8b", json={
        "engine": "trtllm", "enabled": False, "gpu_type": "NVIDIA-H200"})
    assert put.status_code == 200
    rows = {r["model_name"]: r for r in client.get("/admin/infra/models").json()}
    assert rows["local-8b"]["enabled"] is False
    assert rows["local-8b"]["engine"] == "trtllm"
    assert rows["local-8b"]["managed"] is True
    # the roster cache was refreshed on write
    snap = client.app.state.infra_roster.current()
    assert snap is not None and snap.models["local-8b"].enabled is False


def test_put_model_bad_engine_422(client):
    _login(client)
    r = client.put("/admin/infra/models/local-8b", json={"engine": "nope"})
    assert r.status_code == 422


def test_put_unknown_model_404(client):
    _login(client)
    r = client.put("/admin/infra/models/not-a-real-model", json={"enabled": True})
    assert r.status_code == 404
