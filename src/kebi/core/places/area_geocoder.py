"""Resolve which area a coordinate is in, from the provider's own answer.

A place's stored area used to come from the components riding its Place
Details response, and those disagree with themselves: three venues a few
hundred metres apart on one island came back with the same component
*types* but different values at the same administrative level — two named
by the desa, one by the island — so one island keyed as two areas. No
ranking rule can repair that, because the ranking already picked the right
level; the values themselves conflict.

Reverse-geocoding the coordinate does not have that problem. The same three
venues resolve to one area with one provider id, and the names come back in
the requested language rather than the local one, which is the other half of
what was splitting keys. Identity therefore comes from where a place *is*,
not from what its address record happened to say — and it costs no
hand-maintained list of areas, which cannot scale past the ones someone
already noticed were wrong.

Cached on a rounded coordinate: administrative boundaries do not move, and
everything inside roughly a city block resolves to the same area, so the
provider is asked once per neighbourhood rather than once per save.
"""

from __future__ import annotations

import logging
from typing import Any, Protocol

import httpx
from pydantic import BaseModel

from ._google_mapper import _map_address_components

logger = logging.getLogger(__name__)

_GEOCODE_URL = "https://maps.googleapis.com/maps/api/geocode/json"
_TIMEOUT_SECONDS = 10.0
# ~11 m at the equator. Fine enough that two sides of a street never merge,
# coarse enough that every save in one small area shares a cache entry.
_COORD_PRECISION = 4
# Boundaries are effectively static; the TTL exists to let corrections land,
# not because the answer expires.
_CACHE_TTL_SECONDS = 60 * 60 * 24 * 30


class ResolvedArea(BaseModel):
    """The area a coordinate falls in, as the provider names it.

    `area_id` is the provider's own identifier for the area — stable across
    spellings, languages, and which venue was asked. It is what makes two
    differently-named responses recognisable as one place.
    """

    area_id: str | None = None
    country_code: str | None = None
    city: str | None = None
    neighborhood: str | None = None


class AreaGeocoderProtocol(Protocol):
    async def resolve(self, lat: float, lng: float) -> ResolvedArea | None: ...


class GoogleAreaGeocoder:
    """Reverse geocoding over Google's Geocoding API.

    A different API from Places, deliberately: Places answers "what is this
    venue", the geocoder answers "what contains this point", and only the
    second is consistent between two venues in one area.
    """

    def __init__(
        self,
        *,
        http: httpx.AsyncClient,
        api_key: str,
        cache: Any | None = None,
        language: str = "en",
    ) -> None:
        self._http = http
        self._api_key = api_key
        self._cache = cache
        self._language = language

    def _cache_key(self, lat: float, lng: float) -> str:
        return (
            f"area_geo:v1:{round(lat, _COORD_PRECISION)}:{round(lng, _COORD_PRECISION)}"
        )

    async def resolve(self, lat: float, lng: float) -> ResolvedArea | None:
        """The area containing this point, or None when it cannot be resolved.

        Never raises: a place whose area cannot be resolved keeps whatever
        geography it already had. Losing the refinement is a worse key, not
        a failed save.
        """
        # Imported here, not at module scope: the tracing helper reaches back
        # into the agent package, and places is imported on the way in.
        from kebi.core.agent._trace_context import traced_call

        key = self._cache_key(lat, lng)
        if self._cache is not None:
            try:
                if (hit := await self._cache.get(key)) is not None:
                    return ResolvedArea.model_validate_json(hit)
            except Exception:
                logger.debug("area geocode cache read failed", exc_info=True)

        try:
            async with traced_call(
                "places.reverse_geocode",
                "geocoding",
                role="area_geocoder",
                standalone=True,
            ) as t:
                response = await self._http.get(
                    _GEOCODE_URL,
                    params={
                        "latlng": f"{lat},{lng}",
                        "key": self._api_key,
                        "language": self._language,
                    },
                    timeout=_TIMEOUT_SECONDS,
                )
                response.raise_for_status()
                payload = response.json()
                t.output = {"status": payload.get("status")}
        except Exception:
            logger.warning("reverse geocode failed for %s,%s", lat, lng, exc_info=True)
            return None

        resolved = _parse(payload)
        if resolved is None:
            return None
        if self._cache is not None:
            try:
                await self._cache.set(
                    key, resolved.model_dump_json(), ttl=_CACHE_TTL_SECONDS
                )
            except Exception:
                logger.debug("area geocode cache write failed", exc_info=True)
        return resolved


def _parse(payload: dict[str, Any]) -> ResolvedArea | None:
    """Pull the area out of a geocode response.

    The first result is the finest-grained match for the point and carries
    the full component chain; later results are the same point described at
    coarser levels. Components are ranked by the one table that already
    knows which type means what, so the geocoder and the details path can
    never disagree about which component is "the city".
    """
    if payload.get("status") != "OK":
        return None
    results = payload.get("results") or []
    if not results:
        return None

    first = results[0]
    # Legacy geocoding names components in snake_case; the ranking table is
    # written against the Places shape, so translate rather than duplicate it.
    components = [
        {"longText": c.get("long_name"), "types": c.get("types") or []}
        for c in (first.get("address_components") or [])
    ]
    mapped = _map_address_components(components)

    country_code = None
    for component in first.get("address_components") or []:
        if "country" in (component.get("types") or []):
            short = component.get("short_name")
            if short:
                country_code = short.lower()
            break

    # The area's own id, not the address match's: prefer the coarsest result
    # that is still an area rather than a street or a building.
    area_id = _area_id(results)

    if not country_code and not mapped.get("city"):
        return None
    return ResolvedArea(
        area_id=area_id,
        country_code=country_code,
        city=mapped.get("city"),
        neighborhood=mapped.get("neighborhood"),
    )


# Result types that name an area rather than an address, finest first.
_AREA_RESULT_TYPES = (
    "sublocality_level_1",
    "neighborhood",
    "administrative_area_level_4",
    "administrative_area_level_3",
    "locality",
)


def _area_id(results: list[dict[str, Any]]) -> str | None:
    for wanted in _AREA_RESULT_TYPES:
        for result in results:
            if wanted in (result.get("types") or []):
                place_id = result.get("place_id")
                if place_id:
                    return str(place_id)
    return None
