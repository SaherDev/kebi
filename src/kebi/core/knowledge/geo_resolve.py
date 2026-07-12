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

        The result's `city` component (the part `build_geo_key` slugifies)
        must slug-match the requested name — a top hit that merely *contains*
        the name ("Paris Kavin" in Huế for "Paris") fails verification.
        """
        key = ("city", f"{country_code}:{_slugify(name)}")
        if key in self._cache:
            return self._cache[key]
        geo: ResolvedGeo | None = None
        result = await self._search(city=name, countrycodes=country_code)
        if (
            result is not None
            and result.country_code
            and result.city
            and slugs_match(name, result.city)
        ):
            geo = ResolvedGeo(country_code=result.country_code, city=result.city)
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
