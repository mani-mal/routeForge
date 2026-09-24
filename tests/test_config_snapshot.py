"""P3a Task 1: `GET /admin/config-snapshot` -- admin-only, read-only,
content-free and secret-free (spec §4)."""
from __future__ import annotations

import json

from fastapi.testclient import TestClient

from app.apps.probe import ProbeResult
from app.main import create_app
from tests.test_admin_api import _login

FORBIDDEN_KEYS = {"endpoint", "api_key_env", "observability", "pii_exempt_patterns",
                  "recognizers", "langfuse_public_key", "langfuse_secret_key",
                  "langfuse_secret_enc", "public_key", "secret_key"}


def _all_keys(obj) -> set[str]:
    keys: set[str] = set()
    if isinstance(obj, dict):
        for k, v in obj.items():
            keys.add(k)
            keys |= _all_keys(v)
    elif isinstance(obj, list):
        for v in obj:
            keys |= _all_keys(v)
    return keys


def test_snapshot_requires_admin():
    with TestClient(create_app()) as c:
        assert c.get("/admin/config-snapshot").status_code == 401


def test_snapshot_shape_and_versions():
    with TestClient(create_app()) as c:
        _login(c)
        r = c.get("/admin/config-snapshot")
        assert r.status_code == 200
        doc = r.json()
        assert doc["schema"] == "routeforge.config-snapshot/v1"
        assert doc["routing_policy_version"] == c.app.state.settings.routing_policy_version
        assert doc["registry_version"] == c.app.state.registry.registry_version()
        assert set(doc["settings"]) == {"routing", "pricing", "policy", "providers"}
        for p in doc["settings"]["providers"].values():
            assert set(p) == {"enabled", "region", "prompt_retention", "accepts_raw_sensitive"}
        slugs = [o["slug"] for o in doc["onboarded"]]
        assert slugs == sorted(slugs) and "local-8b" in slugs   # SP_SEED_TEST_MODELS
        ids = [a["application_id"] for a in doc["apps"]]
        # Seeded apps are acme/hospital/default (app/apps/bootstrap.py::SEED_APPS)
        # -- there is no "demo" application id.
        assert ids == sorted(ids) and "default" in ids
        app = next(a for a in doc["apps"] if a["application_id"] == "default")
        assert "pii_exempt_patterns" not in app["policy"]
        assert "residency" in app["policy"]
        assert set(doc["quality"]) == {"customer", "calibrated"}


def test_snapshot_has_no_forbidden_keys_or_secret_values(monkeypatch):
    monkeypatch.setenv("SP_ONPREM_API_KEY", "sk-SEEDED-SECRET-VALUE-123")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "lf-SEEDED-SECRET-456")
    with TestClient(create_app()) as c:
        _login(c)
        doc = c.get("/admin/config-snapshot").json()
    assert _all_keys(doc) & FORBIDDEN_KEYS == set()
    text = json.dumps(doc)
    assert "sk-SEEDED-SECRET-VALUE-123" not in text
    assert "lf-SEEDED-SECRET-456" not in text
    assert "http://" not in text and "https://" not in text   # no endpoint URLs


def test_snapshot_includes_disabled_onboarded_rows(monkeypatch):
    # No PATCH route exists for /admin/infra/models/{slug} -- only
    # POST/PUT/DELETE/GET (app/api/admin_routes.py). Disabling a model is a
    # full PUT replace with the existing row's fields and enabled=False,
    # same as tests/test_onboard_api.py::test_put_can_disable_healthy_model.
    # probe_endpoint is monkeypatched so the PUT doesn't attempt a real
    # network call against the seeded test fixture endpoint.
    async def _ok(*a, **k):
        return ProbeResult(True, "ok")

    monkeypatch.setattr("app.api.admin_routes.probe_endpoint", _ok)
    with TestClient(create_app()) as c:
        _login(c)
        before = c.get("/admin/config-snapshot").json()["onboarded"][0]
        slug = before["slug"]
        row = c.get("/admin/infra/models").json()
        row = next(m for m in row if m["slug"] == slug)
        body = {
            "slug": row["slug"], "display_name": row["display_name"],
            "tier": row["tier"], "endpoint": row["endpoint"],
            "api_key_env": row["api_key_env"], "price_input": row["price_input"],
            "price_output": row["price_output"],
            "price_cached_input": row["price_cached_input"],
            "quality": row["quality"], "context_window": row["context_window"],
            "allowed_app_ids": row["allowed_app_ids"], "notes": row["notes"],
            "enabled": False, "region": row["region"],
            "prompt_retention": row["prompt_retention"],
            "accepts_raw_sensitive": row["accepts_raw_sensitive"],
        }
        r = c.put(f"/admin/infra/models/{slug}", json=body)
        assert r.status_code == 200, r.text
        assert r.json()["enabled"] is False
        doc = c.get("/admin/config-snapshot").json()
        row = next(o for o in doc["onboarded"] if o["slug"] == slug)
        assert row["enabled"] is False
