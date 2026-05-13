"""Bridges between PlaceObject (v2) and extraction-internal candidate shapes.

The pipeline produces v2 `PlaceObject` instances from
`PlacesSearchService.find()` (search-result side) and v2 `PlaceCore`
instances heading into `PlaceUpsertService.upsert_and_embed`
(persistence side). The LLM picker emits a slim picker-internal shape
(`ValidatedCandidate`) that has to be projected from one and into the
other.

This module owns those projections so individual enrichers, the picker,
and the persistence path never need to construct v2 models by hand.
"""

from __future__ import annotations

from dataclasses import dataclass

from kebi.core.places_v2 import (
    LocationContext,
    PlaceCategory,
    PlaceCore,
    PlaceObject,
    PlaceTag,
)


@dataclass(frozen=True)
class PickerCandidateInput:
    """Slim projection of a v2 `PlaceObject` for the LLM picker prompt.

    The picker consumes this — not the full `PlaceObject` — so the
    prompt template stays compact and Google's live fields
    (rating / hours / phone / popularity / business_status) don't leak
    into the LLM context.

    The picker echoes `provider_id` back on its output; the rest of
    the picked place comes from the original `PlaceObject`.
    """

    provider_id: str
    place_name: str
    location_summary: str  # e.g. "Bangkok, Thailand — Sukhumvit Soi 11"
    categories: tuple[PlaceCategory, ...]


def search_results_to_picker_input(
    places: list[PlaceObject],
) -> list[PickerCandidateInput]:
    """Project a list of v2 PlaceObjects into picker prompt rows.

    Drops any place without a `provider_id` (defensive; shouldn't
    happen because `PlacesSearchService` upserts via provider_id).
    """
    rows: list[PickerCandidateInput] = []
    for p in places:
        if p.provider_id is None:
            continue
        rows.append(
            PickerCandidateInput(
                provider_id=p.provider_id,
                place_name=p.place_name,
                location_summary=_format_location(p.location),
                categories=tuple(p.categories),
            )
        )
    return rows


def candidate_to_core(
    *,
    provider_id: str,
    place_name: str,
    categories: list[PlaceCategory],
    tags: list[PlaceTag],
    location: LocationContext | None = None,
) -> PlaceCore:
    """Build a v2 `PlaceCore` from picker-output fields.

    The picker emits these fields directly on a `ValidatedCandidate`
    (post Phase 3); callers spread the candidate into this constructor.
    This indirection isolates the v2 model shape from the picker's
    output dataclass so the picker stays Python-native + Instructor-friendly.
    """
    return PlaceCore(
        provider_id=provider_id,
        place_name=place_name,
        categories=categories,
        tags=tags,
        location=location,
    )


def _format_location(loc: LocationContext | None) -> str:
    if loc is None:
        return ""
    parts = [loc.neighborhood, loc.city, loc.country]
    return ", ".join(p for p in parts if p)
