"""
Tag-based PlaceQuery patterns for places_v2.

Tags carry type, value, and source.  Querying uses values only (AND semantics).
Every example below maps a human intent to a PlaceQuery the system can execute.

Tag types in use:
  "cuisine"     — Thai, Japanese, Italian, Korean, Mexican, …
  "dietary"     — vegan, vegetarian, halal, vegetarian_options
  "price"       — $  $$  $$$  $$$$
  "feature"     — outdoor_seating, live_music, dog_friendly, family_friendly,
                  group_friendly, sports_viewing, kids_menu
  "service"     — dine_in, takeout, delivery, reservable, serves_beer,
                  serves_wine, serves_cocktails, serves_breakfast,
                  serves_brunch, serves_lunch, serves_dinner
  "atmosphere"  — cozy, romantic, trendy, lively, quiet  (LLM-sourced)
"""

from __future__ import annotations

from totoro_ai.core.places_v2.models import (
    LocationContext,
    PlaceCategory,
    PlaceQuery,
)
from totoro_ai.core.places_v2.tags import (
    AccessibilityTag,
    AtmosphereTag,
    CuisineTag,
    DietaryTag,
    FeatureTag,
    PriceTag,
    SeasonTag,
    ServiceTag,
    TimeTag,
)

# ---------------------------------------------------------------------------
# 1. Cuisine
# ---------------------------------------------------------------------------

find_thai = PlaceQuery(
    tags=[CuisineTag.thai],
)

find_japanese = PlaceQuery(
    category=PlaceCategory.restaurant,
    tags=[CuisineTag.japanese],
)

find_ramen = PlaceQuery(
    # "ramen" is a cuisine tag produced by _GOOGLE_TYPE_TO_CUISINE
    tags=[CuisineTag.japanese],
    # narrow further with a text search in the caller if needed
)

# ---------------------------------------------------------------------------
# 2. Dietary restrictions
# ---------------------------------------------------------------------------

find_vegan = PlaceQuery(
    tags=[DietaryTag.vegan],
)

find_vegetarian = PlaceQuery(
    tags=[DietaryTag.vegetarian],
)

find_halal = PlaceQuery(
    tags=[DietaryTag.halal],
)

# Strict vegan Thai — both tags must be present (AND)
find_vegan_thai = PlaceQuery(
    tags=[DietaryTag.vegan, CuisineTag.thai],
)

find_halal_korean = PlaceQuery(
    tags=[DietaryTag.halal, CuisineTag.korean],
)

# ---------------------------------------------------------------------------
# 3. Price
# ---------------------------------------------------------------------------

find_cheap_eats = PlaceQuery(
    category=PlaceCategory.restaurant,
    tags=[PriceTag.budget],
)

find_mid_range = PlaceQuery(
    tags=[PriceTag.moderate],
)

find_splurge = PlaceQuery(
    tags=[PriceTag.expensive],
)

# Budget sushi
find_cheap_japanese = PlaceQuery(
    tags=[CuisineTag.japanese, PriceTag.budget],
)

# ---------------------------------------------------------------------------
# 4. Features / vibes
# ---------------------------------------------------------------------------

find_outdoor_seating = PlaceQuery(
    tags=[FeatureTag.outdoor_seating],
)

find_dog_friendly = PlaceQuery(
    tags=[FeatureTag.dog_friendly],
)

find_live_music = PlaceQuery(
    tags=[FeatureTag.live_music],
)

find_sports_bar = PlaceQuery(
    category=PlaceCategory.bar,
    tags=[FeatureTag.sports_viewing],
)

find_family_friendly = PlaceQuery(
    tags=[FeatureTag.family_friendly, FeatureTag.kids_menu],
)

# Cozy atmosphere (LLM-tagged places; only present once LLM enrichment runs)
find_cozy = PlaceQuery(
    tags=[AtmosphereTag.cozy],
)

find_romantic = PlaceQuery(
    tags=[AtmosphereTag.romantic],
)

find_trendy = PlaceQuery(
    tags=[AtmosphereTag.trendy],
)

# ---------------------------------------------------------------------------
# 5. Service capabilities
# ---------------------------------------------------------------------------

find_delivery = PlaceQuery(
    tags=[ServiceTag.delivery],
)

