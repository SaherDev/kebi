"""GooglePlacesClient — Places API v1 HTTP adapter."""

from __future__ import annotations

import asyncio
import functools
import logging
from datetime import UTC, datetime
from typing import Any

import httpx

from ._google_mapper import GOOGLE_PROVIDER_PREFIX, map_place
from ._google_query_builder import build_text_search_params, query_to_google_types
from .models import PlaceObject, PlaceQuery

logger = logging.getLogger(__name__)

_PLACES_API_BASE = "https://places.googleapis.com/v1/places"
_FIELD_MASK = (
    "places.id,"
    "places.displayName,"
    "places.formattedAddress,"
    "places.addressComponents,"
    "places.location,"
    "places.rating,"
    "places.regularOpeningHours,"
    "places.nationalPhoneNumber,"
    "places.websiteUri,"
    "places.types,"
    "places.businessStatus,"
    "places.userRatingCount,"
    "places.timeZone,"
    "places.priceLevel,"
    "places.dineIn,"
    "places.takeout,"
    "places.delivery,"
    "places.reservable,"
    "places.servesBreakfast,"
    "places.servesBrunch,"
    "places.servesLunch,"
    "places.servesDinner,"
    "places.servesBeer,"
    "places.servesWine,"
    "places.servesCocktails,"
    "places.servesVegetarianFood,"
    "places.outdoorSeating,"
    "places.liveMusic,"
    "places.menuForChildren,"
    "places.allowsDogs,"
    "places.goodForChildren,"
    "places.goodForGroups,"
    "places.goodForWatchingSports,"
    "places.accessibilityOptions"
)
# Place Details endpoint returns a single Place; the field mask must omit
# the `places.` prefix that the search endpoints require.
_DETAILS_FIELD_MASK = _FIELD_MASK.replace("places.", "")
# Cap on parallel Place Details GETs. Bounds provider QPS and cost when a
# caller asks for many ids at once (e.g. post-TTL stale refresh).
_DETAILS_CONCURRENCY = 5


@functools.cache
def _shared_http_client() -> httpx.AsyncClient:
    """Process-wide httpx.AsyncClient — shares the connection pool across
    every GooglePlacesClient instance. Closed at process exit."""
    return httpx.AsyncClient()


