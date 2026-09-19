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


# --- P1b Task 6: the three new data-handling rejection codes reach
# sp.candidates (design §6) -- an auditor gets the STABLE code, not just the
# operator-facing reason string, so a consumer can branch on "why not that
# endpoint" without parsing prose.

def test_candidates_json_carries_the_stable_rejection_code():
    rec = {
        "step": "router",
        "chosen": "local-8b",
        "chosen_tier": "cheap",
        "chosen_provider": "onprem",
        "reason": "tier cheap (target cheap)",
        "rejected": [
            {"name": "azure-eu-only", "provider": "azure", "tier": "mid",
             "display_name": "Azure EU", "reason": "excluded · region · "
             "endpoint declares 'eu', application allows 'us'",
             "code": "region_not_allowed"},
            {"name": "retains-prompts", "provider": "azure", "tier": "mid",
             "reason": "excluded · prompt retention · endpoint declares "
             "'retained', application requires 'none'",
             "code": "prompt_retention_not_allowed"},
            {"name": "no-raw-sensitive", "provider": "azure", "tier": "mid",
             "reason": "excluded · raw sensitive content · endpoint declares "
             "accepts_raw_sensitive='false', and this request sends "
             "unredacted sensitive content",
             "code": "raw_sensitive_not_accepted"},
        ],
    }
    out = json.loads(_candidates_json(rec))
    assert _find(out, "azure-eu-only")["code"] == "region_not_allowed"
    assert _find(out, "retains-prompts")["code"] == "prompt_retention_not_allowed"
    assert _find(out, "no-raw-sensitive")["code"] == "raw_sensitive_not_accepted"


def test_candidates_json_code_absent_defaults_to_none():
    """A rejection with no code (e.g. 'over budget', 'below target tier') must
    not fabricate one -- `None` reads honestly as 'no stage-2 code applies'."""
    rec = {"step": "router", "chosen": "local-8b", "chosen_tier": "cheap",
          "chosen_provider": "onprem", "reason": "tier cheap",
          "rejected": [{"name": "x", "provider": "onprem", "tier": "cheap",
                        "reason": "over budget"}]}
    out = json.loads(_candidates_json(rec))
    assert _find(out, "x")["code"] is None


def _find(items, model):
    return next(i for i in items if i["model"] == model)
