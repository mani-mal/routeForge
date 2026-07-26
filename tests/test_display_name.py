"""Phase C: real model names (display_name) threaded end-to-end.

The routing pool `name` stays the stable slug (pricing/tier/vLLM served-model-id
key on it), but the UI must show the human-facing model name. This asserts the
display name reaches the response, the per-step router record, and rejected
candidates — without ever losing the slug.
"""
import asyncio

from app.compression.ccr import CCRStore
from app.config.loader import load_settings
from app.gateway.backends.mock import MockBackend
from app.gateway.gateway import Gateway
from app.models import ChatRequest, Message
from app.pipeline import Pipeline
from app.policy.detectors import make_detector
from app.policy.gate import Gate
from app.routing.router import Router


def _pipeline(settings):
    det = make_detector("regex")
    co = settings.serving.context_optimization
    return Pipeline(settings, Gate(settings, det), det,
                    Gateway(settings, MockBackend(settings), Router(settings)),
                    self_port=8080, co_cfg_provider=lambda: co,
                    ccr=CCRStore(3600))


def test_direct_response_carries_display_name():
    s = load_settings()
    pipe = _pipeline(s)
    out = asyncio.run(pipe.run(
        "req-dn", "acme",
        ChatRequest(messages=[Message(role="user", content="What is 2 + 2?")])))

    # slug preserved for joins; display name is the human-facing string
    assert out["model"] == "local-8b"
    assert out["model_display"] == "Qwen2.5-7B-Instruct (local)"

    router_recs = [r for r in out["trace"] if r["step"] == "router"]
    assert router_recs, "expected a router decision record"
    assert router_recs[0]["chosen"] == "local-8b"
    assert router_recs[0]["chosen_display_name"] == "Qwen2.5-7B-Instruct (local)"


def test_rejected_candidates_carry_display_name():
    # Default config disables cloud providers, so azure/bedrock are rejected by
    # the residency filter — each rejected candidate must still carry its
    # display name so the UI can show a real name greyed out.
    s = load_settings()
    pipe = _pipeline(s)
    out = asyncio.run(pipe.run(
        "req-dn2", "acme",
        ChatRequest(messages=[Message(role="user", content="Hello there")])))

    rejected = [c for r in out["trace"] for c in r.get("rejected", [])]
    by_name = {c["name"]: c.get("display_name") for c in rejected}
    assert by_name.get("bedrock-frontier") == "Claude Sonnet (Bedrock)"


def test_display_name_falls_back_to_slug_when_unset():
    # A pool member with no display_name must fall back to its slug, never blank.
    s = load_settings()
    pool = [c.model_copy(update={"display_name": None}) for c in s.routing.model_pool]
    routing = s.routing.model_copy(update={"model_pool": pool})
    s = s.model_copy(update={"routing": routing})
    pipe = _pipeline(s)
    out = asyncio.run(pipe.run(
        "req-dn3", "acme",
        ChatRequest(messages=[Message(role="user", content="Hi")])))
    assert out["model"] == "local-8b"
    assert out["model_display"] == "local-8b"
