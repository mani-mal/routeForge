import logging

from pydantic import BaseModel, Field, model_validator

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
    # --- P1b endpoint data-handling declarations (design doc §2). On a static
    # routing.yaml model_pool entry these three are the per-model DECLARATION
    # (unset = "not declared here"); they are never verified, only recorded
    # and later enforced against. `ModelRegistry.effective_pool()` is the one
    # place that RESOLVES them (per-model value, else the provider default
    # from serving.yaml, else "unknown") and overwrites these same fields with
    # the resolved, never-None result plus `data_handling_source` -- no other
    # consumer should re-derive the resolution.
    region: str | None = None                  # lowercase token (e.g. us/eu/de/onprem), or "unknown"
    prompt_retention: str | None = None         # none | retained | unknown
    accepts_raw_sensitive: str | None = None    # "true" | "false" | "unknown"
    # field -> "model" | "provider_default" | "unknown". Populated only by
    # effective_pool()'s resolution; empty on a raw/unresolved PoolCandidate.
    data_handling_source: dict[str, str] = Field(default_factory=dict)


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


class SignalsCfg(BaseModel):
    """Measured routing signals (P2a). Owned by routing.yaml only."""
    enabled: bool = True
    # Latency used for a model with no usable measurement, per canonical tier.
    # These defaults are NOT arbitrary: with `latency_scale_ms` 1000 they make
    # the normalized latency term evaluate to 0.2/0.5/1.0 -- byte-identical to
    # the pre-P2a hardcoded `_L` table -- so an empty SignalStore reproduces
    # today's scores exactly. Changing either without the other breaks that
    # identity property (spec §6.2).
    #
    # Deliberately NOT the same numbers as
    # app/gateway/backends/base.py::TIER_LATENCY_MS (200/500/2000, test-fake-
    # only) -- this table's `frontier` is 1000, not 2000. Production and
    # test-only tables, on purpose; do not "fix" the discrepancy (spec §9).
    tier_latency_ms: dict[str, float] = Field(
        default_factory=lambda: {"cheap": 200.0, "mid": 500.0, "frontier": 1000.0})
    latency_scale_ms: float = 1000.0
    # Ceiling on the normalized latency term, so one pathologically slow
    # endpoint cannot swamp quality and cost entirely.
    max_latency_norm: float = 3.0
    # Wall-clock half-life for availability's decay back toward 1.0.
    half_life_s: float = 900.0
    # Observations required before a signal is reported as `measured`.
    min_samples: int = 5
    # A latency observation older than this is no longer a measurement.
    staleness_s: float = 3600.0
    seed_window_hours: int = 24
    # EWMA smoothing floor. Early on, alpha = 1/(n+1) behaves as a running
    # mean; once n exceeds 1/alpha_floor it becomes a fixed-width EWMA.
    alpha_floor: float = 0.1
    # Observation-log writer (signal hardening, spec §3.3/§7). The sink buffers
    # observations in memory; a lifespan task flushes them every
    # `flush_interval_s`. `max_buffer` bounds memory if the DB is unreachable
    # (oldest dropped). `max_seed_obs_per_model` caps boot replay: with
    # alpha_floor 0.1 the 200th-oldest observation weighs ~0.9**191, i.e.
    # nothing (spec §3.5).
    flush_interval_s: float = 5.0
    max_buffer: int = 10000
    max_seed_obs_per_model: int = 200


# The four scoring terms `DefaultRoutingStrategy.rank` reads by literal key
# (app/routing/strategy.py). A `weights` dict missing one of these currently
# KeyErrors deep inside `rank` on the first scored request -- pulled forward
# to `RoutingCfg` construction time instead (`_rename_capacity_weight` below),
# so a bad `routing.yaml` or a hand-built weights mapping (e.g.
# `app/eval/strategies.py::router_variant`) fails clearly and immediately.
_REQUIRED_WEIGHT_KEYS = ("quality", "cost", "latency", "availability")


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
    # P2a measured routing signals. Owned here (routing input), never restated.
    signals: SignalsCfg = SignalsCfg()

    @model_validator(mode="before")
    @classmethod
    def _rename_capacity_weight(cls, data):
        """`capacity` -> `availability` (P2a).

        The old term added the SAME constant to every candidate, so it
        cancelled out of the ordering and never influenced a routing
        decision. It is renamed because true capacity -- queue depth,
        admission pressure -- is unobservable across an OpenAI-compatible
        boundary; what RouteForge can observe is whether calls have been
        succeeding, which is availability.

        The alias is accepted so no deployed routing.yaml fails to boot:
        `weights["capacity"]` is renamed to `weights["availability"]` (an
        explicit `availability` wins over a stale `capacity` alias) and the
        `capacity` key is dropped. `app/routing/strategy.py` reads
        `weights["availability"]` only.

        After the rename, every key in `_REQUIRED_WEIGHT_KEYS` must be
        present -- a missing one raises here (a clear `ValidationError` at
        construction) rather than a bare `KeyError` the first time
        `DefaultRoutingStrategy.rank` reads it mid-request.
        """
        if not isinstance(data, dict):
            return data
        weights = data.get("weights")
        if not isinstance(weights, dict):
            return data
        weights = dict(weights)
        if "capacity" in weights:
            legacy = weights.pop("capacity")
            had_explicit_availability = "availability" in weights
            weights.setdefault("availability", legacy)
            if not had_explicit_availability:
                logging.getLogger(__name__).warning(
                    "routing.yaml: weights.capacity is deprecated; rename it "
                    "to weights.availability (the old term never affected "
                    "ranking)")
        missing = [k for k in _REQUIRED_WEIGHT_KEYS if k not in weights]
        if missing:
            raise ValueError(
                f"routing.weights is missing required key(s) {missing} "
                f"(required: {list(_REQUIRED_WEIGHT_KEYS)})")
        data = {**data, "weights": weights}
        return data


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
    # USD/1M for prompt tokens the provider served from ITS OWN prompt cache
    # (`usage.prompt_tokens_details.cached_tokens`). None = undeclared, and an
    # undeclared rate bills cached tokens at the full `input` rate --
    # RouteForge never invents a discount an operator did not declare. On-prem
    # endpoints normally declare this EQUAL to `input`: a vLLM prefix-cache hit
    # saves latency, not GPU amortization.
    cached_input: float | None = None


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
    # --- P1b endpoint data-handling provider defaults (design doc §2). Fall
    # back for any per-model value left unset on an onboarded row or a
    # routing.yaml model_pool entry; unset here too resolves to "unknown" in
    # ModelRegistry.effective_pool(). Declarations, not verified facts.
    region: str | None = None                  # lowercase token, e.g. us/eu/de/onprem
    prompt_retention: str | None = None         # none | retained | unknown
    accepts_raw_sensitive: str | None = None    # "true" | "false" | "unknown"


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
