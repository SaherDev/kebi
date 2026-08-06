"""Area-layer domain models (location-kinds Step 2, widened Step 6).

`AreaEntity` is identity + geometry only — the entity store never holds
experiential data; that stays in the knowledge layer keyed by the same
`entity_key` (ADR-118 spirit). `AreaSummary` is how an area leaves the service
in an answer, once Step 6 let areas win one.
"""

from __future__ import annotations

import math
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict

from kebi.core.knowledge.schemas import PlaceNote

AreaKind = Literal[
    "country",
    "region",
    "city",
    "neighborhood",
    "natural_feature",
    "street",
]

# The span (longest side, km) a real feature of each kind plausibly has.
#
# The provider's viewport is not an extent — it is whatever box the provider
# felt like drawing, and for a linear feature it is routinely wrong by an order
# of magnitude: Google returns a ~0.4 km box for Hai Van Pass, which is a ~20 km
# road. Searching inside that box would find nothing and rendering it would draw
# a shaded square over one hairpin.
#
# So a bbox is trusted only when its span falls inside the band its kind makes
# plausible. Outside the band the extent is not *corrected* — kebi has no better
# geometry and will not invent one — it is simply not used, and the area
# degrades to a point with a radius. That is the honest failure, and it is
# invisible to the user because a point-anchored area still searches and still
# answers.
_PLAUSIBLE_SPAN_KM: dict[AreaKind, tuple[float, float]] = {
    "country": (20.0, 20_000.0),
    "region": (5.0, 3_000.0),
    "city": (1.5, 500.0),
    "neighborhood": (0.3, 30.0),
    "natural_feature": (0.5, 200.0),
    "street": (0.2, 50.0),
}

# Rough km per degree of latitude; longitude is scaled by cos(lat). Good enough
# for a plausibility band — this is a sanity check, not a distance calculation.
_KM_PER_DEG_LAT = 111.0


def extent_span_km(bbox: list[float] | None, lat: float) -> float | None:
    """Longest side of a `[min_lat, max_lat, min_lng, max_lng]` box, in km."""
    if not bbox or len(bbox) != 4:
        return None
    min_lat, max_lat, min_lng, max_lng = bbox
    lat_km = abs(max_lat - min_lat) * _KM_PER_DEG_LAT
    lng_km = (
        abs(max_lng - min_lng)
        * _KM_PER_DEG_LAT
        * max(0.01, math.cos(math.radians(lat)))
    )
    return max(lat_km, lng_km)


def has_usable_extent(entity: AreaEntity) -> bool:
    """Whether this area's stored bbox can be trusted as its real extent.

    False means "no geometry good enough to search inside or shade" — the
    caller anchors on the centroid with a radius instead. The bbox is still
    kept on the row; this decides whether to *believe* it, not whether to
    store it.
    """
    span = extent_span_km(entity.bbox, entity.lat)
    if span is None:
        return False
    low, high = _PLAUSIBLE_SPAN_KM[entity.entity_type]
    return low <= span <= high


class AreaEntity(BaseModel):
    """A verified geographic area — the shared notion every subsystem keys on.

    `entity_key` is the `build_geo_key` format (`vn`, `vn/hoi-an`) — the
    same key knowledge claims carry, so claims and entities join without
    translation. `bbox` is `[min_lat, max_lat, min_lng, max_lng]`.
    """

    model_config = ConfigDict(frozen=True)

    entity_key: str
    entity_type: AreaKind
    name: str
    aliases: list[str] = []
    country_code: str
    lat: float
    lng: float
    bbox: list[float] | None = None
    place_type: str | None = None
    parent_key: str | None = None
    provider_id: str | None = None
    geo_refreshed_at: datetime | None = None


class AreaSummary(BaseModel):
    """An area as it leaves the service in an answer (location-kinds Step 6).

    A deliberate DTO rather than the entity itself (ADR-105): the answer names
    exactly what the client renders and nothing about how the entity is stored
    — no `provider_id`, no `geo_refreshed_at`, no aliases.

    `extent` is the bbox **only when it is believable** (`has_usable_extent`);
    otherwise it is None and the client renders a point. An area is drawn as a
    shaded extent, never a pin, so handing over a provider viewport that is
    ten times too small would draw a confident lie.

    `notes` are the knowledge layer's claims for this entity — the "why this
    area" evidence, and the part a general assistant cannot reproduce.
    """

    model_config = ConfigDict(frozen=True)

    entity_key: str
    name: str
    kind: AreaKind
    lat: float
    lng: float
    extent: list[float] | None = None
    parent_key: str | None = None
    notes: list[PlaceNote] = []

    @classmethod
    def from_entity(
        cls, entity: AreaEntity, *, notes: list[PlaceNote] | None = None
    ) -> AreaSummary:
        return cls(
            entity_key=entity.entity_key,
            name=entity.name,
            kind=entity.entity_type,
            lat=entity.lat,
            lng=entity.lng,
            extent=entity.bbox if has_usable_extent(entity) else None,
            parent_key=entity.parent_key,
            notes=notes or [],
        )


class AreaContext(BaseModel):
    """The location context a noted non-venue name arrived with (ADR-082
    per-candidate area): where the share placed it, used to resolve the
    containing area when the name itself is not an area."""

    model_config = ConfigDict(frozen=True)

    city: str | None = None
    country: str | None = None
    country_code: str | None = None
