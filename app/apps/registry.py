from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass

from app.config.schema import PoolCandidate, PriceCfg, Settings
from app.apps import store

_log = logging.getLogger("serving.registry")


@dataclass(frozen=True)
class _OnboardedRow:
    slug: str
    display_name: str
    provider: str
    tier: str
    quality: float | None
    price_input: float
    price_output: float
    endpoint: str
    api_key_env: str | None
    enabled: bool
    allowed_app_ids: tuple[str, ...] | None
    # P1b endpoint data-handling declarations (design doc §2). The raw,
    # unresolved per-model DECLARATION as stored on the onboarded row --
    # `effective_pool()` is the only place these are resolved (per-model,
    # else provider default, else "unknown"). Not a verified fact.
    region: str | None = None
    prompt_retention: str | None = None
    accepts_raw_sensitive: bool | None = None
    # USD/1M for prompt tokens this endpoint served from its own prompt
    # cache; None = undeclared -> `price_for` bills cached tokens at
    # `price_input` (no invented discount). See app/apps/models.py::
    # OnboardedModel.price_cached_input.
    price_cached_input: float | None = None
    # Real context window in tokens, when known (B50). None means unknown --
    # never a guessed default. An operator's explicit value wins over a
    # probed one; `effective_pool()` carries it onto the candidate untouched.
    context_window: int | None = None


