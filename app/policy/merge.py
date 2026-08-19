"""Redaction-safe entity merge, shared by EffectiveDetector (custom recognizers
over a base detector) and PresidioDetector (regex US_SSN supplement). A leaf
module so both app/policy/effective.py and app/policy/detectors.py import it
without a cycle (effective.py already imports from detectors.py)."""
from __future__ import annotations

from app.models import Entity


def _score(e: Entity) -> float:
    return getattr(e, "score", 0.0) or 0.0


def dedupe(ents: list[Entity]) -> list[Entity]:
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
