from app.compression import compress
from app.config.loader import load_settings
from app.models import ChatRequest, ExecutionPolicyContext, Message
from app.routing.task_mode import decide

CTX = ExecutionPolicyContext(
    request_id="r", tenant_id="default", sensitivity="public", onprem_only=False,
    allowed_providers=frozenset({"onprem"}), allowed_models=frozenset(),
    max_cost_usd=0.05, max_latency_ms=15000, risk_class="low",
    require_verification=False, detected_entities=(), redaction_map_id=None)


def test_compression_is_passthrough():
    # Default context_optimization.enabled is False (demo default) — disabled
    # bypass is passthrough regardless of message size.
    cfg = load_settings().serving.context_optimization
    msgs = [Message(role="user", content="a" * 400)]
    res = compress(msgs, CTX, cfg)
    assert res.tokens_after == res.tokens_before
    assert res.record.applied is False
    assert res.record.ccr_retrievals_to_cloud == 0


def test_task_mode_is_direct():
    req = ChatRequest(messages=[Message(role="user", content="hi")])
    assert decide(CTX, req) == "direct"


def test_task_mode_direct_on_former_trigger_phrases():
    """"step by step" / "look up" used to divert to the internal NAT agent.
    Agent loops are client-driven now: every call routes direct."""
    for content in ("Solve this step by step: 2+2",
                    "Look up account 42 please"):
        req = ChatRequest(messages=[Message(role="user", content=content)])
        assert decide(CTX, req) == "direct"


def test_task_mode_direct_ignores_mock_multistep_flag():
    """`mock_multistep` was a demo-only hint; ChatRequest has extra="allow",
    so a client that still sends it is simply ignored."""
    req = ChatRequest(messages=[Message(role="user", content="hi")],
                      mock_multistep=True)
    assert decide(CTX, req) == "direct"
