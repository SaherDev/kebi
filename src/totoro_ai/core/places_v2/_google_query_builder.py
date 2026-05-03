"""Outbound translation: PlaceQuery → Google Places API request parameters.

Mirrors _google_mapper.py (inbound: Google → our domain) in the opposite
direction. Everything that touches how we talk TO Google lives here.
"""

from __future__ import annotations

from .models import PlaceQuery
from .tags import AccessibilityTag, SeasonTag, TimeTag

# Tag values that add noise to a Google text query — Google doesn't interpret
# time-of-day, seasons, or accessibility codes as place descriptors.
GOOGLE_SKIP_VALUES: frozenset[str] = frozenset(
    {t.value for t in TimeTag}
    | {t.value for t in SeasonTag}
    | {t.value for t in AccessibilityTag}
)

# ---------------------------------------------------------------------------
# Our tag values → Google place type IDs
# ---------------------------------------------------------------------------

# Cuisine and dietary tag values → Google place type ID
_TAG_TO_GOOGLE_TYPE: dict[str, str] = {
    # cuisine
    "Thai": "thai_restaurant",
    "Japanese": "japanese_restaurant",
    "Korean": "korean_restaurant",
    "Chinese": "chinese_restaurant",
    "Italian": "italian_restaurant",
    "French": "french_restaurant",
    "Mexican": "mexican_restaurant",
    "Indian": "indian_restaurant",
    "Vietnamese": "vietnamese_restaurant",
    "Mediterranean": "mediterranean_restaurant",
    "American": "american_restaurant",
    "Greek": "greek_restaurant",
    "Spanish": "spanish_restaurant",
    "Turkish": "turkish_restaurant",
    "Indonesian": "indonesian_restaurant",
    "Middle Eastern": "middle_eastern_restaurant",
    "Brazilian": "brazilian_restaurant",
    "Seafood": "seafood_restaurant",
    "Steakhouse": "steak_house",
    # dietary
    "vegan": "vegan_restaurant",
    "vegetarian": "vegetarian_restaurant",
    "halal": "halal_restaurant",
}

# Our PlaceCategory values → Google place type ID
_CATEGORY_TO_GOOGLE_TYPE: dict[str, str] = {
    "restaurant": "restaurant",
    "cafe": "cafe",
    "bar": "bar",
    "pub": "pub",
    "bakery": "bakery",
    "dessert_shop": "dessert_shop",
    "ice_cream_shop": "ice_cream_shop",
    "street_food": "street_food",
    "food_court": "food_court",
    "food_market": "food_market",
    "juice_bar": "juice_bar",
    "tea_house": "tea_house",
    "brewery": "brewery",
    "winery": "winery",
    "distillery": "distillery",
    "grocery_store": "grocery_store",
    "supermarket": "supermarket",
    "convenience_store": "convenience_store",
    "shopping_mall": "shopping_mall",
    "bookstore": "book_store",
    "pharmacy": "pharmacy",
    "electronics_store": "electronics_store",
    "night_market": "night_market",
    "farmers_market": "farmers_market",
    "flea_market": "flea_market",
    "museum": "museum",
    "art_gallery": "art_gallery",
    "historical_site": "historical_landmark",
    "monument": "monument",
    "shrine": "shrine",
    "temple": "hindu_temple",
    "mosque": "mosque",
    "church": "church",
    "viewpoint": "observation_deck",
    "landmark": "tourist_attraction",
    "theme_park": "theme_park",
    "amusement_park": "amusement_park",
    "zoo": "zoo",
    "aquarium": "aquarium",
    "botanical_garden": "botanical_garden",
    "cinema": "movie_theater",
    "theater": "performing_arts_theater",
    "concert_hall": "concert_hall",
    "live_music_venue": "live_music_venue",
    "nightclub": "night_club",
    "comedy_club": "comedy_club",
    "karaoke": "karaoke",
    "arcade": "arcade",
    "bowling_alley": "bowling_alley",
    "park": "park",
    "beach": "beach",
    "garden": "garden",
    "lake": "lake",
    "hiking_trail": "hiking_area",
    "campground": "campground",
    "gym": "fitness_center",
    "yoga_studio": "yoga_studio",
    "pilates_studio": "pilates_studio",
    "spa": "spa",
    "massage": "massage",
    "hot_spring": "hot_spring",
    "salon": "beauty_salon",
    "barber": "barber_shop",
    "climbing_gym": "climbing_gym",
    "skate_park": "skate_park",
    "golf_course": "golf_course",
    "swimming_pool": "swimming_pool",
    "sports_club": "sports_club",
    "stadium": "stadium",
    "arena": "arena",
    "atm": "atm",
    "bank": "bank",
    "post_office": "post_office",
    "gas_station": "gas_station",
    "parking": "parking",
    "laundry": "laundromat",
    "hotel": "hotel",
    "hostel": "hostel",
    "guesthouse": "guest_house",
    "bed_and_breakfast": "bed_and_breakfast",
    "resort": "resort_hotel",
    "vacation_rental": "vacation_rental",
    "airport": "airport",
    "train_station": "train_station",
    "metro_station": "subway_station",
    "bus_terminal": "bus_station",
    "ferry_terminal": "ferry_terminal",
    "coworking_space": "coworking_space",
    "library": "library",
    "study_cafe": "study_cafe",
}


