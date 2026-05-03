"""Google Places API v1 → PlaceObject field mapping."""

from __future__ import annotations

from datetime import datetime
from typing import Any, cast

from .models import (
    BusinessStatus,
    HoursDict,
    LocationContext,
    PlaceCategory,
    PlaceObject,
    PlaceTag,
)
from .tags import (
    AccessibilityTag,
    CuisineTag,
    DietaryTag,
    FeatureTag,
    PriceTag,
    ServiceTag,
    TagType,
    TagValue,
)

# Namespace tag on every Google-sourced provider_id (e.g. "google:ChIJ...").
# Owned here because this module is what stamps it onto PlaceObject; clients
# strip it again when calling Place Details by id.
GOOGLE_PROVIDER_PREFIX = "google:"


# ---------------------------------------------------------------------------
# Lookup tables
# ---------------------------------------------------------------------------

# Google Places API v1 types → our canonical category list.
# Order within each group matters: more specific types first so the first
# match in _map_types() lands on the most precise category.
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

_GOOGLE_BUSINESS_STATUS: dict[str, BusinessStatus] = {
    "OPERATIONAL": BusinessStatus.operational,
    "CLOSED_TEMPORARILY": BusinessStatus.closed_temporarily,
    "CLOSED_PERMANENTLY": BusinessStatus.closed_permanently,
}

