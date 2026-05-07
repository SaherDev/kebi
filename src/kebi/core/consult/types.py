"""Consult pipeline types — PlaceObject-based (ADR-054, feature 019).

Every "place" flowing between LangGraph nodes is a `PlaceObject`. The ranker
wraps places in `ScoredPlace` for the short window where scores travel
alongside the place; the consult service unwraps them back to `PlaceObject`
before building the response.

External provider results (Google Places Nearby Search) are mapped to
`PlaceObject` via `map_google_place_to_place_object` so the pipeline has one
shape end-to-end.
"""

from __future__ import annotations

import logging
from typing import Any
from uuid import uuid4

from kebi.core.places.models import (
    PlaceAttributes,
    PlaceObject,
    PlaceType,
)
from kebi.core.places.places_client import (
    PlacesMatchResult,
    google_types_to_place_type,
)

logger = logging.getLogger(__name__)


class NoMatchesError(Exception):
    """Raised by ConsultService when no candidates survive the pipeline."""


def map_google_place_to_place_object(google_result: dict[str, Any]) -> PlaceObject:
    """Build a transient `PlaceObject` from a Google Places Nearby Search result.

    These places are NOT persisted — they're consult-only candidates that
    flow through ranking and the response. `place_id` is a synthetic UUID so
    the object satisfies `PlaceObject` invariants; it is never written to
    the DB in this code path.
    """
    geometry = google_result.get("geometry") or {}
    location = geometry.get("location") or {}
    lat = location.get("lat")
    lng = location.get("lng")

    price_level = google_result.get("price_level")
    price_hint = _map_google_price_level(price_level)

    rating = google_result.get("rating")
    popularity = min(1.0, rating / 5.0) if isinstance(rating, int | float) else None

    provider_id = (
        f"google:{google_result['place_id']}" if google_result.get("place_id") else None
    )

    place_type = google_types_to_place_type(google_result.get("types") or [])

    return PlaceObject(
        place_id=str(uuid4()),
        place_name=google_result.get("name", ""),
        place_type=place_type,
        attributes=PlaceAttributes(price_hint=price_hint),
        provider_id=provider_id,
        lat=lat,
        lng=lng,
        address=google_result.get("vicinity"),
        rating=rating if isinstance(rating, int | float) else None,
        popularity=popularity,
        geo_fresh=lat is not None and lng is not None,
    )


def map_match_result_to_place_object(result: PlacesMatchResult) -> PlaceObject:
    """Build a transient PlaceObject from a validated PlacesMatchResult.

    Used by the place_suggestions path. Not persisted; synthetic place_id only.
    """
    provider_id = (
        f"{result.external_provider}:{result.external_id}"
        if result.external_id
        else None
    )
    return PlaceObject(
        place_id=str(uuid4()),
        place_name=result.validated_name or "",
        place_type=PlaceType.services,
        attributes=PlaceAttributes(),
        provider_id=provider_id,
        lat=result.lat,
        lng=result.lng,
        address=result.address,
        geo_fresh=result.lat is not None and result.lng is not None,
    )


def _map_google_price_level(price_level: int | None) -> str | None:
    """Google Places price_level (0-4) → canonical price_hint.

    0          → None (free / unknown)
    1, 2       → "cheap"   (equivalent to legacy "low")
    3          → "moderate" (equivalent to legacy "mid")
    4          → "expensive" (equivalent to legacy "high")
    """
    if price_level is None:
        return None
    if price_level in (1, 2):
        return "cheap"
    if price_level == 3:
        return "moderate"
    if price_level == 4:
        return "expensive"
    return None
