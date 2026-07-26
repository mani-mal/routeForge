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


class ExecutionPolicyContext(BaseModel):
    model_config = ConfigDict(frozen=True)
    request_id: str
    tenant_id: str
    sensitivity: str
    onprem_only: bool
    allowed_providers: frozenset[str]
    allowed_models: frozenset[str]
    max_cost_usd: float
    max_latency_ms: int
    risk_class: str
    require_verification: bool
    detected_entities: tuple[str, ...]  # TYPES only
    redaction_map_id: str | None


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
    reason: str
    rejected: list[RejectedCandidate] = Field(default_factory=list)
    est_cost_usd: float = 0.0
    actual_cost_usd: float | None = None
    latency_ms: float = 0.0
    hop_index: int = 0
    tokens_priced: int = 0


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


class GenerationResult(BaseModel):
    model: str
    text: str
    tokens_in: int
    tokens_out: int
    latency_ms: float
    cost_usd: float
    confidence: float
