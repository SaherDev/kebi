"""Domain models for the places_v2 library.

Three core classes:
- PlaceCore: DB-side curated and locational data, shared across all users.
- PlaceObject: extends PlaceCore with Google-derived live fields (cache-only).
- UserPlace: per (user, place) pair.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from typing_extensions import TypedDict

from .tags import TagType, TagValue


class PlaceSource(str, Enum):
    tiktok = "tiktok"
    instagram = "instagram"
    youtube = "youtube"
    google_maps_list = "google_maps_list"
    manual = "manual"
    totoro = "totoro"


class PlaceCategory(str, Enum):
    # food & drink
    restaurant = "restaurant"
    cafe = "cafe"
    bar = "bar"
    pub = "pub"
    bakery = "bakery"
    dessert_shop = "dessert_shop"
    ice_cream_shop = "ice_cream_shop"
    street_food = "street_food"
    food_court = "food_court"
    food_market = "food_market"
    brewery = "brewery"
    winery = "winery"
    distillery = "distillery"
    tea_house = "tea_house"
    juice_bar = "juice_bar"
    # retail
    grocery_store = "grocery_store"
    supermarket = "supermarket"
    convenience_store = "convenience_store"
    shopping_mall = "shopping_mall"
    boutique = "boutique"
    bookstore = "bookstore"
    specialty_shop = "specialty_shop"
    farmers_market = "farmers_market"
    flea_market = "flea_market"
    night_market = "night_market"
    pharmacy = "pharmacy"
    electronics_store = "electronics_store"
    # culture / sightseeing
    museum = "museum"
    art_gallery = "art_gallery"
    historical_site = "historical_site"
    monument = "monument"
    temple = "temple"
    church = "church"
    mosque = "mosque"
    shrine = "shrine"
    landmark = "landmark"
    viewpoint = "viewpoint"
    # entertainment
    theme_park = "theme_park"
    amusement_park = "amusement_park"
    zoo = "zoo"
    aquarium = "aquarium"
    botanical_garden = "botanical_garden"
    cinema = "cinema"
    theater = "theater"
    concert_hall = "concert_hall"
    live_music_venue = "live_music_venue"
    nightclub = "nightclub"
    comedy_club = "comedy_club"
    karaoke = "karaoke"
    arcade = "arcade"
    bowling_alley = "bowling_alley"
    billiards_hall = "billiards_hall"
    # nature / outdoors
    park = "park"
    beach = "beach"
    hiking_trail = "hiking_trail"
    lake = "lake"
    river = "river"
    garden = "garden"
    campground = "campground"
    scenic_lookout = "scenic_lookout"
    # fitness / wellness
    gym = "gym"
    fitness_studio = "fitness_studio"
    yoga_studio = "yoga_studio"
    pilates_studio = "pilates_studio"
    spa = "spa"
    massage = "massage"
    hot_spring = "hot_spring"
    bathhouse = "bathhouse"
    salon = "salon"
    barber = "barber"
    # services / utilities
    atm = "atm"
    bank = "bank"
    post_office = "post_office"
    gas_station = "gas_station"
    parking = "parking"
    laundry = "laundry"
    # accommodation
    hotel = "hotel"
    hostel = "hostel"
    guesthouse = "guesthouse"
    bed_and_breakfast = "bed_and_breakfast"
    resort = "resort"
    vacation_rental = "vacation_rental"
    # transit
    airport = "airport"
    train_station = "train_station"
    metro_station = "metro_station"
    bus_terminal = "bus_terminal"
    ferry_terminal = "ferry_terminal"
    # sport / recreation
    stadium = "stadium"
    arena = "arena"
    sports_club = "sports_club"
    swimming_pool = "swimming_pool"
    climbing_gym = "climbing_gym"
    skate_park = "skate_park"
    golf_course = "golf_course"
    # work / study
    coworking_space = "coworking_space"
    library = "library"
    study_cafe = "study_cafe"


# weekday → list of "HH:MM-HH:MM" ranges, plus "timezone" key for IANA string.
# total=True: the only constructor (_google_mapper._map_hours) always sets all
# 7 day keys + timezone. Empty days are stored as []; closed-all-day as
# ["00:00-00:00"]. Any future constructor must populate every key.
class HoursDict(TypedDict):
    sunday: list[str]
    monday: list[str]
    tuesday: list[str]
    wednesday: list[str]
    thursday: list[str]
    friday: list[str]
    saturday: list[str]
    timezone: str  # IANA e.g. "Asia/Tokyo"


class LocationContext(BaseModel):
    """Location container used in PlaceQuery and optionally PlaceCore.attributes."""

    lat: float | None = None
    lng: float | None = None
    address: str | None = None
    radius_m: int | None = None
    neighborhood: str | None = None
    city: str | None = None
    country: str | None = None

    model_config = ConfigDict(extra="forbid")


class PlaceTag(BaseModel):
    type: TagType | str  # TagType for known types; plain str for LLM custom types
    value: TagValue      # known enum value (CuisineTag, FeatureTag, …) or free-text str
    source: str          # "google" | "llm" | "manual" | "tiktok" | ...


class PlaceNameAlias(BaseModel):
    """Alternative name for a place contributed by a non-canonical source.

    The canonical place_name comes from the provider (e.g. Google). Aliases
    track names from other writers (TikTok captions, user notes, LLM extracts)
    for richer search and provenance. Deduped by `value` at merge time;
    first writer of a given alias value wins.
    """

    value: str
    source: str          # "tiktok" | "instagram" | "user" | "llm" | ...


SortField = Literal["created_at", "refreshed_at", "place_name", "category"]


class PlaceQuery(BaseModel):
    """Structured search query. All fields optional, combined with AND.

    DB filters:  place_name, category, tags, location, created_after/before, sort_*
    Client hints: open_now (passed through to the search client)
    """

    # DB filters
    place_name: str | None = None    # ILIKE on DB; also drives client text search
    category: PlaceCategory | None = None
    tags: list[str] | None = None   # tag values; all must be present (AND)
    location: LocationContext | None = None

    # date range (DB)
    created_after: datetime | None = None
    created_before: datetime | None = None

    # ordering (DB)
    sort_by: SortField | None = None
    sort_desc: bool = True

    # client hints (ignored for DB queries)
    open_now: bool | None = None     # only return currently open places

    @model_validator(mode="after")
    def _validate_geo_location(self) -> PlaceQuery:
        loc = self.location
        if loc is None:
            return self
        if (loc.lat is not None or loc.lng is not None) and loc.radius_m is None:
            raise ValueError(
                "location.radius_m is required when lat or lng is provided"
            )
        return self


class PlaceCore(BaseModel):
    """Canonical place data. DB-side. Same for all users.

    Curated fields are mergeable on upsert. Locational fields are refreshable
    after a 30-day TTL wipe (Google ToS compliance).
    """

    # identity
    id: str | None = None
    # namespaced: "<provider>:<id>", e.g. "google:ChIJ..."
    provider_id: str | None = None

    @field_validator("provider_id")
    @classmethod
    def _validate_provider_id(cls, v: str | None) -> str | None:
        if v is not None and ":" not in v:
            raise ValueError(
                f"provider_id must be namespaced (e.g. 'google:ChIJ...'), got: {v!r}"
            )
        return v

    # core (mergeable)
    place_name: str
    place_name_aliases: list[PlaceNameAlias] = Field(default_factory=list)
    category: PlaceCategory | None = None
    tags: list[PlaceTag] = Field(default_factory=list)

    # location (Google-derived; wiped by nightly cron after 30 days per ToS)
    location: LocationContext | None = None

    # timestamps
    created_at: datetime | None = None
    refreshed_at: datetime | None = None


class PlaceObject(PlaceCore):
    """Full place: PlaceCore + Google-derived live fields. Cache-only for live half.

    cached_at is set when this object was written to cache (always populated for
    objects that came from Google). It is None for objects reconstructed from DB
    cores that have no cache entry yet.
    """

    rating: float | None = None
    hours: HoursDict | None = None
    phone: str | None = None
    website: str | None = None
    popularity: int | None = None
    cached_at: datetime | None = None

    def to_core(self) -> PlaceCore:
        """Strip live fields to get the persistable PlaceCore."""
        return PlaceCore.model_validate(
            self.model_dump(include=set(PlaceCore.model_fields))
        )


class UserPlace(BaseModel):
    """One row per (user, place). Holds everything the user owns about this place."""

    user_place_id: str
    user_id: str
    place_id: str  # FK to PlaceCore.id

    approved: bool = True
    visited: bool = False
    liked: bool | None = None

    note: str | None = None

    source: PlaceSource
    source_url: str | None = None

    saved_at: datetime
    visited_at: datetime | None = None

    @model_validator(mode="after")
    def _validate_source(self) -> UserPlace:
        if self.source in (PlaceSource.manual, PlaceSource.totoro):
            if self.source_url is not None:
                raise ValueError(
                    f"source_url must be None when source is {self.source.value}"
                )
        else:
            if self.source_url is None:
                raise ValueError(
                    f"source_url is required when source is {self.source.value}"
                )
        return self


class SavedPlaceView(BaseModel):
    """List view combining a UserPlace with its underlying place data."""

    place: PlaceObject
    user_data: UserPlace


class HybridSearchFilters(BaseModel):
    """Filters applied identically to both legs of hybrid search.

    All fields optional, combined with AND. The same filter set is joined
    into both the vector and FTS CTEs so RRF fuses ranks computed within
    the same constrained candidate pool.

    Filters split across two tables:
      - place catalog (places_v2): category, tags, location, geo
      - user_places:  visited, liked, approved, saved_at range
    """

    # ---- place catalog filters --------------------------------------
    category: PlaceCategory | None = None
    tags: list[str] | None = None         # JSONB @>, AND across values

    city: str | None = None               # ILIKE
    neighborhood: str | None = None       # ILIKE
    country: str | None = None            # exact

    lat: float | None = None
    lng: float | None = None
    radius_m: int | None = None           # required if lat/lng set

    # ---- user_places filters (tri-state booleans: omit for "any") ---
    visited: bool | None = None
    liked: bool | None = None             # NULL liked rows pass when None
    approved: bool | None = None

    saved_after: datetime | None = None
    saved_before: datetime | None = None

    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="after")
    def _validate_geo(self) -> HybridSearchFilters:
        has_lat = self.lat is not None
        has_lng = self.lng is not None
        if has_lat != has_lng:
            raise ValueError("lat and lng must both be set or both be None")
        if (has_lat or has_lng) and self.radius_m is None:
            raise ValueError("radius_m is required when lat/lng is provided")
        return self


class HybridSearchHit(BaseModel):
    """One result from hybrid search.

    Carries both the canonical place and the user's relationship to it
    so downstream consumers (LLM, API, evals) get everything in one
    round trip — the repo already JOINs user_places when scoped to a
    user, so emitting the user_data costs nothing extra.

    `user_data` is None when the search ran in unscoped mode (no
    `user_id` passed) — i.e., a global place catalog search not tied
    to any user's saves.

    `vector_rank` / `text_rank` are 1-indexed ranks within each leg's
    candidate pool, or None if this place didn't show up in that leg.
    """

    place: PlaceCore
    user_data: UserPlace | None = None
    rrf_score: float
    vector_rank: int | None
    text_rank: int | None
