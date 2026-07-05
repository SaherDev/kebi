"""Pure merge policy for PlaceCore.

Service-layer helper. Computes the final state of a place given the existing
row (if any) and an incoming candidate. The repo persists the result without
applying any policy of its own.

Policies (per-column):
    place_name              sticky — first non-empty wins.
    place_name_aliases      dedup by value — existing wins on conflict;
                            new values are appended. Never removed.
    categories              order-preserving union — existing order kept,
                            new categories appended. Never removed.
    tags                    dedup by value — existing wins on conflict;
                            new values are appended. Never removed.
    icon                    sticky — first non-empty wins. The consult
                            write-through constantly re-upserts icon-less
                            candidates over existing rows, so a None
                            candidate must never clear a real icon; and
                            first-wins keeps the icon stable across
                            repeated extractions of the same venue.
    location                sticky whole-blob — first non-NULL wins.
    id, provider_id         existing wins (identity is fixed once set).
    created_at              existing wins.
    refreshed_at            bumped only when the candidate brought a location
                            and existing had none (i.e. cold→warm transition).
"""

from __future__ import annotations

from typing import TypeVar

from .models import PlaceCategory, PlaceCore, PlaceNameAlias, PlaceTag

T = TypeVar("T", PlaceTag, PlaceNameAlias)


def merge_place(existing: PlaceCore | None, candidate: PlaceCore) -> PlaceCore:
    """Compute the final PlaceCore to persist.

    `existing` is the row currently in the DB (or None for first write).
    `candidate` is the incoming write.
    """
    if existing is None:
        return candidate

    return existing.model_copy(
        update={
            "place_name": existing.place_name or candidate.place_name,
            "place_name_aliases": _dedup_by_value(
                existing.place_name_aliases, candidate.place_name_aliases
            ),
            "categories": _merge_categories(
                existing.categories, candidate.categories
            ),
            "tags": _dedup_by_value(existing.tags, candidate.tags),
            "icon": existing.icon or candidate.icon,
            "location": existing.location or candidate.location,
            "refreshed_at": (
                candidate.refreshed_at
                if existing.location is None and candidate.location is not None
                else existing.refreshed_at
            ),
        }
    )


def _merge_categories(
    existing: list[PlaceCategory], incoming: list[PlaceCategory]
) -> list[PlaceCategory]:
    """Order-preserving union — keep `existing` order, append unseen `incoming`.

    Categories are an enum, not a value-bearing object, so equality-based
    dedup is enough; no need for the `value`-keyed shape `_dedup_by_value`
    uses.
    """
    seen: set[PlaceCategory] = set(existing)
    merged = list(existing)
    for cat in incoming:
        if cat in seen:
            continue
        seen.add(cat)
        merged.append(cat)
    return merged


def _dedup_by_value(existing: list[T], incoming: list[T]) -> list[T]:
    """Append items from `incoming` whose `value` is not already in `existing`.

    Existing items keep their order and source. Incoming items keep their
    relative order. Duplicates within `incoming` collapse to first occurrence.
    """
    seen: set[object] = {item.value for item in existing}
    merged: list[T] = list(existing)
    for item in incoming:
        if item.value in seen:
            continue
        seen.add(item.value)
        merged.append(item)
    return merged
