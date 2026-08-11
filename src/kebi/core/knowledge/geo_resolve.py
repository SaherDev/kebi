"""Verified entity → canonical geo resolution for the knowledge layer.

A harvested claim names an entity ("Hội An"); its key must come from that
entity, not from whatever place anchored the claim. Free-text geocoding is
too unreliable for bare names (ADR-126: "Hội An" free-texts to Đà Nẵng,
"Muine" to Italy), so this resolver uses Nominatim's *structured* query
parameters and then verifies the round trip: the returned component that
would form the key must slug-match the name that was asked for. A lookup
that can't be verified returns `None` — the caller drops the claim rather
than mis-key it.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from kebi.core.areas.keys import display_from_slug
from kebi.core.knowledge.schemas import ResolvedGeo, _slugify

if TYPE_CHECKING:
    from kebi.core.places.nominatim_geocoding_client import (
        GeocodeResult,
        NominatimGeocodingClient,
    )

logger = logging.getLogger(__name__)


def slugs_match(a: str | None, b: str | None) -> bool:
    """True when two names collapse to the same canonical slug — the
    diacritic- and script-insensitive equality the key builder uses, so
    "Hội An" matches "Hoi An"."""
    if not a or not b:
        return False
    return _slugify(a) == _slugify(b)


# Trailing administrative-unit words Nominatim appends to a settlement's
# name ("Ubud District", "Badung Regency") that a human never types. "city"
# is deliberately absent: stripping it would let "Kansas City" verify an ask
# for Kansas — a wrong-entity swap, the exact thing verification exists for.
_ADMIN_SUFFIXES = (
    "district",
    "subdistrict",
    "regency",
    "municipality",
    "prefecture",
    "province",
    "county",
)


def _admin_stripped_slug(value: str) -> str:
    slug = _slugify(value)
    for suffix in _ADMIN_SUFFIXES:
        if slug.endswith("-" + suffix):
            return slug.removesuffix("-" + suffix)
    return slug


def _verified_city_component(name: str, result: GeocodeResult | None) -> str | None:
    """The city component a verified lookup keys under, or None.

    Verification accepts, keyed by what actually matched — never a
    mismatched other field, so a wrong component can't swap the entity:
    the address `city` or the feature's own `name` slug-matches the asked
    name (the `name` case covers settlements above city rank — Tokyo the
    prefecture, Bali the province — which carry their name at the top level
    and nothing under address city/town/village; keying them in the city
    slot is the established practice: `id/bali` already is Bali's key).
    A match modulo a trailing admin-unit word ("Ubud" ≈ "Ubud District")
    also verifies, keyed under the stripped form.
    """
    if result is None or not result.country_code:
        return None
    for component in (result.city, result.name):
        if component and slugs_match(name, component):
            return component
    asked = _admin_stripped_slug(name)
    for component in (result.city, result.name):
        if component and _admin_stripped_slug(component) == asked:
            return display_from_slug(asked)
    return None


class EntityGeoResolver:
    """Structured-geocode an entity name to a verified `ResolvedGeo`.

    Results are memoized per instance — one instance per harvest keeps
    repeated entity names to a single Nominatim call (the public instance is
    rate-limited to ~1 req/s). Geocoder failures resolve to `None`: the
    knowledge layer is best-effort and never lets a lookup error propagate.
    """

    def __init__(self, geocoder: NominatimGeocodingClient) -> None:
        self._geocoder = geocoder
        self._cache: dict[tuple[str, str], ResolvedGeo | None] = {}

    async def resolve_city(self, name: str, country_code: str) -> ResolvedGeo | None:
        """Resolve a city name within a country, verified round-trip.

        The component that will form the key must slug-match the requested
        name (see `_verified_city_component`) — a top hit that merely
        *contains* the name ("Paris Kavin" in Huế for "Paris") fails
        verification.
        """
        key = ("city", f"{country_code}:{_slugify(name)}")
        if key in self._cache:
            return self._cache[key]
        geo: ResolvedGeo | None = None
        result = await self._search(city=name, countrycodes=country_code)
        city = _verified_city_component(name, result)
        if result is not None and city is not None:
            geo = ResolvedGeo(country_code=result.country_code, city=city)
        self._cache[key] = geo
        return geo

    async def resolve_city_global(self, name: str) -> ResolvedGeo | None:
        """Resolve a bare city name with no country constraint, verified.

        Same structured `city=` lookup and round-trip check as
        `resolve_city`, just unconstrained: Nominatim's importance ranking
        picks the prominent bearer of a shared name (Paris → France), and
        the returned component must still slug-match the asked-for name and
        carry a country — so a name that isn't really a city still refuses.
        For famous-city typeahead ("Tokyo"), the prominent pick is the one
        the human meant; callers needing a specific lesser namesake pass a
        country and go through `resolve_city`.
        """
        key = ("city_global", _slugify(name))
        if key in self._cache:
            return self._cache[key]
        geo: ResolvedGeo | None = None
        result = await self._search(city=name)
        city = _verified_city_component(name, result)
        if result is not None and city is not None:
            geo = ResolvedGeo(country_code=result.country_code, city=city)
        self._cache[key] = geo
        return geo

    async def resolve_country(self, name: str) -> ResolvedGeo | None:
        """Resolve a country name to its ISO code. Verified by feature type:
        the match must *be* a country, not a street or venue named after one."""
        key = ("country", _slugify(name))
        if key in self._cache:
            return self._cache[key]
        geo: ResolvedGeo | None = None
        result = await self._search(country=name)
        if (
            result is not None
            and result.country_code
            and result.place_type == "country"
        ):
            geo = ResolvedGeo(country_code=result.country_code)
        self._cache[key] = geo
        return geo

    async def _search(self, **params: str) -> GeocodeResult | None:
        try:
            return await self._geocoder.search_structured(**params)
        except Exception as exc:
            logger.warning("entity geo lookup failed for %r: %s", params, exc)
            return None
