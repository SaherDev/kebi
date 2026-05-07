"""Known tag vocabulary for places_v2.

All enums are str-based so their values drop directly into PlaceQuery.tags
without conversion.  PlaceTag.type uses TagType | str to allow LLM-generated
tags beyond the known vocabulary.

Usage:
    PlaceQuery(tags=[CuisineTag.thai, FeatureTag.outdoor_seating])
    PlaceTag(type=TagType.cuisine, value=CuisineTag.thai, source="google")
"""

from __future__ import annotations

from enum import Enum


class TagType(str, Enum):
    """Controlled set of tag type identifiers."""

    cuisine = "cuisine"
    dietary = "dietary"
    feature = "feature"           # observable physical/structural attributes
    atmosphere = "atmosphere"     # vibe/mood — LLM-sourced
    service = "service"           # operational capabilities
    price = "price"
    accessibility = "accessibility"
    time = "time"                 # time-of-day suitability
    season = "season"             # weather/season suitability


# ---------------------------------------------------------------------------
# Cuisine
# ---------------------------------------------------------------------------

class CuisineTag(str, Enum):
    thai = "Thai"
    japanese = "Japanese"
    korean = "Korean"
    chinese = "Chinese"
    italian = "Italian"
    french = "French"
    mexican = "Mexican"
    indian = "Indian"
    vietnamese = "Vietnamese"
    mediterranean = "Mediterranean"
    american = "American"
    greek = "Greek"
    spanish = "Spanish"
    turkish = "Turkish"
    indonesian = "Indonesian"
    middle_eastern = "Middle Eastern"
    brazilian = "Brazilian"
    seafood = "Seafood"
    steakhouse = "Steakhouse"


# ---------------------------------------------------------------------------
# Dietary
# ---------------------------------------------------------------------------

class DietaryTag(str, Enum):
    vegan = "vegan"
    vegetarian = "vegetarian"
    halal = "halal"
    vegetarian_options = "vegetarian_options"  # not fully vegan/veg menu


# ---------------------------------------------------------------------------
# Feature — observable, often Google-sourced
# ---------------------------------------------------------------------------

class FeatureTag(str, Enum):
    # seating / space
    outdoor_seating = "outdoor_seating"
    indoor = "indoor"
    outdoor = "outdoor"               # majority-outdoor venue
    rooftop = "rooftop"
    waterfront = "waterfront"
    garden = "garden"
    scenic_view = "scenic_view"
    private_room = "private_room"
    fireplace = "fireplace"

    # people
    dog_friendly = "dog_friendly"
    family_friendly = "family_friendly"
    group_friendly = "group_friendly"
    kids_menu = "kids_menu"
    sports_viewing = "sports_viewing"
    live_music = "live_music"

    # practical
    parking = "parking"
    open_late = "open_late"           # past midnight
    open_24h = "open_24h"


# ---------------------------------------------------------------------------
# Atmosphere — vibe/mood, LLM-sourced
# ---------------------------------------------------------------------------

class AtmosphereTag(str, Enum):
    cozy = "cozy"
    romantic = "romantic"
    trendy = "trendy"
    quiet = "quiet"
    lively = "lively"
    intimate = "intimate"
    spacious = "spacious"
    vibrant = "vibrant"
    laid_back = "laid_back"
    luxurious = "luxurious"
    casual = "casual"
    upscale = "upscale"
    hidden_gem = "hidden_gem"
    instagram_worthy = "instagram_worthy"
    vintage = "vintage"
    industrial = "industrial"
    minimalist = "minimalist"
    bohemian = "bohemian"
    traditional = "traditional"
    modern = "modern"


# ---------------------------------------------------------------------------
# Service — operational capabilities, Google-sourced
# ---------------------------------------------------------------------------

class ServiceTag(str, Enum):
    dine_in = "dine_in"
    takeout = "takeout"
    delivery = "delivery"
    reservable = "reservable"
    serves_breakfast = "serves_breakfast"
    serves_brunch = "serves_brunch"
    serves_lunch = "serves_lunch"
    serves_dinner = "serves_dinner"
    serves_beer = "serves_beer"
    serves_wine = "serves_wine"
    serves_cocktails = "serves_cocktails"


# ---------------------------------------------------------------------------
# Price — human-readable names, no dollar signs
# ---------------------------------------------------------------------------

class PriceTag(str, Enum):
    free = "free"
    budget = "budget"               # $ — street food, fast casual
    moderate = "moderate"           # $$ — casual dining
    expensive = "expensive"         # $$$ — upscale
    very_expensive = "very_expensive"  # $$$$ — fine dining / luxury


# ---------------------------------------------------------------------------
# Accessibility — Google accessibilityOptions fields
# ---------------------------------------------------------------------------

class AccessibilityTag(str, Enum):
    wheelchair_parking = "wheelchair_parking"
    wheelchair_entrance = "wheelchair_entrance"
    wheelchair_restroom = "wheelchair_restroom"
    wheelchair_seating = "wheelchair_seating"


# ---------------------------------------------------------------------------
# Time of day — when a place is best suited
# ---------------------------------------------------------------------------

class TimeTag(str, Enum):
    morning = "morning"         # 6–11am: coffee, breakfast
    brunch = "brunch"           # 10am–1pm
    lunch = "lunch"             # 12–3pm
    afternoon = "afternoon"     # 2–6pm: coffee, snacks, study
    evening = "evening"         # 6–10pm: dinner, pre-drinks
    night = "night"             # 9pm–midnight: bars, clubs
    late_night = "late_night"   # midnight+: street food, 24h spots
    all_day = "all_day"         # good any time of day


# ---------------------------------------------------------------------------
# Season / weather — when conditions suit the place
# ---------------------------------------------------------------------------

class SeasonTag(str, Enum):
    summer = "summer"           # hot/sunny — outdoor, cold drinks, shade
    winter = "winter"           # cold — warmth, hot drinks, indoor
    rainy = "rainy"             # wet weather — indoor, cozy, delivery
    spring = "spring"           # mild — outdoor, light food
    autumn = "autumn"           # mild — outdoor, comfort food
    all_season = "all_season"   # works in any weather


# Union of all known tag value enums — or plain str for LLM / custom tags.
# Use this as the type for PlaceTag.value.
TagValue = (
    CuisineTag
    | DietaryTag
    | FeatureTag
    | AtmosphereTag
    | ServiceTag
    | PriceTag
    | AccessibilityTag
    | TimeTag
    | SeasonTag
    | str
)
