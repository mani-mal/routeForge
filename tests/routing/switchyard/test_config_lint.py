"""B100 lint codes. `installed_version` is monkeypatched; the package is never needed."""

import copy

import pytest

from app.config.lint import lint_settings
from app.config.loader import load_settings
from app.config.schema import SwitchyardCfg
from app.routing import switchyard as sw


def _settings(**block):
    s = copy.deepcopy(load_settings())
    s.routing.switchyard = SwitchyardCfg(**block)
    return s


def _codes(s):
    return [e.split()[0] for e in lint_settings(s)]


def _installed(monkeypatch, version):
    monkeypatch.setattr(sw, "installed_version", lambda: version)


def test_default_block_is_off_and_clean(monkeypatch):
    _installed(monkeypatch, None)
    s = load_settings()
    assert s.routing.switchyard.mode == "off"
    assert lint_settings(s) == []


def test_mode_off_checks_nothing_even_with_bad_fields(monkeypatch):
    _installed(monkeypatch, "0.3.0")
    s = _settings(mode="off", algorithm="other", classifier={"x": 1})
    assert not [c for c in _codes(s) if c.startswith("SWITCHYARD_")]


def test_yaml_bare_off_boolean_means_off():
    assert SwitchyardCfg(mode=False).mode == "off"


@pytest.mark.parametrize("mode", ["shadow", "enforce"])
def test_not_installed(monkeypatch, mode):
    _installed(monkeypatch, None)
    assert "SWITCHYARD_NOT_INSTALLED" in _codes(_settings(mode=mode))


def test_version_unsupported(monkeypatch):
    _installed(monkeypatch, "0.3.0")
    codes = _codes(_settings(mode="shadow"))
    assert "SWITCHYARD_VERSION_UNSUPPORTED" in codes
    assert "SWITCHYARD_NOT_INSTALLED" not in codes


def test_supported_version_is_clean(monkeypatch):
    _installed(monkeypatch, "0.4.0a1")
    assert lint_settings(_settings(mode="shadow")) == []


def test_unknown_algorithm(monkeypatch):
    _installed(monkeypatch, "0.4.0a1")
    errs = lint_settings(_settings(mode="shadow", algorithm="bandit"))
    assert [e for e in errs if e.startswith("SWITCHYARD_ALGORITHM_UNKNOWN")]


def test_classifier_not_allowed_until_b111(monkeypatch):
    _installed(monkeypatch, "0.4.0a1")
    errs = lint_settings(_settings(mode="enforce", classifier={"kind": "x"}))
    msg = [e for e in errs if e.startswith("SWITCHYARD_ALGORITHM_UNKNOWN")]
    assert msg and "B111" in msg[0]


def test_field_bounds():
    with pytest.raises(ValueError):
        SwitchyardCfg(confidence_threshold=1.5)
    with pytest.raises(ValueError):
        SwitchyardCfg(timeout_ms=0)
    with pytest.raises(ValueError):
        SwitchyardCfg(picker="nope")


def test_capable_tier_defaults_to_frontier_and_accepts_mid_only():
    """B103 / design §3: `capable` maps to frontier unless capped at mid."""
    assert SwitchyardCfg().capable_tier == "frontier"
    assert SwitchyardCfg(capable_tier="mid").capable_tier == "mid"
    for bad in ("cheap", "strong", ""):
        with pytest.raises(ValueError):
            SwitchyardCfg(capable_tier=bad)


def test_switchyard_key_is_routing_owned():
    from app.config.lint import OWNED_KEYS
    assert "switchyard" in OWNED_KEYS["routing"]
    assert "switchyard" not in OWNED_KEYS["serving"] | OWNED_KEYS["policy"]


def test_switchyard_key_in_wrong_file_is_flagged():
    s = copy.deepcopy(load_settings())
    s.config_keys = {**s.config_keys, "policy": [*s.config_keys.get("policy", []), "switchyard"]}
    assert any(e.startswith("CONCERN_IN_WRONG_FILE 'switchyard'") for e in lint_settings(s))


def test_switchyard_key_in_routing_is_not_flagged():
    s = copy.deepcopy(load_settings())
    s.config_keys = {**s.config_keys, "routing": [*s.config_keys.get("routing", []), "switchyard"]}
    assert lint_settings(s) == []
