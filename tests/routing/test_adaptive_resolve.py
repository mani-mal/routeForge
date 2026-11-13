"""B108: `app/routing/adaptive.py` -- the effective per-app advisor mode.

Effective mode = min(deployment ceiling, app setting), ordered
off < shadow < enforce; an app with no setting is capped at `shadow`;
unparseable stored values deny (mode -> off, rate -> 0.0). Sampling is a
deterministic function of the request id.
"""
from __future__ import annotations

import math

import pytest

from app.apps.policy import AppPolicyView
from app.config.loader import load_settings
from app.routing.adaptive import (parse_mode, parse_sample_rate,
                                  resolve_adaptive_routing, sampled)

_S = load_settings()


def _with_ceiling(mode: str):
    sy = _S.routing.switchyard.model_copy(update={"mode": mode})
    return _S.model_copy(update={"routing": _S.routing.model_copy(update={"switchyard": sy})})


# (ceiling, app setting, effective)
_TABLE = [
    ("off", None, "off"), ("off", "off", "off"), ("off", "shadow", "off"),
    ("off", "enforce", "off"),
    ("shadow", None, "shadow"), ("shadow", "off", "off"), ("shadow", "shadow", "shadow"),
    ("shadow", "enforce", "shadow"),
    # The inheritance rule: no setting under an enforce ceiling SHADOWS.
    ("enforce", None, "shadow"), ("enforce", "off", "off"),
    ("enforce", "shadow", "shadow"), ("enforce", "enforce", "enforce"),
]


@pytest.mark.parametrize("ceiling, app_mode, effective", _TABLE)
def test_effective_mode_is_min_of_ceiling_and_app(ceiling, app_mode, effective):
    view = AppPolicyView(adaptive_routing=app_mode)
    assert resolve_adaptive_routing(_with_ceiling(ceiling), view) == (effective, 1.0)


@pytest.mark.parametrize("ceiling", ["off", "shadow", "enforce"])
def test_no_app_policy_inherits_capped_at_shadow(ceiling):
    expected = {"off": "off", "shadow": "shadow", "enforce": "shadow"}[ceiling]
    assert resolve_adaptive_routing(_with_ceiling(ceiling), None) == (expected, 1.0)


@pytest.mark.parametrize("bad", ["ENFORCE", "on", "", 1, True, ["enforce"]])
def test_unparseable_mode_denies_to_off(bad):
    assert parse_mode(bad) == ("off", False)
    view = AppPolicyView(adaptive_routing=bad)
    assert resolve_adaptive_routing(_with_ceiling("enforce"), view)[0] == "off"


@pytest.mark.parametrize("rate, expected", [
    (None, (1.0, True)), (0, (0.0, True)), (1, (1.0, True)), (0.25, (0.25, True)),
    (-0.1, (0.0, False)), (1.01, (0.0, False)), (math.nan, (0.0, False)),
    (math.inf, (0.0, False)), (True, (0.0, False)), ("0.5", (0.0, False)),
])
def test_sample_rate_parse(rate, expected):
    assert parse_sample_rate(rate) == expected


def test_rate_travels_with_the_mode():
    view = AppPolicyView(adaptive_routing="enforce", adaptive_routing_sample_rate=0.3)
    assert resolve_adaptive_routing(_with_ceiling("enforce"), view) == ("enforce", 0.3)


def test_sampling_is_deterministic_and_roughly_proportional():
    ids = [f"req-{i:032x}" for i in range(4000)]
    first = [sampled(r, 0.25) for r in ids]
    assert first == [sampled(r, 0.25) for r in ids]          # deterministic
    share = sum(first) / len(ids)
    assert 0.20 < share < 0.30
    assert all(sampled(r, 1.0) for r in ids[:50])
    assert not any(sampled(r, 0.0) for r in ids[:50])
    # Monotone in the rate: a request sampled at 0.25 is sampled at 0.5.
    assert all(sampled(r, 0.5) for r, s in zip(ids, first) if s)


def test_version_folds_in_only_when_set():
    base = AppPolicyView()
    assert AppPolicyView(adaptive_routing=None).version() == base.version()
    assert AppPolicyView(adaptive_routing="enforce").version() != base.version()
    assert (AppPolicyView(adaptive_routing="enforce", adaptive_routing_sample_rate=0.5)
            .version() != AppPolicyView(adaptive_routing="enforce").version())


def test_not_a_policy_axis():
    """Rule 2a: compiling an app's policy is independent of the setting."""
    from app.policy.compile import compile_policy
    for mode in (None, "off", "shadow", "enforce"):
        for residency in ("any", "onprem_only"):
            plain = compile_policy(AppPolicyView(residency=residency), _S)
            with_mode = compile_policy(
                AppPolicyView(residency=residency, adaptive_routing=mode,
                              adaptive_routing_sample_rate=0.5), _S)
            assert with_mode.placement == plain.placement
            assert with_mode.protection == plain.protection
            assert with_mode.allowed_regions == plain.allowed_regions
            assert with_mode.require_no_retention == plain.require_no_retention
