"""The candidate routing overlay (`app/eval/candidate.py`; spec §7.1;
invariant E15: nothing a candidate states is silently ignored).
"""
from __future__ import annotations

import copy

import pytest
import yaml

from app.config.loader import load_settings
from app.eval.candidate import (
    ALLOWED_KEYS,
    ARM,
    NOT_EVALUABLE,
    SCHEMA,
    Candidate,
    CandidateError,
    candidate_id,
    candidate_settings,
    load_candidate,
)
from app.eval.endpoints import harness_settings

BASE = load_settings()
HARNESS = harness_settings(BASE, [])


def _write(tmp_path, doc) -> str:
    path = tmp_path / "candidate.yaml"
    path.write_text(doc if isinstance(doc, str) else yaml.safe_dump(doc))
    return str(path)


def _doc(routing, **extra) -> dict:
    return {"schema": SCHEMA, "routing": routing, **extra}


def _cand(routing) -> Candidate:
    return Candidate(name=None, routing=routing, id=candidate_id(routing))


# --- loading --------------------------------------------------------------------

def test_a_candidate_loads_with_its_name_and_id(tmp_path):
    cand = load_candidate(_write(tmp_path, _doc({"weights": {"latency": 0.6}},
                                                name="latency-heavy")))
    assert cand.name == "latency-heavy"
    assert cand.routing == {"weights": {"latency": 0.6}}
    assert cand.id == candidate_id({"weights": {"latency": 0.6}}) and len(cand.id) == 12
    assert ARM == "candidate"


def test_the_id_is_of_the_overlay_not_of_the_files_formatting(tmp_path):
    a = load_candidate(_write(tmp_path, _doc(
        {"weights": {"latency": 0.6, "cost": 0.1}}, name="a")))
    b = load_candidate(_write(tmp_path, (
        f"# a comment\nname: b\nschema: {SCHEMA}\n"
        "routing:\n  weights:\n    cost: 0.1\n    latency: 0.6\n")))
    c = load_candidate(_write(tmp_path, _doc({"weights": {"latency": 0.7, "cost": 0.1}})))
    assert a.id == b.id != c.id


@pytest.mark.parametrize("doc, where", [
    ("- a\n- b\n", "must be a mapping"),
    ("{{{", "invalid YAML"),
    ({"schema": "routeforge.router-candidate/v2", "routing": {"strategy": "default"}},
     "schema"),
    ({"routing": {"strategy": "default"}}, "schema"),
    ({"schema": SCHEMA}, "routing"),
    ({"schema": SCHEMA, "routing": {}}, "routing"),
    ({"schema": SCHEMA, "routing": ["weights"]}, "routing"),
    ({"schema": SCHEMA, "routing": {"strategy": "default"}, "apply": True}, "unknown key"),
    ({"schema": SCHEMA, "routing": {"strategy": "default"}, "name": ""}, "name"),
    ({"schema": SCHEMA, "routing": {"strategy": "default"}, "name": 7}, "name"),
])
def test_e15_a_malformed_file_is_refused_naming_the_location(tmp_path, doc, where):
    with pytest.raises(CandidateError, match=where):
        load_candidate(_write(tmp_path, doc))


def test_a_missing_file_is_an_os_error(tmp_path):
    with pytest.raises(OSError):
        load_candidate(str(tmp_path / "absent.yaml"))


@pytest.mark.parametrize("key", sorted(NOT_EVALUABLE))
def test_e15_a_key_the_bench_cannot_evaluate_is_refused_with_the_reason(tmp_path, key):
    """Never ignored: a candidate that silently changed nothing would read
    as "no regression"."""
    with pytest.raises(CandidateError) as exc:
        load_candidate(_write(tmp_path, _doc({key: 1})))
    assert f"routing.{key}" in str(exc.value)
    assert "cannot be evaluated by this check" in str(exc.value)
    assert NOT_EVALUABLE[key] in str(exc.value)


def test_e15_an_unknown_routing_key_is_refused(tmp_path):
    with pytest.raises(CandidateError, match="routing.wieghts"):
        load_candidate(_write(tmp_path, _doc({"wieghts": {"latency": 1.0}})))


def test_the_allowed_and_refused_keys_cover_routing_yaml_exactly():
    """A new `routing.yaml` key must be put in one list or the other."""
    assert set(ALLOWED_KEYS) | set(NOT_EVALUABLE) == set(BASE.routing.model_dump())
    assert not set(ALLOWED_KEYS) & set(NOT_EVALUABLE)


# --- applying it in memory ------------------------------------------------------

def test_a_mapping_is_merged_one_level_and_a_scalar_replaces():
    before = copy.deepcopy(HARNESS.routing.model_dump())
    settings = candidate_settings(HARNESS, _cand({
        "weights": {"latency": 0.6},
        "complexity_thresholds": {"cheap_max": 0.25},
        "breaker": {"enabled": False},
        "strategy": "default"}))
    assert settings.routing.weights == {**before["weights"], "latency": 0.6}
    assert settings.routing.complexity_thresholds == {
        **before["complexity_thresholds"], "cheap_max": 0.25}
    assert settings.routing.breaker.enabled is False
    assert settings.routing.breaker.cooldown_s == before["breaker"]["cooldown_s"]
    # Everything the candidate did not state is the harness's.
    after = settings.routing.model_dump()
    for key in set(before) - {"weights", "complexity_thresholds", "breaker"}:
        assert after[key] == before[key], key


def test_the_harness_settings_are_never_mutated():
    before = copy.deepcopy(HARNESS.model_dump())
    candidate_settings(HARNESS, _cand({"weights": {"latency": 9.0},
                                       "signals": {"enabled": False}}))
    assert HARNESS.model_dump() == before


def test_restating_the_base_values_changes_nothing():
    routing = {"weights": dict(HARNESS.routing.weights),
               "tier_quality": dict(HARNESS.routing.tier_quality)}
    assert candidate_settings(HARNESS, _cand(routing)).model_dump() == HARNESS.model_dump()


@pytest.mark.parametrize("routing, where", [
    ({"weights": 3}, "routing.weights"),                       # a mapping key given a scalar
    ({"weights": {"latency": "fast"}}, "routing"),             # RoutingCfg validation
    ({"strategy": "no-such-strategy"}, "routing.strategy"),    # the strategy registry
    ({"risk_floors": {"high": "strong"}}, "config lint"),      # there is no `strong` tier
    ({"breaker": {"failure_threshold": "many"}}, "routing"),
])
def test_e15_a_candidate_the_deployment_could_not_boot_with_is_refused(routing, where):
    with pytest.raises(CandidateError, match=where):
        candidate_settings(HARNESS, _cand(routing))


def test_the_legacy_capacity_weight_is_renamed_like_at_boot():
    settings = candidate_settings(HARNESS, _cand({"weights": {"capacity": 0.9}}))
    assert "capacity" not in settings.routing.weights


def test_nothing_is_written_next_to_the_candidate_file(tmp_path):
    path = _write(tmp_path, _doc({"weights": {"latency": 0.6}}))
    before = (tmp_path / "candidate.yaml").read_bytes()
    candidate_settings(HARNESS, load_candidate(path))
    assert sorted(p.name for p in tmp_path.iterdir()) == ["candidate.yaml"]
    assert (tmp_path / "candidate.yaml").read_bytes() == before
