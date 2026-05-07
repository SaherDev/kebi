"""Google Places API client for place validation and discovery."""

import asyncio
import difflib
import logging
import math
import re
from enum import Enum
from typing import Any, Protocol, cast

import httpx
from pydantic import BaseModel

from kebi.core.config import get_config, get_env
from kebi.core.places.models import (
    HoursDict,
    PlaceAttributes,
    PlaceObject,
    PlaceType,
)

logger = logging.getLogger(__name__)

# Mirrors the `circle:50000@...` locationbias radius used in `validate_place`
# and `geocode` so post-filtering treats "in the search area" the same way
# Google was asked to bias toward.
_LOCATION_BIAS_RADIUS_KM = 50.0
_GOOGLE_GEOCODE_URL = "https://maps.googleapis.com/maps/api/geocode/json"


def _haversine_km(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """Great-circle distance between two lat/lng points in kilometres."""
    radius_km = 6371.0
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lng2 - lng1)
    a = (
        math.sin(dphi / 2) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    )
    return 2 * radius_km * math.asin(math.sqrt(a))


# Google Places "types" array → PlaceType. Longer per-category lists win
# over one-off aliases so near-equivalent strings still route correctly.
# Order matters: a venue tagged both `restaurant` and `store` lands as
# food_and_drink (the more specific food signal wins).
GOOGLE_TYPE_TO_PLACE_TYPE: dict[str, PlaceType] = {
    # food_and_drink
    "restaurant": PlaceType.food_and_drink,
    "cafe": PlaceType.food_and_drink,
    "bar": PlaceType.food_and_drink,
    "bakery": PlaceType.food_and_drink,
    "meal_takeaway": PlaceType.food_and_drink,
    "meal_delivery": PlaceType.food_and_drink,
    "food": PlaceType.food_and_drink,
    "night_club": PlaceType.food_and_drink,
    # things_to_do
    "museum": PlaceType.things_to_do,
    "park": PlaceType.things_to_do,
    "tourist_attraction": PlaceType.things_to_do,
    "aquarium": PlaceType.things_to_do,
    "zoo": PlaceType.things_to_do,
    "art_gallery": PlaceType.things_to_do,
    "amusement_park": PlaceType.things_to_do,
    "stadium": PlaceType.things_to_do,
    "movie_theater": PlaceType.things_to_do,
    # shopping
    "store": PlaceType.shopping,
    "shopping_mall": PlaceType.shopping,
    "book_store": PlaceType.shopping,
    "clothing_store": PlaceType.shopping,
    "shoe_store": PlaceType.shopping,
    "jewelry_store": PlaceType.shopping,
    "department_store": PlaceType.shopping,
    "supermarket": PlaceType.shopping,
    # accommodation
    "lodging": PlaceType.accommodation,
    # services
    "gym": PlaceType.services,
    "spa": PlaceType.services,
    "beauty_salon": PlaceType.services,
    "hair_care": PlaceType.services,
    "pharmacy": PlaceType.services,
    "laundry": PlaceType.services,
    "post_office": PlaceType.services,
    "bank": PlaceType.services,
}


def google_types_to_place_type(types: list[str]) -> PlaceType:
    """Map a Google `types[]` array to a canonical `PlaceType`.

    Scans in order — first known type wins. Defaults to `PlaceType.services`
    when no known type is present and emits `google_place_type_unknown` so
    coverage gaps surface in logs.
    """
    for google_type in types:
        mapped = GOOGLE_TYPE_TO_PLACE_TYPE.get(google_type)
        if mapped is not None:
            return mapped
    logger.info("google_place_type_unknown", extra={"types": list(types)})
    return PlaceType.services

# ---------------------------------------------------------------------------
# Opening-hours mapping (Google Places API v1 → HoursDict)
# ---------------------------------------------------------------------------

# Google v1 uses integer days where 0 = Sunday through 6 = Saturday.
DAY_INT_TO_NAME: dict[int, str] = {
    0: "sunday",
    1: "monday",
    2: "tuesday",
    3: "wednesday",
    4: "thursday",
    5: "friday",
    6: "saturday",
}


