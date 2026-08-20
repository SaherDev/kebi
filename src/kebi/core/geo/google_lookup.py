"""GoogleGeoLookupClient — Geocoding API adapter for registry minting.

The registry mints identity from the same provider the place catalog already
trusts: the Geocoding API is where a named area resolves to its stable
place_id, own clean English name, types, and viewport. Calls happen only at
mint time — once per unique area ever — so no caching layer sits here; the
registry rows *are* the cache.

Every call is pinned to English (`language=en`), the ADR-163 rule: display
localisation is a product feature, key stability is not negotiable.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from .models import GeoComponent, GeoLookupUnit

logger = logging.getLogger(__name__)

_GEOCODE_API_BASE = "https://maps.googleapis.com/maps/api/geocode/json"
_TIMEOUT_SECONDS = 10.0

# Result types that can never be a geographic *unit* — a mint that matches
# one of these matched a street or a mailbox, not an area. This enumerates
# the provider's own type vocabulary, not the world's geography.
_NON_UNIT_TYPES = frozenset(
    {
        "street_address",
        "route",
        "intersection",
        "premise",
        "subpremise",
        "plus_code",
        "postal_code",
        "postal_code_prefix",
        "postal_code_suffix",
        "street_number",
        "floor",
        "room",
        "post_box",
        "parking",
        "bus_station",
        "train_station",
        "transit_station",
    }
)


class GeoLookupError(RuntimeError):
    """Transport- or HTTP-level Geocoding API failure.

    Minting treats this as "cannot mint now" and degrades the write to a
    coarser key rather than failing the caller's operation.
    """


def _parse_viewport(geometry: dict[str, Any]) -> list[float] | None:
    """`geometry.viewport` → `[south, west, north, east]`, or None."""
    viewport = geometry.get("viewport")
    if not isinstance(viewport, dict):
        return None
    try:
        ne, sw = viewport["northeast"], viewport["southwest"]
        return [
            float(sw["lat"]),
            float(sw["lng"]),
            float(ne["lat"]),
            float(ne["lng"]),
        ]
    except (KeyError, TypeError, ValueError):
        return None


def _parse_unit(result: dict[str, Any]) -> GeoLookupUnit | None:
    """One geocode result → GeoLookupUnit, or None when malformed/non-unit."""
    place_id = result.get("place_id")
    types = [t for t in result.get("types") or [] if isinstance(t, str)]
    if not isinstance(place_id, str) or not place_id:
        return None
    if _NON_UNIT_TYPES.intersection(types):
        return None

    raw_components = result.get("address_components") or []
    components: list[GeoComponent] = []
    country_code: str | None = None
    for raw in raw_components:
        if not isinstance(raw, dict):
            continue
        name = raw.get("long_name")
        if not isinstance(name, str) or not name:
            continue
        ctypes = [t for t in raw.get("types") or [] if isinstance(t, str)]
        short = raw.get("short_name")
        components.append(
            GeoComponent(
                name=name,
                short_name=short if isinstance(short, str) else None,
                types=ctypes,
            )
        )
        if "country" in ctypes and isinstance(short, str) and short:
            country_code = short.strip().lower()

    # The result's own name is its first component — the feature itself
    # leads its address chain in Geocoding API responses.
    if not components:
        return None
    geometry = result.get("geometry") or {}
    location = geometry.get("location") or {}
    try:
        lat, lng = float(location["lat"]), float(location["lng"])
    except (KeyError, TypeError, ValueError):
        return None

    return GeoLookupUnit(
        place_id=place_id,
        name=components[0].name,
        types=types,
        country_code=country_code,
        lat=lat,
        lng=lng,
        viewport=_parse_viewport(geometry),
        components=components,
        partial_match=bool(result.get("partial_match")),
    )


class GoogleGeoLookupClient:
    """Registry lookup adapter over the Google Geocoding API."""

    def __init__(self, *, api_key: str, http: httpx.AsyncClient) -> None:
        self._api_key = api_key
        self._http = http

    async def forward(
        self, *, query: str, country_code: str | None
    ) -> GeoLookupUnit | None:
        """Resolve a named unit to its identity.

        `components=country:CC` constrains the match — the same query text
        must never resolve into a different country than the row it will key
        under. Unconstrained only for country-name lookups, where the country
        is the unknown. Returns the first parseable *unit* result, or None.
        """
        params = {"address": query}
        if country_code:
            params["components"] = f"country:{country_code.strip().upper()}"
        data = await self._request(params)
        for result in data:
            unit = _parse_unit(result)
            if unit is not None:
                return unit
        return None

    async def by_place_id(self, place_id: str) -> GeoLookupUnit | None:
        """Resolve a known id back to its unit — refresh and verification."""
        data = await self._request({"place_id": place_id})
        for result in data:
            unit = _parse_unit(result)
            if unit is not None:
                return unit
        return None

    async def _request(self, params: dict[str, str]) -> list[dict[str, Any]]:
        try:
            response = await self._http.get(
                _GEOCODE_API_BASE,
                params={**params, "language": "en", "key": self._api_key},
                timeout=_TIMEOUT_SECONDS,
            )
            response.raise_for_status()
            payload = response.json()
        except httpx.HTTPStatusError as exc:
            logger.error(
                "geo_lookup_http_error",
                extra={"status": exc.response.status_code},
            )
            raise GeoLookupError(
                f"Geocoding API returned {exc.response.status_code}"
            ) from exc
        except Exception as exc:
            logger.exception("geo_lookup_request_error")
            raise GeoLookupError("Geocoding API request failed") from exc

        status = payload.get("status")
        if status == "ZERO_RESULTS":
            return []
        if status != "OK":
            # OVER_QUERY_LIMIT / REQUEST_DENIED / INVALID_REQUEST — operator
            # problems, surfaced as errors rather than silent empty results.
            logger.error("geo_lookup_status_error", extra={"status": status})
            raise GeoLookupError(f"Geocoding API status {status}")
        results = payload.get("results")
        return results if isinstance(results, list) else []