find_takeout = PlaceQuery(
    tags=[ServiceTag.takeout],
)

find_reservable = PlaceQuery(
    tags=[ServiceTag.reservable],
)

find_breakfast_spots = PlaceQuery(
    tags=[ServiceTag.serves_breakfast],
)

find_cocktail_bars = PlaceQuery(
    category=PlaceCategory.bar,
    tags=[ServiceTag.serves_cocktails],
)

# ---------------------------------------------------------------------------
# 6. Combined intents — real user scenarios
# ---------------------------------------------------------------------------

# "I want cheap outdoor Thai food"
cheap_outdoor_thai = PlaceQuery(
    category=PlaceCategory.restaurant,
    tags=[CuisineTag.thai, FeatureTag.outdoor_seating, PriceTag.budget],
)

# "Find me a romantic dinner spot, not too expensive"
romantic_dinner = PlaceQuery(
    category=PlaceCategory.restaurant,
    tags=[AtmosphereTag.romantic, PriceTag.moderate],
)

# "Somewhere I can take my dog for brunch"
dog_brunch = PlaceQuery(
    tags=[FeatureTag.dog_friendly, ServiceTag.serves_brunch],
)

# "Vegan-friendly group dinner"
vegan_group = PlaceQuery(
    category=PlaceCategory.restaurant,
    tags=[DietaryTag.vegan, FeatureTag.group_friendly],
)

# "Cozy café for solo work, not a chain"
solo_work_cafe = PlaceQuery(
    category=PlaceCategory.cafe,
    tags=[AtmosphereTag.cozy],
)

# "Pre-game drinks with outdoor seating"
outdoor_drinks = PlaceQuery(
    category=PlaceCategory.bar,
    tags=[FeatureTag.outdoor_seating, ServiceTag.serves_beer],
)

# "Halal Korean BBQ"
halal_korean_bbq = PlaceQuery(
    category=PlaceCategory.restaurant,
    tags=[CuisineTag.korean, DietaryTag.halal],
)

# "Trendy rooftop cocktail bar"
trendy_cocktails = PlaceQuery(
    category=PlaceCategory.bar,
    tags=[AtmosphereTag.trendy, ServiceTag.serves_cocktails, FeatureTag.outdoor_seating],  # noqa: E501
)

# "Family lunch on a budget"
family_budget_lunch = PlaceQuery(
    tags=[FeatureTag.family_friendly, ServiceTag.serves_lunch, PriceTag.budget],
)

# "Late-night delivery, something spicy"
# Note: "spicy" is an atmosphere/feature tag added by LLM enrichment
late_night_delivery = PlaceQuery(
    tags=[ServiceTag.delivery],
    # add tags=["spicy"] once LLM enrichment is wired
)

# ---------------------------------------------------------------------------
# 7. Location-scoped queries
# ---------------------------------------------------------------------------

# Same patterns above, but scoped to a geo radius
nearby_vegan = PlaceQuery(
    tags=[DietaryTag.vegan],
    location=LocationContext(
        lat=13.7563,
        lng=100.5018,
        radius_m=1000,
    ),
)

nearby_cheap_thai = PlaceQuery(
    category=PlaceCategory.restaurant,
    tags=[CuisineTag.thai, PriceTag.budget],
    location=LocationContext(
        lat=13.7563,
        lng=100.5018,
        radius_m=500,
    ),
)

# Neighbourhood scoping (no radius, string match on location.neighborhood)
sukhumvit_japanese = PlaceQuery(
    category=PlaceCategory.restaurant,
    tags=[CuisineTag.japanese],
    location=LocationContext(neighborhood="Sukhumvit"),
)

bangkok_outdoor_cafes = PlaceQuery(
    category=PlaceCategory.cafe,
    tags=[FeatureTag.outdoor_seating],
    location=LocationContext(city="Bangkok"),
)

# ---------------------------------------------------------------------------
# 8. Category-only (no tags needed)
# ---------------------------------------------------------------------------

all_museums = PlaceQuery(category=PlaceCategory.museum)
all_night_markets = PlaceQuery(category=PlaceCategory.night_market)
nearby_parks = PlaceQuery(
    category=PlaceCategory.park,
    location=LocationContext(lat=13.7563, lng=100.5018, radius_m=2000),
)