def _map_opening_hours(response: dict[str, Any]) -> HoursDict | None:
    """Map a Places v1 Place Details response into a HoursDict.

    Reads `regularOpeningHours.periods` and `timeZone.id` (IANA string).
    Returns None if either is missing — HoursDict requires a timezone
    whenever any day key is present, so a response without `timeZone.id`
    cannot produce a valid HoursDict.

    Each period in the v1 API carries `open` and (optionally) `close`
    objects shaped as:
        {"day": <0-6>, "hour": <0-23>, "minute": <0-59>}
    The classic Places API used `"time": "HHMM"` strings instead; this
    parser is v1-only and is called only from `get_place_details`, which
    targets the v1 endpoint.

    Days with no period at all are returned as None (closed). A period
    with an `open` but no `close` is treated as a 24-hour open day.
    """
    opening_hours = response.get("regularOpeningHours")
    time_zone = response.get("timeZone") or {}
    timezone_id = time_zone.get("id") if isinstance(time_zone, dict) else None
    if not opening_hours or not timezone_id:
        return None

    periods = opening_hours.get("periods", [])
    if not periods:
        return None

    hours: dict[str, str | None] = {}
    for period in periods:
        open_obj = period.get("open") or {}
        close_obj = period.get("close")
        day_int = open_obj.get("day")
        if day_int is None or day_int not in DAY_INT_TO_NAME:
            continue
        day_name = DAY_INT_TO_NAME[day_int]
        if close_obj is None:
            # No close → 24-hour open day.
            hours[day_name] = "00:00-00:00"
        else:
            hours[day_name] = f"{_fmt_clock(open_obj)}-{_fmt_clock(close_obj)}"

    # Any day not represented by a period is closed.
    for day_name in DAY_INT_TO_NAME.values():
        if day_name not in hours:
            hours[day_name] = None

    result = cast(HoursDict, {**hours, "timezone": timezone_id})
    return result


def _fmt_clock(clock: dict[str, Any]) -> str:
    """Format a Places v1 clock object (`{hour, minute}`) as `"HH:MM"`.

    Missing or malformed values default to 0 so partial responses degrade
    gracefully to `"00:00"` rather than producing a malformed string.
    """
    hour = clock.get("hour")
    minute = clock.get("minute")
    h = hour if isinstance(hour, int) and 0 <= hour <= 23 else 0
    m = minute if isinstance(minute, int) and 0 <= minute <= 59 else 0
    return f"{h:02d}:{m:02d}"


def _normalize(text: str) -> str:
    """Lowercase and strip punctuation for name comparison.

    The LLM returns structured output with name and city as separate fields,
    so no location-noise filtering is needed — just basic normalization.
    Returns "" for strings shorter than 4 non-space characters (too short
    to compare meaningfully).
    """
    normalized = re.sub(r"[^\w\s]", "", text.lower()).strip()
    if len(normalized.replace(" ", "")) < 4:
        return ""
    return normalized


class PlacesMatchQuality(str, Enum):
    """Quality of match against Google Places database."""

    EXACT = "EXACT"  # Name similarity ≥ 0.95
    FUZZY = "FUZZY"  # Name similarity ≥ 0.80
    CATEGORY_ONLY = "CATEGORY_ONLY"  # Place found, name similarity < 0.80
    NONE = "NONE"  # No match found


class PlacesMatchResult(BaseModel):
    """Result of validating a place against Google Places."""

    match_quality: PlacesMatchQuality
    validated_name: str | None = None
    external_provider: str = "google"  # set by the client implementation
    external_id: str | None = None  # provider's own ID for the place
    lat: float | None = None
    lng: float | None = None
    address: str | None = None  # formatted_address from the provider
    place_types: list[str] = []  # Google Places 'types' (e.g. ["restaurant", "food"])


