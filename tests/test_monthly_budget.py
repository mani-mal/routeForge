"""Display-only `monthly_budget_usd`: stored and shown, never enforced, never
part of the policy version."""
from __future__ import annotations

import pytest

from tests._litellm_fake import CALLS
from tests.onboarding.harness import install_dual_fake, onboard
from tests.test_admin_api import _login
from tests.test_outcome_hooks import HELLO, _client


@pytest.fixture(autouse=True)
def _calls():
    CALLS.clear()
    yield
    CALLS.clear()


def _put(c, app_id: str, body: dict):
    _login(c)
    return c.put(f"/admin/applications/{app_id}/policy", json=body)


def _get_policy(c, app_id: str) -> dict:
    return c.get(f"/admin/applications/{app_id}").json()["policy"]


def test_put_and_get_monthly_budget():
    with _client() as c:
        onboard(c, "mb-app")
        assert _put(c, "mb-app", {"monthly_budget_usd": 150.0}).status_code == 200
        assert _get_policy(c, "mb-app")["monthly_budget_usd"] == 150.0


@pytest.mark.parametrize("value", [0, 0.0, -5])
def test_zero_or_negative_is_422(value):
    with _client() as c:
        onboard(c, "mb-bad")
        r = _put(c, "mb-bad", {"monthly_budget_usd": value})
        assert r.status_code == 422, r.text
        assert "monthly_budget_usd" in r.json()["detail"]


def test_reset_clears_it():
    with _client() as c:
        onboard(c, "mb-reset")
        assert _put(c, "mb-reset", {"monthly_budget_usd": 50, "max_rpm": 9}).status_code == 200
        assert _put(c, "mb-reset", {"reset": ["monthly_budget_usd"]}).status_code == 200
        got = _get_policy(c, "mb-reset")
        assert got["monthly_budget_usd"] is None
        assert got["max_rpm"] == 9


def test_does_not_change_app_policy_version(monkeypatch):
    install_dual_fake(monkeypatch)
    with _client() as c:
        app = onboard(c, "mb-ver")
        r1 = app.chat(HELLO)
        assert r1.status_code == 200, r1.text
        v1 = r1.json()["policy"]["app_policy_version"]
        assert _put(c, "mb-ver", {"monthly_budget_usd": 10}).status_code == 200
        r2 = app.chat(HELLO)
        assert r2.status_code == 200, r2.text
        assert r2.json()["policy"]["app_policy_version"] == v1


def test_not_enforced(monkeypatch):
    install_dual_fake(monkeypatch)
    with _client() as c:
        app = onboard(c, "mb-free")
        assert _put(c, "mb-free", {"monthly_budget_usd": 0.000001}).status_code == 200
        for _ in range(2):  # the first call's spend is already above the budget
            assert app.chat(HELLO).status_code == 200
        mtd = c.get("/admin/metrics/app/mb-free").json()["current"]["budget"]["month_to_date_usd"]
        assert mtd > 0.000001  # spend really exceeded the budget, and the second call still passed
