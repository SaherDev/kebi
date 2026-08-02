"""Google Places API v1 → PlaceObject field mapping.

Maps only the minimal validator fields (ADR-118): identity, location,
address, and `types`-derived categories + cuisine/dietary tags. Experiential
tags (service, feature, price, atmosphere, accessibility) are owned by the
LLM knowledge layer, not Google.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from ._address_components import country_code, map_address_components
from .models import (
    LocationContext,
    PlaceCategory,
    PlaceObject,
    PlaceTag,
)
from .tags import (
    CuisineTag,
    DietaryTag,
    TagType,
)

# Namespace tag on every Google-sourced provider_id (e.g. "google:ChIJ...").
# Owned here because this module is what stamps it onto PlaceObject; clients
# strip it again when calling Place Details by id.
GOOGLE_PROVIDER_PREFIX = "google:"


# ---------------------------------------------------------------------------
# Lookup tables
# ---------------------------------------------------------------------------

# Google Places API v1 types → our canonical category list.
# A place often matches several entries (e.g. a wine bar is `wine_bar`,
# `bar`, and `store`); _map_categories preserves the order Google returned
# them in `types[]`, which is documented to put the most specific type
# first.
_GOOGLE_TYPE_TO_CATEGORY: dict[str, str] = {
    # restaurants
    "burger_restaurant": "restaurant",
    "pizza_restaurant": "restaurant",
    "sushi_restaurant": "restaurant",
    "ramen_restaurant": "restaurant",
    "thai_restaurant": "restaurant",
    "chinese_restaurant": "restaurant",
    "japanese_restaurant": "restaurant",
    "korean_restaurant": "restaurant",
    "indian_restaurant": "restaurant",
    "american_restaurant": "restaurant",
    "italian_restaurant": "restaurant",
    "mexican_restaurant": "restaurant",
    "seafood_restaurant": "restaurant",
    "steak_house": "restaurant",
    "vegetarian_restaurant": "restaurant",
    "vegan_restaurant": "restaurant",
    "fast_food_restaurant": "restaurant",
    "brunch_restaurant": "restaurant",
    "meal_takeaway": "restaurant",
    "meal_delivery": "restaurant",
    "restaurant": "restaurant",
    "food": "restaurant",
    # cafe / study
    "study_cafe": "study_cafe",
    "coffee_shop": "cafe",
    "cafe": "cafe",
    # bar / pub / nightlife
    "wine_bar": "bar",
    "cocktail_bar": "bar",
    "sports_bar": "bar",
    "bar": "bar",
    "pub": "pub",
    "night_club": "nightclub",
    "casino": "nightclub",
    # bakery / desserts
    "bakery": "bakery",
    "ice_cream_shop": "ice_cream_shop",
    "dessert_shop": "dessert_shop",
    "candy_store": "dessert_shop",
    "chocolate_shop": "dessert_shop",
    # drinks
    "juice_bar": "juice_bar",
    "tea_house": "tea_house",
    "brewery": "brewery",
    "winery": "winery",
    "distillery": "distillery",
    # street / markets
    "street_food": "street_food",
    "food_court": "food_court",
    "food_market": "food_market",
    "night_market": "night_market",
    "farmers_market": "farmers_market",
    "flea_market": "flea_market",
    # retail
    "book_store": "bookstore",
    "electronics_store": "electronics_store",
    "clothing_store": "boutique",
    "shoe_store": "boutique",
    "boutique": "boutique",
    "grocery_store": "grocery_store",
    "supermarket": "supermarket",
    "convenience_store": "convenience_store",
    "department_store": "shopping_mall",
    "shopping_mall": "shopping_mall",
    "jewelry_store": "specialty_shop",
    "home_goods_store": "specialty_shop",
    "furniture_store": "specialty_shop",
    "store": "specialty_shop",
    "pharmacy": "pharmacy",
    "drugstore": "pharmacy",
    # culture / sightseeing
    "art_gallery": "art_gallery",
    "museum": "museum",
    "historical_landmark": "historical_site",
    "monument": "monument",
    "shrine": "shrine",
    "hindu_temple": "temple",
    "place_of_worship": "temple",
    "mosque": "mosque",
    "cathedral": "church",
    "church": "church",
    "observation_deck": "viewpoint",
    "viewpoint": "viewpoint",
    "scenic_point": "scenic_lookout",
    "tourist_attraction": "landmark",
    # nature / outdoors
    "botanical_garden": "botanical_garden",
    "national_park": "park",
    "park": "park",
    "garden": "garden",
    "beach": "beach",
    "lake": "lake",
    "river": "river",
    "hiking_area": "hiking_trail",
    "campground": "campground",
    # entertainment
    "theme_park": "theme_park",
    "amusement_park": "amusement_park",
    "zoo": "zoo",
    "aquarium": "aquarium",
    "performing_arts_theater": "theater",
    "movie_theater": "cinema",
    "concert_hall": "concert_hall",
    "live_music_venue": "live_music_venue",
    "comedy_club": "comedy_club",
    "karaoke": "karaoke",
    "arcade": "arcade",
    "bowling_alley": "bowling_alley",
    "billiards": "billiards_hall",
    "stadium": "stadium",
    "arena": "arena",
    # fitness / wellness
    "yoga_studio": "yoga_studio",
    "pilates_studio": "pilates_studio",
    "climbing_gym": "climbing_gym",
    "skate_park": "skate_park",
    "golf_course": "golf_course",
    "swimming_pool": "swimming_pool",
    "sports_club": "sports_club",
    "fitness_center": "gym",
    "gym": "gym",
    "massage": "massage",
    "hot_spring": "hot_spring",
    "bathhouse": "bathhouse",
    "nail_salon": "salon",
    "hair_salon": "salon",
    "beauty_salon": "salon",
    "barber_shop": "barber",
    "hair_care": "barber",
    "spa": "spa",
    # services / utilities
    "atm": "atm",
    "bank": "bank",
    "post_office": "post_office",
    "gas_station": "gas_station",
    "parking": "parking",
    "laundromat": "laundry",
    "laundry": "laundry",
    # accommodation
    "guest_house": "guesthouse",
    "bed_and_breakfast": "bed_and_breakfast",
    "hostel": "hostel",
    "resort_hotel": "resort",
    "vacation_rental": "vacation_rental",
    "extended_stay_hotel": "hotel",
    "motel": "hotel",
    "lodging": "hotel",
    "hotel": "hotel",
    # transit
    "ferry_terminal": "ferry_terminal",
    "bus_station": "bus_terminal",
    "light_rail_station": "metro_station",
    "subway_station": "metro_station",
    "transit_station": "metro_station",
    "train_station": "train_station",
    "airport": "airport",
    # work / study
    "coworking_space": "coworking_space",
    "library": "library",
}

# Restaurant-specific Google types → cuisine tag
_GOOGLE_TYPE_TO_CUISINE: dict[str, CuisineTag] = {
    "thai_restaurant": CuisineTag.thai,
    "chinese_restaurant": CuisineTag.chinese,
    "japanese_restaurant": CuisineTag.japanese,
    "sushi_restaurant": CuisineTag.japanese,
    "ramen_restaurant": CuisineTag.japanese,
    "korean_restaurant": CuisineTag.korean,
    "indian_restaurant": CuisineTag.indian,
    "italian_restaurant": CuisineTag.italian,
    "pizza_restaurant": CuisineTag.italian,
    "american_restaurant": CuisineTag.american,
    "burger_restaurant": CuisineTag.american,
    "mexican_restaurant": CuisineTag.mexican,
    "french_restaurant": CuisineTag.french,
    "mediterranean_restaurant": CuisineTag.mediterranean,
    "greek_restaurant": CuisineTag.greek,
    "spanish_restaurant": CuisineTag.spanish,
    "vietnamese_restaurant": CuisineTag.vietnamese,
    "indonesian_restaurant": CuisineTag.indonesian,
    "turkish_restaurant": CuisineTag.turkish,
    "middle_eastern_restaurant": CuisineTag.middle_eastern,
    "brazilian_restaurant": CuisineTag.brazilian,
    "seafood_restaurant": CuisineTag.seafood,
    "steak_house": CuisineTag.steakhouse,
}

# Google types that imply dietary restrictions
_GOOGLE_TYPE_TO_DIETARY: dict[str, list[DietaryTag]] = {
    "vegan_restaurant": [DietaryTag.vegan, DietaryTag.vegetarian],
    "vegetarian_restaurant": [DietaryTag.vegetarian],
    "halal_restaurant": [DietaryTag.halal],
}

# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def map_place(
    raw: dict[str, Any],
    now: datetime,
    *,
    require_name: bool = True,
) -> PlaceObject | None:
    """Map a raw Google Place dict to a PlaceObject.

    ``require_name=False`` is the Place Details mode: the details field mask
    deliberately omits ``displayName`` (Essentials tier — the DB name is
    sticky-authoritative), so a nameless response maps to ``place_name=""``
    and the caller backfills the name from the catalog row. Search responses
    always require a name — a nameless search result is unusable.
    """
    raw_id = raw.get("id")
    if not raw_id:
        return None

    display_name = raw.get("displayName") or {}
    place_name = display_name.get("text") or ""
    if not place_name and require_name:
        return None

    types: list[str] = raw.get("types") or []
    mapped = _map_categories(types)
    # ADR-082/ADR-124: a search result that is non-venue geography — a city,
    # district, road, or a route/pass/natural feature even when Google also
    # stamps `tourist_attraction` on it ("Ha Giang Loop", "Hai Van Pass") — is
    # not a savable place, so drop it here at validation before it can be
    # persisted or handed to the picker. A place Google classifies with any
    # specific venue type keeps its category and survives, honoring the
    # district-as-attraction carve-out. Only search results are filtered; the
    # by-id details refresh (`require_name=False`) always maps its
    # already-catalogued venue.
    if require_name and is_non_venue_geography(types):
        return None
    categories = [PlaceCategory(c) for c in mapped]

    tags: list[PlaceTag] = []
    seen: set[tuple[str, str]] = set()

    def _add_tag(tag_type: str, value: str) -> None:
        key = (tag_type, value)
        if key not in seen:
            seen.add(key)
            tags.append(PlaceTag(type=tag_type, value=value, source="google"))

    # cuisine tags from place types
    for t in types:
        if t in _GOOGLE_TYPE_TO_CUISINE:
            _add_tag(TagType.cuisine, _GOOGLE_TYPE_TO_CUISINE[t])

    # dietary tags from place types
    for t in types:
        for item in _GOOGLE_TYPE_TO_DIETARY.get(t, []):
            _add_tag(TagType.dietary, item)

    raw_loc = raw.get("location") or {}
    components = raw.get("addressComponents") or []
    addr = map_address_components(components)

    return PlaceObject(
        provider_id=f"{GOOGLE_PROVIDER_PREFIX}{raw_id}",
        place_name=place_name,
        categories=categories,
        tags=tags,
        location=LocationContext(
            lat=raw_loc.get("latitude"),
            lng=raw_loc.get("longitude"),
            address=raw.get("formattedAddress"),
            city=addr.get("city"),
            neighborhood=addr.get("neighborhood"),
            country=addr.get("country"),
            country_code=country_code(components),
        ),
        cached_at=now,
    )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


# Google place types that denote an administrative area (a city, district,
# region, or road) rather than a savable venue. A text search matches one of
# these when the query is itself a place name; the result must not become a
# place (ADR-082). This is the reliable signal the v2 mapper previously
# discarded when it abstracted Google types down to PlaceCategory.
_ADMINISTRATIVE_TYPES = frozenset(
    {
        "locality",
        "sublocality",
        "sublocality_level_1",
        "sublocality_level_2",
        "sublocality_level_3",
        "sublocality_level_4",
        "sublocality_level_5",
        "neighborhood",
        "administrative_area_level_1",
        "administrative_area_level_2",
        "administrative_area_level_3",
        "administrative_area_level_4",
        "administrative_area_level_5",
        "administrative_area_level_6",
        "administrative_area_level_7",
        "colloquial_area",
        "political",
        "country",
        "archipelago",
        "continent",
        "postal_code",
        "postal_code_prefix",
        "postal_code_suffix",
        "route",
        "street_address",
        "intersection",
    }
)


def _is_administrative_type(types: list[str]) -> bool:
    """True when any Google place type marks an administrative area."""
    return any(t in _ADMINISTRATIVE_TYPES for t in types)


# Machine-readable reasons stamped on a NonVenueDetection — the routing
# signal the location-kinds Step 2 seam promised: an administrative area
# ("Hoi An") can resolve to itself as an area entity, while linear/natural
# geography ("Ha Giang Loop") never can — it collapses to its containing
# area. `NON_VENUE_GEOGRAPHY` remains the generic umbrella value.
NON_VENUE_GEOGRAPHY = "non_venue_geography"
NON_VENUE_ROUTE = "non_venue_route"
NON_VENUE_AREA = "non_venue_area"

# Linear or natural geography — a road, scenic route, mountain pass, loop,
# bay, peak. Google frequently co-stamps these `tourist_attraction`, which
# maps to the generic `landmark` category; that generic mapping must not
# rescue them, unlike the administrative carve-out below.
_LINEAR_NATURAL_GEOGRAPHY_TYPES = frozenset({"route", "natural_feature"})

_GENERIC_ATTRACTION_TYPE = "tourist_attraction"


def classify_non_venue_geography(types: list[str]) -> str | None:
    """Classify Google's type signal: `None` for a venue, else the
    non-venue subtype (`NON_VENUE_ROUTE` / `NON_VENUE_AREA`).

    The decision, in priority order:

    1. Any *specific* venue type (restaurant, museum, beach, lake, ...)
       wins outright — Google itself classifies the result as a venue.
       `tourist_attraction` alone does not count: it is a generic marker
       Google stamps on geography and venues alike.
    2. Linear/natural geography (`route`, `natural_feature`) is non-venue
       even when co-stamped `tourist_attraction` — a scenic loop or a
       mountain pass is never a savable point venue.
    3. An administrative type is non-venue unless `tourist_attraction` is
       present — the district-as-attraction carve-out (ADR-124): a place
       Google deems both a political area and an attraction stays a venue.

    Pure type-signal per ADR-124 — never a name-shape heuristic. The
    subtype is the venue-vs-area routing signal: `NON_VENUE_AREA` names
    can resolve to their own area entity, `NON_VENUE_ROUTE` names always
    collapse to their containing area.
    """
    if any(
        t in _GOOGLE_TYPE_TO_CATEGORY and t != _GENERIC_ATTRACTION_TYPE for t in types
    ):
        return None
    if any(t in _LINEAR_NATURAL_GEOGRAPHY_TYPES for t in types):
        return NON_VENUE_ROUTE
    if _is_administrative_type(types) and _GENERIC_ATTRACTION_TYPE not in types:
        return NON_VENUE_AREA
    return None


def is_non_venue_geography(types: list[str]) -> bool:
    """True when Google's type signal marks non-venue geography — the
    boolean face of `classify_non_venue_geography` (see its docstring for
    the decision rules)."""
    return classify_non_venue_geography(types) is not None


def _map_categories(types: list[str]) -> list[str]:
    """Walk Google's types[] and return every recognized category, in order, deduped.

    Google returns multi-membership facts in `types[]` — a wine bar may come
    back as ``["wine_bar", "bar", "store", "food", ...]``. Preserving the
    order keeps the most specific type first (Google's contract), which is
    what consumers should treat as the "primary" for display purposes.
    """
    seen: set[str] = set()
    result: list[str] = []
    for t in types:
        cat = _GOOGLE_TYPE_TO_CATEGORY.get(t)
        if cat and cat not in seen:
            seen.add(cat)
            result.append(cat)
    return result


