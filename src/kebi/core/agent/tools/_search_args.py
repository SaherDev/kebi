"""Shared LLM-facing arg descriptions for the consult-family tools.

`find_saved` and `suggest_places` accept the same arg
schema (intent + category bias + hard-constraint tags + named-area overrides +
limit). Keeping the descriptions in one place means the agent sees
byte-identical argument docs for both — so it can pick between them on
routing semantics rather than on prompt drift.

**These descriptions are where filter semantics live (ADR-137).** The
controlled tag vocabulary, the category list, and the named-area rules used to
be restated in the system prompt, which spent most of its budget teaching the
model how to fill args and had little left for the answer itself. They belong
here: the model reads them at the point of filling the arg, they ride the
cached tool definitions rather than the per-turn prompt, and the prompt is
free to be about voice and answer quality. Do not re-add them to `agent.txt`.

These are plain constants, not a Pydantic args_schema, because
LangChain's @tool decorator does not compose cleanly with
`args_schema=PydanticModel` when the function also takes InjectedState /
InjectedToolCallId — the schema path strips the injected params from
the call site. Annotated[..., Field(...)] is the only shape that
survives both LLM tool-call schema generation AND runtime injection.
Verified empirically; do not refactor to args_schema= without
re-validating end-to-end against a live agent turn.
"""

from __future__ import annotations

QUERY_DESC = (
    "Free-text intent in noun-phrase form, e.g. 'cozy ramen for dinner'. "
    "Drives the pgvector + FTS retrieval (RRF-fused) on saved places, and "
    "the namer LLM prompt on suggested places. Keep it semantically "
    "rich but tight; do not embed constraint values that already live in `tags`."
)

CATEGORIES_DESC = (
    "OR-combined coarse categories the result must match one of (e.g. "
    "['restaurant', 'cafe']). Pass when the user implies a place type; omit "
    "when the query alone is specific enough. Common values: restaurant, "
    "cafe, bar, pub, bakery, dessert_shop, street_food, night_market, "
    "tea_house, juice_bar, park, beach, garden, museum, art_gallery, "
    "temple, landmark, viewpoint, live_music_venue, concert_hall, theater, "
    "nightclub, karaoke, cinema, bowling_alley, aquarium, spa, massage, "
    "yoga_studio, hot_spring, gym, hotel, hostel, resort, shopping_mall, "
    "bookstore, coworking_space, library, study_cafe — plus the errand "
    "types: pharmacy, atm, bank, supermarket, grocery_store, "
    "convenience_store, gas_station, parking, laundry, post_office."
)

TAGS_DESC = (
    "AND-combined tag values the result must carry — flat strings (values "
    "only, never 'type:value'). Pass every value that applies.\n"
    "\n"
    "Enforcement differs by class. **dietary** and **accessibility** values "
    "are HARD filters: a place without the tag is excluded, and you must "
    "pass them on every call in a turn where the result could be food "
    "(dietary) or where the user has stated an access need. Every other "
    "class steers search and ranking but does not exclude a freshly "
    "discovered place that simply hasn't been tagged yet. On saved-places "
    "searches all values filter strictly.\n"
    "\n"
    "Vocabulary (core/places/tags.py):\n"
    "- cuisine: Thai, Japanese, Korean, Chinese, Italian, French, Mexican, "
    "Indian, Vietnamese, Mediterranean, American, Greek, Spanish, Turkish, "
    "Indonesian, 'Middle Eastern', Brazilian, Seafood, Steakhouse\n"
    "- dietary: vegan, vegetarian, halal, vegetarian_options\n"
    "- feature: outdoor_seating, indoor, outdoor, rooftop, waterfront, "
    "garden, scenic_view, private_room, fireplace, dog_friendly, "
    "family_friendly, group_friendly, kids_menu, sports_viewing, "
    "live_music, parking, open_late, open_24h\n"
    "- atmosphere: cozy, romantic, trendy, quiet, lively, intimate, "
    "spacious, vibrant, laid_back, luxurious, casual, upscale, hidden_gem, "
    "instagram_worthy, vintage, industrial, minimalist, bohemian, "
    "traditional, modern\n"
    "- service: dine_in, takeout, delivery, reservable, serves_breakfast, "
    "serves_brunch, serves_lunch, serves_dinner, serves_beer, serves_wine, "
    "serves_cocktails\n"
    "- price: free, budget, moderate, expensive, very_expensive\n"
    "- accessibility: wheelchair_parking, wheelchair_entrance, "
    "wheelchair_restroom, wheelchair_seating\n"
    "- time: morning, brunch, lunch, afternoon, evening, night, late_night, "
    "all_day\n"
    "- season: summer, winter, rainy, spring, autumn, all_season"
)

# The three area args share one rule, stated once and appended to each, so the
# "don't echo the working location" trap is impossible to read past.
_AREA_RULE = (
    " Pass ONLY when the user named an area DIFFERENT from this turn's "
    "working location. Never echo the working location's own value back: "
    "the geofence already covers it, and echoing suppresses the geofence "
    "AND matches against a frequently-NULL address column, yielding zero "
    "results despite real matches."
)

NEIGHBORHOOD_DESC = "Named neighborhood to scope to (e.g. 'Sukhumvit')." + _AREA_RULE

CITY_DESC = "Named city to scope to (e.g. 'Chiang Mai')." + _AREA_RULE

COUNTRY_DESC = (
    "Named country to scope to (e.g. 'Thailand'). Rarely needed — most turns "
    "are city-scoped or finer." + _AREA_RULE
)

LIMIT_DESC = (
    "How many candidates to return — pick based on context: one confident "
    "pick (1), a short comparison (3-5), a browse (10+). Capped by config; "
    "defaults if omitted."
)
