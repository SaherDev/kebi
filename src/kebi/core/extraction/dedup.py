"""Validated-candidate deduplication by namespaced `provider_id`.

`dedup_by_provider_id` collapses two `ValidatedCandidate`s that share a
`provider_id` (e.g. when two enrichment levels both surfaced the same
Google place). Evidence is unioned, confidence becomes max(group),
and the corroboration bonus is applied when the merged evidence has
more than one distinct (producer, medium) pair (capped at `max_score`).

Categories and tags from later picks are merged into the carrier so
no contribution is lost. The carrier (first candidate in the group)
keeps `place_name`, `subcategory`, and `location` — these reflect the
v2 search result they came from and don't differ between picks of the
same place.
"""

from __future__ import annotations

from kebi.core.config import ConfidenceConfig
from kebi.core.extraction.types import (
    Evidence,
    ValidatedCandidate,
)
from kebi.core.places_v2 import PlaceCategory, PlaceTag


def dedup_by_provider_id(
    results: list[ValidatedCandidate],
    confidence_config: ConfidenceConfig,
) -> list[ValidatedCandidate]:
    """Collapse validated candidates sharing a `provider_id`."""
    if len(results) <= 1:
        return results

    by_provider_id: dict[str, list[ValidatedCandidate]] = {}
    order: list[str] = []
    for result in results:
        pid = result.provider_id
        if pid not in by_provider_id:
            by_provider_id[pid] = []
            order.append(pid)
        by_provider_id[pid].append(result)

    winners: list[ValidatedCandidate] = []
    for pid in order:
        group = by_provider_id[pid]
        if len(group) == 1:
            winners.append(group[0])
            continue

        winner = group[0]
        rest = group[1:]
        winner.evidence = _merge_evidence(
            winner.evidence, *(r.evidence for r in rest)
        )
        winner.categories = _merge_categories(
            winner.categories, *(r.categories for r in rest)
        )
        winner.tags = _merge_tags(winner.tags, *(r.tags for r in rest))
        winner.confidence = max(r.confidence for r in group)
        distinct_pairs = len({(e.producer, e.medium) for e in winner.evidence})
        if distinct_pairs >= 2:
            winner.confidence = min(
                winner.confidence + confidence_config.corroboration_bonus,
                confidence_config.max_score,
            )
        winners.append(winner)

    return winners


def _merge_evidence(*lists: list[Evidence]) -> list[Evidence]:
    seen: set[Evidence] = set()
    merged: list[Evidence] = []
    for source in lists:
        for item in source:
            if item not in seen:
                seen.add(item)
                merged.append(item)
    return merged


def _merge_categories(
    *lists: list[PlaceCategory],
) -> list[PlaceCategory]:
    """Order-preserving union of category lists."""
    seen: set[PlaceCategory] = set()
    merged: list[PlaceCategory] = []
    for source in lists:
        for cat in source:
            if cat not in seen:
                seen.add(cat)
                merged.append(cat)
    return merged


def _merge_tags(*lists: list[PlaceTag]) -> list[PlaceTag]:
    """Order-preserving union of tag lists, deduped on (type, value)."""
    seen: set[tuple[str, str]] = set()
    merged: list[PlaceTag] = []
    for source in lists:
        for tag in source:
            key = (str(tag.type), str(tag.value))
            if key not in seen:
                seen.add(key)
                merged.append(tag)
    return merged
