"""Pydantic models for PlacesService (ADR-054, feature 019).

Single source of truth for the "place" shape that flows between every service
in this repo. `PlaceObject` is the unified return type for all read and write
operations; `PlaceCreate` is the write-side input. Cache-tier data lives in
`GeoData` (Tier 2) and `PlaceEnrichment` (Tier 3).

No other module constructs or parses the provider-namespaced `provider_id`
string — that is exclusively `PlacesRepository._build_provider_id()` (construct)
and `PlacesService._strip_namespace()` (parse).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field, model_validator
from typing_extensions import TypedDict

# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class PlaceType(str, Enum):
    food_and_drink = "food_and_drink"
    things_to_do = "things_to_do"
    shopping = "shopping"
    services = "services"
    accommodation = "accommodation"


class PlaceSource(str, Enum):
    tiktok = "tiktok"
    instagram = "instagram"
    youtube = "youtube"
    google_maps = "google_maps"
    manual = "manual"


class PlaceProvider(str, Enum):
    google = "google"
    foursquare = "foursquare"
    manual = "manual"


# ---------------------------------------------------------------------------
# Subcategory vocabulary — validated at the Pydantic boundary, not the DB
# ---------------------------------------------------------------------------

_SUBCATEGORIES: dict[PlaceType, frozenset[str]] = {
    PlaceType.food_and_drink: frozenset(
        {
            "restaurant",
            "fast_food",
            "cafe",
            "bar",
            "bakery",
            "food_truck",
            "brewery",
            "dessert_shop",
            "market",
        }
    ),
    PlaceType.things_to_do: frozenset(
        {
            "nature",
            "cultural_site",
            "museum",
            "nightlife",
            "experience",
            "wellness",
            "event_venue",
        }
    ),
    PlaceType.shopping: frozenset(
        {
            "market",
            "boutique",
            "mall",
            "bookstore",
            "specialty_store",
        }
    ),
    PlaceType.services: frozenset(
        {
            "coworking",
            "laundry",
            "pharmacy",
            "atm",
            "car_rental",
            "barbershop",
        }
    ),
    PlaceType.accommodation: frozenset(
        {
            "hotel",
            "hostel",
            "rental",
            "unique_stay",
        }
    ),
}


# ---------------------------------------------------------------------------
# Structured attributes (stored as JSONB on places.attributes)
# ---------------------------------------------------------------------------


class LocationContext(BaseModel):
    neighborhood: str | None = None
    city: str | None = None
    country: str | None = None

    model_config = ConfigDict(extra="forbid")


class PlaceAttributes(BaseModel):
    cuisine: str | None = None
    price_hint: str | None = None
    ambiance: str | None = None
    dietary: list[str] = Field(default_factory=list)
    good_for: list[str] = Field(default_factory=list)
    location_context: LocationContext | None = None

    model_config = ConfigDict(extra="forbid")


# ---------------------------------------------------------------------------
# HoursDict (TypedDict — round-trips cleanly through JSON for Redis)
# ---------------------------------------------------------------------------


class HoursDict(TypedDict, total=False):
    sunday: str | None
    monday: str | None
    tuesday: str | None
    wednesday: str | None
    thursday: str | None
    friday: str | None
    saturday: str | None
    timezone: str  # IANA e.g. "Asia/Tokyo" — required when any day key is present


# ---------------------------------------------------------------------------
# Cache-tier Pydantic models
# ---------------------------------------------------------------------------


class GeoData(BaseModel):
    """Tier 2 — cached geo data keyed by `places:geo:{provider_id}`."""

    lat: float
    lng: float
    address: str
    cached_at: datetime

    model_config = ConfigDict(extra="forbid")


class PlaceEnrichment(BaseModel):
    """Tier 3 — cached live details keyed by `places:enrichment:{provider_id}`."""

    hours: HoursDict | None = None
    rating: float | None = None
    phone: str | None = None
    photo_url: str | None = None
    popularity: float | None = None
    fetched_at: datetime

    model_config = ConfigDict(extra="forbid")


# ---------------------------------------------------------------------------
# Unified return type — every read and write path yields this shape
# ---------------------------------------------------------------------------


class PlaceObject(BaseModel):
    """The single shape for a "place" flowing through any service in this repo.

    Tier 1 fields come from PostgreSQL and are always present on any non-None
    return. Tier 2 and Tier 3 fields are populated only when enrich_batch runs
    and writes them through; they are None otherwise.
    """

    # Tier 1 — PostgreSQL, always present
    place_id: str
    place_name: str
    place_type: PlaceType
    subcategory: str | None = None
    tags: list[str] = Field(default_factory=list)
    attributes: PlaceAttributes = Field(default_factory=PlaceAttributes)
    source_url: str | None = None
    source: PlaceSource | None = None
    provider_id: str | None = None  # namespaced; built only by PlacesRepository
    created_at: datetime | None = None  # from ORM; None for freshly-built objects

    # Tier 2 — Redis geo cache
    lat: float | None = None
    lng: float | None = None
    address: str | None = None
    geo_fresh: bool = False

    # Tier 3 — Redis enrichment cache
    hours: HoursDict | None = None
    rating: float | None = None
    phone: str | None = None
    photo_url: str | None = None
    popularity: float | None = None
    enriched: bool = False  # recall mode never sets this True

    model_config = ConfigDict(extra="forbid")

    def copy_with(
        self,
        *,
        geo_fresh: bool = True,
        enriched: bool = True,
    ) -> PlaceObject:
        """Return a copy, stripping tiers whose flag is False.

        `geo_fresh=False` clears Tier 2 (lat, lng, address) and sets geo_fresh=False.
        `enriched=False` clears Tier 3 (hours, rating, phone, photo_url, popularity)
        and sets enriched=False. Defaults preserve both tiers (identity copy).
        """
        updates: dict[str, object] = {}
        if not geo_fresh:
            updates.update(
                {"lat": None, "lng": None, "address": None, "geo_fresh": False}
            )
        if not enriched:
            updates.update(
                {
                    "hours": None,
                    "rating": None,
                    "phone": None,
                    "photo_url": None,
                    "popularity": None,
                    "enriched": False,
                }
            )
        return self.model_copy(update=updates)


# ---------------------------------------------------------------------------
# Write-side input
# ---------------------------------------------------------------------------


class PlaceCreate(BaseModel):
    user_id: str = Field(min_length=1)
    place_name: str = Field(min_length=1)
    place_type: PlaceType
    subcategory: str | None = None
    tags: list[str] = Field(default_factory=list)
    attributes: PlaceAttributes = Field(default_factory=PlaceAttributes)
    source_url: str | None = None
    source: PlaceSource | None = None
    external_id: str | None = None  # raw provider ID, no namespace prefix
    provider: PlaceProvider | None = None

    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="after")
    def _validate(self) -> PlaceCreate:
        # Exactly zero or both of external_id and provider must be present.
        if (self.external_id is None) != (self.provider is None):
            raise ValueError(
                "PlaceCreate: external_id and provider must be both set or both None "
                f"(got external_id={self.external_id!r}, provider={self.provider!r})"
            )

        # subcategory must belong to the vocabulary for its place_type.
        if self.subcategory is not None:
            allowed = _SUBCATEGORIES.get(self.place_type, frozenset())
            if self.subcategory not in allowed:
                raise ValueError(
                    f"PlaceCreate: subcategory {self.subcategory!r} is not valid for "
                    f"place_type {self.place_type.value!r}; allowed={sorted(allowed)}"
                )

        return self


# ---------------------------------------------------------------------------
# Duplicate detection error
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class DuplicateProviderId:
    """One conflict carried by DuplicatePlaceError."""

    provider_id: str  # the namespaced string, e.g. "google:ChIJN1t_..."
    existing_place_id: str  # the internal place_id of the row that already exists


class DuplicatePlaceError(Exception):
    """Raised by PlacesRepository.create / create_batch on provider_id collision.

    `.conflicts` contains one entry per colliding provider_id. For create(),
    always exactly one. For create_batch(), one or more — in the order they
    appeared in the input batch.
    """

    def __init__(self, conflicts: list[DuplicateProviderId]) -> None:
        self.conflicts = conflicts
        provider_ids = ", ".join(c.provider_id for c in conflicts)
        super().__init__(f"Duplicate provider_id(s): {provider_ids}")