# ---------------------------------------------------------------------------
# 9. Time of day
# ---------------------------------------------------------------------------

# Morning (6am–11am) — breakfast, coffee, early work
morning_coffee = PlaceQuery(
    category=PlaceCategory.cafe,
    tags=[ServiceTag.serves_breakfast],
)

morning_breakfast = PlaceQuery(
    category=PlaceCategory.restaurant,
    tags=[ServiceTag.serves_breakfast],
)

early_work_session = PlaceQuery(
    category=PlaceCategory.cafe,
    tags=[ServiceTag.serves_breakfast, AtmosphereTag.cozy],
)

# Late morning (10am–1pm) — brunch
brunch_outdoor = PlaceQuery(
    tags=[ServiceTag.serves_brunch, FeatureTag.outdoor_seating],
)

brunch_with_drinks = PlaceQuery(
    tags=[ServiceTag.serves_brunch, ServiceTag.serves_cocktails],
)

dog_friendly_brunch = PlaceQuery(
    tags=[ServiceTag.serves_brunch, FeatureTag.dog_friendly],
)

# Midday (12pm–3pm) — lunch, quick eats
lunch_quick = PlaceQuery(
    category=PlaceCategory.restaurant,
    tags=[ServiceTag.serves_lunch, ServiceTag.takeout],
)

lunch_outdoor = PlaceQuery(
    tags=[ServiceTag.serves_lunch, FeatureTag.outdoor_seating],
)

lunch_budget = PlaceQuery(
    tags=[ServiceTag.serves_lunch, PriceTag.budget],
)

# Afternoon (2pm–6pm) — coffee, study, snacks
afternoon_study = PlaceQuery(
    category=PlaceCategory.cafe,
    tags=[AtmosphereTag.cozy],
)

afternoon_dessert = PlaceQuery(
    category=PlaceCategory.dessert_shop,
)

afternoon_tea = PlaceQuery(
    category=PlaceCategory.tea_house,
)

study_cafe_afternoon = PlaceQuery(
    category=PlaceCategory.study_cafe,
    tags=[AtmosphereTag.quiet],  # LLM-tagged
)

# Evening (6pm–10pm) — dinner, pre-drinks
dinner_date = PlaceQuery(
    category=PlaceCategory.restaurant,
    tags=[ServiceTag.serves_dinner, AtmosphereTag.romantic, PriceTag.moderate],
)

dinner_group = PlaceQuery(
    category=PlaceCategory.restaurant,
    tags=[ServiceTag.serves_dinner, FeatureTag.group_friendly],
)

pre_dinner_cocktails = PlaceQuery(
    category=PlaceCategory.bar,
    tags=[ServiceTag.serves_cocktails, ServiceTag.reservable],
)

family_dinner = PlaceQuery(
    category=PlaceCategory.restaurant,
    tags=[ServiceTag.serves_dinner, FeatureTag.family_friendly],
)

# Night (9pm–late) — bars, clubs, night food
night_cocktail_bar = PlaceQuery(
    category=PlaceCategory.bar,
    tags=[ServiceTag.serves_cocktails, AtmosphereTag.trendy],
)

night_market_food = PlaceQuery(
    category=PlaceCategory.night_market,
)

night_live_music = PlaceQuery(
    category=PlaceCategory.live_music_venue,
    tags=[FeatureTag.live_music],
)

night_karaoke = PlaceQuery(
    category=PlaceCategory.karaoke,
)

late_night_delivery = PlaceQuery(
    tags=[ServiceTag.delivery],
)

after_midnight_eats = PlaceQuery(
    category=PlaceCategory.street_food,
)

# ---------------------------------------------------------------------------
# 10. Season / weather
# ---------------------------------------------------------------------------

# Hot / summer — outdoor, cold drinks, water, shade
summer_outdoor_dining = PlaceQuery(
    category=PlaceCategory.restaurant,
    tags=[FeatureTag.outdoor_seating, PriceTag.moderate],
)

summer_beach_eats = PlaceQuery(
    category=PlaceCategory.restaurant,
    location=LocationContext(city="Phuket"),
    tags=[FeatureTag.outdoor_seating],
)

summer_rooftop_drinks = PlaceQuery(
    category=PlaceCategory.bar,
    tags=[FeatureTag.outdoor_seating, ServiceTag.serves_cocktails],
)

