"""Google Places API v1 → PlaceObject field mapping.

Maps only the minimal validator fields (ADR-118): identity, location,
address, and `types`-derived categories + cuisine/dietary tags. Experiential
tags (service, feature, price, atmosphere, accessibility) are owned by the
LLM knowledge layer, not Google.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from kebi.core.knowledge.schemas import strip_admin_unit

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

# addressComponents type → (LocationContext field, priority). Lower priority
# wins; component order in Google's response never matters. The fallback
# ranks exist because municipality-style cities (Đà Nẵng, Bangkok) arrive as
# administrative_area_level_1 with the district as level_2 and NO locality —
# without the fallback their city/neighborhood drop on the floor (ADR-119).
# postal_town covers UK addresses. Accepted edge: where level_1 is a
# state/province (US, AU) it becomes city only when Google supplies no
# locality/postal_town at all — rare for venues, and better than null.
#
# Neighborhood ranks deepest-human-unit first (ADR-163): where no sublocality
# exists Google can still carry the colloquial area as admin level 4 or 3
# (Bali: level_4 "Canggu", level_3 "Kec. Kuta Utara", level_2 "Kabupaten
# Badung") — level_2 is the last resort, not the first admin fallback, so a
# save in Canggu stops storing its regency.
_ADDR_COMPONENT_TO_FIELD: dict[str, tuple[str, int]] = {
    "locality": ("city", 0),
    "postal_town": ("city", 1),
    "administrative_area_level_1": ("city", 2),
    "sublocality_level_1": ("neighborhood", 0),
    "neighborhood": ("neighborhood", 1),
    # Japanese district names (Asakusa, Toyosu) arrive as level_2; the
    # levels below that are chōme/block numbers and stay unmapped.
    "sublocality_level_2": ("neighborhood", 2),
    "administrative_area_level_4": ("neighborhood", 3),
    "administrative_area_level_3": ("neighborhood", 4),
    "administrative_area_level_2": ("neighborhood", 5),
    "country": ("country", 0),
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
    # ADR-082: a search matches an administrative area — a city, district,
    # region, or road ("Da Nang", "Hoi An", "Sukhumvit Road") — when the query
    # is itself a place name. Such a result carries an administrative Google
    # type and maps to no venue category; it is not a savable place, so drop it
    # here at validation before it can be persisted or handed to the picker
    # (which would otherwise invent a `landmark` category for it). A place
    # Google itself classifies as a venue keeps its category and survives,
    # honoring the district-as-attraction carve-out. Only search results are
    # filtered; the by-id details refresh (`require_name=False`) always maps its
    # already-catalogued venue.
    if require_name and not mapped and _is_administrative_type(types):
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
    addr = _map_address_components(components)
    country_code = _country_code(components)

    return PlaceObject(
        provider_id=f"{GOOGLE_PROVIDER_PREFIX}{raw_id}",
        place_name=place_name,
        categories=categories,
        tags=tags,
        location=LocationContext(
            lat=raw_loc.get("latitude"),
            lng=raw_loc.get("longitude"),
            address=raw.get("formattedAddress"),
            # Stored display geo is the human name — the unit word ("Khet",
            # "Thành phố", trailing "District") folds away here so screens
            # read it verbatim and slugs agree across write paths (ADR-163).
            city=_human_area_name(addr.get("city"), country_code),
            neighborhood=_human_area_name(addr.get("neighborhood"), country_code),
            country=addr.get("country"),
            country_code=country_code,
        ),
        cached_at=now,
    )


def _human_area_name(value: str | None, country_code: str | None) -> str | None:
    """Strip the admin-unit affix from a mapped city/neighborhood, or None."""
    return strip_admin_unit(value, country_code) if value else value


def _country_code(components: list[dict[str, Any]]) -> str | None:
    """ISO-3166 alpha-2 from the country component's `shortText`.

    The country component carries both `longText` ("United Arab Emirates",
    which populates the display `country`) and `shortText` ("AE"). The code
    is what canonical geo keys need, so it is captured separately here and
    returned lowercased.
    """
    for component in components:
        if "country" in (component.get("types") or []):
            short = component.get("shortText")
            if isinstance(short, str) and short:
                return short.strip().lower()
    return None


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


def _map_address_components(
    components: list[dict[str, Any]],
) -> dict[str, str]:
    """Best-ranked component per field (see _ADDR_COMPONENT_TO_FIELD)."""
    best: dict[str, tuple[int, str]] = {}
    for component in components:
        long_text = component.get("longText") or ""
        if not long_text:
            continue
        for comp_type in component.get("types") or []:
            mapped = _ADDR_COMPONENT_TO_FIELD.get(comp_type)
            if mapped is None:
                continue
            field, rank = mapped
            if field not in best or rank < best[field][0]:
                best[field] = (rank, long_text)
    return {field: text for field, (_, text) in best.items()}