# ---------------------------------------------------------------------------
# Public builders
# ---------------------------------------------------------------------------

def query_to_google_types(query: PlaceQuery) -> list[str]:
    """Map PlaceQuery category + cuisine/dietary tags to Google place type IDs.

    Returns a deduplicated list (category first, then tags). Tags without a
    known Google mapping are handled by the text query instead.
    """
    seen: set[str] = set()
    types: list[str] = []

    def _add(t: str) -> None:
        if t not in seen:
            seen.add(t)
            types.append(t)

    if query.category:
        gtype = _CATEGORY_TO_GOOGLE_TYPE.get(query.category.value)
        if gtype:
            _add(gtype)

    if query.tags:
        for tag_val in query.tags:
            gtype = _TAG_TO_GOOGLE_TYPE.get(str(tag_val))
            if gtype:
                _add(gtype)

    return types


def build_text_search_params(query: PlaceQuery) -> tuple[str, str | None]:
    """Build (textQuery, includedType) for Google searchText.

    The first term mappable to a Google type ID (category checked before
    tags) becomes includedType, and that same term is omitted from
    textQuery to avoid sending the same concept twice.

    Edge case: searchText requires a non-empty textQuery. If stripping
    the type-mapped term would leave textQuery empty, the term is kept
    in text (the duplication is forced by Google's API contract).
    """
    primary_type: str | None = None
    primary_term: str | None = None
    text_parts: list[str] = []

    if query.place_name:
        text_parts.append(query.place_name)

    if query.category:
        cat_text = query.category.value.replace("_", " ")
        gtype = _CATEGORY_TO_GOOGLE_TYPE.get(query.category.value)
        if gtype and primary_type is None:
            primary_type = gtype
            primary_term = cat_text
        else:
            text_parts.append(cat_text)

    if query.tags:
        for tag_val in query.tags:
            if tag_val in GOOGLE_SKIP_VALUES:
                continue
            tag_text = str(tag_val).replace("_", " ")
            gtype = _TAG_TO_GOOGLE_TYPE.get(str(tag_val))
            if gtype and primary_type is None:
                primary_type = gtype
                primary_term = tag_text
            else:
                text_parts.append(tag_text)

    # If everything else was empty, keep the type-mapped term in text —
    # Google searchText rejects an empty textQuery.
    if not text_parts and primary_term is not None:
        text_parts.append(primary_term)

    return " ".join(dict.fromkeys(text_parts)), primary_type
