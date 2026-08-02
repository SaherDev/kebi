"""Geocoding boundary — protocol, result shape, error type.

`GeocoderProtocol` is the seam between the app and whichever geocoding
provider is wired in (Google today). It is deliberately dumb transport:
turn a query into coordinates + address signals, or coordinates into
names. Verification policy — round-trip checks, refuse-don't-substitute
(ADR-126), entity persistence — lives in `AreaService`, never here.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from pydantic import BaseModel


class GeocodingError(RuntimeError):
    """Raised on a transport- or HTTP-level geocoding failure.

    Callers treat this as "could not pin the location" and degrade (ask
    the user, fail open to a fallback) rather than crashing the turn.
    """


class GeocodeResult(BaseModel):
    """A geocoded place — coordinates plus signals reused from the response.

    `place_type` is the provider's classification of the matched feature
    (Google: `locality`, `administrative_area_level_1`, `country`, …) —
    the signal `AreaService` verifies against and the density proxy for
    ADR-084 radius scaling. `bbox` is the feature's bounding box /
    viewport, `[min_lat, max_lat, min_lng, max_lng]`. `provider_id` is
    the namespaced provider identity of the matched feature when the
    provider supplies one (`google:<place_id>`), recorded on persisted
    area entities for dedup/debug.
    """

    lat: float
    lng: float
    # The provider's display name for the matched feature itself ("Hội An",
    # "Hà Giang"). Forward lookups set it; reverse lookups (which match a
    # street address, not a named feature) leave it None. The round-trip
    # verifier compares the asked-for name against this AND `city`.
    name: str | None = None
    country: str | None = None
    # ISO-3166 alpha-2 (lowercased) — the code canonical geo keys need
    # (ADR-121). Free in the same response.
    country_code: str | None = None
    city: str | None = None
    neighborhood: str | None = None
    place_type: str | None = None
    bbox: list[float] | None = None
    provider_id: str | None = None


@runtime_checkable
class GeocoderProtocol(Protocol):
    """Minimal async geocoding surface."""

    async def search_area(
        self, *, query: str, region_code: str | None = None
    ) -> GeocodeResult | None:
        """Resolve a named area/place query to a `GeocodeResult`.

        `region_code` (ISO-3166 alpha-2) biases and constrains the search
        to one country when the caller knows it. Returns `None` on no
        match. The caller owns verification — this method never promises
        the match *is* the asked-for entity.
        """
        ...

    async def geocode_place_id(self, place_id: str) -> GeocodeResult | None:
        """Re-geocode a stored provider place ID (namespaced or bare).

        The geometry-refresh path: place IDs are storable indefinitely
        under provider ToS while coordinates are not, so a stale entity
        refreshes its geometry through its ID. Returns `None` when the ID
        no longer resolves.
        """
        ...

    async def reverse(self, *, lat: float, lng: float) -> GeocodeResult | None:
        """Resolve a coordinate to named places (country/city/neighborhood).

        Returns `None` when country or city cannot be determined. The
        returned `lat`/`lng` echo the input — the caller's GPS is
        canonical.
        """
        ...
