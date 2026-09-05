import hashlib
import json
import os
import re
from pathlib import Path

import yaml

from app.config.schema import (
    BypassCfg, CCRCfg, ContextOptimizationCfg, LangfuseCfg, MemoryCfg,
    ObservabilityCfg, PolicyCfg, PricingCfg, RoutingCfg, SemanticCacheCfg,
    ServingCfg, Settings,
)
from app.prompts.store import prompt_version, resolve_active

_ENV_PAT = re.compile(r"\$\{([A-Z_][A-Z0-9_]*)(?::-([^}]*))?\}")


def _expand_env(value: str) -> str:
    def repl(m: re.Match) -> str:
        return os.environ.get(m.group(1), m.group(2) or "")
    return _ENV_PAT.sub(repl, value)


def _read(config_dir: str, name: str) -> dict:
    return yaml.safe_load(Path(config_dir, name).read_text()) or {}


def _policy_version(routing_raw: dict, pricing_raw: dict) -> str:
    """First 12 hex of sha256 over the canonicalized routing + pricing config.
    Sorted-key JSON makes it stable across boots for identical config and
    change only when a scoring/pool/price value changes."""
    blob = json.dumps({"routing": routing_raw, "pricing": pricing_raw},
                      sort_keys=True, default=str)
    return hashlib.sha256(blob.encode()).hexdigest()[:12]


