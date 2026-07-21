from __future__ import annotations

import re
from typing import Protocol

from app.models import Entity

_PATTERNS = {
    "US_SSN": re.compile(r"\b\d{3}-\d{2}-\d{4}\b"),
    "CREDIT_CARD": re.compile(r"\b(?:\d[ -]*?){13,16}\b"),
    "EMAIL_ADDRESS": re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b"),
    "PHONE_NUMBER": re.compile(r"\b\d{3}[-.]\d{3}[-.]\d{4}\b"),
    "US_BANK_NUMBER": re.compile(r"\b\d{8,17}\b"),
}


class Detector(Protocol):
    def analyze(self, text: str) -> list[Entity]: ...
    def anonymize(self, text: str, entities: list[Entity]) -> tuple[str, dict[str, str]]: ...


class RegexDetector:
    def analyze(self, text: str) -> list[Entity]:
        out: list[Entity] = []
        for etype, pat in _PATTERNS.items():
            for m in pat.finditer(text):
                out.append(Entity(type=etype, start=m.start(), end=m.end(),
                                  value=m.group()))
        # longest-match wins on overlap; keep highest-priority by pattern order
        out.sort(key=lambda e: (e.start, -(e.end - e.start)))
        deduped: list[Entity] = []
        last_end = -1
        for e in out:
            if e.start >= last_end:
                deduped.append(e)
                last_end = e.end
        return deduped

    def anonymize(self, text: str, entities: list[Entity]) -> tuple[str, dict[str, str]]:
        redacted = text
        mapping: dict[str, str] = {}
        counters: dict[str, int] = {}
        for e in sorted(entities, key=lambda e: e.start, reverse=True):
            counters[e.type] = counters.get(e.type, 0) + 1
            ph = f"<{e.type}_{counters[e.type]}>"
            mapping[ph] = e.value
            redacted = redacted[:e.start] + ph + redacted[e.end:]
        return redacted, mapping


class NullDetector:
    def analyze(self, text: str) -> list[Entity]:
        return []

    def anonymize(self, text: str, entities: list[Entity]) -> tuple[str, dict[str, str]]:
        return text, {}


class PresidioDetector:
    """Real Presidio path (demo default). Lazy import so tests never need it."""

    def __init__(self) -> None:
        from presidio_analyzer import AnalyzerEngine
        from presidio_anonymizer import AnonymizerEngine
        self._analyzer = AnalyzerEngine()
        self._anonymizer = AnonymizerEngine()

    def analyze(self, text: str) -> list[Entity]:
        results = self._analyzer.analyze(text=text, language="en")
        return [Entity(type=r.entity_type, start=r.start, end=r.end,
                       value=text[r.start:r.end]) for r in results]

    def anonymize(self, text: str, entities: list[Entity]) -> tuple[str, dict[str, str]]:
        # reuse the deterministic placeholder scheme for map symmetry
        return RegexDetector().anonymize(text, entities)


def make_detector(mode: str) -> Detector:
    if mode == "regex":
        return RegexDetector()
    if mode == "off":
        return NullDetector()
    if mode == "presidio":
        return PresidioDetector()
    raise ValueError(f"unknown pii_detection mode: {mode}")
