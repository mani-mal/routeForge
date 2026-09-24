from __future__ import annotations

import dataclasses
import logging
import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from app.config.schema import Settings

if TYPE_CHECKING:
    from app.apps.registry import ModelRegistry

# Provenance of a resolved quality score, most authoritative first. These four
# strings are the ONLY valid sources (spec §4) — used verbatim on DecisionRecord
# and in the UI.
QUALITY_SOURCES = ("customer", "calibrated", "published", "tier_default")

# The two DB-sourced (override) provenances. The resolver keeps them in
# SEPARATE maps so precedence across keys is decided at resolve time (see
# `QualityResolver.resolve`), never collapsed per key when the snapshot is built.
OVERRIDE_SOURCES = ("customer", "calibrated")

_TIER_DEFAULT_FLOOR = 0.5  # neutral last resort; == the historical cheap-tier _Q


@dataclass(frozen=True)
class QualityScore:
    score: float
    source: str  # one of QUALITY_SOURCES
    # P2b-3: the calibration set a `calibrated` score came from (its
    # `eval_run_id`); None for every other source. Content-free.
    set_id: str | None = None
    # P2b-3: when an override (customer/calibrated) supplied the score, the
    # `(value, source)` the ladder would have given WITHOUT any override --
    # `published` or `tier_default`. It lets the next calibration round find
    # its prior (`app/eval/calibration.py`). None when no override applied.
    # Excluded from equality: it describes the rung below, not this score.
    base: tuple[float, str] | None = field(default=None, compare=False)

OverrideKey = tuple[str, "str | None"]   # (model, task_class | None)

OverrideValue = "QualityScore | Iterable[QualityScore]"


class QualityResolver:
    """Resolves a per-model quality score via a fixed provenance ladder.
    Always succeeds (tier_default covers every tier), never raises for an
    unknown model, never blocks a model from routing -- quality only REORDERS
    the eligible set.

    **Overrides (P2b-3 §7)** are DB-sourced scores (`customer` declarations and
    `calibrated` sets), keyed `(model, task_class | None)` -- `task_class` is
    the request's workload slug. `resolve(model, task_class)` checks, in order:

        customer (model, task_class) -> customer (model, None)
        -> calibrated (model, task_class) -> calibrated (model, None)
        -> published -> tier_default

    An operator's `customer` declaration is never overridden by a calibration,
    not even by a more specific per-workload one. To make that decidable the
    snapshot keeps customer and calibrated candidates SEPARATE per key. The
    snapshot is replaced BY REFERENCE (one atomic assignment of a
    `(customer, calibrated)` pair), so the router reads it synchronously with
    no awaits and no lock; one resolver is shared app-wide
    (`app.state.quality`) and refreshed by `app.main`. A bare model-name key
    is accepted too and means `(model, None)`.

    `registry`, when given, resolves tier/published-quality over the merged
    pool (static YAML + enabled onboarded rows) instead of just the static
    YAML pool -- otherwise every onboarded on-prem model (which never appears
    in `settings.routing.model_pool`) would fall through to the flat
    `_TIER_DEFAULT_FLOOR`. The registry is read LIVE: the maps are rebuilt
    whenever the registry's `generation` moves (every `refresh`), so a model
    onboarded after boot resolves to its own tier, not the floor."""

    def __init__(self, settings: Settings,
                 overrides: "Mapping[OverrideKey | str, OverrideValue] | None" = None,
                 registry: "ModelRegistry | None" = None) -> None:
        self._settings = settings
        self._registry = registry
        self._tier_quality = dict(settings.routing.tier_quality)
        self._pool_gen: object = object()   # never equals a registry generation
        self._tier_of: dict[str, str] = {}
        self._published: dict[str, float] = {}
        if registry is None:
            self._build_maps(settings.routing.model_pool)
        self._snapshot: tuple[dict[OverrideKey, QualityScore],
                              dict[OverrideKey, QualityScore]] = ({}, {})
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

    def set_overrides(self, mapping: "Mapping[OverrideKey | str, OverrideValue]") -> None:
        """Swap the override snapshot. Each value is one `QualityScore` or an
        iterable of them (a key may carry BOTH a customer and a calibrated
        candidate); each is filed under its own `source`. Builds the new maps
        first, then replaces the reference in one assignment -- a concurrent
        `resolve` sees either the old snapshot or the new one, never a mix.

        Defence in depth (spec §8): an entry whose score is non-finite or
        outside [0, 1], or whose source is not an override source, is skipped
        with a warning. The store and the apply route validate too, but a bad
        row must never be able to move the quality term by more than
        `weights.quality`."""
        log = logging.getLogger("serving.routing")
        customer: dict[OverrideKey, QualityScore] = {}
        calibrated: dict[OverrideKey, QualityScore] = {}
        for key, value in mapping.items():
            k = (key, None) if isinstance(key, str) else (key[0], key[1])
            items = [value] if isinstance(value, QualityScore) else list(value)
            for q in items:
                try:
                    ok = math.isfinite(q.score) and 0.0 <= q.score <= 1.0
                except TypeError:
                    ok = False
                if not ok:
                    log.warning(
                        "quality override skipped: model=%s workload=%s score=%r "
                        "is not a finite value in [0, 1]", k[0], k[1], q.score)
                    continue
                if q.source == "customer":
                    customer[k] = q
                elif q.source == "calibrated":
                    calibrated[k] = q
                else:
                    log.warning(
                        "quality override skipped: model=%s workload=%s source=%r "
                        "is not an override source", k[0], k[1], q.source)
        self._snapshot = (customer, calibrated)

    def override_maps(self) -> tuple[dict[OverrideKey, QualityScore],
                                     dict[OverrideKey, QualityScore]]:
        """Copies of the current `(customer, calibrated)` override maps --
        for the calibration dry-run, which must reason about what an applied
        set would and would not win against."""
        customer, calibrated = self._snapshot
        return dict(customer), dict(calibrated)

    def customer_override(self, model: str,
                          task_class: str | None = None) -> QualityScore | None:
        """The customer declaration `resolve(model, task_class)` would use,
        if any: customer (model, task_class), then customer (model, None).
        When this is not None no calibration can win for that request."""
        customer, _ = self._snapshot
        return _first(customer, model, task_class)

    def _base(self, model: str) -> QualityScore:
        """The ladder below every override: published, then tier default."""
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

    def resolve(self, model: str, task_class: str | None = None) -> QualityScore:
        customer, calibrated = self._snapshot   # one read: a stable snapshot
        ov = _first(customer, model, task_class) or _first(calibrated, model, task_class)
        base = self._base(model)
        if ov is None:
            return base
        return dataclasses.replace(ov, base=(base.score, base.source))


def _first(overrides: dict[OverrideKey, QualityScore], model: str,
           task_class: str | None) -> QualityScore | None:
    """`(model, task_class)` then `(model, None)` within ONE source's map."""
    if task_class is not None:
        ov = overrides.get((model, task_class))
        if ov is not None:
            return ov
    return overrides.get((model, None))
