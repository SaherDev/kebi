"""Candidate deduplication — collapses duplicates at two points in the cascade.

1. `dedup_candidates` — pre-validation dedup on `CandidatePlace` by normalised
   `place.place_name`. Runs before the Google Places call so duplicate
   candidates from different enrichers get a single validation call.

2. `dedup_validated_by_provider_id` — post-validation dedup on
   `ValidatedCandidate`. Two candidates that share a `provider_id`
   (namespaced `{provider}:{external_id}`) get collapsed into one; the
   winner inherits any attribute fields a loser filled in but the winner
   left blank (cuisine, price_hint, ambiance, location_context, dietary,
   good_for). The inheritance is a general attribute merge — not just
   "city" — because the relevant data all lives on `PlaceAttributes` now.
"""

from __future__ import annotations

import re
from dataclasses import replace

from totoro_ai.core.config import ConfidenceConfig
from totoro_ai.core.extraction.types import (
    CandidatePlace,
    ExtractionContext,
    ExtractionLevel,
    ValidatedCandidate,
)
from totoro_ai.core.places import PlaceAttributes
from totoro_ai.core.places.repository import build_provider_id

_LEVEL_ORDER = list(ExtractionLevel)


def _normalize(name: str) -> str:
    """Normalize a place name for dedup comparison."""
    without_punct = re.sub(r"[^\w\s]", "", name, flags=re.UNICODE)
    return " ".join(without_punct.lower().split())


def dedup_candidates(context: ExtractionContext) -> None:
    """Deduplicate context.candidates in-place.

    Groups by normalised `place_name`. When multiple candidates share a
    name the one with the lowest ExtractionLevel index (highest priority)
    wins; it is marked `corroborated=True` and inherits any attribute
    fields a loser had but the winner left blank.
    """
    if len(context.candidates) <= 1:
        return

    groups: dict[str, list[CandidatePlace]] = {}
    for candidate in context.candidates:
        key = _normalize(candidate.place_name)
        groups.setdefault(key, []).append(candidate)

    winners: list[CandidatePlace] = []
    for group in groups.values():
        if len(group) == 1:
            winners.append(group[0])
            continue

        winner = min(group, key=lambda c: _LEVEL_ORDER.index(c.source))
        losers = [c for c in group if c is not winner]
        merged = _merge_attributes(
            winner.attributes, *(c.attributes for c in losers)
        )
        winner.attributes = merged
        winner = replace(winner, corroborated=True)
        winners.append(winner)

    context.candidates = winners


def _provider_id(vc: ValidatedCandidate) -> str | None:
    return build_provider_id(vc.provider, vc.external_id)


def dedup_validated_by_provider_id(
    results: list[ValidatedCandidate],
    confidence_config: ConfidenceConfig,
) -> list[ValidatedCandidate]:
    """Collapse validated candidates sharing a `provider_id`.

    Winner is the entry with the highest-priority `resolved_by`. It gets the
    corroboration bonus (capped at `max_score`) and inherits any attribute
    fields a loser filled in but the winner left blank.
    `provider_id=None` results pass through unchanged.
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

        winner = min(group, key=lambda r: _LEVEL_ORDER.index(r.resolved_by))
        losers = [r for r in group if r is not winner]
        merged = _merge_attributes(
            winner.attributes, *(r.attributes for r in losers)
        )
        winner.attributes = merged
        winner.confidence = min(
            winner.confidence + confidence_config.corroboration_bonus,
            confidence_config.max_score,
        )
        winner.corroborated = True
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
