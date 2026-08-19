"""Domain models for the places library.

Three core classes:
- PlaceCore: DB-side curated and locational data, shared across all users.
- PlaceObject: PlaceCore as seen through the provider/cache path (+ cached_at).
- UserPlace: per (user, place) pair.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .tags import TagType, TagValue


class PlaceSource(str, Enum):
    tiktok = "tiktok"
    instagram = "instagram"
    youtube = "youtube"
    google_maps_list = "google_maps_list"
    manual = "manual"
    kebi = "kebi"


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


# Practical "near me" errands — places you walk to, not destinations you
# choose. A turn whose categories fall in this set is clamped to a walkable
# search radius (see core/agent/tools/_scope.py) so "nearest ATM" doesn't
# surface a prominent branch across town.
UTILITY_CATEGORIES: frozenset[PlaceCategory] = frozenset(
    {
        PlaceCategory.atm,
        PlaceCategory.bank,
        PlaceCategory.post_office,
        PlaceCategory.gas_station,
        PlaceCategory.parking,
        PlaceCategory.laundry,
        PlaceCategory.pharmacy,
        PlaceCategory.convenience_store,
        PlaceCategory.supermarket,
        PlaceCategory.grocery_store,
    }
)


class LocationContext(BaseModel):
    """Location container used in PlaceQuery and optionally PlaceCore.attributes."""

    lat: float | None = None
    lng: float | None = None
    address: str | None = None
    radius_m: int | None = None
    neighborhood: str | None = None
    city: str | None = None
    country: str | None = None
    # ISO-3166 alpha-2 (e.g. "ae"), lowercased. Sourced from the provider's
    # country component `shortText` — the code, not the display `country`.
    # Enables code-keyed country filtering and canonical geo keys for the
    # knowledge layer (ADR-120). Nullable: older rows self-heal on re-fetch.
    country_code: str | None = None

    model_config = ConfigDict(extra="forbid")


class PlaceTag(BaseModel):
    type: TagType | str  # TagType for known types; plain str for LLM custom types
    value: TagValue  # known enum value (CuisineTag, FeatureTag, …) or free-text str
    source: str  # "google" | "llm" | "manual" | "tiktok" | ...


class PlaceNameAlias(BaseModel):
    """Alternative name for a place contributed by a non-canonical source.

    The canonical place_name comes from the provider (e.g. Google). Aliases
    track names from other writers (TikTok captions, user notes, LLM extracts)
    for richer search and provenance. Deduped by `value` at merge time;
    first writer of a given alias value wins.
    """

    value: str
    source: str  # "tiktok" | "instagram" | "user" | "llm" | ...


SortField = Literal["created_at", "refreshed_at", "place_name", "distance"]


class PlaceCatalogFilters(BaseModel):
    """Shared catalog-side predicate fields.

    Both `PlaceQuery` (search query) and `HybridSearchFilters` (hybrid search
    predicate) constrain candidates by category + tag identically:
      - `categories`: OR across values (place matches if its category is in the list)
      - `tags`: AND across values (all listed tag values must be present)
    """

    categories: list[PlaceCategory] | None = None  # OR across values
    tags: list[str] | None = None  # tag values; all must be present (AND)


class PlaceQuery(PlaceCatalogFilters):
    """Structured search query. All fields optional, combined with AND.

    DB filters:  ids, provider_ids, place_names, categories, tags,
                 location, created_after/before, sort_*
    Client hints: open_now (passed through to the search client)

    `ids` / `provider_ids` are OR across values (exact match — known-identity
    batch lookup, e.g. resolving saved places through the source-of-truth
    service per ADR-070). `place_names` is OR across values (ILIKE any on DB);
    it also drives client/Google text search.

    `categories` / `tags` semantics inherited from `PlaceCatalogFilters`.
    """

    # DB filters — known-identity batch lookup (exact, OR across values)
    ids: list[str] | None = None  # places.id exact match
    provider_ids: list[str] | None = None  # namespaced provider_id exact match

    place_names: list[str] | None = None  # ILIKE any (OR); also drives text search
    location: LocationContext | None = None

    # date range (DB)
    created_after: datetime | None = None
    created_before: datetime | None = None

    # ordering — provider-agnostic; each backend maps it (DB sorts the
    # column / earth_distance, the place provider maps it to its own rank
    # preference). "distance" orders nearest-first and requires location
    # coords (see _validate_geo_location); sort_desc is ignored for it.
    sort_by: SortField | None = None
    sort_desc: bool = True

    # client hints (ignored for DB queries)
    open_now: bool | None = None  # only return currently open places

    # Write-side annotation, not a filter: when the search cold path
    # persists a provider-fresh result for this query, it stamps this
    # icon on the row so a caller-known icon rides the one normal upsert
    # (fill-only merge keeps it sticky). Meaningful only for lookups that
    # target one specific place by name (the suggest validation path).
    icon_hint: str | None = None

    @field_validator("icon_hint")
    @classmethod
    def _normalize_icon_hint(cls, v: str | None) -> str | None:
        return normalize_icon(v)

    @model_validator(mode="after")
    def _validate_geo_location(self) -> PlaceQuery:
        loc = self.location
        if self.sort_by == "distance" and (
            loc is None or loc.lat is None or loc.lng is None
        ):
            raise ValueError(
                "sort_by='distance' requires location.lat and location.lng "
                "(a named-area location has no anchor to measure from)"
            )
        if loc is None:
            return self
        if (loc.lat is not None or loc.lng is not None) and loc.radius_m is None:
            raise ValueError(
                "location.radius_m is required when lat or lng is provided"
            )
        return self


def normalize_icon(raw: str | None) -> str | None:
    """Normalize an LLM-emitted place icon to a single emoji, or None.

    Icons come from model output (picker / namer / backfill), so junk is
    expected: prose, empty strings, ASCII words. Anything containing an
    ASCII character is rejected — emoji (including ZWJ sequences and
    variation selectors) are entirely non-ASCII, while hallucinated text
    is not. The length cap bounds multi-codepoint sequences (flag pairs,
    skin tones, ZWJ families) without admitting sentences.
    """
    if raw is None:
        return None
    cleaned = raw.strip()
    if not cleaned or len(cleaned) > 8:
        return None
    if any(ch.isascii() for ch in cleaned):
        return None
    return cleaned


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
    categories: list[PlaceCategory] = Field(default_factory=list)
    tags: list[PlaceTag] = Field(default_factory=list)
    # Single emoji capturing the place's identity (🗼, ⛲, 🌴), LLM-picked
    # where an LLM already sees the place. Nullable by design: LLM-less
    # paths leave it None and the client falls back to its category map.
    icon: str | None = None

    @field_validator("icon")
    @classmethod
    def _normalize_icon(cls, v: str | None) -> str | None:
        return normalize_icon(v)

    # location (Google-derived; wiped by nightly cron after 30 days per ToS)
    location: LocationContext | None = None

    # timestamps
    created_at: datetime | None = None
    refreshed_at: datetime | None = None


class PlaceObject(PlaceCore):
    """PlaceCore as seen through the provider/cache path.

    cached_at is set when this object was written to cache (always populated for
    objects that came from Google). It is None for objects reconstructed from DB
    cores that have no cache entry yet. The former Google "live half" (rating,
    hours, phone, website, popularity, business_status) was dropped by ADR-118 —
    the knowledge layer owns experiential data now; old cache entries still
    carrying those keys deserialize fine (extra fields are ignored).
    """

    cached_at: datetime | None = None

    def to_core(self) -> PlaceCore:
        """Strip the cache stamp to get the persistable PlaceCore."""
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
    # Opaque pointer to the place's origin. URL for
    # tiktok/instagram/youtube/google_maps_list; None for manual/kebi.
    source_ref: str | None = None
    # The name this place was shown as in the source post (e.g. a TikTok
    # card label "Mirror Temple"), when it differs from the canonical
    # provider name. Lets the product show the user the name they know
    # it by. None when the source used the canonical name. Set once at
    # save; never rewritten on re-save (mirrors source/source_ref).
    source_label: str | None = None

    saved_at: datetime
    visited_at: datetime | None = None

    @model_validator(mode="after")
    def _validate_source(self) -> UserPlace:
        if self.source in (PlaceSource.manual, PlaceSource.kebi):
            if self.source_ref is not None:
                raise ValueError(
                    f"source_ref must be None when source is {self.source.value}"
                )
        else:
            if self.source_ref is None:
                raise ValueError(
                    f"source_ref is required when source is {self.source.value}"
                )
        return self


class UserPlaceStatusUpdate(BaseModel):
    """A partial update to a user's saved-place state.

    Carries only the fields a caller wants to change: an explicitly-set field
    is written (including to `null` — clearing a note or returning `liked` to
    neutral), an *unset* field is left untouched. The set/unset distinction is
    `model_fields_set`, so callers must build this with the provided fields
    only (e.g. `model_dump(exclude_unset=True)` from the request body) rather
    than relying on `None` to mean "skip".
    """

    model_config = ConfigDict(extra="forbid")

    visited: bool | None = None
    liked: bool | None = None
    approved: bool | None = None
    note: str | None = None


class LibrarySort(str, Enum):
    """Order for the library browse — the screen's recent ↔ A–Z toggle.

    `recent`: newest-saved first (`saved_at` desc), the default.
    `name`: alphabetical by place name, case-insensitive (`lower(place_name)`
    asc). Each maps to a distinct keyset anchor (see `LibraryCursor`), so a
    cursor minted under one sort cannot be replayed under the other.
    """

    recent = "recent"
    name = "name"


class SavedPlaceView(BaseModel):
    """List view combining a UserPlace with its underlying place data.

    Carries `PlaceCore` (DB-side, persistable) — no live Google fields.
    The saved-places list does not need rating/hours/etc., and skipping the
    cache overlay keeps this read cheap.
    """

    place: PlaceCore
    user_data: UserPlace


class SavedPlaceFilters(PlaceCatalogFilters):
    """Predicate over a user's saved places ⋈ the place catalog.

    All fields optional, combined with AND. Shared by every read that
    filters the `places ⋈ user_places` join — hybrid search (which fuses
    ranks within this constrained pool) and the library browse endpoint.

    Filters split across two tables:
      - place catalog (places): categories, tags, location, geo
      - user_places:  source, visited, liked, approved, saved_at range

    `categories` / `tags` inherited from `PlaceCatalogFilters`.
    """

    # ---- place catalog filters --------------------------------------
    # Free-text needle, matched case-insensitively as a substring across a
    # place's name, aliases, city/neighborhood/country, tags, categories (OR
    # within the group, AND with every other filter). A *predicate*, not a
    # relevance query: it narrows the row set and never reorders it, so the
    # library stays the browse ADR-104 defined and its keyset cursor keeps
    # working. Substring rather than full-text because the client searches
    # as the user types — "cang" must find Canggu, which `to_tsquery` won't.
    query: str | None = None

    # Canonical geo key (`id/bali/canggu`) — matched by prefix, so an area
    # includes everything under it and `id/bali` returns the Canggu saves too.
    # Filters on the *stored* key rather than the location strings `city`
    # filters on, which is what makes it agree with the area screen: one save
    # belongs to exactly one area, decided once at write time (ADR-165).
    area: str | None = None

    city: str | None = None  # ILIKE
    neighborhood: str | None = None  # ILIKE
    country: str | None = None  # exact

    lat: float | None = None
    lng: float | None = None
    radius_m: int | None = None  # required if lat/lng set

    # ---- user_places filters (tri-state booleans: omit for "any") ---
    source: PlaceSource | None = None  # exact: where the save came from
    visited: bool | None = None
    liked: bool | None = None  # NULL liked rows pass when None
    approved: bool | None = None

    saved_after: datetime | None = None
    saved_before: datetime | None = None

    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="after")
    def _validate_geo(self) -> SavedPlaceFilters:
        has_lat = self.lat is not None
        has_lng = self.lng is not None
        if has_lat != has_lng:
            raise ValueError("lat and lng must both be set or both be None")
        if (has_lat or has_lng) and self.radius_m is None:
            raise ValueError("radius_m is required when lat/lng is provided")
        return self


class HybridSearchFilters(SavedPlaceFilters):
    """Filters applied identically to both legs of hybrid search.

    The same filter set is joined into both the vector and FTS CTEs so
    RRF fuses ranks computed within the same constrained candidate pool.
    Inherits the full predicate from `SavedPlaceFilters` — no extra fields.
    """


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
