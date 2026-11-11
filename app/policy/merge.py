"""Redaction-safe entity merge, shared by EffectiveDetector (custom recognizers
over a base detector) and PresidioDetector (regex US_SSN supplement). A leaf
module so both app/policy/effective.py and app/policy/detectors.py import it
without a cycle (effective.py already imports from detectors.py)."""
from __future__ import annotations

from app.models import Entity

# The one entity type that carries an absolute privacy guarantee (CLAUDE.md
# rules 3/8). Defined here, in the leaf module, so `dedupe` can honour it;
# re-exported by app/policy/effective.py, its historical home.
PROTECTED_ENTITIES = frozenset({"US_SSN"})


def is_protected(e: Entity) -> bool:
    """True when `e` is, or (as a merged span) contains, a PROTECTED type --
    the absolute US_SSN guarantee no per-app narrowing may lift."""
    return e.type in PROTECTED_ENTITIES or e.contains_protected


def exempt_from_score_bar(e: Entity) -> bool:
    """True when no score bar may drop `e` (B69's tool-argument bar): an
    unscored match (every RegexDetector hit), an operator-declared custom
    recognizer match, a PROTECTED type (US_SSN), or a merged span any of whose
    cluster members was one of those (`Entity.bar_exempt`, set by `dedupe`)."""
    return (e.bar_exempt or e.score is None or e.source == "custom"
            or is_protected(e))


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
        # `bar_exempt`: the merged span inherits type/score/source from
        # `best` alone, so an exempt member (an SSN, a custom or unscored
        # match) that lost the tie-break would otherwise lose its exemption
        # with it -- and a weak `best` could take the whole region below the
        # tool-argument bar. Protection is carried across the cluster.
        merged.append(Entity(type=best.type, start=start, end=end,
                             value=best.value, score=best.score,
                             source=best.source,
                             bar_exempt=any(exempt_from_score_bar(e)
                                            for e in cluster),
                             contains_protected=any(is_protected(e)
                                                    for e in cluster)))
    return merged
