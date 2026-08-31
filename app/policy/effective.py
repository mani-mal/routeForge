"""Detector that layers per-app custom recognizers on top of a base detector
(Presidio or regex). Built once per app and cached; see app/apps/privacy.py."""
from __future__ import annotations

import re
from collections.abc import Iterable

from app.models import Entity
from app.policy.detectors import Detector, RegexDetector
from app.policy.merge import dedupe
from app.policy.recognizers import CustomRecognizer

# The one entity type that carries an absolute privacy guarantee (CLAUDE.md
# rules 3/8): an SSN must never slip the gate, so an app can neither denylist it
# nor threshold it away. PresidioDetector seeds it with score=1.0 precisely so
# it survives dedupe; FilteredDetector honors the same contract on egress from
# the filter, so no per-app tuning can ever un-redact an SSN.
PROTECTED_ENTITIES = frozenset({"US_SSN"})


class FilteredDetector:
    """Drops false-positive entity types (per-app `pii_entity_denylist`),
    entities scoring below a per-app `pii_score_threshold`, and entities whose
    detected VALUE matches a per-app `pii_exempt_patterns` regex, BEFORE
    redaction and classification -- so an app can suppress a recognizer that
    mangles its data (e.g. Presidio tagging a shop-order number
    `US_DRIVER_LICENSE`, or a revision date `DATE_TIME`) WITHOUT disabling the
    whole gate. This is the narrow lever that lets an app keep PERSON/EMAIL
    detection -- and stay onprem_only -- while exempting exactly the types (or
    value shapes) that are destroying mandatory field values.

    `pii_exempt_patterns` is the type-independent complement to the denylist:
    Presidio can assign the SAME false-positive value DIFFERENT entity types
    depending on surrounding whitespace (e.g. a compact drawing number scoring
    `PHONE_NUMBER` vs the spaced form scoring `DATE_TIME`), so chasing types is
    whack-a-mole. A pattern matches the entity's extracted VALUE via
    `re.fullmatch` (not `search`) regardless of which type Presidio picked.

    `PROTECTED_ENTITIES` (US_SSN) can never be denylisted, thresholded, or
    pattern-exempted away: the denylist is filtered clean of them at
    construction, the threshold check skips them, and the pattern-exemption
    check skips them too, so rules 3/8 hold no matter how the app is
    configured -- no regex, however SSN-shaped, can un-redact one. Entities
    with no score (RegexDetector, custom recognizers) are never dropped by the
    threshold -- it only ever culls low-confidence *scored* matches."""

    def __init__(self, base: Detector, denylist: Iterable[str] | None = None,
                 score_threshold: float | None = None,
                 exempt_patterns: Iterable[str] | None = None) -> None:
        self._base = base
        self._deny = frozenset(denylist or ()) - PROTECTED_ENTITIES
        self._threshold = score_threshold
        self._exempt = [re.compile(p) for p in (exempt_patterns or ())]

    def _keep(self, e: Entity) -> bool:
        if e.type in self._deny:
            return False
        if (self._threshold is not None and e.type not in PROTECTED_ENTITIES
                and e.score is not None and e.score < self._threshold):
            return False
        if (e.type not in PROTECTED_ENTITIES and self._exempt
                and any(p.fullmatch(e.value or "") for p in self._exempt)):
            return False
        return True

    def analyze(self, text: str) -> list[Entity]:
        return [e for e in self._base.analyze(text) if self._keep(e)]

    def anonymize(self, text: str, entities: list[Entity]):
        return self._base.anonymize(text, entities)

    def warmup(self) -> None:
        self._base.warmup()


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
