from pydantic import BaseModel


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


class RoutingCfg(BaseModel):
    tiers: list[str]
    score_on: str
    weights: dict[str, float]
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
    risk_floors: dict[str, str]
    model_pool: list[PoolCandidate]
    fallback: dict[str, int]


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
    agent_budgets: dict[str, float]
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
    # secrets via .env only). onprem derives its api_base from the vllm/dynamo
    # block per `inference_backend`, so it needs only a key env; bedrock reads
    # standard AWS_* env, so it needs neither.
    litellm_prefix: str | None = None
    api_base_env: str | None = None
    api_key_env: str | None = None
    api_version_env: str | None = None


class LangfuseCfg(BaseModel):
    enabled: bool
    host: str  # in-cluster URL for OTLP export (e.g. http://langfuse-web:3000)
    redact_on_export: bool
    # Eval-capture switch for request/response I/O attached to the serving.request
    # root span (docs/eval-logging-backlog.md). `off` = today's behaviour (no I/O
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

class VllmCfg(BaseModel):
    base_url: str
    timeout_s: int


class DynamoCfg(BaseModel):
    endpoint: str
    router_mode: str
    timeout_s: int


class ServingCfg(BaseModel):
    inference_backend: str
    providers: dict[str, ProviderCfg]
    pii_detection: str
    reject_stream_requests: bool
    context_optimization: ContextOptimizationCfg
    observability: ObservabilityCfg
    vllm: VllmCfg
    dynamo: DynamoCfg


class Settings(BaseModel):
    serving: ServingCfg
    routing: RoutingCfg
    policy: PolicyCfg
    pricing: PricingCfg
