from pydantic import BaseModel, Field

from app.prompts.store import PromptArtifact


class PoolCandidate(BaseModel):
    name: str
    provider: str
    tier: str
    # Human-facing model name shown in the UI (e.g. "Qwen2.5-7B-Instruct (local)").
    # `name` stays the stable slug that pricing/tier lookups and the served vLLM
    # model id key on — never render `name` to customers, never route on this.
    display_name: str | None = None
    # Downstream LiteLLM model id when it differs from the routing slug (cloud
    # deployment/model names). Defaults to f"{provider_prefix}/{name}" in the
    # backend. onprem's served-model-name == `name`, so it needs no override.
    litellm_model: str | None = None
    # Published quality snapshot in [0,1] (e.g. normalized Artificial Analysis
    # Intelligence Index / 100). Optional: unset means the resolver falls to the
    # tier_default rung. Populating this is an onboarding activity (spec §9/§13);
    # the shipped demo leaves it unset so routing stays a tier-default regression.
    quality: float | None = None


class PromptOptCfg(BaseModel):
    """Feature 3 (DSPy prompt-optimization) Task 1: managed-prompt substrate.
    Home for the step-4 router's classifier pin today, and for any future
    routed-prompt pin (verify/judge, etc.) -- one block, owned here (routing
    input), never restated in another YAML. `enabled` is OFF here on purpose:
    Task 2 wires the LLM classifier that reads it; this task activates
    nothing."""
    enabled: bool = False           # master switch (Task 2 LLM classifier reads this)
    artifacts_dir: str = "config/prompts"
    active: dict[str, str] = {}     # prompt_id -> pinned label (the human-gate pin)


class RoutingCfg(BaseModel):
    tiers: list[str]
    score_on: str
    weights: dict[str, float]
    # Tier-default rung of the quality ladder — the fallback quality for any
    # model with no published/calibrated/customer score. Promoted from the old
    # hardcoded router `_Q`. One value per canonical tier (config lint enforces
    # full coverage and the [0,1] range). Owned here (routing input), never in
    # pricing.yaml.
    tier_quality: dict[str, float]
    complexity_thresholds: dict[str, float]
    confidence_escalation_threshold: float
    # Softening applied to a real backend's mean-token-logprob before it becomes
    # a [0,1] confidence: confidence = exp(mean_logprob / T). T=1 is the raw
    # geometric-mean token probability (systematically ~0.4-0.6 even for fluent
    # answers, so it trips the escalation threshold on nearly every response —
    # see routeforge-router-targets-mid). T>1 calibrates it so only genuinely
    # high-perplexity answers fall below the threshold. Ignored by the mock
    # backend (its confidence is seeded/persona-driven, not logprob-derived).
    confidence_logprob_temperature: float = 1.0
    max_escalations: int
    # Failure-driven lateral model switch: on a retryable backend failure the
    # gateway excludes the failed model and re-selects -- a same-tier peer first,
    # escalating only when the tier empties. This bounds those retries. 0 makes a
    # single failure terminal (no lateral retry). Owned here (routing input).
    max_failover_attempts: int = 2
    risk_floors: dict[str, str]
    model_pool: list[PoolCandidate]
    fallback: dict[str, int]
    prompt_opt: PromptOptCfg = PromptOptCfg()
    # Step-6 scoring strategy name (app/routing/strategy.py's `_STRATEGIES`
    # registry). Owned here only -- the config lint rejects an unknown name at
    # boot (never a silent fallback to "default").
    strategy: str = "default"


class SensitivityRule(BaseModel):
    entities: list[str]
    sensitivity: str
    residency: str
    risk_class: str
    require_verification: bool


class TenantCfg(BaseModel):
    display_name: str
    default_residency: str


class PolicyCfg(BaseModel):
    sensitivity_scale: list[str]
    default_sensitivity: str
    sensitivity_rules: list[SensitivityRule]
    budgets: dict[str, float]
    tenants: dict[str, TenantCfg]


class PriceCfg(BaseModel):
    input: float
    output: float
    tier: str
    provider: str


class PricingCfg(BaseModel):
    models: dict[str, PriceCfg]
    baseline_model: str


