"""Phase C: real model names (display_name) threaded end-to-end.

The routing pool `name` stays the stable slug (pricing/tier/vLLM served-model-id
key on it), but the UI must show the human-facing model name. This asserts the
display name reaches the response, the per-step router record, and rejected
candidates — without ever losing the slug.
"""
import asyncio

from app.compression.ccr import CCRStore
from app.config.loader import load_settings
from app.gateway.gateway import Gateway
from app.models import ChatRequest, Message
from app.pipeline import Pipeline
from app.policy.detectors import make_detector
from app.policy.gate import Gate
from app.routing.router import Router
from tests._litellm_fake import _DeterministicLiteLLM, make_test_registry


def _pipeline(settings):
    det = make_detector("regex")
    co = settings.serving.context_optimization
    registry = make_test_registry(settings)
    return Pipeline(settings, Gate(settings, det, registry=registry), det,
                    Gateway(settings, _DeterministicLiteLLM(settings),
                            Router(settings, registry=registry), registry),
                    co_cfg_provider=lambda: co,
                    ccr=CCRStore(3600), registry=registry)


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
    # B10: a disabled cloud provider now never enters the candidate pool at
    # all (see app/apps/registry.py effective_pool), so with the default
    # config (azure/bedrock disabled) they no longer appear even as a
    # rejected "residency" spur. Enable bedrock here so a real cloud
    # candidate reaches the router and gets legitimately rejected (by
    # residency, since this tenant's request is onprem_only-free but the
    # complexity/risk still lands on the cheap on-prem tier) -- proving
    # display_name still threads onto the rejected-candidate trace entry.
    s = load_settings()
    prov = dict(s.serving.providers)
    prov["bedrock"] = prov["bedrock"].model_copy(update={"enabled": True})
    s = s.model_copy(update={"serving": s.serving.model_copy(update={"providers": prov})})
    pipe = _pipeline(s)
    out = asyncio.run(pipe.run(
        "req-dn2", "acme",
        ChatRequest(messages=[Message(role="user", content="Hello there")])))

    rejected = [c for r in out["trace"] for c in r.get("rejected", [])]
    by_name = {c["name"]: c.get("display_name") for c in rejected}
    assert by_name.get("bedrock-frontier") == "Claude Sonnet (Bedrock)"


def test_display_name_falls_back_to_slug_when_unset():
    # A pool member with no display_name must fall back to its slug, never
    # blank. On-prem candidates now come from onboarded rows (there is no
    # built-in on-prem YAML pool member any more), so this seeds its own
    # onboarded row with an empty display_name rather than using the shared
    # make_test_registry seed (which always sets one).
    from app.apps import store
    from app.apps.db import Database
    from app.apps.registry import ModelRegistry

    s = load_settings()

    async def _build_registry():
        db = Database(url="sqlite+aiosqlite://")
        await db.ensure_ready()
        async with db.session() as session:
            await store.upsert_onboarded(
                session, "local-8b", display_name="", provider="onprem",
                tier="cheap", endpoint="http://test-local/v1", api_key_env=None,
                price_input=0.05, price_output=0.05, enabled=True,
                probe_status="healthy", allowed_app_ids=None)
            await session.commit()
        reg = ModelRegistry(s)
        await reg.refresh(db)
        return reg

    registry = asyncio.run(_build_registry())
    det = make_detector("regex")
    co = s.serving.context_optimization
    pipe = Pipeline(
        s, Gate(s, det, registry=registry), det,
        Gateway(s, _DeterministicLiteLLM(s), Router(s, registry=registry), registry),
        co_cfg_provider=lambda: co, ccr=CCRStore(3600), registry=registry)

    out = asyncio.run(pipe.run(
        "req-dn3", "acme",
        ChatRequest(messages=[Message(role="user", content="Hi")])))
    assert out["model"] == "local-8b"
    assert out["model_display"] == "local-8b"
