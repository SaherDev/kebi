"""NominatimGeocodingClient — OpenStreetMap geocoding (forward + reverse).

Free, no API key. The agent's location-resolution node uses this to turn a
named place into coordinates (forward) and a GPS coordinate into
country/city/neighbourhood (reverse). The OSM usage policy requires an
identifying `User-Agent` header and limits the public instance to roughly
one request per second — a single geocode per chat turn stays within that.
A Redis cache in front of this is a sensible follow-up at scale.

Every call returns a `GeocodeResult` — coordinates plus two signals reused
from the *same* response (no extra call): the place type (a density proxy,
ADR-084) and the feature's bounding box.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx
from pydantic import BaseModel

logger = logging.getLogger(__name__)

_NOMINATIM_API_BASE = "https://nominatim.openstreetmap.org"
_TIMEOUT_SECONDS = 10.0


class GeocodingError(RuntimeError):
    """Raised on a transport- or HTTP-level Nominatim failure.

    The `resolve_location` node treats this as "could not pin the location"
    and asks the user, rather than crashing the turn.
    """


class GeocodeResult(BaseModel):
    """A geocoded place — coordinates plus signals reused from the response.

    `place_type` is Nominatim's settlement type (city / town / village / …),
    used as a density proxy: "near me" reaches further in a sparse town than
    a dense city (ADR-084). `bbox` is the matched feature's bounding box,
    `[min_lat, max_lat, min_lng, max_lng]` — recorded for a future
    extent-aware refinement. Both come free in the geocode response.
    """

    lat: float
    lng: float
    country: str | None = None
    # ISO-3166 alpha-2 (lowercased) from the response's `address.country_code`
    # — the code canonical geo keys need (ADR-121). Free in the same response.
    country_code: str | None = None
    city: str | None = None
    neighborhood: str | None = None
    place_type: str | None = None
    bbox: list[float] | None = None
    # The matched feature's own name (response top-level `name`). Carried
    # because settlements above city rank (Tokyo the prefecture, Bali the
    # province) surface here and NOT under address city/town/village — a
    # verifier that only reads `city` cannot recognize them.
    name: str | None = None


def _parse_bbox(raw: Any) -> list[float] | None:
    """Parse Nominatim's `boundingbox` (`[south, north, west, east]` strings)
    into `[min_lat, max_lat, min_lng, max_lng]` floats, or `None`."""
    if not isinstance(raw, list) or len(raw) != 4:
        return None
    try:
        south, north, west, east = (float(x) for x in raw)
    except (TypeError, ValueError):
        return None
    return [south, north, west, east]


class NominatimGeocodingClient:
    """Geocoding adapter over the public OpenStreetMap Nominatim API."""

    def __init__(self, *, http: httpx.AsyncClient, user_agent: str) -> None:
        self._http = http
        self._user_agent = user_agent

    async def search(self, *, query: str) -> GeocodeResult | None:
        """Resolve a free-text place query to a `GeocodeResult`.

        Used both for structured place lookups (via `forward`) and for
        corridor destinations (ADR-084), which the resolver names in free
        text ("Suvarnabhumi Airport, Bangkok"). Returns `None` when Nominatim
        finds no match.
        """
        return await self._search({"q": query})

    async def search_structured(
        self,
        *,
        city: str | None = None,
        country: str | None = None,
        countrycodes: str | None = None,
    ) -> GeocodeResult | None:
        """Resolve a place via Nominatim's structured query parameters.

        Structured queries (`city=`, `country=`) match against the named
        address component only, which makes bare names dramatically more
        reliable than free-text `q=` — a bare "Hội An" free-texts to Đà Nẵng
        but structured-matches Hoi An. `countrycodes` restricts results to a
        comma-separated list of ISO-3166 alpha-2 codes. Returns `None` when
        Nominatim finds no match.
        """
        params: dict[str, str] = {}
        if city:
            params["city"] = city
        if country:
            params["country"] = country
        if countrycodes:
            params["countrycodes"] = countrycodes
        if not params:
            return None
        return await self._search(params)

    async def _search(self, query_params: dict[str, str]) -> GeocodeResult | None:
        """Run one `/search` call and parse the top hit — shared by the
        free-text and structured entry points."""
        data = await self._request(
            "/search",
            {**query_params, "format": "jsonv2", "limit": "1", "addressdetails": "1"},
        )
        if not isinstance(data, list) or not data:
            return None
        first = data[0]
        try:
            lat, lng = float(first["lat"]), float(first["lon"])
        except (KeyError, TypeError, ValueError):
            logger.warning(
                "nominatim_search_unparseable", extra={"query": query_params}
            )
            return None
        addr = first.get("address") if isinstance(first.get("address"), dict) else {}
        cc = addr.get("country_code")
        return GeocodeResult(
            lat=lat,
            lng=lng,
            country=addr.get("country"),
            country_code=cc.strip().lower() if isinstance(cc, str) and cc else None,
            city=addr.get("city") or addr.get("town") or addr.get("village"),
            neighborhood=(
                addr.get("neighbourhood")
                or addr.get("suburb")
                or addr.get("city_district")
            ),
            place_type=first.get("addresstype") or first.get("type"),
            bbox=_parse_bbox(first.get("boundingbox")),
            name=first.get("name") or None,
        )

    async def forward(
        self,
        *,
        country: str,
        city: str,
        neighborhood: str | None = None,
    ) -> GeocodeResult | None:
        """Resolve a named place to a `GeocodeResult`.

        Returns ``None`` when Nominatim finds no match.
        """
        query = ", ".join(p for p in (neighborhood, city, country) if p)
        return await self.search(query=query)

    async def reverse(self, *, lat: float, lng: float) -> GeocodeResult | None:
        """Resolve a coordinate to a `GeocodeResult` with place names.

        ``neighborhood`` may be absent because Nominatim coverage varies by
        region. ``place_type`` is derived from which settlement key carried
        the city name (city / town / village) — a density proxy. Returns
        ``None`` when `country` or `city` could not be determined. The
        returned `lat`/`lng` echo the input — the caller's GPS is canonical.
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
        # Density proxy: which settlement key the city name came from.
        if address.get("city"):
            place_type: str | None = "city"
        elif address.get("town"):
            place_type = "town"
        elif address.get("village"):
            place_type = "village"
        else:
            place_type = data.get("addresstype") or data.get("type")
        cc = address.get("country_code")
        return GeocodeResult(
            lat=lat,
            lng=lng,
            country=country,
            country_code=cc.strip().lower() if isinstance(cc, str) and cc else None,
            city=city,
            neighborhood=neighborhood,
            place_type=place_type,
            bbox=_parse_bbox(data.get("boundingbox")),
        )

    async def _request(
        self, path: str, params: dict[str, str]
    ) -> dict[str, Any] | list[Any]:
        # Force English (Romanized) place names — Nominatim otherwise returns
        # them in the local script ("กรุงเทพมหานคร" rather than "Bangkok"),
        # which the English-speaking agent and the rest of the system expect.
        request_params = {**params, "accept-language": "en"}
        try:
            response = await self._http.get(
                f"{_NOMINATIM_API_BASE}{path}",
                params=request_params,
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