def load_settings(config_dir: str = "config") -> Settings:
    serving_raw = _read(config_dir, "serving.yaml")
    routing_raw = _read(config_dir, "routing.yaml")
    policy_raw = _read(config_dir, "policy.yaml")
    pricing_raw = _read(config_dir, "pricing.yaml")

    lf_raw = serving_raw["observability"]["langfuse"]
    observability = ObservabilityCfg(
        langfuse=LangfuseCfg(
            enabled=lf_raw["enabled"],
            host=_expand_env(str(lf_raw["host"])),
            redact_on_export=lf_raw["redact_on_export"],
            # SP_CAPTURE_IO overrides observability.langfuse.capture_io — same
            # env-override pattern as SP_PII_DETECTION below, so eval capture
            # can be flipped per-deploy without editing the YAML.
            capture_io=os.environ.get(
                "SP_CAPTURE_IO",
                _expand_env(str(lf_raw.get("capture_io", "off")))).strip().lower(),
            project_id=os.environ.get(
                "LANGFUSE_PROJECT_ID",
                _expand_env(str(lf_raw.get("project_id", "demo-project")))),
            public_host=(os.environ.get("LANGFUSE_PUBLIC_URL")
                         or (_expand_env(str(lf_raw["public_host"]))
                             if lf_raw.get("public_host") else None) or None),
            public_key=os.environ.get("LANGFUSE_PUBLIC_KEY"),
            secret_key=os.environ.get("LANGFUSE_SECRET_KEY"),
        )
    )

    co_raw = serving_raw["context_optimization"]
    # SP_CONTEXT_OPT=on/off overrides context_optimization.enabled — same
    # env-override pattern as SP_PII_DETECTION/SP_CAPTURE_IO above. Used by
    # scripts/smoke-compression.sh to toggle Headroom on/off across two boots
    # without editing config/serving.yaml.
    _co_opt_env = os.environ.get("SP_CONTEXT_OPT")
    co_enabled = (
        co_raw["enabled"] if _co_opt_env is None
        else _co_opt_env.strip().lower() in ("on", "true", "1", "yes")
    )
    # SP_ALLOW_APP_PII_DOWNGRADE overrides policy.allow_app_pii_downgrade — same
    # env-override pattern as SP_PII_DETECTION/SP_CONTEXT_OPT above. Defaults to
    # true (matches config/serving.yaml) if absent from both env and YAML.
    _allow_pii_dg_env = os.environ.get("SP_ALLOW_APP_PII_DOWNGRADE")
    allow_app_pii_downgrade = (
        bool(serving_raw["policy"].get("allow_app_pii_downgrade", True))
        if _allow_pii_dg_env is None
        else _allow_pii_dg_env.strip().lower() in ("on", "true", "1", "yes")
    )
    # SP_ALLOW_APP_PASSTHROUGH overrides policy.allow_app_passthrough — same
    # env-override pattern (and same deployment-ceiling role) as
    # SP_ALLOW_APP_PII_DOWNGRADE above. Defaults to true (matches
    # config/serving.yaml) if absent from both env and YAML, so a mounted
    # serving.yaml predating this feature still boots.
    _allow_passthrough_env = os.environ.get("SP_ALLOW_APP_PASSTHROUGH")
    allow_app_passthrough = (
        bool(serving_raw["policy"].get("allow_app_passthrough", True))
        if _allow_passthrough_env is None
        else _allow_passthrough_env.strip().lower() in ("on", "true", "1", "yes")
    )
    # Feature 5 (Task 1) — memory.enabled. SP_MEMORY overrides serving.yaml's
    # memory.enabled — same env-override pattern as SP_CONTEXT_OPT above.
    # Substrate only: this cfg is surfaced but nothing constructs/uses a
    # store yet (Task 2 wires it in), so flipping it has no runtime effect.
    mem_raw = serving_raw.get("memory", {})
    _mem_opt_env = os.environ.get("SP_MEMORY")
    mem_enabled = (
        bool(mem_raw.get("enabled", False)) if _mem_opt_env is None
        else _mem_opt_env.strip().lower() in ("on", "true", "1", "yes")
    )
    memory = MemoryCfg(
        enabled=mem_enabled,
        residency=mem_raw.get("residency", "local_only"),
        ttl_hours=mem_raw.get("ttl_hours", 24),
        export_to_telemetry=mem_raw.get("export_to_telemetry", False),
        max_recall=mem_raw.get("max_recall", 3),
        min_tokens=mem_raw.get("min_tokens", 0),
        bypass_on=BypassCfg(**mem_raw.get(
            "bypass_on",
            {"high_risk": True, "restricted_sensitivity": True, "user_prompt": False})),
    )


    # Defensive .get() like `memory`/`context_optimization` above: a brand-new
    # default-OFF infra block must be OPTIONAL, or upgrading an existing
    # deployment whose mounted serving.yaml predates this feature (DGX
    # ConfigMap, Helm/HPE serving-config — none ship a semantic_cache: block)
    # would KeyError at boot. Absent block => disabled with schema defaults.
    sc_raw = serving_raw.get("semantic_cache", {})
    # SP_SEMANTIC_CACHE=on/off overrides semantic_cache.enabled — same
    # env-override pattern as SP_CONTEXT_OPT above (feature #6, Task 1).
    _sc_opt_env = os.environ.get("SP_SEMANTIC_CACHE")
    sc_enabled = (
        bool(sc_raw.get("enabled", False)) if _sc_opt_env is None
        else _sc_opt_env.strip().lower() in ("on", "true", "1", "yes")
    )
    serving = ServingCfg(
        providers=serving_raw["providers"],
        pii_detection=os.environ.get(
            "SP_PII_DETECTION", serving_raw["policy"]["pii_detection"]),
        allow_app_pii_downgrade=allow_app_pii_downgrade,
        # SP_DEFAULT_POLICY_MODE overrides policy.default_policy_mode — same
        # env-override pattern as SP_PII_DETECTION above. .get() with the
        # default so an older serving.yaml without the key still boots.
        default_policy_mode=os.environ.get(
            "SP_DEFAULT_POLICY_MODE",
            serving_raw["policy"].get("default_policy_mode", "enforced")),
        allow_app_passthrough=allow_app_passthrough,
        reject_stream_requests=serving_raw["server"]["reject_stream_requests"],
        context_optimization=ContextOptimizationCfg(
            enabled=co_enabled,
            mode=co_raw["mode"],
            min_tokens=co_raw["min_tokens"],
            bypass_on=BypassCfg(**co_raw["bypass_on"]),
            ccr=CCRCfg(**co_raw["ccr"]),
            cache_alignment=co_raw["cache_alignment"],
        ),
        memory=memory,
        semantic_cache=SemanticCacheCfg(
            enabled=sc_enabled,
            residency=sc_raw.get("residency", "local_only"),
            ttl_hours=sc_raw.get("ttl_hours", 24),
            export_to_telemetry=sc_raw.get("export_to_telemetry", False),
            max_entries=sc_raw.get("max_entries", 0),
            bypass_on=BypassCfg(**sc_raw.get(
                "bypass_on",
                {"high_risk": True, "restricted_sensitivity": True, "user_prompt": True})),
            embedding=sc_raw.get("embedding", "off"),
        ),
        observability=observability,
    )
    routing = RoutingCfg(**routing_raw)
    policy = PolicyCfg(**policy_raw)
    pricing = PricingCfg(**pricing_raw)

    # Feature 3 (DSPy prompt-optimization) Task 1: resolve the managed-prompt
    # pins. A missing/invalid pin must fail the boot (CLAUDE.md rule 5 --
    # fail closed), never a silent skip -- but `load_settings()` itself must
    # never raise for it, or `enforce()` (which reports the NAMED
    # PROMPT_ACTIVE_MISSING/PROMPT_ARTIFACT_INVALID error) would never get a
    # chance to run. So: on ANY resolution failure, defer entirely -- leave
    # `active_prompts` empty and `prompt_version` "" -- and let
    # `app/config/lint.py::lint_settings`/`enforce` re-resolve each pin
    # independently from `routing.prompt_opt` and report the named violation.
    # This is deliberately all-or-nothing (not partial): a bad pin must never
    # let its sibling pins quietly activate either.
    try:
        active_prompts = resolve_active(routing.prompt_opt)
    except (FileNotFoundError, ValueError):
        active_prompts = {}
    pv = prompt_version(active_prompts)

    return Settings(serving=serving, routing=routing, policy=policy, pricing=pricing,
                    routing_policy_version=_policy_version(routing_raw, pricing_raw),
                    prompt_version=pv, active_prompts=active_prompts)