class ModelRegistry:
    """Merged model source: static YAML pool (cloud built-ins) + enabled
    onboarded on-prem rows. Replaces InfraRoster. Sync reads on the request
    path; refreshed on every admin write. Last-known-good on DB error; cold
    cache is empty-onboarded (YAML-only) so a DB blip degrades onboarding
    visibility, never residency."""

    def __init__(self, settings: Settings) -> None:
        self._s = settings
        self._rows: dict[str, _OnboardedRow] | None = None
        # Bumped on every successful `refresh`, so derived caches (the shared
        # QualityResolver's tier/published maps) know the pool may have moved.
        self._generation = 0

    @property
    def generation(self) -> int:
        return self._generation

    async def ensure_loaded(self, db) -> None:
        if self._rows is None:
            await self.refresh(db)

    async def refresh(self, db) -> None:
        try:
            async with db.session() as session:
                rows = await store.list_onboarded(session)
            self._rows = {
                r.slug: _OnboardedRow(
                    slug=r.slug, display_name=r.display_name, provider=r.provider,
                    tier=r.tier, quality=r.quality, price_input=r.price_input,
                    price_output=r.price_output, endpoint=r.endpoint,
                    api_key_env=r.api_key_env, enabled=bool(r.enabled),
                    allowed_app_ids=tuple(r.allowed_app_ids) if r.allowed_app_ids else None,
                    region=r.region, prompt_retention=r.prompt_retention,
                    accepts_raw_sensitive=r.accepts_raw_sensitive,
                    price_cached_input=r.price_cached_input,
                    context_window=r.context_window)
                for r in rows}
            self._generation += 1
        except Exception:
            _log.exception("registry refresh failed; keeping last-known-good")

    def _onboarded(self) -> dict[str, _OnboardedRow]:
        return self._rows or {}

    def _resolve_data_handling(
        self, *, model_region: str | None, model_retention: str | None,
        model_raw_sensitive: bool | str | None, provider: str,
    ) -> tuple[str, str, str, dict[str, str]]:
        """Resolve region/prompt_retention/accepts_raw_sensitive for one
        candidate. Resolution order per field: the per-model declared value,
        else the provider default (serving.yaml), else "unknown".

        These are DECLARATIONS an operator has recorded about an endpoint --
        RouteForge cannot verify a provider's contractual claim (e.g. that it
        truly discards prompts). It only records what was declared and later
        enforces policy against that record. "unknown" is the normal,
        expected result for every endpoint that does not declare these
        fields -- never an error, never a boot failure.
        """
        cfg = self._s.serving.providers.get(provider)
        prov_region = cfg.region if cfg else None
        prov_retention = cfg.prompt_retention if cfg else None
        prov_raw = cfg.accepts_raw_sensitive if cfg else None

        def tok(v: bool | str | None) -> str | None:
            if isinstance(v, bool):
                return "true" if v else "false"
            return v

        if model_region:
            region, region_src = model_region.strip().lower(), "model"
        elif prov_region:
            region, region_src = prov_region.strip().lower(), "provider_default"
        else:
            region, region_src = "unknown", "unknown"

        if model_retention:
            retention, retention_src = model_retention, "model"
        elif prov_retention:
            retention, retention_src = prov_retention, "provider_default"
        else:
            retention, retention_src = "unknown", "unknown"

        model_raw = tok(model_raw_sensitive)
        if model_raw is not None:
            raw, raw_src = model_raw, "model"
        elif prov_raw is not None:
            raw, raw_src = prov_raw, "provider_default"
        else:
            raw, raw_src = "unknown", "unknown"

        return region, retention, raw, {
            "region": region_src, "prompt_retention": retention_src,
            "accepts_raw_sensitive": raw_src}

    def effective_pool(self) -> list[PoolCandidate]:
        # A static (YAML) cloud candidate is only routable when its provider is
        # enabled in serving.yaml. A killed provider must never enter the pool --
        # otherwise it surfaces as a phantom "residency" spur on the router card
        # (B10). Onboarded on-prem rows are gated per-model by r.enabled; the
        # on-prem infra kill-switch is handled in policy/context via
        # has_healthy_onprem, so it is not re-applied here.
        #
        # This is also the single place the three P1b data-handling
        # declarations (region/prompt_retention/accepts_raw_sensitive) are
        # resolved -- every consumer reads the resolved PoolCandidate; none
        # should re-derive the resolution.
        enabled = {p for p, c in self._s.serving.providers.items() if c.enabled}
        pool = []
        for c in self._s.routing.model_pool:
            if c.provider not in enabled:
                continue
            region, retention, raw, src = self._resolve_data_handling(
                model_region=c.region, model_retention=c.prompt_retention,
                model_raw_sensitive=c.accepts_raw_sensitive, provider=c.provider)
            pool.append(c.model_copy(update={
                "region": region, "prompt_retention": retention,
                "accepts_raw_sensitive": raw, "data_handling_source": src}))
        for r in self._onboarded().values():
            if r.enabled:
                region, retention, raw, src = self._resolve_data_handling(
                    model_region=r.region, model_retention=r.prompt_retention,
                    model_raw_sensitive=r.accepts_raw_sensitive, provider=r.provider)
                pool.append(PoolCandidate(
                    name=r.slug, provider=r.provider, tier=r.tier,
                    display_name=r.display_name, quality=r.quality,
                    region=region, prompt_retention=retention,
                    accepts_raw_sensitive=raw, data_handling_source=src,
                    context_window=r.context_window))
        return pool

    def registry_version(self) -> str:
        """First 12 hex of sha256 over the effective pool's identity + the
        three P1b data-handling declarations (design §6), sorted for
        stability. Same shape as `AppPolicyView.version()`
        (`app/apps/policy.py`) and `app/config/loader.py::_policy_version`
        for routing/pricing -- sorted-key JSON over exactly the fields a
        decision reads, so the hash is stable across boots for equal input
        and changes the instant any of them does.

        Emitted as `sp.endpoint_registry_version` beside `app_policy_version`
        and `routing_policy_version`: a decision then names the registry it
        was made against, and a changed endpoint declaration (an operator
        editing a region, say, or onboarding/disabling a model) becomes
        visible in the trace instead of silent.

        Deliberately narrow: name/provider/tier (the routing identity) plus
        region/prompt_retention/accepts_raw_sensitive (what stage 2 of
        `app/policy/feasible.py` filters on). `quality`, `display_name` and
        price never gate eligibility, so they are left out -- a cosmetic
        rename or a price update must not churn a version a replay consumer
        keys on."""
        items = sorted(
            ({"name": c.name, "provider": c.provider, "tier": c.tier,
              "region": c.region, "prompt_retention": c.prompt_retention,
              "accepts_raw_sensitive": c.accepts_raw_sensitive}
             for c in self.effective_pool()),
            key=lambda d: (d["name"], d["provider"]))
        blob = json.dumps(items, sort_keys=True, default=str)
        return hashlib.sha256(blob.encode()).hexdigest()[:12]

    def price_for(self, slug: str) -> PriceCfg:
        p = self._s.pricing.models.get(slug)
        if p is not None:
            return p
        r = self._onboarded().get(slug)
        if r is not None:
            return PriceCfg(input=r.price_input, output=r.price_output,
                            cached_input=r.price_cached_input,
                            tier=r.tier, provider=r.provider)
        raise KeyError(slug)

    def endpoint_for(self, slug: str) -> tuple[str, str | None] | None:
        r = self._onboarded().get(slug)
        return (r.endpoint, r.api_key_env) if r is not None else None

    def disabled_onboarded(self) -> tuple[str, ...]:
        """Sorted slugs of onboarded rows the admin has disabled. They are
        absent from `effective_pool()`; this exists ONLY so an explanation
        (P3b's conflicting core, `app/policy/feasible.py::violation_matrix`)
        can report them as `disabled` state. Never an eligibility input."""
        return tuple(sorted(slug for slug, r in self._onboarded().items()
                            if not r.enabled))

    def known_slugs(self) -> frozenset[str]:
        """Every slug the deployment has heard of: the YAML pool plus every
        onboarded row, enabled or not. A pure name read -- no eligibility
        meaning, so it never widens or narrows a candidate set."""
        return (frozenset(c.name for c in self._s.routing.model_pool)
                | frozenset(self._onboarded()))

    def is_enabled(self, slug: str) -> bool:
        r = self._onboarded().get(slug)
        return True if r is None else r.enabled

    def is_allowed_for_app(self, slug: str, app_id: str | None) -> bool:
        r = self._onboarded().get(slug)
        if r is None or not r.allowed_app_ids:
            return True
        return app_id in r.allowed_app_ids

    def has_healthy_onprem(self) -> bool:
        return any(r.enabled and r.provider == "onprem"
                   for r in self._onboarded().values())
