"""B74: structural redaction keeps floats intact, still redacts strings."""
import json

import pytest

from app.obs import export as export_mod
from app.obs import otel
from app.obs.redact import redact_tree

from tests.test_decision_certificate import _cert_sample, mem_provider  # noqa: F401


def test_floats_survive_round_trip():
    obj = {"score": -0.1234567890123456, "ms": 55646.12345678901, "n": 3939}
    dumped = json.dumps(redact_tree(obj))
    assert "<" not in dumped
    assert json.loads(dumped) == obj


def test_string_leaf_and_key_redacted():
    out = redact_tree({"reason": "call 123-45-6789", "k 123-45-6789": [("x",)]})
    assert "<US_SSN" in out["reason"]
    assert "123-45-6789" not in json.dumps(out)
    assert [v for k, v in out.items() if k != "reason"] == [[["x"]]]


def test_export_scoring_floats_intact(mem_provider):  # noqa: F811
    scoring = {"candidates": [{"name": "m", "score": -0.1234567890123456,
                               "latency": {"ms": 55646.12345678901, "n": 3939}}]}
    kw = _cert_sample()
    kw["trace"][0]["scoring"] = scoring
    export_mod.export_trace(**kw)
    otel._provider.force_flush()
    attrs = [s.attributes for s in mem_provider.get_finished_spans()
             if "sp.scoring" in s.attributes]
    assert attrs
    raw = attrs[0]["sp.scoring"]
    assert "<" not in raw
    assert json.loads(raw) == scoring
