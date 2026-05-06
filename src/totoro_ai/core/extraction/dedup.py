"""Validated-candidate deduplication.

`dedup_validated_by_provider_id` collapses two `ValidatedCandidate`s
that share a `provider_id` (namespaced `{provider}:{external_id}`).
Evidence is unioned, confidence becomes max(group), and the
corroboration bonus is applied when the merged evidence has more than
one distinct (producer, medium) pair (capped at `max_score`).
"""

from __future__ import annotations

from totoro_ai.core.config import ConfidenceConfig
from totoro_ai.core.extraction.types import (
    Evidence,
    ValidatedCandidate,
)
from totoro_ai.core.places import PlaceAttributes
from totoro_ai.core.places.repository import build_provider_id


def _merge_evidence(*lists: list[Evidence]) -> list[Evidence]:
    """Concatenate evidence lists preserving first-seen order, dropping
    duplicates so each `(producer, medium, snippet)` triple appears at
    most once. Equality is structural (Evidence is frozen)."""
    seen: set[Evidence] = set()
    merged: list[Evidence] = []
    for source in lists:
        for item in source:
            if item not in seen:
                seen.add(item)
                merged.append(item)
    return merged


def _provider_id(vc: ValidatedCandidate) -> str | None:
    return build_provider_id(vc.provider, vc.external_id)


def dedup_validated_by_provider_id(
    results: list[ValidatedCandidate],
    confidence_config: ConfidenceConfig,
) -> list[ValidatedCandidate]:
    """Collapse validated candidates sharing a `provider_id`.

    Evidence is unioned across the group. Confidence becomes max(group);
    the corroboration bonus is added when the merged evidence has more
    than one distinct `(producer, medium)` pair (capped at `max_score`).
    The first candidate in the group keeps the role of "carrier" — its
    place_name, place_type, etc. survive. Attribute fields the carrier
    left blank are inherited from the rest. `provider_id=None` results
    pass through unchanged.
    """
    if len(results) <= 1:
        return results

    no_id: list[ValidatedCandidate] = []
    by_provider_id: dict[str, list[ValidatedCandidate]] = {}

    for result in results:
        pid = _provider_id(result)
        if pid is None:
            no_id.append(result)
        else:
            by_provider_id.setdefault(pid, []).append(result)

    winners: list[ValidatedCandidate] = []
    for group in by_provider_id.values():
        if len(group) == 1:
            winners.append(group[0])
            continue

        winner = group[0]
        rest = group[1:]
        winner.evidence = _merge_evidence(
            winner.evidence, *(r.evidence for r in rest)
        )
        winner.attributes = _merge_attributes(
            winner.attributes, *(r.attributes for r in rest)
        )
        winner.confidence = max(r.confidence for r in group)
        distinct_pairs = len({(e.producer, e.medium) for e in winner.evidence})
        if distinct_pairs >= 2:
            winner.confidence = min(
                winner.confidence + confidence_config.corroboration_bonus,
                confidence_config.max_score,
            )
        winners.append(winner)

    return no_id + winners


def _merge_attributes(
    winner: PlaceAttributes, *losers: PlaceAttributes
) -> PlaceAttributes:
    """Return a `PlaceAttributes` where any field the winner left empty is
    backfilled from the first loser that has a non-empty value.

    "Empty" means `None` for scalars / nested models, and an empty list for
    list fields. The merge is shallow — nested models (like
    `location_context`) are copied over whole, not field-merged.
    """
    merged: dict[str, object] = winner.model_dump()
    for loser in losers:
        loser_dict = loser.model_dump()
        for key, val in loser_dict.items():
            if _is_empty(merged.get(key)) and not _is_empty(val):
                merged[key] = val
    return PlaceAttributes.model_validate(merged)


def _is_empty(value: object) -> bool:
    if value is None:
        return True
    return isinstance(value, list | tuple | dict) and len(value) == 0
