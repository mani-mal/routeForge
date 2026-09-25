from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, field_validator

RISK_CLASSES = ("low", "medium", "high")
RESIDENCIES = ("any", "private_cloud_ok", "onprem_only")

# OpenAI sampling/length knobs a client sets and expects the model to honour.
# Structural keys (model, messages, stream, tools, n) are deliberately absent:
# RouteForge owns those.
FORWARDED_PARAMS = (
    "max_tokens", "max_completion_tokens", "temperature", "top_p", "stop", "seed",
    "presence_penalty", "frequency_penalty", "response_format", "parallel_tool_calls",
)


class Message(BaseModel):
    role: str
    # Optional so an OpenAI tool-calling round-trip parses: an assistant turn
    # that ONLY requested tools carries `content: null` + `tool_calls`, and a
    # `role:"tool"` result carries `content` + `tool_call_id`. Kept a plain
    # str for every ordinary user/assistant turn. Consumers that read raw text
    # (the gate, compression, token counting) MUST coerce `content or ""`.
    content: str | None = None

    @field_validator("content", mode="before")
    @classmethod
    def _flatten_content_parts(cls, v):
        # OpenAI also allows content as a list of parts
        # ([{"type":"text","text":...}], as sent by Qwen Code and other
        # openai-SDK agents). Join the text parts into one string so every
        # consumer -- including the PII gate -- still sees plain text. Non-text
        # parts (images) are dropped; extra keys like cache_control are ignored.
        if isinstance(v, list):
            return "\n".join(
                p.get("text") or "" for p in v
                if isinstance(p, dict) and p.get("type") == "text"
            )
        return v
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
    # serves a single completion that may return `tool_calls` — see
    # Pipeline.run's `tool_mode` branch.
    tools: list[dict] | None = None
    tool_choice: str | dict | None = None
    # Output length caps. Declared (not left to extra="allow") because the
    # router prices a request at this cap: a string/list would 500 there and a
    # negative value would drive the cost estimate below the per-request cap.
    # `ge=1` makes every bad value a clean request-validation 4xx.
    max_tokens: int | None = Field(None, ge=1)
    max_completion_tokens: int | None = Field(None, ge=1)

    def generation_params(self) -> dict:
        """The client's sampling/length parameters, to pass to the model."""
        extra = self.model_extra or {}
        out: dict = {}
        for k in FORWARDED_PARAMS:
            v = getattr(self, k) if k in type(self).model_fields else extra.get(k)
            if v is not None:
                out[k] = v
        return out


class Usage(BaseModel):
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    # Prompt tokens the provider served from its own prompt cache. 0 when the
    # provider does not report the field, or when no provider call happened
    # (a semantic-cache hit). Carried here so the meter in app/api/routes.py
    # can persist it: routes.py reads resp["usage"], never a GenerationResult.
    cached_prompt_tokens: int = 0


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
    # Does the app narrow the model set at all (allowed_tiers/allowed_models)?
    # A UI/reporting signal only. It is NOT an enforcement switch: for years it
    # doubled as one in `Router.select`, which meant an eligibility rule written
    # into `allowed_models` was ignored for every app that set no allowlist.
    # `allowed_models` above is binding either way -- there is no flag, default
    # or otherwise, that can turn eligibility off.
    model_scope_active: bool = False
    # P3b: the app's OWN scope lists, exactly as the app policy stores them
    # (`None` = no constraint, and `None` for a caller with no app policy).
    # Content-free policy values. Read ONLY by `app/policy/feasible.py::
    # Scope.from_context` to report which scope requirement a candidate
    # violates (`allowed_tiers` vs `allowed_models`); enforcement still reads
    # `allowed_models` above, which stays the first-fail authority.
    app_allowed_tiers: frozenset[str] | None = None
    app_allowed_models: frozenset[str] | None = None
    # P3b: the application's OWN resolved residency token (`any` |
    # `private_cloud_ok` | `onprem_only`), BEFORE any sensitivity-rule
    # escalation -- set once in `build_context`. Content-free. Read ONLY by
    # the router's conflicting core to say whether `onprem_only` came from the
    # app (`app_residency`) or was raised by a sensitivity rule
    # (`sensitivity_rule`). Never a placement input: `onprem_only` /
    # `allowed_providers` above are the only placement authority. `None` for a
    # hand-built context (the core then reports `app_residency`).
    app_residency: str | None = None
    # P3b: the entity TYPES of the sensitivity rule that actually RAISED
    # residency above `app_residency` for this request -- the detected types
    # that rule matched on, sorted. `()` when no rule raised residency
    # (escalation off, or the rule's residency is no stricter than the
    # app's). Content-free; explanation only (the core's `residency`
    # source/detail), never a placement input.
    escalation_entities: tuple[str, ...] = ()
    requested_model: str | None = None
    # P2b-3: the client's validated workload slug (`app/apps/outcomes.
    # parse_workload`), content-free. It selects which `model_quality` row the
    # router's quality term reads -- a RANKING input only. Eligibility
    # (`app/policy/feasible.py`), placement and residency never read it.
    workload: str | None = None
    max_cost_usd: float
    max_latency_ms: int
    risk_class: str
    require_verification: bool
    detected_entities: tuple[str, ...]  # TYPES only
    redaction_map_id: str | None
    # Per-app Headroom (step 5) override, resolved at gate time so it travels
    # with the request into the pipeline. None = inherit the live global
    # co_cfg toggle; True/False = force compression on/off for this app.
    # Safety bypasses still apply.
    compression_override: bool | None = None
    # Per-app data-fidelity mode (design P1a §8 / app/policy/compile.py's
    # Protection.content_integrity), resolved at gate time so it travels with
    # the request the same way `compression_override` does. "exact" means
    # step 5 must never transform this app's content -- byte-identical in,
    # byte-identical out; "transformations_allowed" (default) is the ordinary
    # conditional-compression behaviour. A DATA control, never placement or
    # protection: it must never change `onprem_only`, `allowed_providers`,
    # sensitivity, or detection.
    content_integrity: str = "transformations_allowed"
    # Short content hash of the per-app AppPolicyView in force for this request
    # (residency, pii_detection, denylist, score floor, tier/model/provider
    # allowlists). Stamped on every response's `policy` block and on the trace so
    # a consumer can detect a stale probe — a PASS/FAIL taken before a later
    # policy edit. "" when no per-app policy applies. Mirrors
    # Settings.routing_policy_version's empty-string default (upgrade-safe).
    app_policy_version: str = ""
    # --- P1b endpoint data-handling (design §3/§4). The app's COMPILED
    # requirements plus the one request-shaped condition the third rule needs,
    # resolved at gate time so they travel with the request exactly as
    # `content_integrity` does. `app/policy/feasible.py` is the only consumer:
    # it compares these against each endpoint's declarations.
    #
    # They are PLACEMENT-ADJACENT, not placement: they narrow which endpoints
    # this request may reach and can never widen `allowed_providers` or clear
    # `onprem_only` (stage 1 runs first and is untouched by them).
    #
    # Defaults are the no-requirement values, so a context built by a test or
    # an internal path that sets none of them behaves exactly as before.
    allowed_regions: frozenset[str] | None = None   # None = no regional constraint
    require_no_retention: bool = False
    # "This request would send raw sensitive content": the compiled
    # `model_input` is `raw` AND EITHER the gate classified the request above
    # `public` OR the app runs with `detection: off`, in which case nobody
    # classified it and it cannot prove itself public (a `passthrough` app is
    # exactly this shape -- `raw` + `off` -- and would otherwise never arm the
    # rule at all). Computed ONCE, in `app/policy/context.py::build_context`,
    # which is the only place holding both halves -- the rule's condition must
    # exist in exactly one expression. Under `model_input: redact` the model
    # sees placeholders, so this is false and the rule does not fire ("eligible
    # after redaction").
    sends_raw_sensitive: bool = False


