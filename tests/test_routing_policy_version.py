import copy

from app.config.loader import _policy_version, load_settings

S = load_settings()


def test_settings_carries_a_policy_version():
    assert isinstance(S.routing_policy_version, str)
    assert len(S.routing_policy_version) == 12


def test_version_stable_across_loads():
    assert load_settings().routing_policy_version == \
        load_settings().routing_policy_version


def test_version_changes_when_routing_changes():
    routing = {"weights": {"cost": 0.6}}
    pricing = {"baseline_model": "x"}
    v1 = _policy_version(routing, pricing)
    routing2 = copy.deepcopy(routing)
    routing2["weights"]["cost"] = 0.7
    v2 = _policy_version(routing2, pricing)
    assert v1 != v2


def test_version_ignores_serving_and_policy_files():
    # Only routing + pricing feed the hash. Same routing/pricing -> same hash,
    # regardless of what serving.yaml/policy.yaml contain.
    routing = {"a": 1}
    pricing = {"b": 2}
    assert _policy_version(routing, pricing) == _policy_version(dict(routing), dict(pricing))
