"""Address-component ranking shared by the places mapper and the geocoder.

Google ships the same component semantics in two casings: the Places API
(New) uses `longText`/`shortText`, the legacy Geocoding API (reverse
geocoding) uses `long_name`/`short_name`. Both call sites rank components
identically (ADR-119) — this module owns the table and the walk,
parameterized by casing, so the two can never drift.
"""

from __future__ import annotations

from typing import Any

# addressComponents type → (field, priority). Lower priority wins; component
# order in Google's response never matters. The fallback ranks exist because
# municipality-style cities (Đà Nẵng, Bangkok) arrive as
# administrative_area_level_1 with the district as level_2 and NO locality —
# without the fallback their city/neighborhood drop on the floor (ADR-119).
# postal_town covers UK addresses. Accepted edge: where level_1 is a
# state/province (US, AU) it becomes city only when Google supplies no
# locality/postal_town at all — rare for venues, and better than null.
ADDR_COMPONENT_TO_FIELD: dict[str, tuple[str, int]] = {
    "locality": ("city", 0),
    "postal_town": ("city", 1),
    "administrative_area_level_1": ("city", 2),
    "sublocality_level_1": ("neighborhood", 0),
    "neighborhood": ("neighborhood", 1),
    # Japanese district names (Asakusa, Toyosu) arrive as level_2; the
    # levels below that are chōme/block numbers and stay unmapped.
    "sublocality_level_2": ("neighborhood", 2),
    "administrative_area_level_2": ("neighborhood", 3),
    "country": ("country", 0),
}


def map_address_components(
    components: list[dict[str, Any]],
    *,
    text_key: str = "longText",
) -> dict[str, str]:
    """Best-ranked component per field (see ADDR_COMPONENT_TO_FIELD)."""
    best: dict[str, tuple[int, str]] = {}
    for component in components:
        text = component.get(text_key) or ""
        if not text:
            continue
        for comp_type in component.get("types") or []:
            mapped = ADDR_COMPONENT_TO_FIELD.get(comp_type)
            if mapped is None:
                continue
            field, rank = mapped
            if field not in best or rank < best[field][0]:
                best[field] = (rank, text)
    return {field: text for field, (_, text) in best.items()}


def country_code(
    components: list[dict[str, Any]],
    *,
    short_key: str = "shortText",
) -> str | None:
    """ISO-3166 alpha-2 from the country component's short text, lowercased.

    The country component carries both the display name ("United Arab
    Emirates") and the code ("AE"). The code is what canonical geo keys
    need (ADR-121), so it is captured separately here.
    """
    for component in components:
        if "country" in (component.get("types") or []):
            short = component.get(short_key)
            if isinstance(short, str) and short:
                return short.strip().lower()
    return None
