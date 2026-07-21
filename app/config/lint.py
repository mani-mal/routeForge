import sys

from app.config.loader import load_settings
from app.config.schema import Settings

_ORDER = {"cheap": 0, "mid": 1, "frontier": 2}


def lint_settings(s: Settings) -> list[str]:
    errs: list[str] = []

    serv_prov = set(s.serving.providers)
    pool_prov = {c.provider for c in s.routing.model_pool}
    price_prov = {p.provider for p in s.pricing.models.values()}
    if not (serv_prov == pool_prov == price_prov):
        errs.append(
            f"PROVIDER_NAME_DRIFT serving={sorted(serv_prov)} "
            f"routing={sorted(pool_prov)} pricing={sorted(price_prov)}")

    tiers = set(s.routing.tiers)
    pool_tiers = [c.tier for c in s.routing.model_pool]
    for t in s.routing.risk_floors.values():
        if t not in tiers:
            errs.append(f"RISK_FLOOR_TIER_UNKNOWN {t} not in {sorted(tiers)}")
    for t in set(pool_tiers):
        if t not in tiers:
            errs.append(f"POOL_TIER_UNKNOWN {t} not in {sorted(tiers)}")
    for t in tiers:
        if t not in pool_tiers:
            errs.append(f"TIER_HAS_NO_MEMBER {t}")
    for name, p in s.pricing.models.items():
        if p.tier not in tiers:
            errs.append(f"PRICING_TIER_UNKNOWN {name}:{p.tier}")

    scale = set(s.policy.sensitivity_scale)
    if s.policy.default_sensitivity not in scale:
        errs.append(f"DEFAULT_SENSITIVITY_UNKNOWN {s.policy.default_sensitivity}")
    for r in s.policy.sensitivity_rules:
        if r.sensitivity not in scale:
            errs.append(f"RULE_SENSITIVITY_UNKNOWN {r.sensitivity}")
        if "on_prem" in r.residency:
            errs.append(f"RESIDENCY_SPELLING {r.residency} (use onprem_only)")

    return errs


def enforce(s: Settings) -> None:
    errs = lint_settings(s)
    if errs:
        for e in errs:
            print(f"CONFIG_LINT: {e}", file=sys.stderr)
        raise SystemExit(1)


if __name__ == "__main__":
    enforce(load_settings())
    print("CONFIG_LINT: ok")
