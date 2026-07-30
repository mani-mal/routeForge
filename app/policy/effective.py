"""Detector that layers per-app custom recognizers on top of a base detector
(Presidio or regex). Built once per app and cached; see app/apps/privacy.py."""
from __future__ import annotations

from app.models import Entity
from app.policy.detectors import Detector, RegexDetector
from app.policy.recognizers import CustomRecognizer


class EffectiveDetector:
    def __init__(self, base: Detector, recognizers: list[CustomRecognizer]) -> None:
        self._base = base
        self._recognizers = recognizers

    def analyze(self, text: str) -> list[Entity]:
        ents = list(self._base.analyze(text))
        for r in self._recognizers:
            ents.extend(r.find(text))
        return _dedupe(ents)

    def anonymize(self, text: str, entities: list[Entity]):
        # reuse the deterministic placeholder scheme (map symmetry with the gate)
        return RegexDetector().anonymize(text, entities)

    def warmup(self) -> None:
        self._base.warmup()


def _dedupe(ents: list[Entity]) -> list[Entity]:
    # Prefer longer spans, then higher-confidence: sort so the winner for any
    # overlapping region comes first, then greedily drop overlaps.
    def _score(e: Entity) -> float:
        return getattr(e, "score", 0.0) or 0.0
    ents = sorted(ents, key=lambda e: (e.start, -(e.end - e.start), -_score(e)))
    kept: list[Entity] = []
    last_end = -1
    for e in ents:
        if e.start >= last_end:
            kept.append(e)
            last_end = e.end
    return kept
