from app.apps.policy import AppPolicyView
from app.config.loader import load_settings
from app.policy.context import build_context
from tests._litellm_fake import make_test_registry
from tests.onboarding.clients import qwen_code_turn
from tests.onboarding.harness import (
    add_model, conformance_client, disable_seeded_models, onboard, set_policy_direct,
)
from tests.test_admin_api import _login


def _ctx(view):
    s = load_settings()
    return s, build_context(s, "r", "acme", [], None, app_policy=view,
                            registry=make_test_registry(s))


def test_cap_defaults_to_deployment_value():
    s, ctx = _ctx(AppPolicyView())
    assert ctx.max_cost_usd == s.policy.budgets["default_max_spend_usd"]


def test_cap_overrides_deployment_value():
    _, ctx = _ctx(AppPolicyView(max_cost_usd_per_request=1.5))
    assert ctx.max_cost_usd == 1.5


def test_unset_cap_leaves_policy_version_unchanged_and_set_cap_moves_it():
    assert AppPolicyView(max_cost_usd_per_request=None).version() == AppPolicyView().version()
    assert AppPolicyView(max_cost_usd_per_request=1.0).version() != AppPolicyView().version()


def test_admin_put_validates_and_round_trips(client):
    _login(client)
    client.post("/admin/applications", json={"id": "capapp", "name": "capapp"})
    bad = client.put("/admin/applications/capapp/policy", json={"max_cost_usd_per_request": 0})
    assert bad.status_code == 422 and "max_cost_usd_per_request" in bad.json()["detail"]
    ok = client.put("/admin/applications/capapp/policy", json={"max_cost_usd_per_request": 1.0})
    assert ok.status_code == 200, ok.text
    assert ok.json()["policy"]["max_cost_usd_per_request"] == 1.0


def test_per_app_cap_unblocks_a_large_agent_turn(_fake_acompletion, monkeypatch):
    from tests.onboarding.harness import install_dual_fake
    install_dual_fake(monkeypatch)
    turn = {**qwen_code_turn(), "stream": False}
    with conformance_client() as c:
        disable_seeded_models(c)
        add_model(c, "conf-model", price_input=10.0, price_output=10.0)
        app = onboard(c, "cap-agent")
        assert app.chat(turn).status_code == 422
        set_policy_direct(c, "cap-agent", max_cost_usd_per_request=1.0)
        r = app.chat(turn)
    assert r.status_code == 200, r.text