class RejectedCandidate(BaseModel):
    name: str
    provider: str
    tier: str
    reason: str
    display_name: str | None = None  # human-facing model name; falls back to name in UI
    # Stable stage identifier (`app/policy/feasible.py`'s `code`, e.g.
    # "residency", "region_not_allowed", "circuit_open"). Every eligibility-
    # stage rejection (all seven feasible_set stages) carries one; None only
    # for the router's own later filters (tier, budget, lower score), which
    # have no stable code of their own. Lets `sp.candidates` answer
    # "why not that endpoint" by code, not just by parsing the operator-facing
    # `reason` string (P1b design §6).
    code: str | None = None


class DecisionRecord(BaseModel):
    step: str
    chosen: str | None
    chosen_display_name: str | None = None  # human-facing name for `chosen`
    chosen_tier: str | None = None       # tier of the chosen candidate (F3 sp.candidates)
    chosen_provider: str | None = None   # provider of the chosen candidate (F3 sp.candidates)
    # P2b-2: the router's FINAL target tier this decision was made for
    # (RoutingDecision.tier_target) -- may differ from chosen_tier under a
    # budget fallback. None on a hop with no router decision behind it (e.g.
    # a step="gateway" escalation-skipped record).
    tier_target: str | None = None
    reason: str
    rejected: list[RejectedCandidate] = Field(default_factory=list)
    est_cost_usd: float = 0.0
    actual_cost_usd: float | None = None
    latency_ms: float = 0.0
    hop_index: int = 0
    tokens_priced: int = 0
    tokens_out: int = 0   # completion tokens for this attempt (baseline pricing, B12)
    quality_score: float | None = None
    quality_source: str | None = None  # customer|calibrated|published|tier_default
    # P2a scoring record: per-eligible-candidate term values and provenance.
    # Ids/enums/numbers only -- emitted regardless of capture_io, like
    # `candidates`. Rejected candidates are absent by design: they were never
    # scored, and inventing terms for them would be exactly the overstatement
    # P2a exists to remove.
    scoring: dict | None = None
    # Step-7 breaker: True when every candidate policy admitted was held open
    # and the router tried one anyway (app/routing/router.py::select).
    circuit_bypass: bool = False


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
    see docs/architecture/specs/semantic-cache-residency-spec.md C6). If a content
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
    # Tool calls the model requested this turn (client tool-calling path).
    # None on the direct path (which never passes tools). Each item is an
    # OpenAI-shape tool_call dict: {id, type, function:{name, arguments}}.
    tool_calls: list[dict] | None = None
    # Prompt tokens the PROVIDER served from its own prompt cache
    # (`usage.prompt_tokens_details.cached_tokens`). 0 when the provider does
    # not report the field, which is indistinguishable from a genuine miss.
    cached_prompt_tokens: int = 0
