"""Detector that layers per-app custom recognizers on top of a base detector
(Presidio or regex). Built once per app and cached; see app/apps/privacy.py."""
from __future__ import annotations

from app.models import Entity
from app.policy.detectors import Detector, RegexDetector
from app.policy.merge import dedupe
from app.policy.recognizers import CustomRecognizer


class EffectiveDetector:
    def __init__(self, base: Detector, recognizers: list[CustomRecognizer]) -> None:
        self._base = base
        self._recognizers = recognizers

    def analyze(self, text: str) -> list[Entity]:
        ents = list(self._base.analyze(text))
        for r in self._recognizers:
            ents.extend(r.find(text))
        return dedupe(ents)

    def anonymize(self, text: str, entities: list[Entity]):
        # reuse the deterministic placeholder scheme (map symmetry with the gate)
        return RegexDetector().anonymize(text, entities)

    def warmup(self) -> None:
        self._base.warmup()
