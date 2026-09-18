from __future__ import annotations

import re
from typing import Protocol

from app.models import Entity
from app.policy.merge import dedupe

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


def detection_ran(det: object) -> bool:
    """"No detector ran" is the NullDetector and nothing else -- an app with
    `pii_detection: off` but custom recognizers layered on top
    (EffectiveDetector) HAS scanned. `Gate.evaluate` and the pipeline's
    decision-certificate builder must never disagree about this, so both call
    this one predicate instead of each inlining `isinstance(det, NullDetector)`."""
    return not isinstance(det, NullDetector)


class PresidioDetector:
    """Real Presidio path (demo default). Lazy import so tests never need it.

    Supplements Presidio with the regex US_SSN pattern (B4): Presidio's
    UsSsnRecognizer deliberately blocklists textbook sample SSNs (e.g.
    123-45-6789), which would otherwise slip past BOTH the gate and egress
    verification under the demo-default config. Only US_SSN is layered --
    Presidio's own EMAIL/PHONE/CREDIT_CARD recognizers are stronger and are
    left as-is. dedupe() ensures a valid SSN Presidio already matched is not
    double-covered."""

    _SSN = _PATTERNS["US_SSN"]

    def __init__(self) -> None:
        from presidio_analyzer import AnalyzerEngine
        self._analyzer = AnalyzerEngine()

    def analyze(self, text: str) -> list[Entity]:
        results = self._analyzer.analyze(text=text, language="en")
        # Carry Presidio's confidence onto the Entity: dedupe already treats a
        # missing score as 0.0 (so this only ever SHARPENS same-span tie-breaks,
        # and the SSN backstop's score=1.0 still wins), and it is what a per-app
        # `pii_score_threshold` (FilteredDetector) needs to cull low-confidence
        # matches like US_DRIVER_LICENSE/DATE_TIME while keeping PERSON/EMAIL.
        ents = [Entity(type=r.entity_type, start=r.start, end=r.end,
                       value=text[r.start:r.end], score=r.score) for r in results]
        # score=1.0 is LOAD-BEARING, not cosmetic. An exact SSN-format match is a
        # high-confidence privacy signal that MUST win dedupe's same-span overlap
        # resolution. spaCy nondeterministically tags an SSN like 818-30-4221 as
        # DATE_TIME/PHONE_NUMBER (818 is a real area code); without a decisive
        # score dedupe keeps that overlapping Presidio entity and drops US_SSN,
        # which would DOWNGRADE the request out of restricted/onprem_only
        # (policy.yaml) and let an SSN route to cloud (rule 3/8). The regex
        # backstop (B4) exists precisely so an SSN never slips the gate; the
        # score makes that guarantee survive dedupe. Fail-safe: an SSN-format
        # string is always treated as the most sensitive interpretation.
        ents += [Entity(type="US_SSN", start=m.start(), end=m.end(),
                        value=m.group(), score=1.0) for m in self._SSN.finditer(text)]
        return dedupe(ents)

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