class PlacesClient(Protocol):
    """Protocol for place validation, discovery, and validation against external database."""

    async def validate_place(
        self,
        name: str,
        location: str | None = None,
        location_bias: dict[str, float] | None = None,
    ) -> PlacesMatchResult:
        """Validate a place name and return match result."""
        ...

    async def validate_places(
        self,
        names: list[str],
        location_bias: dict[str, float] | None = None,
    ) -> list[PlaceObject]:
        """Validate a list of place names in parallel and return confirmed PlaceObjects."""
        ...

    async def discover(
        self, search_location: dict[str, float], filters: dict[str, Any]
    ) -> list[dict[str, Any]]:
        """Discover places near a location using Nearby Search.

        Args:
            search_location: {"lat": float, "lng": float}
            filters: {"opennow": bool, "type": str, "keyword": str, ...}

        Returns:
            List of place result dicts from Google Places API
        """
        ...

    async def validate(self, candidate: Any, filters: dict[str, Any]) -> bool:
        """Validate a candidate place against filter constraints.

        Args:
            candidate: Candidate object with lat, lng, place_id, place_name
            filters: {"opennow": bool, ...}

        Returns:
            True if candidate passes all constraints, False otherwise
        """
        ...

    async def geocode(
        self,
        place_name: str,
        location_bias: dict[str, float] | None = None,
    ) -> dict[str, float] | None:
        """Resolve a place name to coordinates.

        Args:
            place_name: Place name to geocode
            location_bias: Optional {'lat': float, 'lng': float} to bias results

        Returns:
            {'lat': float, 'lng': float} or None if not found or on failure
        """
        ...

    async def reverse_geocode(self, lat: float, lng: float) -> str | None:
        """Resolve coordinates to a human-readable city/country label.

        Returns a short label like "Magdeburg, Germany" or None on failure
        / no results / coords in unpopulated areas. Used to give the agent
        a city name it can reason about — raw lat/lng alone is too low-info
        for an LLM to generate locality-correct place suggestions.
        """
        ...

    async def get_place_details(self, external_id: str) -> dict[str, Any] | None:
        """Fetch full place details by raw provider external_id (feature 019).

        Receives a PLAIN external_id (no namespace prefix — the namespace is
        stripped by PlacesService.enrich_batch before the call).

        Returns a dict with the keys (any may be missing):
            lat:        float
            lng:        float
            address:    str
            hours:      HoursDict (contains 'timezone' IANA key when any day
                                   key is present)
            rating:     float
            phone:      str
            photo_url:  str
            popularity: float (normalized 0-1)

        Returns None on any HTTP failure — the caller (PlacesService) treats
        per-place failures as "no enrichment this call", not a fatal error
        (per FR-026 / ADR-054). The caller splits the dict into GeoData
        (Tier 2) and PlaceEnrichment (Tier 3).
        """
        ...


