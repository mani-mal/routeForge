"""B103: result types and the lazy-import contract. No Switchyard needed."""

import builtins
import subprocess
import sys

import pytest

from app.config.schema import SwitchyardCfg
from app.routing.switchyard import (
    ROLE_MODELS,
    AdvisorResult,
    SwitchyardAdvisor,
    SwitchyardProposal,
    SwitchyardUnavailable,
)


def _proposal(**kw):
    base = dict(
        role="efficient", tier="cheap", confidence=0.87,
        evidence={"source": "dimensions", "confidence": 0.87, "threshold": 0.5},
        latency_ms=0.4, version="0.4.0a1", algorithm="stage_router",
    )
    return SwitchyardProposal(**{**base, **kw})


def test_role_models_are_placeholders_only():
    assert ROLE_MODELS == {
        "efficient": ["efficient"],
        "capable": ["capable"],
        "any": ["efficient", "capable"],
    }


def test_result_needs_exactly_one_of_proposal_or_fallback():
    with pytest.raises(ValueError):
        AdvisorResult(latency_ms=1.0)
    with pytest.raises(ValueError):
        AdvisorResult(latency_ms=1.0, proposal=_proposal(), fallback_cause="timeout")


def test_results_are_frozen():
    r = AdvisorResult.fallback("timeout", 1.0)
    with pytest.raises(AttributeError):
        r.fallback_cause = "error"
    with pytest.raises(AttributeError):
        _proposal().role = "capable"


def test_proposal_record_is_flat_ids_enums_and_numbers():
    rec = AdvisorResult(latency_ms=0.5, proposal=_proposal()).to_record()
    assert rec == {
        "role": "efficient", "tier": "cheap", "confidence": 0.87,
        "evidence": {"source": "dimensions", "confidence": 0.87, "threshold": 0.5},
        "version": "0.4.0a1", "algorithm": "stage_router",
        "fallback_cause": None, "latency_ms": 0.5,
    }


def test_fallback_record():
    rec = AdvisorResult.fallback("unconvertible", 0.2).to_record()
    assert rec["fallback_cause"] == "unconvertible"
    assert rec["role"] is None and rec["tier"] is None and rec["evidence"] == {}
    assert rec["latency_ms"] == 0.2


def test_package_import_does_not_import_upstream():
    code = (
        "import sys; import app.routing.switchyard as s; "
        "assert s.SwitchyardAdvisor; "
        "assert 'switchyard' not in sys.modules and 'switchyard_rust' not in sys.modules"
    )
    subprocess.run([sys.executable, "-c", code], check=True)


def test_constructing_without_the_extra_raises_a_clear_error(monkeypatch):
    real_import = builtins.__import__

    def no_switchyard(name, *args, **kwargs):
        if name.split(".")[0] in {"switchyard", "switchyard_rust"}:
            raise ImportError(f"No module named {name!r}")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", no_switchyard)
    with pytest.raises(SwitchyardUnavailable, match="switchyard` extra"):
        SwitchyardAdvisor(SwitchyardCfg(mode="shadow"))


@pytest.mark.parametrize(
    "cfg", [SwitchyardCfg(algorithm="random"), SwitchyardCfg(classifier={"target": "judge"})]
)
def test_constructor_refuses_anything_but_classifierless_stage_router(cfg):
    with pytest.raises(ValueError):
        SwitchyardAdvisor(cfg)
