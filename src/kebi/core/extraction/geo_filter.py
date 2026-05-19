"""Drop geographic-feature results from a PlacesSearchService result set.

`PlacesSearchService.find()` returns whatever venues match the query —
including administrative-only matches when the LLM picker's input is
something like "Bangkok" or "Sukhumvit Road". The legacy `PlacesSearcher`
filtered these out using Google's raw `place_types` field (locality,
country, route, postal_code, intersection, …). The v2 module abstracts
Google types away to `PlaceCategory`, so the new filter keys on category
emptiness + a small administrative-name allow-list instead.

This is an extraction-side concern, not a generic search concern, so it
lives here. `core/places/` is frozen per spec 030 FR-010a.
"""

from __future__ import annotations

import re

from kebi.core.places import PlaceObject

# Names that resolve to administrative units rather than savable venues.
# Kept short on purpose — these are the most common picker-confusion
# cases (cities, districts, road names) where Google returns a match
# but there's nothing for the user to "save".
_ADMINISTRATIVE_NAME_RE = re.compile(
    r"\b(district|province|prefecture|county|state|country|road|street|"
    r"avenue|boulevard|highway|expressway|alley|soi|neighborhood)\b",
    re.IGNORECASE,
)


def drop_geographic_features(places: list[PlaceObject]) -> list[PlaceObject]:
    """Return only the places that look like savable venues.

    A place is dropped when:
    - its `categories` list is empty (Google returned no business-type
      classification — typical for locality / country / route hits), AND
    - its `place_name` matches the administrative-name heuristic.

    Empty-categories alone is not enough: a valid niche venue may have
    no Google category. Empty + administrative-sounding name is the
    high-precision signal.
    """
    kept: list[PlaceObject] = []
    for place in places:
        has_category = bool(place.categories)
        looks_administrative = bool(_ADMINISTRATIVE_NAME_RE.search(place.place_name))
        if not has_category and looks_administrative:
            continue
        kept.append(place)
    return kept
