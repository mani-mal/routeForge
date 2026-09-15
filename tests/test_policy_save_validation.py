import pytest

from tests.onboarding.harness import conformance_client, disable_seeded_models
from tests.test_admin_api import _login


@pytest.fixture
def admin():
    with conformance_client() as c:
        _login(c)
        c.post("/admin/applications", json={"id": "lint", "name": "lint"})
        yield c


def _put(c, **body):
    return c.put("/admin/applications/lint/policy", json=body)


@pytest.mark.parametrize("body,needle", [
    ({"allowed_tiers": ["strong"]}, "unknown tier"),
    ({"allowed_providers": ["on-prem"]}, "unknown provider"),
    ({"allowed_models": ["no-such-model"]}, "unknown model"),
    ({"allowed_tiers": []}, "empty list"),
    ({"allowed_models": []}, "empty list"),
    ({"max_rpm": 0}, "max_rpm"),
    ({"max_spend_usd_per_day": 0}, "max_spend_usd_per_day"),
])
def test_impossible_policy_is_refused(admin, body, needle):
    r = _put(admin, **body)
    assert r.status_code == 422, r.text
    assert needle in r.json()["detail"]


def test_valid_policy_saves_with_no_warnings(admin):
    r = _put(admin, allowed_models=["local-8b"])
    assert r.status_code == 200, r.text
    assert r.json()["warnings"] == []


def test_disabled_onboarded_model_is_a_known_slug(admin):
    from tests.onboarding.harness import add_model
    add_model(admin, "parked", enabled=False)
    assert _put(admin, allowed_models=["parked"]).status_code == 200


def test_unroutable_policy_saves_with_a_warning(admin):
    disable_seeded_models(admin)
    r = _put(admin, residency="onprem_only")
    assert r.status_code == 200, r.text
    assert any("no model is currently requestable" in w for w in r.json()["warnings"])


def test_price_that_blocks_agent_turns_warns_on_onboarding(admin, monkeypatch):
    import app.api.admin_routes as ar

    async def ok_probe(*a, **k):
        return True, ""

    monkeypatch.setattr(ar, "probe_endpoint", ok_probe)
    r = admin.post("/admin/infra/models", json={
        "slug": "pricey", "display_name": "pricey", "tier": "cheap",
        "endpoint": "http://test-local/v1", "price_input": 10.0, "price_output": 10.0})
    assert r.status_code == 200, r.text
    assert any("max_cost_usd_per_request" in w for w in r.json()["warnings"])
    cheap = admin.post("/admin/infra/models", json={
        "slug": "free", "display_name": "free", "tier": "cheap",
        "endpoint": "http://test-local/v1", "price_input": 0.0, "price_output": 0.0})
    assert cheap.json()["warnings"] == []


def test_app_warns_when_every_model_is_over_its_cap(admin):
    from tests.onboarding.harness import add_model
    disable_seeded_models(admin)
    add_model(admin, "pricey", price_input=10.0, price_output=10.0)
    r = _put(admin, allowed_models=["pricey"])
    assert r.status_code == 200
    assert any("typical coding-agent turn" in w for w in r.json()["warnings"])


def test_unchanged_stored_illegal_value_does_not_block_an_unrelated_save(admin):
    # The console always sends the whole policy. A value stored before save-time
    # validation existed must not make an unrelated edit (capture_io) fail.
    from tests.onboarding.harness import set_policy_direct
    set_policy_direct(admin, "lint", max_rpm=0, allowed_tiers=[])
    r = _put(admin, max_rpm=0, allowed_tiers=[], capture_io="off")
    assert r.status_code == 200, r.text
    assert r.json()["policy"]["capture_io"] == "off"


def test_changed_illegal_value_is_still_refused(admin):
    from tests.onboarding.harness import set_policy_direct
    set_policy_direct(admin, "lint", max_rpm=0)
    r = _put(admin, max_rpm=-1, capture_io="off")
    assert r.status_code == 422, r.text
    assert "max_rpm" in r.json()["detail"]


def test_stale_stored_model_slug_does_not_block_an_unrelated_save(admin):
    from tests.onboarding.harness import set_policy_direct
    set_policy_direct(admin, "lint", allowed_models=["retired-model"])
    r = _put(admin, allowed_models=["retired-model"], capture_io="off")
    assert r.status_code == 200, r.text


def test_warnings_failure_after_commit_is_not_a_500(admin, monkeypatch):
    import app.apps.policy_lint as pl

    def boom(*a, **k):
        raise RuntimeError("lint exploded")

    monkeypatch.setattr(pl, "policy_warnings", boom)
    r = _put(admin, capture_io="off")
    assert r.status_code == 200, r.text
    assert r.json()["warnings"] == []
