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


def _score(e: Entity) -> float:
    return getattr(e, "score", 0.0) or 0.0


def _dedupe(ents: list[Entity]) -> list[Entity]:
    # Redaction-safe merge: entities can PARTIALLY overlap (e.g. a custom
    # recognizer matching a substring of a longer built-in match, or vice
    # versa). anonymize() splices one placeholder per entity span, so two
    # overlapping entities can never both be redacted independently --
    # dropping the "loser" of an overlap (as a naive greedy scan would)
    # always risks leaving the loser's non-overlapping tail as raw PII in
    # the output. Instead, group entities that overlap (transitively) into
    # a single cluster and emit ONE entity spanning the whole cluster
    # [min(start), max(end)) -- this guarantees the entire sensitive region
    # is covered by exactly one placeholder. Non-overlapping entities pass
    # through unchanged.
    if not ents:
        return []
    ordered = sorted(ents, key=lambda e: e.start)
    clusters: list[list[Entity]] = [[ordered[0]]]
    cluster_end = ordered[0].end
    for e in ordered[1:]:
        if e.start < cluster_end:
            clusters[-1].append(e)
            cluster_end = max(cluster_end, e.end)
        else:
            clusters.append([e])
            cluster_end = e.end

    merged: list[Entity] = []
    for cluster in clusters:
        if len(cluster) == 1:
            merged.append(cluster[0])
            continue
        start = min(e.start for e in cluster)
        end = max(e.end for e in cluster)
        # "Best" member decides type/score/value: highest score (None -> 0.0),
        # then longest span, as the tiebreak.
        best = max(cluster, key=lambda e: (_score(e), e.end - e.start))
        merged.append(Entity(type=best.type, start=start, end=end,
                              value=best.value, score=best.score))
    return merged
