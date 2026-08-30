from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

RISK_CLASSES = ("low", "medium", "high")
RESIDENCIES = ("any", "private_cloud_ok", "onprem_only")


class Message(BaseModel):
    role: str
    # Optional so an OpenAI tool-calling round-trip parses: an assistant turn
    # that ONLY requested tools carries `content: null` + `tool_calls`, and a
    # `role:"tool"` result carries `content` + `tool_call_id`. Kept a plain
    # str for every ordinary user/assistant turn. Consumers that read raw text
    # (the gate, compression, token counting) MUST coerce `content or ""`.
    content: str | None = None
    # OpenAI tool-calling fields. Accepted on request messages (so a client can
    # replay an assistant/tool turn back to us) and emitted on the response
    # assistant message. `tool_calls` items are OpenAI-shape dicts:
    # {id, type, function:{name, arguments}}.
    tool_calls: list[dict] | None = None
    tool_call_id: str | None = None   # on a role:"tool" result message
    name: str | None = None           # optional function/tool name on a tool message


class ChatRequest(BaseModel):
    model_config = ConfigDict(extra="allow")
    model: str = "auto"
    messages: list[Message]
    stream: bool = False
    # OpenAI tool-calling inputs. Declared (rather than left to extra="allow")
    # so the pipeline can read them reliably and thread them to the backend;
    # a client that supplies `tools` drives its OWN agent loop, so RouteForge
    # serves a single completion that may return `tool_calls` (it never hands
    # a tools request to its internal NAT agent — see Pipeline.run).
    tools: list[dict] | None = None
    tool_choice: str | dict | None = None


class Usage(BaseModel):
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int


class Choice(BaseModel):
    index: int
    message: Message
    # "stop" for a normal answer; "tool_calls" when the assistant message
    # carries tool_calls (OpenAI clients switch on this to run their tools).
    finish_reason: str = "stop"


class ChatResponse(BaseModel):
    id: str
    object: str = "chat.completion"
    # Unix-epoch seconds. REQUIRED by OpenAI-compatible clients (the openai
    # SDK's ChatCompletion model rejects a response without it) — omitting it
    # was why every turn failed before reaching a tool. Set in routes._finalize.
    created: int = 0
    model: str
    choices: list[Choice]
    usage: Usage
    trace: list[dict] = Field(default_factory=list)


class Entity(BaseModel):
    type: str
    start: int
    end: int
    value: str  # never leaves the gate
    # Optional confidence, set by CustomRecognizer so EffectiveDetector's dedupe
    # can break same-span/same-length ties deterministically (highest wins).
    # Built-in RegexDetector entities leave this None (longest-span-wins only).
    score: float | None = None


class ExecutionPolicyContext(BaseModel):
    model_config = ConfigDict(frozen=True)
    request_id: str
    tenant_id: str
    sensitivity: str
    onprem_only: bool
    allowed_providers: frozenset[str]
    allowed_models: frozenset[str]
    model_scope_active: bool = False
    requested_model: str | None = None
    max_cost_usd: float
    max_latency_ms: int
    risk_class: str
    require_verification: bool
    detected_entities: tuple[str, ...]  # TYPES only
    redaction_map_id: str | None
    # Per-app Headroom (step 5) override, resolved at gate time so it travels
    # with the request into BOTH the direct pipeline and the per-hop agent
    # endpoint. None = inherit the live global co_cfg toggle; True/False =
    # force compression on/off for this app. Safety bypasses still apply.
    compression_override: bool | None = None
    # Short content hash of the per-app AppPolicyView in force for this request
    # (residency, pii_detection, denylist, score floor, tier/model/provider
    # allowlists). Stamped on every response's `policy` block and on the trace so
    # a consumer can detect a stale probe — a PASS/FAIL taken before a later
    # policy edit. "" when no per-app policy applies. Mirrors
    # Settings.routing_policy_version's empty-string default (upgrade-safe).
    app_policy_version: str = ""


