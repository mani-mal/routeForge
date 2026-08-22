from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

RISK_CLASSES = ("low", "medium", "high")
RESIDENCIES = ("any", "private_cloud_ok", "onprem_only")


class Message(BaseModel):
    role: str
    content: str


class ChatRequest(BaseModel):
    model_config = ConfigDict(extra="allow")
    model: str = "auto"
    messages: list[Message]
    stream: bool = False


class Usage(BaseModel):
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int


class Choice(BaseModel):
    index: int
    message: Message
    finish_reason: str = "stop"


class ChatResponse(BaseModel):
    id: str
    object: str = "chat.completion"
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
