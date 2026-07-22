"""GooglePlacesClient — Places API v1 HTTP adapter."""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from typing import Any

import httpx

from kebi.core.utils.geo import bounding_box

from ._google_mapper import (
    GOOGLE_PROVIDER_PREFIX,
    NON_VENUE_GEOGRAPHY,
    is_non_venue_geography,
    map_place,
)
from ._google_query_builder import (
    build_text_search_param_sets,
    build_text_search_params,
    query_to_google_types,
)
from .models import NonVenueDetection, PlaceObject, PlaceQuery

logger = logging.getLogger(__name__)

_PLACES_API_BASE = "https://places.googleapis.com/v1/places"
# Search mask — Google Places Pro tier by design (ADR-118). Google is a
# minimal location validator: identity, name, address, location, and
# `types` (which still yields categories + cuisine/dietary tags at no
# extra tier cost). Experiential data (service/feature/price/atmosphere/
# accessibility) is owned by the LLM knowledge layer, never requested
# from Google. `displayName` is the Pro tier-setter; every other field
# here is Essentials.
_FIELD_MASK = (
    "places.id,"
    "places.displayName,"
    "places.formattedAddress,"
    "places.addressComponents,"
    "places.location,"
    "places.types"
)
# Place Details mask — Essentials tier (ADR-118), deliberately narrower
# than the search mask: no `displayName`. Details only refreshes the
# location of already-persisted rows, and the catalog name is
# sticky-authoritative (the merge discards a provider name anyway), so
# the search service backfills `place_name` from the DB row instead of
# paying the Pro rate for a name we throw away. Single-Place endpoint,
# so no `places.` prefix.
_DETAILS_FIELD_MASK = "id,formattedAddress,addressComponents,location,types"
# Cap on parallel Place Details GETs. Bounds provider QPS and cost when a
# caller asks for many ids at once (e.g. post-TTL stale refresh).
_DETAILS_CONCURRENCY = 5
# Cap on parallel :searchText calls when a query carries multiple place_names
# (OR semantics → one searchText per name). Bounds provider QPS/cost the same
# way _DETAILS_CONCURRENCY does for Place Details.
_TEXT_SEARCH_CONCURRENCY = 5