class RejectedCandidate(BaseModel):
    name: str
    provider: str
    tier: str
    reason: str
    display_name: str | None = None  # human-facing model name; falls back to name in UI


class DecisionRecord(BaseModel):
    step: str
    chosen: str | None
    chosen_display_name: str | None = None  # human-facing name for `chosen`
    chosen_tier: str | None = None       # tier of the chosen candidate (F3 sp.candidates)
    chosen_provider: str | None = None   # provider of the chosen candidate (F3 sp.candidates)
    reason: str
    rejected: list[RejectedCandidate] = Field(default_factory=list)
    est_cost_usd: float = 0.0
    actual_cost_usd: float | None = None
    latency_ms: float = 0.0
    hop_index: int = 0
    tokens_priced: int = 0
    tokens_out: int = 0   # completion tokens for this hop (multi-step baseline pricing, B12)
    quality_score: float | None = None
    quality_source: str | None = None  # customer|calibrated|published|tier_default


class VerificationRecord(BaseModel):
    step: str = "verify"
    required: bool
    performed: bool
    passed: bool
    entities_found: tuple[str, ...] = ()
    escalated: bool = False
    escalations_used: int = 0
    reason: str = ""
    latency_ms: float = 0.0


class CompressionRecord(BaseModel):
    step: str = "compression"
    enabled: bool
    applied: bool
    bypass_reason: str | None = None
    tokens_before: int
    tokens_after: int
    reduction_pct: float = 0.0
    compressor: str | None = None
    ccr_stored: bool = False
    ccr_retrievals: int = 0
    ccr_retrievals_to_cloud: int = 0  # must be 0 for onprem_only — honesty counter
    latency_ms: float = 0.0


class MemoryRecord(BaseModel):
    """Persistent memory (Feature 5, step-5 extension) trace record —
    counts-only, mirroring CompressionRecord's residency posture. Memory
    contents NEVER leave the process (CLAUDE.md rule 6); this is the only
    thing that reaches a trace (Task 2 appends it). If a content field is
    ever added it MUST go in app/obs/otel.py::REDACT_KEYS."""
    step: str = "memory"
    recalled_count: int = 0
    tokens_recalled: int = 0
    recall_stayed_local: bool = True
    retrievals_to_cloud: int = 0  # must be 0 for onprem_only — honesty counter
class CacheRecord(BaseModel):
    """Semantic-cache trace record (feature #6). COUNTS ONLY -- mirrors
    CompressionRecord. Never a query or response/content field: cache
    contents live only in-process and must never leave it (CLAUDE.md rule 6;
    see docs/architecture/semantic-cache-residency-spec.md C6). If a content
    field is ever added here it MUST also be added to
    app/obs/otel.py::REDACT_KEYS."""
    step: str = "cache"
    enabled: bool
    hit: bool
    bypass_reason: str = ""
    tokens_saved: int = 0
    cache_saved_usd: float = 0.0
    entries_size: int = 0
    blocked_cloud_entries: int = 0  # must stay 0 for onprem_only -- honesty counter
    latency_ms: float = 0.0


class GenerationResult(BaseModel):
    model: str
    text: str
    tokens_in: int
    tokens_out: int
    latency_ms: float
    cost_usd: float
    # None ONLY on the real streaming path (step 8): logprobs are unavailable
    # while streaming, so no confidence can be derived. The non-streaming
    # generate() path always sets a float, and the gateway's confidence-based
    # escalation loop runs only on that non-streaming path -- streaming is a
    # single-shot direct-path call that never escalates -- so a None here is
    # never compared against the escalation threshold.
    confidence: float | None = None
    # Tool calls the model requested this turn (multi-step agent path). None on
    # the direct path (which never passes tools). Each item is an OpenAI-shape
    # tool_call dict: {id, type, function:{name, arguments}}.
    tool_calls: list[dict] | None = None
