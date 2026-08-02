"""GoogleGeocoder — Geocoding API adapter (forward + reverse + by-id).

Uses the Google Geocoding API for every direction: forward (named area →
coordinates), reverse (GPS → named places), and by-place-id (the ToS
geometry refresh — place IDs are storable indefinitely, coordinates are
not). One flat Essentials-tier rate, one legacy response shape
(`long_name`/`short_name` casing), one parser.

The Places API is deliberately NOT used here: a Text Search costs 6× as
much and adds nothing the area layer needs. POI-shaped lookups (corridor
airport destinations) stay on the existing `GooglePlacesClient`.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from .protocol import GeocodeResult, GeocodingError

logger = logging.getLogger(__name__)

_GEOCODING_API_URL = "https://maps.googleapis.com/maps/api/geocode/json"

# Namespace tag on provider ids, mirroring the places layer (ADR-054).
# Geocoding API place IDs are the same Google place IDs.
_GOOGLE_PROVIDER_PREFIX = "google:"

# Google result types that say nothing about what kind of feature matched.
_GENERIC_RESULT_TYPES = frozenset(
    {"political", "establishment", "point_of_interest", "geocode", "premise"}
)

# Statuses that mean "no match" rather than "call failed".
_NO_MATCH_STATUSES = frozenset({"ZERO_RESULTS"})


class GoogleGeocoder:
    """Geocoding adapter over the Google Geocoding API."""

    def __init__(
        self,
        *,
        api_key: str,
        http: httpx.AsyncClient,
        timeout_seconds: float = 10.0,
    ) -> None:
        self._api_key = api_key
        self._http = http
        self._timeout_seconds = timeout_seconds

    async def search_area(
        self, *, query: str, region_code: str | None = None
    ) -> GeocodeResult | None:
        """Forward-geocode a named area. `region_code` becomes a hard
        `components=country:XX` filter — results outside it don't return."""
        params = {"address": query}
        if region_code:
            params["components"] = f"country:{region_code.upper()}"
        result = self._top_result(await self._request(params))
        return self._map_result(result) if result else None

    async def geocode_place_id(self, place_id: str) -> GeocodeResult | None:
        """Re-geocode a stored place ID — the geometry refresh path.

        Accepts the namespaced form (`google:ChIJ...`) or a bare ID.
        Identity is pinned by the ID, so no round-trip verification is
        needed on refresh.
        """
        bare = place_id.removeprefix(_GOOGLE_PROVIDER_PREFIX)
        result = self._top_result(await self._request({"place_id": bare}))
        return self._map_result(result) if result else None

    async def reverse(self, *, lat: float, lng: float) -> GeocodeResult | None:
        data = await self._request({"latlng": f"{lat},{lng}"})
        result = self._top_result(data)
        if not result:
            return None
        components = result.get("address_components") or []
        addr = _map_components(components)
        country = addr.get("country")
        city = addr.get("city")
        if not country or not city:
            return None
        return GeocodeResult(
            # Echo the input — the caller's GPS is canonical.
            lat=lat,
            lng=lng,
            country=country,
            country_code=_component_country_code(components),
            city=city,
            neighborhood=addr.get("neighborhood"),
            # Google has no settlement-size signal (every city, town, and
            # village is `locality`), so reverse results carry no density
            # proxy; radius scaling degrades to the medium default.
            place_type=None,
        )

    def _map_result(self, result: dict[str, Any]) -> GeocodeResult | None:
        geometry = result.get("geometry") or {}
        loc = geometry.get("location") or {}
        lat, lng = loc.get("lat"), loc.get("lng")
        if not isinstance(lat, int | float) or not isinstance(lng, int | float):
            return None
        components = result.get("address_components") or []
        addr = _map_components(components)
        place_type = _primary_type(result.get("types") or [])
        # The matched feature's own name is its first address component
        # ("Hội An" for a locality match) — the display name the entity
        # store records and the round-trip verifier compares against.
        name = None
        if components:
            first = components[0].get("long_name")
            name = first if isinstance(first, str) and first else None
        place_id = result.get("place_id")
        return GeocodeResult(
            lat=float(lat),
            lng=float(lng),
            name=name,
            country=addr.get("country"),
            country_code=_component_country_code(components),
            city=addr.get("city"),
            neighborhood=addr.get("neighborhood"),
            place_type=place_type,
            bbox=_parse_viewport(geometry),
            provider_id=(
                f"{_GOOGLE_PROVIDER_PREFIX}{place_id}"
                if isinstance(place_id, str) and place_id
                else None
            ),
        )

    @staticmethod
    def _top_result(data: dict[str, Any]) -> dict[str, Any] | None:
        results = data.get("results")
        if not isinstance(results, list) or not results:
            return None
        first = results[0]
        return first if isinstance(first, dict) else None

    async def _request(self, params: dict[str, str]) -> dict[str, Any]:
        from kebi.core.agent._trace_context import (  # noqa: PLC0415
            current_tool,
            traced_call,
        )
        from kebi.core.config import get_config  # noqa: PLC0415

        # Which param drove the call — for the trace, never the values
        # (latlng is user location; address may name private intent).
        mode = next(iter(params))
        feature = "agent" if current_tool.get() is not None else "geocoding"
        pricing = get_config().pricing.external.google_geocoding
        async with traced_call(
            "google_geocoding", feature, extra={"mode": mode}
        ) as t:
            try:
                response = await self._http.get(
                    _GEOCODING_API_URL,
                    params={**params, "language": "en", "key": self._api_key},
                    timeout=self._timeout_seconds,
                )
                response.raise_for_status()
                data: dict[str, Any] = response.json()
            except httpx.HTTPStatusError as exc:
                logger.error(
                    "google_geocoding_http_error %s -> %s",
                    mode,
                    exc.response.status_code,
                )
                t.fail(exc)
                raise GeocodingError(
                    f"Geocoding API returned {exc.response.status_code}"
                ) from exc
            except Exception as exc:
                logger.exception("google_geocoding_request_error %s", mode)
                t.fail(exc)
                raise GeocodingError("Geocoding API request failed") from exc
            # Google bills per request regardless of match count — cost
            # lands even on ZERO_RESULTS.
            t.cost_usd = pricing.per_call
            status = data.get("status")
            if status in _NO_MATCH_STATUSES:
                t.output = {"status": status, "results": 0}
                return {}
            if status != "OK":
                # REQUEST_DENIED / OVER_QUERY_LIMIT / INVALID_REQUEST —
                # call-level failure, not a no-match.
                message = data.get("error_message") or ""
                logger.error("google_geocoding_status %s: %s", status, message)
                t.output = {"status": status, "results": 0}
                raise GeocodingError(f"Geocoding API status {status}")
            t.output = {"status": status, "results": len(data.get("results") or [])}
            return data