_PRICE_LEVEL_MAP: dict[str, PriceTag] = {
    "PRICE_LEVEL_FREE": PriceTag.free,
    "PRICE_LEVEL_INEXPENSIVE": PriceTag.budget,
    "PRICE_LEVEL_MODERATE": PriceTag.moderate,
    "PRICE_LEVEL_EXPENSIVE": PriceTag.expensive,
    "PRICE_LEVEL_VERY_EXPENSIVE": PriceTag.very_expensive,
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

# Google boolean Place fields → (TagType, TagValue)
_GOOGLE_BOOL_TO_TAG: dict[str, tuple[TagType, TagValue]] = {
    "dineIn": (TagType.service, ServiceTag.dine_in),
    "takeout": (TagType.service, ServiceTag.takeout),
    "delivery": (TagType.service, ServiceTag.delivery),
    "reservable": (TagType.service, ServiceTag.reservable),
    "servesBreakfast": (TagType.service, ServiceTag.serves_breakfast),
    "servesBrunch": (TagType.service, ServiceTag.serves_brunch),
    "servesLunch": (TagType.service, ServiceTag.serves_lunch),
    "servesDinner": (TagType.service, ServiceTag.serves_dinner),
    "servesBeer": (TagType.service, ServiceTag.serves_beer),
    "servesWine": (TagType.service, ServiceTag.serves_wine),
    "servesCocktails": (TagType.service, ServiceTag.serves_cocktails),
    "servesVegetarianFood": (TagType.dietary, DietaryTag.vegetarian_options),
    "outdoorSeating": (TagType.feature, FeatureTag.outdoor_seating),
    "liveMusic": (TagType.feature, FeatureTag.live_music),
    "menuForChildren": (TagType.feature, FeatureTag.kids_menu),
    "allowsDogs": (TagType.feature, FeatureTag.dog_friendly),
    "goodForChildren": (TagType.feature, FeatureTag.family_friendly),
    "goodForGroups": (TagType.feature, FeatureTag.group_friendly),
    "goodForWatchingSports": (TagType.feature, FeatureTag.sports_viewing),
}

# accessibilityOptions sub-object fields → (TagType, TagValue)
_acc = TagType.accessibility
_GOOGLE_ACCESSIBILITY_TO_TAG: dict[str, tuple[TagType, TagValue]] = {
    "wheelchairAccessibleParking": (_acc, AccessibilityTag.wheelchair_parking),
    "wheelchairAccessibleEntrance": (_acc, AccessibilityTag.wheelchair_entrance),
    "wheelchairAccessibleRestroom": (_acc, AccessibilityTag.wheelchair_restroom),
    "wheelchairAccessibleSeating": (_acc, AccessibilityTag.wheelchair_seating),
}

# addressComponents type → LocationContext field name
_ADDR_COMPONENT_TO_FIELD: dict[str, str] = {
    "locality": "city",
    "sublocality_level_1": "neighborhood",
    "neighborhood": "neighborhood",
    "country": "country",
}

_DAY_INT_TO_NAME: dict[int, str] = {
    0: "sunday",
    1: "monday",
    2: "tuesday",
    3: "wednesday",
    4: "thursday",
    5: "friday",
    6: "saturday",
}


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def map_place(raw: dict[str, Any], now: datetime) -> PlaceObject | None:
    raw_id = raw.get("id")
    if not raw_id:
        return None

    display_name = raw.get("displayName") or {}
    place_name = display_name.get("text") or ""
    if not place_name:
        return None

    types: list[str] = raw.get("types") or []
    category_str = _map_category(types)
    category = PlaceCategory(category_str) if category_str else None

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

    # feature/service tags from top-level boolean fields
    for field, (tag_type, tag_value) in _GOOGLE_BOOL_TO_TAG.items():
        if raw.get(field) is True:
            _add_tag(tag_type, tag_value)

    # accessibility tags from nested accessibilityOptions object
    accessibility = raw.get("accessibilityOptions") or {}
    for field, (tag_type, tag_value) in _GOOGLE_ACCESSIBILITY_TO_TAG.items():
        if accessibility.get(field) is True:
            _add_tag(tag_type, tag_value)

    # price tag
    price_str = _PRICE_LEVEL_MAP.get(raw.get("priceLevel") or "")
    if price_str:
        _add_tag(TagType.price, price_str)

    raw_loc = raw.get("location") or {}
    addr = _map_address_components(raw.get("addressComponents") or [])

    return PlaceObject(
        provider_id=f"{GOOGLE_PROVIDER_PREFIX}{raw_id}",
        place_name=place_name,
        category=category,
        tags=tags,
        location=LocationContext(
            lat=raw_loc.get("latitude"),
            lng=raw_loc.get("longitude"),
            address=raw.get("formattedAddress"),
            city=addr.get("city"),
            neighborhood=addr.get("neighborhood"),
            country=addr.get("country"),
        ),
        rating=raw.get("rating"),
        hours=_map_hours(raw),
        phone=raw.get("nationalPhoneNumber"),
        website=raw.get("websiteUri"),
        popularity=raw.get("userRatingCount"),
        business_status=_GOOGLE_BUSINESS_STATUS.get(raw.get("businessStatus") or ""),
        cached_at=now,
    )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _map_category(types: list[str]) -> str | None:
    for t in types:
        cat = _GOOGLE_TYPE_TO_CATEGORY.get(t)
        if cat:
            return cat
    return None


def _map_address_components(
    components: list[dict[str, Any]],
) -> dict[str, str]:
    result: dict[str, str] = {}
    for component in components:
        long_text = component.get("longText") or ""
        for comp_type in component.get("types") or []:
            field = _ADDR_COMPONENT_TO_FIELD.get(comp_type)
            if field and field not in result and long_text:
                result[field] = long_text
    return result


def _map_hours(raw: dict[str, Any]) -> HoursDict | None:
    opening_hours = raw.get("regularOpeningHours") or {}
    time_zone = raw.get("timeZone") or {}
    timezone_id = time_zone.get("id") if isinstance(time_zone, dict) else None
    periods = opening_hours.get("periods") or []
    if not periods or not timezone_id:
        return None

    hours: dict[str, Any] = {}
    for period in periods:
        open_obj = period.get("open") or {}
        close_obj = period.get("close")
        day_int = open_obj.get("day")
        if day_int is None or day_int not in _DAY_INT_TO_NAME:
            continue
        day_name = _DAY_INT_TO_NAME[day_int]
        if close_obj is None:
            hours[day_name] = ["00:00-00:00"]
        else:
            slot = f"{_fmt_clock(open_obj)}-{_fmt_clock(close_obj)}"
            hours.setdefault(day_name, []).append(slot)

    for day_name in _DAY_INT_TO_NAME.values():
        if day_name not in hours:
            hours[day_name] = []

    hours["timezone"] = timezone_id
    return cast(HoursDict, hours)


def _fmt_clock(clock: dict[str, Any]) -> str:
    hour = clock.get("hour")
    minute = clock.get("minute")
    h = hour if isinstance(hour, int) and 0 <= hour <= 23 else 0
    m = minute if isinstance(minute, int) and 0 <= minute <= 59 else 0
    return f"{h:02d}:{m:02d}"