hot_day_dessert = PlaceQuery(
    category=PlaceCategory.ice_cream_shop,
)

summer_juice_bar = PlaceQuery(
    category=PlaceCategory.juice_bar,
)

# Rainy / indoor — cozy, delivery, covered spots
rainy_day_cafe = PlaceQuery(
    category=PlaceCategory.cafe,
    tags=[AtmosphereTag.cozy],
)

rainy_day_delivery = PlaceQuery(
    category=PlaceCategory.restaurant,
    tags=[ServiceTag.delivery],
)

rainy_indoor_activity = PlaceQuery(
    category=PlaceCategory.museum,
)

rainy_shopping = PlaceQuery(
    category=PlaceCategory.shopping_mall,
)

rainy_bowling = PlaceQuery(
    category=PlaceCategory.bowling_alley,
)

# Cool / winter — warmth, hot drinks, hot food
cool_weather_hotpot = PlaceQuery(
    category=PlaceCategory.restaurant,
    tags=[PriceTag.moderate],
    # "hotpot" would come from LLM cuisine tagging
)

cool_weather_ramen = PlaceQuery(
    category=PlaceCategory.restaurant,
    tags=[CuisineTag.japanese],
)

warm_coffee_cozy = PlaceQuery(
    category=PlaceCategory.cafe,
    tags=[AtmosphereTag.cozy],
)

hot_spring_visit = PlaceQuery(
    category=PlaceCategory.hot_spring,
)

cool_spa_day = PlaceQuery(
    category=PlaceCategory.spa,
)

# ---------------------------------------------------------------------------
# 11. Social occasion
# ---------------------------------------------------------------------------

# Solo
solo_work_anywhere = PlaceQuery(
    category=PlaceCategory.cafe,
    tags=[AtmosphereTag.quiet, AtmosphereTag.cozy],
)

solo_ramen_quick = PlaceQuery(
    category=PlaceCategory.restaurant,
    tags=[CuisineTag.japanese, ServiceTag.takeout],
)

solo_museum_afternoon = PlaceQuery(
    category=PlaceCategory.museum,
)

# Date night
date_night_italian = PlaceQuery(
    category=PlaceCategory.restaurant,
    tags=[CuisineTag.italian, AtmosphereTag.romantic, PriceTag.expensive],
)

date_night_cocktails = PlaceQuery(
    category=PlaceCategory.bar,
    tags=[AtmosphereTag.romantic, ServiceTag.serves_cocktails],
)

date_night_experience = PlaceQuery(
    category=PlaceCategory.live_music_venue,
    tags=[FeatureTag.live_music, AtmosphereTag.romantic],
)

# Group / friends
group_korean_bbq = PlaceQuery(
    category=PlaceCategory.restaurant,
    tags=[CuisineTag.korean, FeatureTag.group_friendly],
)

group_karaoke_drinks = PlaceQuery(
    category=PlaceCategory.karaoke,
)

group_sports_bar = PlaceQuery(
    category=PlaceCategory.bar,
    tags=[FeatureTag.sports_viewing, ServiceTag.serves_beer],
)

friends_brunch = PlaceQuery(
    tags=[ServiceTag.serves_brunch, FeatureTag.group_friendly, PriceTag.moderate],
)

# Family
family_lunch_kids = PlaceQuery(
    category=PlaceCategory.restaurant,
    tags=[FeatureTag.family_friendly, FeatureTag.kids_menu, ServiceTag.serves_lunch],
)

family_park_picnic = PlaceQuery(
    category=PlaceCategory.park,
)

family_aquarium = PlaceQuery(
    category=PlaceCategory.aquarium,
)

# Work meeting / client
client_lunch = PlaceQuery(
    category=PlaceCategory.restaurant,
    tags=[ServiceTag.serves_lunch, ServiceTag.reservable, PriceTag.moderate],
)

casual_work_coffee = PlaceQuery(
    category=PlaceCategory.cafe,
    tags=[AtmosphereTag.quiet],
)

team_dinner = PlaceQuery(
    category=PlaceCategory.restaurant,
    tags=[FeatureTag.group_friendly, ServiceTag.serves_dinner, PriceTag.expensive],
)

