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
    # P2b-3: the calibration set a `calibrated` score came from (its
    # `eval_run_id`); None for every other source. Content-free.
    set_id: str | None = None

OverrideKey = tuple[str, "str | None"]   # (model, task_class | None)

class QualityResolver:
    """Resolves a per-model quality score via a fixed provenance ladder:
    customer > calibrated > published > tier_default. Always succeeds
    (tier_default covers every tier), never raises for an unknown model, never
    blocks a model from routing -- quality only REORDERS the eligible set.

    **Overrides (P2b-3 §7)** are DB-sourced scores (`customer` declarations and
    `calibrated` sets), keyed `(model, task_class | None)` -- `task_class` is
    the request's workload slug. `resolve(model, task_class)` checks, in order:
    the `(model, task_class)` override, the `(model, None)` override,
    published, tier default. The snapshot is a plain dict that
    `set_overrides` REPLACES BY REFERENCE (one atomic assignment), so the
    router reads it synchronously with no awaits and no lock; one resolver is
    shared app-wide (`app.state.quality`) and refreshed by `app.main`. A bare
    model-name key is accepted too and means `(model, None)`.

    `registry`, when given, resolves tier/published-quality over the merged
    pool (static YAML + enabled onboarded rows) instead of just the static
    YAML pool -- otherwise every onboarded on-prem model (which never appears
    in `settings.routing.model_pool`) would fall through to the flat
    `_TIER_DEFAULT_FLOOR`. The registry is read LIVE: the maps are rebuilt
    whenever the registry's `generation` moves (every `refresh`), so a model
    onboarded after boot resolves to its own tier, not the floor."""

    def __init__(self, settings: Settings,
                 overrides: "dict[OverrideKey | str, QualityScore] | None" = None,
                 registry: "ModelRegistry | None" = None) -> None:
        self._settings = settings
        self._registry = registry
        self._tier_quality = dict(settings.routing.tier_quality)
        self._pool_gen: object = object()   # never equals a registry generation
        self._tier_of: dict[str, str] = {}
        self._published: dict[str, float] = {}
        if registry is None:
            self._build_maps(settings.routing.model_pool)
        self._overrides: dict[OverrideKey, QualityScore] = {}
        self.set_overrides(overrides or {})

    def _build_maps(self, pool) -> None:
        self._tier_of = {c.name: c.tier for c in pool}
        self._published = {c.name: c.quality for c in pool if c.quality is not None}

    def _sync_pool(self) -> None:
        reg = self._registry
        if reg is None:
            return
        # A registry stand-in without a generation counter (test doubles) is
        # re-read every time: correct, just not cached.
        gen = getattr(reg, "generation", None)
        if gen is None or gen != self._pool_gen:
            self._build_maps(reg.effective_pool())
            self._pool_gen = gen

    def set_overrides(self, mapping: "dict[OverrideKey | str, QualityScore]") -> None:
        """Swap the override snapshot. Builds the new dict first, then replaces
        the reference in one assignment -- a concurrent `resolve` sees either
        the old snapshot or the new one, never a mix."""
        new: dict[OverrideKey, QualityScore] = {}
        for key, q in mapping.items():
            new[(key, None) if isinstance(key, str) else (key[0], key[1])] = q
        self._overrides = new

    def resolve(self, model: str, task_class: str | None = None) -> QualityScore:
        overrides = self._overrides   # one read: a stable snapshot
        if task_class is not None:
            ov = overrides.get((model, task_class))
            if ov is not None:
                return ov
        ov = overrides.get((model, None))
        if ov is not None:
            return ov
        self._sync_pool()
        pub = self._published.get(model)
        if pub is not None:
            return QualityScore(pub, "published")
        tier = self._tier_of.get(model)
        if tier is not None and tier in self._tier_quality:
            return QualityScore(self._tier_quality[tier], "tier_default")
        # Unknown model with no tier mapping: neutral floor so the resolver never
        # raises and never blocks a custom model.
        return QualityScore(_TIER_DEFAULT_FLOOR, "tier_default")
