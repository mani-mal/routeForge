"""Operator feedback at save time: configurations that will reject real traffic.

Hard impossibilities are refused (422) in admin_routes.set_policy. Everything
here is a WARNING: the configuration is legal and may be intentional or
temporary (e.g. an app created before its model is onboarded), but the
operator must see that requests will be rejected before a client finds out.

The agent-turn warning is a heuristic: it prices a typical coding-agent turn
against each requestable model's own price and ignores risk floors and tier
targeting, so the router may pick a different (pricier or cheaper) model for
the same request than the one the warning reasons about."""
from __future__ import annotations

from app.apps.policy import AppPolicyView
from app.config.schema import Settings
from app.policy.compile import compile_policy
from app.policy.context import requestable_models

# A typical coding-agent turn (Qwen Code / Claude-Code-style clients).
AGENT_PROMPT_TOKENS = 25_000
AGENT_OUTPUT_TOKENS = 8_192


def _agent_turn_cost(price_input: float, price_output: float) -> float:
    return (AGENT_PROMPT_TOKENS * price_input + AGENT_OUTPUT_TOKENS * price_output) / 1_000_000


def price_warnings(settings: Settings, price_input: float, price_output: float) -> list[str]:
    cap = settings.policy.budgets["default_max_spend_usd"]
    est = _agent_turn_cost(price_input, price_output)
    if est <= cap:
        return []
    return [f"at ${price_input}/${price_output} per 1M tokens, a typical coding-agent turn "
            f"({AGENT_PROMPT_TOKENS} prompt + {AGENT_OUTPUT_TOKENS} output tokens) is estimated "
            f"at ${est:.4f}, over the deployment's default per-request cap ${cap:.4f}. Apps using "
            "this model need max_cost_usd_per_request raised, or lower the price (on-prem models "
            "are often priced 0)."]


def region_warnings(settings: Settings, registry, view: AppPolicyView) -> list[str]:
    """P1b (design §7): an app requiring a region that no endpoint in the
    pool currently declares can only ever produce an empty candidate set on
    region grounds alone. A WARNING, not a failure -- the app-side
    requirement lives in the DB and the matching endpoint may be onboarded
    later; this is the same "may be fixed after this write" reasoning
    `policy_warnings` below already applies to an unroutable policy.

    Resolved through `compile_policy` (not the raw, un-normalized
    `view.allowed_regions`) so this compares on exactly the same lowercased
    tokens `app/policy/feasible.py`'s eligibility rule does -- the one
    function that resolves the axis, read here rather than re-derived."""
    eff = compile_policy(view, settings)
    if not eff.allowed_regions:
        return []
    declared = {c.region for c in registry.effective_pool() if c.region}
    if declared & eff.allowed_regions:
        return []
    wants = ", ".join(sorted(eff.allowed_regions))
    has = ", ".join(sorted(declared)) or "(none)"
    return [f"allowed_regions requires one of [{wants}], but no onboarded endpoint "
            f"currently declares any of those regions (endpoints declare: {has}); every "
            "request will be rejected until a matching endpoint is onboarded, or its "
            "region is declared, or this application's allowed_regions is changed."]


def policy_warnings(settings: Settings, registry, app_id: str, view: AppPolicyView) -> list[str]:
    warnings = region_warnings(settings, registry, view)
    models = requestable_models(settings, registry, app_id, view)
    if not models:
        warnings.append(
            "no model is currently requestable for this application, so every request "
            "will be rejected with 422. Check residency (onprem_only needs an enabled "
            "on-prem model), allowed_providers, allowed_tiers, allowed_models, and the "
            "models' allowed_app_ids.")
        return warnings
    cap = (view.max_cost_usd_per_request if view.max_cost_usd_per_request is not None
           else settings.policy.budgets["default_max_spend_usd"])
    over = [c.name for c in models
            if _agent_turn_cost(registry.price_for(c.name).input,
                                registry.price_for(c.name).output) > cap]
    if len(over) == len(models):
        warnings.append(
            f"a typical coding-agent turn ({AGENT_PROMPT_TOKENS} prompt + "
            f"{AGENT_OUTPUT_TOKENS} output tokens) exceeds this application's per-request "
            f"cap ${cap:.4f} on every requestable model ({', '.join(sorted(over))}). Raise "
            "max_cost_usd_per_request or lower the model price.")
    return warnings
