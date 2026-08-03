from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from app.config.schema import Settings

if TYPE_CHECKING:
    from app.apps.registry import ModelRegistry

# Provenance of a resolved quality score, most authoritative first. These four
# strings are the ONLY valid sources (spec §4) — used verbatim on DecisionRecord
# and in the UI. Precedence between `customer` and `calibrated` is applied when
# BUILDING the overrides snapshot (store.load_quality_overrides), so the resolver
# sees one winning override per model.
QUALITY_SOURCES = ("customer", "calibrated", "published", "tier_default")

_TIER_DEFAULT_FLOOR = 0.5  # neutral last resort; == the historical cheap-tier _Q


@dataclass(frozen=True)
class QualityScore:
    score: float
    source: str  # one of QUALITY_SOURCES


class QualityResolver:
    """Resolves a per-model quality score via a fixed provenance ladder:
    customer > calibrated > published > tier_default. Always succeeds
    (tier_default covers every tier), never raises for an unknown model, never
    blocks a model from routing.

    `overrides` carries DB-sourced scores (Stage 2 calibration / customer
    declarations), keyed by model name -> QualityScore whose `source` is already
    `customer` or `calibrated`. It is a plain dict snapshot so the router stays
    synchronous and DB-free in unit tests; the caller refreshes it. Empty in
    Stage 1 (no calibration written yet).

    `registry`, when given, resolves tier/published-quality over the merged
    pool (static YAML + enabled onboarded rows) instead of just the static
    YAML pool -- otherwise every onboarded on-prem model (which never appears
    in `settings.routing.model_pool`) would fall through to the flat
    `_TIER_DEFAULT_FLOOR`, silently ignoring its actual tier's tier_default
    quality (or any published quality set at onboarding)."""

    def __init__(self, settings: Settings,
                 overrides: dict[str, QualityScore] | None = None,
                 registry: "ModelRegistry | None" = None) -> None:
        pool = registry.effective_pool() if registry else settings.routing.model_pool
        self._tier_quality = dict(settings.routing.tier_quality)
        self._tier_of = {c.name: c.tier for c in pool}
        self._published = {c.name: c.quality for c in pool if c.quality is not None}
        self._overrides = overrides or {}

    def resolve(self, model: str, task_class: str | None = None) -> QualityScore:
        # task_class is accepted for forward-compat (quality_by_class, Stage 3+)
        # and ignored while a single scalar quality per model ships (YAGNI).
        ov = self._overrides.get(model)
        if ov is not None:
            return ov
        pub = self._published.get(model)
        if pub is not None:
            return QualityScore(pub, "published")
        tier = self._tier_of.get(model)
        if tier is not None and tier in self._tier_quality:
            return QualityScore(self._tier_quality[tier], "tier_default")
        # Unknown model with no tier mapping: neutral floor so the resolver never
        # raises and never blocks a custom model.
        return QualityScore(_TIER_DEFAULT_FLOOR, "tier_default")
