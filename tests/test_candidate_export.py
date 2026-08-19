"""Feature 3 F2: sp.candidates is always-on metadata (independent of
capture_io). These are unit tests of the `_candidates_json` serializer
(app/obs/export.py) called directly with representative DecisionRecord-shaped
dicts -- not span-capture tests."""
import json

from app.obs.export import _candidates_json


def test_candidates_json_shape():
    rec = {
        "step": "router",
        "chosen": "local-8b",
        "chosen_tier": "cheap",
        "chosen_provider": "onprem",
        "reason": "tier cheap (target cheap)",
        "rejected": [
            {"name": "bedrock-frontier", "provider": "bedrock", "tier": "frontier",
             "display_name": "Claude (bedrock)", "reason": "excluded · residency"},
        ],
    }
    out = json.loads(_candidates_json(rec))
    chosen_entry = _find(out, "local-8b")
    assert {"model": "local-8b", "eligible": True}.items() <= chosen_entry.items()
    assert chosen_entry["tier"] == "cheap"
    assert chosen_entry["provider"] == "onprem"
    rej = _find(out, "bedrock-frontier")
    assert rej["eligible"] is False
    assert rej["reason"] == "excluded · residency"
    assert rej["tier"] == "frontier"


def test_candidates_empty_when_no_decision():
    assert _candidates_json({"step": "verify"}) is None


def _find(items, model):
    return next(i for i in items if i["model"] == model)
