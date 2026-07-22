from pydantic import BaseModel


class PoolCandidate(BaseModel):
    name: str
    provider: str
    tier: str


class RoutingCfg(BaseModel):
    tiers: list[str]
    score_on: str
    weights: dict[str, float]
    complexity_thresholds: dict[str, float]
    confidence_escalation_threshold: float
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


class LangfuseCfg(BaseModel):
    enabled: bool
    host: str
    redact_on_export: bool
    public_key: str | None = None
    secret_key: str | None = None


class ObservabilityCfg(BaseModel):
    langfuse: LangfuseCfg


class ServingCfg(BaseModel):
    inference_backend: str
    allow_backend_fallback: bool
    providers: dict[str, ProviderCfg]
    pii_detection: str
    reject_stream_requests: bool
    context_optimization_enabled: bool
    observability: ObservabilityCfg


class Settings(BaseModel):
    serving: ServingCfg
    routing: RoutingCfg
    policy: PolicyCfg
    pricing: PricingCfg
