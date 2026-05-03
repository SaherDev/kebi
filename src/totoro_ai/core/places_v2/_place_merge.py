"""Pure merge policy for PlaceCore.

Service-layer helper. Computes the final state of a place given the existing
row (if any) and an incoming candidate. The repo persists the result without
applying any policy of its own.

Policies (per-column):
    place_name              sticky — first non-empty wins.
    place_name_aliases      dedup by value — existing wins on conflict;
                            new values are appended. Never removed.
    category                sticky — first non-NULL wins.
    tags                    dedup by value — existing wins on conflict;
                            new values are appended. Never removed.
    location                sticky whole-blob — first non-NULL wins.
    id, provider_id         existing wins (identity is fixed once set).
    created_at              existing wins.
    refreshed_at            bumped only when the candidate brought a location
                            and existing had none (i.e. cold→warm transition).
"""

from __future__ import annotations

from typing import TypeVar

from .models import PlaceCore, PlaceNameAlias, PlaceTag

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
            "category": existing.category or candidate.category,
            "tags": _dedup_by_value(existing.tags, candidate.tags),
            "location": existing.location or candidate.location,
            "refreshed_at": (
                candidate.refreshed_at
                if existing.location is None and candidate.location is not None
                else existing.refreshed_at
            ),
        }
    )


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