class GooglePlacesClient:
    """Google Places API client for place validation, discovery, and validation (ADR-049, ADR-022)."""

    def __init__(self) -> None:
        """Initialize with API key from config."""
        api_key = get_env().GOOGLE_API_KEY
        if not api_key:
            raise ValueError("Google API key not configured")
        self.api_key: str = api_key

    async def validate_place(
        self,
        name: str,
        location: str | None = None,
        location_bias: dict[str, float] | None = None,
    ) -> PlacesMatchResult:
        """
        Validate place name against Google Places using Text Search API.

        Args:
            name: Place name to validate
            location: Optional location/address context appended to query
            location_bias: Optional {"lat": float, "lng": float} passed as
                circle locationbias to pin results to the right area

        Returns:
            PlacesMatchResult with match quality and details

        """
        config = get_config()
        places_config = config.external_services.google_places

        query = f"{name} {location}".strip() if location else name
        fields = ",".join(places_config.request_fields)
        params: dict[str, str] = {
            "input": query,
            "inputtype": "textquery",
            "fields": fields,
            "key": self.api_key,
            "region": places_config.default_region,
        }
        if location_bias:
            params["locationbias"] = (
                f"circle:50000@{location_bias['lat']},{location_bias['lng']}"
            )

        async with httpx.AsyncClient() as client:
            try:
                response = await client.get(
                    places_config.base_url,
                    params=params,
                    timeout=places_config.timeout_seconds,
                )
                response.raise_for_status()
            except (httpx.HTTPError, httpx.TimeoutException) as e:
                raise RuntimeError(f"Google Places API error: {e}") from e

        data = response.json()

        if not data.get("candidates"):
            return PlacesMatchResult(match_quality=PlacesMatchQuality.NONE)

        first_match = data["candidates"][0]
        matched_name = first_match.get("name", "")
        geometry = first_match.get("geometry", {})
        location_data = geometry.get("location", {})

        core_candidate = _normalize(name)
        core_google = _normalize(matched_name)

        similarity = difflib.SequenceMatcher(None, core_candidate, core_google).ratio()

        if similarity >= 0.85:
            match_quality = PlacesMatchQuality.EXACT
        elif similarity >= 0.70:
            match_quality = PlacesMatchQuality.FUZZY
        elif similarity >= 0.35:
            # Google found a result but the names diverge significantly.
            # Ratios below 0.35 indicate the candidate and result share almost
            # no token structure (e.g. "xyzzy" vs "fyzz gastropub" → 0.32) and
            # should be classified NONE, not CATEGORY_ONLY.
            match_quality = PlacesMatchQuality.CATEGORY_ONLY
        else:
            match_quality = PlacesMatchQuality.NONE

        return PlacesMatchResult(
            match_quality=match_quality,
            validated_name=matched_name,
            external_provider="google",
            external_id=first_match.get("place_id"),
            lat=location_data.get("lat"),
            lng=location_data.get("lng"),
            address=first_match.get("formatted_address"),
            place_types=first_match.get("types", []),
        )

    async def validate_places(
        self,
        names: list[str],
        location_bias: dict[str, float] | None = None,
    ) -> list[PlaceObject]:
        """Validate a list of place names in parallel, return confirmed PlaceObjects.

        Runs one validate_place call per name concurrently. NONE/CATEGORY_ONLY
        matches are dropped. Failures are silently skipped.
        """
        from uuid import uuid4

        async def _validate_one(name: str) -> PlaceObject | None:
            try:
                result = await self.validate_place(name, location_bias=location_bias)
                if result.match_quality not in (
                    PlacesMatchQuality.EXACT, PlacesMatchQuality.FUZZY
                ):
                    return None
                provider_id = (
                    f"{result.external_provider}:{result.external_id}"
                    if result.external_id
                    else None
                )
                place_type = google_types_to_place_type(result.place_types or [])
                return PlaceObject(
                    place_id=str(uuid4()),
                    place_name=result.validated_name or name,
                    place_type=place_type,
                    attributes=PlaceAttributes(),
                    provider_id=provider_id,
                    lat=result.lat,
                    lng=result.lng,
                    address=result.address,
                    geo_fresh=result.lat is not None and result.lng is not None,
                )
            except Exception:
                return None

        results = await asyncio.gather(*[_validate_one(n) for n in names])
        validated = [p for p in results if p is not None]

        # locationbias is a soft hint — Google freely returns global matches
        # when no in-circle result exists (e.g. asking for "Menya Ramen House"
        # near Magdeburg returns the famous London location). Drop anything
        # outside the same radius the bias circle was built with.
        if location_bias is not None:
            return [
                p
                for p in validated
                if p.lat is not None
                and p.lng is not None
                and _haversine_km(
                    p.lat, p.lng, location_bias["lat"], location_bias["lng"]
                )
                <= _LOCATION_BIAS_RADIUS_KM
            ]
        return validated

    async def discover(
        self, search_location: dict[str, float], filters: dict[str, Any]
    ) -> list[dict[str, Any]]:
        """
        Discover places near a location using Google Places Nearby Search.

        Args:
            search_location: {"lat": float, "lng": float}
            filters: {"opennow": bool, "type": str, "keyword": str, ...}

        Returns:
            List of place result dicts from Google Places API

        """
        config = get_config()
        places_config = config.external_services.google_places

        # Build request parameters
        params: dict[str, Any] = {
            "location": f"{search_location['lat']},{search_location['lng']}",
            "key": self.api_key,
        }

        # Add radius — fallback to config default if not in filters
        params["radius"] = (
            filters.get("radius") or get_config().consult.default_radius_m
        )

        # Add open_now if present
        if filters.get("opennow"):
            params["opennow"] = "true"

        # Add type if present
        if "type" in filters:
            params["type"] = filters["type"]

        # Add keyword if present
        if "keyword" in filters:
            params["keyword"] = filters["keyword"]

        async with httpx.AsyncClient() as client:
            try:
                response = await client.get(
                    places_config.nearbysearch_url,
                    params=params,
                    timeout=places_config.timeout_seconds,
                )
                response.raise_for_status()
            except (httpx.HTTPError, httpx.TimeoutException) as e:
                raise RuntimeError(f"Google Places Nearby Search API error: {e}") from e

        data = response.json()
        results = data.get("results", [])
        return cast(list[dict[str, Any]], results)

    async def geocode(
        self,
        place_name: str,
        location_bias: dict[str, float] | None = None,
    ) -> dict[str, float] | None:
        """Resolve a place name to coordinates using the Places Text Search API.

        Reuses the findplacefromtext endpoint — same URL and auth as validate_place.
        When location_bias is provided, results are biased toward that area
        (e.g., "Sukhumvit" resolves to Bangkok instead of Rayong).
        Returns {'lat': float, 'lng': float} or None on failure or no results.
        """
        config = get_config()
        places_config = config.external_services.google_places

        params: dict[str, Any] = {
            "input": place_name,
            "inputtype": "textquery",
            "fields": "geometry",
            "key": self.api_key,
            "region": places_config.default_region,
        }
        if location_bias:
            params["locationbias"] = (
                f"circle:50000@{location_bias['lat']},{location_bias['lng']}"
            )

        try:
            async with httpx.AsyncClient() as client:
                response = await client.get(
                    places_config.base_url,
                    params=params,
                    timeout=places_config.timeout_seconds,
                )
                response.raise_for_status()
        except (httpx.HTTPError, httpx.TimeoutException):
            return None

        data = response.json()
        candidates = data.get("candidates")
        if not candidates:
            return None

        location = candidates[0].get("geometry", {}).get("location", {})
        lat = location.get("lat")
        lng = location.get("lng")
        if lat is None or lng is None:
            return None

        return {"lat": float(lat), "lng": float(lng)}

    async def reverse_geocode(self, lat: float, lng: float) -> str | None:
        """Resolve coords to "<locality>, <country>" via Google Geocoding API.

        Returns None on HTTP failure, status != "OK", or when the response
        carries neither a locality nor an administrative_area_level_1.
        Falls back to admin_area_level_1 (state/region) when locality is
        absent — useful for rural coords that don't fall inside a city.
        """
        params: dict[str, Any] = {
            "latlng": f"{lat},{lng}",
            "key": self.api_key,
            "result_type": "locality|administrative_area_level_1|country",
        }
        try:
            async with httpx.AsyncClient() as client:
                response = await client.get(
                    _GOOGLE_GEOCODE_URL,
                    params=params,
                    timeout=get_config().external_services.google_places.timeout_seconds,
                )
                response.raise_for_status()
        except (httpx.HTTPError, httpx.TimeoutException):
            logger.warning("reverse_geocode failed for %s,%s", lat, lng)
            return None

        data = response.json()
        if data.get("status") != "OK":
            return None
        results = data.get("results") or []
        if not results:
            return None

        components = results[0].get("address_components") or []
        locality: str | None = None
        admin_area: str | None = None
        country: str | None = None
        for c in components:
            types = c.get("types") or []
            if "locality" in types and locality is None:
                locality = c.get("long_name")
            elif "administrative_area_level_1" in types and admin_area is None:
                admin_area = c.get("long_name")
            elif "country" in types and country is None:
                country = c.get("long_name")

        city = locality or admin_area
        if city is None and country is None:
            return None
        if city and country:
            return f"{city}, {country}"
        return city or country

    async def validate(self, candidate: Any, filters: dict[str, Any]) -> bool:
        """
        Validate a candidate place against filter constraints.

        Currently supports: opennow constraint via Nearby Search.

        Args:
            candidate: Candidate object with lat, lng, place_id, place_name
            filters: {"opennow": bool, ...}

        Returns:
            True if candidate passes all constraints, False otherwise

        """
        # If no constraints, candidate passes
        if not filters:
            return True

        # If opennow is required, re-query the Nearby Search to verify
        if filters.get("opennow"):
            results = await self.discover(
                {"lat": candidate.lat, "lng": candidate.lng},
                {"opennow": True, "keyword": candidate.place_name},
            )
            # Check if this place_id appears in the opennow results
            return any(r.get("place_id") == candidate.place_id for r in results)

        return True

    async def get_place_details(self, external_id: str) -> dict[str, Any] | None:
        """Fetch full place details from Google Places API v1 (feature 019).

        One HTTP GET to `https://places.googleapis.com/v1/places/{placeId}`
        with a single field mask that covers BOTH tier 2 geo data AND
        tier 3 enrichment data:

            location, formattedAddress               (tier 2)
            regularOpeningHours, internationalPhoneNumber,
            rating, photos, userRatingCount,
            utcOffsetMinutes, timeZone               (tier 3)

        One API call, two cache writes — PlacesService.enrich_batch splits
        the returned dict into GeoData and PlaceEnrichment locally via
        `_map_provider_response`, then writes both Redis tiers in one
        pipeline each.

        Called by PlacesService.enrich_batch when a cache miss occurs in
        consult mode. On any HTTP / JSON / mapping failure, returns None —
        the caller treats per-place failures as "skip this place this call"
        per FR-026c / ADR-054.
        """
        # Places API v1 field mask — required header.
        field_mask = ",".join(
            [
                "location",
                "formattedAddress",
                "regularOpeningHours",
                "internationalPhoneNumber",
                "rating",
                "photos",
                "userRatingCount",
                "utcOffsetMinutes",
                "timeZone",
            ]
        )
        url = f"https://places.googleapis.com/v1/places/{external_id}"
        headers = {
            "X-Goog-Api-Key": self.api_key,
            "X-Goog-FieldMask": field_mask,
        }
        # `languageCode=en` forces English `formattedAddress` values.
        # Without it, the v1 Places API infers locale from server IP and
        # can return addresses in the wrong language (e.g. "Japonya" /
        # "Tayland" for Japan / Thailand when the server geolocates to
        # Turkey). English is the product default; swap to a user pref
        # if the product grows a locale dimension.
        params = {"languageCode": "en"}

        async with httpx.AsyncClient() as client:
            try:
                response = await client.get(
                    url,
                    headers=headers,
                    params=params,
                    timeout=get_config().external_services.google_places.timeout_seconds,
                )
                response.raise_for_status()
            except httpx.HTTPStatusError as exc:
                # 4xx/5xx with a response body — surface the status and a
                # prefix of the body so the operator can diagnose
                # misconfiguration (disabled API, expired key, quota,
                # region block) without having to reproduce the call.
                logger.warning(
                    "places.get_place_details.http_error",
                    extra={
                        "provider_id": external_id,
                        "status_code": exc.response.status_code,
                        "error_body": exc.response.text[:200],
                    },
                )
                return None
            except (httpx.HTTPError, httpx.TimeoutException) as exc:
                # Network-level failures (connection refused, DNS, timeout)
                # — no response body to log, just the exception message.
                logger.warning(
                    "places.get_place_details.request_failed",
                    extra={
                        "provider_id": external_id,
                        "error": str(exc),
                    },
                )
                return None

        try:
            result = response.json()
        except ValueError:
            return None

        if not isinstance(result, dict):
            return None

        # --- Tier 2: geo ----------------------------------------------------
        location = result.get("location") or {}
        lat = location.get("latitude") if isinstance(location, dict) else None
        lng = location.get("longitude") if isinstance(location, dict) else None
        address = result.get("formattedAddress")

        # --- Tier 3: enrichment --------------------------------------------
        hours = _map_opening_hours(result)
        rating = result.get("rating")
        phone = result.get("internationalPhoneNumber")

        # Photo: Places API v1 photos carry a `name` (resource path). Build
        # the media URL that resolves to the actual image on request. We
        # don't pre-fetch the image — callers load it lazily.
        photo_url: str | None = None
        photos = result.get("photos") or []
        if photos and isinstance(photos, list):
            photo_name = photos[0].get("name") if isinstance(photos[0], dict) else None
            if photo_name:
                photo_url = (
                    f"https://places.googleapis.com/v1/{photo_name}/media"
                    f"?maxWidthPx=400&key={self.api_key}"
                )

        # Popularity: v1 returns `userRatingCount`. Normalize via log10 so
        # the value is bounded to [0, 1]. log10(1)=0, log10(10000)=4.
        popularity: float | None = None
        user_rating_count = result.get("userRatingCount")
        if isinstance(user_rating_count, int) and user_rating_count > 0:
            from math import log10

            popularity = min(1.0, log10(user_rating_count + 1) / 4.0)

        return {
            "lat": lat,
            "lng": lng,
            "address": address,
            "hours": hours,
            "rating": rating,
            "phone": phone,
            "photo_url": photo_url,
            "popularity": popularity,
        }
