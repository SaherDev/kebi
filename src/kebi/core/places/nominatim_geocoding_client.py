"""NominatimGeocodingClient — OpenStreetMap geocoding (forward + reverse).

Free, no API key. The agent's location-resolution node uses this to turn a
named place into coordinates (forward) and a GPS coordinate into
country/city/neighbourhood (reverse). The OSM usage policy requires an
identifying `User-Agent` header and limits the public instance to roughly
one request per second — a single geocode per chat turn stays within that.
A Redis cache in front of this is a sensible follow-up at scale.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

logger = logging.getLogger(__name__)

_NOMINATIM_API_BASE = "https://nominatim.openstreetmap.org"
_TIMEOUT_SECONDS = 10.0


class GeocodingError(RuntimeError):
    """Raised on a transport- or HTTP-level Nominatim failure.

    The `resolve_location` node treats this as "could not pin the location"
    and asks the user, rather than crashing the turn.
    """


class NominatimGeocodingClient:
    """Geocoding adapter over the public OpenStreetMap Nominatim API."""

    def __init__(self, *, http: httpx.AsyncClient, user_agent: str) -> None:
        self._http = http
        self._user_agent = user_agent

    async def forward(
        self,
        *,
        country: str,
        city: str,
        neighborhood: str | None = None,
    ) -> tuple[float, float] | None:
        """Resolve a named place to ``(lat, lng)``.

        Returns ``None`` when Nominatim finds no match.
        """
        query = ", ".join(p for p in (neighborhood, city, country) if p)
        data = await self._request(
            "/search", {"q": query, "format": "jsonv2", "limit": "1"}
        )
        if not isinstance(data, list) or not data:
            return None
        first = data[0]
        try:
            return float(first["lat"]), float(first["lon"])
        except (KeyError, TypeError, ValueError):
            logger.warning("nominatim_forward_unparseable", extra={"query": query})
            return None

    async def reverse(self, *, lat: float, lng: float) -> dict[str, str] | None:
        """Resolve a coordinate to country / city / neighborhood.

        Returns a dict with whatever components were found; ``neighborhood``
        may be absent because Nominatim coverage varies by region. Returns
        ``None`` when `country` or `city` could not be determined.
        """
        data = await self._request(
            "/reverse",
            {
                "lat": str(lat),
                "lon": str(lng),
                "format": "jsonv2",
                "addressdetails": "1",
            },
        )
        if not isinstance(data, dict):
            return None
        address = data.get("address")
        if not isinstance(address, dict):
            return None
        country = address.get("country")
        city = address.get("city") or address.get("town") or address.get("village")
        neighborhood = (
            address.get("neighbourhood")
            or address.get("suburb")
            or address.get("city_district")
        )
        if not country or not city:
            return None
        result: dict[str, str] = {"country": country, "city": city}
        if neighborhood:
            result["neighborhood"] = neighborhood
        return result

    async def _request(
        self, path: str, params: dict[str, str]
    ) -> dict[str, Any] | list[Any]:
        try:
            response = await self._http.get(
                f"{_NOMINATIM_API_BASE}{path}",
                params=params,
                headers={"User-Agent": self._user_agent},
                timeout=_TIMEOUT_SECONDS,
            )
            response.raise_for_status()
            payload: dict[str, Any] | list[Any] = response.json()
            return payload
        except httpx.HTTPStatusError as exc:
            logger.error(
                "nominatim_http_error",
                extra={"path": path, "status": exc.response.status_code},
            )
            raise GeocodingError(
                f"Nominatim {path} returned {exc.response.status_code}"
            ) from exc
        except Exception as exc:
            logger.exception("nominatim_request_error", extra={"path": path})
            raise GeocodingError(f"Nominatim {path} request failed") from exc
