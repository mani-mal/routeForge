from __future__ import annotations

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


class ModelRegistry:
    """Merged model source: static YAML pool (cloud built-ins) + enabled
    onboarded on-prem rows. Replaces InfraRoster. Sync reads on the request
    path; refreshed on every admin write. Last-known-good on DB error; cold
    cache is empty-onboarded (YAML-only) so a DB blip degrades onboarding
    visibility, never residency."""

    def __init__(self, settings: Settings) -> None:
        self._s = settings
        self._rows: dict[str, _OnboardedRow] | None = None

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
                    allowed_app_ids=tuple(r.allowed_app_ids) if r.allowed_app_ids else None)
                for r in rows}
        except Exception:
            _log.exception("registry refresh failed; keeping last-known-good")

    def _onboarded(self) -> dict[str, _OnboardedRow]:
        return self._rows or {}

    def effective_pool(self) -> list[PoolCandidate]:
        # A static (YAML) cloud candidate is only routable when its provider is
        # enabled in serving.yaml. A killed provider must never enter the pool --
        # otherwise it surfaces as a phantom "residency" spur on the router card
        # (B10). Onboarded on-prem rows are gated per-model by r.enabled; the
        # on-prem infra kill-switch is handled in policy/context via
        # has_healthy_onprem, so it is not re-applied here.
        enabled = {p for p, c in self._s.serving.providers.items() if c.enabled}
        pool = [c for c in self._s.routing.model_pool if c.provider in enabled]
        for r in self._onboarded().values():
            if r.enabled:
                pool.append(PoolCandidate(
                    name=r.slug, provider=r.provider, tier=r.tier,
                    display_name=r.display_name, quality=r.quality))
        return pool

    def price_for(self, slug: str) -> PriceCfg:
        p = self._s.pricing.models.get(slug)
        if p is not None:
            return p
        r = self._onboarded().get(slug)
        if r is not None:
            return PriceCfg(input=r.price_input, output=r.price_output,
                            tier=r.tier, provider=r.provider)
        raise KeyError(slug)

    def endpoint_for(self, slug: str) -> tuple[str, str | None] | None:
        r = self._onboarded().get(slug)
        return (r.endpoint, r.api_key_env) if r is not None else None

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