class ProviderCfg(BaseModel):
    enabled: bool
    # LiteLLM connection (transport owner: serving.yaml). `litellm_prefix` is the
    # LiteLLM provider route (openai | azure | bedrock). The *_env fields name the
    # environment variables holding creds/endpoints (values never in config —
    # secrets via .env only). onprem derives its api_base per-model from the
    # ModelRegistry (onboarded at runtime — routeForge deploys nothing), so it
    # needs only a key env; bedrock reads standard AWS_* env, so it needs neither.
    litellm_prefix: str | None = None
    api_base_env: str | None = None
    api_key_env: str | None = None
    api_version_env: str | None = None


class LangfuseCfg(BaseModel):
    enabled: bool
    host: str  # in-cluster URL for OTLP export (e.g. http://langfuse-web:3000)
    redact_on_export: bool
    # Eval-capture switch for request/response I/O attached to the serving.request
    # root span (docs/archive/eval-logging-backlog.md; current contract in
    # docs/reference/per-app-config.md). `off` = today's behaviour (no I/O
    # captured — the demo default, keeps rule 2 intact). `redacted` = attach
    # redact()'d prompt/completion (entity VALUES masked to <TYPE>; structure kept,
    # eval-usable, no raw PII). `full` = raw prompt/completion for eval — gated by
    # the config lint to a residency-local Langfuse (rejected when any tenant is
    # onprem_only; see app/config/lint.py) and NOT redacted on egress for the I/O
    # keys (see app/obs/otel.py _capture_io_full). Owned here (observability infra).
    capture_io: str = "off"  # off | redacted | full
    project_id: str = "demo-project"  # for building trace deep-link URLs for the UI
    # Browser-reachable Langfuse URL for the UI deep-link (e.g. a NodePort/ingress).
    # `host` is in-cluster and unreachable from the user's browser; when this is unset
    # the deep-link falls back to `host`.
    public_host: str | None = None
    public_key: str | None = None
    secret_key: str | None = None


class ObservabilityCfg(BaseModel):
    langfuse: LangfuseCfg


class BypassCfg(BaseModel):
    high_risk: bool
    restricted_sensitivity: bool
    user_prompt: bool

class CCRCfg(BaseModel):
    enabled: bool
    residency: str
    ttl_hours: int
    export_to_telemetry: bool

class ContextOptimizationCfg(BaseModel):
    enabled: bool
    mode: str
    min_tokens: int
    bypass_on: BypassCfg
    ccr: CCRCfg
    cache_alignment: bool


class MemoryCfg(BaseModel):
    """Feature 5 (Task 1) — persistent memory substrate config. CCR-class
    residency posture (CLAUDE.md rule 6): local-only, enforced TTL, never
    exported. Default OFF; Task 1 adds NO pipeline wiring, so flipping this
    to true still has no effect on a request until Task 2 wires read/write
    in. See docs/architecture/specs/persistent-memory-residency-spec.md."""
    enabled: bool = False
    residency: str = "local_only"
    ttl_hours: int = 24
    export_to_telemetry: bool = False
    max_recall: int = 3          # most-recent-first cap per tenant partition
    min_tokens: int = 0          # small-request bypass knob (like compression's min_tokens)
    # Reuses the compression BypassCfg shape for parity — `user_prompt` has no
    # meaning for memory recall (no "live prompt" to protect) and is unused
    # until Task 2 wires bypass logic in; kept for one coherent bypass shape
    # across the two step-5 levers rather than inventing a second one.
    bypass_on: BypassCfg = BypassCfg(
        high_risk=True, restricted_sensitivity=True, user_prompt=False)