# ---------------------------------------------------------------------------
# 12. Special occasion
# ---------------------------------------------------------------------------

anniversary_dinner = PlaceQuery(
    category=PlaceCategory.restaurant,
    tags=[AtmosphereTag.romantic, PriceTag.expensive, ServiceTag.reservable],
)

birthday_group = PlaceQuery(
    category=PlaceCategory.restaurant,
    tags=[FeatureTag.group_friendly, ServiceTag.serves_cocktails, PriceTag.expensive],
)

celebration_rooftop = PlaceQuery(
    category=PlaceCategory.bar,
    tags=[FeatureTag.outdoor_seating, ServiceTag.serves_cocktails, AtmosphereTag.trendy],  # noqa: E501
)

farewell_drinks = PlaceQuery(
    category=PlaceCategory.bar,
    tags=[FeatureTag.group_friendly, ServiceTag.serves_beer],
)

# ---------------------------------------------------------------------------
# 13. Health / fitness context
# ---------------------------------------------------------------------------

pre_workout_coffee = PlaceQuery(
    category=PlaceCategory.cafe,
    tags=[ServiceTag.serves_breakfast],
)

post_workout_smoothie = PlaceQuery(
    category=PlaceCategory.juice_bar,
)

post_workout_protein = PlaceQuery(
    category=PlaceCategory.restaurant,
    tags=[ServiceTag.serves_lunch],
    # "high-protein" would be an LLM atmosphere tag
)

healthy_vegan_lunch = PlaceQuery(
    category=PlaceCategory.restaurant,
    tags=[DietaryTag.vegan, ServiceTag.serves_lunch],
)

spa_recovery = PlaceQuery(
    category=PlaceCategory.spa,
)

yoga_studio_nearby = PlaceQuery(
    category=PlaceCategory.yoga_studio,
    location=LocationContext(lat=13.7563, lng=100.5018, radius_m=1000),
)

# ---------------------------------------------------------------------------
# 14. Budget-conscious
# ---------------------------------------------------------------------------

cheapest_meal = PlaceQuery(
    category=PlaceCategory.restaurant,
    tags=[PriceTag.budget],
)

street_food_crawl = PlaceQuery(
    category=PlaceCategory.street_food,
)

budget_vegan = PlaceQuery(
    tags=[DietaryTag.vegan, PriceTag.budget],
)

cheap_delivery_tonight = PlaceQuery(
    tags=[ServiceTag.delivery, PriceTag.budget],
)

free_afternoon = PlaceQuery(
    category=PlaceCategory.park,
    # parks are free; combine with neighbourhood for nearby options
    location=LocationContext(lat=13.7563, lng=100.5018, radius_m=2000),
)

# ---------------------------------------------------------------------------
# 15. Treat yourself / splurge
# ---------------------------------------------------------------------------

splurge_omakase = PlaceQuery(
    category=PlaceCategory.restaurant,
    tags=[CuisineTag.japanese, PriceTag.very_expensive, ServiceTag.reservable],
)

splurge_cocktail_bar = PlaceQuery(
    category=PlaceCategory.bar,
    tags=[PriceTag.very_expensive, ServiceTag.serves_cocktails, AtmosphereTag.trendy],
)

splurge_spa = PlaceQuery(
    category=PlaceCategory.spa,
    tags=[PriceTag.expensive],
)

luxury_hotel_bar = PlaceQuery(
    category=PlaceCategory.bar,
    tags=[AtmosphereTag.romantic, PriceTag.expensive],
)

# ---------------------------------------------------------------------------
# 16. Accessibility
# ---------------------------------------------------------------------------

wheelchair_friendly = PlaceQuery(
    tags=[AccessibilityTag.wheelchair_entrance],
)

fully_accessible = PlaceQuery(
    tags=[
        AccessibilityTag.wheelchair_entrance,
        AccessibilityTag.wheelchair_restroom,
        AccessibilityTag.wheelchair_seating,
    ],
)

accessible_parking_restaurant = PlaceQuery(
    category=PlaceCategory.restaurant,
    tags=[AccessibilityTag.wheelchair_parking, AccessibilityTag.wheelchair_entrance],
)

# ---------------------------------------------------------------------------
# 17. Time of day — typed
# ---------------------------------------------------------------------------

