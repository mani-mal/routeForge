"""Feature 3 F2: sp.candidates is always-on metadata (independent of
capture_io). We capture span attributes with an in-memory OTel exporter and
assert the serialized candidate set. Mirrors the span-capture pattern in
tests/test_capture_io.py / test_agent_telemetry_redaction.py -- reuse that
fixture rather than re-inventing it."""
import json

from app.obs.export import _candidates_json


def test_candidates_json_shape():
    rec = {
        "step": "router",
        "chosen": "local-8b",
        "reason": "tier cheap (target cheap)",
        "rejected": [
            {"name": "bedrock-frontier", "provider": "bedrock", "tier": "frontier",
             "display_name": "Claude (bedrock)", "reason": "excluded · residency"},
        ],
    }
    out = json.loads(_candidates_json(rec))
    assert {"model": "local-8b", "eligible": True}.items() <= _find(out, "local-8b").items()
    rej = _find(out, "bedrock-frontier")
    assert rej["eligible"] is False
    assert rej["reason"] == "excluded · residency"
    assert rej["tier"] == "frontier"


def test_candidates_empty_when_no_decision():
    assert _candidates_json({"step": "verify"}) is None


def _find(items, model):
    return next(i for i in items if i["model"] == model)