class SemanticCacheCfg(BaseModel):
    """Feature #6 -- infra switch only, mirrors context_optimization's
    placement/shape. Default OFF (C7): with enabled=false nothing in the
    pipeline constructs a DeterministicSemanticCache (Task 1 wires no
    pipeline code at all). See
    docs/architecture/specs/semantic-cache-residency-spec.md."""
    enabled: bool = False
    residency: str = "local_only"     # lint-enforced when enabled -- CLAUDE.md rule 6
    ttl_hours: int = 24                # lint-enforced > 0 when enabled
    export_to_telemetry: bool = False  # lint-enforced when enabled
    max_entries: int = 0               # 0 = unbounded beyond TTL (a size cap is optional)
    # Reuses BypassCfg's shape for config-shape parity with
    # context_optimization.bypass_on. `user_prompt` is IGNORED here -- it only
    # means something for per-message compression treatment ("don't compress
    # the live question"); a cache check/store is a whole-request decision,
    # not a per-message one. Kept in the shape so operators don't have to
    # learn a second bypass schema. Defaulted so the whole block is optional
    # (see ServingCfg.semantic_cache) — an upgrade whose mounted serving.yaml
    # predates this feature must still boot.
    bypass_on: BypassCfg = BypassCfg(
        high_risk=True, restricted_sensitivity=True, user_prompt=True)
    # Deferred embedding seam (C8): a future SemanticCacheBackend behind this
    # value. "off" is the only meaningful value today -- inert placeholder,
    # nothing reads it to construct anything. No embedding library is
    # imported anywhere in this codebase.
    embedding: str = "off"


class ServingCfg(BaseModel):
    providers: dict[str, ProviderCfg]
    pii_detection: str
    # Deployment ceiling: whether a per-app policy write may set pii_detection
    # to a weaker mode ("off"/"regex") than the global default at all. A
    # locked-down deployment sets this false so no app can weaken PII
    # detection regardless of its residency/capture_io state (admin_routes.
    # set_policy write-time clamp). Default true.
    allow_app_pii_downgrade: bool
    # Deployment-wide default protection posture (app.policy.enums.POLICY_MODES).
    # An app whose AppPolicy.policy_mode is null inherits this, so an install
    # where no customer wants PII checks sets it once instead of per app.
    # Defaulted for the same reason as `semantic_cache` below: a mounted
    # serving.yaml that predates this feature has no such key, and a required
    # field would KeyError every existing deployment on upgrade.
    default_policy_mode: str = "enforced"
    # Deployment ceiling, mirroring allow_app_pii_downgrade: whether ANY app may
    # resolve to "passthrough" at all. false = locked-down deployment where the
    # gate always runs, regardless of an app's own policy_mode or the default
    # above. Defaulted (true) for the upgrade-safety reason given above.
    allow_app_passthrough: bool = True
    reject_stream_requests: bool
    stream_keepalive_s: float = Field(5.0, gt=0)
    # Derive a stable per-conversation session id (auto-<24 hex>) when the
    # client sends no x-routeforge-session header. Defaulted (true) for the
    # same upgrade-safety reason as stream_keepalive_s above: a mounted
    # serving.yaml that predates this feature has no such key.
    derive_session_id: bool = True
    context_optimization: ContextOptimizationCfg
    memory: MemoryCfg = MemoryCfg()
    # Optional/defaulted like `memory` and `context_optimization`: a mounted
    # serving.yaml that predates feature #6 has no semantic_cache: block, and a
    # required field here would KeyError every existing deployment on upgrade.
    semantic_cache: SemanticCacheCfg = SemanticCacheCfg()
    observability: ObservabilityCfg


class Settings(BaseModel):
    serving: ServingCfg
    routing: RoutingCfg
    policy: PolicyCfg
    pricing: PricingCfg
    # Feature 3 F3: short content hash of routing.yaml + pricing.yaml (the
    # scoring weights, thresholds, tiers, pool, and prices that determine a
    # routing decision). Stamped on every trace as sp.routing_policy_version so
    # decisions can later be grouped by the exact policy that produced them
    # (rlhf-readiness F3). Automatic -- no manual semver.
    routing_policy_version: str = ""
    # Feature 3 (DSPy prompt-optimization) Task 1: sha256[:12] over the active
    # managed-prompt texts (routing.prompt_opt.active), mirroring
    # routing_policy_version's shape/empty-default exactly. Stamped on every
    # trace as sp.prompt_version when non-empty. `active_prompts` is the
    # already-resolved artifact set so downstream readers (Task 2's
    # classifier) don't need to re-read from disk.
    prompt_version: str = ""
    active_prompts: dict[str, PromptArtifact] = {}