morning_spots = PlaceQuery(tags=[TimeTag.morning])
brunch_spots = PlaceQuery(tags=[TimeTag.brunch])
lunch_spots = PlaceQuery(tags=[TimeTag.lunch])
afternoon_spots = PlaceQuery(tags=[TimeTag.afternoon])
evening_spots = PlaceQuery(tags=[TimeTag.evening])
night_spots = PlaceQuery(tags=[TimeTag.night])
late_night_spots = PlaceQuery(tags=[TimeTag.late_night])

# Combining time + vibe
romantic_evening = PlaceQuery(
    category=PlaceCategory.restaurant,
    tags=[TimeTag.evening, AtmosphereTag.romantic],
)

late_night_street_food = PlaceQuery(
    category=PlaceCategory.street_food,
    tags=[TimeTag.late_night],
)

morning_outdoor_coffee = PlaceQuery(
    category=PlaceCategory.cafe,
    tags=[TimeTag.morning, FeatureTag.outdoor_seating],
)

# ---------------------------------------------------------------------------
# 18. Season / weather — typed
# ---------------------------------------------------------------------------

summer_spots = PlaceQuery(tags=[SeasonTag.summer])
rainy_day_spots = PlaceQuery(tags=[SeasonTag.rainy])
winter_spots = PlaceQuery(tags=[SeasonTag.winter])
all_season_spots = PlaceQuery(tags=[SeasonTag.all_season])

# Summer outdoor dining
summer_outdoor_brunch = PlaceQuery(
    tags=[SeasonTag.summer, FeatureTag.outdoor_seating, ServiceTag.serves_brunch],
)

# Rainy day cozy café
rainy_cozy_cafe = PlaceQuery(
    category=PlaceCategory.cafe,
    tags=[SeasonTag.rainy, AtmosphereTag.cozy],
)

# Rainy indoor activity
rainy_indoor_museum = PlaceQuery(
    category=PlaceCategory.museum,
    tags=[SeasonTag.rainy, SeasonTag.all_season],
)

# Hot day — ice cream or juice, outdoor shade optional
summer_cool_treat = PlaceQuery(
    category=PlaceCategory.ice_cream_shop,
    tags=[SeasonTag.summer],
)

# Winter warmth — hot food, cozy
winter_ramen = PlaceQuery(
    category=PlaceCategory.restaurant,
    tags=[CuisineTag.japanese, SeasonTag.winter, AtmosphereTag.cozy],
)

# ---------------------------------------------------------------------------
# 19. Combined time + season + vibe (full intent)
# ---------------------------------------------------------------------------

# "A cozy rainy afternoon café to work from"
rainy_afternoon_work = PlaceQuery(
    category=PlaceCategory.cafe,
    tags=[SeasonTag.rainy, TimeTag.afternoon, AtmosphereTag.cozy, AtmosphereTag.quiet],
)

# "Summer evening rooftop drinks with a view"
summer_rooftop_evening = PlaceQuery(
    category=PlaceCategory.bar,
    tags=[SeasonTag.summer, TimeTag.evening, FeatureTag.rooftop, FeatureTag.scenic_view],  # noqa: E501
)

# "Accessible brunch spot on a budget"
accessible_budget_brunch = PlaceQuery(
    tags=[
        TimeTag.brunch,
        PriceTag.budget,
        AccessibilityTag.wheelchair_entrance,
    ],
)

# "Late-night vegan delivery when it's raining"
rainy_late_vegan_delivery = PlaceQuery(
    tags=[SeasonTag.rainy, TimeTag.late_night, DietaryTag.vegan, ServiceTag.delivery],
)

# "Romantic winter dinner, splurge"
winter_romantic_splurge = PlaceQuery(
    category=PlaceCategory.restaurant,
    tags=[
        SeasonTag.winter,
        TimeTag.evening,
        AtmosphereTag.romantic,
        PriceTag.very_expensive,
        ServiceTag.reservable,
    ],
)

# "Summer morning dog walk + coffee"
summer_morning_dog_cafe = PlaceQuery(
    category=PlaceCategory.cafe,
    tags=[SeasonTag.summer, TimeTag.morning, FeatureTag.dog_friendly, FeatureTag.outdoor_seating],  # noqa: E501
)
