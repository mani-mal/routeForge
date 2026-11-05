"""B32/B61: api_key_env is a NAME; a 401 is `unauthorized`, not `unreachable`."""
import asyncio

import httpx
import pytest

from app.apps.probe import ProbeResult, probe_endpoint
from tests.test_onboard_api import _BODY, _fail, _login, _ok


@pytest.mark.parametrize("bad", ["sk-abc123", "lower_case", "HAS SPACE", "A-B",
                                 "9LEADING", "", "A" * 129])
def test_key_shaped_or_bad_name_is_422(client, monkeypatch, bad):
    monkeypatch.setattr("app.api.admin_routes.probe_endpoint", _ok)
    _login(client)
    r = client.post("/admin/infra/models", json={**_BODY, "api_key_env": bad})
    assert r.status_code == 422
    assert "NAME of an environment variable" in r.json()["detail"]
    r = client.put("/admin/infra/models/x", json={**_BODY, "api_key_env": bad})
    assert r.status_code == 422
    # the bad value was never stored
    assert not any(m["slug"] == _BODY["slug"] for m in client.get("/admin/infra/models").json())


def test_unset_env_var_accepted_with_warning(client, monkeypatch):
    monkeypatch.setattr("app.api.admin_routes.probe_endpoint", _ok)
    monkeypatch.delenv("RF_TEST_KEY", raising=False)
    _login(client)
    r = client.post("/admin/infra/models", json={**_BODY, "api_key_env": "RF_TEST_KEY"})
    assert r.status_code == 200
    assert "env var RF_TEST_KEY is not set in the API container" in r.json()["warnings"]


def test_set_env_var_has_no_warning(client, monkeypatch):
    monkeypatch.setattr("app.api.admin_routes.probe_endpoint", _ok)
    monkeypatch.setenv("RF_TEST_KEY", "x")
    _login(client)
    r = client.post("/admin/infra/models", json={**_BODY, "api_key_env": "RF_TEST_KEY"})
    assert not any("not set" in w for w in r.json()["warnings"])


async def _unauth(*a, **k):
    return ProbeResult(False, "endpoint rejected the credentials (HTTP 401)",
                       unauthorized=True)


def test_unauthorized_probe_status_and_not_enabled(client, monkeypatch):
    monkeypatch.setattr("app.api.admin_routes.probe_endpoint", _unauth)
    _login(client)
    r = client.post("/admin/infra/models", json=_BODY)
    body = r.json()
    assert body["probe_status"] == "unauthorized"
    assert body["enabled"] is False
    assert "rejected the credentials" in body["probe_error"]
    row = next(m for m in client.get("/admin/infra/models").json() if m["slug"] == _BODY["slug"])
    assert row["probe_status"] == "unauthorized" and row["enabled"] is False
    # unreachable stays unreachable
    monkeypatch.setattr("app.api.admin_routes.probe_endpoint", _fail)
    r = client.put(f"/admin/infra/models/{_BODY['slug']}", json=_BODY)
    assert r.json()["probe_status"] == "unreachable"


@pytest.mark.parametrize("code", [401, 403])
def test_probe_maps_auth_failure(monkeypatch, code):
    transport = httpx.MockTransport(lambda req: httpx.Response(code))
    real = httpx.AsyncClient
    monkeypatch.setattr("app.apps.probe.httpx.AsyncClient",
                        lambda **k: real(transport=transport, **k))
    res = asyncio.run(probe_endpoint("http://e/v1", "m", None))
    assert not res.ok and res.unauthorized and res.status == "unauthorized"
    assert str(code) in res.detail


def test_probe_result_status_is_fail_closed():
    assert ProbeResult(True, "ok").status == "healthy"
    assert ProbeResult(False, "x").status == "unreachable"


def test_listing_masks_non_name_api_key_env():
    from types import SimpleNamespace
    from app.api.admin_routes import _INVALID_ENV_MASK, _ENV_NAME_RE, _onboarded_dict

    def row(v):
        return SimpleNamespace(
            slug="s", display_name="S", provider="onprem", tier="mid", quality=None,
            price_input=0.1, price_output=0.1, price_cached_input=None,
            endpoint="http://e/v1", api_key_env=v, context_window=None,
            context_window_source=None, enabled=True, allowed_app_ids=None,
            probe_status="ok", probe_error=None, notes=None, updated_at=None,
            region=None, prompt_retention=None, accepts_raw_sensitive=None)

    assert _onboarded_dict(row("sk-live-SECRET"))["api_key_env"] == _INVALID_ENV_MASK
    assert "SECRET" not in str(_onboarded_dict(row("sk-live-SECRET")))
    assert _onboarded_dict(row("MY_KEY"))["api_key_env"] == "MY_KEY"
    assert _onboarded_dict(row(None))["api_key_env"] is None
    # re-submitting the mask is refused by the PUT validation
    assert not _ENV_NAME_RE.match(_INVALID_ENV_MASK)