def _map_components(components: list[dict[str, Any]]) -> dict[str, str]:
    from kebi.core.places._address_components import (  # noqa: PLC0415
        map_address_components,
    )

    return map_address_components(components, text_key="long_name")


def _component_country_code(components: list[dict[str, Any]]) -> str | None:
    from kebi.core.places._address_components import country_code  # noqa: PLC0415

    return country_code(components, short_key="short_name")


def _primary_type(types: list[str]) -> str | None:
    """First type that says what the feature *is* (`locality`,
    `administrative_area_level_1`, `country`, `route`, …)."""
    for t in types:
        if t not in _GENERIC_RESULT_TYPES:
            return t
    return types[0] if types else None


def _parse_viewport(geometry: dict[str, Any]) -> list[float] | None:
    """`geometry.bounds` (the feature's real extent) preferred over
    `geometry.viewport` (a display recommendation); either → the repo-wide
    `[min_lat, max_lat, min_lng, max_lng]` convention."""
    box = geometry.get("bounds") or geometry.get("viewport")
    if not isinstance(box, dict):
        return None
    sw, ne = box.get("southwest") or {}, box.get("northeast") or {}
    try:
        return [
            float(sw["lat"]),
            float(ne["lat"]),
            float(sw["lng"]),
            float(ne["lng"]),
        ]
    except (KeyError, TypeError, ValueError):
        return None