class GooglePlacesClient:
    def __init__(
        self, api_key: str, http: httpx.AsyncClient | None = None
    ) -> None:
        self._api_key = api_key
        self._http = http if http is not None else _shared_http_client()

    async def search(self, query: PlaceQuery, limit: int = 20) -> list[PlaceObject]:
        """Route to Google's :searchText or :searchNearby based on what the
        query can express.

        Tags like TimeTag/SeasonTag/AccessibilityTag produce no text, so a query
        with only those tags falls back to nearby search when geo is present.
        """
        loc = query.location
        has_geo = (
            loc is not None
            and loc.lat is not None
            and loc.lng is not None
            and loc.radius_m is not None
        )
        if build_text_search_params(query)[0]:
            return await self._text_search(query, limit)
        if has_geo:
            return await self._nearby_search(query, limit)
        return []

    async def get_by_ids(self, provider_ids: list[str]) -> list[PlaceObject]:
        """Fetch places by namespaced provider_ids (Place Details), in parallel.

        Used to refresh DB rows whose location was wiped by the 30-day TTL
        cron — provider_id is the stable identity, place_name is not.
        Concurrency is capped at _DETAILS_CONCURRENCY to bound provider QPS
        and cost; results are filtered to the ids that resolved.
        """
        if not provider_ids:
            return []
        sem = asyncio.Semaphore(_DETAILS_CONCURRENCY)

        async def _bounded(provider_id: str) -> PlaceObject | None:
            async with sem:
                return await self._get_details(provider_id)

        results = await asyncio.gather(*[_bounded(p) for p in provider_ids])
        return [r for r in results if r is not None]

    async def _get_details(self, provider_id: str) -> PlaceObject | None:
        if not provider_id.startswith(GOOGLE_PROVIDER_PREFIX):
            logger.warning(
                "get_by_ids_unsupported_provider",
                extra={"provider_id": provider_id},
            )
            return None
        google_id = provider_id[len(GOOGLE_PROVIDER_PREFIX):]
        results = await self._request("GET", f"/{google_id}", _DETAILS_FIELD_MASK)
        return results[0] if results else None

    async def _text_search(
        self,
        query: PlaceQuery,
        limit: int = 20,
    ) -> list[PlaceObject]:
        text, included_type = build_text_search_params(query)
        if not text:
            return []
        loc = query.location
        body: dict[str, Any] = {
            "textQuery": text,
            "maxResultCount": min(limit, 20),
        }
        if (
            loc
            and loc.lat is not None
            and loc.lng is not None
            and loc.radius_m is not None
        ):
            # searchText only allows `rectangle` inside `locationRestriction`.
            # For a circular bound we have to use `locationBias` — Google rejects
            # `locationRestriction.circle` here with a 400. (searchNearby is the
            # opposite shape: it requires `locationRestriction.circle`.)
            body["locationBias"] = {
                "circle": {
                    "center": {"latitude": loc.lat, "longitude": loc.lng},
                    "radius": float(loc.radius_m),
                }
            }
        if included_type:
            body["includedType"] = included_type
        _apply_common_filters(body, query)
        return await self._request(
            "POST", ":searchText", _FIELD_MASK, body=body
        )

    async def _nearby_search(
        self, query: PlaceQuery, limit: int = 20
    ) -> list[PlaceObject]:
        loc = query.location
        if not loc or loc.lat is None or loc.lng is None or loc.radius_m is None:
            logger.warning("nearby_search_requires_full_location")
            return []
        body: dict[str, Any] = {
            "locationRestriction": {
                "circle": {
                    "center": {"latitude": loc.lat, "longitude": loc.lng},
                    "radius": float(loc.radius_m),
                }
            },
            "maxResultCount": min(limit, 20),
        }
        google_types = query_to_google_types(query)
        if google_types:
            body["includedTypes"] = google_types
        _apply_common_filters(body, query)
        return await self._request(
            "POST", ":searchNearby", _FIELD_MASK, body=body
        )

    async def _request(
        self,
        method: str,
        path: str,
        field_mask: str,
        body: dict[str, Any] | None = None,
    ) -> list[PlaceObject]:
        """Shared HTTP path: auth, error handling, JSON decode, Place parsing.

        Search endpoints return ``{"places": [...]}``; the Place Details
        endpoint returns a flat Place dict. Both shapes are normalized to
        ``list[PlaceObject]`` here. Returns ``[]`` on transport/HTTP errors so
        callers degrade gracefully instead of bubbling exceptions to the agent.
        """
        try:
            response = await self._http.request(
                method,
                f"{_PLACES_API_BASE}{path}",
                json=body,
                headers={
                    "X-Goog-Api-Key": self._api_key,
                    "X-Goog-FieldMask": field_mask,
                },
                timeout=10.0,
            )
            response.raise_for_status()
            data: dict[str, Any] = response.json()
        except httpx.HTTPStatusError as exc:
            # Google returns the actual cause (bad field name, quota, etc.) in
            # the response body — log it so 4xx/5xx are diagnosable from logs
            # without a repro.
            logger.error(
                "google_places_http_error",
                extra={
                    "method": method,
                    "path": path,
                    "status": exc.response.status_code,
                    "body": exc.response.text[:1000],
                },
            )
            return []
        except Exception:
            logger.exception(
                "google_places_request_error",
                extra={"method": method, "path": path},
            )
            return []
        raws = data.get("places") if "places" in data else [data]
        now = datetime.now(UTC)
        return [
            obj
            for raw in (raws or [])
            if (obj := map_place(raw, now)) is not None
        ]


def _apply_common_filters(body: dict[str, Any], query: PlaceQuery) -> None:
    """Decorate the request body with filters shared by text and nearby search."""
    if query.open_now is True:
        body["openNow"] = True