class GooglePlacesClient:
    def __init__(self, *, api_key: str, http: httpx.AsyncClient) -> None:
        self._api_key = api_key
        self._http = http

    async def search(
        self,
        query: PlaceQuery,
        limit: int = 20,
        *,
        rejections: list[NonVenueDetection] | None = None,
    ) -> list[PlaceObject]:
        """Route to Google's :searchText or :searchNearby based on what the
        query can express.

        Tags like TimeTag/SeasonTag/AccessibilityTag produce no text, so a query
        with only those tags falls back to nearby search when geo is present.

        `rejections`, when supplied, collects a `NonVenueDetection` for each
        result the mapper rejected as non-venue geography — the caller can
        narrate the rejection instead of seeing a silently thinner list.
        """
        loc = query.location
        has_geo = (
            loc is not None
            and loc.lat is not None
            and loc.lng is not None
            and loc.radius_m is not None
        )
        if build_text_search_params(query)[0]:
            return await self._text_search(query, limit, rejections=rejections)
        if has_geo:
            return await self._nearby_search(query, limit, rejections=rejections)
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
        google_id = provider_id[len(GOOGLE_PROVIDER_PREFIX) :]
        # require_name=False: the details mask carries no displayName; the
        # search service backfills the name from the catalog row.
        results = await self._request(
            "GET", f"/{google_id}", _DETAILS_FIELD_MASK, require_name=False
        )
        return results[0] if results else None

    async def _text_search(
        self,
        query: PlaceQuery,
        limit: int = 20,
        *,
        rejections: list[NonVenueDetection] | None = None,
    ) -> list[PlaceObject]:
        """Run Google :searchText, fanning out one request per place_name.

        place_names is OR across values; searchText has no OR, so each name is
        its own request (build_text_search_param_sets). Single-name / no-name
        queries stay a single request. Multi-name results are merged in name
        order, deduped on provider_id (then id), and truncated to `limit`.
        """
        param_sets = build_text_search_param_sets(query)
        if not param_sets:
            return []
        if len(param_sets) == 1:
            return await self._search_text_once(
                query, param_sets[0], limit, rejections=rejections
            )

        sem = asyncio.Semaphore(_TEXT_SEARCH_CONCURRENCY)

        async def _bounded(
            params: tuple[str, str | None],
        ) -> list[PlaceObject]:
            async with sem:
                return await self._search_text_once(
                    query, params, limit, rejections=rejections
                )

        batches = await asyncio.gather(*[_bounded(p) for p in param_sets])
        merged: list[PlaceObject] = []
        seen: set[str] = set()
        for batch in batches:
            for place in batch:
                key = place.provider_id or place.id
                if key is not None:
                    if key in seen:
                        continue
                    seen.add(key)
                merged.append(place)
                if len(merged) >= limit:
                    return merged
        return merged

    async def _search_text_once(
        self,
        query: PlaceQuery,
        params: tuple[str, str | None],
        limit: int,
        *,
        rejections: list[NonVenueDetection] | None = None,
    ) -> list[PlaceObject]:
        text, included_type = params
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
            # searchText only allows `rectangle` inside `locationRestriction`;
            # a circular bound has to go through `locationBias` (Google rejects
            # `locationRestriction.circle` here with a 400 — searchNearby is the
            # opposite shape).
            #
            # `locationBias` is a *soft* preference: it ranks toward the circle
            # but does not exclude prominent results outside it. For a distance
            # search that means a far flagship can still out-rank the nearest
            # branch — so a nearest-first query is bounded *hard* with a
            # rectangle derived from the circle. Other searches keep the soft
            # bias (a famous place just outside a city circle shouldn't be cut).
            if query.sort_by == "distance":
                lo_lat, lo_lng, hi_lat, hi_lng = bounding_box(
                    loc.lat, loc.lng, float(loc.radius_m)
                )
                body["locationRestriction"] = {
                    "rectangle": {
                        "low": {"latitude": lo_lat, "longitude": lo_lng},
                        "high": {"latitude": hi_lat, "longitude": hi_lng},
                    }
                }
            else:
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
            "POST", ":searchText", _FIELD_MASK, body=body, rejections=rejections
        )

    async def _nearby_search(
        self,
        query: PlaceQuery,
        limit: int = 20,
        *,
        rejections: list[NonVenueDetection] | None = None,
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
            "POST", ":searchNearby", _FIELD_MASK, body=body, rejections=rejections
        )

    async def _request(
        self,
        method: str,
        path: str,
        field_mask: str,
        body: dict[str, Any] | None = None,
        require_name: bool = True,
        rejections: list[NonVenueDetection] | None = None,
    ) -> list[PlaceObject]:
        """Shared HTTP path: auth, error handling, JSON decode, Place parsing.

        Search endpoints return ``{"places": [...]}``; the Place Details
        endpoint returns a flat Place dict. Both shapes are normalized to
        ``list[PlaceObject]`` here. Returns ``[]`` on transport/HTTP errors so
        callers degrade gracefully instead of bubbling exceptions to the agent.
        """
        # Per-call cost lookup. Place Details paths are dynamic ("/{id}");
        # normalize to "/{place_id}" so the config key is stable across
        # every Place Details call. Endpoint key ↔ SKU tier is 1:1:
        # search endpoints use _FIELD_MASK (Pro), Place Details uses
        # _DETAILS_FIELD_MASK (Essentials) — ADR-118.
        from kebi.core.agent._trace_context import (  # noqa: PLC0415
            current_tool,
            traced_call,
        )
        from kebi.core.config import get_config  # noqa: PLC0415

        endpoint_key = path if path.startswith(":") else "/{place_id}"
        feature = "agent" if current_tool.get() is not None else "extraction"
        pricing = get_config().pricing.external.google_places
        async with traced_call(
            "google_places",
            feature,
            extra={
                "endpoint": endpoint_key,
                "method": method,
                "field_mask_len": len(field_mask),
            },
        ) as t:
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
                # Fold status + body into the message: the app configures no
                # log formatter, so `extra` fields are never rendered. A 403
                # here is almost always billing/API-not-enabled — keep that
                # visible at a glance, since the call still degrades to [].
                logger.error(
                    "google_places_http_error %s %s -> %s: %s",
                    method,
                    path,
                    exc.response.status_code,
                    exc.response.text[:1000],
                )
                t.output = {"status": exc.response.status_code, "places": 0}
                return []
            except Exception as exc:
                logger.exception("google_places_request_error %s %s", method, path)
                t.fail(exc)
                return []
            raws = data.get("places") if "places" in data else [data]
            results = _parse_places(
                raws or [],
                datetime.now(UTC),
                require_name=require_name,
                rejections=rejections,
            )
            # Google bills per request regardless of result count, so cost
            # lands on the call even when results is [] (no-match still cost).
            t.cost_usd = pricing.cost_for(endpoint_key)
            t.output = {"places": len(results)}
            return results


def _parse_places(
    raws: list[dict[str, Any]],
    now: datetime,
    *,
    require_name: bool = True,
    rejections: list[NonVenueDetection] | None = None,
) -> list[PlaceObject]:
    """Map raw Place dicts, collecting non-venue geography rejections.

    Never a silent drop: when the caller supplies `rejections`, each
    named search result the mapper refused as non-venue geography is
    recorded so it can be narrated as a noted interest. Details-mode
    responses (`require_name=False`) never gate, so they never reject.
    """
    results: list[PlaceObject] = []
    for raw in raws:
        obj = map_place(raw, now, require_name=require_name)
        if obj is not None:
            results.append(obj)
            continue
        if rejections is None or not require_name:
            continue
        types = raw.get("types") or []
        name = (raw.get("displayName") or {}).get("text")
        if name and is_non_venue_geography(types):
            raw_id = raw.get("id")
            rejections.append(
                NonVenueDetection(
                    name=name,
                    provider_id=(
                        f"{GOOGLE_PROVIDER_PREFIX}{raw_id}" if raw_id else None
                    ),
                    reason=NON_VENUE_GEOGRAPHY,
                )
            )
    return results


def _apply_common_filters(body: dict[str, Any], query: PlaceQuery) -> None:
    """Decorate the request body with filters shared by text and nearby search."""
    if query.open_now is True:
        body["openNow"] = True
    # Distance ordering: flip both endpoints off their default rank
    # (searchText=RELEVANCE, searchNearby=POPULARITY) to nearest-first.
    # Guarded on coords — searchText only honours DISTANCE with a
    # locationBias, which is set iff lat/lng are present.
    loc = query.location
    if (
        query.sort_by == "distance"
        and loc is not None
        and loc.lat is not None
        and loc.lng is not None
    ):
        body["rankPreference"] = "DISTANCE"
