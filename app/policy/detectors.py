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
    def warmup(self) -> None: ...


# Exercises a recognizer and the NLP pipeline so lazy first-call loading happens
# at boot, not on the first real request. Kept small and PII-shaped on purpose.
_WARMUP_TEXT = "Warm up the analyzer: email a@b.co, ssn 123-45-6789."


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
            last_end = max(last_end, e.end)
        return deduped

    def anonymize(self, text: str, entities: list[Entity]) -> tuple[str, dict[str, str]]:
        # Pass 1 (left-to-right): assign stable placeholders in reading order.
        counters: dict[str, int] = {}
        placeholders: list[tuple[Entity, str]] = []
        mapping: dict[str, str] = {}
        for e in sorted(entities, key=lambda e: e.start):
            counters[e.type] = counters.get(e.type, 0) + 1
            ph = f"<{e.type}_{counters[e.type]}>"
            placeholders.append((e, ph))
            mapping[ph] = e.value
        # Pass 2 (right-to-left): splice so earlier indices stay valid.
        redacted = text
        for e, ph in sorted(placeholders, key=lambda item: item[0].start, reverse=True):
            redacted = redacted[:e.start] + ph + redacted[e.end:]
        return redacted, mapping

    def warmup(self) -> None:
        # Regex patterns compile at module import; nothing to warm.
        return None


class NullDetector:
    def analyze(self, text: str) -> list[Entity]:
        return []

    def anonymize(self, text: str, entities: list[Entity]) -> tuple[str, dict[str, str]]:
        return text, {}

    def warmup(self) -> None:
        return None


class PresidioDetector:
    """Real Presidio path (demo default). Lazy import so tests never need it."""

    def __init__(self) -> None:
        from presidio_analyzer import AnalyzerEngine
        self._analyzer = AnalyzerEngine()

    def analyze(self, text: str) -> list[Entity]:
        results = self._analyzer.analyze(text=text, language="en")
        return [Entity(type=r.entity_type, start=r.start, end=r.end,
                       value=text[r.start:r.end]) for r in results]

    def anonymize(self, text: str, entities: list[Entity]) -> tuple[str, dict[str, str]]:
        # reuse the deterministic placeholder scheme for map symmetry
        return RegexDetector().anonymize(text, entities)

    def warmup(self) -> None:
        # AnalyzerEngine() (in __init__) loads the spaCy model, but the NLP
        # pipeline and recognizer registry lazy-initialize on the FIRST analyze()
        # call — that first call is what paid cold-start latency and occasionally
        # tripped the fail-closed gate into a 503 on a demo's opening request.
        # Run one throwaway analyze at boot so real traffic never pays it.
        self.analyze(_WARMUP_TEXT)


def make_detector(mode: str) -> Detector:
    if mode == "regex":
        return RegexDetector()
    if mode == "off":
        return NullDetector()
    if mode == "presidio":
        return PresidioDetector()
    raise ValueError(f"unknown pii_detection mode: {mode}")
