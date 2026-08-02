"""No-op geocoder — used when GOOGLE_API_KEY is unset.

Lets local dev and tests run without a key. Every lookup logs at debug
level and resolves to "no match"; callers already treat `None` as
"could not pin the location" and degrade.
"""

from __future__ import annotations

import logging

from .protocol import GeocodeResult

logger = logging.getLogger(__name__)


class NullGeocoder:
    """Drop-in no-op adapter. Honors the GeocoderProtocol surface."""

    async def search_area(
        self, *, query: str, region_code: str | None = None
    ) -> GeocodeResult | None:
        logger.debug("null_geocoder_search_area")
        return None

    async def geocode_place_id(self, place_id: str) -> GeocodeResult | None:
        logger.debug("null_geocoder_place_id")
        return None

    async def reverse(self, *, lat: float, lng: float) -> GeocodeResult | None:
        logger.debug("null_geocoder_reverse")
        return None
